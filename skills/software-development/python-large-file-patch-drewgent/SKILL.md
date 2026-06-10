---
title: python-large-file-patch-drewgent
type: skill
space: growth
tags: [skill, software-development, patching, python]
created: 2026-06-01
updated: 2026-06-01
links:
  - "[[P0-brainstem/brain/rules]]"
---

# Skill: Patching Large Python Files in Drewgent

## When to Use
When you need to edit a specific function or code block inside a large Python file (500+ lines) in the Drewgent codebase (signal_processor.py, run_agent.py, gateway/run.py, etc.), and direct `mcp_patch` fails due to text pattern mismatch.

## The Problem
`mcp_patch` relies on exact string matching. Large files with complex docstrings (especially with newlines and special characters) often don't match what you expect from reading partial snippets. You get "old_string not found" even when the function exists.

## Solution: Python Patch Script with Diagnostic Output

When `mcp_patch` fails, use `mcp_execute_code` to:
1. First read the file and locate the exact text with `in` operator
2. Print diagnostics so you know exactly what to match
3. Apply with `content.replace(old, new, 1)` — the `1` limit prevents accidental multi-match
4. Write back and verify

```python
import sys
sys.path.insert(0, '/Users/drew/.drewgent/source/drewgent-agent')

with open('/path/to/file.py') as f:
    content = f.read()

# Step 1: find the exact text (use a generous but unique snippet)
old1 = '''    def _on_dangerous_op(self, event: BrainEvent) -> None:
        """Handle dangerous.op'''

if old1 not in content:
    # Try alternative — search for just the function name line
    import re
    match = re.search(r'def _on_dangerous_op.*?(?=\n    def )', content, re.DOTALL)
    if match:
        print("FOUND at char", match.start())
        print("Actual text:", repr(match.group()[:200]))
    else:
        print("ERROR: _on_dangerous_op pattern not found")
else:
    content = content.replace(old1, new1, 1)
    print("Patch OK")

with open('/path/to/file.py', 'w') as f:
    f.write(content)
```

## Key Rules

1. **Always use `1` in `replace(old, new, 1)`** — prevents replacing the same pattern multiple times accidentally
2. **Search before patching** — use `in` operator to confirm exact text exists before replacing
3. **Use `re.DOTALL` for multi-line matches** — docstrings and indented blocks span lines
4. **Print `repr()` of found text** — reveals hidden characters (`\n`, `\r`, trailing spaces) that break exact matching
5. **Match from unique anchor to unique anchor** — not just the function header alone; include enough context to be unique

## Drewgent File Paths (for reference)
- Brain signals: `/Users/drew/.drewgent/source/drewgent-agent/agent/signal_processor.py` (~1981 lines)
- Agent core: `/Users/drew/.drewgent/source/drewgent-agent/run_agent.py` (~15000+ lines)
- Gateway: `/Users/drew/.drewgent/source/drewgent-agent/gateway/run.py` (~8700 lines)
- Prompt builder: `/Users/drew/.drewgent/source/drewgent-agent/agent/prompt_builder.py`

## Anti-Patterns
- Don't use `cat` or `head` to read files before patching — use `mcp_read_file` with offset/limit
- Don't try to match just `"""docstring"""` alone — docstrings often have newlines and vary in wording
- Don't use `replace_all=True` unless you intentionally want to replace every occurrence