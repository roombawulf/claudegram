from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from .claude_client import ClaudeClient
from .config import Config
from .conversation import ConversationManager
from .cost_tracker import format_usage_report, get_daily_cost
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
        managers[user_id] = ConversationManager(db, user_id, config.model)
    return managers[user_id]



PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png"}
ANIMATION_EXTENSIONS = {".gif", ".webp"}


async def _persist_intermediate_messages(
    manager: ConversationManager, intermediate: list[dict]
) -> None:
    """Persist tool loop assistant+tool_result pairs to the conversation.

    Only saves properly paired messages (assistant followed by user tool_result).
    Skips unpaired assistant messages (e.g. from pause_turn server tool rounds).
    """
    i = 0
    while i < len(intermediate):
        msg = intermediate[i]
        next_msg = intermediate[i + 1] if i + 1 < len(intermediate) else None

        if (
            msg["role"] == "assistant"
            and next_msg is not None
            and next_msg["role"] == "user"
        ):
            # Valid assistant + tool_result pair
            await manager.add_assistant_message(msg["content"])
            await manager.add_tool_result(next_msg["content"])
            i += 2
        else:
            # Skip unpaired messages (e.g. pause_turn server tool rounds)
            i += 1


def _make_file_sender(chat):
    """Create an on_file_send callback bound to a Telegram chat."""

    async def send_file_to_user(path: str, caption: str) -> str:
        file_path = Path(path)
        if not file_path.exists():
            return f"Error: File not found: {path}"
        if not file_path.is_file():
            return f"Error: Not a file: {path}"
        size = file_path.stat().st_size
        if size > 50 * 1024 * 1024:
            return f"Error: File too large ({size / 1024 / 1024:.1f} MB). Telegram limit is 50 MB."

        try:
            suffix = file_path.suffix.lower()
            with open(file_path, "rb") as f:
                if suffix in ANIMATION_EXTENSIONS:
                    await chat.send_animation(animation=f, caption=caption or None)
                elif suffix in PHOTO_EXTENSIONS:
                    await chat.send_photo(photo=f, caption=caption or None)
                else:
                    await chat.send_document(document=f, caption=caption or None)
            return "File sent successfully."
        except Exception as e:
            return f"Error sending file: {e}"

    return send_file_to_user


def _make_widget_sender(chat, reply_to_message, streamer, bot):
    """Create an on_widget_send callback for rich Telegram content."""

    async def send_widget(params: dict) -> str:
        widget_type = params.get("type", "")
        emoji = params.get("emoji", "")

        if widget_type == "reaction":
            if not emoji:
                return "Error: 'emoji' is required for reaction type."
            try:
                from telegram import ReactionTypeEmoji

                await reply_to_message.set_reaction(
                    [ReactionTypeEmoji(emoji=emoji)]
                )
                return f"Reacted with {emoji}"
            except Exception as e:
                return f"Error setting reaction: {e}"

        elif widget_type == "sticker":
            set_name = params.get("sticker_set_name", "")
            if not set_name:
                return "Error: 'sticker_set_name' is required for sticker type."
            try:
                sticker_set = await bot.get_sticker_set(set_name)
                # Find a sticker matching the emoji, or use the first one
                match = None
                if emoji:
                    for s in sticker_set.stickers:
                        if s.emoji == emoji:
                            match = s
                            break
                if match is None:
                    match = sticker_set.stickers[0] if sticker_set.stickers else None
                if match is None:
                    return f"Error: Sticker set '{set_name}' is empty."
                await chat.send_sticker(match.file_id)
                return f"Sent sticker from {set_name}" + (f" (matched {match.emoji})" if match.emoji else "")
            except Exception as e:
                return f"Error sending sticker: {e}"

        elif widget_type == "inline_buttons":
            buttons = params.get("buttons", [])
            if not buttons:
                return "Error: 'buttons' array is required for inline_buttons type."
            try:
                keyboard = []
                for row in buttons:
                    keyboard.append([
                        InlineKeyboardButton(text=btn["text"], url=btn["url"])
                        for btn in row
                    ])
                streamer.set_reply_markup(InlineKeyboardMarkup(keyboard))
                return "Buttons will be attached to your response."
            except Exception as e:
                return f"Error building inline buttons: {e}"

        elif widget_type == "dice":
            dice_emoji = emoji or "🎲"
            try:
                await chat.send_dice(emoji=dice_emoji)
                return f"Sent dice {dice_emoji}"
            except Exception as e:
                return f"Error sending dice: {e}"

        else:
            return f"Error: Unknown widget type '{widget_type}'."

    return send_widget


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

    db = context.bot_data["db"]
    daily = await get_daily_cost(db, update.effective_user.id)

    await update.message.reply_text(
        f"<b>Session Status</b>\n\n"
        f"Conversation: <code>{conv_id[:8]}...</code>\n"
        f"Messages: {msg_count}\n"
        f"Est. tokens: ~{est_tokens:,}\n"
        f"Model: {config.model}\n"
        f"Today's cost: ${daily.get('total_cost', 0):.4f}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /restart - restart the bot process via systemd."""
    config: Config = context.bot_data["config"]
    if not _is_authorized(update.effective_user.id, config):
        await update.message.reply_text("Unauthorized.")
        return

    await update.message.reply_text("Restarting...")
    Path("/tmp/claudegram_restart_chat").write_text(str(update.effective_chat.id))
    await asyncio.sleep(1)

    await asyncio.create_subprocess_exec(
        "sudo", "systemctl", "restart", "claudegram",
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

    model = config.model

    # Get conversation manager
    manager = _get_conversation_manager(context, user_id)
    claude: ClaudeClient = context.bot_data["claude"]

    # Add user message
    await manager.add_user_message(text)

    # Maybe summarize if conversation is getting long
    await manager.maybe_summarize(claude.client)

    # Set up streaming (message sent lazily when real content arrives)
    streamer = StreamingResponseManager(
        chat=update.message.chat,
        reply_to_message=update.message,
        config=config,
    )

    # Build messages for API
    messages = manager.get_messages_for_api()
    original_msg_count = len(messages)
    conv_id = manager._conversation_id

    widget_sender = _make_widget_sender(
        update.message.chat, update.message, streamer, context.bot,
    )

    # Get cancel event from the update processor
    processor = context.bot_data.get("update_processor")
    cancel_event = processor.get_cancel_event(user_id) if processor else None

    try:
        # Run the conversation turn
        content_blocks, stop_reason = await claude.run_conversation_turn(
            messages=messages,
            model=model,
            user_id=user_id,
            conversation_id=conv_id,
            on_text_chunk=streamer.on_chunk,
            on_tool_status=streamer.on_tool_status,
            on_file_send=_make_file_sender(update.message.chat),
            on_widget_send=widget_sender,
            cancel_event=cancel_event,
        )
    except asyncio.CancelledError:
        content_blocks = []
        stop_reason = "cancelled"
    except Exception:
        streamer.stop()
        raise

    if stop_reason == "cancelled":
        # Kill any running bash command
        bash = claude._get_bash_session(user_id)
        await bash.cancel()

        # Persist completed intermediate pairs
        await _persist_intermediate_messages(manager, messages[original_msg_count:])

        # Save partial response
        partial = streamer.buffer
        if partial:
            await manager.add_assistant_message(
                [{"type": "text", "text": partial + "\n\n[interrupted]"}]
            )
            await streamer.finalize(partial + "\n\n_[interrupted]_")
        else:
            streamer.stop()
        return

    # Persist intermediate tool loop messages (assistant+tool_result pairs)
    await _persist_intermediate_messages(manager, messages[original_msg_count:])

    # Extract final text
    final_text = ""
    for block in content_blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            final_text += block["text"]

    # Store assistant response (strip any tool_use blocks — they belong to
    # intermediate messages which were already persisted above)
    save_blocks = [
        b for b in content_blocks
        if not (isinstance(b, dict) and b.get("type") in ("tool_use", "server_tool_use"))
    ]
    if not save_blocks:
        save_blocks = [{"type": "text", "text": final_text or "(Completed)"}]
    await manager.add_assistant_message(save_blocks, model=model)

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

    model = config.model

    manager = _get_conversation_manager(context, user_id)
    claude: ClaudeClient = context.bot_data["claude"]

    await manager.add_user_message(content)

    streamer = StreamingResponseManager(
        chat=update.message.chat,
        reply_to_message=update.message,
        config=config,
    )

    messages = manager.get_messages_for_api()
    original_msg_count = len(messages)
    conv_id = manager._conversation_id

    widget_sender = _make_widget_sender(
        update.message.chat, update.message, streamer, context.bot,
    )

    processor = context.bot_data.get("update_processor")
    cancel_event = processor.get_cancel_event(user_id) if processor else None

    try:
        content_blocks, stop_reason = await claude.run_conversation_turn(
            messages=messages,
            model=model,
            user_id=user_id,
            conversation_id=conv_id,
            on_text_chunk=streamer.on_chunk,
            on_tool_status=streamer.on_tool_status,
            on_file_send=_make_file_sender(update.message.chat),
            on_widget_send=widget_sender,
            cancel_event=cancel_event,
        )
    except asyncio.CancelledError:
        content_blocks = []
        stop_reason = "cancelled"
    except Exception:
        streamer.stop()
        raise

    if stop_reason == "cancelled":
        bash = claude._get_bash_session(user_id)
        await bash.cancel()
        await _persist_intermediate_messages(manager, messages[original_msg_count:])
        partial = streamer.buffer
        if partial:
            await manager.add_assistant_message(
                [{"type": "text", "text": partial + "\n\n[interrupted]"}]
            )
            await streamer.finalize(partial + "\n\n_[interrupted]_")
        else:
            streamer.stop()
        return

    await _persist_intermediate_messages(manager, messages[original_msg_count:])

    final_text = ""
    for block in content_blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            final_text += block["text"]

    save_blocks = [
        b for b in content_blocks
        if not (isinstance(b, dict) and b.get("type") in ("tool_use", "server_tool_use"))
    ]
    if not save_blocks:
        save_blocks = [{"type": "text", "text": final_text or "(Completed)"}]
    await manager.add_assistant_message(save_blocks, model=model)
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

    model = config.model

    manager = _get_conversation_manager(context, user_id)
    claude: ClaudeClient = context.bot_data["claude"]

    await manager.add_user_message(text)

    streamer = StreamingResponseManager(
        chat=update.message.chat,
        reply_to_message=update.message,
        config=config,
    )

    messages = manager.get_messages_for_api()
    original_msg_count = len(messages)
    conv_id = manager._conversation_id

    widget_sender = _make_widget_sender(
        update.message.chat, update.message, streamer, context.bot,
    )

    processor = context.bot_data.get("update_processor")
    cancel_event = processor.get_cancel_event(user_id) if processor else None

    try:
        content_blocks, stop_reason = await claude.run_conversation_turn(
            messages=messages,
            model=model,
            user_id=user_id,
            conversation_id=conv_id,
            on_text_chunk=streamer.on_chunk,
            on_tool_status=streamer.on_tool_status,
            on_file_send=_make_file_sender(update.message.chat),
            on_widget_send=widget_sender,
            cancel_event=cancel_event,
        )
    except asyncio.CancelledError:
        content_blocks = []
        stop_reason = "cancelled"
    except Exception:
        streamer.stop()
        raise

    if stop_reason == "cancelled":
        bash = claude._get_bash_session(user_id)
        await bash.cancel()
        await _persist_intermediate_messages(manager, messages[original_msg_count:])
        partial = streamer.buffer
        if partial:
            await manager.add_assistant_message(
                [{"type": "text", "text": partial + "\n\n[interrupted]"}]
            )
            await streamer.finalize(partial + "\n\n_[interrupted]_")
        else:
            streamer.stop()
        return

    await _persist_intermediate_messages(manager, messages[original_msg_count:])

    final_text = ""
    for block in content_blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            final_text += block["text"]

    save_blocks = [
        b for b in content_blocks
        if not (isinstance(b, dict) and b.get("type") in ("tool_use", "server_tool_use"))
    ]
    if not save_blocks:
        save_blocks = [{"type": "text", "text": final_text or "(Completed)"}]
    await manager.add_assistant_message(save_blocks, model=model)
    await streamer.finalize(final_text or "(No text response)")
