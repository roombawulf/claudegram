from __future__ import annotations

import asyncio
import logging
import time
from html import escape

from telegram import Message
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut

from .config import Config
from .formatting import markdown_to_telegram_html, split_message

logger = logging.getLogger(__name__)


class StreamingResponseManager:
    """Manages streaming text updates to a Telegram message with rate limiting."""

    def __init__(self, message: Message, config: Config):
        self.message = message
        self.config = config
        self.buffer = ""
        self.last_edit_time = 0.0
        self.last_edit_length = 0
        self.edit_interval = config.stream_edit_interval_ms / 1000.0
        self.min_chars = config.stream_min_chars
        self._pending_edit: asyncio.Task | None = None
        self._finalized = False

    async def on_chunk(self, text_delta: str) -> None:
        """Accumulate text and throttle edits."""
        if self._finalized:
            return

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

        desc_escaped = escape(description)
        status = f"\u2699\ufe0f Running <code>{escape(tool_name)}</code>: <code>{desc_escaped}</code>..."

        if self.buffer:
            # Show accumulated text + tool status
            display = escape(self.buffer) + f"\n\n{status}"
        else:
            display = status

        await self._safe_edit(display, parse_mode=ParseMode.HTML)

    async def _do_edit(self) -> None:
        """Edit the message with current buffer (plain text during streaming)."""
        if not self.buffer:
            return

        # During streaming: show escaped plain text with typing indicator
        display = escape(self.buffer) + " \u2588"
        await self._safe_edit(display, parse_mode=ParseMode.HTML)
        self.last_edit_time = time.monotonic()
        self.last_edit_length = len(self.buffer)

    async def _safe_edit(self, text: str, parse_mode: str = ParseMode.HTML) -> None:
        """Edit message with retry handling for Telegram rate limits."""
        if not text.strip():
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
            # If HTML parsing fails, fall back to plain text
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

    async def finalize(self, final_text: str) -> list[Message]:
        """Send the final formatted response, splitting if needed."""
        self._finalized = True
        messages_sent: list[Message] = []

        if not final_text.strip():
            final_text = "(Empty response)"

        # Convert markdown to Telegram HTML
        html = markdown_to_telegram_html(final_text)
        chunks = split_message(html)

        for i, chunk in enumerate(chunks):
            if i == 0:
                # Edit the existing placeholder message
                try:
                    await self.message.edit_text(
                        chunk,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                    messages_sent.append(self.message)
                except Exception:
                    # Fall back to escaped text
                    try:
                        plain_chunks = split_message(escape(final_text))
                        await self.message.edit_text(
                            plain_chunks[0],
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                        )
                        messages_sent.append(self.message)
                    except Exception as e:
                        logger.error(f"Failed to finalize message: {e}")
            else:
                # Send additional chunks as new messages
                try:
                    msg = await self.message.chat.send_message(
                        chunk,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                    messages_sent.append(msg)
                except Exception:
                    try:
                        msg = await self.message.chat.send_message(
                            escape(chunk),
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                        )
                        messages_sent.append(msg)
                    except Exception as e:
                        logger.error(f"Failed to send chunk: {e}")

        return messages_sent
