from __future__ import annotations

import os
import shutil
import subprocess
from typing import Optional


TMUX_BIN = shutil.which("tmux") or "/opt/homebrew/bin/tmux"


def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        [TMUX_BIN, *args],
        capture_output=True,
        text=True,
        check=check,
    )


def server_pid() -> int:
    """Return tmux server PID, or 0 if no server running."""
    try:
        r = _run(["display-message", "-p", "#{pid}"], check=False)
    except FileNotFoundError:
        return 0
    if r.returncode != 0:
        return 0
    try:
        return int(r.stdout.strip())
    except ValueError:
        return 0


def pane_exists(pane_id: str) -> bool:
    if not pane_id:
        return False
    r = _run(["display-message", "-p", "-t", pane_id, "#{pane_id}"], check=False)
    return r.returncode == 0 and r.stdout.strip() == pane_id


def pane_in_copy_mode(pane_id: str) -> bool:
    r = _run(["display-message", "-p", "-t", pane_id, "#{pane_in_mode}"], check=False)
    return r.returncode == 0 and r.stdout.strip() == "1"


def pane_pid(pane_id: str) -> int:
    """PID of the pane's root process (the shell)."""
    r = _run(["display-message", "-p", "-t", pane_id, "#{pane_pid}"], check=False)
    if r.returncode != 0:
        return 0
    try:
        return int(r.stdout.strip())
    except ValueError:
        return 0


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it (different user). Treat as alive.
        return True
    except OSError:
        return False


def _descendant_pids(root_pid: int) -> list[int]:
    """All descendant PIDs of root_pid (via `pgrep -P` walk)."""
    out: list[int] = []
    frontier = [root_pid]
    seen: set[int] = set()
    while frontier:
        parent = frontier.pop()
        if parent in seen:
            continue
        seen.add(parent)
        r = subprocess.run(
            ["pgrep", "-P", str(parent)],
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            continue
        for line in r.stdout.split():
            try:
                pid = int(line)
            except ValueError:
                continue
            out.append(pid)
            frontier.append(pid)
    return out


def claude_pid_in_pane(pane_id: str) -> Optional[int]:
    """Find a `claude`-named process under the given tmux pane.

    Claude Code is a node script, but argv[0] / command name in `ps` is "claude"
    on most installs (the npm shim execs node with the script path).  Falls back
    to scanning all descendants' `ps` command for "claude".
    """
    root = pane_pid(pane_id)
    if not root:
        return None
    candidates = _descendant_pids(root)
    if not candidates:
        return None
    # Query `ps` once for all candidates.
    args = ["ps", "-o", "pid=,comm=", "-p", ",".join(str(p) for p in candidates)]
    r = subprocess.run(args, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        return None
    best: Optional[int] = None
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        comm = parts[1]
        base = os.path.basename(comm)
        if base == "claude" or "claude" in base.lower():
            return pid
        # Fall back to any node child if no claude match — we'd rather pick
        # something than nothing, since macOS sometimes shows "node" for the wrapper.
        if base in ("node",) and best is None:
            best = pid
    return best


def send_paste(pane_id: str, text: str) -> None:
    """Inject `text` into a pane as a single bracketed-paste, then press Enter once.

    Bracketed paste prevents the TUI from interpreting embedded newlines as Enter,
    so multi-line prompts go in atomically.
    """
    # Start bracketed paste
    _run(["send-keys", "-t", pane_id, "--", "\x1b[200~"])
    # Literal payload (-l disables key-name lookup)
    if text:
        _run(["send-keys", "-t", pane_id, "-l", "--", text])
    # End bracketed paste
    _run(["send-keys", "-t", pane_id, "--", "\x1b[201~"])
    # Single submit
    _run(["send-keys", "-t", pane_id, "Enter"])


def session_exists(name: str) -> bool:
    r = _run(["has-session", "-t", name], check=False)
    return r.returncode == 0


def ensure_session(name: str) -> bool:
    """Create a detached tmux session if missing. Returns True if it now exists."""
    if session_exists(name):
        return True
    r = _run(["new-session", "-d", "-s", name], check=False)
    return r.returncode == 0


def new_window(session: str, cwd: str, command: str, window_name: str | None = None) -> str | None:
    """Open a new tmux window in `session` running `command` in `cwd`.

    Returns the new pane id (e.g. "%12") or None on failure.
    """
    args = ["new-window", "-d", "-t", session, "-c", cwd, "-P", "-F", "#{pane_id}"]
    if window_name:
        args += ["-n", window_name]
    args.append(command)
    r = _run(args, check=False)
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def kill_window(pane_id: str) -> bool:
    """Kill the window containing the given pane. Returns True on success."""
    if not pane_id:
        return False
    r = _run(["kill-window", "-t", pane_id], check=False)
    return r.returncode == 0


def send_ctrl(pane_id: str, key: str) -> None:
    """Send a control sequence (e.g. 'C-d', 'C-c', 'Enter') to a pane."""
    _run(["send-keys", "-t", pane_id, key])
