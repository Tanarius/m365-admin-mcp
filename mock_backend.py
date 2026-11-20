"""The fake Microsoft 365 tenant, used when MODE=mock.

A "backend" is the part that fetches data. This one reads mock_users.json and
hands data back in the same shape Microsoft Graph uses, including fields the
tools are NOT allowed to share. That way the trimming in server.py is tested
for real, and swapping in the Graph backend later changes nothing above it.
"""

import json
from datetime import datetime, timedelta, timezone

import config


class MockBackend:
    """Answers the same four questions the Graph backend will, from a JSON file."""

    def __init__(self, now: datetime | None = None):
        """Load the fake tenant. `now` lets tests freeze the clock."""
        self.now = now or datetime.now(timezone.utc)
        with open(config.MOCK_DATA_PATH, encoding="utf-8") as data_file:
            data = json.load(data_file)
        self.users = data["users"]
        self.skus = data["subscribedSkus"]

    def _last_sign_in(self, user: dict) -> str | None:
        """Turn "signed in N days ago" into the ISO date string Graph would send."""
        days_ago = user["lastSignInDaysAgo"]
        if days_ago is None:
            return None  # never signed in
        moment = self.now - timedelta(days=days_ago)
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    def get_user(self, upn: str) -> dict | None:
        """Find one user by UPN. Mirrors three Graph calls: the user, memberOf, licenseDetails."""
        for user in self.users:
            if user["userPrincipalName"].lower() == upn.lower():
                return {
                    "user": user,  # the full record, phone number and all
                    "groups": [{"displayName": name} for name in user["groups"]],
                    "licenses": [{"skuPartNumber": sku} for sku in user["licenses"]],
                }
        return None

    def subscribed_skus(self) -> list[dict]:
        """Every license the tenant owns. Mirrors GET /subscribedSkus."""
        return self.skus

    def users_without_mfa(self) -> list[dict]:
        """Users with no MFA method registered. Mirrors the userRegistrationDetails report."""
        return [
            {
                "id": user["id"],
                "userPrincipalName": user["userPrincipalName"],
                "userDisplayName": user["displayName"],
                "isMfaRegistered": user["isMfaRegistered"],
                "methodsRegistered": user["methodsRegistered"],
            }
            for user in self.users
            if not user["isMfaRegistered"]
        ]

    def inactive_users(self, cutoff: datetime) -> list[dict]:
        """Users whose last sign-in was on or before `cutoff`.

        Like Graph's filter, this skips users who have never signed in at all,
        because they have no sign-in date to compare.
        """
        cutoff_text = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        matches = []
        for user in self.users:
            last_sign_in = self._last_sign_in(user)
            if last_sign_in is not None and last_sign_in <= cutoff_text:
                matches.append(
                    {
                        "id": user["id"],
                        "displayName": user["displayName"],
                        "userPrincipalName": user["userPrincipalName"],
                        "signInActivity": {"lastSignInDateTime": last_sign_in},
                    }
                )
        return matches
