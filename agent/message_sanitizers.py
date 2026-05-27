"""Message sanitizers — surrogate and budget-warning cleanup for LLM message lists."""

import json
import re

# ── Budget warning pattern ──────────────────────────────────────────────────────

_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")

_BUDGET_WARNING_RE = re.compile(
    r"\[BUDGET(?:\s+WARNING)?:\s+Iteration\s+\d+/\d+\..*?\]",
    re.DOTALL,
)


def _sanitize_surrogates(text: str) -> str:
    """Replace lone surrogate code points with U+FFFD (replacement character).

    Surrogates are invalid in UTF-8 and will crash ``json.dumps()`` inside the
    OpenAI SDK.  This is a fast no-op when the text contains no surrogates.
    """
    if _SURROGATE_RE.search(text):
        return _SURROGATE_RE.sub("\ufffd", text)
    return text


def _sanitize_messages_surrogates(messages: list) -> bool:
    """Sanitize surrogate characters from all string content in a messages list.

    Walks message dicts in-place.  Returns True if any surrogates were found
    and replaced, False otherwise.
    """
    found = False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str) and _SURROGATE_RE.search(content):
            msg["content"] = _SURROGATE_RE.sub("\ufffd", content)
            found = True
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str) and _SURROGATE_RE.search(text):
                        part["text"] = _SURROGATE_RE.sub("\ufffd", text)
                        found = True
    return found


def _strip_budget_warnings_from_history(messages: list) -> None:
    """Remove budget pressure warnings from tool-result messages in-place.

    Budget warnings are turn-scoped signals that must not leak into replayed
    history.  They live in tool-result ``content`` either as a JSON key
    (``_budget_warning``) or appended plain text.
    """
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if (
            not isinstance(content, str)
            or "_budget_warning" not in content
            and "[BUDGET" not in content
        ):
            continue

        # Try JSON first (the common case: _budget_warning key in a dict)
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict) and "_budget_warning" in parsed:
                del parsed["_budget_warning"]
                msg["content"] = json.dumps(parsed, ensure_ascii=False)
                continue
        except (json.JSONDecodeError, TypeError):
            pass

        # Fallback: strip the text pattern from plain-text tool results
        cleaned = _BUDGET_WARNING_RE.sub("", content).strip()
        if cleaned != content:
            msg["content"] = cleaned