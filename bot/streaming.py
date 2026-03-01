from __future__ import annotations

import asyncio
import logging
import time
from html import escape

from telegram import Chat, Message
from telegram.constants import ChatAction, ParseMode
from telegram.error import RetryAfter, TimedOut

from .config import Config
from .formatting import markdown_to_telegram_html, split_message

logger = logging.getLogger(__name__)


class StreamingResponseManager:
    """Manages streaming text updates to a Telegram message with rate limiting.

    Instead of sending a placeholder message up-front, this manager delays
    sending the first message until real text content is available.  In the
    meantime it pulses a chat action (typing / upload_document) every 4 s so
    the user always sees feedback.
    """

    def __init__(self, chat: Chat, reply_to_message: Message, config: Config):
        self.chat = chat
        self.reply_to_message = reply_to_message
        self.config = config

        self.message: Message | None = None  # created lazily
        self.buffer = ""
        self.last_edit_time = 0.0
        self.last_edit_length = 0
        self.edit_interval = config.stream_edit_interval_ms / 1000.0
        self.min_chars = config.stream_min_chars
        self._pending_edit: asyncio.Task | None = None
        self._finalized = False
        self.reply_markup = None  # optional InlineKeyboardMarkup

        # Chat-action pulse
        self._phase: str = "typing"  # "typing" | "tool"
        self._pulse_task: asyncio.Task | None = None
        self._start_pulse()

    # ------------------------------------------------------------------
    # Chat action pulse
    # ------------------------------------------------------------------

    def _start_pulse(self) -> None:
        self._pulse_task = asyncio.create_task(self._pulse_loop())

    async def _pulse_loop(self) -> None:
        """Send a chat action every 4 s until cancelled."""
        try:
            while True:
                action = (
                    ChatAction.UPLOAD_DOCUMENT
                    if self._phase == "tool"
                    else ChatAction.TYPING
                )
                try:
                    await self.chat.send_action(action)
                except Exception:
                    pass  # best-effort
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass

    def _stop_pulse(self) -> None:
        if self._pulse_task is not None:
            self._pulse_task.cancel()
            self._pulse_task = None

    def set_reply_markup(self, markup) -> None:
        """Set an InlineKeyboardMarkup to attach when the message is finalized."""
        self.reply_markup = markup

    # ------------------------------------------------------------------
    # Lazy first-message creation
    # ------------------------------------------------------------------

    async def _ensure_message(self, text: str, parse_mode: str = ParseMode.HTML) -> None:
        """Send the very first reply (push notification shows real text)."""
        try:
            self.message = await self.chat.send_message(
                text[:4096],
                parse_mode=parse_mode,
                reply_to_message_id=self.reply_to_message.message_id,
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.error(f"Failed to send first message: {e}")

    # ------------------------------------------------------------------
    # Public streaming callbacks
    # ------------------------------------------------------------------

    async def on_chunk(self, text_delta: str) -> None:
        """Accumulate text and throttle edits."""
        if self._finalized:
            return

        self._phase = "typing"
        self.buffer += text_delta

        now = time.monotonic()
        elapsed = now - self.last_edit_time
        new_chars = len(self.buffer) - self.last_edit_length

        if elapsed >= self.edit_interval and new_chars >= self.min_chars:
            await self._do_edit()

    async def on_tool_status(self, tool_name: str, description: str) -> None:
        """Show a tool execution status message."""
        if self._finalized:
            return

        self._phase = "tool"

        desc_escaped = escape(description)
        status = f"\u2699\ufe0f Running <code>{escape(tool_name)}</code>: <code>{desc_escaped}</code>..."

        if self.buffer:
            display = escape(self.buffer) + f"\n\n{status}"
        else:
            display = status

        # Only edit/send if we already have a message; otherwise let the
        # UPLOAD_DOCUMENT pulse handle the visual feedback.
        if self.message is not None:
            await self._safe_edit(display, parse_mode=ParseMode.HTML)

    # ------------------------------------------------------------------
    # Internal editing helpers
    # ------------------------------------------------------------------

    async def _do_edit(self) -> None:
        """Edit the message with current buffer (plain text during streaming)."""
        if not self.buffer:
            return

        display = escape(self.buffer) + " \u2588"

        if self.message is None:
            await self._ensure_message(display)
        else:
            await self._safe_edit(display, parse_mode=ParseMode.HTML)

        self.last_edit_time = time.monotonic()
        self.last_edit_length = len(self.buffer)

    async def _safe_edit(self, text: str, parse_mode: str = ParseMode.HTML) -> None:
        """Edit message with retry handling for Telegram rate limits."""
        if self.message is None or not text.strip():
            return

        try:
            await self.message.edit_text(
                text[:4096],
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
        except RetryAfter as e:
            logger.warning(f"Telegram rate limit, waiting {e.retry_after}s")
            await asyncio.sleep(e.retry_after + 0.5)
            try:
                await self.message.edit_text(
                    text[:4096],
                    parse_mode=parse_mode,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
        except TimedOut:
            logger.warning("Telegram edit timed out")
        except Exception as e:
            if "can't parse" in str(e).lower():
                try:
                    await self.message.edit_text(
                        escape(self.buffer[:4096]) if self.buffer else text[:4096],
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                except Exception:
                    pass
            else:
                logger.error(f"Edit failed: {e}")

    # ------------------------------------------------------------------
    # Finalize / stop
    # ------------------------------------------------------------------

    async def finalize(self, final_text: str) -> list[Message]:
        """Send the final formatted response, splitting if needed."""
        self._finalized = True
        self._stop_pulse()
        messages_sent: list[Message] = []

        if not final_text.strip():
            final_text = "(Empty response)"

        html = markdown_to_telegram_html(final_text)
        chunks = split_message(html)

        for i, chunk in enumerate(chunks):
            # Only attach reply_markup to the first chunk
            markup = self.reply_markup if i == 0 else None

            if i == 0 and self.message is not None:
                # Edit the existing message
                try:
                    await self.message.edit_text(
                        chunk,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                        reply_markup=markup,
                    )
                    messages_sent.append(self.message)
                except Exception:
                    try:
                        plain_chunks = split_message(escape(final_text))
                        await self.message.edit_text(
                            plain_chunks[0],
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                            reply_markup=markup,
                        )
                        messages_sent.append(self.message)
                    except Exception as e:
                        logger.error(f"Failed to finalize message: {e}")
            else:
                # No existing message yet (i==0) or additional chunks (i>0)
                try:
                    kwargs = {}
                    if i == 0:
                        kwargs["reply_to_message_id"] = self.reply_to_message.message_id
                    msg = await self.chat.send_message(
                        chunk,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                        reply_markup=markup,
                        **kwargs,
                    )
                    if i == 0:
                        self.message = msg
                    messages_sent.append(msg)
                except Exception:
                    try:
                        kwargs = {}
                        if i == 0:
                            kwargs["reply_to_message_id"] = self.reply_to_message.message_id
                        msg = await self.chat.send_message(
                            escape(chunk),
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                            reply_markup=markup,
                            **kwargs,
                        )
                        if i == 0:
                            self.message = msg
                        messages_sent.append(msg)
                    except Exception as e:
                        logger.error(f"Failed to send chunk: {e}")

        return messages_sent

    def stop(self) -> None:
        """Cancel the pulse task (call on error paths)."""
        self._stop_pulse()
