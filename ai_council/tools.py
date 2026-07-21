"""
Read-only tools for the agentic synthesizer loop.

All file-system tools are sandboxed to a workspace_root resolved at call time.
No write, no bash, no network. The `think` tool is pure text reflection.

These tools are NOT exposed to the MCP client (Kilo). They are internal to the
synthesizer's tool-calling loop only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .logger import AICouncilLogger


class SandboxViolation(ValueError):
    """Raised when a tool attempts to read outside workspace_root."""


class ToolRegistry:
    """Holds the sandbox root and dispatches tool calls by name."""

    def __init__(
        self,
        workspace_root: str,
        allowed_tools: Optional[List[str]] = None,
        logger: Optional[AICouncilLogger] = None,
    ):
        self.workspace_root: Path = Path(workspace_root).resolve()
        if not self.workspace_root.exists():
            raise ValueError(f"workspace_root does not exist: {self.workspace_root}")
        if not self.workspace_root.is_dir():
            raise ValueError(f"workspace_root is not a directory: {self.workspace_root}")
        # None => no allowlist (every tool permitted). [] => an empty allowlist
        # (no tool permitted). `set(allowed_tools or [])` collapsed those two,
        # so `allowed_tools=[]` silently permitted everything.
        self.allowed: Optional[set] = None if allowed_tools is None else set(allowed_tools)
        self.logger = logger or AICouncilLogger()
        # --- Activity record -------------------------------------------------
        # One registry is built per consultant, so these accumulate exactly one
        # consultant's investigation. They are reported back to the orchestrator
        # so it can see WHICH evidence each perspective actually rests on: two
        # consultants disagreeing matters far less when one of them never opened
        # the relevant file. Ordered by first touch, deduplicated.
        self.files_read: List[str] = []
        self.paths_listed: List[str] = []
        self.call_counts: Dict[str, int] = {}

    def _record(self, bucket: List[str], rel_path: str) -> None:
        """Append to an activity list, preserving order and skipping repeats."""
        if rel_path not in bucket:
            bucket.append(rel_path)

    @property
    def activity(self) -> Dict[str, Any]:
        """A snapshot of what this consultant actually touched."""
        return {
            "files_read": list(self.files_read),
            "paths_listed": list(self.paths_listed),
            "tool_calls": dict(self.call_counts),
        }

    def _resolve(self, raw_path: str) -> Path:
        """Resolve a path strictly inside workspace_root.

        Relative paths are joined to workspace_root. Absolute paths must already
        be inside it. Symlinks are resolved before the boundary check.
        """
        p = Path(raw_path)
        if not p.is_absolute():
            p = self.workspace_root / p
        resolved = p.resolve()
        try:
            resolved.relative_to(self.workspace_root)
        except ValueError:
            raise SandboxViolation(
                f"Path '{raw_path}' resolves outside workspace_root "
                f"({self.workspace_root})"
            )
        return resolved

    # Hard cap on a single file read. Not overridable by the caller: read_file
    # takes no max_bytes argument, so a model cannot pull an arbitrarily large
    # file into its context by passing a bigger cap through the tool dispatcher.
    MAX_READ_BYTES = 200_000

    def read_file(self, path: str) -> str:
        """Read a UTF-8 text file, capped at MAX_READ_BYTES."""
        resolved = self._resolve(path)
        if not resolved.exists():
            return f"Error: file not found: {path}"
        if not resolved.is_file():
            return f"Error: not a file: {path}"
        try:
            raw = resolved.read_bytes()
        except Exception as e:
            return f"Error reading {path}: {e}"
        # Decide truncation from the BYTE length before decoding — comparing the
        # decoded character count against a byte cap mislabels multibyte files
        # (missing marker) and exact-size ASCII files (spurious marker).
        truncated = len(raw) > self.MAX_READ_BYTES
        text = raw[: self.MAX_READ_BYTES].decode("utf-8", errors="replace")
        if truncated:
            text += f"\n...[truncated at {self.MAX_READ_BYTES} bytes]"
        # Record only reads that actually returned content — a miss or a
        # sandbox rejection is not evidence the consultant's answer rests on.
        self._record(self.files_read, resolved.relative_to(self.workspace_root).as_posix())
        return text

    def list_dir(self, path: str = ".") -> str:
        """List directory entries one per line, with trailing / for dirs."""
        resolved = self._resolve(path)
        if not resolved.exists():
            return f"Error: directory not found: {path}"
        if not resolved.is_dir():
            return f"Error: not a directory: {path}"
        try:
            entries = sorted(resolved.iterdir(), key=lambda p: (p.is_file(), p.name))
        except Exception as e:
            return f"Error listing {path}: {e}"
        lines = []
        for entry in entries:
            rel = entry.relative_to(self.workspace_root)
            suffix = "/" if entry.is_dir() else ""
            lines.append(f"{rel}{suffix}")
        self._record(self.paths_listed, resolved.relative_to(self.workspace_root).as_posix() or ".")
        return "\n".join(lines) if lines else "(empty directory)"

    def glob_search(self, pattern: str, max_results: int = 100) -> str:
        """Glob match files inside workspace_root. Pattern is relative to root."""
        if not pattern:
            return "Error: empty pattern"
        # Patterns must be treated as relative to the workspace root.
        # We match via Path.glob from the root itself, after stripping any
        # leading absolute-looking prefix.
        clean = pattern.lstrip("/")
        try:
            matches = []
            for p in self.workspace_root.glob(clean):
                if not (p.is_file() or p.is_dir()):
                    continue
                # Enforce the sandbox boundary on every match. Path.glob does
                # NOT stop a '..' segment (or a symlink) from escaping the root,
                # so resolve and re-check — otherwise glob_search leaks the names
                # of files and directories OUTSIDE workspace_root even though
                # read_file/list_dir would reject the same paths.
                try:
                    rel = p.resolve().relative_to(self.workspace_root)
                except ValueError:
                    continue
                matches.append(rel.as_posix())
                if len(matches) >= max_results:
                    break
        except Exception as e:
            return f"Error running glob '{pattern}': {e}"
        matches.sort()
        if not matches:
            return f"No matches for '{pattern}'"
        return "\n".join(matches)

    def think(self, thought: str) -> str:
        """Pure reflection — echoes the thought back. No I/O."""
        return f"[noted] {thought}"

    def call(self, name: str, arguments: Dict[str, Any]) -> str:
        """Dispatch a tool call by name. Returns the tool result as a string."""
        if self.allowed is not None and name not in self.allowed:
            return f"Error: tool '{name}' is not in allowed_tools"
        dispatch: Dict[str, Callable[..., str]] = {
            "read_file": self.read_file,
            "list_dir": self.list_dir,
            "glob_search": self.glob_search,
            "think": self.think,
        }
        fn = dispatch.get(name)
        if fn is None:
            return f"Error: unknown tool '{name}'"
        # Count every dispatched call, including ones that go on to fail — the
        # count measures effort spent, unlike files_read which measures evidence.
        self.call_counts[name] = self.call_counts.get(name, 0) + 1
        try:
            return fn(**arguments)
        except SandboxViolation as e:
            self.logger.warning(f"Sandbox violation in {name}: {e}")
            return f"Error: {e}"
        except TypeError as e:
            return f"Error: bad arguments for {name}: {e}"
        except Exception as e:
            self.logger.error(f"Tool {name} failed: {e}")
            return f"Error in {name}: {e}"


# --- Ollama / OpenAI-compatible tool schemas ---------------------------------
# These are passed to the model via the `tools=` param so it knows what it can
# call. Keep descriptions tight — they shape how often the synthesizer fires.

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a UTF-8 text file inside the workspace. Use ONLY to verify "
                "a factual claim one of the systems made about specific code or "
                "docs. Do not read files speculatively."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to the workspace root (e.g. 'app/services/foo.py').",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": (
                "List entries in a directory inside the workspace. Use to confirm "
                "a file exists or to locate a sibling module. Returns names with "
                "trailing / for directories."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path relative to the workspace root. Defaults to the root.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob_search",
            "description": (
                "Find files by glob pattern (e.g. '**/*.py', 'app/**/routes.py'). "
                "Use sparingly to locate a file when you don't know the exact path."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern relative to the workspace root.",
                    }
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "think",
            "description": (
                "Reflect internally before responding. Use to structure your "
                "synthesis reasoning (agreements, disagreements, final answer). "
                "Does not touch the filesystem."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "thought": {
                        "type": "string",
                        "description": "Your internal reasoning step.",
                    }
                },
                "required": ["thought"],
            },
        },
    },
]


def filter_schemas(allowed: Optional[List[str]]) -> List[Dict[str, Any]]:
    """Return the tool schemas permitted by `allowed`.

    ``None`` means no allowlist — every schema is returned. An empty list means
    an empty allowlist — no schema is returned. (Previously both collapsed to
    "return everything", so an explicit empty allowlist advertised all tools.)
    """
    if allowed is None:
        return TOOL_SCHEMAS
    allowed_set = set(allowed)
    return [s for s in TOOL_SCHEMAS if s["function"]["name"] in allowed_set]