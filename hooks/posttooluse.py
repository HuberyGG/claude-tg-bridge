#!/usr/bin/env python3
"""PostToolUse hook: pushes Bash stdout/stderr back to the phone.

Scoped to tool_name=Bash (configured via the settings.json matcher).
Other tools fall through (exit 0, no-op).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import gate, post, read_payload  # noqa: E402


def main() -> int:
    pane = gate()
    if pane is None:
        return 0

    payload = read_payload()
    tool_name = payload.get("tool_name", "")
    if tool_name != "Bash":
        return 0

    tool_input = payload.get("tool_input", {}) or {}
    tool_response = payload.get("tool_response", {}) or {}

    body = {
        "kind": "tool_result",
        "session_id": payload.get("session_id", ""),
        "transcript_path": payload.get("transcript_path", ""),
        "cwd": payload.get("cwd", ""),
        "pane": pane,
        "extra": {
            "tool_name": "Bash",
            "command": tool_input.get("command", ""),
            "stdout": tool_response.get("stdout", "") or "",
            "stderr": tool_response.get("stderr", "") or "",
            "interrupted": bool(tool_response.get("interrupted", False)),
        },
    }
    post("/event", body, timeout=5.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
