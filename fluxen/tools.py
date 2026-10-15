"""Workspace tools: files, search, edits, Git, shell and undo."""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "dist",
    "build",
}
MAX_LIST = 300
MAX_MATCHES = 100
MAX_SEARCH_FILE = 1_000_000
DEFAULT_READ_LINES = 400

# name -> (description, {param: (json type, description, required)})
SPECS: dict[str, tuple[str, dict[str, tuple[str, str, bool]]]] = {
    "read_file": (
        "Read a UTF-8 workspace file. Large files are returned in pages of lines; use offset/limit to read further.",
        {
            "path": ("string", "File path relative to the workspace", True),
            "offset": ("integer", "First line to return, 1-based (default 1)", False),
            "limit": ("integer", f"Maximum lines to return (default {DEFAULT_READ_LINES})", False),
        },
    ),
    "list_files": (
        "List files in the workspace (excluding common ignored directories)",
        {"path": ("string", "Directory relative to the workspace; '.' for the root", False)},
    ),
    "search": (
        "Search workspace text files for a literal string",
        {"query": ("string", "Literal text to find", True)},
    ),
    "edit_file": (
        "Replace an exact string in a workspace file. old_string must match exactly once "
        "unless replace_all is true. Prefer this over write_file for changes to existing files.",
        {
            "path": ("string", "File path relative to the workspace", True),
            "old_string": ("string", "Exact text to replace, including whitespace", True),
            "new_string": ("string", "Replacement text", True),
            "replace_all": ("boolean", "Replace every occurrence (default false)", False),
        },
    ),
    "write_file": (
        "Create a new UTF-8 workspace file or overwrite one completely",
        {
            "path": ("string", "File path relative to the workspace", True),
            "content": ("string", "Full file content", True),
        },
    ),
    "delete_file": (
        "Delete a workspace file",
        {"path": ("string", "File path relative to the workspace", True)},
    ),
    "run_shell": (
        "Run a shell command in the workspace (requires approval)",
        {"command": ("string", "Shell command", True)},
    ),
    "git_status": ("Show Git status", {}),
    "git_diff": ("Show working tree Git diff", {}),
}
MUTATING = {"write_file", "edit_file", "delete_file"}
RISKY = MUTATING | {"run_shell"}


def schemas() -> list[dict]:
    result = []
    for name, (description, params) in SPECS.items():
        properties = {key: {"type": kind, "description": text} for key, (kind, text, _) in params.items()}
        required = [key for key, (_, _, needed) in params.items() if needed]
        result.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                        "additionalProperties": False,
                    },
                },
            }
        )
    return result


def clip(text: str, limit: int) -> str:
    """Truncate text, saying how much was left out so the model knows it is incomplete."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated: {len(text) - limit} more characters not shown]"


def _string(args: dict, key: str, default: str | None = None) -> str:
    value = args.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"'{key}' must be a string")
    return value


def _integer(args: dict, key: str, default: int) -> int:
    value = args.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"'{key}' must be an integer") from None


def _walk(base: Path):
    """Yield regular, non-symlink files under base, pruning ignored and symlinked directories."""
    for root, dirs, files in os.walk(base, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS and not (Path(root) / d).is_symlink())
        for name in sorted(files):
            path = Path(root) / name
            if not path.is_symlink():
                yield path


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill a process and everything it started, then reap it without hanging on open pipes."""
    if proc.returncode is None:
        if sys.platform == "win32":
            with contextlib.suppress(OSError):
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    check=False,
                )
        else:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    # A grandchild that escaped the group could still hold the pipe; don't wait for it forever.
    with contextlib.suppress(asyncio.TimeoutError, Exception):
        await asyncio.wait_for(proc.communicate(), 5)


async def _run(argv_or_command: str | list[str], cwd: Path, timeout: float) -> tuple[int | None, str]:
    """Run a command in its own process group, returning (exit code or None on timeout, output)."""
    options: dict[str, Any] = {
        "cwd": cwd,
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.STDOUT,
    }
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    if isinstance(argv_or_command, str):
        proc = await asyncio.create_subprocess_shell(argv_or_command, **options)
    else:
        proc = await asyncio.create_subprocess_exec(*argv_or_command, **options)
    try:
        data, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        await _kill_tree(proc)
        return None, ""
    except asyncio.CancelledError:
        await _kill_tree(proc)
        raise
    return proc.returncode, data.decode(errors="replace")


class Workspace:
    def __init__(self, root: Path, limit: int = 12000, shell_timeout: float = 120):
        self.root = root.resolve(strict=True)
        self.limit = limit
        self.shell_timeout = shell_timeout
        self.undo: list[tuple[Path, bytes | None]] = []
        self.last_diff = ""

    def path(self, value: str) -> Path:
        p = (self.root / value).resolve()
        if not p.is_relative_to(self.root):
            raise PermissionError("Path escapes workspace")
        if p == self.root:
            raise PermissionError("Expected a file or subdirectory")
        return p

    def target(self, value: str) -> Path:
        """Path for a write/edit/delete. Git metadata is off limits: hooks and config can run commands."""
        p = self.path(value)
        if any(part.rstrip(". ").lower() == ".git" for part in p.relative_to(self.root).parts):
            raise PermissionError("Refusing to modify .git")
        return p

    def _edited(self, args: dict) -> tuple[Path, str, str]:
        """Return (path, old text, new text) for an edit_file call, validating the match."""
        p = self.target(_string(args, "path"))
        if not p.is_file():
            raise FileNotFoundError(f"{args['path']} is not an existing file")
        old_string = _string(args, "old_string")
        new_string = _string(args, "new_string")
        if not old_string:
            raise ValueError("old_string must not be empty; use write_file to create files")
        # Decode bytes directly so the file's own line endings are preserved.
        text = p.read_bytes().decode("utf-8")
        if "\r\n" in text and "\r\n" not in old_string and old_string not in text:
            old_string = old_string.replace("\n", "\r\n")
            new_string = new_string.replace("\n", "\r\n")
        count = text.count(old_string)
        if count == 0:
            raise ValueError("old_string not found; read the file and copy the exact text")
        if count > 1 and not args.get("replace_all"):
            raise ValueError(
                f"old_string matches {count} times; add surrounding lines to make it unique or set replace_all"
            )
        return p, text, text.replace(old_string, new_string)

    def preview(self, name: str, args: dict) -> str:
        if name == "edit_file":
            p, old, new = self._edited(args)
        else:
            p = self.target(_string(args, "path"))
            if name == "delete_file":
                body = p.read_text(errors="replace")[:3000] if p.is_file() else "(not a file)"
                return f"DELETE {p.relative_to(self.root)}\n{body}"
            old = p.read_text(errors="replace") if p.exists() else ""
            new = _string(args, "content")
        rel = p.relative_to(self.root).as_posix()
        diff = difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
        return clip("".join(diff), self.limit)

    async def execute(self, name: str, args: dict) -> str:
        if name == "read_file":
            p = self.path(_string(args, "path"))
            if not p.is_file():
                return "Not a file"
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            offset = max(1, _integer(args, "offset", 1))
            limit = max(1, _integer(args, "limit", DEFAULT_READ_LINES))
            chunk = lines[offset - 1 : offset - 1 + limit]
            text = "".join(chunk)
            if len(text) > self.limit:
                # Cut at a line boundary so the next offset is exact.
                kept: list[str] = []
                size = 0
                for line in chunk:
                    if kept and size + len(line) > self.limit:
                        break
                    kept.append(line)
                    size += len(line)
                chunk = kept
                text = clip("".join(chunk), self.limit)
            end = offset - 1 + len(chunk)
            if offset > 1 or end < len(lines):
                if not chunk:
                    return f"[file has {len(lines)} lines; offset {offset} is past the end]"
                text += f"\n[showing lines {offset}-{end} of {len(lines)}" + (
                    f"; use offset={end + 1} to continue]" if end < len(lines) else "]"
                )
            return text
        if name == "list_files":
            value = args.get("path")
            base = self.root if value in (None, "", ".") else self.path(_string(args, "path"))
            if not base.is_dir():
                return "Not a directory"
            found: list[str] = []
            for p in _walk(base):
                if len(found) >= MAX_LIST:
                    found.append(f"…[stopped after {MAX_LIST} files; list a subdirectory]")
                    break
                found.append(p.relative_to(self.root).as_posix())
            return "\n".join(found) or "(empty)"
        if name == "search":
            query = _string(args, "query")
            if not query:
                return "Empty query"
            results: list[str] = []
            for p in _walk(self.root):
                try:
                    if p.stat().st_size > MAX_SEARCH_FILE:
                        continue
                    with p.open(encoding="utf-8") as handle:
                        for i, line in enumerate(handle, 1):
                            if query in line:
                                rel = p.relative_to(self.root).as_posix()
                                results.append(f"{rel}:{i}: {line.rstrip()[:250]}")
                                if len(results) >= MAX_MATCHES:
                                    break
                except (UnicodeError, OSError):
                    continue
                if len(results) >= MAX_MATCHES:
                    results.append(f"…[stopped after {MAX_MATCHES} matches; refine the query]")
                    break
            return clip("\n".join(results), self.limit) or "No matches"
        if name in MUTATING:
            if name == "edit_file":
                p, _, new_text = self._edited(args)
            else:
                p = self.target(_string(args, "path"))
                new_text = _string(args, "content") if name == "write_file" else ""
            if p.exists() and not p.is_file():
                raise ValueError("Not a regular file")
            original = p.read_bytes() if p.exists() else None
            if name == "delete_file":
                if original is None:
                    return "Already absent"
                self.last_diff = self.preview(name, args)
                p.unlink()
            else:
                self.last_diff = self.preview(name, args)
                p.parent.mkdir(parents=True, exist_ok=True)
                # Re-resolve after directory creation, to reject symlink races in ordinary cases.
                if not p.resolve().is_relative_to(self.root):
                    raise PermissionError("Path escapes workspace")
                tmp = p.with_name(p.name + ".fluxen-tmp")
                with tmp.open("w", encoding="utf-8", newline="" if name == "edit_file" else None) as out:
                    out.write(new_text)
                os.replace(tmp, p)
            self.undo.append((p, original))
            return f"{name}: {p.relative_to(self.root).as_posix()}"
        if name == "run_shell":
            code, output = await _run(_string(args, "command"), self.root, self.shell_timeout)
            if code is None:
                return f"Command timed out after {self.shell_timeout:g}s and was stopped"
            return f"exit={code}\n" + clip(output, self.limit)
        if name in ("git_status", "git_diff"):
            argv = ["git", "status", "--short"] if name == "git_status" else ["git", "diff", "--no-ext-diff"]
            code, output = await _run(argv, self.root, 15)
            if code is None:
                return "git timed out after 15s"
            return clip(output, self.limit) or ("Clean working tree" if name == "git_status" else "No changes")
        raise ValueError("Unknown tool: " + name)

    def undo_last(self) -> str:
        if not self.undo:
            return "Nothing to undo in this process"
        p, old = self.undo.pop()
        if old is None:
            if p.exists():
                p.unlink()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(old)
        return f"Undid change to {p.relative_to(self.root).as_posix()}"
