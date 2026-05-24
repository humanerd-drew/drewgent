"""Streaming API calls — extracted from run_agent.py (step 1 of 3)."""
#!/usr/bin/env python3
"""
AI Agent Runner with Tool Calling

This module provides a clean, standalone agent that can execute AI models
with tool calling capabilities. It handles the conversation loop, tool execution,
and response management.

Features:
- Automatic tool calling loop until completion
- Configurable model parameters
- Error handling and recovery
- Message history management
- Support for multiple model providers

Usage:
    from run_agent import AIAgent

    agent = AIAgent(base_url="http://localhost:30000/v1", model="claude-opus-4-20250514")
    response = agent.run_conversation("Tell me about the latest Python updates")
"""

import atexit
import asyncio
import base64
import concurrent.futures
import copy
import hashlib
import json
import logging

logger = logging.getLogger(__name__)
import os
import random
import re
import sys
import tempfile
import time
import threading
import weakref
from types import SimpleNamespace
import uuid
from typing import List, Dict, Any, Optional
from openai import OpenAI
import fire
from datetime import datetime
from pathlib import Path

from drewgent_constants import get_drewgent_home

# Load .env from ~/.drewgent/.env first, then project root as dev fallback.
# User-managed env files should override stale shell exports on restart.
from drewgent_cli.env_loader import load_drewgent_dotenv

_drewgent_home = get_drewgent_home()
_project_env = Path(__file__).parent / ".env"
_loaded_env_paths = load_drewgent_dotenv(
    drewgent_home=_drewgent_home, project_env=_project_env
)
if _loaded_env_paths:
    for _env_path in _loaded_env_paths:
        logger.info("Loaded environment variables from %s", _env_path)
else:
    logger.info("No .env file found. Using system environment variables.")


# Import our tool system
from model_tools import (
    get_tool_definitions,
    get_toolset_for_tool,
    handle_function_call,
    check_toolset_requirements,
)
from tools.terminal_tool import cleanup_vm
from tools.interrupt import set_interrupt as _set_interrupt
from tools.browser_tool import cleanup_browser


from drewgent_constants import OPENROUTER_BASE_URL

# Agent internals extracted to agent/ package for modularity
from agent.memory_manager import build_memory_context_block
from agent.auto_learn import AutoLearner
from agent.prompt_builder import (
    DEFAULT_AGENT_IDENTITY,
    PLATFORM_HINTS,
    MEMORY_GUIDANCE,
    SESSION_SEARCH_GUIDANCE,
    SKILLS_GUIDANCE,
    build_nous_subscription_prompt,
)
from agent.model_metadata import (
    fetch_model_metadata,
    estimate_tokens_rough,
    estimate_messages_tokens_rough,
    estimate_request_tokens_rough,
    get_next_probe_tier,
    parse_context_limit_from_error,
    save_context_length,
    is_local_endpoint,
)
from agent.context_compressor import ContextCompressor
from agent.subdirectory_hints import SubdirectoryHintTracker
from agent.prompt_caching import apply_anthropic_cache_control
from agent.prompt_builder import (
    build_skills_system_prompt,
    build_context_files_prompt,
    load_soul_md,
    TOOL_USE_ENFORCEMENT_GUIDANCE,
    TOOL_USE_ENFORCEMENT_MODELS,
    DEVELOPER_ROLE_MODELS,
    GOOGLE_MODEL_OPERATIONAL_GUIDANCE,
    OPENAI_MODEL_EXECUTION_GUIDANCE,
)
from agent.brain_signals import (
    emit_turn_start,
    emit_turn_end,
    emit_qa_gate,
    emit_agent_complete,
    get_signal_emitter,
)
from agent.brain_processor import get_brain_processor
from agent.signal_processor import get_signal_processor
from agent.awareness_reporter import get_awareness_reporter
from agent.usage_pricing import estimate_usage_cost, normalize_usage
from agent.display import (
    KawaiiSpinner,
    build_tool_preview as _build_tool_preview,
    get_cute_tool_message as _get_cute_tool_message_impl,
    _detect_tool_failure,
    get_tool_emoji as _get_tool_emoji,
)
from agent.trajectory import (
    convert_scratchpad_to_think,
    has_incomplete_scratchpad,
    save_trajectory as _save_trajectory_to_file,
)
from utils import atomic_json_write, env_var_enabled
from agent.prompt_builder import (
    _build_self_model_hint,
    _build_prefrontal_hint,
)
from agent.budget import IterationBudget
from agent.safe_io import _SafeWriter, _install_safe_stdio

from agent.parallel_tools import (
    _NEVER_PARALLEL_TOOLS,
    _PARALLEL_SAFE_TOOLS,
    _PATH_SCOPED_TOOLS,
    _MAX_TOOL_WORKERS,
    _DESTRUCTIVE_PATTERNS,
    _REDIRECT_OVERWRITE,
    _is_destructive_command,
    _should_parallelize_tool_batch,
    _extract_parallel_scope_path,
    _paths_overlap,
)


# ── Latent task detection (HP-3 QA gate) ─────────────────────────────────────
# Garry Tan Complexity Ratchet: tasks requiring model judgment/synthesis
# should go through QA contract-first flow before delivery.

_LATENT_KEYWORDS = (
    "implement", "build", "create", "design", "research",
    "analyze", "write code", "develop", "architect",
    "coding task", "refactor",
)


def _is_latent_task(user_message: str) -> bool:
    """Detect latent (judgment/synthesis) tasks that benefit from QA gates."""
    if not user_message:
        return False
    msg_lower = user_message.lower()
    return any(kw in msg_lower for kw in _LATENT_KEYWORDS)


def _qa_evidence_dir_for_task(task_id: str) -> str:
    """Return the QA evidence directory path for a given task_id."""
    import os

    return os.path.join(
        os.path.expanduser("~/.drewgent"),
        "P2-hippocampus", "qa-evidence", task_id,
    )


def _emit_qa_gate_for_task(task_id: str, phase: str) -> None:
    """Emit qa.gate for a task, creating evidence_dir if needed."""
    import os

    evidence_dir = _qa_evidence_dir_for_task(task_id)
    os.makedirs(evidence_dir, exist_ok=True)
    try:
        emit_qa_gate(task_id=task_id, phase=phase, evidence_dir=evidence_dir)
    except Exception:
        pass  # Brain signals are best-effort


# ── Brain signal: file-path extraction from tool results ──────────────────────
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
    re.compile(r"(?:sed|perl).*?\s+-i(?:_\w+)?\s+['\"]([^\'\"]+)['\"]"),
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


# ── Message sanitizers (extracted to agent/message_sanitizers.py) ──────────────

from agent.message_sanitizers import (
    _sanitize_surrogates,
    _sanitize_messages_surrogates,
    _strip_budget_warnings_from_history,
    _SURROGATE_RE,
)

# =========================================================================
# Large tool result handler — save oversized output to temp file
# =========================================================================

# Threshold at which tool results are saved to a file instead of kept inline.
# 100K chars ≈ 25K tokens — generous for any reasonable output but prevents
# catastrophic context explosions.
_LARGE_RESULT_CHARS = 100_000

# How many characters of the original result to include as an inline preview
# so the model has immediate context about what the tool returned.
_LARGE_RESULT_PREVIEW_CHARS = 1_500


def _save_oversized_tool_result(function_name: str, function_result: str) -> str:
    """Replace oversized tool results with a file reference + preview.

    When a tool returns more than ``_LARGE_RESULT_CHARS`` characters, the full
    content is written to a temporary file under ``DREW_HOME/cache/tool_responses/``
    and the result sent to the model is replaced with:
      • a brief head preview  (first ``_LARGE_RESULT_PREVIEW_CHARS`` chars)
      • the file path so the model can use ``read_file`` / ``search_files``

    Falls back to destructive truncation if the file write fails.
    """
    original_len = len(function_result)
    if original_len <= _LARGE_RESULT_CHARS:
        return function_result

    # Build the target directory
    try:
        response_dir = os.path.join(get_drewgent_home(), "cache", "tool_responses")
        os.makedirs(response_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        # Sanitize tool name for use in filename
        safe_name = re.sub(r"[^\w\-]", "_", function_name)[:40]
        filename = f"{safe_name}_{timestamp}.txt"
        filepath = os.path.join(response_dir, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(function_result)

        preview = function_result[:_LARGE_RESULT_PREVIEW_CHARS]
        return (
            f"{preview}\n\n"
            f"[Large tool response: {original_len:,} characters total — "
            f"only the first {_LARGE_RESULT_PREVIEW_CHARS:,} shown above. "
            f"Full output saved to: {filepath}\n"
            f"Use read_file or search_files on that path to access the rest.]"
        )
    except Exception as exc:
        # Fall back to destructive truncation if file write fails
        logger.warning("Failed to save large tool result to file: %s", exc)
        return (
            function_result[:_LARGE_RESULT_CHARS]
            + f"\n\n[Truncated: tool response was {original_len:,} chars, "
            f"exceeding the {_LARGE_RESULT_CHARS:,} char limit. "
            f"File save failed: {exc}]"
        )


class AIAgent:
    """
    AI Agent with tool calling capabilities.

    This class manages the conversation flow, tool execution, and response handling
    for AI models that support function calling.
    """



class StreamingHandler:
    """Streaming call implementation methods."""

    @staticmethod
    def _sanitize_api_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Fix orphaned tool_call / tool_result pairs before every LLM call.

        Runs unconditionally — not gated on whether the context compressor
        is present — so orphans from session loading or manual message
        manipulation are always caught.
        """
        # --- Role allowlist: drop messages with roles the API won't accept ---
        filtered = []
        for msg in messages:
            role = msg.get("role")
            if role not in AIAgent._VALID_API_ROLES:
                logger.debug(
                    "Pre-call sanitizer: dropping message with invalid role %r",
                    role,
                )
                continue
            filtered.append(msg)
        messages = filtered

        surviving_call_ids: set = set()
        for msg in messages:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    cid = AIAgent._get_tool_call_id_static(tc)
                    if cid:
                        surviving_call_ids.add(cid)

        result_call_ids: set = set()
        for msg in messages:
            if msg.get("role") == "tool":
                cid = msg.get("tool_call_id")
                if cid:
                    result_call_ids.add(cid)

        # 1. Drop tool results with no matching assistant call
        orphaned_results = result_call_ids - surviving_call_ids
        if orphaned_results:
            messages = [
                m
                for m in messages
                if not (
                    m.get("role") == "tool"
                    and m.get("tool_call_id") in orphaned_results
                )
            ]
            logger.debug(
                "Pre-call sanitizer: removed %d orphaned tool result(s)",
                len(orphaned_results),
            )

        # 2. Inject stub results for calls whose result was dropped
        missing_results = surviving_call_ids - result_call_ids
        if missing_results:
            patched: List[Dict[str, Any]] = []
            for msg in messages:
                patched.append(msg)
                if msg.get("role") == "assistant":
                    for tc in msg.get("tool_calls") or []:
                        cid = AIAgent._get_tool_call_id_static(tc)
                        if cid in missing_results:
                            patched.append(
                                {
                                    "role": "tool",
                                    "content": "[Result unavailable — see context summary above]",
                                    "tool_call_id": cid,
                                }
                            )
            messages = patched
            logger.debug(
                "Pre-call sanitizer: added %d stub tool result(s)",
                len(missing_results),
            )
        return messages

@staticmethod
def _cap_delegate_task_calls(tool_calls: list) -> list:
    """Truncate excess delegate_task calls to MAX_CONCURRENT_CHILDREN.

    The delegate_tool caps the task list inside a single call, but the
    model can emit multiple separate delegate_task tool_calls in one
    turn.  This truncates the excess, preserving all non-delegate calls.

    Returns the original list if no truncation was needed.
    """
    from tools.delegate_tool import MAX_CONCURRENT_CHILDREN

    delegate_count = sum(
        1 for tc in tool_calls if tc.function.name == "delegate_task"
    )
    if delegate_count <= MAX_CONCURRENT_CHILDREN:
        return tool_calls
    kept_delegates = 0
    truncated = []
    for tc in tool_calls:
        if tc.function.name == "delegate_task":
            if kept_delegates < MAX_CONCURRENT_CHILDREN:
                truncated.append(tc)
                kept_delegates += 1
        else:
            truncated.append(tc)
    logger.warning(
        "Truncated %d excess delegate_task call(s) to enforce "
        "MAX_CONCURRENT_CHILDREN=%d limit",
        delegate_count - MAX_CONCURRENT_CHILDREN,
        MAX_CONCURRENT_CHILDREN,
    )
    return truncated

@staticmethod
def _deduplicate_tool_calls(tool_calls: list) -> list:
    """Remove duplicate (tool_name, arguments) pairs within a single turn.

    Only the first occurrence of each unique pair is kept.
    Returns the original list if no duplicates were found.
    """
    seen: set = set()
    unique: list = []
    for tc in tool_calls:
        key = (tc.function.name, tc.function.arguments)
        if key not in seen:
            seen.add(key)
            unique.append(tc)
        else:
            logger.warning("Removed duplicate tool call: %s", tc.function.name)
    return unique if len(unique) < len(tool_calls) else tool_calls

def _repair_tool_call(self, tool_name: str) -> str | None:
    """Attempt to repair a mismatched tool name before aborting.

    1. Try lowercase
    2. Try normalized (lowercase + hyphens/spaces -> underscores)
    3. Try fuzzy match (difflib, cutoff=0.7)

    Returns the repaired name if found in valid_tool_names, else None.
    """
    from difflib import get_close_matches

    # 1. Lowercase
    lowered = tool_name.lower()
    if lowered in self.valid_tool_names:
        return lowered

    # 2. Normalize
    normalized = lowered.replace("-", "_").replace(" ", "_")
    if normalized in self.valid_tool_names:
        return normalized

    # 3. Fuzzy match
    matches = get_close_matches(lowered, self.valid_tool_names, n=1, cutoff=0.7)
    if matches:
        return matches[0]

    return None

def _ensure_tool_schemas_expanded(self, tool_calls) -> None:
    """Expand tool schemas for tools not yet in _active_tool_schemas.

    In manifest mode, we send only the lightweight manifest (name + one-line).
    When the model calls a tool, we load its full schema into _active_tool_schemas
    so the schema is available on subsequent turns.
    """
    if not self._tool_manifest_mode:
        return
    if not tool_calls:
        return

    from model_tools import get_full_schema_for_tools

    # Find tools that need schema loading
    to_load = []
    for tc in tool_calls:
        name = tc.function.name if hasattr(tc.function, "name") else str(tc.get("function", {}).get("name", ""))
        if name and name not in self._active_tool_schemas_names:
            to_load.append(name)

    if not to_load:
        return

    # Load full schemas for the new tools
    new_schemas = get_full_schema_for_tools(to_load)
    self._active_tool_schemas.extend(new_schemas)

    # Update valid_tool_names to include newly loaded tools
    for schema in new_schemas:
        fname = schema.get("function", {}).get("name", "")
        if fname:
            self.valid_tool_names.add(fname)

    if new_schemas:
        newly_needed = [n for n in to_load if n not in _CORE_MANIFEST_TOOLS]
        if newly_needed and not self.quiet_mode:
            print(f"  📋 Expanded tool schemas: {', '.join(newly_needed[:5])}")

@property
def _active_tool_schemas_names(self) -> set:
    """Return set of tool names currently in active schemas."""
    return {t.get("function", {}).get("name", "") for t in self._active_tool_schemas}

def _invalidate_system_prompt(self):
    """
    Invalidate the cached system prompt, forcing a rebuild on the next turn.

    Called after context compression events. Also reloads memory from disk
    so the rebuilt prompt captures any writes from this session.
    """
    self._cached_system_prompt = None
    if self._memory_store:
        self._memory_store.load_from_disk()

def _responses_tools(
    self, tools: Optional[List[Dict[str, Any]]] = None
) -> Optional[List[Dict[str, Any]]]:
    """Convert chat-completions tool schemas to Responses function-tool schemas."""
    source_tools = tools if tools is not None else self.tools
    if not source_tools:
        return None

    converted: List[Dict[str, Any]] = []
    for item in source_tools:
        fn = item.get("function", {}) if isinstance(item, dict) else {}
        name = fn.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        converted.append(
            {
                "type": "function",
                "name": name,
                "description": fn.get("description", ""),
                "strict": False,
                "parameters": fn.get(
                    "parameters", {"type": "object", "properties": {}}
                ),
            }
        )
    return converted or None

@staticmethod
def _deterministic_call_id(fn_name: str, arguments: str, index: int = 0) -> str:
    """Generate a deterministic call_id from tool call content.

    Used as a fallback when the API doesn't provide a call_id.
    Deterministic IDs prevent cache invalidation — random UUIDs would
    make every API call's prefix unique, breaking OpenAI's prompt cache.
    """
    import hashlib

    seed = f"{fn_name}:{arguments}:{index}"
    digest = hashlib.sha256(seed.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"call_{digest}"

@staticmethod
def _split_responses_tool_id(raw_id: Any) -> tuple[Optional[str], Optional[str]]:
    """Split a stored tool id into (call_id, response_item_id)."""
    if not isinstance(raw_id, str):
        return None, None
    value = raw_id.strip()
    if not value:
        return None, None
    if "|" in value:
        call_id, response_item_id = value.split("|", 1)
        call_id = call_id.strip() or None
        response_item_id = response_item_id.strip() or None
        return call_id, response_item_id
    if value.startswith("fc_"):
        return None, value
    return value, None

def _derive_responses_function_call_id(
    self,
    call_id: str,
    response_item_id: Optional[str] = None,
) -> str:
    """Build a valid Responses `function_call.id` (must start with `fc_`)."""
    if isinstance(response_item_id, str):
        candidate = response_item_id.strip()
        if candidate.startswith("fc_"):
            return candidate

    source = (call_id or "").strip()
    if source.startswith("fc_"):
        return source
    if source.startswith("call_") and len(source) > len("call_"):
        return f"fc_{source[len('call_') :]}"

    sanitized = re.sub(r"[^A-Za-z0-9_-]", "", source)
    if sanitized.startswith("fc_"):
        return sanitized
    if sanitized.startswith("call_") and len(sanitized) > len("call_"):
        return f"fc_{sanitized[len('call_') :]}"
    if sanitized:
        return f"fc_{sanitized[:48]}"

    seed = source or str(response_item_id or "") or uuid.uuid4().hex
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:24]
    return f"fc_{digest}"

def _chat_messages_to_responses_input(
    self, messages: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Convert internal chat-style messages to Responses input items."""
    items: List[Dict[str, Any]] = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "system":
            continue

        if role in {"user", "assistant"}:
            content = msg.get("content", "")
            content_text = str(content) if content is not None else ""

            if role == "assistant":
                # Replay encrypted reasoning items from previous turns
                # so the API can maintain coherent reasoning chains.
                codex_reasoning = msg.get("codex_reasoning_items")
                has_codex_reasoning = False
                if isinstance(codex_reasoning, list):
                    for ri in codex_reasoning:
                        if isinstance(ri, dict) and ri.get("encrypted_content"):
                            items.append(ri)
                            has_codex_reasoning = True

                if content_text.strip():
                    items.append({"role": "assistant", "content": content_text})
                elif has_codex_reasoning:
                    # The Responses API requires a following item after each
                    # reasoning item (otherwise: missing_following_item error).
                    # When the assistant produced only reasoning with no visible
                    # content, emit an empty assistant message as the required
                    # following item.
                    items.append({"role": "assistant", "content": ""})

                tool_calls = msg.get("tool_calls")
                if isinstance(tool_calls, list):
                    for tc in tool_calls:
                        if not isinstance(tc, dict):
                            continue
                        fn = tc.get("function", {})
                        fn_name = fn.get("name")
                        if not isinstance(fn_name, str) or not fn_name.strip():
                            continue

                        embedded_call_id, embedded_response_item_id = (
                            self._split_responses_tool_id(tc.get("id"))
                        )
                        call_id = tc.get("call_id")
                        if not isinstance(call_id, str) or not call_id.strip():
                            call_id = embedded_call_id
                        if not isinstance(call_id, str) or not call_id.strip():
                            if (
                                isinstance(embedded_response_item_id, str)
                                and embedded_response_item_id.startswith("fc_")
                                and len(embedded_response_item_id) > len("fc_")
                            ):
                                call_id = f"call_{embedded_response_item_id[len('fc_') :]}"
                            else:
                                _raw_args = str(fn.get("arguments", "{}"))
                                call_id = self._deterministic_call_id(
                                    fn_name, _raw_args, len(items)
                                )
                        call_id = call_id.strip()

                        arguments = fn.get("arguments", "{}")
                        if isinstance(arguments, dict):
                            arguments = json.dumps(arguments, ensure_ascii=False)
                        elif not isinstance(arguments, str):
                            arguments = str(arguments)
                        arguments = arguments.strip() or "{}"

                        items.append(
                            {
                                "type": "function_call",
                                "call_id": call_id,
                                "name": fn_name,
                                "arguments": arguments,
                            }
                        )
                continue

            items.append({"role": role, "content": content_text})
            continue

        if role == "tool":
            raw_tool_call_id = msg.get("tool_call_id")
            call_id, _ = self._split_responses_tool_id(raw_tool_call_id)
            if not isinstance(call_id, str) or not call_id.strip():
                if isinstance(raw_tool_call_id, str) and raw_tool_call_id.strip():
                    call_id = raw_tool_call_id.strip()
            if not isinstance(call_id, str) or not call_id.strip():
                continue
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": str(msg.get("content", "") or ""),
                }
            )

    return items

def _preflight_codex_input_items(self, raw_items: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_items, list):
        raise ValueError("Codex Responses input must be a list of input items.")

    normalized: List[Dict[str, Any]] = []
    for idx, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise ValueError(f"Codex Responses input[{idx}] must be an object.")

        item_type = item.get("type")
        if item_type == "function_call":
            call_id = item.get("call_id")
            name = item.get("name")
            if not isinstance(call_id, str) or not call_id.strip():
                raise ValueError(
                    f"Codex Responses input[{idx}] function_call is missing call_id."
                )
            if not isinstance(name, str) or not name.strip():
                raise ValueError(
                    f"Codex Responses input[{idx}] function_call is missing name."
                )

            arguments = item.get("arguments", "{}")
            if isinstance(arguments, dict):
                arguments = json.dumps(arguments, ensure_ascii=False)
            elif not isinstance(arguments, str):
                arguments = str(arguments)
            arguments = arguments.strip() or "{}"

            normalized.append(
                {
                    "type": "function_call",
                    "call_id": call_id.strip(),
                    "name": name.strip(),
                    "arguments": arguments,
                }
            )
            continue

        if item_type == "function_call_output":
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id.strip():
                raise ValueError(
                    f"Codex Responses input[{idx}] function_call_output is missing call_id."
                )
            output = item.get("output", "")
            if output is None:
                output = ""
            if not isinstance(output, str):
                output = str(output)

            normalized.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id.strip(),
                    "output": output,
                }
            )
            continue

        if item_type == "reasoning":
            encrypted = item.get("encrypted_content")
            if isinstance(encrypted, str) and encrypted:
                reasoning_item = {
                    "type": "reasoning",
                    "encrypted_content": encrypted,
                }
                item_id = item.get("id")
                if isinstance(item_id, str) and item_id:
                    reasoning_item["id"] = item_id
                summary = item.get("summary")
                if isinstance(summary, list):
                    reasoning_item["summary"] = summary
                else:
                    reasoning_item["summary"] = []
                normalized.append(reasoning_item)
            continue

        role = item.get("role")
        if role in {"user", "assistant"}:
            content = item.get("content", "")
            if content is None:
                content = ""
            if not isinstance(content, str):
                content = str(content)

            normalized.append({"role": role, "content": content})
            continue

        raise ValueError(
            f"Codex Responses input[{idx}] has unsupported item shape (type={item_type!r}, role={role!r})."
        )

    return normalized

def _preflight_codex_api_kwargs(
    self,
    api_kwargs: Any,
    *,
    allow_stream: bool = False,
) -> Dict[str, Any]:
    if not isinstance(api_kwargs, dict):
        raise ValueError("Codex Responses request must be a dict.")

    required = {"model", "instructions", "input"}
    missing = [key for key in required if key not in api_kwargs]
    if missing:
        raise ValueError(
            f"Codex Responses request missing required field(s): {', '.join(sorted(missing))}."
        )

    model = api_kwargs.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError(
            "Codex Responses request 'model' must be a non-empty string."
        )
    model = model.strip()

    instructions = api_kwargs.get("instructions")
    if instructions is None:
        instructions = ""
    if not isinstance(instructions, str):
        instructions = str(instructions)
    instructions = instructions.strip() or DEFAULT_AGENT_IDENTITY

    normalized_input = self._preflight_codex_input_items(api_kwargs.get("input"))

    tools = api_kwargs.get("tools")
    normalized_tools = None
    if tools is not None:
        if not isinstance(tools, list):
            raise ValueError(
                "Codex Responses request 'tools' must be a list when provided."
            )
        normalized_tools = []
        for idx, tool in enumerate(tools):
            if not isinstance(tool, dict):
                raise ValueError(f"Codex Responses tools[{idx}] must be an object.")
            if tool.get("type") != "function":
                raise ValueError(
                    f"Codex Responses tools[{idx}] has unsupported type {tool.get('type')!r}."
                )

            name = tool.get("name")
            parameters = tool.get("parameters")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(
                    f"Codex Responses tools[{idx}] is missing a valid name."
                )
            if not isinstance(parameters, dict):
                raise ValueError(
                    f"Codex Responses tools[{idx}] is missing valid parameters."
                )

            description = tool.get("description", "")
            if description is None:
                description = ""
            if not isinstance(description, str):
                description = str(description)

            strict = tool.get("strict", False)
            if not isinstance(strict, bool):
                strict = bool(strict)

            normalized_tools.append(
                {
                    "type": "function",
                    "name": name.strip(),
                    "description": description,
                    "strict": strict,
                    "parameters": parameters,
                }
            )

    store = api_kwargs.get("store", False)
    if store is not False:
        raise ValueError("Codex Responses contract requires 'store' to be false.")

    allowed_keys = {
        "model",
        "instructions",
        "input",
        "tools",
        "store",
        "reasoning",
        "include",
        "max_output_tokens",
        "temperature",
        "tool_choice",
        "parallel_tool_calls",
        "prompt_cache_key",
    }
    normalized: Dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": normalized_input,
        "store": False,
    }
    if normalized_tools is not None:
        normalized["tools"] = normalized_tools

    # Pass through reasoning config
    reasoning = api_kwargs.get("reasoning")
    if isinstance(reasoning, dict):
        normalized["reasoning"] = reasoning
    include = api_kwargs.get("include")
    if isinstance(include, list):
        normalized["include"] = include

    # Pass through max_output_tokens and temperature
    max_output_tokens = api_kwargs.get("max_output_tokens")
    if isinstance(max_output_tokens, (int, float)) and max_output_tokens > 0:
        normalized["max_output_tokens"] = int(max_output_tokens)
    temperature = api_kwargs.get("temperature")
    if isinstance(temperature, (int, float)):
        normalized["temperature"] = float(temperature)

    # Pass through tool_choice, parallel_tool_calls, prompt_cache_key
    for passthrough_key in (
        "tool_choice",
        "parallel_tool_calls",
        "prompt_cache_key",
    ):
        val = api_kwargs.get(passthrough_key)
        if val is not None:
            normalized[passthrough_key] = val

    if allow_stream:
        stream = api_kwargs.get("stream")
        if stream is not None and stream is not True:
            raise ValueError("Codex Responses 'stream' must be true when set.")
        if stream is True:
            normalized["stream"] = True
        allowed_keys.add("stream")
    elif "stream" in api_kwargs:
        raise ValueError(
            "Codex Responses stream flag is only allowed in fallback streaming requests."
        )

    unexpected = sorted(key for key in api_kwargs.keys() if key not in allowed_keys)
    if unexpected:
        raise ValueError(
            f"Codex Responses request has unsupported field(s): {', '.join(unexpected)}."
        )

    return normalized

def _extract_responses_message_text(self, item: Any) -> str:
    """Extract assistant text from a Responses message output item."""
    content = getattr(item, "content", None)
    if not isinstance(content, list):
        return ""

    chunks: List[str] = []
    for part in content:
        ptype = getattr(part, "type", None)
        if ptype not in {"output_text", "text"}:
            continue
        text = getattr(part, "text", None)
        if isinstance(text, str) and text:
            chunks.append(text)
    return "".join(chunks).strip()

def _extract_responses_reasoning_text(self, item: Any) -> str:
    """Extract a compact reasoning text from a Responses reasoning item."""
    summary = getattr(item, "summary", None)
    if isinstance(summary, list):
        chunks: List[str] = []
        for part in summary:
            text = getattr(part, "text", None)
            if isinstance(text, str) and text:
                chunks.append(text)
        if chunks:
            return "\n".join(chunks).strip()
    text = getattr(item, "text", None)
    if isinstance(text, str) and text:
        return text.strip()
    return ""

def _normalize_codex_response(self, response: Any) -> tuple[Any, str]:
    """Normalize a Responses API object to an assistant_message-like object."""
    output = getattr(response, "output", None)
    if not isinstance(output, list) or not output:
        # The Codex backend can return empty output when the answer was
        # delivered entirely via stream events. Check output_text as a
        # last-resort fallback before raising.
        out_text = getattr(response, "output_text", None)
        if isinstance(out_text, str) and out_text.strip():
            logger.debug(
                "Codex response has empty output but output_text is present (%d chars); "
                "synthesizing output item.",
                len(out_text.strip()),
            )
            output = [
                SimpleNamespace(
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[
                        SimpleNamespace(type="output_text", text=out_text.strip())
                    ],
                )
            ]
            response.output = output
        else:
            raise RuntimeError("Responses API returned no output items")

    response_status = getattr(response, "status", None)
    if isinstance(response_status, str):
        response_status = response_status.strip().lower()
    else:
        response_status = None

    if response_status in {"failed", "cancelled"}:
        error_obj = getattr(response, "error", None)
        if isinstance(error_obj, dict):
            error_msg = error_obj.get("message") or str(error_obj)
        else:
            error_msg = (
                str(error_obj)
                if error_obj
                else f"Responses API returned status '{response_status}'"
            )
        raise RuntimeError(error_msg)

    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    reasoning_items_raw: List[Dict[str, Any]] = []
    tool_calls: List[Any] = []
    has_incomplete_items = response_status in {
        "queued",
        "in_progress",
        "incomplete",
    }
    saw_commentary_phase = False
    saw_final_answer_phase = False

    for item in output:
        item_type = getattr(item, "type", None)
        item_status = getattr(item, "status", None)
        if isinstance(item_status, str):
            item_status = item_status.strip().lower()
        else:
            item_status = None

        if item_status in {"queued", "in_progress", "incomplete"}:
            has_incomplete_items = True

        if item_type == "message":
            item_phase = getattr(item, "phase", None)
            if isinstance(item_phase, str):
                normalized_phase = item_phase.strip().lower()
                if normalized_phase in {"commentary", "analysis"}:
                    saw_commentary_phase = True
                elif normalized_phase in {"final_answer", "final"}:
                    saw_final_answer_phase = True
            message_text = self._extract_responses_message_text(item)
            if message_text:
                content_parts.append(message_text)
        elif item_type == "reasoning":
            reasoning_text = self._extract_responses_reasoning_text(item)
            if reasoning_text:
                reasoning_parts.append(reasoning_text)
            # Capture the full reasoning item for multi-turn continuity.
            # encrypted_content is an opaque blob the API needs back on
            # subsequent turns to maintain coherent reasoning chains.
            encrypted = getattr(item, "encrypted_content", None)
            if isinstance(encrypted, str) and encrypted:
                raw_item = {"type": "reasoning", "encrypted_content": encrypted}
                item_id = getattr(item, "id", None)
                if isinstance(item_id, str) and item_id:
                    raw_item["id"] = item_id
                # Capture summary — required by the API when replaying reasoning items
                summary = getattr(item, "summary", None)
                if isinstance(summary, list):
                    raw_summary = []
                    for part in summary:
                        text = getattr(part, "text", None)
                        if isinstance(text, str):
                            raw_summary.append(
                                {"type": "summary_text", "text": text}
                            )
                    raw_item["summary"] = raw_summary
                reasoning_items_raw.append(raw_item)
        elif item_type == "function_call":
            if item_status in {"queued", "in_progress", "incomplete"}:
                continue
            fn_name = getattr(item, "name", "") or ""
            arguments = getattr(item, "arguments", "{}")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            raw_call_id = getattr(item, "call_id", None)
            raw_item_id = getattr(item, "id", None)
            embedded_call_id, _ = self._split_responses_tool_id(raw_item_id)
            call_id = (
                raw_call_id
                if isinstance(raw_call_id, str) and raw_call_id.strip()
                else embedded_call_id
            )
            if not isinstance(call_id, str) or not call_id.strip():
                call_id = self._deterministic_call_id(
                    fn_name, arguments, len(tool_calls)
                )
            call_id = call_id.strip()
            response_item_id = raw_item_id if isinstance(raw_item_id, str) else None
            response_item_id = self._derive_responses_function_call_id(
                call_id, response_item_id
            )
            tool_calls.append(
                SimpleNamespace(
                    id=call_id,
                    call_id=call_id,
                    response_item_id=response_item_id,
                    type="function",
                    function=SimpleNamespace(name=fn_name, arguments=arguments),
                )
            )
        elif item_type == "custom_tool_call":
            fn_name = getattr(item, "name", "") or ""
            arguments = getattr(item, "input", "{}")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            raw_call_id = getattr(item, "call_id", None)
            raw_item_id = getattr(item, "id", None)
            embedded_call_id, _ = self._split_responses_tool_id(raw_item_id)
            call_id = (
                raw_call_id
                if isinstance(raw_call_id, str) and raw_call_id.strip()
                else embedded_call_id
            )
            if not isinstance(call_id, str) or not call_id.strip():
                call_id = self._deterministic_call_id(
                    fn_name, arguments, len(tool_calls)
                )
            call_id = call_id.strip()
            response_item_id = raw_item_id if isinstance(raw_item_id, str) else None
            response_item_id = self._derive_responses_function_call_id(
                call_id, response_item_id
            )
            tool_calls.append(
                SimpleNamespace(
                    id=call_id,
                    call_id=call_id,
                    response_item_id=response_item_id,
                    type="function",
                    function=SimpleNamespace(name=fn_name, arguments=arguments),
                )
            )

    final_text = "\n".join([p for p in content_parts if p]).strip()
    if not final_text and hasattr(response, "output_text"):
        out_text = getattr(response, "output_text", "")
        if isinstance(out_text, str):
            final_text = out_text.strip()

    assistant_message = SimpleNamespace(
        content=final_text,
        tool_calls=tool_calls,
        reasoning="\n\n".join(reasoning_parts).strip() if reasoning_parts else None,
        reasoning_content=None,
        reasoning_details=None,
        codex_reasoning_items=reasoning_items_raw or None,
    )

    if tool_calls:
        finish_reason = "tool_calls"
    elif has_incomplete_items or (
        saw_commentary_phase and not saw_final_answer_phase
    ):
        finish_reason = "incomplete"
    elif reasoning_items_raw and not final_text:
        # Response contains only reasoning (encrypted thinking state) with
        # no visible content or tool calls.  The model is still thinking and
        # needs another turn to produce the actual answer.  Marking this as
        # "stop" would send it into the empty-content retry loop which burns
        # 3 retries then fails — treat it as incomplete instead so the Codex
        # continuation path handles it correctly.
        finish_reason = "incomplete"
    else:
        finish_reason = "stop"
    return assistant_message, finish_reason

def _thread_identity(self) -> str:
    thread = threading.current_thread()
    return f"{thread.name}:{thread.ident}"

def _client_log_context(self) -> str:
    provider = getattr(self, "provider", "unknown")
    base_url = getattr(self, "base_url", "unknown")
    model = getattr(self, "model", "unknown")
    return (
        f"thread={self._thread_identity()} provider={provider} "
        f"base_url={base_url} model={model}"
    )

def _openai_client_lock(self) -> threading.RLock:
    lock = getattr(self, "_client_lock", None)
    if lock is None:
        lock = threading.RLock()
        self._client_lock = lock
    return lock

@staticmethod
def _is_openai_client_closed(client: Any) -> bool:
    """Check if an OpenAI client is closed.

    Handles both property and method forms of is_closed:
    - httpx.Client.is_closed is a bool property
    - openai.OpenAI.is_closed is a method returning bool

    Prior bug: getattr(client, "is_closed", False) returned the bound method,
    which is always truthy, causing unnecessary client recreation on every call.
    """
    from unittest.mock import Mock

    if isinstance(client, Mock):
        return False

    is_closed_attr = getattr(client, "is_closed", None)
    if is_closed_attr is not None:
        # Handle method (openai SDK) vs property (httpx)
        if callable(is_closed_attr):
            if is_closed_attr():
                return True
        elif bool(is_closed_attr):
            return True

    http_client = getattr(client, "_client", None)
    if http_client is not None:
        return bool(getattr(http_client, "is_closed", False))
    return False

def _create_openai_client(
    self, client_kwargs: dict, *, reason: str, shared: bool
) -> Any:
    if self.provider == "copilot-acp" or str(
        client_kwargs.get("base_url", "")
    ).startswith("acp://copilot"):
        from agent.copilot_acp_client import CopilotACPClient

        client = CopilotACPClient(**client_kwargs)
        logger.info(
            "Copilot ACP client created (%s, shared=%s) %s",
            reason,
            shared,
            self._client_log_context(),
        )
        return client
    client = OpenAI(**client_kwargs)
    logger.info(
        "OpenAI client created (%s, shared=%s) %s",
        reason,
        shared,
        self._client_log_context(),
    )
    return client

@staticmethod
def _force_close_tcp_sockets(client: Any) -> int:
    """Force-close underlying TCP sockets to prevent CLOSE-WAIT accumulation.

    When a provider drops a connection mid-stream, httpx's ``client.close()``
    performs a graceful shutdown which leaves sockets in CLOSE-WAIT until the
    OS times them out (often minutes).  This method walks the httpx transport
    pool and issues ``socket.shutdown(SHUT_RDWR)`` + ``socket.close()`` to
    force an immediate TCP RST, freeing the file descriptors.

    Returns the number of sockets force-closed.
    """
    import socket as _socket

    closed = 0
    try:
        http_client = getattr(client, "_client", None)
        if http_client is None:
            return 0
        transport = getattr(http_client, "_transport", None)
        if transport is None:
            return 0
        pool = getattr(transport, "_pool", None)
        if pool is None:
            return 0
        # httpx uses httpcore connection pools; connections live in
        # _connections (list) or _pool (list) depending on version.
        connections = (
            getattr(pool, "_connections", None)
            or getattr(pool, "_pool", None)
            or []
        )
        for conn in list(connections):
            stream = getattr(conn, "_network_stream", None) or getattr(
                conn, "_stream", None
            )
            if stream is None:
                continue
            sock = getattr(stream, "_sock", None)
            if sock is None:
                sock = getattr(stream, "stream", None)
                if sock is not None:
                    sock = getattr(sock, "_sock", None)
            if sock is None:
                continue
            try:
                sock.shutdown(_socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
            closed += 1
    except Exception as exc:
        logger.debug("Force-close TCP sockets sweep error: %s", exc)
    return closed

def _close_openai_client(self, client: Any, *, reason: str, shared: bool) -> None:
    if client is None:
        return
    # Force-close TCP sockets first to prevent CLOSE-WAIT accumulation,
    # then do the graceful SDK-level close.
    force_closed = self._force_close_tcp_sockets(client)
    try:
        client.close()
        logger.info(
            "OpenAI client closed (%s, shared=%s, tcp_force_closed=%d) %s",
            reason,
            shared,
            force_closed,
            self._client_log_context(),
        )
    except Exception as exc:
        logger.debug(
            "OpenAI client close failed (%s, shared=%s) %s error=%s",
            reason,
            shared,
            self._client_log_context(),
            exc,
        )

def _replace_primary_openai_client(self, *, reason: str) -> bool:
    with self._openai_client_lock():
        old_client = getattr(self, "client", None)
        try:
            new_client = self._create_openai_client(
                self._client_kwargs, reason=reason, shared=True
            )
        except Exception as exc:
            logger.warning(
                "Failed to rebuild shared OpenAI client (%s) %s error=%s",
                reason,
                self._client_log_context(),
                exc,
            )
            return False
        self.client = new_client
    self._close_openai_client(old_client, reason=f"replace:{reason}", shared=True)
    return True

def _ensure_primary_openai_client(self, *, reason: str) -> Any:
    with self._openai_client_lock():
        client = getattr(self, "client", None)
        if client is not None and not self._is_openai_client_closed(client):
            return client

    logger.warning(
        "Detected closed shared OpenAI client; recreating before use (%s) %s",
        reason,
        self._client_log_context(),
    )
    if not self._replace_primary_openai_client(reason=f"recreate_closed:{reason}"):
        raise RuntimeError("Failed to recreate closed OpenAI client")
    with self._openai_client_lock():
        return self.client

def _cleanup_dead_connections(self) -> bool:
    """Detect and clean up dead TCP connections on the primary client.

    Inspects the httpx connection pool for sockets in unhealthy states
    (CLOSE-WAIT, errors).  If any are found, force-closes all sockets
    and rebuilds the primary client from scratch.

    Returns True if dead connections were found and cleaned up.
    """
    client = getattr(self, "client", None)
    if client is None:
        return False
    try:
        http_client = getattr(client, "_client", None)
        if http_client is None:
            return False
        transport = getattr(http_client, "_transport", None)
        if transport is None:
            return False
        pool = getattr(transport, "_pool", None)
        if pool is None:
            return False
        connections = (
            getattr(pool, "_connections", None)
            or getattr(pool, "_pool", None)
            or []
        )
        dead_count = 0
        for conn in list(connections):
            # Check for connections that are idle but have closed sockets
            stream = getattr(conn, "_network_stream", None) or getattr(
                conn, "_stream", None
            )
            if stream is None:
                continue
            sock = getattr(stream, "_sock", None)
            if sock is None:
                sock = getattr(stream, "stream", None)
                if sock is not None:
                    sock = getattr(sock, "_sock", None)
            if sock is None:
                continue
            # Probe socket health with a non-blocking recv peek
            import socket as _socket

            try:
                sock.setblocking(False)
                data = sock.recv(1, _socket.MSG_PEEK | _socket.MSG_DONTWAIT)
                if data == b"":
                    dead_count += 1
            except BlockingIOError:
                pass  # No data available — socket is healthy
            except OSError:
                dead_count += 1
            finally:
                try:
                    sock.setblocking(True)
                except OSError:
                    pass
        if dead_count > 0:
            logger.warning(
                "Found %d dead connection(s) in client pool — rebuilding client",
                dead_count,
            )
            self._replace_primary_openai_client(reason="dead_connection_cleanup")
            return True
    except Exception as exc:
        logger.debug("Dead connection check error: %s", exc)
    return False

def _create_request_openai_client(self, *, reason: str) -> Any:
    from unittest.mock import Mock

    primary_client = self._ensure_primary_openai_client(reason=reason)
    if isinstance(primary_client, Mock):
        return primary_client
    with self._openai_client_lock():
        request_kwargs = dict(self._client_kwargs)
    return self._create_openai_client(request_kwargs, reason=reason, shared=False)

def _close_request_openai_client(self, client: Any, *, reason: str) -> None:
    self._close_openai_client(client, reason=reason, shared=False)

def _run_codex_stream(
    self, api_kwargs: dict, client: Any = None, on_first_delta: callable = None
):
    """Execute one streaming Responses API request and return the final response."""
    import httpx as _httpx

    active_client = client or self._ensure_primary_openai_client(
        reason="codex_stream_direct"
    )
    max_stream_retries = 1
    has_tool_calls = False
    first_delta_fired = False
    self._reasoning_deltas_fired = False
    # Accumulate streamed text so we can recover if get_final_response()
    # returns empty output (e.g. chatgpt.com backend-api sends
    # response.incomplete instead of response.completed).
    self._codex_streamed_text_parts: list = []
    for attempt in range(max_stream_retries + 1):
        collected_output_items: list = []
        try:
            with active_client.responses.stream(**api_kwargs) as stream:
                for event in stream:
                    if self._interrupt_requested:
                        break
                    event_type = getattr(event, "type", "")
                    # Fire callbacks on text content deltas (suppress during tool calls)
                    if (
                        "output_text.delta" in event_type
                        or event_type == "response.output_text.delta"
                    ):
                        delta_text = getattr(event, "delta", "")
                        if delta_text:
                            self._codex_streamed_text_parts.append(delta_text)
                        if delta_text and not has_tool_calls:
                            if not first_delta_fired:
                                first_delta_fired = True
                                if on_first_delta:
                                    try:
                                        on_first_delta()
                                    except Exception:
                                        pass
                            self._fire_stream_delta(delta_text)
                    # Track tool calls to suppress text streaming
                    elif "function_call" in event_type:
                        has_tool_calls = True
                    # Fire reasoning callbacks
                    elif "reasoning" in event_type and "delta" in event_type:
                        reasoning_text = getattr(event, "delta", "")
                        if reasoning_text:
                            self._fire_reasoning_delta(reasoning_text)
                    # Collect completed output items — some backends
                    # (chatgpt.com/backend-api/codex) stream valid items
                    # via response.output_item.done but the SDK's
                    # get_final_response() returns an empty output list.
                    elif event_type == "response.output_item.done":
                        done_item = getattr(event, "item", None)
                        if done_item is not None:
                            collected_output_items.append(done_item)
                    # Log non-completed terminal events for diagnostics
                    elif event_type in ("response.incomplete", "response.failed"):
                        resp_obj = getattr(event, "response", None)
                        status = (
                            getattr(resp_obj, "status", None) if resp_obj else None
                        )
                        incomplete_details = (
                            getattr(resp_obj, "incomplete_details", None)
                            if resp_obj
                            else None
                        )
                        logger.warning(
                            "Codex Responses stream received terminal event %s "
                            "(status=%s, incomplete_details=%s, streamed_chars=%d). %s",
                            event_type,
                            status,
                            incomplete_details,
                            sum(len(p) for p in self._codex_streamed_text_parts),
                            self._client_log_context(),
                        )
                final_response = stream.get_final_response()
                # PATCH: ChatGPT Codex backend streams valid output items
                # but get_final_response() can return an empty output list.
                # Backfill from collected items or synthesize from deltas.
                _out = getattr(final_response, "output", None)
                if isinstance(_out, list) and not _out:
                    if collected_output_items:
                        final_response.output = list(collected_output_items)
                        logger.debug(
                            "Codex stream: backfilled %d output items from stream events",
                            len(collected_output_items),
                        )
                    elif self._codex_streamed_text_parts and not has_tool_calls:
                        assembled = "".join(self._codex_streamed_text_parts)
                        final_response.output = [
                            SimpleNamespace(
                                type="message",
                                role="assistant",
                                status="completed",
                                content=[
                                    SimpleNamespace(
                                        type="output_text", text=assembled
                                    )
                                ],
                            )
                        ]
                        logger.debug(
                            "Codex stream: synthesized output from %d text deltas (%d chars)",
                            len(self._codex_streamed_text_parts),
                            len(assembled),
                        )
                return final_response
        except (
            _httpx.RemoteProtocolError,
            _httpx.ReadTimeout,
            _httpx.ConnectError,
            ConnectionError,
        ) as exc:
            if attempt < max_stream_retries:
                logger.debug(
                    "Codex Responses stream transport failed (attempt %s/%s); retrying. %s error=%s",
                    attempt + 1,
                    max_stream_retries + 1,
                    self._client_log_context(),
                    exc,
                )
                continue
            logger.debug(
                "Codex Responses stream transport failed; falling back to create(stream=True). %s error=%s",
                self._client_log_context(),
                exc,
            )
            return self._run_codex_create_stream_fallback(
                api_kwargs, client=active_client
            )
        except RuntimeError as exc:
            err_text = str(exc)
            missing_completed = "response.completed" in err_text
            if missing_completed and attempt < max_stream_retries:
                logger.debug(
                    "Responses stream closed before completion (attempt %s/%s); retrying. %s",
                    attempt + 1,
                    max_stream_retries + 1,
                    self._client_log_context(),
                )
                continue
            if missing_completed:
                logger.debug(
                    "Responses stream did not emit response.completed; falling back to create(stream=True). %s",
                    self._client_log_context(),
                )
                return self._run_codex_create_stream_fallback(
                    api_kwargs, client=active_client
                )
            raise

def _run_codex_create_stream_fallback(self, api_kwargs: dict, client: Any = None):
    """Fallback path for stream completion edge cases on Codex-style Responses backends."""
    active_client = client or self._ensure_primary_openai_client(
        reason="codex_create_stream_fallback"
    )
    fallback_kwargs = dict(api_kwargs)
    fallback_kwargs["stream"] = True
    fallback_kwargs = self._preflight_codex_api_kwargs(
        fallback_kwargs, allow_stream=True
    )
    stream_or_response = active_client.responses.create(**fallback_kwargs)

    # Compatibility shim for mocks or providers that still return a concrete response.
    if hasattr(stream_or_response, "output"):
        return stream_or_response
    if not hasattr(stream_or_response, "__iter__"):
        return stream_or_response

    terminal_response = None
    collected_output_items: list = []
    collected_text_deltas: list = []
    try:
        for event in stream_or_response:
            event_type = getattr(event, "type", None)
            if not event_type and isinstance(event, dict):
                event_type = event.get("type")

            # Collect output items and text deltas for backfill
            if event_type == "response.output_item.done":
                done_item = getattr(event, "item", None)
                if done_item is None and isinstance(event, dict):
                    done_item = event.get("item")
                if done_item is not None:
                    collected_output_items.append(done_item)
            elif event_type in ("response.output_text.delta",):
                delta = getattr(event, "delta", "")
                if not delta and isinstance(event, dict):
                    delta = event.get("delta", "")
                if delta:
                    collected_text_deltas.append(delta)

            if event_type not in {
                "response.completed",
                "response.incomplete",
                "response.failed",
            }:
                continue

            terminal_response = getattr(event, "response", None)
            if terminal_response is None and isinstance(event, dict):
                terminal_response = event.get("response")
            if terminal_response is not None:
                # Backfill empty output from collected stream events
                _out = getattr(terminal_response, "output", None)
                if isinstance(_out, list) and not _out:
                    if collected_output_items:
                        terminal_response.output = list(collected_output_items)
                        logger.debug(
                            "Codex fallback stream: backfilled %d output items",
                            len(collected_output_items),
                        )
                    elif collected_text_deltas:
                        assembled = "".join(collected_text_deltas)
                        terminal_response.output = [
                            SimpleNamespace(
                                type="message",
                                role="assistant",
                                status="completed",
                                content=[
                                    SimpleNamespace(
                                        type="output_text", text=assembled
                                    )
                                ],
                            )
                        ]
                        logger.debug(
                            "Codex fallback stream: synthesized from %d deltas (%d chars)",
                            len(collected_text_deltas),
                            len(assembled),
                        )
                return terminal_response
    finally:
        close_fn = getattr(stream_or_response, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass

    if terminal_response is not None:
        return terminal_response
    raise RuntimeError(
        "Responses create(stream=True) fallback did not emit a terminal response."
    )

def _try_refresh_codex_client_credentials(self, *, force: bool = True) -> bool:
    if self.api_mode != "codex_responses" or self.provider != "openai-codex":
        return False

    try:
        from drewgent_cli.auth import resolve_codex_runtime_credentials

        creds = resolve_codex_runtime_credentials(force_refresh=force)
    except Exception as exc:
        logger.debug("Codex credential refresh failed: %s", exc)
        return False

    api_key = creds.get("api_key")
    base_url = creds.get("base_url")
    if not isinstance(api_key, str) or not api_key.strip():
        return False
    if not isinstance(base_url, str) or not base_url.strip():
        return False

    self.api_key = api_key.strip()
    self.base_url = base_url.strip().rstrip("/")
    self._client_kwargs["api_key"] = self.api_key
    self._client_kwargs["base_url"] = self.base_url

    if not self._replace_primary_openai_client(reason="codex_credential_refresh"):
        return False

    return True

def _try_refresh_nous_client_credentials(self, *, force: bool = True) -> bool:
    if self.api_mode != "chat_completions" or self.provider != "nous":
        return False

    try:
        from drewgent_cli.auth import resolve_nous_runtime_credentials

        creds = resolve_nous_runtime_credentials(
            min_key_ttl_seconds=max(
                60, int(os.getenv("HERMES_NOUS_MIN_KEY_TTL_SECONDS", "1800"))
            ),
            timeout_seconds=float(os.getenv("HERMES_NOUS_TIMEOUT_SECONDS", "15")),
            force_mint=force,
        )
    except Exception as exc:
        logger.debug("Nous credential refresh failed: %s", exc)
        return False

    api_key = creds.get("api_key")
    base_url = creds.get("base_url")
    if not isinstance(api_key, str) or not api_key.strip():
        return False
    if not isinstance(base_url, str) or not base_url.strip():
        return False

    self.api_key = api_key.strip()
    self.base_url = base_url.strip().rstrip("/")
    self._client_kwargs["api_key"] = self.api_key
    self._client_kwargs["base_url"] = self.base_url
    # Nous requests should not inherit OpenRouter-only attribution headers.
    self._client_kwargs.pop("default_headers", None)

    if not self._replace_primary_openai_client(reason="nous_credential_refresh"):
        return False

    return True

def _try_refresh_anthropic_client_credentials(self) -> bool:
    if self.api_mode != "anthropic_messages" or not hasattr(
        self, "_anthropic_api_key"
    ):
        return False
    # Only refresh credentials for the native Anthropic provider.
    # Other anthropic_messages providers (MiniMax, Alibaba, etc.) use their own keys.
    if self.provider != "anthropic":
        return False

    try:
        from agent.anthropic_adapter import (
            resolve_anthropic_token,
            build_anthropic_client,
        )

        new_token = resolve_anthropic_token()
    except Exception as exc:
        logger.debug("Anthropic credential refresh failed: %s", exc)
        return False

    if not isinstance(new_token, str) or not new_token.strip():
        return False
    new_token = new_token.strip()
    if new_token == self._anthropic_api_key:
        return False

    try:
        self._anthropic_client.close()
    except Exception:
        pass

    try:
        self._anthropic_client = build_anthropic_client(
            new_token, getattr(self, "_anthropic_base_url", None)
        )
    except Exception as exc:
        logger.warning(
            "Failed to rebuild Anthropic client after credential refresh: %s", exc
        )
        return False

    self._anthropic_api_key = new_token
    # Update OAuth flag — token type may have changed (API key ↔ OAuth)
    from agent.anthropic_adapter import _is_oauth_token

    self._is_anthropic_oauth = _is_oauth_token(new_token)
    return True

def _apply_client_headers_for_base_url(self, base_url: str) -> None:
    from agent.auxiliary_client import _OR_HEADERS

    normalized = (base_url or "").lower()
    if "openrouter" in normalized:
        self._client_kwargs["default_headers"] = dict(_OR_HEADERS)
    elif "api.githubcopilot.com" in normalized:
        from drewgent_cli.models import copilot_default_headers

        self._client_kwargs["default_headers"] = copilot_default_headers()
    elif "api.kimi.com" in normalized:
        self._client_kwargs["default_headers"] = {"User-Agent": "KimiCLI/1.3"}
    else:
        self._client_kwargs.pop("default_headers", None)

def _swap_credential(self, entry) -> None:
    runtime_key = getattr(entry, "runtime_api_key", None) or getattr(
        entry, "access_token", ""
    )
    runtime_base = (
        getattr(entry, "runtime_base_url", None)
        or getattr(entry, "base_url", None)
        or self.base_url
    )

    if self.api_mode == "anthropic_messages":
        from agent.anthropic_adapter import build_anthropic_client, _is_oauth_token

        try:
            self._anthropic_client.close()
        except Exception:
            pass

        self._anthropic_api_key = runtime_key
        self._anthropic_base_url = runtime_base
        self._anthropic_client = build_anthropic_client(runtime_key, runtime_base)
        self._is_anthropic_oauth = (
            _is_oauth_token(runtime_key) if self.provider == "anthropic" else False
        )
        self.api_key = runtime_key
        self.base_url = runtime_base
        return

    self.api_key = runtime_key
    self.base_url = (
        runtime_base.rstrip("/") if isinstance(runtime_base, str) else runtime_base
    )
    self._client_kwargs["api_key"] = self.api_key
    self._client_kwargs["base_url"] = self.base_url
    self._apply_client_headers_for_base_url(self.base_url)
    self._replace_primary_openai_client(reason="credential_rotation")

def _recover_with_credential_pool(
    self,
    *,
    status_code: Optional[int],
    has_retried_429: bool,
    error_context: Optional[Dict[str, Any]] = None,
) -> tuple[bool, bool]:
    """Attempt credential recovery via pool rotation.

    Returns (recovered, has_retried_429).
    On 429: first occurrence retries same credential (sets flag True).
            second consecutive 429 rotates to next credential (resets flag).
    On 402: immediately rotates (billing exhaustion won't resolve with retry).
    On 401: attempts token refresh before rotating.
    """
    pool = self._credential_pool
    if pool is None or status_code is None:
        return False, has_retried_429

    if status_code == 402:
        next_entry = pool.mark_exhausted_and_rotate(
            status_code=402, error_context=error_context
        )
        if next_entry is not None:
            logger.info(
                f"Credential 402 (billing) — rotated to pool entry {getattr(next_entry, 'id', '?')}"
            )
            self._swap_credential(next_entry)
            return True, False
        return False, has_retried_429

    if status_code == 429:
        if not has_retried_429:
            return False, True
        next_entry = pool.mark_exhausted_and_rotate(
            status_code=429, error_context=error_context
        )
        if next_entry is not None:
            logger.info(
                f"Credential 429 (rate limit) — rotated to pool entry {getattr(next_entry, 'id', '?')}"
            )
            self._swap_credential(next_entry)
            return True, False
        return False, True

    if status_code == 401:
        refreshed = pool.try_refresh_current()
        if refreshed is not None:
            logger.info(
                f"Credential 401 — refreshed pool entry {getattr(refreshed, 'id', '?')}"
            )
            self._swap_credential(refreshed)
            return True, has_retried_429
        # Refresh failed — rotate to next credential instead of giving up.
        # The failed entry is already marked exhausted by try_refresh_current().
        next_entry = pool.mark_exhausted_and_rotate(
            status_code=401, error_context=error_context
        )
        if next_entry is not None:
            logger.info(
                f"Credential 401 (refresh failed) — rotated to pool entry {getattr(next_entry, 'id', '?')}"
            )
            self._swap_credential(next_entry)
            return True, False

    return False, has_retried_429

def _anthropic_messages_create(self, api_kwargs: dict):
    if self.api_mode == "anthropic_messages":
        self._try_refresh_anthropic_client_credentials()
    return self._anthropic_client.messages.create(**api_kwargs)

def _interruptible_api_call(self, api_kwargs: dict):
    """
    Run the API call in a background thread so the main conversation loop
    can detect interrupts without waiting for the full HTTP round-trip.

    Each worker thread gets its own OpenAI client instance. Interrupts only
    close that worker-local client, so retries and other requests never
    inherit a closed transport.
    """
    result = {"response": None, "error": None}
    request_client_holder = {"client": None}

    def _call():
        try:
            if self.api_mode == "codex_responses":
                request_client_holder["client"] = (
                    self._create_request_openai_client(
                        reason="codex_stream_request"
                    )
                )
                result["response"] = self._run_codex_stream(
                    api_kwargs,
                    client=request_client_holder["client"],
                    on_first_delta=getattr(self, "_codex_on_first_delta", None),
                )
            elif self.api_mode == "anthropic_messages":
                result["response"] = self._anthropic_messages_create(api_kwargs)
            else:
                request_client_holder["client"] = (
                    self._create_request_openai_client(
                        reason="chat_completion_request"
                    )
                )
                result["response"] = request_client_holder[
                    "client"
                ].chat.completions.create(**api_kwargs)
        except Exception as e:
            result["error"] = e
        finally:
            request_client = request_client_holder.get("client")
            if request_client is not None:
                self._close_request_openai_client(
                    request_client, reason="request_complete"
                )

    t = threading.Thread(target=_call, daemon=True)
    t.start()
    while t.is_alive():
        t.join(timeout=0.3)
        if self._interrupt_requested:
            # Force-close the in-flight worker-local HTTP connection to stop
            # token generation without poisoning the shared client used to
            # seed future retries.
            try:
                if self.api_mode == "anthropic_messages":
                    from agent.anthropic_adapter import build_anthropic_client

                    self._anthropic_client.close()
                    self._anthropic_client = build_anthropic_client(
                        self._anthropic_api_key,
                        getattr(self, "_anthropic_base_url", None),
                    )
                else:
                    request_client = request_client_holder.get("client")
                    if request_client is not None:
                        self._close_request_openai_client(
                            request_client, reason="interrupt_abort"
                        )
            except Exception:
                pass
            raise InterruptedError("Agent interrupted during API call")
    if result["error"] is not None:
        raise result["error"]
    return result["response"]

# ── Unified streaming API call ─────────────────────────────────────────

def _fire_stream_delta(self, text: str) -> None:
    """Fire all registered stream delta callbacks (display + TTS)."""
    # If a tool iteration set the break flag, prepend a single paragraph
    # break before the first real text delta.  This prevents the original
    # problem (text concatenation across tool boundaries) without stacking
    # blank lines when multiple tool iterations run back-to-back.
    if getattr(self, "_stream_needs_break", False) and text and text.strip():
        self._stream_needs_break = False
        text = "\n\n" + text
    for cb in (self.stream_delta_callback, self._stream_callback):
        if cb is not None:
            try:
                cb(text)
            except Exception:
                pass

def _fire_reasoning_delta(self, text: str) -> None:
    """Fire reasoning callback if registered."""
    self._reasoning_deltas_fired = True
    cb = self.reasoning_callback
    if cb is not None:
        try:
            cb(text)
        except Exception:
            pass

def _fire_tool_gen_started(self, tool_name: str) -> None:
    """Notify display layer that the model is generating tool call arguments.

    Fires once per tool name when the streaming response begins producing
    tool_call / tool_use tokens.  Gives the TUI a chance to show a spinner
    or status line so the user isn't staring at a frozen screen while a
    large tool payload (e.g. a 45 KB write_file) is being generated.
    """
    cb = self.tool_gen_callback
    if cb is not None:
        try:
            cb(tool_name)
        except Exception:
            pass

def _has_stream_consumers(self) -> bool:
    """Return True if any streaming consumer is registered."""
    return (
        self.stream_delta_callback is not None
        or getattr(self, "_stream_callback", None) is not None
    )

def _interruptible_streaming_api_call(
    self, api_kwargs: dict, *, on_first_delta: callable = None
):
    """Streaming variant of _interruptible_api_call for real-time token delivery.

    Handles all three api_modes:
    - chat_completions: stream=True on OpenAI-compatible endpoints
    - anthropic_messages: client.messages.stream() via Anthropic SDK
    - codex_responses: delegates to _run_codex_stream (already streaming)

    Fires stream_delta_callback and _stream_callback for each text token.
    Tool-call turns suppress the callback — only text-only final responses
    stream to the consumer.  Returns a SimpleNamespace that mimics the
    non-streaming response shape so the rest of the agent loop is unchanged.

    Falls back to _interruptible_api_call on provider errors indicating
    streaming is not supported.
    """
    if self.api_mode == "codex_responses":
        # Codex streams internally via _run_codex_stream. The main dispatch
        # in _interruptible_api_call already calls it; we just need to
        # ensure on_first_delta reaches it. Store it on the instance
        # temporarily so _run_codex_stream can pick it up.
        self._codex_on_first_delta = on_first_delta
        try:
            return self._interruptible_api_call(api_kwargs)
        finally:
            self._codex_on_first_delta = None

    result = {"response": None, "error": None}
    request_client_holder = {"client": None}
    first_delta_fired = {"done": False}
    deltas_were_sent = {
        "yes": False
    }  # Track if any deltas were fired (for fallback)
    # Wall-clock timestamp of the last real streaming chunk.  The outer
    # poll loop uses this to detect stale connections that keep receiving
    # SSE keep-alive pings but no actual data.
    last_chunk_time = {"t": time.time()}

    def _fire_first_delta():
        if not first_delta_fired["done"] and on_first_delta:
            first_delta_fired["done"] = True
            try:
                on_first_delta()
            except Exception:
                pass

    def _call_chat_completions():
        """Stream a chat completions response."""
        import httpx as _httpx

        _base_timeout = float(os.getenv("DREW_API_TIMEOUT", 1800.0))
        _stream_read_timeout = float(os.getenv("HERMES_STREAM_READ_TIMEOUT", 60.0))
        stream_kwargs = {
            **api_kwargs,
            "stream": True,
            "stream_options": {"include_usage": True},
            "timeout": _httpx.Timeout(
                connect=30.0,
                read=_stream_read_timeout,
                write=_base_timeout,
                pool=30.0,
            ),
        }
        request_client_holder["client"] = self._create_request_openai_client(
            reason="chat_completion_stream_request"
        )
        # Reset stale-stream timer so the detector measures from this
        # attempt's start, not a previous attempt's last chunk.
        last_chunk_time["t"] = time.time()
        self._touch_activity("waiting for provider response (streaming)")
        stream = request_client_holder["client"].chat.completions.create(
            **stream_kwargs
        )

        content_parts: list = []
        tool_calls_acc: dict = {}
        tool_gen_notified: set = set()
        # Ollama-compatible endpoints reuse index 0 for every tool call
        # in a parallel batch, distinguishing them only by id.  Track
        # the last seen id per raw index so we can detect a new tool
        # call starting at the same index and redirect it to a fresh slot.
        _last_id_at_idx: dict = {}  # raw_index -> last seen non-empty id
        _active_slot_by_idx: dict = {}  # raw_index -> current slot in tool_calls_acc
        finish_reason = None
        model_name = None
        role = "assistant"
        reasoning_parts: list = []
        usage_obj = None
        # Reset per-call reasoning tracking so _build_assistant_message
        # knows whether reasoning was already displayed during streaming.
        self._reasoning_deltas_fired = False

        _first_chunk_seen = False
        for chunk in stream:
            last_chunk_time["t"] = time.time()
            if not _first_chunk_seen:
                _first_chunk_seen = True
                self._touch_activity("receiving stream response")

            if self._interrupt_requested:
                break

            if not chunk.choices:
                if hasattr(chunk, "model") and chunk.model:
                    model_name = chunk.model
                # Usage comes in the final chunk with empty choices
                if hasattr(chunk, "usage") and chunk.usage:
                    usage_obj = chunk.usage
                continue

            delta = chunk.choices[0].delta
            if hasattr(chunk, "model") and chunk.model:
                model_name = chunk.model

            # Accumulate reasoning content
            reasoning_text = getattr(delta, "reasoning_content", None) or getattr(
                delta, "reasoning", None
            )
            if reasoning_text:
                reasoning_parts.append(reasoning_text)
                _fire_first_delta()
                self._fire_reasoning_delta(reasoning_text)

            # Accumulate text content — fire callback only when no tool calls
            if delta and delta.content:
                content_parts.append(delta.content)
                if not tool_calls_acc:
                    _fire_first_delta()
                    self._fire_stream_delta(delta.content)
                    deltas_were_sent["yes"] = True
                else:
                    # Tool calls suppress regular content streaming (avoids
                    # displaying chatty "I'll use the tool..." text alongside
                    # tool calls).  But reasoning tags embedded in suppressed
                    # content should still reach the display — otherwise the
                    # reasoning box only appears as a post-response fallback,
                    # rendering it confusingly after the already-streamed
                    # response.  Route suppressed content through the stream
                    # delta callback so its tag extraction can fire the
                    # reasoning display.  Non-reasoning text is harmlessly
                    # suppressed by the CLI's _stream_delta when the stream
                    # box is already closed (tool boundary flush).
                    if self.stream_delta_callback:
                        try:
                            self.stream_delta_callback(delta.content)
                        except Exception:
                            pass

            # Accumulate tool call deltas — notify display on first name
            if delta and delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    raw_idx = tc_delta.index if tc_delta.index is not None else 0
                    delta_id = tc_delta.id or ""

                    # Ollama fix: detect a new tool call reusing the same
                    # raw index (different id) and redirect to a fresh slot.
                    if raw_idx not in _active_slot_by_idx:
                        _active_slot_by_idx[raw_idx] = raw_idx
                    if (
                        delta_id
                        and raw_idx in _last_id_at_idx
                        and delta_id != _last_id_at_idx[raw_idx]
                    ):
                        new_slot = max(tool_calls_acc, default=-1) + 1
                        _active_slot_by_idx[raw_idx] = new_slot
                    if delta_id:
                        _last_id_at_idx[raw_idx] = delta_id
                    idx = _active_slot_by_idx[raw_idx]

                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {
                            "id": tc_delta.id or "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                            "extra_content": None,
                        }
                    entry = tool_calls_acc[idx]
                    if tc_delta.id:
                        entry["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            entry["function"]["name"] += tc_delta.function.name
                        if tc_delta.function.arguments:
                            entry["function"]["arguments"] += (
                                tc_delta.function.arguments
                            )
                    extra = getattr(tc_delta, "extra_content", None)
                    if extra is None and hasattr(tc_delta, "model_extra"):
                        extra = (tc_delta.model_extra or {}).get("extra_content")
                    if extra is not None:
                        if hasattr(extra, "model_dump"):
                            extra = extra.model_dump()
                        entry["extra_content"] = extra
                    # Fire once per tool when the full name is available
                    name = entry["function"]["name"]
                    if name and idx not in tool_gen_notified:
                        tool_gen_notified.add(idx)
                        _fire_first_delta()
                        self._fire_tool_gen_started(name)

            if chunk.choices[0].finish_reason:
                finish_reason = chunk.choices[0].finish_reason

            # Usage in the final chunk
            if hasattr(chunk, "usage") and chunk.usage:
                usage_obj = chunk.usage

        # Build mock response matching non-streaming shape
        full_content = "".join(content_parts) or None
        mock_tool_calls = None
        if tool_calls_acc:
            mock_tool_calls = []
            for idx in sorted(tool_calls_acc):
                tc = tool_calls_acc[idx]
                mock_tool_calls.append(
                    SimpleNamespace(
                        id=tc["id"],
                        type=tc["type"],
                        extra_content=tc.get("extra_content"),
                        function=SimpleNamespace(
                            name=tc["function"]["name"],
                            arguments=tc["function"]["arguments"],
                        ),
                    )
                )

        full_reasoning = "".join(reasoning_parts) or None
        mock_message = SimpleNamespace(
            role=role,
            content=full_content,
            tool_calls=mock_tool_calls,
            reasoning_content=full_reasoning,
        )
        mock_choice = SimpleNamespace(
            index=0,
            message=mock_message,
            finish_reason=finish_reason or "stop",
        )
        return SimpleNamespace(
            id="stream-" + str(uuid.uuid4()),
            model=model_name,
            choices=[mock_choice],
            usage=usage_obj,
        )

    def _call_anthropic():
        """Stream an Anthropic Messages API response.

        Fires delta callbacks for real-time token delivery, but returns
        the native Anthropic Message object from get_final_message() so
        the rest of the agent loop (validation, tool extraction, etc.)
        works unchanged.
        """
        has_tool_use = False
        self._reasoning_deltas_fired = False

        # Reset stale-stream timer for this attempt
        last_chunk_time["t"] = time.time()
        # Use the Anthropic SDK's streaming context manager
        with self._anthropic_client.messages.stream(**api_kwargs) as stream:
            for event in stream:
                if self._interrupt_requested:
                    break

                event_type = getattr(event, "type", None)

                if event_type == "content_block_start":
                    block = getattr(event, "content_block", None)
                    if block and getattr(block, "type", None) == "tool_use":
                        has_tool_use = True
                        tool_name = getattr(block, "name", None)
                        if tool_name:
                            _fire_first_delta()
                            self._fire_tool_gen_started(tool_name)

                elif event_type == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    if delta:
                        delta_type = getattr(delta, "type", None)
                        if delta_type == "text_delta":
                            text = getattr(delta, "text", "")
                            if text and not has_tool_use:
                                _fire_first_delta()
                                self._fire_stream_delta(text)
                        elif delta_type == "thinking_delta":
                            thinking_text = getattr(delta, "thinking", "")
                            if thinking_text:
                                _fire_first_delta()
                                self._fire_reasoning_delta(thinking_text)

            # Return the native Anthropic Message for downstream processing
            return stream.get_final_message()

    def _call():
        import httpx as _httpx

        _max_stream_retries = int(os.getenv("HERMES_STREAM_RETRIES", 2))

        try:
            for _stream_attempt in range(_max_stream_retries + 1):
                try:
                    if self.api_mode == "anthropic_messages":
                        self._try_refresh_anthropic_client_credentials()
                        result["response"] = _call_anthropic()
                    else:
                        result["response"] = _call_chat_completions()
                    return  # success
                except Exception as e:
                    if deltas_were_sent["yes"]:
                        # Streaming failed AFTER some tokens were already
                        # delivered.  Don't retry or fall back — partial
                        # content already reached the user.
                        logger.warning(
                            "Streaming failed after partial delivery, not retrying: %s",
                            e,
                        )
                        result["error"] = e
                        return

                    _is_timeout = isinstance(
                        e,
                        (
                            _httpx.ReadTimeout,
                            _httpx.ConnectTimeout,
                            _httpx.PoolTimeout,
                        ),
                    )
                    _is_conn_err = isinstance(
                        e,
                        (
                            _httpx.ConnectError,
                            _httpx.RemoteProtocolError,
                            ConnectionError,
                        ),
                    )

                    # SSE error events from proxies (e.g. OpenRouter sends
                    # {"error":{"message":"Network connection lost."}}) are
                    # raised as APIError by the OpenAI SDK.  These are
                    # semantically identical to httpx connection drops —
                    # the upstream stream died — and should be retried with
                    # a fresh connection.  Distinguish from HTTP errors:
                    # APIError from SSE has no status_code, while
                    # APIStatusError (4xx/5xx) always has one.
                    _is_sse_conn_err = False
                    if not _is_timeout and not _is_conn_err:
                        from openai import APIError as _APIError

                        if isinstance(e, _APIError) and not getattr(
                            e, "status_code", None
                        ):
                            _err_lower_sse = str(e).lower()
                            _SSE_CONN_PHRASES = (
                                "connection lost",
                                "connection reset",
                                "connection closed",
                                "connection terminated",
                                "network error",
                                "network connection",
                                "terminated",
                                "peer closed",
                                "broken pipe",
                                "upstream connect error",
                            )
                            _is_sse_conn_err = any(
                                phrase in _err_lower_sse
                                for phrase in _SSE_CONN_PHRASES
                            )

                    if _is_timeout or _is_conn_err or _is_sse_conn_err:
                        # Transient network / timeout error. Retry the
                        # streaming request with a fresh connection first.
                        if _stream_attempt < _max_stream_retries:
                            logger.info(
                                "Streaming attempt %s/%s failed (%s: %s), "
                                "retrying with fresh connection...",
                                _stream_attempt + 1,
                                _max_stream_retries + 1,
                                type(e).__name__,
                                e,
                            )
                            self._emit_status(
                                f"⚠️ Connection to provider dropped "
                                f"({type(e).__name__}). Reconnecting… "
                                f"(attempt {_stream_attempt + 2}/{_max_stream_retries + 1})"
                            )
                            # Close the stale request client before retry
                            stale = request_client_holder.get("client")
                            if stale is not None:
                                self._close_request_openai_client(
                                    stale, reason="stream_retry_cleanup"
                                )
                                request_client_holder["client"] = None
                            # Also rebuild the primary client to purge
                            # any dead connections from the pool.
                            try:
                                self._replace_primary_openai_client(
                                    reason="stream_retry_pool_cleanup"
                                )
                            except Exception:
                                pass
                            continue
                        self._emit_status(
                            "❌ Connection to provider failed after "
                            f"{_max_stream_retries + 1} attempts. "
                            "The provider may be experiencing issues — "
                            "try again in a moment."
                        )
                        logger.warning(
                            "Streaming exhausted %s retries on transient error, "
                            "falling back to non-streaming: %s",
                            _max_stream_retries + 1,
                            e,
                        )
                    else:
                        _err_lower = str(e).lower()
                        _is_stream_unsupported = (
                            "stream" in _err_lower and "not supported" in _err_lower
                        )
                        if _is_stream_unsupported:
                            self._safe_print(
                                "\n⚠  Streaming is not supported for this "
                                "model/provider. Falling back to non-streaming.\n"
                                "   To avoid this delay, set display.streaming: false "
                                "in config.yaml\n"
                            )
                        logger.info(
                            "Streaming failed before delivery, falling back to non-streaming: %s",
                            e,
                        )

                    try:
                        # Reset stale timer — the non-streaming fallback
                        # uses its own client; prevent the stale detector
                        # from firing on stale timestamps from failed streams.
                        last_chunk_time["t"] = time.time()
                        result["response"] = self._interruptible_api_call(
                            api_kwargs
                        )
                    except Exception as fallback_err:
                        result["error"] = fallback_err
                    return
        finally:
            request_client = request_client_holder.get("client")
            if request_client is not None:
                self._close_request_openai_client(
                    request_client, reason="stream_request_complete"
                )

    _stream_stale_timeout_base = float(
        os.getenv("HERMES_STREAM_STALE_TIMEOUT", 180.0)
    )
    # Scale the stale timeout for large contexts: slow models (like Opus)
    # can legitimately think for minutes before producing the first token
    # when the context is large.  Without this, the stale detector kills
    # healthy connections during the model's thinking phase, producing
    # spurious RemoteProtocolError ("peer closed connection").
    _est_tokens = sum(len(str(v)) for v in api_kwargs.get("messages", [])) // 4
    if _est_tokens > 100_000:
        _stream_stale_timeout = max(_stream_stale_timeout_base, 300.0)
    elif _est_tokens > 50_000:
        _stream_stale_timeout = max(_stream_stale_timeout_base, 240.0)
    else:
        _stream_stale_timeout = _stream_stale_timeout_base

    _touch_interval = 30.0  # seconds between activity touches during streaming
    _last_touch = time.time()
    t = threading.Thread(target=_call, daemon=True)
    t.start()
    while t.is_alive():
        t.join(timeout=0.3)

        # Detect stale streams: connections kept alive by SSE pings
        # but delivering no real chunks.  Kill the client so the
        # inner retry loop can start a fresh connection.
        _stale_elapsed = time.time() - last_chunk_time["t"]
        if _stale_elapsed > _stream_stale_timeout:
            _est_ctx = sum(len(str(v)) for v in api_kwargs.get("messages", [])) // 4
            logger.warning(
                "Stream stale for %.0fs (threshold %.0fs) — no chunks received. "
                "model=%s context=~%s tokens. Killing connection.",
                _stale_elapsed,
                _stream_stale_timeout,
                api_kwargs.get("model", "unknown"),
                f"{_est_ctx:,}",
            )
            self._emit_status(
                f"⚠️ No response from provider for {int(_stale_elapsed)}s "
                f"(model: {api_kwargs.get('model', 'unknown')}, "
                f"context: ~{_est_ctx:,} tokens). "
                f"Reconnecting..."
            )
            try:
                rc = request_client_holder.get("client")
                if rc is not None:
                    self._close_request_openai_client(
                        rc, reason="stale_stream_kill"
                    )
            except Exception:
                pass
            # Rebuild the primary client too — its connection pool
            # may hold dead sockets from the same provider outage.
            try:
                self._replace_primary_openai_client(
                    reason="stale_stream_pool_cleanup"
                )
            except Exception:
                pass
            # Reset the timer so we don't kill repeatedly while
            # the inner thread processes the closure.
            last_chunk_time["t"] = time.time()

        # Periodically touch activity so the cron inactivity monitor
        # knows the agent is still alive during streaming.  Tokens are
        # arriving (last_chunk_time is fresh) but the main thread is here
        # in the polling loop, not in the stream consumer — so the
        # inactivity monitor would fire on the cron side unless we update
        # _last_activity_ts from this thread too.
        if time.time() - _last_touch >= _touch_interval:
            _last_touch = time.time()
            self._touch_activity(f"receiving stream (stale={int(_stale_elapsed)}s)")

        if self._interrupt_requested:
            try:
                if self.api_mode == "anthropic_messages":
                    from agent.anthropic_adapter import build_anthropic_client

                    self._anthropic_client.close()
                    self._anthropic_client = build_anthropic_client(
                        self._anthropic_api_key,
                        getattr(self, "_anthropic_base_url", None),
                    )
                else:
                    request_client = request_client_holder.get("client")
                    if request_client is not None:
                        self._close_request_openai_client(
                            request_client, reason="stream_interrupt_abort"
                        )
            except Exception:
                pass
            raise InterruptedError("Agent interrupted during streaming API call")
    if result["error"] is not None:
        if deltas_were_sent["yes"]:
            # Streaming failed AFTER some tokens were already delivered to
            # the platform.  Re-raising would let the outer retry loop make
            # a new API call, creating a duplicate message.  Return a
            # partial "stop" response instead so the outer loop treats this
            # turn as complete (no retry, no fallback).
            logger.warning(
                "Partial stream delivered before error; returning stub "
                "response to prevent duplicate messages: %s",
                result["error"],
            )
            _stub_msg = SimpleNamespace(
                role="assistant",
                content=None,
                tool_calls=None,
                reasoning_content=None,
            )
            return SimpleNamespace(
                id="partial-stream-stub",
                model=getattr(self, "model", "unknown"),
                choices=[
                    SimpleNamespace(
                        index=0,
                        message=_stub_msg,
                        finish_reason="stop",
                    )
                ],
                usage=None,
            )
        raise result["error"]
    return result["response"]

# ── Provider fallback ──────────────────────────────────────────────────


