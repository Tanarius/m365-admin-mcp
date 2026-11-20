"""Tests for the four tools and the guardrails, all against the mock backend.

Run with: uv run pytest
"""

import json
from datetime import datetime, timezone

import pytest

import config
import server
from mock_backend import MockBackend

FROZEN_NOW = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc)
FORBIDDEN = ["mobilePhone", "streetAddress", "+1 555", "Main St"]


@pytest.fixture(autouse=True)
def mock_tenant(tmp_path, monkeypatch):
    """Give every test a fresh mock backend, a frozen clock, and its own audit log."""
    monkeypatch.setattr(config, "AUDIT_LOG_PATH", tmp_path / "audit.log")
    monkeypatch.setattr(server, "_backend", MockBackend(now=FROZEN_NOW))
    return tmp_path / "audit.log"


def read_audit(path):
    """Load the audit log as a list of dicts, one per line."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ---- get_user ----

def test_get_user_returns_profile_groups_and_licenses():
    result = server.get_user(upn="ben.carter@contoso.com")
    assert result["found"] is True
    assert result["user"] == {
        "displayName": "Ben Carter",
        "userPrincipalName": "ben.carter@contoso.com",
        "accountEnabled": True,
        "jobTitle": "Help Desk Technician",
        "department": "IT",
        "groups": ["Help Desk", "All Staff"],
        "licenses": ["SPE_E3", "FLOW_FREE"],
    }


def test_get_user_is_case_insensitive():
    assert server.get_user(upn="AVA.NGUYEN@contoso.com")["user"]["displayName"] == "Ava Nguyen"


def test_get_user_not_found():
    result = server.get_user(upn="nobody@contoso.com")
    assert result["found"] is False


def test_get_user_rejects_non_upn():
    assert "not a UPN" in server.get_user(upn="ben")["error"]


# ---- license_summary ----

def test_license_summary_counts_available():
    result = server.license_summary()
    by_sku = {row["skuPartNumber"]: row for row in result["results"]}
    assert result["count"] == 3
    assert by_sku["SPE_E3"] == {"skuPartNumber": "SPE_E3", "enabled": 10, "consumed": 6, "available": 4}
    assert by_sku["SPB"]["available"] == 2


# ---- list_users_without_mfa ----

def test_list_users_without_mfa():
    result = server.list_users_without_mfa()
    upns = {row["userPrincipalName"] for row in result["results"]}
    assert upns == {"carla.diaz@contoso.com", "dev.patel@contoso.com", "ivy.chen@contoso.com"}
    assert result["truncated"] is False


# ---- find_inactive_users ----

def test_find_inactive_users_default_90_days():
    result = server.find_inactive_users()
    upns = {row["userPrincipalName"] for row in result["results"]}
    # 120, 400 and 200 days ago. Ivy never signed in, so she is not listed.
    assert upns == {"dev.patel@contoso.com", "frank.osei@contoso.com", "grace.kim@contoso.com"}


def test_find_inactive_users_custom_days():
    result = server.find_inactive_users(days=365)
    assert [row["userPrincipalName"] for row in result["results"]] == ["frank.osei@contoso.com"]
    assert result["results"][0]["lastSignInDateTime"] == "2025-10-12T12:00:00Z"


def test_find_inactive_users_rejects_bad_days():
    assert "days must be" in server.find_inactive_users(days=0)["error"]


# ---- guardrail: row cap ----

def test_row_cap_truncates_and_says_so(monkeypatch):
    monkeypatch.setattr(config, "MAX_ROWS", 2)
    result = server.list_users_without_mfa()  # 3 users match
    assert result["count"] == 2
    assert len(result["results"]) == 2
    assert result["truncated"] is True
    assert "truncated" in result["note"]


def test_row_cap_default_is_25():
    assert config.MAX_ROWS == 25


# ---- guardrail: field trimming ----

def test_no_tool_leaks_personal_fields():
    outputs = [
        server.get_user(upn="ava.nguyen@contoso.com"),
        server.license_summary(),
        server.list_users_without_mfa(),
        server.find_inactive_users(days=1),
    ]
    text = json.dumps(outputs)
    for forbidden in FORBIDDEN:
        assert forbidden not in text


def test_list_tools_return_only_listed_fields():
    mfa_row = server.list_users_without_mfa()["results"][0]
    assert set(mfa_row) == {"userPrincipalName", "userDisplayName", "methodsRegistered"}
    inactive_row = server.find_inactive_users()["results"][0]
    assert set(inactive_row) == {"displayName", "userPrincipalName", "lastSignInDateTime"}


# ---- guardrail: audit log ----

def test_audit_log_records_every_call(mock_tenant):
    server.get_user(upn="ava.nguyen@contoso.com")
    server.find_inactive_users(days=90)
    entries = read_audit(mock_tenant)
    assert [entry["tool"] for entry in entries] == ["get_user", "find_inactive_users"]
    assert entries[0]["inputs"] == {"upn": "ava.nguyen@contoso.com"}
    assert entries[0]["row_count"] == 1
    assert entries[1]["row_count"] == 3
    assert all(entry["status"] == "success" and entry["timestamp"] for entry in entries)


def test_audit_log_records_errors(mock_tenant):
    server.get_user(upn="not-a-upn")
    entry = read_audit(mock_tenant)[0]
    assert entry["status"] == "error"
    assert entry["row_count"] == 0
    assert "not a UPN" in entry["error"]


def test_audit_log_never_contains_user_data(mock_tenant):
    server.list_users_without_mfa()
    assert "carla.diaz" not in mock_tenant.read_text(encoding="utf-8")


# ---- tool annotations ----

def test_every_tool_is_annotated_read_only():
    import asyncio

    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    assert set(tools) == {"get_user", "license_summary", "list_users_without_mfa", "find_inactive_users"}
    for name, tool in tools.items():
        assert tool.annotations is not None, name
        assert tool.annotations.read_only_hint is True, name
        assert tool.annotations.destructive_hint is False, name