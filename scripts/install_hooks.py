#!/usr/bin/env python3
"""Idempotently install hooks into ~/.claude/settings.json.

Preserves any existing `env`, permissions, etc. blocks. Safe to run multiple times.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


SETTINGS = Path.home() / ".claude" / "settings.json"
BRIDGE_ROOT = Path(__file__).resolve().parent.parent
PYTHON = str(BRIDGE_ROOT / ".venv" / "bin" / "python")


def hook_cmd(script: str) -> str:
    return f"{PYTHON} {BRIDGE_ROOT / 'hooks' / script}"


HOOKS = {
    "Stop": [
        {"hooks": [{"type": "command", "command": hook_cmd("stop.py")}]}
    ],
    "Notification": [
        {"hooks": [{"type": "command", "command": hook_cmd("notification.py")}]}
    ],
    "PreToolUse": [
        {
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": hook_cmd("pretooluse.py")}],
        }
    ],
    "PostToolUse": [
        {
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": hook_cmd("posttooluse.py")}],
        }
    ],
}


def main() -> int:
    if not SETTINGS.exists():
        data: dict = {}
    else:
        try:
            data = json.loads(SETTINGS.read_text())
        except json.JSONDecodeError as e:
            print(f"ERROR: {SETTINGS} is not valid JSON: {e}", file=sys.stderr)
            return 1

    existing = data.get("hooks", {}) or {}
    # Replace just our three keys; preserve any other event hooks the user set up.
    for k, v in HOOKS.items():
        existing[k] = v
    data["hooks"] = existing

    # Atomic write
    tmp = SETTINGS.with_suffix(SETTINGS.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(SETTINGS)
    print(f"Updated {SETTINGS}")
    print(f"  Python: {PYTHON}")
    print(f"  Hooks dir: {BRIDGE_ROOT / 'hooks'}")
    print()
    print("Don't forget:")
    print("  1) Add to ~/.tmux.conf:  set-environment -g CLAUDE_TG_BRIDGE 1")
    print("  2) Restart tmux:  tmux kill-server; tmux")
    print("  3) Start daemon: .venv/bin/python -m bridge.daemon")
    return 0


if __name__ == "__main__":
    sys.exit(main())
