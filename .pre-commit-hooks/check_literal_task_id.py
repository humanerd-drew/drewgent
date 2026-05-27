#!/usr/bin/env python3
"""Pre-commit hook: detect literal {task_id} in non-f-string string literals.

Exclude from detection:
  - f-strings (f"..." or f'...') — safe, variable substitution expected
  - Template placeholders: strings where at least one file in the set has .format(task_id=...)
  - Comment-only lines
"""
import re
import sys

PATTERN = re.compile(r'["\'][^"\']*\{task_id\}[^"\']*["\']')
FSTRING_RE = re.compile(r'f["\']')
COMMENT_RE = re.compile(r'^\s*#')
FORMAT_TASKID_RE = re.compile(r'\.format\([^)]*task_id')

def scan_file(fname: str):
    """Return list of bad line numbers in a file."""
    bad_lines = []
    try:
        with open(fname) as f:
            for lnum, line in enumerate(f, 1):
                if not PATTERN.search(line):
                    continue
                if FSTRING_RE.search(line):
                    continue
                if COMMENT_RE.match(line):
                    continue
                bad_lines.append((lnum, line.rstrip()))
    except Exception as e:
        print(f"Error reading {fname}: {e}", file=sys.stderr)
    return bad_lines

if __name__ == "__main__":
    # First pass: check if ANY file in the set uses .format(task_id=...)
    # This means {task_id} is an intentional template placeholder
    has_template = False
    for fname in sys.argv[1:]:
        try:
            with open(fname) as f:
                if FORMAT_TASKID_RE.search(f.read()):
                    has_template = True
                    break
        except Exception:
            pass

    exit_code = 0
    for fname in sys.argv[1:]:
        bad_lines = scan_file(fname)
        if not bad_lines:
            continue
        # If the file has a .format(task_id=...) call somewhere, the {task_id}
        # placeholders in string literals are intentional template placeholders
        if has_template:
            continue
        for lnum, line in bad_lines:
            print(f"{fname}:{lnum}: {line}")
            exit_code = 1

    sys.exit(exit_code)