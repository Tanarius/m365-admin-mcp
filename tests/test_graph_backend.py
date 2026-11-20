"""Tests for the Graph backend, using a fake network. No Microsoft account needed.

httpx.MockTransport lets us answer each request ourselves, so we can replay
exactly what Graph sends back for errors and paging.
"""

import base64
import json

import httpx
import pytest

import config
import server
from graph_backend import GraphBackend, GraphError

TENANT = "11111111-1111-1111-1111-111111111111"
OTHER_TENANT = "22222222-2222-2222-2222-222222222222"


def fake_token(tenant_id: str) -> str:
    """Build a token-shaped string whose middle part says which tenant issued it."""
    payload = base64.urlsafe_b64encode(json.dumps({"tid": tenant_id}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def make_backend(handler, token_tenant: str = TENANT) -> GraphBackend:
    """Create a GraphBackend wired to a fake network and a fake token."""
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return GraphBackend(http_client=client, get_token=lambda: fake_token(token_tenant))


@pytest.fixture(autouse=True)
def graph_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TENANT_ID", TENANT)
    monkeypatch.setattr(config, "AUDIT_LOG_PATH", tmp_path / "audit.log")


def page(rows, next_link=None):
    body = {"value": rows}
    if next_link:
        body["@odata.nextLink"] = next_link
    return httpx.Response(200, json=body)


# ---- tenant lock ----

def test_tenant_lock_refuses_wrong_tenant():
    with pytest.raises(GraphError, match="Tenant lock"):
        make_backend(lambda request: page([]), token_tenant=OTHER_TENANT)


def test_tenant_id_must_be_a_guid(monkeypatch):
    monkeypatch.setattr(config, "TENANT_ID", "contoso.onmicrosoft.com")
    with pytest.raises(GraphError, match="GUID"):
        make_backend(lambda request: page([]))


# ---- requests ----

def test_every_request_is_a_get_with_a_bearer_token():
    seen = []

    def handler(request):
        seen.append(request)
        return page([])

    backend = make_backend(handler)
    backend.subscribed_skus()
    backend.users_without_mfa()
    assert all(request.method == "GET" for request in seen)
    assert all(request.headers["Authorization"].startswith("Bearer ") for request in seen)
    assert seen[1].url.params["$filter"] == "isMfaRegistered eq false"


def test_get_user_combines_three_calls_and_keeps_only_groups():
    def handler(request):
        path = request.url.path
        if path.endswith("/memberOf"):
            return page([
                {"@odata.type": "#microsoft.graph.group", "displayName": "Help Desk"},
                {"@odata.type": "#microsoft.graph.directoryRole", "displayName": "Global Reader"},
            ])
        if path.endswith("/licenseDetails"):
            return page([{"skuPartNumber": "SPE_E3"}])
        return httpx.Response(200, json={"id": "abc", "displayName": "Ben Carter", "userPrincipalName": "ben@contoso.com",
                                         "accountEnabled": True, "jobTitle": "Tech", "department": "IT"})

    server._backend = make_backend(handler)
    user = server.get_user(upn="ben@contoso.com")["user"]
    assert user["groups"] == ["Help Desk"]
    assert user["licenses"] == ["SPE_E3"]
    assert "id" not in user


def test_get_user_404_means_not_found():
    server._backend = make_backend(lambda request: httpx.Response(404, json={"error": {"code": "Request_ResourceNotFound"}}))
    assert server.get_user(upn="nobody@contoso.com")["found"] is False


# ---- error handling ----

def test_403_names_the_missing_permission():
    server._backend = make_backend(lambda request: httpx.Response(403, json={"error": {"code": "Authorization_RequestDenied"}}))
    error = server.license_summary()["error"]
    assert "403" in error and "Organization.Read.All" in error


def test_premium_license_error_explains_p1():
    body = {"error": {"code": "Authentication_RequestFromNonPremiumTenantOrB2CTenant",
                      "message": "Neither tenant is B2C or tenant doesn't have premium license"}}
    server._backend = make_backend(lambda request: httpx.Response(403, json=body))
    assert "Entra ID P1" in server.list_users_without_mfa()["error"]
    assert "Entra ID P1" in server.find_inactive_users()["error"]


# ---- paging and the row cap ----

def test_paging_follows_next_link_and_stops_at_the_cap(monkeypatch):
    monkeypatch.setattr(config, "MAX_ROWS", 3)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        number = len(calls)
        rows = [{"userPrincipalName": f"user{number}{letter}@contoso.com", "userDisplayName": "X",
                 "methodsRegistered": [], "isAdmin": False} for letter in "ab"]
        return page(rows, next_link=f"https://graph.microsoft.com/v1.0/next-page-{number}")

    server._backend = make_backend(handler)
    result = server.list_users_without_mfa()
    assert len(calls) == 2  # 2 pages give 4 rows, one past the cap, so page 3 is never fetched
    assert calls[1].endswith("/next-page-1")
    assert result["count"] == 3 and result["truncated"] is True
    assert set(result["results"][0]) == {"userPrincipalName", "userDisplayName", "methodsRegistered"}


def test_inactive_users_flattens_sign_in_activity():
    rows = [{"displayName": "Dev Patel", "userPrincipalName": "dev@contoso.com", "id": "x",
             "signInActivity": {"lastSignInDateTime": "2026-01-01T00:00:00Z", "lastSignInRequestId": "secret-ish"}}]
    server._backend = make_backend(lambda request: page(rows))
    result = server.find_inactive_users(days=90)
    assert result["results"] == [{"displayName": "Dev Patel", "userPrincipalName": "dev@contoso.com",
                                  "lastSignInDateTime": "2026-01-01T00:00:00Z"}]
