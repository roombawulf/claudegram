from __future__ import annotations

import logging
from pathlib import Path

from telegram.ext import Application, CommandHandler, MessageHandler, filters

from .claude_client import ClaudeClient
from .config import Config
from .database import init_database
from .memory import Memory
from .telegram_handler import (
    cmd_memory,
    cmd_new,
    cmd_restart,
    cmd_start,
    cmd_status,
    cmd_usage,
    handle_document,
    handle_photo,
    handle_text,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def post_init(app: Application) -> None:
    """Initialize all components after the application is built."""
    config = Config.from_env()

    # Ensure workspace exists
    config.workspace_dir.mkdir(parents=True, exist_ok=True)

    # Initialize database
    db = await init_database(config.db_path)

    # Initialize memory
    memory = Memory(config.workspace_dir)

    # Initialize Claude client
    claude = ClaudeClient(config, db, memory)

    # Store in bot_data for access in handlers
    app.bot_data["config"] = config
    app.bot_data["db"] = db
    app.bot_data["memory"] = memory
    app.bot_data["claude"] = claude

    logger.info(
        "Bot initialized. Workspace: %s, Allowed users: %s",
        config.workspace_dir,
        config.allowed_user_ids,
    )

    restart_file = Path("/tmp/claudegram_restart_chat")
    if restart_file.exists():
        try:
            chat_id = int(restart_file.read_text().strip())
            await app.bot.send_message(chat_id, "Restarted.")
        except Exception:
            pass
        restart_file.unlink(missing_ok=True)


async def post_shutdown(app: Application) -> None:
    """Clean up resources on shutdown."""
    if "claude" in app.bot_data:
        await app.bot_data["claude"].close()
    if "db" in app.bot_data:
        await app.bot_data["db"].close()
    logger.info("Bot shut down.")


def main() -> None:
    """Entry point."""
    config = Config.from_env()

    app = (
        Application.builder()
        .token(config.telegram_bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Register command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("restart", cmd_restart))

    # Register message handlers
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    logger.info("Starting bot...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
