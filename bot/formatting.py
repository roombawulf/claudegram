from __future__ import annotations

import re
from html import escape


def markdown_to_telegram_html(text: str) -> str:
    """Convert Claude's markdown output to Telegram-compatible HTML."""
    # Escape HTML entities first, then convert markdown
    # We need to be careful about ordering to avoid double-escaping

    lines = text.split("\n")
    result_lines: list[str] = []
    in_code_block = False
    code_block_lang = ""
    code_block_lines: list[str] = []

    for line in lines:
        # Code block toggle
        if line.strip().startswith("```"):
            if not in_code_block:
                in_code_block = True
                code_block_lang = line.strip()[3:].strip()
                code_block_lines = []
            else:
                in_code_block = False
                code_content = escape("\n".join(code_block_lines))
                if code_block_lang:
                    result_lines.append(
                        f'<pre><code class="language-{escape(code_block_lang)}">{code_content}</code></pre>'
                    )
                else:
                    result_lines.append(f"<pre><code>{code_content}</code></pre>")
            continue

        if in_code_block:
            code_block_lines.append(line)
            continue

        # Process inline markdown
        line = _convert_inline(line)
        result_lines.append(line)

    # Handle unclosed code block
    if in_code_block:
        code_content = escape("\n".join(code_block_lines))
        result_lines.append(f"<pre><code>{code_content}</code></pre>")

    return "\n".join(result_lines)


def _convert_inline(line: str) -> str:
    """Convert inline markdown elements to HTML."""
    # Headers → bold
    header_match = re.match(r"^(#{1,6})\s+(.+)$", line)
    if header_match:
        content = escape(header_match.group(2))
        content = _inline_formatting(content)
        return f"\n<b>{content}</b>\n"

    # Blockquotes
    if line.startswith("> "):
        content = escape(line[2:])
        content = _inline_formatting(content)
        return f"<blockquote>{content}</blockquote>"

    # Bullet points
    bullet_match = re.match(r"^(\s*)[-*]\s+(.+)$", line)
    if bullet_match:
        indent = bullet_match.group(1)
        content = escape(bullet_match.group(2))
        content = _inline_formatting(content)
        prefix = "  " * (len(indent) // 2) if indent else ""
        return f"{prefix}\u2022 {content}"

    # Numbered lists - keep as is
    num_match = re.match(r"^(\s*)\d+\.\s+(.+)$", line)
    if num_match:
        content = escape(line)
        content = _inline_formatting(content)
        return content

    # Regular line
    escaped = escape(line)
    return _inline_formatting(escaped)


def _inline_formatting(text: str) -> str:
    """Apply inline formatting (bold, italic, code, links, strikethrough)."""
    # Inline code (must be first to avoid processing markdown inside code)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)

    # Links [text](url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)

    # Bold **text** or __text__
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__([^_]+)__", r"<b>\1</b>", text)

    # Italic *text* or _text_ (not inside words)
    text = re.sub(r"(?<!\w)\*([^*]+)\*(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"<i>\1</i>", text)

    # Strikethrough ~~text~~
    text = re.sub(r"~~([^~]+)~~", r"<s>\1</s>", text)

    return text


def split_message(text: str, max_length: int = 4096) -> list[str]:
    """Split a long message into chunks respecting Telegram's limits."""
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        # Try to split at paragraph boundary
        split_pos = remaining.rfind("\n\n", 0, max_length)
        if split_pos == -1:
            # Try single newline
            split_pos = remaining.rfind("\n", 0, max_length)
        if split_pos == -1:
            # Try space
            split_pos = remaining.rfind(" ", 0, max_length)
        if split_pos == -1:
            # Hard split
            split_pos = max_length

        chunks.append(remaining[:split_pos])
        remaining = remaining[split_pos:].lstrip("\n")

    return chunks
