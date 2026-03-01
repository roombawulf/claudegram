from __future__ import annotations

import asyncio
import logging
import platform
import socket
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

import anthropic

from .config import Config
from .cost_tracker import log_usage
from .memory import Memory
from .tools import BashSession, TextEditorHandler, execute_tool, get_tool_definitions, resolve_file_path

logger = logging.getLogger(__name__)

# Server-side tools - we don't execute these, Anthropic does
SERVER_TOOLS = {"web_search", "web_fetch"}


ALLOWED_SERVER_RESULT_KEYS = {"type", "tool_use_id", "content", "cache_control"}

CACHE_BREAKPOINT = {"type": "ephemeral"}


def _inject_cache_breakpoints(messages: list[dict]) -> None:
    """Ensure exactly one cache breakpoint on the last message in the history.

    Strips any existing breakpoints from messages first, then adds one to the
    last content block. Combined with the system prompt breakpoint, this gives
    2 total (well under the API limit of 4).
    """
    if not messages:
        return

    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block.pop("cache_control", None)

    last_msg = messages[-1]
    content = last_msg.get("content")

    if isinstance(content, list) and content:
        last_block = content[-1]
        if isinstance(last_block, dict):
            last_block["cache_control"] = CACHE_BREAKPOINT
    elif isinstance(content, str):
        last_msg["content"] = [
            {"type": "text", "text": content, "cache_control": CACHE_BREAKPOINT}
        ]


class ClaudeClient:
    """Wraps the Anthropic async client with caching, streaming, and tool loop."""

    def __init__(self, config: Config, db, memory: Memory):
        self.config = config
        self.db = db
        self.memory = memory
        self.client = anthropic.AsyncAnthropic(api_key=config.anthropic_api_key)
        self.tools = get_tool_definitions()

        # Per-user tool sessions
        self._bash_sessions: dict[int, BashSession] = {}
        self._text_editors: dict[int, TextEditorHandler] = {}

    def _get_bash_session(self, user_id: int) -> BashSession:
        if user_id not in self._bash_sessions:
            self._bash_sessions[user_id] = BashSession(self.config.workspace_dir)
        return self._bash_sessions[user_id]

    def _get_text_editor(self, user_id: int) -> TextEditorHandler:
        if user_id not in self._text_editors:
            allowed = []
            if self.config.bot_source_dir:
                allowed.append(self.config.bot_source_dir)
            self._text_editors[user_id] = TextEditorHandler(
                self.config.workspace_dir, allowed_paths=allowed
            )
        return self._text_editors[user_id]

    def _build_system_prompt(self) -> list[dict]:
        """Build system prompt with memory and environment context."""
        memory_text = self.memory.format_for_prompt()

        # List workspace contents
        workspace = self.config.workspace_dir
        workspace.mkdir(parents=True, exist_ok=True)
        try:
            entries = sorted(workspace.iterdir())[:50]
            workspace_listing = "\n".join(
                f"  {'dir ' if e.is_dir() else 'file'} {e.name}" for e in entries
            )
        except OSError:
            workspace_listing = "  (unable to list)"

        # Build self-awareness section if source dir is configured
        source_section = ""
        source_dir = self.config.bot_source_dir
        if source_dir and source_dir.exists():
            source_section = f"""
## Your Own Source Code
You ARE this bot. Your source code lives at: {source_dir}
The text editor tool has access to this directory — you can view and edit your own code.
You can also use bash to run git commands in that directory.

Key files:
  bot/main.py              — Entry point, handler registration
  bot/config.py            — Config dataclass, env var loading
  bot/claude_client.py     — THIS system prompt, streaming, tool loop (you are here)
  bot/telegram_handler.py  — Telegram command & message handlers
  bot/streaming.py         — Telegram message edit manager
  bot/conversation.py      — Message history, summarization
  bot/tools.py             — BashSession, TextEditorHandler, tool definitions
  bot/formatting.py        — Markdown to Telegram HTML conversion
  bot/model_router.py      — Haiku/Sonnet classification heuristic
  bot/cost_tracker.py      — Token pricing, usage logging
  bot/memory.py            — Persistent memory (memory.json)
  bot/database.py          — SQLite schema and helpers
  requirements.txt         — Python dependencies
  install.sh               — VPS deployment script
  claudegram.service       — systemd unit file

### Self-Modification Guidelines
- You can edit any of these files to fix bugs, add features, or improve yourself.
- After editing, commit and push with: `cd {source_dir} && git add -A && git commit -m "description" && git push`
- To apply changes, restart yourself: `sudo systemctl restart claudegram`
  (Note: this will end the current response. Tell the user you're restarting first.)
- Always explain what you're changing and why before making edits.
- Test changes mentally before applying — there's no staging environment.
- Be careful editing claude_client.py (this file) — a syntax error will prevent startup.
"""

        text = f"""You are Claude, a personal AI assistant running on a Linux VPS via Telegram.
You have access to tools for running bash commands, editing files, searching the web, and fetching web pages.

## Persistent Memory
{memory_text}

To save new memories, use the text editor tool to edit the memory.json file in your workspace.
The memory file is at: {workspace}/memory.json

## Environment
- Workspace: {workspace}
- Host: {socket.gethostname()}
- Platform: {platform.system()} {platform.release()}
- Date: {datetime.now().strftime("%Y-%m-%d %H:%M %Z")}
- Python: {platform.python_version()}

## Workspace Contents
{workspace_listing}
{source_section}
## Guidelines
- Be concise in responses — this is a Telegram chat, not a document.
- Use markdown formatting (the bot converts it to Telegram HTML).
- When running commands, prefer the workspace directory.
- For multi-step tasks, explain what you're doing briefly before each tool use.
- If a command might be destructive, confirm with the user first.
- Remember important facts and preferences by updating memory.json.

## Tool Notes
- bash: Persistent shell session in the workspace directory.
- str_replace_based_edit_tool: View, create, and edit files in the workspace{' and your own source code' if source_dir else ''}.
- web_search: Search the web (max 5 uses per turn).
- web_fetch: Fetch and read web pages (max 5 uses per turn).
- send_file: Send a file from the workspace to the user via Telegram. Use after creating/downloading a file. Images (jpg, png, gif, webp) are sent as photos; everything else as a document.
- send_telegram_widget: Send rich Telegram content — react to messages with emoji, send stickers from a set, attach inline URL buttons to your response, or send animated dice. Use naturally to add personality.
"""
        # Pad to ensure >2048 tokens for cache eligibility
        current_estimate = len(text) // 4
        if current_estimate < 2200:
            padding_needed = 2200 - current_estimate
            text += "\n" + " " * (padding_needed * 4)

        return [
            {
                "type": "text",
                "text": text,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    async def run_conversation_turn(
        self,
        messages: list[dict],
        model: str,
        user_id: int,
        conversation_id: str | None,
        on_text_chunk: Callable[[str], Any] | None = None,
        on_tool_status: Callable[[str, str], Any] | None = None,
        on_file_send: Callable[[str, str], Awaitable[str]] | None = None,
        on_widget_send: Callable[[dict], Awaitable[str]] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> tuple[list[dict], str]:
        """Run a full conversation turn with streaming and tool loop.

        Returns (assistant_content_blocks, stop_reason).
        """
        system = self._build_system_prompt()
        bash = self._get_bash_session(user_id)
        editor = self._get_text_editor(user_id)
        max_tool_rounds = 200  # safety net; cancel_event is the real limit

        all_content_blocks: list[dict] = []
        text_buffer = ""

        for round_num in range(max_tool_rounds):
            # Check for cancellation between tool rounds
            if cancel_event and cancel_event.is_set():
                if text_buffer:
                    all_content_blocks = [{"type": "text", "text": text_buffer}]
                return all_content_blocks, "cancelled"

            text_buffer = ""
            content_blocks: list[dict] = []
            current_tool_input = ""
            current_tool_name = ""
            cancelled_mid_stream = False

            try:
                _inject_cache_breakpoints(messages)
                async with self.client.messages.stream(
                    model=model,
                    max_tokens=8192,
                    system=system,
                    tools=self.tools,
                    messages=messages,
                ) as stream:
                    async for event in stream:
                        # Check for cancellation during streaming
                        if cancel_event and cancel_event.is_set():
                            cancelled_mid_stream = True
                            break

                        if event.type == "content_block_start":
                            if event.content_block.type == "text":
                                text_buffer = ""
                            elif event.content_block.type == "tool_use":
                                current_tool_name = event.content_block.name
                                current_tool_input = ""

                        elif event.type == "content_block_delta":
                            if event.delta.type == "text_delta":
                                text_buffer += event.delta.text
                                if on_text_chunk:
                                    await on_text_chunk(event.delta.text)
                            elif event.delta.type == "input_json_delta":
                                current_tool_input += event.delta.partial_json

                        elif event.type == "content_block_stop":
                            pass

                    if cancelled_mid_stream:
                        if text_buffer:
                            all_content_blocks = [{"type": "text", "text": text_buffer}]
                        return all_content_blocks, "cancelled"

                    # Get the final message for full content and usage
                    response = await stream.get_final_message()

            except anthropic.APIError as e:
                logger.error(f"Anthropic API error: {e}")
                return [{"type": "text", "text": f"API error: {e.message}"}], "error"

            # Log usage
            if response.usage:
                usage_dict = {
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
                    "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0),
                }
                await log_usage(self.db, user_id, conversation_id, model, usage_dict)

            # Extract content blocks, sanitizing server-side tool results
            # so they can be safely replayed in subsequent API calls
            content_blocks = []
            for block in response.content:
                if block.type == "text":
                    content_blocks.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    content_blocks.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })
                elif block.type == "server_tool_use":
                    content_blocks.append({
                        "type": "server_tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })
                elif hasattr(block, "type"):
                    if hasattr(block, "model_dump"):
                        dumped = block.model_dump()
                        sanitized = {k: v for k, v in dumped.items() if k in ALLOWED_SERVER_RESULT_KEYS}
                        content_blocks.append(sanitized)
                    else:
                        content_blocks.append({"type": block.type})

            all_content_blocks = content_blocks
            stop_reason = response.stop_reason

            # If no tool use, we're done
            if stop_reason == "end_turn":
                return all_content_blocks, stop_reason

            # Handle pause_turn (server-side tool loops)
            if stop_reason == "pause_turn":
                # Append sanitized content blocks and continue
                messages.append({"role": "assistant", "content": content_blocks})
                continue

            # Handle tool_use - execute client-side tools
            if stop_reason == "tool_use":
                tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

                if not tool_use_blocks:
                    return all_content_blocks, stop_reason

                # Append sanitized assistant message
                messages.append({"role": "assistant", "content": content_blocks})

                # Execute tools and collect results
                tool_results = []
                for tool_block in tool_use_blocks:
                    if tool_block.name in SERVER_TOOLS:
                        continue

                    # Show tool status
                    tool_desc = ""
                    if tool_block.name == "bash":
                        tool_desc = tool_block.input.get("command", "")[:100]
                    elif tool_block.name == "str_replace_based_edit_tool":
                        cmd = tool_block.input.get("command", "")
                        path = tool_block.input.get("path", "")
                        tool_desc = f"{cmd}: {path}"
                    elif tool_block.name == "send_file":
                        tool_desc = tool_block.input.get("path", "")
                    elif tool_block.name == "send_telegram_widget":
                        tool_desc = tool_block.input.get("type", "")

                    if on_tool_status and tool_desc:
                        await on_tool_status(tool_block.name, tool_desc)

                    # Handle send_file separately
                    if tool_block.name == "send_file":
                        result = await self._handle_send_file(
                            tool_block.input, user_id, on_file_send
                        )
                    elif tool_block.name == "send_telegram_widget":
                        if on_widget_send is None:
                            result = "Error: Widget sending is not available in this context."
                        else:
                            try:
                                result = await on_widget_send(tool_block.input)
                            except Exception as e:
                                logger.error(f"send_telegram_widget failed: {e}")
                                result = f"Error sending widget: {e}"
                    else:
                        result = await execute_tool(
                            tool_block.name,
                            tool_block.input,
                            bash,
                            editor,
                        )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_block.id,
                        "content": result,
                    })

                if tool_results:
                    messages.append({"role": "user", "content": tool_results})
                continue

            # Unknown stop reason
            return all_content_blocks, stop_reason

        # Exceeded safety limit — strip tool_use blocks to prevent orphans
        all_content_blocks = [
            b for b in all_content_blocks
            if not (isinstance(b, dict) and b.get("type") in ("tool_use", "server_tool_use"))
        ]
        return all_content_blocks, "end_turn"

    async def _handle_send_file(
        self,
        tool_input: dict,
        user_id: int,
        on_file_send: Callable[[str, str], Awaitable[str]] | None,
    ) -> str:
        """Resolve, validate, and send a file via the on_file_send callback."""
        file_path = tool_input.get("path", "")
        caption = tool_input.get("caption", "")

        if not file_path:
            return "Error: 'path' is required."

        # Resolve and sandbox the path
        try:
            allowed = []
            if self.config.bot_source_dir:
                allowed.append(self.config.bot_source_dir)
            resolved = resolve_file_path(file_path, self.config.workspace_dir, allowed)
        except ValueError as e:
            return f"Error: {e}"

        if not resolved.exists():
            return f"Error: File not found: {resolved}"

        if not resolved.is_file():
            return f"Error: Not a file: {resolved}"

        # 50 MB limit
        size = resolved.stat().st_size
        if size > 50 * 1024 * 1024:
            return f"Error: File too large ({size / 1024 / 1024:.1f} MB). Telegram limit is 50 MB."

        if on_file_send is None:
            return "Error: File sending is not available in this context."

        try:
            return await on_file_send(str(resolved), caption)
        except Exception as e:
            logger.error(f"send_file failed: {e}")
            return f"Error sending file: {e}"

    async def close(self):
        """Clean up resources."""
        for session in self._bash_sessions.values():
            await session.close()
        self._bash_sessions.clear()
        await self.client.close()
