from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_active BOOLEAN DEFAULT 1
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    model TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
);

CREATE TABLE IF NOT EXISTS usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    conversation_id TEXT,
    model TEXT NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_log(user_id);
CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_log(created_at);
CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id, is_active);
"""


async def init_database(db_path: str) -> aiosqlite.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    db = await aiosqlite.connect(str(path))
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    await db.commit()
    return db


async def save_message(
    db: aiosqlite.Connection,
    conversation_id: str,
    role: str,
    content: list | str,
    model: str | None = None,
) -> int:
    content_json = json.dumps(content) if isinstance(content, list) else content
    cursor = await db.execute(
        "INSERT INTO messages (conversation_id, role, content, model) VALUES (?, ?, ?, ?)",
        (conversation_id, role, content_json, model),
    )
    await db.execute(
        "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (conversation_id,),
    )
    await db.commit()
    return cursor.lastrowid


async def get_messages(db: aiosqlite.Connection, conversation_id: str) -> list[dict]:
    cursor = await db.execute(
        "SELECT role, content, model FROM messages WHERE conversation_id = ? ORDER BY id",
        (conversation_id,),
    )
    rows = await cursor.fetchall()
    messages = []
    for row in rows:
        content = row["content"]
        try:
            content = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            pass
        messages.append({"role": row["role"], "content": content})
    return messages


async def get_active_conversation(db: aiosqlite.Connection, user_id: int) -> str | None:
    cursor = await db.execute(
        "SELECT id FROM conversations WHERE user_id = ? AND is_active = 1 ORDER BY updated_at DESC LIMIT 1",
        (user_id,),
    )
    row = await cursor.fetchone()
    return row["id"] if row else None


async def create_conversation(db: aiosqlite.Connection, conversation_id: str, user_id: int) -> str:
    await db.execute(
        "UPDATE conversations SET is_active = 0 WHERE user_id = ? AND is_active = 1",
        (user_id,),
    )
    await db.execute(
        "INSERT INTO conversations (id, user_id) VALUES (?, ?)",
        (conversation_id, user_id),
    )
    await db.commit()
    return conversation_id
