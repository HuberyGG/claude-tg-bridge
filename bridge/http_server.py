from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .approvals import ApprovalRegistry
from .config import Config
from .registry import Registry
from .telegram_client import TelegramClient
from . import tmux

log = logging.getLogger(__name__)


# ---------- request models ----------

class EventBody(BaseModel):
    kind: str  # "stop" | "notification" | "tool_result"
    session_id: str
    transcript_path: str
    cwd: str
    pane: str
    extra: dict = Field(default_factory=dict)


class ApproveBody(BaseModel):
    session_id: str
    tool_use_id: str
    tool_name: str
    tool_input: dict
    pane: str
    cwd: str


class ApproveResponse(BaseModel):
    decision: str  # "allow" | "deny" | "ask"


# ---------- factory ----------

def build_app(
    cfg: Config,
    registry: Registry,
    approvals: ApprovalRegistry,
    tg: TelegramClient,
) -> FastAPI:
    app = FastAPI()

    def _auth(secret: Optional[str]) -> None:
        if secret != cfg.ipc.shared_secret:
            raise HTTPException(status_code=401, detail="bad secret")

    async def _ensure_session(session_id: str, cwd: str, pane: str):
        existing = registry.get(session_id)
        if existing is not None and existing.status == "active":
            return existing
        # Detect claude PID under this pane (best-effort)
        claude_pid = tmux.claude_pid_in_pane(pane) or tmux.pane_pid(pane) or 0
        return await tg.ensure_topic(session_id, cwd, pane, claude_pid)

    @app.post("/event", status_code=204)
    async def event(
        body: EventBody,
        x_bridge_secret: Optional[str] = Header(default=None, alias="X-Bridge-Secret"),
    ):
        _auth(x_bridge_secret)
        sess = await _ensure_session(body.session_id, body.cwd, body.pane)
        if body.kind == "stop":
            try:
                await tg.send_assistant_turn(sess, body.transcript_path)
            except Exception:
                log.exception("send_assistant_turn failed")
        elif body.kind == "notification":
            msg = (body.extra or {}).get("message", "")
            try:
                await tg.send_notification(sess, msg or "(notification)")
            except Exception:
                log.exception("send_notification failed")
        elif body.kind == "tool_result":
            extra = body.extra or {}
            if extra.get("tool_name") != "Bash":
                # Only Bash results are surfaced for now.
                return None
            try:
                await tg.send_bash_result(
                    sess,
                    command=extra.get("command", ""),
                    stdout=extra.get("stdout", "") or "",
                    stderr=extra.get("stderr", "") or "",
                    interrupted=bool(extra.get("interrupted", False)),
                )
            except Exception:
                log.exception("send_bash_result failed")
        else:
            raise HTTPException(status_code=400, detail=f"unknown kind: {body.kind}")
        return None

    @app.post("/approve", response_model=ApproveResponse)
    async def approve(
        body: ApproveBody,
        x_bridge_secret: Optional[str] = Header(default=None, alias="X-Bridge-Secret"),
    ):
        _auth(x_bridge_secret)
        sess = await _ensure_session(body.session_id, body.cwd, body.pane)
        loop = asyncio.get_running_loop()
        entry, is_new = await approvals.create_or_attach(
            body.tool_use_id, body.session_id, body.tool_name, loop
        )
        if is_new:
            try:
                await tg.send_approval_prompt(
                    sess, body.tool_use_id, body.tool_name, body.tool_input
                )
            except Exception:
                log.exception("send_approval_prompt failed")
                await approvals.resolve(body.tool_use_id, cfg.approval.on_timeout)

        timeout = cfg.approval.timeout_seconds
        try:
            decision = await asyncio.wait_for(entry.future, timeout=timeout)
        except asyncio.TimeoutError:
            decision = cfg.approval.on_timeout
            await approvals.resolve(body.tool_use_id, decision)
            if entry.message_id and entry.chat_id:
                await tg.finalize_approval(
                    entry.chat_id, entry.message_id, f"⌛ Timed out → {decision}"
                )
        return ApproveResponse(decision=decision)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "active_sessions": len(registry.all_active())}

    return app
