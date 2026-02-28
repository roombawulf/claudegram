<p align="center">
  <h1 align="center">claudegram</h1>
  <p align="center">A self-hosted Telegram bot that puts Claude on your VPS — with shell access, file editing, web browsing, persistent memory, and the ability to modify its own source code.</p>
</p>

<p align="center">
  <a href="#install">Install</a> &nbsp;&bull;&nbsp;
  <a href="#features">Features</a> &nbsp;&bull;&nbsp;
  <a href="#commands">Commands</a> &nbsp;&bull;&nbsp;
  <a href="#tools">Tools</a> &nbsp;&bull;&nbsp;
  <a href="#configuration">Configuration</a>
</p>

---

## Install

**Prerequisites:** A VPS with `python3` (3.10+) and `git` installed.

**You'll need:**
1. A Telegram Bot Token — from [@BotFather](https://t.me/BotFather)
2. An Anthropic API Key — from [console.anthropic.com](https://console.anthropic.com)
3. Your Telegram User ID — from [@userinfobot](https://t.me/userinfobot)

### Quick start

SSH into your VPS and run:

```bash
curl -fsSL https://raw.githubusercontent.com/roombawulf/claudegram/main/install.sh | sudo bash
```

The installer prompts for your credentials, then handles everything — cloning, Python venv, systemd, and starting the bot.

### Fork first (recommended for self-modification)

If you want Claude to be able to push changes to the repo, fork it first and install from your fork:

```bash
# Replace with your fork URL
curl -fsSL https://raw.githubusercontent.com/YOUR_USER/claudegram/main/install.sh \
  | sudo bash -s -- https://github.com/YOUR_USER/claudegram.git
```

Then set up git push credentials for the `claude-bot` user (SSH key or token).

<details>
<summary><b>Manual install</b></summary>

```bash
git clone https://github.com/roombawulf/claudegram.git
cd claudegram
sudo ./install.sh
```

</details>

<details>
<summary><b>Local development</b></summary>

```bash
git clone https://github.com/roombawulf/claudegram.git
cd claudegram

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env with your API keys

python -m bot.main
```

</details>

<details>
<summary><b>Updating</b></summary>

```bash
cd /opt/claude-telegram
sudo -u claude-bot git pull
sudo systemctl restart claude-telegram
```

Or re-run the installer — it detects the existing clone and pulls the latest.

</details>

---

## Features

| | |
|---|---|
| **Streaming responses** | Live message edits in Telegram as Claude generates — no waiting for the full response |
| **Shell access** | Persistent bash session on your VPS, stateful across commands |
| **File editing** | View, create, and edit files with path sandboxing |
| **Web search & fetch** | Anthropic server-side tools for browsing the web |
| **Vision** | Send photos and Claude analyzes them |
| **Document uploads** | Files saved to workspace, content passed to Claude |
| **Persistent memory** | JSON-backed memory Claude can read and write across conversations |
| **Smart model routing** | Simple messages auto-route to Haiku, complex ones to Sonnet |
| **Cost tracking** | Per-request token logging with daily/monthly reports and spend alerts |
| **Conversation management** | SQLite-backed history with auto-summarization at 120K tokens |
| **Prompt caching** | System prompt cached for ~90% input token savings on subsequent turns |
| **Self-modification** | Claude can read, edit, commit, and push its own source code |

---

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and usage info |
| `/new` | Start a fresh conversation |
| `/model [haiku\|sonnet\|auto]` | Lock model or use auto-routing |
| `/usage` | Daily and monthly cost breakdown |
| `/memory` | View stored persistent memories |
| `/status` | Session info — tokens, cost, conversation ID |
| `/restart` | Restart the bot process |

---

## Tools

| Tool | Type | Description |
|------|------|-------------|
| `bash` | Client-side | Persistent shell session in the workspace directory |
| `text_editor` | Client-side | View, create, and edit files in workspace and own source |
| `web_search` | Server-side | Anthropic-hosted web search (max 5 per turn) |
| `web_fetch` | Server-side | Anthropic-hosted page fetching (max 5 per turn) |

**Client-side** tools run on your VPS. **Server-side** tools run on Anthropic's infrastructure.

---

## Architecture

```
Telegram  <-->  python-telegram-bot (async)  <-->  TelegramHandler
                                                         |
                                                   ClaudeClient
                                                  (Anthropic SDK)
                                                   /     |     \
                                            Caching  Streaming  Tool Loop
                                                                   |
                                              ┌── Client (we run) ──────┐
                                              │  bash                   │
                                              │  text_editor            │
                                              ├── Server (Anthropic) ───┤
                                              │  web_search             │
                                              │  web_fetch              │
                                              └─────────────────────────┘
```

<details>
<summary><b>Project structure</b></summary>

```
claudegram/
├── bot/
│   ├── main.py               Entry point, handler registration
│   ├── config.py              Config dataclass, env var loading
│   ├── database.py            SQLite schema + async operations
│   ├── formatting.py          Markdown → Telegram HTML
│   ├── model_router.py        Haiku/Sonnet classification
│   ├── cost_tracker.py        Token pricing, usage logging
│   ├── conversation.py        Message history, summarization
│   ├── claude_client.py       SDK client: caching, streaming, tool loop
│   ├── streaming.py           Telegram message edit manager
│   ├── telegram_handler.py    Command & message handlers
│   ├── tools.py               BashSession + TextEditorHandler
│   └── memory.py              Persistent memory system
├── .env.example
├── requirements.txt
├── claude-telegram.service    systemd unit
└── install.sh                 VPS installer
```

</details>

---

## Configuration

All config lives in `.env` (written automatically by the installer):

| Variable | Required | Default | Description |
|----------|:--------:|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | — | From [@BotFather](https://t.me/BotFather) |
| `ANTHROPIC_API_KEY` | Yes | — | From [console.anthropic.com](https://console.anthropic.com) |
| `ALLOWED_USER_IDS` | Yes | — | Comma-separated Telegram user IDs |
| `WORKSPACE_DIR` | | `~/claude-workspace` | Directory for files and bash sessions |
| `DB_PATH` | | `data/bot.db` | SQLite database path |
| `BOT_SOURCE_DIR` | | — | Path to this repo (enables self-modification) |
| `SONNET_MODEL` | | `claude-sonnet-4-6` | Model for complex messages |
| `HAIKU_MODEL` | | `claude-haiku-4-5-20251001` | Model for simple messages |
| `STREAM_EDIT_INTERVAL_MS` | | `1500` | Min ms between Telegram edits |
| `STREAM_MIN_CHARS` | | `50` | Min new chars before editing |
| `DAILY_COST_ALERT_USD` | | `5.0` | Daily spend alert threshold |

---

## Self-Modification

When `BOT_SOURCE_DIR` is set, Claude knows it **is** this bot. The system prompt includes its full source layout, and the text editor tool gets access to that directory.

Claude can:
- Read its own source to understand how it works
- Edit files to fix bugs or add features
- Commit and push changes via bash
- Restart itself with `/restart`

The installer configures this automatically — `BOT_SOURCE_DIR` is set to `/opt/claude-telegram` and the service user gets passwordless `sudo systemctl restart claude-telegram`.

For git push to work, configure credentials for the `claude-bot` user (SSH key or GitHub token).

---

## Cost Optimization

| Strategy | Savings |
|----------|---------|
| **Prompt caching** — system prompt marked with `cache_control: ephemeral` | ~90% on cached input tokens |
| **Model routing** — greetings and simple replies go to Haiku | ~3x cheaper per token |
| **Auto-summarization** — old messages compressed at 120K tokens | Prevents unbounded growth |
| **Output truncation** — bash output capped at 10K chars | Fewer wasted input tokens |

Track your spending with `/usage`.

---

## License

MIT
