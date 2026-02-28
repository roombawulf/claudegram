from __future__ import annotations

import json
from pathlib import Path


class Memory:
    """Persistent memory system backed by a JSON file in the workspace."""

    def __init__(self, workspace: Path):
        self.path = workspace / "memory.json"

    def load(self) -> dict:
        """Load memory from disk."""
        if not self.path.exists():
            return {"facts": [], "preferences": {}, "projects": {}}
        try:
            return json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return {"facts": [], "preferences": {}, "projects": {}}

    def save(self, data: dict) -> None:
        """Write memory atomically."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        tmp.rename(self.path)

    def format_for_prompt(self) -> str:
        """Format memory contents for inclusion in system prompt."""
        data = self.load()

        parts: list[str] = []

        if data.get("facts"):
            parts.append("## Remembered Facts")
            for fact in data["facts"]:
                parts.append(f"- {fact}")

        if data.get("preferences"):
            parts.append("\n## User Preferences")
            for key, value in data["preferences"].items():
                parts.append(f"- {key}: {value}")

        if data.get("projects"):
            parts.append("\n## Projects")
            for name, info in data["projects"].items():
                parts.append(f"### {name}")
                if isinstance(info, dict):
                    for k, v in info.items():
                        parts.append(f"- {k}: {v}")
                else:
                    parts.append(f"- {info}")

        if not parts:
            return "(No memories stored yet. You can save memories by editing the memory.json file in the workspace.)"

        return "\n".join(parts)
