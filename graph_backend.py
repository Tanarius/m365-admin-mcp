"""The real Microsoft 365 tenant, used when MODE=graph.

This backend answers the same four questions as mock_backend.py, but by
calling Microsoft Graph. PowerShell comparison: __init__ is Connect-MgGraph
(sign in as the app registration), and each method below is one
Invoke-MgGraphRequest -Method GET. There is no POST, PATCH or DELETE anywhere
in this file: it can only read.
"""

import base64
import json
import re
from datetime import datetime
from urllib.parse import quote

import httpx
import msal

import config

GRAPH = "https://graph.microsoft.com/v1.0"
GUID = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")


class GraphError(Exception):
    """A Graph problem, already translated into a plain-English message."""


def tenant_of(token: str) -> str:
    """Read which tenant issued an access token.

    A token is three base64 chunks joined by dots; the middle one is JSON, and
    its "tid" field is the tenant ID. We only read it. Checking the token's
    signature is Microsoft's job when we present the token to Graph.
    """
    middle = token.split(".")[1]
    middle += "=" * (-len(middle) % 4)  # base64 needs its length padded to a multiple of 4
    return json.loads(base64.urlsafe_b64decode(middle)).get("tid", "")


class GraphBackend:
    """Reads users, licenses and sign-in reports from Microsoft Graph."""

    def __init__(self, http_client: httpx.Client | None = None, get_token=None):
        """Sign in as the app registration and prove we are in the right tenant.

        The two arguments exist so tests can plug in a fake network and a fake
        token. In normal use both are left empty.
        """
        if not GUID.match(config.TENANT_ID):
            raise GraphError("TENANT_ID in .env must be the tenant's ID (a GUID), not a domain name.")
        if get_token is None:
            if not (config.CLIENT_ID and config.CLIENT_SECRET):
                raise GraphError("MODE=graph needs TENANT_ID, CLIENT_ID and CLIENT_SECRET in .env.")
            self._app = msal.ConfidentialClientApplication(
                config.CLIENT_ID,
                authority=f"https://login.microsoftonline.com/{config.TENANT_ID}",
                client_credential=config.CLIENT_SECRET,
            )
        self._get_token = get_token or self._token_from_microsoft
        self._http = http_client or httpx.Client(timeout=30)
        self._token()  # fail now, at startup, if sign-in or the tenant lock fails

    def _token_from_microsoft(self) -> str:
        """Ask Microsoft for an access token (client credentials flow).

        msal keeps the token in memory and only fetches a new one when the old
        one is about to expire, so calling this before every request is cheap.
        """
        result = self._app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in result:
            raise GraphError(f"Could not sign in to Microsoft: {result.get('error_description', result)}")
        return result["access_token"]

    def _token(self) -> str:
        """Get a token and enforce the tenant lock on it."""
        token = self._get_token()
        actual = tenant_of(token)
        if actual.lower() != config.TENANT_ID.lower():
            raise GraphError(
                f"Tenant lock: the token is for tenant {actual}, but .env allows only {config.TENANT_ID}. Refusing to run."
            )
        return token

    def _get(self, url: str, params: dict | None, needs: str) -> dict | None:
        """Send one GET to Graph and turn failures into plain messages.

        `needs` is the permission this call relies on, so a 403 can name it.
        Returns None for a 404 (the thing asked for does not exist).
        """
        response = self._http.get(url, params=params, headers={"Authorization": f"Bearer {self._token()}"})
        if response.status_code == 200:
            return response.json()
        if response.status_code == 404:
            return None
        if "premium license" in response.text or "NonPremiumTenant" in response.text:
            raise GraphError(
                "This tool needs an Entra ID P1 (or higher) license in the tenant. "
                "Microsoft only provides sign-in and MFA registration data to premium tenants."
            )
        if response.status_code == 403:
            raise GraphError(
                f"Microsoft Graph refused this call (403). The app registration is missing the application "
                f"permission {needs}, or admin consent has not been granted for it."
            )
        if response.status_code == 401:
            raise GraphError("Microsoft Graph rejected the token (401). Check CLIENT_ID and CLIENT_SECRET in .env.")
        if response.status_code == 429:
            raise GraphError("Microsoft Graph is throttling requests (429). Wait a minute and try again.")
        raise GraphError(f"Microsoft Graph returned an error ({response.status_code}): {response.text[:200]}")

    def _get_rows(self, path: str, params: dict | None, needs: str) -> list[dict]:
        """Collect rows from a list endpoint, following Graph's paging.

        Graph sends big lists one page at a time, with an @odata.nextLink URL
        pointing at the next page. We stop one row past the cap: enough for the
        tool to say "truncated" without downloading the whole directory.
        """
        enough = config.MAX_ROWS + 1
        rows: list[dict] = []
        url, page_params = f"{GRAPH}{path}", params
        while url and len(rows) < enough:
            page = self._get(url, page_params, needs) or {}
            rows.extend(page.get("value", []))
            url, page_params = page.get("@odata.nextLink"), None  # the next link already includes the query
        return rows[:enough]

    def get_user(self, upn: str) -> dict | None:
        """Fetch one user, then their groups and licenses. Three GETs."""
        fields = "id,displayName,userPrincipalName,accountEnabled,jobTitle,department"
        user = self._get(f"{GRAPH}/users/{quote(upn)}", {"$select": fields}, "User.Read.All")
        if user is None:
            return None
        member_of = self._get_rows(f"/users/{user['id']}/memberOf", {"$select": "displayName"}, "GroupMember.Read.All")
        licenses = self._get_rows(f"/users/{user['id']}/licenseDetails", {"$select": "skuPartNumber"}, "User.Read.All")
        # memberOf also lists directory roles; keep only real groups.
        groups = [item for item in member_of if item.get("@odata.type") == "#microsoft.graph.group"]
        return {"user": user, "groups": groups, "licenses": licenses}

    def subscribed_skus(self) -> list[dict]:
        """Every license the tenant owns."""
        return self._get_rows("/subscribedSkus", None, "Organization.Read.All")

    def users_without_mfa(self) -> list[dict]:
        """Users with no MFA method registered, from the registration report."""
        return self._get_rows(
            "/reports/authenticationMethods/userRegistrationDetails",
            {"$filter": "isMfaRegistered eq false"},
            "AuditLog.Read.All",
        )

    def inactive_users(self, cutoff: datetime) -> list[dict]:
        """Users whose last sign-in was on or before `cutoff`."""
        cutoff_text = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        return self._get_rows(
            "/users",
            {
                "$filter": f"signInActivity/lastSignInDateTime le {cutoff_text}",
                "$select": "displayName,userPrincipalName,signInActivity",
            },
            "User.Read.All and AuditLog.Read.All",
        )
