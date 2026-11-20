"""The MCP server: four read-only Microsoft 365 admin tools for Claude.

PowerShell comparison: this file is the module, and each @mcp.tool() function
is one cmdlet. Claude reads the docstrings (like Get-Help text) to decide
which tool answers the question it was asked.
"""

from datetime import datetime, timedelta, timezone

# MCPServer is the class that turns plain Python functions into MCP tools.
# (In version 1 of the SDK it was called FastMCP. Same idea, new name.)
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

import config
from guardrails import audited, cap_rows, trim
from mock_backend import MockBackend

mcp = MCPServer("m365-admin")

# The only user fields Claude is ever shown. No phone numbers, no addresses.
USER_FIELDS = ["displayName", "userPrincipalName", "accountEnabled", "jobTitle", "department"]
MFA_FIELDS = ["userPrincipalName", "userDisplayName", "methodsRegistered"]

# Hints sent to the MCP client with every tool. They tell Claude (and the app
# showing you the tool) that these tools only read, never delete, give the same
# answer when repeated, and reach outside this machine (to Microsoft Graph).
# They are hints, not enforcement: the read-only Entra permissions do that.
READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)

_backend = None  # created on first use, then reused


def get_backend():
    """Return the data source chosen by MODE in .env (mock or graph)."""
    global _backend
    if _backend is None:
        if config.MODE == "mock":
            _backend = MockBackend()
        elif config.MODE == "graph":
            from graph_backend import GraphBackend  # imported here so mock mode never needs Microsoft

            _backend = GraphBackend()
        else:
            raise RuntimeError(f"MODE must be 'mock' or 'graph' in .env, but it is '{config.MODE}'.")
    return _backend


@mcp.tool(annotations=READ_ONLY)
@audited
def get_user(upn: str) -> dict:
    """Look up one Microsoft 365 user by their sign-in email address (UPN).

    Use this when asked about a specific person, for example
    "tell me about jane@contoso.com". Returns their name, whether the account
    is enabled, job title, department, group names, and license SKUs.
    Read-only. Never returns phone numbers or addresses.
    """
    upn = upn.strip()
    if "@" not in upn:
        raise ValueError(f"'{upn}' is not a UPN. Pass a full sign-in address like jane@contoso.com.")
    record = get_backend().get_user(upn)
    if record is None:
        return {"found": False, "message": f"No user found with UPN {upn}."}
    user = trim(record["user"], USER_FIELDS)
    user["groups"] = [group["displayName"] for group in record["groups"]]
    user["licenses"] = [license["skuPartNumber"] for license in record["licenses"]]
    return {"found": True, "user": user}


@mcp.tool(annotations=READ_ONLY)
@audited
def license_summary() -> dict:
    """Summarize the tenant's Microsoft 365 licenses: owned, assigned, and still available.

    Use this for questions like "how many E3 licenses are left?" or
    "are we out of licenses?". One row per license SKU (for example SPE_E3 is
    Microsoft 365 E3). Read-only.
    """
    rows = []
    for sku in get_backend().subscribed_skus():
        enabled = sku["prepaidUnits"]["enabled"]
        consumed = sku["consumedUnits"]
        rows.append(
            {
                "skuPartNumber": sku["skuPartNumber"],
                "enabled": enabled,
                "consumed": consumed,
                "available": enabled - consumed,
            }
        )
    return cap_rows(rows)


@mcp.tool(annotations=READ_ONLY)
@audited
def list_users_without_mfa() -> dict:
    """List users who have not registered any multifactor authentication (MFA) method.

    Use this for questions like "who isn't set up for MFA?". Returns each
    user's UPN, display name, and registered methods. At most 25 users; the
    result says if there are more. Read-only. On a real tenant this needs an
    Entra ID P1 license.
    """
    rows = [trim(row, MFA_FIELDS) for row in get_backend().users_without_mfa()]
    return cap_rows(rows)


@mcp.tool(annotations=READ_ONLY)
@audited
def find_inactive_users(days: int = 90) -> dict:
    """Find users who have not signed in for at least `days` days (default 90).

    Use this for questions like "who hasn't signed in for 90 days?" or
    "which accounts look stale?". Returns display name, UPN, and last sign-in
    time. Users who have never signed in are not included. At most 25 users;
    the result says if there are more. Read-only. On a real tenant this needs
    an Entra ID P1 license.
    """
    if not 1 <= days <= 3650:
        raise ValueError("days must be between 1 and 3650.")
    backend = get_backend()
    now = getattr(backend, "now", None) or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    rows = [
        {
            "displayName": user["displayName"],
            "userPrincipalName": user["userPrincipalName"],
            "lastSignInDateTime": user["signInActivity"]["lastSignInDateTime"],
        }
        for user in backend.inactive_users(cutoff)
    ]
    return cap_rows(rows)


if __name__ == "__main__":
    get_backend()  # connect first, so a bad .env or the tenant lock stops the server before it starts
    mcp.run()  # stdio: talks to Claude through this program's input and output
