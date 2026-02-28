from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes

from .claude_client import ClaudeClient
from .config import Config
from .conversation import ConversationManager
from .cost_tracker import format_usage_report, get_daily_cost
from .model_router import classify_message
from .streaming import StreamingResponseManager

logger = logging.getLogger(__name__)


def _is_authorized(user_id: int, config: Config) -> bool:
    return user_id in config.allowed_user_ids


def _get_conversation_manager(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> ConversationManager:
    """Get or create a ConversationManager for this user."""
    managers: dict = context.bot_data.setdefault("conversation_managers", {})
    if user_id not in managers:
        db = context.bot_data["db"]
        config: Config = context.bot_data["config"]
        managers[user_id] = ConversationManager(db, user_id, config.haiku_model)
    return managers[user_id]


def _get_model(context: ContextTypes.DEFAULT_TYPE, user_id: int, tier: str) -> str:
    """Resolve model tier to model ID, respecting overrides."""
    config: Config = context.bot_data["config"]
    overrides: dict = context.bot_data.setdefault("model_overrides", {})

    override = overrides.get(user_id)
    if override == "haiku":
        return config.haiku_model
    elif override == "sonnet":
        return config.sonnet_model
    elif override:
        return override  # custom model string

    return config.haiku_model if tier == "haiku" else config.sonnet_model


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    config: Config = context.bot_data["config"]
    if not _is_authorized(update.effective_user.id, config):
        await update.message.reply_text("Unauthorized.")
        return

    await update.message.reply_text(
        "<b>Claude Telegram Bot</b>\n\n"
        "I'm Claude, running with direct API access for fast, cost-efficient responses.\n\n"
        "<b>Commands:</b>\n"
        "/new - Start a new conversation\n"
        "/model [haiku|sonnet|auto] - Set model\n"
        "/usage - View cost & usage stats\n"
        "/memory - View stored memories\n"
        "/status - Current session info\n"
        "/restart - Restart the bot process\n\n"
        "Send me any message to get started!",
        parse_mode=ParseMode.HTML,
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /new - reset conversation."""
    config: Config = context.bot_data["config"]
    if not _is_authorized(update.effective_user.id, config):
        await update.message.reply_text("Unauthorized.")
        return

    manager = _get_conversation_manager(context, update.effective_user.id)
    await manager.reset()
    await update.message.reply_text("New conversation started.")


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /model - set model override."""
    config: Config = context.bot_data["config"]
    if not _is_authorized(update.effective_user.id, config):
        await update.message.reply_text("Unauthorized.")
        return

    overrides: dict = context.bot_data.setdefault("model_overrides", {})
    args = context.args

    if not args:
        current = overrides.get(update.effective_user.id, "auto")
        await update.message.reply_text(
            f"Current model: <b>{current}</b>\n\n"
            f"Usage: /model [haiku|sonnet|auto]",
            parse_mode=ParseMode.HTML,
        )
        return

    choice = args[0].lower()
    if choice == "auto":
        overrides.pop(update.effective_user.id, None)
        await update.message.reply_text("Model set to <b>auto</b> (router decides).", parse_mode=ParseMode.HTML)
    elif choice in ("haiku", "sonnet"):
        overrides[update.effective_user.id] = choice
        await update.message.reply_text(f"Model locked to <b>{choice}</b>.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("Unknown model. Use: haiku, sonnet, or auto.")


async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /usage - show cost breakdown."""
    config: Config = context.bot_data["config"]
    if not _is_authorized(update.effective_user.id, config):
        await update.message.reply_text("Unauthorized.")
        return

    db = context.bot_data["db"]
    report = await format_usage_report(db, update.effective_user.id)
    await update.message.reply_text(report, parse_mode=ParseMode.HTML)


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /memory - show stored memories."""
    config: Config = context.bot_data["config"]
    if not _is_authorized(update.effective_user.id, config):
        await update.message.reply_text("Unauthorized.")
        return

    from .memory import Memory
    memory = Memory(config.workspace_dir)
    text = memory.format_for_prompt()
    await update.message.reply_text(f"<b>Stored Memories:</b>\n\n{text}", parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status - show session info."""
    config: Config = context.bot_data["config"]
    if not _is_authorized(update.effective_user.id, config):
        await update.message.reply_text("Unauthorized.")
        return

    manager = _get_conversation_manager(context, update.effective_user.id)
    conv_id = await manager.get_or_create_conversation()
    msg_count = len(manager.get_messages_for_api())
    est_tokens = manager.estimate_tokens()

    overrides: dict = context.bot_data.setdefault("model_overrides", {})
    model_setting = overrides.get(update.effective_user.id, "auto")

    db = context.bot_data["db"]
    daily = await get_daily_cost(db, update.effective_user.id)

    await update.message.reply_text(
        f"<b>Session Status</b>\n\n"
        f"Conversation: <code>{conv_id[:8]}...</code>\n"
        f"Messages: {msg_count}\n"
        f"Est. tokens: ~{est_tokens:,}\n"
        f"Model: {model_setting}\n"
        f"Today's cost: ${daily.get('total_cost', 0):.4f}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /restart - restart the bot process via systemd."""
    config: Config = context.bot_data["config"]
    if not _is_authorized(update.effective_user.id, config):
        await update.message.reply_text("Unauthorized.")
        return

    await update.message.reply_text("Restarting bot process...")

    # Small delay so the reply actually sends before we die
    await asyncio.sleep(1)

    # This will kill our own process; systemd will restart it
    proc = await asyncio.create_subprocess_exec(
        "sudo", "systemctl", "restart", "claudegram",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    # If we get here, the restart didn't kill us (e.g. sudo failed)
    if proc.returncode != 0:
        err = stderr.decode().strip()
        await update.message.reply_text(
            f"Restart failed (exit {proc.returncode}):\n<code>{err}</code>",
            parse_mode=ParseMode.HTML,
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle regular text messages."""
    config: Config = context.bot_data["config"]
    user_id = update.effective_user.id

    if not _is_authorized(user_id, config):
        await update.message.reply_text("Unauthorized.")
        return

    text = update.message.text or ""
    if not text.strip():
        return

    # Classify and select model
    tier = classify_message(text)
    model = _get_model(context, user_id, tier)

    # Get conversation manager
    manager = _get_conversation_manager(context, user_id)
    claude: ClaudeClient = context.bot_data["claude"]

    # Add user message
    await manager.add_user_message(text)

    # Maybe summarize if conversation is getting long
    await manager.maybe_summarize(claude.client)

    # Send typing indicator and placeholder message
    await update.message.chat.send_action(ChatAction.TYPING)
    placeholder = await update.message.reply_text("\u2026")

    # Set up streaming
    streamer = StreamingResponseManager(placeholder, config)

    # Build messages for API
    messages = manager.get_messages_for_api()
    conv_id = manager._conversation_id

    # Run the conversation turn
    content_blocks, stop_reason = await claude.run_conversation_turn(
        messages=messages,
        model=model,
        user_id=user_id,
        conversation_id=conv_id,
        on_text_chunk=streamer.on_chunk,
        on_tool_status=streamer.on_tool_status,
    )

    # Extract final text
    final_text = ""
    for block in content_blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            final_text += block["text"]

    # Store assistant response
    await manager.add_assistant_message(content_blocks, model=model)

    # Finalize the streamed message
    await streamer.finalize(final_text or "(No text response)")

    # Check daily cost alert
    db = context.bot_data["db"]
    daily = await get_daily_cost(db, user_id)
    daily_cost = daily.get("total_cost", 0) or 0
    if daily_cost > config.daily_cost_alert_usd:
        await update.message.reply_text(
            f"\u26a0\ufe0f Daily cost alert: ${daily_cost:.2f} (limit: ${config.daily_cost_alert_usd:.2f})",
        )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo messages - send as image content block."""
    config: Config = context.bot_data["config"]
    user_id = update.effective_user.id

    if not _is_authorized(user_id, config):
        await update.message.reply_text("Unauthorized.")
        return

    # Get highest resolution photo
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)

    # Download to bytes
    photo_bytes = await file.download_as_bytearray()
    b64_data = base64.standard_b64encode(bytes(photo_bytes)).decode("utf-8")

    # Build content blocks
    caption = update.message.caption or "What's in this image?"
    content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": b64_data,
            },
        },
        {"type": "text", "text": caption},
    ]

    # Always use Sonnet for images
    model = _get_model(context, user_id, "sonnet")

    manager = _get_conversation_manager(context, user_id)
    claude: ClaudeClient = context.bot_data["claude"]

    await manager.add_user_message(content)

    await update.message.chat.send_action(ChatAction.TYPING)
    placeholder = await update.message.reply_text("\u2026")
    streamer = StreamingResponseManager(placeholder, config)

    messages = manager.get_messages_for_api()
    conv_id = manager._conversation_id

    content_blocks, _ = await claude.run_conversation_turn(
        messages=messages,
        model=model,
        user_id=user_id,
        conversation_id=conv_id,
        on_text_chunk=streamer.on_chunk,
        on_tool_status=streamer.on_tool_status,
    )

    final_text = ""
    for block in content_blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            final_text += block["text"]

    await manager.add_assistant_message(content_blocks, model=model)
    await streamer.finalize(final_text or "(No text response)")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle document uploads - save to workspace and pass to Claude."""
    config: Config = context.bot_data["config"]
    user_id = update.effective_user.id

    if not _is_authorized(user_id, config):
        await update.message.reply_text("Unauthorized.")
        return

    doc = update.message.document
    file = await context.bot.get_file(doc.file_id)

    # Save to workspace
    save_path = config.workspace_dir / "uploads" / doc.file_name
    save_path.parent.mkdir(parents=True, exist_ok=True)
    await file.download_to_drive(str(save_path))

    # Try to read as text
    file_content = ""
    try:
        file_content = save_path.read_text(errors="replace")[:10000]
    except Exception:
        file_content = "(Binary file - saved to workspace)"

    caption = update.message.caption or f"I've uploaded a file: {doc.file_name}"
    text = f"{caption}\n\nFile saved to: {save_path}\n\nContent preview:\n```\n{file_content[:3000]}\n```"

    model = _get_model(context, user_id, "sonnet")

    manager = _get_conversation_manager(context, user_id)
    claude: ClaudeClient = context.bot_data["claude"]

    await manager.add_user_message(text)

    await update.message.chat.send_action(ChatAction.TYPING)
    placeholder = await update.message.reply_text("\u2026")
    streamer = StreamingResponseManager(placeholder, config)

    messages = manager.get_messages_for_api()
    conv_id = manager._conversation_id

    content_blocks, _ = await claude.run_conversation_turn(
        messages=messages,
        model=model,
        user_id=user_id,
        conversation_id=conv_id,
        on_text_chunk=streamer.on_chunk,
        on_tool_status=streamer.on_tool_status,
    )

    final_text = ""
    for block in content_blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            final_text += block["text"]

    await manager.add_assistant_message(content_blocks, model=model)
    await streamer.finalize(final_text or "(No text response)")
