"""Common helpers for Claude Code hook scripts.

Hooks run as short-lived child processes invoked by Claude Code. They must:
  - exit 0 quickly when not in a tmux session OR bridge is not enabled
  - fail-open if the daemon is unreachable (never block the agent loop)
  - communicate with the daemon via loopback HTTP
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

import tomli

# Add project root to sys.path so `from bridge.config` works when hooks
# are launched via /usr/bin/env python3 hooks/foo.py — but we deliberately
# keep this module's imports tiny to start fast.


CONFIG_PATH = Path(os.environ.get("CLAUDE_TG_CONFIG", str(Path.home() / "claude-tg-bridge" / "config.toml")))


def gate() -> Optional[str]:
    """Return the tmux pane id, or None if hook should silently no-op."""
    if os.environ.get("CLAUDE_TG_BRIDGE") != "1":
        return None
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None
    return pane


def read_payload() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def load_min_config() -> dict:
    """Load just the bits hooks need (host/port/secret/approval). Returns {} on failure."""
    try:
        with open(CONFIG_PATH, "rb") as f:
            return tomli.load(f)
    except Exception:
        return {}


def post(path: str, body: dict, timeout: float) -> Optional[dict]:
    """POST to the daemon. Returns parsed JSON, or None on any failure."""
    cfg = load_min_config()
    ipc = cfg.get("ipc", {})
    host = ipc.get("host", "127.0.0.1")
    port = int(ipc.get("port", 8765))
    secret = ipc.get("shared_secret", "")
    if not secret:
        return None
    try:
        # Use stdlib only to keep hooks self-contained (no venv activation needed)
        import urllib.request
        import urllib.error
        req = urllib.request.Request(
            f"http://{host}:{port}{path}",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Bridge-Secret": secret,
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            if not data:
                return {}
            return json.loads(data.decode("utf-8"))
    except Exception:
        return None


def emit_decision(decision: str) -> None:
    """Print a PreToolUse hookSpecificOutput JSON block and exit 0."""
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
        }
    }
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def approval_config() -> dict:
    return load_min_config().get("approval", {}) or {}
