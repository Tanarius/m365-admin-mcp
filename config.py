"""Settings for the server, read once from the .env file.

PowerShell comparison: this is like the param() block at the top of a script.
Everything the rest of the code needs to know about "how am I configured?"
lives here, so there is one place to look.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# The folder this file lives in. Paths below are built from it, so the server
# works no matter which folder Claude Desktop happens to start it from.
PROJECT_DIR = Path(__file__).parent

# Read .env (if it exists) and copy its lines into environment variables.
load_dotenv(PROJECT_DIR / ".env")

# "mock" reads mock_users.json. "graph" calls Microsoft Graph for real.
MODE = os.getenv("MODE", "mock").strip().lower()

# Only used when MODE=graph. They identify your Entra app registration.
# TENANT_ID doubles as the tenant lock: the server refuses any other tenant.
TENANT_ID = os.getenv("TENANT_ID", "").strip()
CLIENT_ID = os.getenv("CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "").strip()

# Guardrail: no tool ever returns more rows than this.
MAX_ROWS = 25

# Guardrail: every tool call is appended to this file.
AUDIT_LOG_PATH = PROJECT_DIR / "audit.log"

# The fake tenant used in mock mode.
MOCK_DATA_PATH = PROJECT_DIR / "mock_users.json"
