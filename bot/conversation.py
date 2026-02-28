from __future__ import annotations

import json
import uuid

import aiosqlite

from .database import (
    create_conversation,
    get_active_conversation,
    get_messages,
    save_message,
)

TOKEN_ESTIMATE_DIVISOR = 4  # rough: 1 token ≈ 4 chars
SUMMARIZE_THRESHOLD = 120_000  # tokens
SUMMARIZE_OLDEST_RATIO = 0.6


class ConversationManager:
    """Manage conversation state for a single user."""

    def __init__(self, db: aiosqlite.Connection, user_id: int, haiku_model: str):
        self.db = db
        self.user_id = user_id
        self.haiku_model = haiku_model
        self._conversation_id: str | None = None
        self._messages_cache: list[dict] = []

    async def get_or_create_conversation(self) -> str:
        """Get the active conversation or create a new one."""
        if self._conversation_id:
            return self._conversation_id

        conv_id = await get_active_conversation(self.db, self.user_id)
        if conv_id:
            self._conversation_id = conv_id
            self._messages_cache = await get_messages(self.db, conv_id)
        else:
            conv_id = str(uuid.uuid4())
            await create_conversation(self.db, conv_id, self.user_id)
            self._conversation_id = conv_id
            self._messages_cache = []

        return self._conversation_id

    async def add_user_message(self, content: list[dict] | str) -> None:
        """Add a user message to the conversation."""
        conv_id = await self.get_or_create_conversation()
        await save_message(self.db, conv_id, "user", content)
        self._messages_cache.append({"role": "user", "content": content})

    async def add_assistant_message(self, content: list[dict], model: str | None = None) -> None:
        """Add an assistant response to the conversation."""
        conv_id = await self.get_or_create_conversation()
        await save_message(self.db, conv_id, "assistant", content, model=model)
        self._messages_cache.append({"role": "assistant", "content": content})

    async def add_tool_result(self, tool_results: list[dict]) -> None:
        """Add tool results as a user message."""
        conv_id = await self.get_or_create_conversation()
        await save_message(self.db, conv_id, "user", tool_results)
        self._messages_cache.append({"role": "user", "content": tool_results})

    def get_messages_for_api(self) -> list[dict]:
        """Return messages formatted for the Anthropic API."""
        return list(self._messages_cache)

    def estimate_tokens(self) -> int:
        """Rough token count estimate for current conversation."""
        text = json.dumps(self._messages_cache)
        return len(text) // TOKEN_ESTIMATE_DIVISOR

    async def maybe_summarize(self, client) -> bool:
        """Summarize old messages if conversation is too long.

        Returns True if summarization was performed.
        """
        tokens = self.estimate_tokens()
        if tokens < SUMMARIZE_THRESHOLD:
            return False

        # Take oldest 60% of messages to summarize
        total = len(self._messages_cache)
        split_idx = int(total * SUMMARIZE_OLDEST_RATIO)
        if split_idx < 4:
            return False

        old_messages = self._messages_cache[:split_idx]
        remaining_messages = self._messages_cache[split_idx:]

        # Build summarization request
        summary_prompt = (
            "Summarize the following conversation concisely. "
            "Preserve key facts, decisions, file paths, code snippets, and important context. "
            "Format as a structured summary."
        )

        summary_messages = [
            {"role": "user", "content": f"{summary_prompt}\n\n{json.dumps(old_messages, indent=2)}"}
        ]

        try:
            response = await client.messages.create(
                model=self.haiku_model,
                max_tokens=2048,
                messages=summary_messages,
            )
            summary_text = response.content[0].text
        except Exception:
            return False

        # Replace old messages with summary pair
        summary_pair = [
            {"role": "user", "content": "[Conversation summary from earlier messages]"},
            {"role": "assistant", "content": summary_text},
        ]

        self._messages_cache = summary_pair + remaining_messages

        # Rebuild DB messages for this conversation
        conv_id = await self.get_or_create_conversation()
        await self.db.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
        for msg in self._messages_cache:
            await save_message(self.db, conv_id, msg["role"], msg["content"])

        return True

    async def reset(self) -> str:
        """Create a new conversation, deactivating the current one."""
        self._conversation_id = None
        self._messages_cache = []
        conv_id = str(uuid.uuid4())
        await create_conversation(self.db, conv_id, self.user_id)
        self._conversation_id = conv_id
        return conv_id
