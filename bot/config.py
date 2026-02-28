from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Config:
    telegram_bot_token: str
    anthropic_api_key: str
    allowed_user_ids: list[int]

    workspace_dir: Path = field(default_factory=lambda: Path.home() / "claude-workspace")
    bot_source_dir: Path | None = None
    db_path: str = "data/bot.db"

    model: str = "claude-sonnet-4-6"

    stream_edit_interval_ms: int = 1500
    stream_min_chars: int = 50

    daily_cost_alert_usd: float = 5.0

    @classmethod
    def from_env(cls) -> Config:
        load_dotenv()

        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        user_ids_raw = os.environ.get("ALLOWED_USER_IDS", "")

        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is required")
        if not user_ids_raw:
            raise ValueError("ALLOWED_USER_IDS is required")

        allowed_user_ids = [int(uid.strip()) for uid in user_ids_raw.split(",") if uid.strip()]

        workspace_dir = Path(os.environ.get("WORKSPACE_DIR", "~/claude-workspace")).expanduser()

        bot_source_raw = os.environ.get("BOT_SOURCE_DIR", "")
        bot_source_dir = Path(bot_source_raw).expanduser().resolve() if bot_source_raw else None

        return cls(
            telegram_bot_token=token,
            anthropic_api_key=api_key,
            allowed_user_ids=allowed_user_ids,
            workspace_dir=workspace_dir,
            bot_source_dir=bot_source_dir,
            db_path=os.environ.get("DB_PATH", "data/bot.db"),
            model=os.environ.get("MODEL", "claude-sonnet-4-6"),
            stream_edit_interval_ms=int(os.environ.get("STREAM_EDIT_INTERVAL_MS", "1500")),
            stream_min_chars=int(os.environ.get("STREAM_MIN_CHARS", "50")),
            daily_cost_alert_usd=float(os.environ.get("DAILY_COST_ALERT_USD", "5.0")),
        )
