from __future__ import annotations

import re

# Patterns that suggest simple messages suitable for Haiku
SIMPLE_PATTERNS = [
    r"^(hi|hello|hey|yo|sup|thanks|thank you|ok|okay|yes|no|yep|nope|sure|cool|nice|great|good|bye|goodbye|gn|gm)[\s!.?]*$",
    r"^(what time|what day|what date).*\??\s*$",
    r"^(how are you|how's it going|what's up).*\??\s*$",
]

# Patterns that suggest complex messages needing Sonnet
COMPLEX_PATTERNS = [
    r"(def |class |function |import |from |const |let |var |async |await )",
    r"(explain|analyze|compare|implement|refactor|debug|optimize|design|architect)",
    r"(write|create|build|make|generate)\s+(a |an |the )?(script|program|function|class|api|app|bot|server)",
    r"```",
    r"(how do i|how can i|how to|what is the best way to)",
]

SIMPLE_RE = [re.compile(p, re.IGNORECASE) for p in SIMPLE_PATTERNS]
COMPLEX_RE = [re.compile(p, re.IGNORECASE) for p in COMPLEX_PATTERNS]


def classify_message(text: str, has_attachments: bool = False) -> str:
    """Classify a message as 'haiku' or 'sonnet' based on heuristics.

    Returns the model tier, not the model ID.
    """
    if has_attachments:
        return "sonnet"

    # Long messages → Sonnet
    if len(text) > 200:
        return "sonnet"

    # Multiple questions → Sonnet
    if text.count("?") > 1:
        return "sonnet"

    # Check simple patterns first
    for pattern in SIMPLE_RE:
        if pattern.search(text):
            return "haiku"

    # Check complex patterns
    for pattern in COMPLEX_RE:
        if pattern.search(text):
            return "sonnet"

    # Default to Sonnet when uncertain
    return "sonnet"
