from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Iterable, List, Tuple


TELEGRAM_LIMIT = 4000  # leave headroom under 4096


# ---------- transcript parsing ----------

_NON_TURN_TYPES = {
    "system",
    "attachment",
    "permission-mode",
    "last-prompt",
    "ai-title",
    "agent-name",
    "file-history-snapshot",
}


def _read_lines(path: Path) -> List[dict]:
    """Read JSONL defensively — file may be written concurrently."""
    out: List[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # tail may be partial
    except OSError:
        return []
    return out


def last_main_assistant_turn(transcript_path: str) -> List[dict]:
    """Return the trailing run of assistant entries from the main conversation.

    Walks backwards skipping non-conversational entries and `isSidechain=True`
    (subagent) entries. Stops when a user message or session boundary is hit.
    Returns entries in chronological (forward) order.
    """
    lines = _read_lines(Path(transcript_path))
    collected: List[dict] = []
    for entry in reversed(lines):
        t = entry.get("type")
        if t in _NON_TURN_TYPES:
            continue
        if entry.get("isSidechain"):
            continue
        if t == "assistant":
            collected.append(entry)
            continue
        # any other type (user, etc.) closes the trailing assistant run
        break
    collected.reverse()
    return collected


def _summarize_tool_use(name: str, tool_input: dict) -> str:
    name = name or "Tool"
    inp = tool_input or {}

    def trunc(s: str, n: int = 200) -> str:
        s = " ".join(s.split())
        return s if len(s) <= n else s[: n - 1] + "…"

    if name == "Bash":
        cmd = trunc(str(inp.get("command", "")), 240)
        return f"▸ Bash: {cmd}"
    if name in ("Read", "NotebookRead"):
        return f"▸ {name}: {inp.get('file_path', inp.get('notebook_path', '?'))}"
    if name in ("Write", "NotebookEdit"):
        return f"▸ {name}: {inp.get('file_path', inp.get('notebook_path', '?'))}"
    if name == "Edit":
        return f"▸ Edit: {inp.get('file_path', '?')}"
    if name in ("Glob", "Grep"):
        q = inp.get("pattern") or inp.get("query") or "?"
        return f"▸ {name}: {trunc(str(q), 120)}"
    if name == "TodoWrite":
        todos = inp.get("todos", [])
        return f"▸ TodoWrite ({len(todos)} tasks)"
    if name == "Task":
        st = inp.get("subagent_type") or "general-purpose"
        desc = trunc(str(inp.get("description", "")), 80)
        return f"▸ Task[{st}]: {desc}"
    if name == "WebFetch":
        return f"▸ WebFetch: {inp.get('url', '?')}"
    if name == "WebSearch":
        return f"▸ WebSearch: {trunc(str(inp.get('query', '')), 120)}"
    return f"▸ {name}"


def render_turn(entries: List[dict]) -> str:
    """Render a list of assistant entries (one logical turn) into plain text.

    - `text` blocks → rendered as-is
    - `tool_use` blocks → one-line summary prefixed with ▸
    - `thinking` blocks → dropped
    """
    parts: List[str] = []
    for e in entries:
        msg = e.get("message", {}) or {}
        for block in msg.get("content", []) or []:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "text":
                txt = (block.get("text") or "").strip()
                if txt:
                    parts.append(txt)
            elif bt == "tool_use":
                parts.append(_summarize_tool_use(block.get("name", ""), block.get("input", {}) or {}))
            # thinking, etc. — skip
    return "\n\n".join(parts).strip()


# ---------- Telegram formatting ----------

# Use HTML parse_mode (simpler escaping than MarkdownV2). We never emit raw HTML
# tags in the rendered text — only the wrapping <pre><code> for code blocks.

_CODE_FENCE_RE = None  # built lazily to avoid import-time cost


def _split_code_blocks(text: str) -> List[Tuple[str, str]]:
    """Split markdown text into ('text', segment) and ('code', segment) chunks.

    Recognizes ```fenced``` blocks. The fence backticks are stripped from the
    code segment.
    """
    global _CODE_FENCE_RE
    if _CODE_FENCE_RE is None:
        import re
        _CODE_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
    out: List[Tuple[str, str]] = []
    pos = 0
    for m in _CODE_FENCE_RE.finditer(text):
        if m.start() > pos:
            out.append(("text", text[pos : m.start()]))
        out.append(("code", m.group(1)))
        pos = m.end()
    if pos < len(text):
        out.append(("text", text[pos:]))
    return out


def to_html(text: str) -> str:
    """Convert plain/markdown-ish text to Telegram HTML, escaping safely."""
    pieces: List[str] = []
    for kind, seg in _split_code_blocks(text):
        if kind == "code":
            pieces.append("<pre>" + html.escape(seg) + "</pre>")
        else:
            # Inline `code` spans → <code>
            esc = html.escape(seg)
            # naive backtick substitution: only single backticks (not triple)
            import re
            esc = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", esc)
            pieces.append(esc)
    return "".join(pieces)


def chunk_for_telegram(html_text: str, limit: int = TELEGRAM_LIMIT) -> List[str]:
    """Split a possibly-long HTML string into chunks <= limit chars.

    Tries paragraph boundaries first, then line boundaries, then hard cuts.
    Won't split inside a <pre>...</pre> block (instead opens/closes the tag
    across chunk boundaries).
    """
    if len(html_text) <= limit:
        return [html_text]

    chunks: List[str] = []
    remaining = html_text

    while len(remaining) > limit:
        cut = _find_cut(remaining, limit)
        head = remaining[:cut]
        tail = remaining[cut:]
        head, tail = _balance_pre(head, tail)
        chunks.append(head.rstrip())
        remaining = tail.lstrip()

    if remaining:
        chunks.append(remaining)
    return chunks


def _find_cut(s: str, limit: int) -> int:
    window = s[:limit]
    for sep in ("\n\n", "\n", " "):
        idx = window.rfind(sep)
        if idx >= limit // 2:
            return idx + len(sep)
    return limit


def _balance_pre(head: str, tail: str) -> tuple[str, str]:
    """If head opened a <pre> without closing it, close it and reopen in tail."""
    opens = head.count("<pre>")
    closes = head.count("</pre>")
    if opens > closes:
        head = head + "</pre>"
        tail = "<pre>" + tail
    return head, tail


# ---------- inbound notification helpers ----------

def render_notification(message: str) -> str:
    return "<b>🔔 Claude Code</b>\n" + html.escape(message)


def render_session_header(cwd: str, session_id: str) -> str:
    short = session_id[:8] if session_id else "?"
    return (
        f"<b>📡 Session opened</b>\n"
        f"<code>{html.escape(cwd)}</code>\n"
        f"<i>id: {short}</i>"
    )


def render_bash_result(command: str, stdout: str, stderr: str, interrupted: bool) -> str:
    """Format a Bash PostToolUse result as a single Telegram HTML message."""
    OUTPUT_LIMIT = 3500  # keep one chunk under 4000

    def trim(s: str, n: int) -> str:
        if len(s) <= n:
            return s
        keep_head = n // 4
        keep_tail = n - keep_head - 32
        return s[:keep_head] + f"\n…[trimmed {len(s) - keep_head - keep_tail} chars]…\n" + s[-keep_tail:]

    parts: List[str] = []
    header = "🛑 <b>Bash interrupted</b>" if interrupted else "▸ <b>Bash result</b>"
    parts.append(header)

    cmd_short = " ".join((command or "").split())
    if len(cmd_short) > 200:
        cmd_short = cmd_short[:197] + "…"
    parts.append(f"<code>{html.escape(cmd_short)}</code>")

    body = ""
    if stdout:
        body += trim(stdout, OUTPUT_LIMIT)
    if stderr:
        sep = "\n--- stderr ---\n" if body else ""
        # split budget between stdout and stderr if both present
        remaining = max(400, OUTPUT_LIMIT - len(body))
        body += sep + trim(stderr, remaining)

    if not body:
        parts.append("<i>(no output)</i>")
    else:
        parts.append(f"<pre>{html.escape(body)}</pre>")

    return "\n".join(parts)
