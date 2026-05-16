#!/usr/bin/env python3
"""Stop hook: invoked after Claude finishes a turn. Pushes the assistant reply to Telegram."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import gate, read_payload, post  # noqa: E402


def main() -> int:
    pane = gate()
    if pane is None:
        return 0
    payload = read_payload()
    if payload.get("stop_hook_active"):
        return 0
    body = {
        "kind": "stop",
        "session_id": payload.get("session_id", ""),
        "transcript_path": payload.get("transcript_path", ""),
        "cwd": payload.get("cwd", ""),
        "pane": pane,
    }
    post("/event", body, timeout=5.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
