from __future__ import annotations

import asyncio
import datetime
import html
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from .approvals import ApprovalRegistry
from .config import Config
from .outbound import (
    chunk_for_telegram,
    last_main_assistant_turn,
    render_bash_result,
    render_notification,
    render_session_header,
    render_turn,
    to_html,
)
from .registry import Registry, Session
from .sessions import SessionInfo, find_transcript, list_sessions, extract_cwd
from . import tmux as tmux_lib

log = logging.getLogger(__name__)


# Callback for inbound text messages: takes (session, text) and dispatches to tmux.
InboundHandler = Callable[[Session, str], Awaitable[None]]


def _build_request(proxy: str) -> HTTPXRequest:
    return HTTPXRequest(proxy=proxy or None)


class TelegramClient:
    def __init__(
        self,
        cfg: Config,
        registry: Registry,
        approvals: ApprovalRegistry,
        on_inbound: InboundHandler,
    ):
        self.cfg = cfg
        self.registry = registry
        self.approvals = approvals
        self.on_inbound = on_inbound
        self.app: Application = (
            ApplicationBuilder()
            .token(cfg.telegram.bot_token)
            .request(_build_request(cfg.telegram.proxy))
            .get_updates_request(_build_request(cfg.telegram.proxy))
            .build()
        )
        # Filter all updates to our chat only
        chat_filter = filters.Chat(chat_id=cfg.telegram.chat_id)
        self.app.add_handler(
            MessageHandler(
                chat_filter & filters.TEXT & ~filters.COMMAND,
                self._on_text,
            )
        )
        self.app.add_handler(
            CommandHandler("new", self._on_new, filters=chat_filter)
        )
        self.app.add_handler(
            CommandHandler("exit", self._on_exit, filters=chat_filter)
        )
        self.app.add_handler(
            CommandHandler("list", self._on_list, filters=chat_filter)
        )
        self.app.add_handler(
            CommandHandler("resume", self._on_resume, filters=chat_filter)
        )
        self.app.add_handler(CallbackQueryHandler(self._on_callback))

    # ----- topic management -----

    async def ensure_topic(self, session_id: str, cwd: str, pane_id: str, claude_pid: int) -> Session:
        existing = self.registry.get(session_id)
        if existing is not None and existing.status == "active":
            return existing

        # If a placeholder session (created by /new) exists on this pane, reuse
        # its topic instead of creating a new one.
        placeholder = self.registry.get(f"pane-{pane_id}")
        if placeholder is not None and placeholder.status == "active":
            sess = Session(
                session_id=session_id,
                topic_id=placeholder.topic_id,
                pane_id=pane_id,
                claude_pid=claude_pid or placeholder.claude_pid,
                cwd=cwd,
                topic_name=placeholder.topic_name,
                created_at=time.time(),
                last_event_at=time.time(),
                status="active",
            )
            self.registry.upsert(sess)
            return sess

        name = self._topic_name(cwd, session_id)
        topic_id = await self._create_topic(name)
        sess = Session(
            session_id=session_id,
            topic_id=topic_id,
            pane_id=pane_id,
            claude_pid=claude_pid,
            cwd=cwd,
            topic_name=name,
            created_at=time.time(),
            last_event_at=time.time(),
            status="active",
        )
        self.registry.upsert(sess)
        try:
            await self.send_to_topic(topic_id, render_session_header(cwd, session_id))
        except Exception as e:  # pragma: no cover
            log.warning("failed to send session header: %s", e)
        return sess

    def _topic_name(self, cwd: str, session_id: str) -> str:
        base = os.path.basename(cwd.rstrip("/")) or "/"
        short = (session_id or "")[:8]
        # Telegram topic name limit is 128 chars
        name = f"{base} · {short}"
        return name[:128]

    async def _create_topic(self, name: str) -> int:
        topic = await self.app.bot.create_forum_topic(
            chat_id=self.cfg.telegram.chat_id,
            name=name,
        )
        return topic.message_thread_id

    # ----- outbound -----

    async def send_to_topic(self, topic_id: int, html_text: str, reply_markup=None) -> int:
        """Send (possibly chunked) HTML message; returns the last message_id."""
        chunks = chunk_for_telegram(html_text)
        last_id = 0
        for i, chunk in enumerate(chunks):
            markup = reply_markup if i == len(chunks) - 1 else None
            try:
                msg = await self.app.bot.send_message(
                    chat_id=self.cfg.telegram.chat_id,
                    message_thread_id=topic_id,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
                last_id = msg.message_id
            except BadRequest as e:
                # HTML parse failure or invalid content — re-send as plain text.
                log.warning("HTML send failed (%s); retrying plain", e)
                msg = await self.app.bot.send_message(
                    chat_id=self.cfg.telegram.chat_id,
                    message_thread_id=topic_id,
                    text=_strip_html(chunk),
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
                last_id = msg.message_id
        return last_id

    async def send_assistant_turn(self, sess: Session, transcript_path: str) -> None:
        entries = last_main_assistant_turn(transcript_path)
        if not entries:
            return
        text = render_turn(entries)
        if not text:
            return
        await self.send_to_topic(sess.topic_id, to_html(text))
        self.registry.touch(sess.session_id)

    async def send_notification(self, sess: Session, message: str) -> None:
        await self.send_to_topic(sess.topic_id, render_notification(message))
        self.registry.touch(sess.session_id)

    async def send_bash_result(
        self,
        sess: Session,
        command: str,
        stdout: str,
        stderr: str,
        interrupted: bool,
    ) -> None:
        body = render_bash_result(command, stdout, stderr, interrupted)
        await self.send_to_topic(sess.topic_id, body)
        self.registry.touch(sess.session_id)

    async def send_approval_prompt(
        self,
        sess: Session,
        tool_use_id: str,
        tool_name: str,
        tool_input: dict,
    ) -> int:
        body = _render_approval(tool_name, tool_input)
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Allow", callback_data=f"a:{tool_use_id}"),
                    InlineKeyboardButton("❌ Deny", callback_data=f"d:{tool_use_id}"),
                ]
            ]
        )
        msg_id = await self.send_to_topic(sess.topic_id, body, reply_markup=kb)
        await self.approvals.attach_message(tool_use_id, self.cfg.telegram.chat_id, msg_id)
        return msg_id

    async def finalize_approval(
        self,
        chat_id: int,
        message_id: int,
        decision: str,
        original_html: Optional[str] = None,
    ) -> None:
        """Strip buttons from the approval message and append the decision."""
        suffix = "✅ <b>Allowed</b>" if decision == "allow" else (
            "❌ <b>Denied</b>" if decision == "deny" else f"<b>{html.escape(decision)}</b>"
        )
        try:
            await self.app.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=None,
            )
            await self.app.bot.send_message(
                chat_id=chat_id,
                reply_to_message_id=message_id,
                text=suffix,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as e:  # pragma: no cover
            log.warning("finalize_approval failed: %s", e)

    # ----- inbound handlers -----

    async def _on_text(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg is None or msg.text is None:
            return
        topic_id = msg.message_thread_id
        if topic_id is None:
            # Posted in General topic — ignore
            return
        sess = self.registry.by_topic(topic_id)
        if sess is None:
            await msg.reply_text("⚠️ No active session bound to this topic.")
            return
        if sess.status != "active":
            await msg.reply_text("⚠️ Session has ended.")
            return
        try:
            await self.on_inbound(sess, msg.text)
        except Exception as e:
            log.exception("inbound dispatch failed")
            await msg.reply_text(f"⚠️ Inject failed: {e}")

    async def _on_callback(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None or q.data is None:
            return
        # ACK fast
        try:
            await q.answer()
        except Exception:
            pass
        data = q.data
        if ":" not in data:
            return
        verb, payload = data.split(":", 1)
        if verb == "r":
            # Resume button — payload is a session UUID.
            try:
                await self._do_resume(q.message, payload)
            except Exception:
                log.exception("resume callback failed")
            return
        decision = {"a": "allow", "d": "deny"}.get(verb)
        if decision is None:
            return
        entry = await self.approvals.resolve(payload, decision)
        if entry is None or entry.message_id is None or entry.chat_id is None:
            # No pending request (already resolved or timed out)
            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return
        await self.finalize_approval(entry.chat_id, entry.message_id, decision)

    async def _on_new(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /new [path] — launches `claude` in a fresh tmux window.

        Path argument is resolved against the launcher.allowed_cwds whitelist.
        Without an argument, uses launcher.default_cwd.
        """
        msg = update.effective_message
        if msg is None:
            return
        args = ctx.args or []
        launcher = self.cfg.launcher

        if args:
            raw = " ".join(args).strip()
            candidate = Path(os.path.expanduser(raw)).resolve()
        else:
            candidate = launcher.default_cwd

        if not candidate.exists() or not candidate.is_dir():
            await msg.reply_text(f"⚠️ Not a directory: {candidate}")
            return

        allowed = [p.resolve() for p in launcher.allowed_cwds]
        if not any(_is_within(candidate, root) for root in allowed):
            pretty = ", ".join(str(p) for p in allowed)
            await msg.reply_text(
                f"⚠️ Path not in allowlist.\nGot: {candidate}\nAllowed roots: {pretty}"
            )
            return

        # Whitelist implies trust — pre-accept Claude's workspace trust dialog
        # so the new pane doesn't block on it. Best-effort; failures don't abort.
        try:
            _mark_path_trusted(candidate)
        except Exception as e:
            log.warning("could not pre-trust %s: %s", candidate, e)

        # Ensure the daemon's tmux session exists, then open a window.
        if not tmux_lib.ensure_session(launcher.tmux_session):
            await msg.reply_text(
                f"⚠️ Could not create or attach to tmux session '{launcher.tmux_session}'."
            )
            return

        window_name = candidate.name or "claude"
        pane = tmux_lib.new_window(
            launcher.tmux_session,
            cwd=str(candidate),
            command=launcher.claude_command,
            window_name=window_name[:16],
        )
        if pane is None:
            await msg.reply_text("⚠️ Failed to open new tmux window.")
            return

        # Eagerly create a topic so the user can send the first prompt right away.
        # We use the pane id as a placeholder session_id; the real session_id
        # arrives later via the first hook event. The placeholder session stays
        # active so inbound routing works; when the real session_id appears we
        # add a second alias entry pointing at the same topic.
        placeholder_sid = f"pane-{pane}"
        try:
            sess = await self.ensure_topic(
                session_id=placeholder_sid,
                cwd=str(candidate),
                pane_id=pane,
                claude_pid=tmux_lib.pane_pid(pane) or 0,
            )
            topic_hint = f"Topic created — send your first prompt there."
        except Exception:
            log.exception("eager ensure_topic failed")
            topic_hint = "Topic will appear after Claude's first response."

        await msg.reply_text(
            f"🚀 Launched <code>{html.escape(launcher.claude_command)}</code>\n"
            f"<code>{html.escape(str(candidate))}</code>\n"
            f"<i>tmux {html.escape(launcher.tmux_session)} pane {html.escape(pane)}. {html.escape(topic_hint)}</i>",
            parse_mode=ParseMode.HTML,
        )

    # ----- /exit -----

    async def _on_exit(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg is None:
            return
        topic_id = msg.message_thread_id
        if topic_id is None:
            await msg.reply_text("⚠️ /exit must be sent inside a session topic.")
            return
        sess = self.registry.by_topic(topic_id)
        if sess is None or sess.status != "active":
            await msg.reply_text("⚠️ No active session bound to this topic.")
            return

        pane = sess.pane_id
        pid = sess.claude_pid

        async def claude_alive() -> bool:
            if not tmux_lib.pane_exists(pane):
                return False
            return tmux_lib.pid_alive(pid)

        # Step 1: graceful /exit
        try:
            await asyncio.to_thread(tmux_lib.send_paste, pane, "/exit")
        except Exception:
            log.warning("send_paste(/exit) failed", exc_info=True)
        for _ in range(10):  # ~5 seconds
            await asyncio.sleep(0.5)
            if not await claude_alive():
                break

        # Step 2: Ctrl-D (EOF)
        if await claude_alive():
            try:
                await asyncio.to_thread(tmux_lib.send_ctrl, pane, "C-d")
            except Exception:
                log.warning("send_ctrl C-d failed", exc_info=True)
            for _ in range(6):  # ~3 seconds
                await asyncio.sleep(0.5)
                if not await claude_alive():
                    break

        # Step 3: SIGTERM
        if await claude_alive():
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            await asyncio.sleep(1.0)

        # Clean up the now-empty tmux window (if pane still alive, this also kills it)
        try:
            tmux_lib.kill_window(pane)
        except Exception:
            log.warning("kill_window failed", exc_info=True)

        # Mark all sessions on this pane ended (placeholder + real)
        for s in list(self.registry.all_active()):
            if s.pane_id == pane:
                self.registry.mark_ended(s.session_id)

        await msg.reply_text("✅ Session ended.")

    # ----- /list -----

    async def _on_list(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg is None:
            return
        try:
            limit = int(ctx.args[0]) if ctx.args else 10
        except ValueError:
            limit = 10
        limit = max(1, min(limit, 50))

        infos = await asyncio.to_thread(
            list_sessions, self.cfg.launcher.allowed_cwds, limit
        )
        if not infos:
            await msg.reply_text("📭 No matching sessions found.")
            return

        lines = ["📜 <b>Recent sessions</b>"]
        buttons: list[list[InlineKeyboardButton]] = []
        for i, s in enumerate(infos, 1):
            when = datetime.datetime.fromtimestamp(s.mtime).strftime("%m-%d %H:%M")
            base = Path(s.cwd).name or "/"
            lines.append(
                f"{i}. <b>{html.escape(base)}</b> · <i>{when}</i> · "
                f"{html.escape(s.preview)}"
            )
            label = f"{i}. {s.preview}"
            if len(label) > 60:
                label = label[:57] + "…"
            buttons.append([InlineKeyboardButton(label, callback_data=f"r:{s.uuid}")])

        await msg.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
            disable_web_page_preview=True,
        )

    # ----- /resume -----

    async def _on_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg is None:
            return
        if not ctx.args:
            await msg.reply_text("Usage: <code>/resume &lt;session-uuid&gt;</code>", parse_mode=ParseMode.HTML)
            return
        uuid = ctx.args[0].strip()
        await self._do_resume(msg, uuid)

    async def _do_resume(self, msg, uuid: str) -> None:
        """Shared resume logic for /resume command and r: callback button."""
        if msg is None:
            return
        transcript = await asyncio.to_thread(find_transcript, uuid)
        if transcript is None:
            await msg.reply_text(f"⚠️ Session not found: <code>{html.escape(uuid)}</code>", parse_mode=ParseMode.HTML)
            return

        cwd_str = await asyncio.to_thread(extract_cwd, transcript)
        if not cwd_str:
            await msg.reply_text("⚠️ Could not determine session cwd.")
            return
        cwd = Path(cwd_str).resolve()
        if not cwd.exists():
            await msg.reply_text(f"⚠️ Original cwd no longer exists: <code>{html.escape(str(cwd))}</code>", parse_mode=ParseMode.HTML)
            return

        allowed = [p.resolve() for p in self.cfg.launcher.allowed_cwds]
        if not any(_is_within(cwd, root) for root in allowed):
            await msg.reply_text("⚠️ Session cwd is outside the allowlist.")
            return

        try:
            _mark_path_trusted(cwd)
        except Exception:
            log.warning("could not pre-trust %s", cwd, exc_info=True)

        launcher = self.cfg.launcher
        if not tmux_lib.ensure_session(launcher.tmux_session):
            await msg.reply_text(f"⚠️ tmux session '{launcher.tmux_session}' unavailable.")
            return

        command = f"{launcher.claude_command} --resume {uuid}"
        window_name = (cwd.name or "claude")[:16]
        pane = tmux_lib.new_window(
            launcher.tmux_session,
            cwd=str(cwd),
            command=command,
            window_name=window_name,
        )
        if pane is None:
            await msg.reply_text("⚠️ Failed to open new tmux window.")
            return

        # Eagerly create a topic (placeholder); the real session_id from hooks
        # will be the same uuid, and ensure_topic merges placeholders.
        placeholder_sid = f"pane-{pane}"
        try:
            await self.ensure_topic(
                session_id=placeholder_sid,
                cwd=str(cwd),
                pane_id=pane,
                claude_pid=tmux_lib.pane_pid(pane) or 0,
            )
            topic_hint = "Topic created — send your next prompt there."
        except Exception:
            log.exception("eager ensure_topic failed")
            topic_hint = "Topic will appear after Claude's first response."

        short = uuid[:8]
        await msg.reply_text(
            f"🔄 Resuming <code>{html.escape(short)}</code>…\n"
            f"<code>{html.escape(str(cwd))}</code>\n"
            f"<i>tmux {html.escape(launcher.tmux_session)} pane {html.escape(pane)}. {html.escape(topic_hint)}</i>",
            parse_mode=ParseMode.HTML,
        )

    # ----- lifecycle -----

    async def start(self) -> None:
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)

    async def stop(self) -> None:
        try:
            await self.app.updater.stop()
        except Exception:
            pass
        await self.app.stop()
        await self.app.shutdown()


# ---------- helpers ----------

def _render_approval(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        desc = tool_input.get("description") or ""
        out = f"<b>⚠️ Approve Bash</b>\n<pre>{html.escape(cmd)}</pre>"
        if desc:
            out += f"\n<i>{html.escape(desc)}</i>"
        return out
    pretty = json.dumps(tool_input, ensure_ascii=False, indent=2)
    return (
        f"<b>⚠️ Approve {html.escape(tool_name)}</b>\n"
        f"<pre>{html.escape(pretty)[:3500]}</pre>"
    )


def _strip_html(s: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", s)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _mark_path_trusted(path: Path) -> None:
    """Set hasTrustDialogAccepted=True for `path` in ~/.claude.json.

    Claude Code records workspace trust per project in this file. Pre-setting
    it prevents the interactive trust prompt blocking a remotely-launched session.

    No-op if the file is missing, malformed, or already trusted.
    """
    claude_json = Path.home() / ".claude.json"
    if not claude_json.exists():
        return
    try:
        data = json.loads(claude_json.read_text())
    except (json.JSONDecodeError, OSError):
        return
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return
    key = str(path)
    entry = projects.get(key)
    if entry is None:
        # Create a minimal entry. Claude will fill in the rest on first run.
        entry = {"allowedTools": [], "hasTrustDialogAccepted": True}
        projects[key] = entry
    else:
        if entry.get("hasTrustDialogAccepted") is True:
            return  # already trusted
        entry["hasTrustDialogAccepted"] = True
    tmp = claude_json.with_suffix(claude_json.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(claude_json)
