from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Optional


@dataclass
class Session:
    session_id: str
    topic_id: int
    pane_id: str
    claude_pid: int
    cwd: str
    topic_name: str
    created_at: float
    last_event_at: float
    status: str = "active"  # active | ended

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        return cls(**d)


@dataclass
class RegistryData:
    version: int = 1
    tmux_server_pid: int = 0
    sessions: Dict[str, Session] = field(default_factory=dict)
    topics: Dict[str, str] = field(default_factory=dict)  # topic_id (str) -> session_id


class Registry:
    """Thread-safe JSON-backed session registry. Atomic writes."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._data = RegistryData()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._save_locked()
            return
        try:
            raw = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            self._save_locked()
            return
        sessions = {
            sid: Session.from_dict(s) for sid, s in raw.get("sessions", {}).items()
        }
        self._data = RegistryData(
            version=raw.get("version", 1),
            tmux_server_pid=raw.get("tmux_server_pid", 0),
            sessions=sessions,
            topics={str(k): v for k, v in raw.get("topics", {}).items()},
        )

    def _save_locked(self) -> None:
        out = {
            "version": self._data.version,
            "tmux_server_pid": self._data.tmux_server_pid,
            "sessions": {sid: s.to_dict() for sid, s in self._data.sessions.items()},
            "topics": self._data.topics,
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(out, indent=2, ensure_ascii=False))
        os.replace(tmp, self.path)

    def bootstrap(self, current_tmux_pid: int) -> None:
        """Reconcile active sessions with reality at daemon start.

        For each active session: keep it active iff the tmux pane still exists
        AND the recorded claude_pid is still alive. Else mark ended.

        Lazy imports of tmux/os to avoid a circular dependency at module load.
        """
        import os as _os

        from . import tmux as _tmux  # local import to avoid cycles

        def _alive(pid: int) -> bool:
            if pid <= 0:
                return False
            try:
                _os.kill(pid, 0)
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            except OSError:
                return False

        with self._lock:
            self._data.tmux_server_pid = current_tmux_pid
            for s in self._data.sessions.values():
                if s.status != "active":
                    continue
                pane_ok = _tmux.pane_exists(s.pane_id)
                pid_ok = _alive(s.claude_pid)
                if not (pane_ok and pid_ok):
                    s.status = "ended"
            self._save_locked()

    def get(self, session_id: str) -> Optional[Session]:
        with self._lock:
            return self._data.sessions.get(session_id)

    def by_topic(self, topic_id: int) -> Optional[Session]:
        with self._lock:
            sid = self._data.topics.get(str(topic_id))
            return self._data.sessions.get(sid) if sid else None

    def upsert(self, sess: Session) -> None:
        with self._lock:
            self._data.sessions[sess.session_id] = sess
            self._data.topics[str(sess.topic_id)] = sess.session_id
            self._save_locked()

    def touch(self, session_id: str) -> None:
        with self._lock:
            s = self._data.sessions.get(session_id)
            if s:
                s.last_event_at = time.time()
                self._save_locked()

    def mark_ended(self, session_id: str) -> None:
        with self._lock:
            s = self._data.sessions.get(session_id)
            if s and s.status != "ended":
                s.status = "ended"
                self._save_locked()

    def all_active(self) -> list[Session]:
        with self._lock:
            return [s for s in self._data.sessions.values() if s.status == "active"]
