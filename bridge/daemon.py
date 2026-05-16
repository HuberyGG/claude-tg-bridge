from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

import uvicorn

from . import tmux
from .approvals import ApprovalRegistry
from .config import load as load_config
from .http_server import build_app
from .registry import Registry
from .telegram_client import TelegramClient


def _setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(sys.stderr),
        ],
    )
    # Quiet down noisy libs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


async def _inbound_to_tmux(sess, text: str) -> None:
    log = logging.getLogger("inbound")
    # Verify pane and claude are alive
    if not tmux.pane_exists(sess.pane_id):
        raise RuntimeError(f"pane {sess.pane_id} no longer exists")
    if not tmux.pid_alive(sess.claude_pid):
        # Re-probe — claude may have been restarted in same pane (different PID)
        new_pid = tmux.claude_pid_in_pane(sess.pane_id)
        if not new_pid:
            raise RuntimeError("claude process not detected in pane")
        sess.claude_pid = new_pid
    if tmux.pane_in_copy_mode(sess.pane_id):
        raise RuntimeError("pane is in copy-mode; press q in the pane to exit")
    log.info("inject %d chars into %s", len(text), sess.pane_id)
    # Run subprocess-heavy call in default thread pool
    await asyncio.to_thread(tmux.send_paste, sess.pane_id, text)


async def main_async() -> None:
    cfg = load_config()
    _setup_logging(cfg.paths.log)
    log = logging.getLogger("daemon")

    registry = Registry(cfg.paths.registry)
    registry.bootstrap(current_tmux_pid=tmux.server_pid())

    approvals = ApprovalRegistry()

    tg = TelegramClient(cfg, registry, approvals, on_inbound=_inbound_to_tmux)
    await tg.start()
    log.info("Telegram bot started")

    app = build_app(cfg, registry, approvals, tg)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=cfg.ipc.host,
            port=cfg.ipc.port,
            log_level="info",
            access_log=False,
            loop="asyncio",
        )
    )

    log.info("HTTP server listening on %s", cfg.base_url)

    # Graceful shutdown via signals
    stop_event = asyncio.Event()

    def _sig(*_a):
        log.info("signal received, shutting down")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, _sig)
        except NotImplementedError:
            pass

    server_task = asyncio.create_task(server.serve())

    await stop_event.wait()
    server.should_exit = True
    await server_task
    await tg.stop()
    log.info("daemon stopped")


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
