"""The safety controls every tool goes through.

Three of the guardrails live here: field trimming, the row cap, and the audit
log. They are in their own file so you can point at one place and say
"this is what stops the AI from seeing or doing too much."
"""

import functools
import json
from datetime import datetime, timezone

import config


def trim(record: dict, allowed_fields: list[str]) -> dict:
    """Keep only the fields we chose to share, and drop everything else.

    PowerShell comparison: this is Select-Object. Even if Graph (or the mock
    file) hands back a phone number, it is dropped here before Claude sees it.
    """
    return {field: record.get(field) for field in allowed_fields}


def cap_rows(rows: list[dict]) -> dict:
    """Cut a list down to MAX_ROWS and say honestly whether anything was cut.

    PowerShell comparison: Select-Object -First 25, plus a note so Claude
    knows the list is incomplete and can tell the person asking.
    """
    limit = config.MAX_ROWS
    truncated = len(rows) > limit
    result = {
        "count": min(len(rows), limit),
        "truncated": truncated,
        "results": rows[:limit],
    }
    if truncated:
        result["note"] = (
            f"Results were truncated: showing the first {limit} rows only. "
            "There are more matches than this."
        )
    return result


def write_audit_entry(tool: str, inputs: dict, row_count: int, status: str, error: str | None = None) -> None:
    """Append one line to audit.log describing one tool call.

    Each line is a small JSON object, so the log is easy to read by eye and
    easy to load later (PowerShell: Get-Content audit.log | ConvertFrom-Json).
    We log the row COUNT, never the rows, so the log holds no user data.
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": tool,
        "inputs": inputs,
        "row_count": row_count,
        "status": status,
    }
    if error:
        entry["error"] = error
    with open(config.AUDIT_LOG_PATH, "a", encoding="utf-8") as log_file:
        log_file.write(json.dumps(entry) + "\n")


def count_rows(result: dict) -> int:
    """Work out how many rows a tool returned, for the audit log."""
    if "count" in result:
        return result["count"]
    return 1 if result.get("found") else 0


def audited(tool_function):
    """Wrap a tool so every call is logged, whether it works or fails.

    This is a decorator: putting @audited above a function swaps it for the
    `wrapper` below, which runs the real function in the middle. If the tool
    raises an error, we log it and hand Claude a plain message instead of a
    stack trace.
    """

    @functools.wraps(tool_function)
    def wrapper(**inputs):
        name = tool_function.__name__
        try:
            result = tool_function(**inputs)
        except Exception as problem:
            write_audit_entry(name, inputs, 0, "error", str(problem))
            return {"error": str(problem)}
        write_audit_entry(name, inputs, count_rows(result), "success")
        return result

    return wrapper
