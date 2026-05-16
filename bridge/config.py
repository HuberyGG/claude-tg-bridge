from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List

import tomli


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()


@dataclass
class TelegramCfg:
    bot_token: str
    chat_id: int
    proxy: str  # empty string = no proxy


@dataclass
class IpcCfg:
    host: str
    port: int
    shared_secret: str


@dataclass
class ApprovalCfg:
    tools: List[str]
    auto_allow_bash: List[str]
    on_timeout: str  # "ask" | "allow" | "deny"
    timeout_seconds: int


@dataclass
class LauncherCfg:
    default_cwd: Path
    allowed_cwds: List[Path]
    tmux_session: str  # session daemon opens new windows in
    claude_command: str  # what to run in the new window


@dataclass
class PathsCfg:
    registry: Path
    log: Path


@dataclass
class Config:
    telegram: TelegramCfg
    ipc: IpcCfg
    approval: ApprovalCfg
    launcher: LauncherCfg
    paths: PathsCfg
    source: Path

    @property
    def base_url(self) -> str:
        return f"http://{self.ipc.host}:{self.ipc.port}"


_DEFAULT_PATH = Path.home() / "claude-tg-bridge" / "config.toml"


def load(path: Path | None = None) -> Config:
    cfg_path = path or _DEFAULT_PATH
    with open(cfg_path, "rb") as f:
        raw = tomli.load(f)

    tg = raw["telegram"]
    ipc = raw["ipc"]
    appr = raw["approval"]
    launcher = raw.get("launcher", {}) or {}
    paths = raw.get("paths", {})

    on_timeout = appr.get("on_timeout", "deny")
    if on_timeout not in ("ask", "allow", "deny"):
        raise ValueError(f"approval.on_timeout must be ask|allow|deny, got {on_timeout!r}")

    if not tg.get("bot_token") or tg["bot_token"].startswith("PASTE_"):
        raise ValueError("telegram.bot_token not set in config.toml")
    if not isinstance(tg.get("chat_id"), int) or tg["chat_id"] == -1001234567890:
        raise ValueError("telegram.chat_id not set (must be a real supergroup id, negative number)")
    if not ipc.get("shared_secret") or ipc["shared_secret"].startswith("REPLACE_"):
        raise ValueError("ipc.shared_secret not set in config.toml")

    return Config(
        telegram=TelegramCfg(
            bot_token=tg["bot_token"],
            chat_id=int(tg["chat_id"]),
            proxy=tg.get("proxy", "") or "",
        ),
        ipc=IpcCfg(
            host=ipc.get("host", "127.0.0.1"),
            port=int(ipc.get("port", 8765)),
            shared_secret=ipc["shared_secret"],
        ),
        approval=ApprovalCfg(
            tools=list(appr.get("tools", ["Bash"])),
            auto_allow_bash=list(appr.get("auto_allow_bash", [])),
            on_timeout=on_timeout,
            timeout_seconds=int(appr.get("timeout_seconds", 240)),
        ),
        launcher=LauncherCfg(
            default_cwd=_expand(launcher.get("default_cwd", "~")),
            allowed_cwds=[_expand(p) for p in launcher.get("allowed_cwds", ["~"])],
            tmux_session=str(launcher.get("tmux_session", "bridge")),
            claude_command=str(launcher.get("claude_command", "claude")),
        ),
        paths=PathsCfg(
            registry=_expand(paths.get("registry", "~/claude-tg-bridge/registry.json")),
            log=_expand(paths.get("log", "~/claude-tg-bridge/bridge.log")),
        ),
        source=cfg_path,
    )
