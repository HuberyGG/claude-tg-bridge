from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class PendingApproval:
    tool_use_id: str
    session_id: str
    tool_name: str
    future: asyncio.Future
    message_id: Optional[int] = None  # Telegram message id of the approval prompt
    chat_id: Optional[int] = None


class ApprovalRegistry:
    """In-memory pool of pending tool-approval requests, keyed by tool_use_id.

    Hooks call /approve; the HTTP handler creates or attaches to a PendingApproval
    and awaits its Future. Telegram callback handlers resolve the Future.
    """

    def __init__(self) -> None:
        self._pending: Dict[str, PendingApproval] = {}
        self._lock = asyncio.Lock()

    async def create_or_attach(
        self,
        tool_use_id: str,
        session_id: str,
        tool_name: str,
        loop: asyncio.AbstractEventLoop,
    ) -> tuple[PendingApproval, bool]:
        """Return (entry, is_new)."""
        async with self._lock:
            entry = self._pending.get(tool_use_id)
            if entry is not None:
                return entry, False
            fut: asyncio.Future = loop.create_future()
            entry = PendingApproval(
                tool_use_id=tool_use_id,
                session_id=session_id,
                tool_name=tool_name,
                future=fut,
            )
            self._pending[tool_use_id] = entry
            return entry, True

    async def attach_message(self, tool_use_id: str, chat_id: int, message_id: int) -> None:
        async with self._lock:
            entry = self._pending.get(tool_use_id)
            if entry is not None:
                entry.chat_id = chat_id
                entry.message_id = message_id

    async def resolve(self, tool_use_id: str, decision: str) -> Optional[PendingApproval]:
        async with self._lock:
            entry = self._pending.pop(tool_use_id, None)
        if entry and not entry.future.done():
            entry.future.set_result(decision)
        return entry

    async def get(self, tool_use_id: str) -> Optional[PendingApproval]:
        async with self._lock:
            return self._pending.get(tool_use_id)
