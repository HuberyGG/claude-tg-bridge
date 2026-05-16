#!/usr/bin/env python3
"""Notification hook: forwards Claude Code notifications to Telegram."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import gate, read_payload, post  # noqa: E402


def main() -> int:
    pane = gate()
    if pane is None:
        return 0
    payload = read_payload()
    body = {
        "kind": "notification",
        "session_id": payload.get("session_id", ""),
        "transcript_path": payload.get("transcript_path", ""),
        "cwd": payload.get("cwd", ""),
        "pane": pane,
        "extra": {"message": payload.get("message", "")},
    }
    post("/event", body, timeout=5.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
