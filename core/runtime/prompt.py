"""System prompt building — extracted from run_agent.py (step 1 of 3)."""
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



class PromptBuilder:
    """System prompt construction methods."""

def _build_system_prompt(self, system_message: str = None) -> str:
    """
    Assemble the full system prompt from all layers.

    Called once per session (cached on self._cached_system_prompt) and only
    rebuilt after context compression events. This ensures the system prompt
    is stable across all turns in a session, maximizing prefix cache hits.
    """
    # Layers (in order):
    #   1. Agent identity — SOUL.md when available, else DEFAULT_AGENT_IDENTITY
    #   2. User / gateway system prompt (if provided)
    #   3. Persistent memory (frozen snapshot)
    #   4. Skills guidance (if skills tools are loaded)
    #   5. Context files (AGENTS.md, .cursorrules — SOUL.md excluded here when used as identity)
    #   6. Current date & time (frozen at build time)
    #   7. Platform-specific formatting hint

    # Try SOUL.md as primary identity (unless context files are skipped)
    _soul_loaded = False
    if not self.skip_context_files:
        _soul_content = load_soul_md()
        if _soul_content:
            prompt_parts = [_soul_content]
            _soul_loaded = True

    # Dreams — insights from past sessions (Claude-style "dream" system)
    from agent.prompt_builder import load_dreams
    _dreams_content = load_dreams()
    if _dreams_content:
        prompt_parts.append(_dreams_content)

    if not _soul_loaded:
        # Fallback to hardcoded identity
        _ai_peer_name = None if False else None
        if _ai_peer_name:
            _identity = DEFAULT_AGENT_IDENTITY.replace(
                "You are Drewgent Agent",
                f"You are {_ai_peer_name}",
                1,
            )
        else:
            _identity = DEFAULT_AGENT_IDENTITY
        prompt_parts = [_identity]

    # Tool-aware behavioral guidance: only inject when the tools are loaded
    tool_guidance = []
    if "memory" in self.valid_tool_names:
        tool_guidance.append(MEMORY_GUIDANCE)
    if "session_search" in self.valid_tool_names:
        tool_guidance.append(SESSION_SEARCH_GUIDANCE)
    if "skill_manage" in self.valid_tool_names:
        tool_guidance.append(SKILLS_GUIDANCE)
    if tool_guidance:
        prompt_parts.append(" ".join(tool_guidance))

    # Brain governance (NeuronFS) - loaded after session search guidance
    # This renders the active brain's 7-layer subsumption hierarchy
    from agent.prompt_builder import brain_load

    # HP-3: Inject QA self-verification guidance when this is a latent task
    if hasattr(self, "_qa_task_id") and self._qa_task_id:
        from agent.prompt_builder import QA_GUIDANCE_TEMPLATE

        _evidence_dir = _qa_evidence_dir_for_task(self._qa_task_id)
        _qa_prompt = QA_GUIDANCE_TEMPLATE.format(
            task_id=self._qa_task_id, qa_evidence_dir=_evidence_dir
        )
        prompt_parts.append(_qa_prompt)
    brain_prompt = brain_load()
    if brain_prompt:
        prompt_parts.append(brain_prompt)

    nous_subscription_prompt = build_nous_subscription_prompt(self.valid_tool_names)
    if nous_subscription_prompt:
        prompt_parts.append(nous_subscription_prompt)
    # Tool-use enforcement: tells the model to actually call tools instead
    # of describing intended actions.  Controlled by config.yaml
    # agent.tool_use_enforcement:
    #   "auto" (default) — matches TOOL_USE_ENFORCEMENT_MODELS
    #   true  — always inject (all models)
    #   false — never inject
    #   list  — custom model-name substrings to match
    if self.valid_tool_names:
        _enforce = self._tool_use_enforcement
        _inject = False
        if _enforce is True or (
            isinstance(_enforce, str)
            and _enforce.lower() in ("true", "always", "yes", "on")
        ):
            _inject = True
        elif _enforce is False or (
            isinstance(_enforce, str)
            and _enforce.lower() in ("false", "never", "no", "off")
        ):
            _inject = False
        elif isinstance(_enforce, list):
            model_lower = (self.model or "").lower()
            _inject = any(
                p.lower() in model_lower for p in _enforce if isinstance(p, str)
            )
        else:
            # "auto" or any unrecognised value — use hardcoded defaults
            model_lower = (self.model or "").lower()
            _inject = any(p in model_lower for p in TOOL_USE_ENFORCEMENT_MODELS)
        if _inject:
            prompt_parts.append(TOOL_USE_ENFORCEMENT_GUIDANCE)
            _model_lower = (self.model or "").lower()
            # Google model operational guidance (conciseness, absolute
            # paths, parallel tool calls, verify-before-edit, etc.)
            if "gemini" in _model_lower or "gemma" in _model_lower:
                prompt_parts.append(GOOGLE_MODEL_OPERATIONAL_GUIDANCE)
            # OpenAI GPT/Codex execution discipline (tool persistence,
            # prerequisite checks, verification, anti-hallucination).
            if "gpt" in _model_lower or "codex" in _model_lower:
                prompt_parts.append(OPENAI_MODEL_EXECUTION_GUIDANCE)

    # so it can refer the user to them rather than reinventing answers.

    # Note: ephemeral_system_prompt is NOT included here. It's injected at
    # API-call time only so it stays out of the cached/stored system prompt.
    if system_message is not None:
        prompt_parts.append(system_message)

    if self._memory_store:
        if self._memory_enabled:
            mem_block = self._memory_store.format_for_system_prompt("memory")
            if mem_block:
                prompt_parts.append(mem_block)
        # USER.md is always included when enabled.
        if self._user_profile_enabled:
            user_block = self._memory_store.format_for_system_prompt("user")
            if user_block:
                prompt_parts.append(user_block)

    # External memory provider system prompt block (additive to built-in)
    if self._memory_manager:
        try:
            _ext_mem_block = self._memory_manager.build_system_prompt()
            if _ext_mem_block:
                prompt_parts.append(_ext_mem_block)
        except Exception:
            pass

    # Wiki knowledge base context (Obsidian bidirectional sync)
    # Reads recent entries from entities/, concepts/, insights/ for context
    if self._auto_learner and self._auto_learner.is_enabled:
        try:
            _wiki_context_enabled = getattr(self, "_wiki_context_enabled", True)
            if _wiki_context_enabled:
                _wiki_context = self._auto_learner.read_wiki_for_context(
                    max_entries=getattr(self, "_wiki_context_max_entries", 10),
                    max_chars=getattr(self, "_wiki_context_max_chars", 4000),
                )
                if _wiki_context:
                    prompt_parts.append(_wiki_context)
        except Exception:
            pass

    has_skills_tools = any(
        name in self.valid_tool_names
        for name in ["skills_list", "skill_view", "skill_manage"]
    )
    if has_skills_tools:
        avail_toolsets = {
            toolset
            for toolset in (
                get_toolset_for_tool(tool_name)
                for tool_name in self.valid_tool_names
            )
            if toolset
        }
        skills_prompt = build_skills_system_prompt(
            available_tools=self.valid_tool_names,
            available_toolsets=avail_toolsets,
        )
    else:
        skills_prompt = ""
    if skills_prompt:
        prompt_parts.append(skills_prompt)

    # P5-Ego Self-Model — 에이전트가 자기 구조를 인식하는 힌트
    # ~P5-ego/SELF_MODEL.md + ~P4-cortex/growth/INTEGRATION_PROTOCOL.md
    self_model_hint = _build_self_model_hint()
    if self_model_hint:
        prompt_parts.append(self_model_hint)

    # P6-prefrontal strategic context — plans and incidents
    prefrontal_hint = _build_prefrontal_hint()
    if prefrontal_hint:
        prompt_parts.append(prefrontal_hint)

    if not self.skip_context_files:
        # Use TERMINAL_CWD for context file discovery when set (gateway
        # mode).  The gateway process runs from the drewgent-agent install
        # dir, so os.getcwd() would pick up the repo's AGENTS.md and
        # other dev files — inflating token usage by ~10k for no benefit.
        _context_cwd = os.getenv("TERMINAL_CWD") or None
        context_files_prompt = build_context_files_prompt(
            cwd=_context_cwd, skip_soul=_soul_loaded
        )
        if context_files_prompt:
            prompt_parts.append(context_files_prompt)

    # Project context: load from ~/.drewgent/projects/<name>/.brain/ if set
    if not self.skip_context_files:
        from agent.project_context import build_project_context_prompt
        project_context = build_project_context_prompt()
        if project_context:
            prompt_parts.append(project_context)

    from drewgent_time import now as _drewgent_now

    now = _drewgent_now()
    timestamp_line = (
        f"Conversation started: {now.strftime('%A, %B %d, %Y %I:%M %p')}"
    )
    if self.pass_session_id and self.session_id:
        timestamp_line += f"\nSession ID: {self.session_id}"
    if self.model:
        timestamp_line += f"\nModel: {self.model}"
    if self.provider:
        timestamp_line += f"\nProvider: {self.provider}"
    prompt_parts.append(timestamp_line)

    # Alibaba Coding Plan API always returns "glm-4.7" as model name regardless
    # of the requested model. Inject explicit model identity into the system prompt
    # so the agent can correctly report which model it is (workaround for API bug).
    if self.provider == "alibaba":
        _model_short = (
            self.model.split("/")[-1] if "/" in self.model else self.model
        )
        prompt_parts.append(
            f"You are powered by the model named {_model_short}. "
            f"The exact model ID is {self.model}. "
            f"When asked what model you are, always answer based on this information, "
            f"not on any model name returned by the API."
        )

    platform_key = (self.platform or "").lower().strip()
    if platform_key in PLATFORM_HINTS:
        prompt_parts.append(PLATFORM_HINTS[platform_key])

    return "\n\n".join(prompt_parts)

def _invalidate_system_prompt(self):
    """
    Invalidate the cached system prompt, forcing a rebuild on the next turn.

    Called after context compression events. Also reloads memory from disk
    so the rebuilt prompt captures any writes from this session.
    """
    self._cached_system_prompt = None
    if self._memory_store:
        self._memory_store.load_from_disk()

