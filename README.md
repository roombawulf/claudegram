# Claude Telegram Bot

A Telegram bot powered by the Anthropic Python SDK with direct API calls — no CLI wrapper, no wasted tokens.

## Why This Exists

The common approach of shelling out to the Claude CLI (`subprocess.run()`) rebuilds full context on every message, resulting in a ~132:1 input:output token ratio and ~$13/day for moderate use. This bot uses the SDK directly, giving explicit control over:

- **Prompt caching** — system prompt + conversation prefix cached at 90% discount
- **Model routing** — simple messages go to Haiku (~3x cheaper), complex ones to Sonnet
- **Streaming** — real-time response updates in Telegram, no waiting for full generation
- **Conversation summarization** — auto-compresses history at 120K tokens instead of growing unbounded
- **Tool loop** — persistent bash session, file editor, web search, web fetch

## Features

- Streaming responses with live Telegram message edits
- Persistent bash session (stateful shell on your VPS)
- File editing with path sandboxing to workspace
- Web search and web fetch (Anthropic server-side tools)
- Image analysis (photos sent via Telegram)
- Document uploads (saved to workspace, content passed to Claude)
- Persistent memory system (JSON file Claude can read/write)
- Per-user conversation management with SQLite
- Cost tracking with daily/monthly reports and alerts
- Haiku/Sonnet auto-routing based on message complexity
- **Self-modifying** — Claude can read, edit, commit, and push its own source code

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and usage info |
| `/new` | Start a fresh conversation |
| `/model [haiku\|sonnet\|auto]` | Lock model or use auto-routing |
| `/usage` | Daily and monthly cost breakdown |
| `/memory` | View stored persistent memories |
| `/status` | Current session info (tokens, cost, conversation ID) |
| `/restart` | Restart the bot process (for applying code changes) |

## Architecture

```
Telegram <-> python-telegram-bot (async) <-> TelegramHandler
                                                    |
                                              ClaudeClient (Anthropic SDK)
                                              /     |      \
                                       Caching  Streaming  Tool Loop
                                                              |
                                    Client-side (we execute):
                                      bash, text_editor
                                    Server-side (Anthropic runs):
                                      web_search, web_fetch
```

## Project Structure

```
claude-telegram/
├── bot/
│   ├── main.py              # Entry point, Application lifecycle
│   ├── config.py             # .env loading, Config dataclass
│   ├── database.py           # SQLite schema + async operations
│   ├── formatting.py         # Markdown → Telegram HTML conversion
│   ├── model_router.py       # Haiku/Sonnet classification heuristic
│   ├── cost_tracker.py       # Token/cost logging, /usage reports
│   ├── conversation.py       # Message history, summarization
│   ├── claude_client.py      # Anthropic SDK: caching, streaming, tool loop
│   ├── streaming.py          # Telegram message edit manager
│   ├── telegram_handler.py   # All Telegram handlers (commands, text, photos, docs)
│   ├── tools.py              # BashSession + TextEditorHandler
│   └── memory.py             # Persistent memory system
├── .env.example
├── requirements.txt
├── claude-telegram.service   # systemd unit
└── install.sh                # VPS setup script
```

## Setup

### Local Development

```bash
git clone https://github.com/YOUR_USER/claude-telegram.git
cd claude-telegram

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env with your API keys

python -m bot.main
```

### VPS Deployment

```bash
sudo ./install.sh
sudo nano /opt/claude-telegram/.env  # Add your API keys
sudo systemctl start claude-telegram
sudo journalctl -u claude-telegram -f  # Watch logs
```

## Configuration

All config is via environment variables (`.env` file):

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | — | From [@BotFather](https://t.me/BotFather) |
| `ANTHROPIC_API_KEY` | Yes | — | From [console.anthropic.com](https://console.anthropic.com) |
| `ALLOWED_USER_IDS` | Yes | — | Comma-separated Telegram user IDs |
| `WORKSPACE_DIR` | No | `~/claude-workspace` | Directory for files and bash sessions |
| `DB_PATH` | No | `data/bot.db` | SQLite database location |
| `SONNET_MODEL` | No | `claude-sonnet-4-6` | Model ID for complex messages |
| `HAIKU_MODEL` | No | `claude-haiku-4-5-20251001` | Model ID for simple messages |
| `STREAM_EDIT_INTERVAL_MS` | No | `1500` | Min ms between Telegram message edits |
| `STREAM_MIN_CHARS` | No | `50` | Min new chars before editing message |
| `BOT_SOURCE_DIR` | No | — | Path to this repo on disk (enables self-modification) |
| `DAILY_COST_ALERT_USD` | No | `5.0` | Alert threshold for daily spending |

## Self-Modification

When `BOT_SOURCE_DIR` is set, Claude becomes aware that it *is* this bot. Its system prompt includes the full file layout of its own source code, and the text editor tool is granted access to that directory alongside the workspace.

This means Claude can:
- Read its own source to understand how it works
- Edit files to fix bugs or add features you ask for
- `git commit` and `git push` changes via the bash tool
- Restart itself with `sudo systemctl restart claude-telegram`

**Requirements:**
- Set `BOT_SOURCE_DIR=/opt/claude-telegram` (or wherever the repo lives) in `.env`
- The service user needs git configured with push credentials (SSH key or token)
- The service user needs passwordless `sudo systemctl restart claude-telegram` (add via `visudo`)

**Example sudoers line:**
```
claude-bot ALL=(ALL) NOPASSWD: /bin/systemctl restart claude-telegram
```

## Cost Optimization

The bot uses several strategies to minimize API costs:

1. **Prompt caching** — The system prompt has `cache_control: ephemeral`, so subsequent turns in a conversation pay ~10% for the cached portion
2. **Model routing** — Simple messages ("thanks", "ok", "hello") automatically route to Haiku ($1/$5 per 1M tokens) instead of Sonnet ($3/$15)
3. **Conversation summarization** — When context exceeds ~120K tokens, older messages are summarized via Haiku and replaced, preventing unbounded growth
4. **Tool output truncation** — Bash output capped at 10K chars to avoid wasting input tokens on verbose command output

## Tools

| Tool | Type | Description |
|------|------|-------------|
| `bash` | Client-side | Persistent shell session in workspace directory |
| `str_replace_based_edit_tool` | Client-side | View, create, and edit files (workspace + own source) |
| `web_search` | Server-side | Anthropic-hosted web search (max 5/turn) |
| `web_fetch` | Server-side | Anthropic-hosted page fetching (max 5/turn) |

## License

MIT
