"""Scan ~/.claude/projects/ for resumable Claude Code sessions.

Each session is stored as `~/.claude/projects/<cwd-slug>/<session-uuid>.jsonl`.
The slug is a lossy transformation of the cwd (slashes and underscores → dashes),
so we always read the real cwd from inside the JSONL.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional


PROJECTS_ROOT = Path.home() / ".claude" / "projects"


@dataclass
class SessionInfo:
    uuid: str  # session_id, also the jsonl filename stem
    cwd: str
    transcript_path: Path
    mtime: float  # seconds since epoch
    preview: str  # opening user prompt, truncated


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _iter_transcripts() -> Iterable[Path]:
    if not PROJECTS_ROOT.exists():
        return
    for slug_dir in PROJECTS_ROOT.iterdir():
        if not slug_dir.is_dir():
            continue
        for f in slug_dir.iterdir():
            if f.is_file() and f.suffix == ".jsonl":
                yield f


def _read_lines(path: Path, max_lines: int = 50) -> List[dict]:
    out: List[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def extract_cwd(transcript_path: Path) -> Optional[str]:
    """Find the cwd field in the first few entries of a JSONL transcript."""
    for entry in _read_lines(transcript_path, max_lines=20):
        cwd = entry.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
    return None


def extract_first_user_prompt(transcript_path: Path, max_chars: int = 60) -> str:
    """First human-typed prompt: type=user with string content (not tool_result list)."""
    for entry in _read_lines(transcript_path, max_lines=30):
        if entry.get("type") != "user":
            continue
        msg = entry.get("message") or {}
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        text = content.strip()
        if not text:
            continue
        return _truncate(text, max_chars)
    return "(no prompt)"


def _truncate(s: str, n: int) -> str:
    s = " ".join(s.split())
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def find_transcript(uuid: str) -> Optional[Path]:
    """Locate <uuid>.jsonl anywhere under PROJECTS_ROOT."""
    if not uuid or "/" in uuid:
        return None
    for f in _iter_transcripts():
        if f.stem == uuid:
            return f
    return None


def list_sessions(allowed_cwds: List[Path], limit: int) -> List[SessionInfo]:
    """Return up to `limit` SessionInfos, most-recently-modified first,
    filtered to transcripts whose internal cwd lies under one of `allowed_cwds`.
    """
    roots = [p.resolve() for p in allowed_cwds]
    candidates: List[tuple[float, Path]] = []
    for f in _iter_transcripts():
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if f.stat().st_size < 16:
            continue
        candidates.append((mtime, f))
    candidates.sort(key=lambda t: t[0], reverse=True)

    results: List[SessionInfo] = []
    # Walk in mtime order, stop when we have `limit` allowed ones.
    # We probe ~2x to skip out-of-whitelist hits without scanning the whole tree.
    probe_budget = max(limit * 4, 30)
    for mtime, path in candidates[:probe_budget]:
        cwd = extract_cwd(path)
        if not cwd:
            continue
        cwd_path = Path(cwd).resolve()
        if not any(_is_within(cwd_path, root) for root in roots):
            continue
        preview = extract_first_user_prompt(path)
        results.append(
            SessionInfo(
                uuid=path.stem,
                cwd=cwd,
                transcript_path=path,
                mtime=mtime,
                preview=preview,
            )
        )
        if len(results) >= limit:
            break
    return results
