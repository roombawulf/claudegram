from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path


def get_tool_definitions() -> list[dict]:
    """Return the list of tool definitions for the Anthropic API."""
    return [
        {"type": "bash_20250124", "name": "bash"},
        {"type": "text_editor_20250728", "name": "str_replace_based_edit_tool"},
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 5},
        {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 5},
    ]


# Commands that are too dangerous to run
BLOCKED_COMMANDS = [
    "rm -rf /",
    "rm -rf /*",
    "mkfs",
    "dd if=",
    ":(){:|:&};:",
    "chmod -R 777 /",
    "shutdown",
    "reboot",
    "halt",
    "init 0",
    "init 6",
]

MAX_OUTPUT_LENGTH = 10000


def _truncate_output(output: str) -> str:
    """Truncate output to MAX_OUTPUT_LENGTH, keeping head and tail."""
    if len(output) <= MAX_OUTPUT_LENGTH:
        return output
    head = output[:5000]
    tail = output[-3000:]
    truncated = len(output) - 8000
    return f"{head}\n\n... [{truncated} characters truncated] ...\n\n{tail}"


def _is_blocked(command: str) -> bool:
    """Check if a command is in the blocklist."""
    cmd_lower = command.strip().lower()
    for blocked in BLOCKED_COMMANDS:
        if blocked in cmd_lower:
            return True
    return False


class BashSession:
    """Persistent bash session using a subprocess."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def _ensure_started(self):
        if self._process is None or self._process.returncode is not None:
            self._process = await asyncio.create_subprocess_exec(
                "/bin/bash",
                "--norc",
                "--noprofile",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace),
                env={**os.environ, "HOME": str(self.workspace)},
            )

    async def execute(self, command: str, timeout: int = 30) -> str:
        """Execute a command and return combined stdout+stderr."""
        if _is_blocked(command):
            return "Error: This command is blocked for safety reasons."

        async with self._lock:
            await self._ensure_started()
            assert self._process is not None
            assert self._process.stdin is not None
            assert self._process.stdout is not None
            assert self._process.stderr is not None

            sentinel = f"__SENTINEL_{uuid.uuid4().hex[:8]}__"
            # Write command followed by sentinel echo
            full_cmd = f"{command}\necho {sentinel} $?\n"
            self._process.stdin.write(full_cmd.encode())
            await self._process.stdin.drain()

            # Read until sentinel
            output_lines: list[str] = []
            try:
                while True:
                    line = await asyncio.wait_for(
                        self._process.stdout.readline(), timeout=timeout
                    )
                    decoded = line.decode("utf-8", errors="replace")
                    if sentinel in decoded:
                        # Extract exit code
                        parts = decoded.strip().split()
                        exit_code = parts[-1] if parts else "0"
                        break
                    output_lines.append(decoded)
            except asyncio.TimeoutError:
                await self.restart()
                return f"Error: Command timed out after {timeout}s. Session restarted."

            # Also try to read any stderr that's available
            stderr_output = ""
            try:
                while True:
                    line = await asyncio.wait_for(
                        self._process.stderr.readline(), timeout=0.1
                    )
                    if not line:
                        break
                    stderr_output += line.decode("utf-8", errors="replace")
            except asyncio.TimeoutError:
                pass

            result = "".join(output_lines)
            if stderr_output:
                result = result + stderr_output

            if exit_code != "0":
                result = result + f"\n[exit code: {exit_code}]"

            return _truncate_output(result.strip())

    async def restart(self):
        """Kill and restart the bash session."""
        if self._process and self._process.returncode is None:
            try:
                self._process.kill()
                await self._process.wait()
            except ProcessLookupError:
                pass
        self._process = None

    async def close(self):
        """Close the bash session."""
        await self.restart()


class TextEditorHandler:
    """Handle text editor tool commands within the workspace."""

    def __init__(self, workspace: Path, allowed_paths: list[Path] | None = None):
        self.workspace = workspace
        # Additional directories the editor is allowed to access (e.g. bot source)
        self._allowed_roots: list[Path] = [workspace.resolve()]
        if allowed_paths:
            self._allowed_roots.extend(p.resolve() for p in allowed_paths)

    def _resolve_path(self, file_path: str) -> Path:
        """Resolve a path, allowing workspace and any extra allowed roots."""
        resolved = Path(file_path)
        if not resolved.is_absolute():
            resolved = self.workspace / resolved
        resolved = resolved.resolve()

        # Security: ensure path is within an allowed root
        for root in self._allowed_roots:
            if str(resolved).startswith(str(root)):
                return resolved

        raise ValueError(f"Path {file_path} is outside allowed directories")

        return resolved

    def handle(self, tool_input: dict) -> str:
        """Handle a text editor command."""
        command = tool_input.get("command")
        path = tool_input.get("path", "")

        try:
            if command == "view":
                return self._view(path, tool_input.get("view_range"))
            elif command == "str_replace":
                return self._str_replace(
                    path,
                    tool_input["old_str"],
                    tool_input["new_str"],
                )
            elif command == "create":
                return self._create(path, tool_input["file_text"])
            elif command == "insert":
                return self._insert(
                    path,
                    tool_input["insert_line"],
                    tool_input["new_str"],
                )
            else:
                return f"Error: Unknown command '{command}'"
        except (ValueError, KeyError, FileNotFoundError, OSError) as e:
            return f"Error: {e}"

    def _view(self, path: str, view_range: list[int] | None = None) -> str:
        resolved = self._resolve_path(path)

        if resolved.is_dir():
            entries = sorted(resolved.iterdir())
            lines = []
            for entry in entries[:100]:
                prefix = "dir " if entry.is_dir() else "file"
                lines.append(f"  {prefix}  {entry.name}")
            return f"Directory listing of {path}:\n" + "\n".join(lines)

        if not resolved.exists():
            return f"Error: File {path} does not exist"

        content = resolved.read_text(errors="replace")
        lines = content.split("\n")

        if view_range:
            start = max(1, view_range[0])
            end = min(len(lines), view_range[1])
            lines = lines[start - 1 : end]
            offset = start
        else:
            offset = 1

        numbered = []
        for i, line in enumerate(lines):
            numbered.append(f"{offset + i:6d}\t{line}")

        return "\n".join(numbered)

    def _str_replace(self, path: str, old_str: str, new_str: str) -> str:
        resolved = self._resolve_path(path)
        if not resolved.exists():
            return f"Error: File {path} does not exist"

        content = resolved.read_text(errors="replace")
        count = content.count(old_str)

        if count == 0:
            return f"Error: '{old_str[:50]}...' not found in {path}"
        if count > 1:
            return f"Error: '{old_str[:50]}...' found {count} times. Be more specific."

        new_content = content.replace(old_str, new_str, 1)
        resolved.write_text(new_content)
        return f"Successfully replaced text in {path}"

    def _create(self, path: str, file_text: str) -> str:
        resolved = self._resolve_path(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(file_text)
        return f"Successfully created {path}"

    def _insert(self, path: str, insert_line: int, new_str: str) -> str:
        resolved = self._resolve_path(path)
        if not resolved.exists():
            return f"Error: File {path} does not exist"

        content = resolved.read_text(errors="replace")
        lines = content.split("\n")
        idx = max(0, min(insert_line, len(lines)))
        new_lines = new_str.split("\n")
        lines[idx:idx] = new_lines
        resolved.write_text("\n".join(lines))
        return f"Successfully inserted {len(new_lines)} lines at line {insert_line} in {path}"


async def execute_tool(
    name: str,
    tool_input: dict,
    bash_session: BashSession,
    text_editor: TextEditorHandler,
) -> str:
    """Dispatch tool execution to the appropriate handler."""
    if name == "bash":
        command = tool_input.get("command", "")
        timeout = tool_input.get("timeout", 30)
        return await bash_session.execute(command, timeout=timeout)
    elif name == "str_replace_based_edit_tool":
        return text_editor.handle(tool_input)
    else:
        return f"Error: Unknown client tool '{name}'"
