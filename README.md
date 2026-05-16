<div align="center">

# claude-tg-bridge

**Operate Claude Code from your phone via Telegram.**

Read replies, send prompts, approve tool calls — without touching your laptop.

[Installation](#-installation) · [Commands](#-commands) · [Architecture](#-architecture) · [中文](#-中文)

![status](https://img.shields.io/badge/status-MVP-black?style=flat-square)
![python](https://img.shields.io/badge/python-3.9%2B-black?style=flat-square)
![license](https://img.shields.io/badge/license-MIT-black?style=flat-square)
![platform](https://img.shields.io/badge/platform-macOS-black?style=flat-square)

<br/>

<!-- Drop a demo gif at assets/demo.gif and it will show here. -->
<img src="assets/demo.gif" alt="demo" width="640" onerror="this.style.display='none'"/>

</div>

---

## Why

You leave the desk. Claude is mid-task. You want to nudge it, approve a `git push`, or kick off a new conversation — from your phone. This bridge gives you a small Telegram channel into every running Claude Code session: each session is its own forum topic; what you type goes back to the terminal; tool calls ask for approval inline.

## ✨ Features

- **Bidirectional chat** — assistant replies push to your phone; messages typed in a topic are injected back into the matching `tmux` pane via bracketed-paste.
- **Per-session topics** — each `claude` process gets its own Telegram forum topic. Multiple sessions in parallel, no crosstalk.
- **Inline approvals** — sensitive tool calls (Bash by default) surface ✅ Allow / ❌ Deny buttons on your phone. Configurable auto-allow regex list. Timeout falls back to *deny*.
- **Result echo** — Bash `stdout` / `stderr` is pushed to the topic after the tool runs. Long output is head/tail trimmed.
- **Remote launch** — `/new [path]` opens a fresh `claude` session in a whitelisted directory.
- **History resume** — `/list` shows recent sessions; tap a button or use `/resume <uuid>` to continue.
- **Graceful exit** — `/exit` walks Claude through `/exit` → `Ctrl-D` → `SIGTERM` and cleans up the window.
- **Fail-open hooks** — daemon down? Claude keeps running unimpeded.
- **Cursor support** — works the same: open Cursor's integrated terminal, `tmux attach`, then `claude`.

## 🧭 Commands

| Command           | What it does                                                   |
| ----------------- | -------------------------------------------------------------- |
| `/new [path]`     | Open a new `claude` session in a whitelisted directory.        |
| `/list [N=10]`    | Show recent sessions as tappable buttons.                      |
| `/resume <uuid>`  | Resume a past session in a new topic.                          |
| `/exit`           | Cleanly terminate the session bound to the current topic.      |
| *(plain text)*    | Anything else in a session topic is sent to that Claude pane.  |

## 🏗 Architecture

```
   iPhone Telegram                                                         
        │                                                                  
        │  long-poll (SOCKS5)                                              
        ▼                                                                  
 ┌────────────────────────────────────────────┐                            
 │  bridge daemon  (single asyncio loop)      │                            
 │   ├─ python-telegram-bot                   │                            
 │   ├─ FastAPI on 127.0.0.1                  │                            
 │   └─ tmux send-keys                        │                            
 └─────────▲──────────────────────────┬───────┘                            
   loopback HTTP                      │                                    
   X-Bridge-Secret                    │ tmux send-keys                     
           │                          ▼                                    
 ┌─────────┴──────┐         ┌────────────────────────────┐                 
 │  hook scripts  │◄────────┤   tmux pane → claude TUI   │                 
 │  Stop / Notif  │  invoke └────────────────────────────┘                 
 │  Pre/PostTool  │                                                        
 └────────────────┘                                                        
```

**Outbound** (terminal → phone) flows through Claude Code hooks. **Inbound** (phone → terminal) flows through `tmux send-keys` with bracketed paste so multi-line prompts stay atomic. Approvals are a synchronous PreToolUse hook that blocks the agent loop on an `asyncio.Future` until you tap a button.

## 📦 Installation

```sh
# 1. clone and install
git clone https://github.com/HuberyGG/claude-tg-bridge.git ~/claude-tg-bridge
cd ~/claude-tg-bridge

# 2. dependencies (tmux + python venv)
brew install tmux
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. config
cp config.toml.example config.toml
chmod 600 config.toml
# edit config.toml — see "Telegram setup" below

# 4. register hooks into ~/.claude/settings.json (preserves existing env)
.venv/bin/python scripts/install_hooks.py

# 5. enable the bridge gate in tmux
echo 'set-environment -g CLAUDE_TG_BRIDGE 1' >> ~/.tmux.conf
tmux kill-server   # if a server is already running
```

### Telegram setup

1. Talk to [@BotFather](https://t.me/BotFather) → `/newbot` → save the token.
2. `/mybots` → your bot → **Bot Settings** → **Group Privacy** → **Turn off**. *(Bots see only @-mentions otherwise.)*
3. Create a **private** group, enable **Topics** in group settings.
4. Add the bot, promote to admin with **Manage Topics**.
5. Send any message in the group, then fetch the chat id:
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
   Look for `"chat":{"id":-100…}` — that negative number is your `chat_id`.

### Configure

Edit `config.toml`:

```toml
[telegram]
bot_token = "1234567:AA…"
chat_id   = -100…             # supergroup id from getUpdates
proxy     = "socks5://127.0.0.1:1080"  # or "" for direct

[ipc]
shared_secret = "long-random-string"   # used between hooks and daemon

[approval]
tools           = ["Bash"]
auto_allow_bash = ["^ls( |$)", "^pwd$", "^cat ", "^grep ", "^git (status|diff|log|show)( |$)"]
on_timeout      = "deny"
timeout_seconds = 240

[launcher]
default_cwd  = "~"
allowed_cwds = ["~"]          # /new and /resume restricted to these roots
tmux_session = "bridge"
claude_command = "claude"
```

## 🚀 Running

```sh
# inside a tmux session named the same as `launcher.tmux_session`
.venv/bin/python -m bridge.daemon
```

Then in another tmux window: `claude` — your phone gets a new topic on the first reply. Or `/new` from your phone and the daemon opens the window for you.

## 🛡 Security

- **Anyone with access to the Telegram account can drive Claude — and thus your shell.** Enable two-factor auth on your Telegram.
- `config.toml` contains the bot token. Always `chmod 600`.
- The daemon rejects any update whose `chat_id` doesn't match the configured one.
- Loopback HTTP is authenticated by a shared secret header. Bind only to `127.0.0.1`.
- Whitelisted `allowed_cwds` is enforced on `/new` and `/resume`. Choose narrowly.

## 🗂 Project layout

```
claude-tg-bridge/
├── bridge/                  daemon: tg client, http server, registry, tmux glue
│   ├── daemon.py            entry point (single asyncio loop)
│   ├── telegram_client.py   PTB Application + command/callback handlers
│   ├── http_server.py       /event /approve over 127.0.0.1
│   ├── registry.py          session ↔ topic ↔ pane mapping (JSON, atomic)
│   ├── sessions.py          scan ~/.claude/projects for /list and /resume
│   ├── tmux.py              send-keys, pane probing, new-window
│   ├── outbound.py          transcript → HTML, chunking, Bash result render
│   ├── approvals.py         tool_use_id → asyncio.Future
│   └── config.py            TOML loader
├── hooks/                   short-lived scripts invoked by Claude Code
│   ├── stop.py
│   ├── notification.py
│   ├── pretooluse.py
│   ├── posttooluse.py
│   └── _common.py
├── scripts/
│   └── install_hooks.py
├── config.toml.example
└── requirements.txt
```

## ⚠️ Status

MVP. Used daily by the author; not battle-tested. Pull requests welcome.

Roadmap:
- `launchd` autostart
- "Allow + remember" approval button
- Cross-tool approvals beyond Bash (Edit, Write, WebFetch)
- Pagination for `/list` > 50 entries

## 📜 License

MIT — see [LICENSE](LICENSE).

---

<a name="-中文"></a>

## 🇨🇳 中文

把 Mac 终端里跑的 Claude Code 镜像到手机 Telegram。每个 `claude` 进程对应一个 Telegram 论坛 topic；手机上输入的消息回传给终端；Bash 调用通过手机点按钮审批；离开电脑也能继续工作。

### 功能

- **双向对话**：助手回复推到手机；topic 里的消息通过 `tmux send-keys` 注入回对应 pane，多行用 bracketed paste 保证原子。
- **多会话**：每个 `claude` 一个 topic，互不串扰。
- **行内审批**：Bash（默认）调用时手机出现 ✅ Allow / ❌ Deny；可配自动放行正则；超时回退 *deny*。
- **结果回推**：Bash `stdout`/`stderr` 跑完后推到 topic，过长首尾截断。
- **远程拉起**：`/new [path]` 在白名单目录内新开会话；daemon 自动开 tmux window。
- **历史恢复**：`/list` 列出最近会话（带按钮），`/resume <uuid>` 接续。
- **优雅退出**：`/exit` 走 `/exit` → `Ctrl-D` → `SIGTERM` 递进式终止。
- **fail-open**：daemon 挂掉时 hook 静默放行，不会卡 Claude。
- **Cursor 兼容**：在 Cursor 集成终端 `tmux attach` 后跑 `claude` 即可。

### 命令

| 命令               | 说明                                          |
| ------------------ | --------------------------------------------- |
| `/new [path]`      | 在白名单目录内新开一个 `claude` 会话。        |
| `/list [N=10]`     | 列出最近会话，按钮可点击恢复。                |
| `/resume <uuid>`   | 在新 topic 恢复历史会话。                     |
| `/exit`            | 优雅退出当前 topic 绑定的会话。               |
| *（普通文本）*     | 在 session topic 里发的消息会注入到对应 pane。 |

### 安装

完整步骤同上方英文版。要点：
1. `brew install tmux` + 建 venv 装 `requirements.txt`
2. BotFather 建 bot、**关掉 Group Privacy**
3. 建私有 supergroup、启用 Topics、把 bot 设为可管 topic 的管理员
4. 在群里发消息，`getUpdates` 拿 `chat_id`
5. `cp config.toml.example config.toml` 后填好；`chmod 600 config.toml`
6. `.venv/bin/python scripts/install_hooks.py` 注册 4 个 hook 到 `~/.claude/settings.json`
7. `~/.tmux.conf` 加 `set-environment -g CLAUDE_TG_BRIDGE 1`，`tmux kill-server` 重开
8. 在叫 `bridge` 的 tmux session 里：`.venv/bin/python -m bridge.daemon`

### 安全提醒

- **拿到 Telegram 账号 = 拿到你的 shell**。Telegram 必须开两步验证。
- `config.toml` 含 bot token，`chmod 600` 保管。
- daemon 只信任配置的 `chat_id`，其它消息直接丢。
- Loopback HTTP 用 shared secret 认证，绑 `127.0.0.1`。
- `allowed_cwds` 白名单控制 `/new` 和 `/resume` 能进哪些目录，按需收紧。
