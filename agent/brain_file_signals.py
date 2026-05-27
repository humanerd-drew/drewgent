import re
from typing import Optional

# ── Brain signal: file-path extraction from tool results ──────────────────────
# Maps tool name → JSON key that carries the affected file path in the result.
_FILE_PATH_KEYS = {
    "write_file": "path",
    "patch": "path",
    "terminal": None,  # Terminal requires special handling via command parsing
}
_FILE_PATH_PATTERNS = [
    r'"path"\s*:\s*"([^"]+)"',
    r"'path'\s*:\s*'([^']+)'",
    r"File path[:\s]+([^\n]+)",
    r"Wrote to ([^\n]+)",
    r"Created directory[s]?\s+(.+)",
    r"bytes_written.*?([^\n]+)",
]

# Terminal command patterns for file path extraction
_TERMINAL_FILE_PATTERNS = [
    # Redirection: echo "..." >> path
    re.compile(r">>\s*([^\s>]+)"),
    # Redirect with space: cat > path
    re.compile(r">\s+([^\s]+)"),
    # Path-based commands: patch path/to/file, write path/to/file
    re.compile(r"(?:patch|write_file|edit|copy|move)\s+([^\s]+)"),
    # mkdir/create path
    re.compile(r"(?:mkdir|touch|mkdir\s+-p)\s+([^\s]+)"),
    # sed/perl path replacement
    re.compile(r"(?:sed|perl).*?\s+-i(?:_\w+)?\s+['\"]([^'\"]+)['\"]"),
]


def _extract_file_path_from_tool_args(tool_name: str, args: dict) -> Optional[str]:
    """Extract file path from tool arguments for known file-modifying tools."""
    key = _FILE_PATH_KEYS.get(tool_name)
    if key is None:
        return None
    path = args.get(key) if isinstance(args, dict) else None
    if isinstance(path, str) and path.strip():
        return path.strip()
    return None


def _extract_file_path_from_result(result: str, tool_name: str) -> Optional[str]:
    """Extract file path from tool result JSON or terminal output.

    Used by brain signals to track which files are being modified during
    an integration workflow.
    """
    if not isinstance(result, str):
        return None

    # Terminal tool: parse file paths from command output
    if tool_name == "terminal":
        for pattern in _TERMINAL_FILE_PATTERNS:
            m = pattern.search(result)
            if m:
                candidate = m.group(1).strip()
                if _looks_like_path(candidate):
                    return candidate
        return None

    # Fast path: try to parse as JSON and look for 'path' key
    try:
        parsed = json.loads(result)
        if isinstance(parsed, dict):
            # Direct path field
            path = parsed.get("path")
            if isinstance(path, str) and path.strip():
                return path.strip()
            # dirs_created might contain the created directory path
            if tool_name == "write_file":
                dirs = parsed.get("dirs_created")
                if isinstance(dirs, list) and dirs:
                    return dirs[0]
    except (json.JSONDecodeError, TypeError):
        pass

    # Slow path: regex search for common file-path patterns in the result text
    for pattern in _FILE_PATH_PATTERNS:
        m = re.search(pattern, result, re.IGNORECASE)
        if m:
            candidate = m.group(1).strip()
            if _looks_like_path(candidate):
                return candidate

    return None


def _looks_like_path(s: str) -> bool:
    """Heuristic: does this string look like a file path?"""
    if not s:
        return False
    return any(c in s for c in "/\\") or s.startswith(("~", ".", "/"))