from __future__ import annotations

import aiosqlite

# Pricing per 1M tokens
PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {
        "input": 3.0,
        "output": 15.0,
        "cache_write": 3.75,
        "cache_read": 0.30,
    },
    "claude-haiku-4-5-20251001": {
        "input": 1.0,
        "output": 5.0,
        "cache_write": 1.25,
        "cache_read": 0.10,
    },
}

WEB_SEARCH_COST = 0.01  # $10 per 1000 searches


def estimate_cost(model: str, usage: dict) -> float:
    """Estimate cost in USD from a usage dict."""
    rates = PRICING.get(model)
    if not rates:
        # Fallback to Sonnet pricing for unknown models
        rates = PRICING["claude-sonnet-4-6"]

    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0) or usage.get("cache_read_tokens", 0)
    cache_write = usage.get("cache_creation_input_tokens", 0) or usage.get("cache_write_tokens", 0)

    # Non-cached input tokens = total input - cache reads - cache writes
    regular_input = max(0, input_tokens - cache_read - cache_write)

    cost = (
        regular_input * rates["input"] / 1_000_000
        + output_tokens * rates["output"] / 1_000_000
        + cache_read * rates["cache_read"] / 1_000_000
        + cache_write * rates["cache_write"] / 1_000_000
    )
    return cost


async def log_usage(
    db: aiosqlite.Connection,
    user_id: int,
    conversation_id: str | None,
    model: str,
    usage: dict,
) -> float:
    """Log usage to the database and return estimated cost."""
    cost = estimate_cost(model, usage)

    await db.execute(
        """INSERT INTO usage_log
           (user_id, conversation_id, model, input_tokens, output_tokens,
            cache_read_tokens, cache_write_tokens, estimated_cost_usd)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id,
            conversation_id,
            model,
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            usage.get("cache_read_input_tokens", 0) or usage.get("cache_read_tokens", 0),
            usage.get("cache_creation_input_tokens", 0) or usage.get("cache_write_tokens", 0),
            cost,
        ),
    )
    await db.commit()
    return cost


async def get_daily_cost(db: aiosqlite.Connection, user_id: int) -> dict:
    """Get today's usage totals."""
    cursor = await db.execute(
        """SELECT
             COALESCE(SUM(input_tokens), 0) as input_tokens,
             COALESCE(SUM(output_tokens), 0) as output_tokens,
             COALESCE(SUM(cache_read_tokens), 0) as cache_read_tokens,
             COALESCE(SUM(cache_write_tokens), 0) as cache_write_tokens,
             COALESCE(SUM(estimated_cost_usd), 0.0) as total_cost,
             COUNT(*) as request_count
           FROM usage_log
           WHERE user_id = ? AND DATE(created_at) = DATE('now')""",
        (user_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else {}


async def get_monthly_cost(db: aiosqlite.Connection, user_id: int) -> dict:
    """Get this month's usage totals."""
    cursor = await db.execute(
        """SELECT
             COALESCE(SUM(input_tokens), 0) as input_tokens,
             COALESCE(SUM(output_tokens), 0) as output_tokens,
             COALESCE(SUM(cache_read_tokens), 0) as cache_read_tokens,
             COALESCE(SUM(cache_write_tokens), 0) as cache_write_tokens,
             COALESCE(SUM(estimated_cost_usd), 0.0) as total_cost,
             COUNT(*) as request_count
           FROM usage_log
           WHERE user_id = ? AND strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')""",
        (user_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else {}


async def format_usage_report(db: aiosqlite.Connection, user_id: int) -> str:
    """Generate a human-readable usage report."""
    daily = await get_daily_cost(db, user_id)
    monthly = await get_monthly_cost(db, user_id)

    def _fmt(data: dict) -> str:
        total = data.get("total_cost", 0) or 0
        inp = data.get("input_tokens", 0) or 0
        out = data.get("output_tokens", 0) or 0
        cached = data.get("cache_read_tokens", 0) or 0
        reqs = data.get("request_count", 0) or 0
        cache_pct = (cached / inp * 100) if inp > 0 else 0
        return (
            f"  Cost: ${total:.4f}\n"
            f"  Requests: {reqs}\n"
            f"  Input: {inp:,} tokens\n"
            f"  Output: {out:,} tokens\n"
            f"  Cache hit: {cache_pct:.1f}%"
        )

    return (
        f"📊 <b>Usage Report</b>\n\n"
        f"<b>Today:</b>\n{_fmt(daily)}\n\n"
        f"<b>This Month:</b>\n{_fmt(monthly)}"
    )
