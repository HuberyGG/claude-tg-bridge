#!/usr/bin/env python3
"""PreToolUse hook: surfaces tool approvals on the phone for configured tools.

Default: only `Bash` is gated. Bash commands matching `auto_allow_bash` regex
list are auto-approved without phone interaction.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    approval_config,
    emit_decision,
    gate,
    post,
    read_payload,
)


def main() -> int:
    pane = gate()
    if pane is None:
        return 0

    payload = read_payload()
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}

    cfg = approval_config()
    gated_tools = cfg.get("tools", ["Bash"])
    if tool_name not in gated_tools:
        return 0

    # Bash auto-allow patterns
    if tool_name == "Bash":
        cmd = tool_input.get("command", "") or ""
        for rx in cfg.get("auto_allow_bash", []) or []:
            try:
                if re.match(rx, cmd):
                    emit_decision("allow")
                    return 0
            except re.error:
                continue

    on_timeout = cfg.get("on_timeout", "deny")
    timeout_seconds = int(cfg.get("timeout_seconds", 240))

    body = {
        "session_id": payload.get("session_id", ""),
        "tool_use_id": payload.get("tool_use_id", ""),
        "tool_name": tool_name,
        "tool_input": tool_input,
        "pane": pane,
        "cwd": payload.get("cwd", ""),
    }
    # Add a small buffer over the server-side timeout so the daemon's own
    # timeout fires first and we get a clean JSON response.
    resp = post("/approve", body, timeout=timeout_seconds + 10)
    if resp is None:
        emit_decision(on_timeout)
        return 0
    decision = resp.get("decision") or on_timeout
    if decision not in ("allow", "deny", "ask"):
        decision = on_timeout
    emit_decision(decision)
    return 0


if __name__ == "__main__":
    sys.exit(main())
