"""AIAgent internals — extracted from run_agent.py (step 1 of 3)."""
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



class AIAgentInternals:
    """All internal AIAgent methods.

    AIAgent in run_agent.py inherits from AIAgentInternals to get
    these methods while keeping the public API in one file.

    For backwards compatibility, run_agent.py is NOT changed in this step.
    Next: move AIAgentInternals into run_agent.py as a base class,
    then shrink run_agent.py.
    """

    """Reset all session-scoped token counters to 0 for a fresh session.

    This method encapsulates the reset logic for all session-level metrics
    including:
    - Token usage counters (input, output, total, prompt, completion)
    - Cache read/write tokens
    - API call count
    - Reasoning tokens
    - Estimated cost tracking
    - Context compressor internal counters

    The method safely handles optional attributes (e.g., context compressor)
    using ``hasattr`` checks.

    This keeps the counter reset logic DRY and maintainable in one place
    rather than scattering it across multiple methods.
    """
    # Token usage counters
    self.session_total_tokens = 0
    self.session_input_tokens = 0
    self.session_output_tokens = 0
    self.session_prompt_tokens = 0
    self.session_completion_tokens = 0
    self.session_cache_read_tokens = 0
    self.session_cache_write_tokens = 0
    self.session_reasoning_tokens = 0
    self.session_api_calls = 0
    self.session_estimated_cost_usd = 0.0
    self.session_cost_status = "unknown"
    self.session_cost_source = "none"

    # Turn counter (added after reset_session_state was first written — #2635)
    self._user_turn_count = 0

    # Context compressor internal counters (if present)
    if hasattr(self, "context_compressor") and self.context_compressor:
        self.context_compressor.last_prompt_tokens = 0
        self.context_compressor.last_completion_tokens = 0
        self.context_compressor.last_total_tokens = 0
        self.context_compressor.compression_count = 0
        self.context_compressor._context_probed = False
        self.context_compressor._context_probe_persistable = False
        # Iterative summary from previous session must not bleed into new one (#2635)
        self.context_compressor._previous_summary = None

def switch_model(
    self, new_model, new_provider, api_key="", base_url="", api_mode=""
):
    """Switch the model/provider in-place for a live agent.

    Called by the /model command handlers (CLI and gateway) after
    ``model_switch.switch_model()`` has resolved credentials and
    validated the model.  This method performs the actual runtime
    swap: rebuilding clients, updating caching flags, and refreshing
    the context compressor.

    The implementation mirrors ``_try_activate_fallback()`` for the
    client-swap logic but also updates ``_primary_runtime`` so the
    change persists across turns (unlike fallback which is
    turn-scoped).
    """
    import logging
    from drewgent_cli.providers import determine_api_mode

    # ── Determine api_mode if not provided ──
    if not api_mode:
        api_mode = determine_api_mode(new_provider, base_url)

    old_model = self.model
    old_provider = self.provider

    # ── Swap core runtime fields ──
    self.model = new_model
    self.provider = new_provider
    self.base_url = base_url or self.base_url
    self.api_mode = api_mode
    if api_key:
        self.api_key = api_key

    # ── Build new client ──
    if api_mode == "anthropic_messages":
        from agent.anthropic_adapter import (
            build_anthropic_client,
            resolve_anthropic_token,
            _is_oauth_token,
        )

        effective_key = api_key or self.api_key or resolve_anthropic_token() or ""
        self.api_key = effective_key
        self._anthropic_api_key = effective_key
        self._anthropic_base_url = base_url or getattr(
            self, "_anthropic_base_url", None
        )
        self._anthropic_client = build_anthropic_client(
            effective_key,
            self._anthropic_base_url,
        )
        self._is_anthropic_oauth = _is_oauth_token(effective_key)
        self.client = None
        self._client_kwargs = {}
    else:
        effective_key = api_key or self.api_key
        effective_base = base_url or self.base_url
        self._client_kwargs = {
            "api_key": effective_key,
            "base_url": effective_base,
        }
        self.client = self._create_openai_client(
            dict(self._client_kwargs),
            reason="switch_model",
            shared=True,
        )

    # ── Re-evaluate prompt caching ──
    is_native_anthropic = api_mode == "anthropic_messages"
    self._use_prompt_caching = (
        "openrouter" in (self.base_url or "").lower()
        and "claude" in new_model.lower()
    ) or is_native_anthropic

    # ── Update context compressor ──
    if hasattr(self, "context_compressor") and self.context_compressor:
        from agent.model_metadata import get_model_context_length

        new_context_length = get_model_context_length(
            self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            provider=self.provider,
        )
        self.context_compressor.model = self.model
        self.context_compressor.base_url = self.base_url
        self.context_compressor.api_key = self.api_key
        self.context_compressor.provider = self.provider
        self.context_compressor.context_length = new_context_length
        self.context_compressor.threshold_tokens = int(
            new_context_length * self.context_compressor.threshold_percent
        )

    # ── Invalidate cached system prompt so it rebuilds next turn ──
    self._cached_system_prompt = None

    # ── Update _primary_runtime so the change persists across turns ──
    _cc = (
        self.context_compressor
        if hasattr(self, "context_compressor") and self.context_compressor
        else None
    )
    self._primary_runtime = {
        "model": self.model,
        "provider": self.provider,
        "base_url": self.base_url,
        "api_mode": self.api_mode,
        "api_key": getattr(self, "api_key", ""),
        "client_kwargs": dict(self._client_kwargs),
        "use_prompt_caching": self._use_prompt_caching,
        "compressor_model": _cc.model if _cc else self.model,
        "compressor_base_url": _cc.base_url if _cc else self.base_url,
        "compressor_api_key": getattr(_cc, "api_key", "") if _cc else "",
        "compressor_provider": _cc.provider if _cc else self.provider,
        "compressor_context_length": _cc.context_length if _cc else 0,
        "compressor_threshold_tokens": _cc.threshold_tokens if _cc else 0,
    }
    if api_mode == "anthropic_messages":
        self._primary_runtime.update(
            {
                "anthropic_api_key": self._anthropic_api_key,
                "anthropic_base_url": self._anthropic_base_url,
                "is_anthropic_oauth": self._is_anthropic_oauth,
            }
        )

    # ── Reset fallback state ──
    self._fallback_activated = False
    self._fallback_index = 0

    logging.info(
        "Model switched in-place: %s (%s) -> %s (%s)",
        old_model,
        old_provider,
        new_model,
        new_provider,
    )

def _safe_print(self, *args, **kwargs):
    """Print that silently handles broken pipes / closed stdout.

    In headless environments (systemd, Docker, nohup) stdout may become
    unavailable mid-session.  A raw ``print()`` raises ``OSError`` which
    can crash cron jobs and lose completed work.

    Internally routes through ``self._print_fn`` (default: builtin
    ``print``) so callers such as the CLI can inject a renderer that
    handles ANSI escape sequences properly (e.g. prompt_toolkit's
    ``print_formatted_text(ANSI(...))``) without touching this method.
    """
    try:
        fn = self._print_fn or print
        fn(*args, **kwargs)
    except (OSError, ValueError):
        pass

def _vprint(self, *args, force: bool = False, **kwargs):
    """Verbose print — suppressed when actively streaming tokens.

    Pass ``force=True`` for error/warning messages that should always be
    shown even during streaming playback (TTS or display).

    During tool execution (``_executing_tools`` is True), printing is
    allowed even with stream consumers registered because no tokens
    are being streamed at that point.

    After the main response has been delivered and the remaining tool
    calls are post-response housekeeping (``_mute_post_response``),
    all non-forced output is suppressed.
    """
    if not force and getattr(self, "_mute_post_response", False):
        return
    if not force and self._has_stream_consumers() and not self._executing_tools:
        return
    self._safe_print(*args, **kwargs)

def _should_start_quiet_spinner(self) -> bool:
    """Return True when quiet-mode spinner output has a safe sink.

    In headless/stdio-protocol environments, a raw spinner with no custom
    ``_print_fn`` falls back to ``sys.stdout`` and can corrupt protocol
    streams such as ACP JSON-RPC. Allow quiet spinners only when either:
    - output is explicitly rerouted via ``_print_fn``; or
    - stdout is a real TTY.
    """
    if self._print_fn is not None:
        return True
    stream = getattr(sys, "stdout", None)
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False

def _emit_status(self, message: str) -> None:
    """Emit a lifecycle status message to both CLI and gateway channels.

    CLI users see the message via ``_vprint(force=True)`` so it is always
    visible regardless of verbose/quiet mode.  Gateway consumers receive
    it through ``status_callback("lifecycle", ...)``.

    This helper never raises — exceptions are swallowed so it cannot
    interrupt the retry/fallback logic.
    """
    try:
        self._vprint(f"{self.log_prefix}{message}", force=True)
    except Exception:
        pass
    if self.status_callback:
        try:
            self.status_callback("lifecycle", message)
        except Exception:
            logger.debug("status_callback error in _emit_status", exc_info=True)

def _is_direct_openai_url(self, base_url: str = None) -> bool:
    """Return True when a base URL targets OpenAI's native API."""
    url = (base_url or self._base_url_lower).lower()
    return "api.openai.com" in url and "openrouter" not in url

def _is_openrouter_url(self) -> bool:
    """Return True when the base URL targets OpenRouter."""
    return "openrouter" in self._base_url_lower

def _is_azure_openai_url(self, base_url: str = None) -> bool:
    """Return True when the base URL targets Azure OpenAI."""
    url = (base_url or self._base_url_lower).lower()
    return ".openai.azure.com" in url

def _is_github_copilot_url(self, base_url: str = None) -> bool:
    """Return True when the base URL targets GitHub Copilot."""
    url = (base_url or self._base_url_lower).lower()
    return "githubcopilot.com" in url

def _is_anthropic_url(self) -> bool:
    """Return True when the base URL targets Anthropic (native or /anthropic proxy path)."""
    return (
        "api.anthropic.com" in self._base_url_lower
        or self._base_url_lower.rstrip("/").endswith("/anthropic")
    )

    if is_native_anthropic:
        return True, True
    if is_openrouter and is_claude:
        return True, False
    if is_anthropic_wire and is_claude:
        # Third-party Anthropic-compatible gateway.
        return True, True

    # MiniMax on its Anthropic-compatible endpoint serves its own
    # model family (MiniMax-M2.7, M2.5, M2.1, M2) with documented
    # cache_control support (0.1× read pricing, 5-minute TTL).  The
    # blanket is_claude gate above excludes these — opt them in
    # explicitly via provider id or host match so users on
    # provider=minimax / minimax-cn (or custom endpoints pointing at
    # api.minimax.io/anthropic / api.minimaxi.com/anthropic) get the
    # same cost reduction as Claude traffic.
    # Docs: https://platform.minimax.io/docs/api-reference/anthropic-api-compatible-cache
    if is_anthropic_wire:
        is_minimax_provider = provider_lower in {"minimax", "minimax-cn"}
        is_minimax_host = (
            base_url_host_matches(eff_base_url, "api.minimax.io")
            or base_url_host_matches(eff_base_url, "api.minimaxi.com")
        )
        if is_minimax_provider or is_minimax_host:
            return True, True

    # Qwen/Alibaba on OpenCode (Zen/Go) and native DashScope: OpenAI-wire
    # transport that accepts Anthropic-style cache_control markers and
    # rewards them with real cache hits.  Without this branch
    # qwen3.6-plus on opencode-go reports 0% cached tokens and burns
    # through the subscription on every turn.
    model_is_qwen = "qwen" in model_lower
    provider_is_alibaba_family = provider_lower in {
        "opencode", "opencode-zen", "opencode-go", "alibaba",
    }
    if provider_is_alibaba_family and model_is_qwen:
        # Envelope layout (native_anthropic=False): markers on inner
        # content parts, not top-level tool messages.  Matches
        # pi-mono's "alibaba" cacheControlFormat.
        return True, False

    return False, False

@staticmethod
def _model_requires_responses_api(model: str) -> bool:
    """Return True for models that require the Responses API path.

    GPT-5.x models are rejected on /v1/chat/completions by both
    OpenAI and OpenRouter (error: ``unsupported_api_for_model``).
    Detect these so the correct api_mode is set regardless of
    which provider is serving the model.
    """
    m = model.lower()
    # Strip vendor prefix (e.g. "openai/gpt-5.4" → "gpt-5.4")
    if "/" in m:
        m = m.rsplit("/", 1)[-1]
    return m.startswith("gpt-5")

@staticmethod
def _provider_model_requires_responses_api(
    model: str,
    *,
    provider: Optional[str] = None,
) -> bool:
    """Return True when this provider/model pair should use Responses API."""
    normalized_provider = (provider or "").strip().lower()
    # Nous serves GPT-5.x models via its OpenAI-compatible chat
    # completions endpoint; its /v1/responses endpoint returns 404.
    if normalized_provider == "nous":
        return False
    if normalized_provider == "copilot":
        try:
            from hermes_cli.models import _should_use_copilot_responses_api
            return _should_use_copilot_responses_api(model)
        except Exception:
            # Fall back to the generic GPT-5 rule if Copilot-specific
            # logic is unavailable for any reason.
            pass
    return AIAgent._model_requires_responses_api(model)

def _max_tokens_param(self, value: int) -> dict:
    """Return the correct max tokens kwarg for the current provider.

    OpenAI's newer models (gpt-4o, o-series, gpt-5+) require
    'max_completion_tokens'. OpenRouter, local models, and older
    OpenAI models use 'max_tokens'.

    Azure OpenAI and GitHub Copilot also require 'max_completion_tokens'
    (they are OpenAI-compatible but their APIs reject 'max_tokens' for
    newer models).
    """
    if (
        self._is_direct_openai_url()
        or self._is_azure_openai_url()
        or self._is_github_copilot_url()
    ):
        return {"max_completion_tokens": value}
    return {"max_tokens": value}

def _has_content_after_think_block(self, content: str) -> bool:
    """
    Check if content has actual text after any reasoning/thinking blocks.

    This detects cases where the model only outputs reasoning but no actual
    response, which indicates an incomplete generation that should be retried.
    Must stay in sync with _strip_think_blocks() tag variants.

    Args:
        content: The assistant message content to check

    Returns:
        True if there's meaningful content after think blocks, False otherwise
    """
    if not content:
        return False

    # Remove all reasoning tag variants (must match _strip_think_blocks)
    cleaned = self._strip_think_blocks(content)

    # Check if there's any non-whitespace content remaining
    return bool(cleaned.strip())

def _strip_think_blocks(self, content: str) -> str:
    """Remove reasoning/thinking blocks from content, returning only visible text."""
    if not content:
        return ""
    # Strip all reasoning tag variants: <think>, <thinking>, <THINKING>,
    # <reasoning>, <REASONING_SCRATCHPAD>
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
    content = re.sub(
        r"<thinking>.*?</thinking>", "", content, flags=re.DOTALL | re.IGNORECASE
    )
    content = re.sub(r"<reasoning>.*?</reasoning>", "", content, flags=re.DOTALL)
    content = re.sub(
        r"<REASONING_SCRATCHPAD>.*?</REASONING_SCRATCHPAD>",
        "",
        content,
        flags=re.DOTALL,
    )
    content = re.sub(
        r"</?(?:think|thinking|reasoning|REASONING_SCRATCHPAD)>\s*",
        "",
        content,
        flags=re.IGNORECASE,
    )
    return content

def _looks_like_codex_intermediate_ack(
    self,
    user_message: str,
    assistant_content: str,
    messages: List[Dict[str, Any]],
) -> bool:
    """Detect a planning/ack message that should continue instead of ending the turn."""
    if any(isinstance(msg, dict) and msg.get("role") == "tool" for msg in messages):
        return False

    assistant_text = (
        self._strip_think_blocks(assistant_content or "").strip().lower()
    )
    if not assistant_text:
        return False
    if len(assistant_text) > 1200:
        return False

    has_future_ack = bool(
        re.search(
            r"\b(i['’]ll|i will|let me|i can do that|i can help with that)\b",
            assistant_text,
        )
    )
    if not has_future_ack:
        return False

    action_markers = (
        "look into",
        "look at",
        "inspect",
        "scan",
        "check",
        "analyz",
        "review",
        "explore",
        "read",
        "open",
        "run",
        "test",
        "fix",
        "debug",
        "search",
        "find",
        "walkthrough",
        "report back",
        "summarize",
    )
    workspace_markers = (
        "directory",
        "current directory",
        "current dir",
        "cwd",
        "repo",
        "repository",
        "codebase",
        "project",
        "folder",
        "filesystem",
        "file tree",
        "files",
        "path",
    )

    user_text = (user_message or "").strip().lower()
    user_targets_workspace = (
        any(marker in user_text for marker in workspace_markers)
        or "~/" in user_text
        or "/" in user_text
    )
    assistant_mentions_action = any(
        marker in assistant_text for marker in action_markers
    )
    assistant_targets_workspace = any(
        marker in assistant_text for marker in workspace_markers
    )
    return (
        user_targets_workspace or assistant_targets_workspace
    ) and assistant_mentions_action

def _extract_reasoning(self, assistant_message) -> Optional[str]:
    """
    Extract reasoning/thinking content from an assistant message.

    OpenRouter and various providers can return reasoning in multiple formats:
    1. message.reasoning - Direct reasoning field (DeepSeek, Qwen, etc.)
    2. message.reasoning_content - Alternative field (Moonshot AI, Novita, etc.)
    3. message.reasoning_details - Array of {type, summary, ...} objects (OpenRouter unified)

    Args:
        assistant_message: The assistant message object from the API response

    Returns:
        Combined reasoning text, or None if no reasoning found
    """
    reasoning_parts = []

    # Check direct reasoning field
    if hasattr(assistant_message, "reasoning") and assistant_message.reasoning:
        reasoning_parts.append(assistant_message.reasoning)

    # Check reasoning_content field (alternative name used by some providers)
    if (
        hasattr(assistant_message, "reasoning_content")
        and assistant_message.reasoning_content
    ):
        # Don't duplicate if same as reasoning
        if assistant_message.reasoning_content not in reasoning_parts:
            reasoning_parts.append(assistant_message.reasoning_content)

    # Check reasoning_details array (OpenRouter unified format)
    # Format: [{"type": "reasoning.summary", "summary": "...", ...}, ...]
    if (
        hasattr(assistant_message, "reasoning_details")
        and assistant_message.reasoning_details
    ):
        for detail in assistant_message.reasoning_details:
            if isinstance(detail, dict):
                # Extract summary from reasoning detail object
                summary = (
                    detail.get("summary")
                    or detail.get("thinking")
                    or detail.get("content")
                    or detail.get("text")
                )
                if summary and summary not in reasoning_parts:
                    reasoning_parts.append(summary)

    # Some providers embed reasoning directly inside assistant content
    # instead of returning structured reasoning fields.  Only fall back
    # to inline extraction when no structured reasoning was found.
    content = getattr(assistant_message, "content", None)
    if not reasoning_parts and isinstance(content, str) and content:
        inline_patterns = (
            r"<think>(.*?)</think>",
            r"<thinking>(.*?)</thinking>",
            r"<reasoning>(.*?)</reasoning>",
            r"<REASONING_SCRATCHPAD>(.*?)</REASONING_SCRATCHPAD>",
        )
        for pattern in inline_patterns:
            flags = re.DOTALL | re.IGNORECASE
            for block in re.findall(pattern, content, flags=flags):
                cleaned = block.strip()
                if cleaned and cleaned not in reasoning_parts:
                    reasoning_parts.append(cleaned)

    # Combine all reasoning parts
    if reasoning_parts:
        return "\n\n".join(reasoning_parts)

    return None

def _classify_empty_content_response(
    self,
    assistant_message,
    *,
    finish_reason: Optional[str],
    approx_tokens: int,
    api_messages: List[Dict[str, Any]],
    conversation_history: Optional[List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Classify think-only/empty responses so we can retry, compress, or salvage.

    We intentionally do NOT short-circuit all structured-reasoning responses.
    Prior discussion/PR history shows some models recover on retry. Instead we:
    - compress immediately when the pattern looks like implicit context pressure
    - salvage reasoning early when the same reasoning-only payload repeats
    - otherwise preserve the normal retry path
    """
    reasoning_text = self._extract_reasoning(assistant_message)
    has_structured_reasoning = bool(
        getattr(assistant_message, "reasoning", None)
        or getattr(assistant_message, "reasoning_content", None)
        or getattr(assistant_message, "reasoning_details", None)
    )
    content = getattr(assistant_message, "content", None) or ""
    stripped_content = self._strip_think_blocks(content).strip()
    signature = (
        content,
        reasoning_text or "",
        bool(has_structured_reasoning),
        finish_reason or "",
    )
    repeated_signature = signature == getattr(
        self, "_last_empty_content_signature", None
    )

    compressor = getattr(self, "context_compressor", None)
    ctx_len = getattr(compressor, "context_length", 0) or 0
    threshold_tokens = getattr(compressor, "threshold_tokens", 0) or 0
    is_large_session = bool(
        (ctx_len and approx_tokens >= max(int(ctx_len * 0.4), threshold_tokens))
        or len(api_messages) > 80
    )
    is_local_custom = is_local_endpoint(getattr(self, "base_url", "") or "")
    is_resumed = bool(conversation_history)
    context_pressure_signals = any(
        [
            finish_reason == "length",
            getattr(compressor, "_context_probed", False),
            is_large_session,
            is_resumed,
        ]
    )
    should_compress = bool(
        self.compression_enabled
        and is_local_custom
        and context_pressure_signals
        and not stripped_content
    )

    self._last_empty_content_signature = signature
    return {
        "reasoning_text": reasoning_text,
        "has_structured_reasoning": has_structured_reasoning,
        "repeated_signature": repeated_signature,
        "should_compress": should_compress,
        "is_local_custom": is_local_custom,
        "is_large_session": is_large_session,
        "is_resumed": is_resumed,
    }

def _cleanup_task_resources(self, task_id: str) -> None:
    """Clean up VM and browser resources for a given task."""
    try:
        cleanup_vm(task_id)
    except Exception as e:
        if self.verbose_logging:
            logging.warning(f"Failed to cleanup VM for task {task_id}: {e}")
    try:
        cleanup_browser(task_id)
    except Exception as e:
        if self.verbose_logging:
            logging.warning(f"Failed to cleanup browser for task {task_id}: {e}")

# ------------------------------------------------------------------
# Background memory/skill review
# ------------------------------------------------------------------

_MEMORY_REVIEW_PROMPT = (
    "Review the conversation above and consider saving to memory if appropriate.\n\n"
    "Focus on:\n"
    "1. Has the user revealed things about themselves — their persona, desires, "
    "preferences, or personal details worth remembering?\n"
    "2. Has the user expressed expectations about how you should behave, their work "
    "style, or ways they want you to operate?\n\n"
    "If something stands out, save it using the memory tool. "
    "If nothing is worth saving, just say 'Nothing to save.' and stop."
)

_SKILL_REVIEW_PROMPT = (
    "Review the conversation above and consider saving or updating a skill if appropriate.\n\n"
    "Focus on: was a non-trivial approach used to complete a task that required trial "
    "and error, or changing course due to experiential findings along the way, or did "
    "the user expect or desire a different method or outcome?\n\n"
    "If a relevant skill already exists, update it with what you learned. "
    "Otherwise, create a new skill if the approach is reusable.\n"
    "If nothing is worth saving, just say 'Nothing to save.' and stop."
)

_COMBINED_REVIEW_PROMPT = (
    "Review the conversation above and consider two things:\n\n"
    "**Memory**: Has the user revealed things about themselves — their persona, "
    "desires, preferences, or personal details? Has the user expressed expectations "
    "about how you should behave, their work style, or ways they want you to operate? "
    "If so, save using the memory tool.\n\n"
    "**Skills**: Was a non-trivial approach used to complete a task that required trial "
    "and error, or changing course due to experiential findings along the way, or did "
    "the user expect or desire a different method or outcome? If a relevant skill "
    "already exists, update it. Otherwise, create a new one if the approach is reusable.\n\n"
    "Only act if there's something genuinely worth saving. "
    "If nothing stands out, just say 'Nothing to save.' and stop."
)

def _spawn_background_review(
    self,
    messages_snapshot: List[Dict],
    review_memory: bool = False,
    review_skills: bool = False,
) -> None:
    """Spawn a background thread to review the conversation for memory/skill saves.

    Creates a full AIAgent fork with the same model, tools, and context as the
    main session. The review prompt is appended as the next user turn in the
    forked conversation. Writes directly to the shared memory/skill stores.
    Never modifies the main conversation history or produces user-visible output.
    """
    import threading

    # Pick the right prompt based on which triggers fired
    if review_memory and review_skills:
        prompt = self._COMBINED_REVIEW_PROMPT
    elif review_memory:
        prompt = self._MEMORY_REVIEW_PROMPT
    else:
        prompt = self._SKILL_REVIEW_PROMPT

    def _run_review():
        import contextlib, os as _os

        review_agent = None
        try:
            with (
                open(_os.devnull, "w") as _devnull,
                contextlib.redirect_stdout(_devnull),
                contextlib.redirect_stderr(_devnull),
            ):
                review_agent = AIAgent(
                    model=self.model,
                    max_iterations=8,
                    quiet_mode=True,
                    platform=self.platform,
                    provider=self.provider,
                )
                review_agent._memory_store = self._memory_store
                review_agent._memory_enabled = self._memory_enabled
                review_agent._user_profile_enabled = self._user_profile_enabled
                review_agent._memory_nudge_interval = 0
                review_agent._skill_nudge_interval = 0

                review_agent.run_conversation(
                    user_message=prompt,
                    conversation_history=messages_snapshot,
                )

            # Scan the review agent's messages for successful tool actions
            # and surface a compact summary to the user.
            actions = []
            for msg in getattr(review_agent, "_session_messages", []):
                if not isinstance(msg, dict) or msg.get("role") != "tool":
                    continue
                try:
                    data = json.loads(msg.get("content", "{}"))
                except (json.JSONDecodeError, TypeError):
                    continue
                if not data.get("success"):
                    continue
                message = data.get("message", "")
                target = data.get("target", "")
                if "created" in message.lower():
                    actions.append(message)
                elif "updated" in message.lower():
                    actions.append(message)
                elif "added" in message.lower() or (
                    target and "add" in message.lower()
                ):
                    label = (
                        "Memory"
                        if target == "memory"
                        else "User profile"
                        if target == "user"
                        else target
                    )
                    actions.append(f"{label} updated")
                elif "Entry added" in message:
                    label = (
                        "Memory"
                        if target == "memory"
                        else "User profile"
                        if target == "user"
                        else target
                    )
                    actions.append(f"{label} updated")
                elif "removed" in message.lower() or "replaced" in message.lower():
                    label = (
                        "Memory"
                        if target == "memory"
                        else "User profile"
                        if target == "user"
                        else target
                    )
                    actions.append(f"{label} updated")

            if actions:
                summary = " · ".join(dict.fromkeys(actions))
                self._safe_print(f"  💾 {summary}")
                _bg_cb = self.background_review_callback
                if _bg_cb:
                    try:
                        _bg_cb(f"💾 {summary}")
                    except Exception:
                        pass

        except Exception as e:
            logger.debug("Background memory/skill review failed: %s", e)
        finally:
            # Explicitly close the OpenAI/httpx client so GC doesn't
            # try to clean it up on a dead asyncio event loop (which
            # produces "Event loop is closed" errors in the terminal).
            if review_agent is not None:
                client = getattr(review_agent, "client", None)
                if client is not None:
                    try:
                        review_agent._close_openai_client(
                            client, reason="bg_review_done", shared=True
                        )
                        review_agent.client = None
                    except Exception:
                        pass

    t = threading.Thread(target=_run_review, daemon=True, name="bg-review")
    t.start()

def _apply_persist_user_message_override(self, messages: List[Dict]) -> None:
    """Rewrite the current-turn user message before persistence/return.

    Some call paths need an API-only user-message variant without letting
    that synthetic text leak into persisted transcripts or resumed session
    history. When an override is configured for the active turn, mutate the
    in-memory messages list in place so both persistence and returned
    history stay clean.
    """
    idx = getattr(self, "_persist_user_message_idx", None)
    override = getattr(self, "_persist_user_message_override", None)
    if override is None or idx is None:
        return
    if 0 <= idx < len(messages):
        msg = messages[idx]
        if isinstance(msg, dict) and msg.get("role") == "user":
            msg["content"] = override

def _persist_session(
    self, messages: List[Dict], conversation_history: List[Dict] = None
):
    """Save session state to both JSON log and SQLite on any exit path.

    Ensures conversations are never lost, even on errors or early returns.
    Skipped when ``persist_session=False`` (ephemeral helper flows).
    """
    if not self.persist_session:
        return
    self._apply_persist_user_message_override(messages)
    self._session_messages = messages
    self._save_session_log(messages)
    self._flush_messages_to_session_db(messages, conversation_history)

def _flush_messages_to_session_db(
    self, messages: List[Dict], conversation_history: List[Dict] = None
):
    """Persist any un-flushed messages to the SQLite session store.

    Uses _last_flushed_db_idx to track which messages have already been
    written, so repeated calls (from multiple exit paths) only write
    truly new messages — preventing the duplicate-write bug (#860).
    """
    if not self._session_db:
        return
    self._apply_persist_user_message_override(messages)
    try:
        # If create_session() failed at startup (e.g. transient lock), the
        # session row may not exist yet.  ensure_session() uses INSERT OR
        # IGNORE so it is a no-op when the row is already there.
        self._session_db.ensure_session(
            self.session_id,
            source=self.platform or "cli",
            model=self.model,
        )
        start_idx = len(conversation_history) if conversation_history else 0
        flush_from = max(start_idx, self._last_flushed_db_idx)
        for msg in messages[flush_from:]:
            role = msg.get("role", "unknown")
            content = msg.get("content")
            tool_calls_data = None
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                tool_calls_data = [
                    {"name": tc.function.name, "arguments": tc.function.arguments}
                    for tc in msg.tool_calls
                ]
            elif isinstance(msg.get("tool_calls"), list):
                tool_calls_data = msg["tool_calls"]
            self._session_db.append_message(
                session_id=self.session_id,
                role=role,
                content=content,
                tool_name=msg.get("tool_name"),
                tool_calls=tool_calls_data,
                tool_call_id=msg.get("tool_call_id"),
                finish_reason=msg.get("finish_reason"),
                reasoning=msg.get("reasoning") if role == "assistant" else None,
                reasoning_details=msg.get("reasoning_details")
                if role == "assistant"
                else None,
                codex_reasoning_items=msg.get("codex_reasoning_items")
                if role == "assistant"
                else None,
            )
        self._last_flushed_db_idx = len(messages)
    except Exception as e:
        logger.warning("Session DB append_message failed: %s", e)

def _get_messages_up_to_last_assistant(self, messages: List[Dict]) -> List[Dict]:
    """
    Get messages up to (but not including) the last assistant turn.

    This is used when we need to "roll back" to the last successful point
    in the conversation, typically when the final assistant message is
    incomplete or malformed.

    Args:
        messages: Full message list

    Returns:
        Messages up to the last complete assistant turn (ending with user/tool message)
    """
    if not messages:
        return []

    # Find the index of the last assistant message
    last_assistant_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            last_assistant_idx = i
            break

    if last_assistant_idx is None:
        # No assistant message found, return all messages
        return messages.copy()

    # Return everything up to (not including) the last assistant message
    return messages[:last_assistant_idx]

def _format_tools_for_system_message(self) -> str:
    """
    Format tool definitions for the system message in the trajectory format.

    Returns:
        str: JSON string representation of tool definitions
    """
    if not self.tools:
        return "[]"

    # Convert tool definitions to the format expected in trajectories
    formatted_tools = []
    for tool in self.tools:
        func = tool["function"]
        formatted_tool = {
            "name": func["name"],
            "description": func.get("description", ""),
            "parameters": func.get("parameters", {}),
            "required": None,  # Match the format in the example
        }
        formatted_tools.append(formatted_tool)

    return json.dumps(formatted_tools, ensure_ascii=False)

def _convert_to_trajectory_format(
    self, messages: List[Dict[str, Any]], user_query: str, completed: bool
) -> List[Dict[str, Any]]:
    """
    Convert internal message format to trajectory format for saving.

    Args:
        messages (List[Dict]): Internal message history
        user_query (str): Original user query
        completed (bool): Whether the conversation completed successfully

    Returns:
        List[Dict]: Messages in trajectory format
    """
    trajectory = []

    # Add system message with tool definitions
    system_msg = (
        "You are a function calling AI model. You are provided with function signatures within <tools> </tools> XML tags. "
        "You may call one or more functions to assist with the user query. If available tools are not relevant in assisting "
        "with user query, just respond in natural conversational language. Don't make assumptions about what values to plug "
        "into functions. After calling & executing the functions, you will be provided with function results within "
        "<tool_response> </tool_response> XML tags. Here are the available tools:\n"
        f"<tools>\n{self._format_tools_for_system_message()}\n</tools>\n"
        "For each function call return a JSON object, with the following pydantic model json schema for each:\n"
        "{'title': 'FunctionCall', 'type': 'object', 'properties': {'name': {'title': 'Name', 'type': 'string'}, "
        "'arguments': {'title': 'Arguments', 'type': 'object'}}, 'required': ['name', 'arguments']}\n"
        "Each function call should be enclosed within <tool_call> </tool_call> XML tags.\n"
        "Example:\n<tool_call>\n{'name': <function-name>,'arguments': <args-dict>}\n</tool_call>"
    )

    trajectory.append({"from": "system", "value": system_msg})

    # Add the actual user prompt (from the dataset) as the first human message
    trajectory.append({"from": "human", "value": user_query})

    # Skip the first message (the user query) since we already added it above.
    # Prefill messages are injected at API-call time only (not in the messages
    # list), so no offset adjustment is needed here.
    i = 1

    while i < len(messages):
        msg = messages[i]

        if msg["role"] == "assistant":
            # Check if this message has tool calls
            if "tool_calls" in msg and msg["tool_calls"]:
                # Format assistant message with tool calls
                # Add <think> tags around reasoning for trajectory storage
                content = ""

                # Prepend reasoning in <think> tags if available (native thinking tokens)
                if msg.get("reasoning") and msg["reasoning"].strip():
                    content = f"<think>\n{msg['reasoning']}\n</think>\n"

                if msg.get("content") and msg["content"].strip():
                    # Convert any <REASONING_SCRATCHPAD> tags to <think> tags
                    # (used when native thinking is disabled and model reasons via XML)
                    content += convert_scratchpad_to_think(msg["content"]) + "\n"

                # Add tool calls wrapped in XML tags
                for tool_call in msg["tool_calls"]:
                    if not tool_call or not isinstance(tool_call, dict):
                        continue
                    # Parse arguments - should always succeed since we validate during conversation
                    # but keep try-except as safety net
                    try:
                        arguments = (
                            json.loads(tool_call["function"]["arguments"])
                            if isinstance(tool_call["function"]["arguments"], str)
                            else tool_call["function"]["arguments"]
                        )
                    except json.JSONDecodeError:
                        # This shouldn't happen since we validate and retry during conversation,
                        # but if it does, log warning and use empty dict
                        logging.warning(
                            f"Unexpected invalid JSON in trajectory conversion: {tool_call['function']['arguments'][:100]}"
                        )
                        arguments = {}

                    tool_call_json = {
                        "name": tool_call["function"]["name"],
                        "arguments": arguments,
                    }
                    content += f"<tool_call>\n{json.dumps(tool_call_json, ensure_ascii=False)}\n</tool_call>\n"

                # Ensure every gpt turn has a <think> block (empty if no reasoning)
                # so the format is consistent for training data
                if "<think>" not in content:
                    content = "<think>\n</think>\n" + content

                trajectory.append({"from": "gpt", "value": content.rstrip()})

                # Collect all subsequent tool responses
                tool_responses = []
                j = i + 1
                while j < len(messages) and messages[j]["role"] == "tool":
                    tool_msg = messages[j]
                    # Format tool response with XML tags
                    tool_response = "<tool_response>\n"

                    # Try to parse tool content as JSON if it looks like JSON
                    tool_content = tool_msg["content"]
                    try:
                        if tool_content.strip().startswith(("{", "[")):
                            tool_content = json.loads(tool_content)
                    except (json.JSONDecodeError, AttributeError):
                        pass  # Keep as string if not valid JSON

                    tool_index = len(tool_responses)
                    tool_name = (
                        msg["tool_calls"][tool_index]["function"]["name"]
                        if tool_index < len(msg["tool_calls"])
                        else "unknown"
                    )
                    tool_response += json.dumps(
                        {
                            "tool_call_id": tool_msg.get("tool_call_id", ""),
                            "name": tool_name,
                            "content": tool_content,
                        },
                        ensure_ascii=False,
                    )
                    tool_response += "\n</tool_response>"
                    tool_responses.append(tool_response)
                    j += 1

                # Add all tool responses as a single message
                if tool_responses:
                    trajectory.append(
                        {"from": "tool", "value": "\n".join(tool_responses)}
                    )
                    i = j - 1  # Skip the tool messages we just processed

            else:
                # Regular assistant message without tool calls
                # Add <think> tags around reasoning for trajectory storage
                content = ""

                # Prepend reasoning in <think> tags if available (native thinking tokens)
                if msg.get("reasoning") and msg["reasoning"].strip():
                    content = f"<think>\n{msg['reasoning']}\n</think>\n"

                # Convert any <REASONING_SCRATCHPAD> tags to <think> tags
                # (used when native thinking is disabled and model reasons via XML)
                raw_content = msg["content"] or ""
                content += convert_scratchpad_to_think(raw_content)

                # Ensure every gpt turn has a <think> block (empty if no reasoning)
                if "<think>" not in content:
                    content = "<think>\n</think>\n" + content

                trajectory.append({"from": "gpt", "value": content.strip()})

        elif msg["role"] == "user":
            trajectory.append({"from": "human", "value": msg["content"]})

        i += 1

    return trajectory

def _save_trajectory(
    self, messages: List[Dict[str, Any]], user_query: str, completed: bool
):
    """
    Save conversation trajectory to JSONL file.

    Args:
        messages (List[Dict]): Complete message history
        user_query (str): Original user query
        completed (bool): Whether the conversation completed successfully
    """
    if not self.save_trajectories:
        return

    trajectory = self._convert_to_trajectory_format(messages, user_query, completed)
    _save_trajectory_to_file(trajectory, self.model, completed)

@staticmethod
def _summarize_api_error(error: Exception) -> str:
    """Extract a human-readable one-liner from an API error.

    Handles Cloudflare HTML error pages (502, 503, etc.) by pulling the
    <title> tag instead of dumping raw HTML.  Falls back to a truncated
    str(error) for everything else.
    """
    import re as _re

    raw = str(error)

    # Cloudflare / proxy HTML pages: grab the <title> for a clean summary
    if "<!DOCTYPE" in raw or "<html" in raw:
        m = _re.search(r"<title[^>]*>([^<]+)</title>", raw, _re.IGNORECASE)
        title = m.group(1).strip() if m else "HTML error page (title not found)"
        # Also grab Cloudflare Ray ID if present
        ray = _re.search(r"Cloudflare Ray ID:\s*<strong[^>]*>([^<]+)</strong>", raw)
        ray_id = ray.group(1).strip() if ray else None
        status_code = getattr(error, "status_code", None)
        parts = []
        if status_code:
            parts.append(f"HTTP {status_code}")
        parts.append(title)
        if ray_id:
            parts.append(f"Ray {ray_id}")
        return " — ".join(parts)

    # JSON body errors from OpenAI/Anthropic SDKs
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        msg = (
            body.get("error", {}).get("message")
            if isinstance(body.get("error"), dict)
            else body.get("message")
        )
        if msg:
            status_code = getattr(error, "status_code", None)
            prefix = f"HTTP {status_code}: " if status_code else ""
            return f"{prefix}{msg[:300]}"

    # Fallback: truncate the raw string but give more room than 200 chars
    status_code = getattr(error, "status_code", None)
    prefix = f"HTTP {status_code}: " if status_code else ""
    return f"{prefix}{raw[:500]}"

def _mask_api_key_for_logs(self, key: Optional[str]) -> Optional[str]:
    if not key:
        return None
    if len(key) <= 12:
        return "***"
    return f"{key[:8]}...{key[-4:]}"

def _clean_error_message(self, error_msg: str) -> str:
    """
    Clean up error messages for user display, removing HTML content and truncating.

    Args:
        error_msg: Raw error message from API or exception

    Returns:
        Clean, user-friendly error message
    """
    if not error_msg:
        return "Unknown error"

    # Remove HTML content (common with CloudFlare and gateway error pages)
    if error_msg.strip().startswith("<!DOCTYPE html") or "<html" in error_msg:
        return "Service temporarily unavailable (HTML error page returned)"

    # Remove newlines and excessive whitespace
    cleaned = " ".join(error_msg.split())

    # Truncate if too long
    if len(cleaned) > 150:
        cleaned = cleaned[:150] + "..."

    return cleaned

@staticmethod
def _extract_api_error_context(error: Exception) -> Dict[str, Any]:
    """Extract structured rate-limit details from provider errors."""
    context: Dict[str, Any] = {}

    body = getattr(error, "body", None)
    payload = None
    if isinstance(body, dict):
        payload = body.get("error") if isinstance(body.get("error"), dict) else body
    if isinstance(payload, dict):
        reason = payload.get("code") or payload.get("error")
        if isinstance(reason, str) and reason.strip():
            context["reason"] = reason.strip()
        message = payload.get("message") or payload.get("error_description")
        if isinstance(message, str) and message.strip():
            context["message"] = message.strip()
        for key in ("resets_at", "reset_at"):
            value = payload.get(key)
            if value not in (None, ""):
                context["reset_at"] = value
                break
        retry_after = payload.get("retry_after")
        if retry_after not in (None, "") and "reset_at" not in context:
            try:
                context["reset_at"] = time.time() + float(retry_after)
            except (TypeError, ValueError):
                pass

    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        retry_after = headers.get("retry-after") or headers.get("Retry-After")
        if retry_after and "reset_at" not in context:
            try:
                context["reset_at"] = time.time() + float(retry_after)
            except (TypeError, ValueError):
                pass
        ratelimit_reset = headers.get("x-ratelimit-reset")
        if ratelimit_reset and "reset_at" not in context:
            context["reset_at"] = ratelimit_reset

    if "message" not in context:
        raw_message = str(error).strip()
        if raw_message:
            context["message"] = raw_message[:500]

    if "reset_at" not in context:
        message = context.get("message") or ""
        if isinstance(message, str):
            delay_match = re.search(
                r"quotaResetDelay[:\s\"]+(\\d+(?:\\.\\d+)?)(ms|s)",
                message,
                re.IGNORECASE,
            )
            if delay_match:
                value = float(delay_match.group(1))
                seconds = (
                    value / 1000.0
                    if delay_match.group(2).lower() == "ms"
                    else value
                )
                context["reset_at"] = time.time() + seconds
            else:
                sec_match = re.search(
                    r"retry\s+(?:after\s+)?(\d+(?:\.\d+)?)\s*(?:sec|secs|seconds|s\b)",
                    message,
                    re.IGNORECASE,
                )
                if sec_match:
                    context["reset_at"] = time.time() + float(sec_match.group(1))

    return context

def _usage_summary_for_api_request_hook(
    self, response: Any
) -> Optional[Dict[str, Any]]:
    """Token buckets for ``post_api_request`` plugins (no raw ``response`` object)."""
    if response is None:
        return None
    raw_usage = getattr(response, "usage", None)
    if not raw_usage:
        return None
    from dataclasses import asdict

    cu = normalize_usage(raw_usage, provider=self.provider, api_mode=self.api_mode)
    summary = asdict(cu)
    summary.pop("raw_usage", None)
    summary["prompt_tokens"] = cu.prompt_tokens
    summary["total_tokens"] = cu.total_tokens
    return summary

def _dump_api_request_debug(
    self,
    api_kwargs: Dict[str, Any],
    *,
    reason: str,
    error: Optional[Exception] = None,
) -> Optional[Path]:
    """
    Dump a debug-friendly HTTP request record for the active inference API.

    Captures the request body from api_kwargs (excluding transport-only keys
    like timeout). Intended for debugging provider-side 4xx failures where
    retries are not useful.
    """
    try:
        body = copy.deepcopy(api_kwargs)
        body.pop("timeout", None)
        body = {k: v for k, v in body.items() if v is not None}

        api_key = None
        try:
            api_key = getattr(self.client, "api_key", None)
        except Exception as e:
            logger.debug("Could not extract API key for debug dump: %s", e)

        dump_payload: Dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "session_id": self.session_id,
            "reason": reason,
            "request": {
                "method": "POST",
                "url": f"{self.base_url.rstrip('/')}{'/responses' if self.api_mode == 'codex_responses' else '/chat/completions'}",
                "headers": {
                    "Authorization": f"Bearer {self._mask_api_key_for_logs(api_key)}",
                    "Content-Type": "application/json",
                },
                "body": body,
            },
        }

        if error is not None:
            error_info: Dict[str, Any] = {
                "type": type(error).__name__,
                "message": str(error),
            }
            for attr_name in ("status_code", "request_id", "code", "param", "type"):
                attr_value = getattr(error, attr_name, None)
                if attr_value is not None:
                    error_info[attr_name] = attr_value

            body_attr = getattr(error, "body", None)
            if body_attr is not None:
                error_info["body"] = body_attr

            response_obj = getattr(error, "response", None)
            if response_obj is not None:
                try:
                    error_info["response_status"] = getattr(
                        response_obj, "status_code", None
                    )
                    error_info["response_text"] = response_obj.text
                except Exception as e:
                    logger.debug("Could not extract error response details: %s", e)

            dump_payload["error"] = error_info

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        dump_file = (
            self.logs_dir / f"request_dump_{self.session_id}_{timestamp}.json"
        )
        dump_file.write_text(
            json.dumps(dump_payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

        self._vprint(
            f"{self.log_prefix}🧾 Request debug dump written to: {dump_file}"
        )

        if env_var_enabled("HERMES_DUMP_REQUEST_STDOUT"):
            print(
                json.dumps(dump_payload, ensure_ascii=False, indent=2, default=str)
            )

        return dump_file
    except Exception as dump_error:
        if self.verbose_logging:
            logging.warning(
                f"Failed to dump API request debug payload: {dump_error}"
            )
        return None

@staticmethod
def _clean_session_content(content: str) -> str:
    """Convert REASONING_SCRATCHPAD to think tags and clean up whitespace."""
    if not content:
        return content
    content = convert_scratchpad_to_think(content)
    content = re.sub(r"\n+(<think>)", r"\n\1", content)
    content = re.sub(r"(</think>)\n+", r"\1\n", content)
    return content.strip()

def _save_session_log(self, messages: List[Dict[str, Any]] = None):
    """
    Save the full raw session to a JSON file.

    Stores every message exactly as the agent sees it: user messages,
    assistant messages (with reasoning, finish_reason, tool_calls),
    tool responses (with tool_call_id, tool_name), and injected system
    messages (compression summaries, todo snapshots, etc.).

    REASONING_SCRATCHPAD tags are converted to <think> blocks for consistency.
    Overwritten after each turn so it always reflects the latest state.
    """
    messages = messages or self._session_messages
    if not messages:
        return

    try:
        # Clean assistant content for session logs
        cleaned = []
        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("content"):
                msg = dict(msg)
                msg["content"] = self._clean_session_content(msg["content"])
            cleaned.append(msg)

        # Guard: never overwrite a larger session log with fewer messages.
        # This protects against data loss when --resume loads a session whose
        # messages weren't fully written to SQLite — the resumed agent starts
        # with partial history and would otherwise clobber the full JSON log.
        if self.session_log_file.exists():
            try:
                existing = json.loads(
                    self.session_log_file.read_text(encoding="utf-8")
                )
                existing_count = existing.get(
                    "message_count", len(existing.get("messages", []))
                )
                if existing_count > len(cleaned):
                    logging.debug(
                        "Skipping session log overwrite: existing has %d messages, current has %d",
                        existing_count,
                        len(cleaned),
                    )
                    return
            except Exception:
                pass  # corrupted existing file — allow the overwrite

        entry = {
            "session_id": self.session_id,
            "model": self.model,
            "base_url": self.base_url,
            "platform": self.platform,
            "session_start": self.session_start.isoformat(),
            "last_updated": datetime.now().isoformat(),
            "system_prompt": self._cached_system_prompt or "",
            "tools": self.tools or [],
            "message_count": len(cleaned),
            "messages": cleaned,
            # Phase 1-3: brain decision log per turn
            "brain_decision_log": getattr(self, "_brain_decision_log", []) or [],
        }

        atomic_json_write(
            self.session_log_file,
            entry,
            indent=2,
            default=str,
        )

    except Exception as e:
        if self.verbose_logging:
            logging.warning(f"Failed to save session log: {e}")

def interrupt(self, message: str = None) -> None:
    """
    Request the agent to interrupt its current tool-calling loop.

    Call this from another thread (e.g., input handler, message receiver)
    to gracefully stop the agent and process a new message.

    Also signals long-running tool executions (e.g. terminal commands)
    to terminate early, so the agent can respond immediately.

    Args:
        message: Optional new message that triggered the interrupt.
                 If provided, the agent will include this in its response context.

    Example (CLI):
        # In a separate input thread:
        if user_typed_something:
            agent.interrupt(user_input)

    Example (Messaging):
        # When new message arrives for active session:
        if session_has_running_agent:
            running_agent.interrupt(new_message.text)
    """
    self._interrupt_requested = True
    self._interrupt_message = message
    # Signal all tools to abort any in-flight operations immediately
    _set_interrupt(True)
    # Propagate interrupt to any running child agents (subagent delegation)
    with self._active_children_lock:
        children_copy = list(self._active_children)
    for child in children_copy:
        try:
            child.interrupt(message)
        except Exception as e:
            logger.debug("Failed to propagate interrupt to child agent: %s", e)
    if not self.quiet_mode:
        print(
            "\n⚡ Interrupt requested"
            + (
                f": '{message[:40]}...'"
                if message and len(message) > 40
                else f": '{message}'"
                if message
                else ""
            )
        )

def clear_interrupt(self) -> None:
    """Clear any pending interrupt request and the global tool interrupt signal."""
    self._interrupt_requested = False
    self._interrupt_message = None
    _set_interrupt(False)

def _touch_activity(self, desc: str) -> None:
    """Update the last-activity timestamp and description (thread-safe)."""
    self._last_activity_ts = time.time()
    self._last_activity_desc = desc

def get_activity_summary(self) -> dict:
    """Return a snapshot of the agent's current activity for diagnostics.

    Called by the gateway timeout handler to report what the agent was doing
    when it was killed, and by the periodic "still working" notifications.
    """
    elapsed = time.time() - self._last_activity_ts
    return {
        "last_activity_ts": self._last_activity_ts,
        "last_activity_desc": self._last_activity_desc,
        "seconds_since_activity": round(elapsed, 1),
        "current_tool": self._current_tool,
        "api_call_count": self._api_call_count,
        "max_iterations": self.max_iterations,
        "budget_used": self.iteration_budget.used,
        "budget_max": self.iteration_budget.max_total,
    }

def shutdown_memory_provider(self, messages: list = None) -> None:
    """Shut down the memory provider — call at actual session boundaries.

    This calls on_session_end() then shutdown_all() on the memory
    manager. NOT called per-turn — only at CLI exit, /reset, gateway
    session expiry, etc.
    """
    # Deep reflection: session-end auto-learning via AutoLearner
    if self._auto_learner and messages:
        try:
            self._auto_learner.on_session_end(messages)
        except Exception:
            pass
        try:
            self._auto_learner.run_maintenance()
        except Exception:
            pass

    if self._memory_manager:
        try:
            self._memory_manager.on_session_end(messages or [])
        except Exception:
            pass
        try:
            self._memory_manager.shutdown_all()
        except Exception:
            pass

    # Brain signal: persist active workflows at session end
    try:
        _sp = get_signal_processor()
        if self.session_id:
            # session_db is the SessionDB instance; persist workflows to it
            if hasattr(self, "_session_db") and self._session_db:
                _sp.persist_active_workflows(self._session_db, self.session_id)
    except Exception:
        pass

    # Brain signal monitor: stop and flush at session end
    if getattr(self, "_brain_monitor_started", False):
        try:
            from agent.brain_monitor import stop_monitor
            stop_monitor(self.session_id)
            self._brain_monitor_started = False
        except Exception:
            pass

def _hydrate_todo_store(self, history: List[Dict[str, Any]]) -> None:
    """
    Recover todo state from conversation history.

    The gateway creates a fresh AIAgent per message, so the in-memory
    TodoStore is empty. We scan the history for the most recent todo
    tool response and replay it to reconstruct the state.
    """
    # Walk history backwards to find the most recent todo tool response
    last_todo_response = None
    for msg in reversed(history):
        if msg.get("role") != "tool":
            continue
        content = msg.get("content", "")
        # Quick check: todo responses contain "todos" key
        if '"todos"' not in content:
            continue
        try:
            data = json.loads(content)
            if "todos" in data and isinstance(data["todos"], list):
                last_todo_response = data["todos"]
                break
        except (json.JSONDecodeError, TypeError):
            continue

    if last_todo_response:
        # Replay the items into the store (replace mode)
        self._todo_store.write(last_todo_response, merge=False)
        if not self.quiet_mode:
            self._vprint(
                f"{self.log_prefix}📋 Restored {len(last_todo_response)} todo item(s) from history"
            )
    _set_interrupt(False)

@property
def is_interrupted(self) -> bool:
    """Check if an interrupt has been requested."""
    return self._interrupt_requested


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


def _try_activate_fallback(self) -> bool:
    """Switch to the next fallback model/provider in the chain.

    Called when the current model is failing after retries.  Swaps the
    OpenAI client, model slug, and provider in-place so the retry loop
    can continue with the new backend.  Advances through the chain on
    each call; returns False when exhausted.

    Uses the centralized provider router (resolve_provider_client) for
    auth resolution and client construction — no duplicated provider→key
    mappings.
    """
    if self._fallback_index >= len(self._fallback_chain):
        return False

    fb = self._fallback_chain[self._fallback_index]
    self._fallback_index += 1
    fb_provider = (fb.get("provider") or "").strip().lower()
    fb_model = (fb.get("model") or "").strip()
    if not fb_provider or not fb_model:
        return self._try_activate_fallback()  # skip invalid, try next

    # Use centralized router for client construction.
    # raw_codex=True because the main agent needs direct responses.stream()
    # access for Codex providers.
    try:
        from agent.auxiliary_client import resolve_provider_client

        # Pass base_url and api_key from fallback config so custom
        # endpoints (e.g. Ollama Cloud) resolve correctly instead of
        # falling through to OpenRouter defaults.
        fb_base_url_hint = (fb.get("base_url") or "").strip() or None
        fb_api_key_hint = (fb.get("api_key") or "").strip() or None
        # For Ollama Cloud endpoints, pull OLLAMA_API_KEY from env
        # when no explicit key is in the fallback config.
        if (
            fb_base_url_hint
            and "ollama.com" in fb_base_url_hint.lower()
            and not fb_api_key_hint
        ):
            fb_api_key_hint = os.getenv("OLLAMA_API_KEY") or None
        fb_client, _ = resolve_provider_client(
            fb_provider,
            model=fb_model,
            raw_codex=True,
            explicit_base_url=fb_base_url_hint,
            explicit_api_key=fb_api_key_hint,
        )
        if fb_client is None:
            logging.warning(
                "Fallback to %s failed: provider not configured", fb_provider
            )
            return self._try_activate_fallback()  # try next in chain

        # Determine api_mode from provider / base URL
        fb_api_mode = "chat_completions"
        fb_base_url = str(fb_client.base_url)
        if fb_provider == "openai-codex":
            fb_api_mode = "codex_responses"
        elif fb_provider == "anthropic" or fb_base_url.rstrip("/").lower().endswith(
            "/anthropic"
        ):
            fb_api_mode = "anthropic_messages"
        elif self._is_direct_openai_url(fb_base_url):
            fb_api_mode = "codex_responses"

        old_model = self.model
        self.model = fb_model
        self.provider = fb_provider
        self.base_url = fb_base_url
        self.api_mode = fb_api_mode
        self._fallback_activated = True

        if fb_api_mode == "anthropic_messages":
            # Build native Anthropic client instead of using OpenAI client
            from agent.anthropic_adapter import (
                build_anthropic_client,
                resolve_anthropic_token,
                _is_oauth_token,
            )

            effective_key = (
                (fb_client.api_key or resolve_anthropic_token() or "")
                if fb_provider == "anthropic"
                else (fb_client.api_key or "")
            )
            self.api_key = effective_key
            self._anthropic_api_key = effective_key
            self._anthropic_base_url = getattr(fb_client, "base_url", None)
            self._anthropic_client = build_anthropic_client(
                effective_key, self._anthropic_base_url
            )
            self._is_anthropic_oauth = _is_oauth_token(effective_key)
            self.client = None
            self._client_kwargs = {}
        else:
            # Swap OpenAI client and config in-place
            self.api_key = fb_client.api_key
            self.client = fb_client
            self._client_kwargs = {
                "api_key": fb_client.api_key,
                "base_url": fb_base_url,
            }

        # Re-evaluate prompt caching for the new provider/model
        is_native_anthropic = fb_api_mode == "anthropic_messages"
        self._use_prompt_caching = (
            "openrouter" in fb_base_url.lower() and "claude" in fb_model.lower()
        ) or is_native_anthropic

        # Update context compressor limits for the fallback model.
        # Without this, compression decisions use the primary model's
        # context window (e.g. 200K) instead of the fallback's (e.g. 32K),
        # causing oversized sessions to overflow the fallback.
        if hasattr(self, "context_compressor") and self.context_compressor:
            from agent.model_metadata import get_model_context_length

            fb_context_length = get_model_context_length(
                self.model,
                base_url=self.base_url,
                api_key=self.api_key,
                provider=self.provider,
            )
            self.context_compressor.model = self.model
            self.context_compressor.base_url = self.base_url
            self.context_compressor.api_key = self.api_key
            self.context_compressor.provider = self.provider
            self.context_compressor.context_length = fb_context_length
            self.context_compressor.threshold_tokens = int(
                fb_context_length * self.context_compressor.threshold_percent
            )

        self._emit_status(
            f"🔄 Primary model failed — switching to fallback: "
            f"{fb_model} via {fb_provider}"
        )
        logging.info(
            "Fallback activated: %s → %s (%s)",
            old_model,
            fb_model,
            fb_provider,
        )
        return True
    except Exception as e:
        logging.error("Failed to activate fallback %s: %s", fb_model, e)
        return self._try_activate_fallback()  # try next in chain

# ── Per-turn primary restoration ─────────────────────────────────────

def _restore_primary_runtime(self) -> bool:
    """Restore the primary runtime at the start of a new turn.

    In long-lived CLI sessions a single AIAgent instance spans multiple
    turns.  Without restoration, one transient failure pins the session
    to the fallback provider for every subsequent turn.  Calling this at
    the top of ``run_conversation()`` makes fallback turn-scoped.

    The gateway creates a fresh agent per message so this is a no-op
    there (``_fallback_activated`` is always False at turn start).
    """
    if not self._fallback_activated:
        return False

    rt = self._primary_runtime
    try:
        # ── Core runtime state ──
        self.model = rt["model"]
        self.provider = rt["provider"]
        self.base_url = rt["base_url"]  # setter updates _base_url_lower
        self.api_mode = rt["api_mode"]
        self.api_key = rt["api_key"]
        self._client_kwargs = dict(rt["client_kwargs"])
        self._use_prompt_caching = rt["use_prompt_caching"]

        # ── Rebuild client for the primary provider ──
        if self.api_mode == "anthropic_messages":
            from agent.anthropic_adapter import build_anthropic_client

            self._anthropic_api_key = rt["anthropic_api_key"]
            self._anthropic_base_url = rt["anthropic_base_url"]
            self._anthropic_client = build_anthropic_client(
                rt["anthropic_api_key"],
                rt["anthropic_base_url"],
            )
            self._is_anthropic_oauth = rt["is_anthropic_oauth"]
            self.client = None
        else:
            self.client = self._create_openai_client(
                dict(rt["client_kwargs"]),
                reason="restore_primary",
                shared=True,
            )

        # ── Restore context compressor state ──
        cc = self.context_compressor
        cc.model = rt["compressor_model"]
        cc.base_url = rt["compressor_base_url"]
        cc.api_key = rt["compressor_api_key"]
        cc.provider = rt["compressor_provider"]
        cc.context_length = rt["compressor_context_length"]
        cc.threshold_tokens = rt["compressor_threshold_tokens"]

        # ── Reset fallback chain for the new turn ──
        self._fallback_activated = False
        self._fallback_index = 0

        logging.info(
            "Primary runtime restored for new turn: %s (%s)",
            self.model,
            self.provider,
        )
        return True
    except Exception as e:
        logging.warning("Failed to restore primary runtime: %s", e)
        return False

# Which error types indicate a transient transport failure worth
# one more attempt with a rebuilt client / connection pool.
_TRANSIENT_TRANSPORT_ERRORS = frozenset(
    {
        "ReadTimeout",
        "ConnectTimeout",
        "PoolTimeout",
        "ConnectError",
        "RemoteProtocolError",
    }
)

def _try_recover_primary_transport(
    self,
    api_error: Exception,
    *,
    retry_count: int,
    max_retries: int,
) -> bool:
    """Attempt one extra primary-provider recovery cycle for transient transport failures.

    After ``max_retries`` exhaust, rebuild the primary client (clearing
    stale connection pools) and give it one more attempt before falling
    back.  This is most useful for direct endpoints (custom, Z.AI,
    Anthropic, OpenAI, local models) where a TCP-level hiccup does not
    mean the provider is down.

    Skipped for proxy/aggregator providers (OpenRouter, Nous) which
    already manage connection pools and retries server-side — if our
    retries through them are exhausted, one more rebuilt client won't help.
    """
    if self._fallback_activated:
        return False

    # Only for transient transport errors
    error_type = type(api_error).__name__
    if error_type not in self._TRANSIENT_TRANSPORT_ERRORS:
        return False

    # Skip for aggregator providers — they manage their own retry infra
    if self._is_openrouter_url():
        return False
    provider_lower = (self.provider or "").strip().lower()
    if provider_lower in ("nous", "nous-research"):
        return False

    try:
        # Close existing client to release stale connections
        if getattr(self, "client", None) is not None:
            try:
                self._close_openai_client(
                    self.client,
                    reason="primary_recovery",
                    shared=True,
                )
            except Exception:
                pass

        # Rebuild from primary snapshot
        rt = self._primary_runtime
        self._client_kwargs = dict(rt["client_kwargs"])
        self.model = rt["model"]
        self.provider = rt["provider"]
        self.base_url = rt["base_url"]
        self.api_mode = rt["api_mode"]
        self.api_key = rt["api_key"]

        if self.api_mode == "anthropic_messages":
            from agent.anthropic_adapter import build_anthropic_client

            self._anthropic_api_key = rt["anthropic_api_key"]
            self._anthropic_base_url = rt["anthropic_base_url"]
            self._anthropic_client = build_anthropic_client(
                rt["anthropic_api_key"],
                rt["anthropic_base_url"],
            )
            self._is_anthropic_oauth = rt["is_anthropic_oauth"]
            self.client = None
        else:
            self.client = self._create_openai_client(
                dict(rt["client_kwargs"]),
                reason="primary_recovery",
                shared=True,
            )

        wait_time = min(3 + retry_count, 8)
        self._vprint(
            f"{self.log_prefix}🔁 Transient {error_type} on {self.provider} — "
            f"rebuilt client, waiting {wait_time}s before one last primary attempt.",
            force=True,
        )
        time.sleep(wait_time)
        return True
    except Exception as e:
        logging.warning("Primary transport recovery failed: %s", e)
        return False

# ── End provider fallback ──────────────────────────────────────────────

@staticmethod
def _content_has_image_parts(content: Any) -> bool:
    if not isinstance(content, list):
        return False
    for part in content:
        if isinstance(part, dict) and part.get("type") in {
            "image_url",
            "input_image",
        }:
            return True
    return False

@staticmethod
def _materialize_data_url_for_vision(image_url: str) -> tuple[str, Optional[Path]]:
    header, _, data = str(image_url or "").partition(",")
    mime = "image/jpeg"
    if header.startswith("data:"):
        mime_part = header[len("data:") :].split(";", 1)[0].strip()
        if mime_part.startswith("image/"):
            mime = mime_part
    suffix = {
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
    }.get(mime, ".jpg")
    tmp = tempfile.NamedTemporaryFile(
        prefix="anthropic_image_", suffix=suffix, delete=False
    )
    with tmp:
        tmp.write(base64.b64decode(data))
    path = Path(tmp.name)
    return str(path), path

def _describe_image_for_anthropic_fallback(self, image_url: str, role: str) -> str:
    cache_key = hashlib.sha256(str(image_url or "").encode("utf-8")).hexdigest()
    cached = self._anthropic_image_fallback_cache.get(cache_key)
    if cached:
        return cached

    role_label = {
        "assistant": "assistant",
        "tool": "tool result",
    }.get(role, "user")
    analysis_prompt = (
        "Describe everything visible in this image in thorough detail. "
        "Include any text, code, UI, data, objects, people, layout, colors, "
        "and any other notable visual information."
    )

    vision_source = str(image_url or "")
    cleanup_path: Optional[Path] = None
    if vision_source.startswith("data:"):
        vision_source, cleanup_path = self._materialize_data_url_for_vision(
            vision_source
        )

    description = ""
    try:
        from tools.vision_tools import vision_analyze_tool

        result_json = asyncio.run(
            vision_analyze_tool(
                image_url=vision_source, user_prompt=analysis_prompt
            )
        )
        result = json.loads(result_json) if isinstance(result_json, str) else {}
        description = (result.get("analysis") or "").strip()
    except Exception as e:
        description = f"Image analysis failed: {e}"
    finally:
        if cleanup_path and cleanup_path.exists():
            try:
                cleanup_path.unlink()
            except OSError:
                pass

    if not description:
        description = "Image analysis failed."

    note = f"[The {role_label} attached an image. Here's what it contains:\n{description}]"
    if vision_source and not str(image_url or "").startswith("data:"):
        note += f"\n[If you need a closer look, use vision_analyze with image_url: {vision_source}]"

    self._anthropic_image_fallback_cache[cache_key] = note
    return note

def _preprocess_anthropic_content(self, content: Any, role: str) -> Any:
    if not self._content_has_image_parts(content):
        return content

    text_parts: List[str] = []
    image_notes: List[str] = []
    for part in content:
        if isinstance(part, str):
            if part.strip():
                text_parts.append(part.strip())
            continue
        if not isinstance(part, dict):
            continue

        ptype = part.get("type")
        if ptype in {"text", "input_text"}:
            text = str(part.get("text", "") or "").strip()
            if text:
                text_parts.append(text)
            continue

        if ptype in {"image_url", "input_image"}:
            image_data = part.get("image_url", {})
            image_url = (
                image_data.get("url", "")
                if isinstance(image_data, dict)
                else str(image_data or "")
            )
            if image_url:
                image_notes.append(
                    self._describe_image_for_anthropic_fallback(image_url, role)
                )
            else:
                image_notes.append(
                    "[An image was attached but no image source was available.]"
                )
            continue

        text = str(part.get("text", "") or "").strip()
        if text:
            text_parts.append(text)

    prefix = "\n\n".join(note for note in image_notes if note).strip()
    suffix = "\n".join(text for text in text_parts if text).strip()
    if prefix and suffix:
        return f"{prefix}\n\n{suffix}"
    if prefix:
        return prefix
    if suffix:
        return suffix
    return (
        "[A multimodal message was converted to text for Anthropic compatibility.]"
    )

def _prepare_anthropic_messages_for_api(self, api_messages: list) -> list:
    if not any(
        isinstance(msg, dict) and self._content_has_image_parts(msg.get("content"))
        for msg in api_messages
    ):
        return api_messages

    transformed = copy.deepcopy(api_messages)
    for msg in transformed:
        if not isinstance(msg, dict):
            continue
        msg["content"] = self._preprocess_anthropic_content(
            msg.get("content"),
            str(msg.get("role", "user") or "user"),
        )
    return transformed

def _anthropic_preserve_dots(self) -> bool:
    """True when using an anthropic-compatible endpoint that preserves dots in model names.
    Alibaba/DashScope keeps dots (e.g. qwen3.5-plus).
    OpenCode Go keeps dots (e.g. minimax-m2.7)."""
    if (getattr(self, "provider", "") or "").lower() in {"alibaba", "opencode-go"}:
        return True
    base = (getattr(self, "base_url", "") or "").lower()
    return "dashscope" in base or "aliyuncs" in base or "opencode.ai/zen/go" in base

def _build_api_kwargs(self, api_messages: list) -> dict:
    """Build the keyword arguments dict for the active API mode."""
    if self.api_mode == "anthropic_messages":
        from agent.anthropic_adapter import build_anthropic_kwargs

        anthropic_messages = self._prepare_anthropic_messages_for_api(api_messages)
        ctx_len = getattr(self, "context_compressor", None)
        ctx_len = ctx_len.context_length if ctx_len else None

        # Tool manifest mode: send lightweight manifest first, expand on demand
        if self._tool_manifest_mode:
            # Import here only when actually needed (tool manifest mode is off by default)
            from model_tools import get_tool_manifest, _CORE_MANIFEST_TOOLS
            if self._active_tool_schemas:
                # Expanded schemas accumulated from prior tool calls
                return build_anthropic_kwargs(
                    model=self.model,
                    messages=anthropic_messages,
                    tools=self._active_tool_schemas,
                    max_tokens=self.max_tokens,
                    reasoning_config=self.reasoning_config,
                    is_oauth=self._is_anthropic_oauth,
                    preserve_dots=self._anthropic_preserve_dots(),
                    context_length=ctx_len,
                )
            # First turn: send only the manifest (name + one-line descriptions)
            manifest_tools = get_tool_manifest()
            return build_anthropic_kwargs(
                model=self.model,
                messages=anthropic_messages,
                tools=manifest_tools,
                max_tokens=self.max_tokens,
                reasoning_config=self.reasoning_config,
                is_oauth=self._is_anthropic_oauth,
                preserve_dots=self._anthropic_preserve_dots(),
                context_length=ctx_len,
            )

        # Normal mode: send all available tools
        return build_anthropic_kwargs(
            model=self.model,
            messages=anthropic_messages,
            tools=self.tools,
            max_tokens=self.max_tokens,
            reasoning_config=self.reasoning_config,
            is_oauth=self._is_anthropic_oauth,
            preserve_dots=self._anthropic_preserve_dots(),
            context_length=ctx_len,
        )

    if self.api_mode == "codex_responses":
        instructions = ""
        payload_messages = api_messages
        if api_messages and api_messages[0].get("role") == "system":
            instructions = str(api_messages[0].get("content") or "").strip()
            payload_messages = api_messages[1:]
        if not instructions:
            instructions = DEFAULT_AGENT_IDENTITY

        is_github_responses = (
            "models.github.ai" in self.base_url.lower()
            or "api.githubcopilot.com" in self.base_url.lower()
        )

        # Resolve reasoning effort: config > default (medium)
        reasoning_effort = "medium"
        reasoning_enabled = True
        if self.reasoning_config and isinstance(self.reasoning_config, dict):
            if self.reasoning_config.get("enabled") is False:
                reasoning_enabled = False
            elif self.reasoning_config.get("effort"):
                reasoning_effort = self.reasoning_config["effort"]

        kwargs = {
            "model": self.model,
            "instructions": instructions,
            "input": self._chat_messages_to_responses_input(payload_messages),
            "tools": self._responses_tools(),
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "store": False,
        }

        if not is_github_responses:
            kwargs["prompt_cache_key"] = self.session_id

        if reasoning_enabled:
            if is_github_responses:
                # Copilot's Responses route advertises reasoning-effort support,
                # but not OpenAI-specific prompt cache or encrypted reasoning
                # fields. Keep the payload to the documented subset.
                github_reasoning = self._github_models_reasoning_extra_body()
                if github_reasoning is not None:
                    kwargs["reasoning"] = github_reasoning
            else:
                kwargs["reasoning"] = {
                    "effort": reasoning_effort,
                    "summary": "auto",
                }
                kwargs["include"] = ["reasoning.encrypted_content"]
        elif not is_github_responses:
            kwargs["include"] = []

        if self.max_tokens is not None:
            kwargs["max_output_tokens"] = self.max_tokens

        return kwargs

    sanitized_messages = api_messages
    needs_sanitization = False
    for msg in api_messages:
        if not isinstance(msg, dict):
            continue
        if "codex_reasoning_items" in msg:
            needs_sanitization = True
            break

        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                if "call_id" in tool_call or "response_item_id" in tool_call:
                    needs_sanitization = True
                    break
            if needs_sanitization:
                break

    if needs_sanitization:
        sanitized_messages = copy.deepcopy(api_messages)
        for msg in sanitized_messages:
            if not isinstance(msg, dict):
                continue

            # Codex-only replay state must not leak into strict chat-completions APIs.
            msg.pop("codex_reasoning_items", None)

            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    if isinstance(tool_call, dict):
                        tool_call.pop("call_id", None)
                        tool_call.pop("response_item_id", None)

    # GPT-5 and Codex models respond better to 'developer' than 'system'
    # for instruction-following.  Swap the role at the API boundary so
    # internal message representation stays uniform ("system").
    _model_lower = (self.model or "").lower()
    if (
        sanitized_messages
        and sanitized_messages[0].get("role") == "system"
        and any(p in _model_lower for p in DEVELOPER_ROLE_MODELS)
    ):
        # Shallow-copy the list + first message only — rest stays shared.
        sanitized_messages = list(sanitized_messages)
        sanitized_messages[0] = {**sanitized_messages[0], "role": "developer"}

    provider_preferences = {}
    if self.providers_allowed:
        provider_preferences["only"] = self.providers_allowed
    if self.providers_ignored:
        provider_preferences["ignore"] = self.providers_ignored
    if self.providers_order:
        provider_preferences["order"] = self.providers_order
    if self.provider_sort:
        provider_preferences["sort"] = self.provider_sort
    if self.provider_require_parameters:
        provider_preferences["require_parameters"] = True
    if self.provider_data_collection:
        provider_preferences["data_collection"] = self.provider_data_collection

    api_kwargs = {
        "model": self.model,
        "messages": sanitized_messages,
        "timeout": float(os.getenv("DREW_API_TIMEOUT", 1800.0)),
    }
    if self.tools:
        api_kwargs["tools"] = self.tools

    if self.max_tokens is not None:
        api_kwargs.update(self._max_tokens_param(self.max_tokens))
    elif self._is_openrouter_url() and "claude" in (self.model or "").lower():
        # OpenRouter translates requests to Anthropic's Messages API,
        # which requires max_tokens as a mandatory field.  When we omit
        # it, OpenRouter picks a default that can be too low — the model
        # spends its output budget on thinking and has almost nothing
        # left for the actual response (especially large tool calls like
        # write_file).  Sending the model's real output limit ensures
        # full capacity.  Other providers handle the default fine.
        try:
            from agent.anthropic_adapter import _get_anthropic_max_output

            _model_output_limit = _get_anthropic_max_output(self.model)
            api_kwargs["max_tokens"] = _model_output_limit
        except Exception:
            pass  # fail open — let OpenRouter pick its default

    extra_body = {}

    _is_openrouter = self._is_openrouter_url()
    _is_github_models = (
        "models.github.ai" in self._base_url_lower
        or "api.githubcopilot.com" in self._base_url_lower
    )

    # Provider preferences (only, ignore, order, sort) are OpenRouter-
    # specific.  Only send to OpenRouter-compatible endpoints.
    # TODO: Nous Portal will add transparent proxy support — re-enable
    # for _is_nous when their backend is updated.
    if provider_preferences and _is_openrouter:
        extra_body["provider"] = provider_preferences
    _is_nous = "nousresearch" in self._base_url_lower

    if self._supports_reasoning_extra_body():
        if _is_github_models:
            github_reasoning = self._github_models_reasoning_extra_body()
            if github_reasoning is not None:
                extra_body["reasoning"] = github_reasoning
        else:
            if self.reasoning_config is not None:
                rc = dict(self.reasoning_config)
                # Nous Portal requires reasoning enabled — don't send
                # enabled=false to it (would cause 400).
                if _is_nous and rc.get("enabled") is False:
                    pass  # omit reasoning entirely for Nous when disabled
                else:
                    extra_body["reasoning"] = rc
            else:
                extra_body["reasoning"] = {"enabled": True, "effort": "medium"}

    # Nous Portal product attribution
    if _is_nous:
        extra_body["tags"] = ["product=drewgent-agent"]

    if extra_body:
        api_kwargs["extra_body"] = extra_body

    # xAI prompt caching: send x-grok-conv-id header to route requests
    # to the same server, maximizing automatic cache hits.
    # https://docs.x.ai/developers/advanced-api-usage/prompt-caching
    if (
        "x.ai" in self._base_url_lower
        and hasattr(self, "session_id")
        and self.session_id
    ):
        api_kwargs["extra_headers"] = {"x-grok-conv-id": self.session_id}

    return api_kwargs

def _supports_reasoning_extra_body(self) -> bool:
    """Return True when reasoning extra_body is safe to send for this route/model.

    OpenRouter forwards unknown extra_body fields to upstream providers.
    Some providers/routes reject `reasoning` with 400s, so gate it to
    known reasoning-capable model families and direct Nous Portal.
    """
    if "nousresearch" in self._base_url_lower:
        return True
    if "ai-gateway.vercel.sh" in self._base_url_lower:
        return True
    if (
        "models.github.ai" in self._base_url_lower
        or "api.githubcopilot.com" in self._base_url_lower
    ):
        try:
            from drewgent_cli.models import github_model_reasoning_efforts

            return bool(github_model_reasoning_efforts(self.model))
        except Exception:
            return False
    if "openrouter" not in self._base_url_lower:
        return False
    if "api.mistral.ai" in self._base_url_lower:
        return False

    model = (self.model or "").lower()
    reasoning_model_prefixes = (
        "deepseek/",
        "anthropic/",
        "openai/",
        "x-ai/",
        "google/gemini-2",
        "qwen/qwen3",
    )
    return any(model.startswith(prefix) for prefix in reasoning_model_prefixes)

def _github_models_reasoning_extra_body(self) -> dict | None:
    """Format reasoning payload for GitHub Models/OpenAI-compatible routes."""
    try:
        from drewgent_cli.models import github_model_reasoning_efforts
    except Exception:
        return None

    supported_efforts = github_model_reasoning_efforts(self.model)
    if not supported_efforts:
        return None

    if self.reasoning_config and isinstance(self.reasoning_config, dict):
        if self.reasoning_config.get("enabled") is False:
            return None
        requested_effort = (
            str(self.reasoning_config.get("effort", "medium")).strip().lower()
        )
    else:
        requested_effort = "medium"

    if requested_effort == "xhigh" and "high" in supported_efforts:
        requested_effort = "high"
    elif requested_effort not in supported_efforts:
        if requested_effort == "minimal" and "low" in supported_efforts:
            requested_effort = "low"
        elif "medium" in supported_efforts:
            requested_effort = "medium"
        else:
            requested_effort = supported_efforts[0]

    return {"effort": requested_effort}

def _build_assistant_message(self, assistant_message, finish_reason: str) -> dict:
    """Build a normalized assistant message dict from an API response message.

    Handles reasoning extraction, reasoning_details, and optional tool_calls
    so both the tool-call path and the final-response path share one builder.
    """
    reasoning_text = self._extract_reasoning(assistant_message)
    _from_structured = bool(reasoning_text)

    # Fallback: extract inline <think> blocks from content when no structured
    # reasoning fields are present (some models/providers embed thinking
    # directly in the content rather than returning separate API fields).
    if not reasoning_text:
        content = assistant_message.content or ""
        think_blocks = re.findall(r"<think>(.*?)</think>", content, flags=re.DOTALL)
        if think_blocks:
            combined = "\n\n".join(b.strip() for b in think_blocks if b.strip())
            reasoning_text = combined or None

    if reasoning_text and self.verbose_logging:
        logging.debug(
            f"Captured reasoning ({len(reasoning_text)} chars): {reasoning_text}"
        )

    if reasoning_text and self.reasoning_callback:
        # Skip callback when streaming is active — reasoning was already
        # displayed during the stream via one of two paths:
        #   (a) _fire_reasoning_delta (structured reasoning_content deltas)
        #   (b) _stream_delta tag extraction (<think>/<REASONING_SCRATCHPAD>)
        # When streaming is NOT active, always fire so non-streaming modes
        # (gateway, batch, quiet) still get reasoning.
        # Any reasoning that wasn't shown during streaming is caught by the
        # CLI post-response display fallback (cli.py _reasoning_shown_this_turn).
        if not self.stream_delta_callback:
            try:
                self.reasoning_callback(reasoning_text)
            except Exception:
                pass

    msg = {
        "role": "assistant",
        "content": assistant_message.content or "",
        "reasoning": reasoning_text,
        "finish_reason": finish_reason,
    }

    if (
        hasattr(assistant_message, "reasoning_details")
        and assistant_message.reasoning_details
    ):
        # Pass reasoning_details back unmodified so providers (OpenRouter,
        # Anthropic, OpenAI) can maintain reasoning continuity across turns.
        # Each provider may include opaque fields (signature, encrypted_content)
        # that must be preserved exactly.
        raw_details = assistant_message.reasoning_details
        preserved = []
        for d in raw_details:
            if isinstance(d, dict):
                preserved.append(d)
            elif hasattr(d, "__dict__"):
                preserved.append(d.__dict__)
            elif hasattr(d, "model_dump"):
                preserved.append(d.model_dump())
        if preserved:
            msg["reasoning_details"] = preserved

    # Codex Responses API: preserve encrypted reasoning items for
    # multi-turn continuity. These get replayed as input on the next turn.
    codex_items = getattr(assistant_message, "codex_reasoning_items", None)
    if codex_items:
        msg["codex_reasoning_items"] = codex_items

    if assistant_message.tool_calls:
        tool_calls = []
        for tool_call in assistant_message.tool_calls:
            raw_id = getattr(tool_call, "id", None)
            call_id = getattr(tool_call, "call_id", None)
            if not isinstance(call_id, str) or not call_id.strip():
                embedded_call_id, _ = self._split_responses_tool_id(raw_id)
                call_id = embedded_call_id
            if not isinstance(call_id, str) or not call_id.strip():
                if isinstance(raw_id, str) and raw_id.strip():
                    call_id = raw_id.strip()
                else:
                    _fn = getattr(tool_call, "function", None)
                    _fn_name = getattr(_fn, "name", "") if _fn else ""
                    _fn_args = getattr(_fn, "arguments", "{}") if _fn else "{}"
                    call_id = self._deterministic_call_id(
                        _fn_name, _fn_args, len(tool_calls)
                    )
            call_id = call_id.strip()

            response_item_id = getattr(tool_call, "response_item_id", None)
            if (
                not isinstance(response_item_id, str)
                or not response_item_id.strip()
            ):
                _, embedded_response_item_id = self._split_responses_tool_id(raw_id)
                response_item_id = embedded_response_item_id

            response_item_id = self._derive_responses_function_call_id(
                call_id,
                response_item_id if isinstance(response_item_id, str) else None,
            )

            tc_dict = {
                "id": call_id,
                "call_id": call_id,
                "response_item_id": response_item_id,
                "type": tool_call.type,
                "function": {
                    "name": tool_call.function.name,
                    "arguments": tool_call.function.arguments,
                },
            }
            # Preserve extra_content (e.g. Gemini thought_signature) so it
            # is sent back on subsequent API calls.  Without this, Gemini 3
            # thinking models reject the request with a 400 error.
            extra = getattr(tool_call, "extra_content", None)
            if extra is not None:
                if hasattr(extra, "model_dump"):
                    extra = extra.model_dump()
                tc_dict["extra_content"] = extra
            tool_calls.append(tc_dict)
        msg["tool_calls"] = tool_calls

    return msg

@staticmethod
def _sanitize_tool_calls_for_strict_api(api_msg: dict) -> dict:
    """Strip Codex Responses API fields from tool_calls for strict providers.

    Providers like Mistral, Fireworks, and other strict OpenAI-compatible APIs
    validate the Chat Completions schema and reject unknown fields (call_id,
    response_item_id) with 400 or 422 errors. These fields are preserved in
    the internal message history — this method only modifies the outgoing
    API copy.

    Creates new tool_call dicts rather than mutating in-place, so the
    original messages list retains call_id/response_item_id for Codex
    Responses API compatibility (e.g. if the session falls back to a
    Codex provider later).

    Fields stripped: call_id, response_item_id
    """
    tool_calls = api_msg.get("tool_calls")
    if not isinstance(tool_calls, list):
        return api_msg
    _STRIP_KEYS = {"call_id", "response_item_id"}
    api_msg["tool_calls"] = [
        {k: v for k, v in tc.items() if k not in _STRIP_KEYS}
        if isinstance(tc, dict)
        else tc
        for tc in tool_calls
    ]
    return api_msg

def _should_sanitize_tool_calls(self) -> bool:
    """Determine if tool_calls need sanitization for strict APIs.

    Codex Responses API uses fields like call_id and response_item_id
    that are not part of the standard Chat Completions schema. These
    fields must be stripped when calling any other API to avoid
    validation errors (400 Bad Request).

    Returns:
        bool: True if sanitization is needed (non-Codex API), False otherwise.
    """
    return self.api_mode != "codex_responses"

def flush_memories(self, messages: list = None, min_turns: int = None):
    """Give the model one turn to persist memories before context is lost.

    Called before compression, session reset, or CLI exit. Injects a flush
    message, makes one API call, executes any memory tool calls, then
    strips all flush artifacts from the message list.

    Args:
        messages: The current conversation messages. If None, uses
                  self._session_messages (last run_conversation state).
        min_turns: Minimum user turns required to trigger the flush.
                   None = use config value (flush_min_turns).
                   0 = always flush (used for compression).
    """
    if self._memory_flush_min_turns == 0 and min_turns is None:
        return
    if "memory" not in self.valid_tool_names or not self._memory_store:
        return
    effective_min = (
        min_turns if min_turns is not None else self._memory_flush_min_turns
    )
    if self._user_turn_count < effective_min:
        return

    if messages is None:
        messages = getattr(self, "_session_messages", None)
    if not messages or len(messages) < 3:
        return

    flush_content = (
        "[System: The session is being compressed. "
        "Save anything worth remembering — prioritize user preferences, "
        "corrections, and recurring patterns over task-specific details.]"
    )
    _sentinel = f"__flush_{id(self)}_{time.monotonic()}"
    flush_msg = {
        "role": "user",
        "content": flush_content,
        "_flush_sentinel": _sentinel,
    }
    messages.append(flush_msg)

    try:
        # Build API messages for the flush call
        _needs_sanitize = self._should_sanitize_tool_calls()
        api_messages = []
        for msg in messages:
            api_msg = msg.copy()
            if msg.get("role") == "assistant":
                reasoning = msg.get("reasoning")
                if reasoning:
                    api_msg["reasoning_content"] = reasoning
            api_msg.pop("reasoning", None)
            api_msg.pop("finish_reason", None)
            api_msg.pop("_flush_sentinel", None)
            if _needs_sanitize:
                self._sanitize_tool_calls_for_strict_api(api_msg)
            api_messages.append(api_msg)

        if self._cached_system_prompt:
            api_messages = [
                {"role": "system", "content": self._cached_system_prompt}
            ] + api_messages

        # Make one API call with only the memory tool available
        memory_tool_def = None
        for t in self.tools or []:
            if t.get("function", {}).get("name") == "memory":
                memory_tool_def = t
                break

        if not memory_tool_def:
            messages.pop()  # remove flush msg
            return

        # Use auxiliary client for the flush call when available --
        # it's cheaper and avoids Codex Responses API incompatibility.
        from agent.auxiliary_client import call_llm as _call_llm

        _aux_available = True
        try:
            response = _call_llm(
                task="flush_memories",
                messages=api_messages,
                tools=[memory_tool_def],
                temperature=0.3,
                max_tokens=5120,
                timeout=30.0,
            )
        except RuntimeError:
            _aux_available = False
            response = None

        if not _aux_available and self.api_mode == "codex_responses":
            # No auxiliary client -- use the Codex Responses path directly
            codex_kwargs = self._build_api_kwargs(api_messages)
            codex_kwargs["tools"] = self._responses_tools([memory_tool_def])
            codex_kwargs["temperature"] = 0.3
            if "max_output_tokens" in codex_kwargs:
                codex_kwargs["max_output_tokens"] = 5120
            response = self._run_codex_stream(codex_kwargs)
        elif not _aux_available and self.api_mode == "anthropic_messages":
            # Native Anthropic — use the Anthropic client directly
            from agent.anthropic_adapter import (
                build_anthropic_kwargs as _build_ant_kwargs,
            )

            ant_kwargs = _build_ant_kwargs(
                model=self.model,
                messages=api_messages,
                tools=[memory_tool_def],
                max_tokens=5120,
                reasoning_config=None,
                preserve_dots=self._anthropic_preserve_dots(),
            )
            response = self._anthropic_messages_create(ant_kwargs)
        elif not _aux_available:
            api_kwargs = {
                "model": self.model,
                "messages": api_messages,
                "tools": [memory_tool_def],
                "temperature": 0.3,
                **self._max_tokens_param(5120),
            }
            response = self._ensure_primary_openai_client(
                reason="flush_memories"
            ).chat.completions.create(**api_kwargs, timeout=30.0)

        # Extract tool calls from the response, handling all API formats
        tool_calls = []
        if self.api_mode == "codex_responses" and not _aux_available:
            assistant_msg, _ = self._normalize_codex_response(response)
            if assistant_msg and assistant_msg.tool_calls:
                tool_calls = assistant_msg.tool_calls
        elif self.api_mode == "anthropic_messages" and not _aux_available:
            from agent.anthropic_adapter import (
                normalize_anthropic_response as _nar_flush,
            )

            _flush_msg, _ = _nar_flush(
                response, strip_tool_prefix=self._is_anthropic_oauth
            )
            if _flush_msg and _flush_msg.tool_calls:
                tool_calls = _flush_msg.tool_calls
        elif hasattr(response, "choices") and response.choices:
            assistant_message = response.choices[0].message
            if assistant_message.tool_calls:
                tool_calls = assistant_message.tool_calls

        for tc in tool_calls:
            if tc.function.name == "memory":
                try:
                    args = json.loads(tc.function.arguments)
                    flush_target = args.get("target", "memory")
                    from tools.memory_tool import memory_tool as _memory_tool

                    result = _memory_tool(
                        action=args.get("action"),
                        target=flush_target,
                        content=args.get("content"),
                        old_text=args.get("old_text"),
                        store=self._memory_store,
                    )
                    if not self.quiet_mode:
                        print(
                            f"  🧠 Memory flush: saved to {args.get('target', 'memory')}"
                        )
                except Exception as e:
                    logger.debug("Memory flush tool call failed: %s", e)
    except Exception as e:
        logger.debug("Memory flush API call failed: %s", e)
    finally:
        # Strip flush artifacts: remove everything from the flush message onward.
        # Use sentinel marker instead of identity check for robustness.
        while messages and messages[-1].get("_flush_sentinel") != _sentinel:
            messages.pop()
            if not messages:
                break
        if messages and messages[-1].get("_flush_sentinel") == _sentinel:
            messages.pop()

def _compress_context(
    self,
    messages: list,
    system_message: str,
    *,
    approx_tokens: int = None,
    task_id: str = "default",
) -> tuple:
    """Compress conversation context and split the session in SQLite.

    Returns:
        (compressed_messages, new_system_prompt) tuple
    """
    _pre_msg_count = len(messages)
    logger.info(
        "context compression started: session=%s messages=%d tokens=~%s model=%s",
        self.session_id or "none",
        _pre_msg_count,
        f"{approx_tokens:,}" if approx_tokens else "unknown",
        self.model,
    )
    # Pre-compression memory flush: let the model save memories before they're lost
    self.flush_memories(messages, min_turns=0)

    # Notify external memory provider before compression discards context
    if self._memory_manager:
        try:
            self._memory_manager.on_pre_compress(messages)
        except Exception:
            pass

    compressed = self.context_compressor.compress(
        messages, current_tokens=approx_tokens
    )

    todo_snapshot = self._todo_store.format_for_injection()
    if todo_snapshot:
        compressed.append({"role": "user", "content": todo_snapshot})

    self._invalidate_system_prompt()
    new_system_prompt = self._build_system_prompt(system_message)
    self._cached_system_prompt = new_system_prompt

    if self._session_db:
        try:
            # Propagate title to the new session with auto-numbering
            old_title = self._session_db.get_session_title(self.session_id)
            self._session_db.end_session(self.session_id, "compression")
            old_session_id = self.session_id
            self.session_id = (
                f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
            )
            # Update session_log_file to point to the new session's JSON file
            self.session_log_file = (
                self.logs_dir / f"session_{self.session_id}.json"
            )
            self._session_db.create_session(
                session_id=self.session_id,
                source=self.platform
                or os.environ.get("DREW_SESSION_SOURCE", "cli"),
                model=self.model,
                parent_session_id=old_session_id,
            )
            # Auto-number the title for the continuation session
            if old_title:
                try:
                    new_title = self._session_db.get_next_title_in_lineage(
                        old_title
                    )
                    self._session_db.set_session_title(self.session_id, new_title)
                except (ValueError, Exception) as e:
                    logger.debug("Could not propagate title on compression: %s", e)
            self._session_db.update_system_prompt(
                self.session_id, new_system_prompt
            )
            # Reset flush cursor — new session starts with no messages written
            self._last_flushed_db_idx = 0
        except Exception as e:
            logger.warning(
                "Session DB compression split failed — new session will NOT be indexed: %s",
                e,
            )

    # Update token estimate after compaction so pressure calculations
    # use the post-compression count, not the stale pre-compression one.
    _compressed_est = estimate_tokens_rough(
        new_system_prompt
    ) + estimate_messages_tokens_rough(compressed)
    self.context_compressor.last_prompt_tokens = _compressed_est
    self.context_compressor.last_completion_tokens = 0

    # Only reset the pressure warning if compression actually brought
    # us below the warning level (85% of threshold).  When compression
    # can't reduce enough (e.g. threshold is very low, or system prompt
    # alone exceeds the warning level), keep the flag set to prevent
    # spamming the user with repeated warnings every loop iteration.
    if self.context_compressor.threshold_tokens > 0:
        _post_progress = _compressed_est / self.context_compressor.threshold_tokens
        if _post_progress < 0.85:
            self._context_pressure_warned = False

    # Clear the file-read dedup cache.  After compression the original
    # read content is summarised away — if the model re-reads the same
    # file it needs the full content, not a "file unchanged" stub.
    try:
        from tools.file_tools import reset_file_dedup

        reset_file_dedup(task_id)
    except Exception:
        pass

    logger.info(
        "context compression done: session=%s messages=%d->%d tokens=~%s",
        self.session_id or "none",
        _pre_msg_count,
        len(compressed),
        f"{_compressed_est:,}",
    )
    return compressed, new_system_prompt

def _execute_tool_calls(
    self,
    assistant_message,
    messages: list,
    effective_task_id: str,
    api_call_count: int = 0,
) -> None:
    """Execute tool calls from the assistant message and append results to messages.

    Dispatches to concurrent execution only for batches that look
    independent: read-only tools may always share the parallel path, while
    file reads/writes may do so only when their target paths do not overlap.
    """
    tool_calls = assistant_message.tool_calls

    # Allow _vprint during tool execution even with stream consumers
    self._executing_tools = True
    try:
        if not _should_parallelize_tool_batch(tool_calls):
            return self._execute_tool_calls_sequential(
                assistant_message, messages, effective_task_id, api_call_count
            )

        return self._execute_tool_calls_concurrent(
            assistant_message, messages, effective_task_id, api_call_count
        )
    finally:
        self._executing_tools = False

def _invoke_tool(
    self,
    function_name: str,
    function_args: dict,
    effective_task_id: str,
    tool_call_id: Optional[str] = None,
) -> str:
    """Invoke a single tool and return the result string. No display logic.

    Handles both agent-level tools (todo, memory, etc.) and registry-dispatched
    tools. Used by the concurrent execution path; the sequential path retains
    its own inline invocation for backward-compatible display handling.
    """
    if function_name == "todo":
        from tools.todo_tool import todo_tool as _todo_tool

        return _todo_tool(
            todos=function_args.get("todos"),
            merge=function_args.get("merge", False),
            store=self._todo_store,
        )
    elif function_name == "session_search":
        if not self._session_db:
            return json.dumps(
                {"success": False, "error": "Session database not available."}
            )
        from tools.session_search_tool import session_search as _session_search

        return _session_search(
            query=function_args.get("query", ""),
            role_filter=function_args.get("role_filter"),
            limit=function_args.get("limit", 3),
            db=self._session_db,
            current_session_id=self.session_id,
        )
    elif function_name == "memory":
        target = function_args.get("target", "memory")
        from tools.memory_tool import memory_tool as _memory_tool

        result = _memory_tool(
            action=function_args.get("action"),
            target=target,
            content=function_args.get("content"),
            old_text=function_args.get("old_text"),
            store=self._memory_store,
        )
        # Bridge: notify external memory provider of built-in memory writes
        if self._memory_manager and function_args.get("action") in (
            "add",
            "replace",
        ):
            try:
                self._memory_manager.on_memory_write(
                    function_args.get("action", ""),
                    target,
                    function_args.get("content", ""),
                )
            except Exception:
                pass
        return result
    elif self._memory_manager and self._memory_manager.has_tool(function_name):
        return self._memory_manager.handle_tool_call(function_name, function_args)
    elif function_name == "clarify":
        from tools.clarify_tool import clarify_tool as _clarify_tool

        return _clarify_tool(
            question=function_args.get("question", ""),
            choices=function_args.get("choices"),
            callback=self.clarify_callback,
        )
    elif function_name == "delegate_task":
        from tools.delegate_tool import delegate_task as _delegate_task

        return _delegate_task(
            goal=function_args.get("goal"),
            context=function_args.get("context"),
            toolsets=function_args.get("toolsets"),
            tasks=function_args.get("tasks"),
            max_iterations=function_args.get("max_iterations"),
            parent_agent=self,
        )
    else:
        return handle_function_call(
            function_name,
            function_args,
            effective_task_id,
            tool_call_id=tool_call_id,
            session_id=self.session_id or "",
            enabled_tools=list(self.valid_tool_names)
            if self.valid_tool_names
            else None,
        )

def _execute_tool_calls_concurrent(
    self,
    assistant_message,
    messages: list,
    effective_task_id: str,
    api_call_count: int = 0,
) -> None:
    """Execute multiple tool calls concurrently using a thread pool.

    Results are collected in the original tool-call order and appended to
    messages so the API sees them in the expected sequence.
    """
    tool_calls = assistant_message.tool_calls
    num_tools = len(tool_calls)

    # ── Pre-flight: interrupt check ──────────────────────────────────
    if self._interrupt_requested:
        print(f"{self.log_prefix}⚡ Interrupt: skipping {num_tools} tool call(s)")
        for tc in tool_calls:
            messages.append(
                {
                    "role": "tool",
                    "content": f"[Tool execution cancelled — {tc.function.name} was skipped due to user interrupt]",
                    "tool_call_id": tc.id,
                }
            )
        return

    # ── Parse args + pre-execution bookkeeping ───────────────────────
    parsed_calls = []  # list of (tool_call, function_name, function_args)
    for tool_call in tool_calls:
        function_name = tool_call.function.name

        # Reset nudge counters
        if function_name == "memory":
            self._turns_since_memory = 0
        elif function_name == "skill_manage":
            self._iters_since_skill = 0

        try:
            function_args = json.loads(tool_call.function.arguments)
        except json.JSONDecodeError:
            function_args = {}
        if not isinstance(function_args, dict):
            function_args = {}

        # Checkpoint for file-mutating tools
        if (
            function_name in ("write_file", "patch")
            and self._checkpoint_mgr.enabled
        ):
            try:
                file_path = function_args.get("path", "")
                if file_path:
                    work_dir = self._checkpoint_mgr.get_working_dir_for_path(
                        file_path
                    )
                    self._checkpoint_mgr.ensure_checkpoint(
                        work_dir, f"before {function_name}"
                    )
            except Exception:
                pass

        # Checkpoint before destructive terminal commands
        if function_name == "terminal" and self._checkpoint_mgr.enabled:
            try:
                cmd = function_args.get("command", "")
                if _is_destructive_command(cmd):
                    cwd = function_args.get("workdir") or os.getenv(
                        "TERMINAL_CWD", os.getcwd()
                    )
                    self._checkpoint_mgr.ensure_checkpoint(
                        cwd, f"before terminal: {cmd[:60]}"
                    )
            except Exception:
                pass

        parsed_calls.append((tool_call, function_name, function_args))

    # ── Logging / callbacks ──────────────────────────────────────────
    tool_names_str = ", ".join(name for _, name, _ in parsed_calls)
    if not self.quiet_mode:
        print(f"  ⚡ Concurrent: {num_tools} tool calls — {tool_names_str}")
        for i, (tc, name, args) in enumerate(parsed_calls, 1):
            args_str = json.dumps(args, ensure_ascii=False)
            if self.verbose_logging:
                print(f"  📞 Tool {i}: {name}({list(args.keys())})")
                print(f"     Args: {args_str}")
            else:
                args_preview = (
                    args_str[: self.log_prefix_chars] + "..."
                    if len(args_str) > self.log_prefix_chars
                    else args_str
                )
                print(
                    f"  📞 Tool {i}: {name}({list(args.keys())}) - {args_preview}"
                )

    for tc, name, args in parsed_calls:
        if self.tool_progress_callback:
            try:
                preview = _build_tool_preview(name, args)
                self.tool_progress_callback("tool.started", name, preview, args)
            except Exception as cb_err:
                logging.debug(f"Tool progress callback error: {cb_err}")

    for tc, name, args in parsed_calls:
        if self.tool_start_callback:
            try:
                self.tool_start_callback(tc.id, name, args)
            except Exception as cb_err:
                logging.debug(f"Tool start callback error: {cb_err}")

    # Brain signal: emit tool_start for integration workflow tracking
    # Only for file-modifying tools that indicate integration intent
    for tc, name, args in parsed_calls:
        try:
            emitter = get_signal_emitter()
            emitter.tool_start(name, args)
        except Exception:
            pass  # Brain signals are best-effort

    # ── Concurrent execution ─────────────────────────────────────────
    # Each slot holds (function_name, function_args, function_result, duration, error_flag)
    results = [None] * num_tools

    def _run_tool(index, tool_call, function_name, function_args):
        """Worker function executed in a thread."""
        start = time.time()
        try:
            result = self._invoke_tool(
                function_name, function_args, effective_task_id, tool_call.id
            )
        except Exception as tool_error:
            result = f"Error executing tool '{function_name}': {tool_error}"
            logger.error(
                "_invoke_tool raised for %s: %s",
                function_name,
                tool_error,
                exc_info=True,
            )
        duration = time.time() - start
        is_error, _ = _detect_tool_failure(function_name, result)
        if is_error:
            logger.info(
                "tool %s failed (%.2fs): %s", function_name, duration, result[:200]
            )
        else:
            logger.info(
                "tool %s completed (%.2fs, %d chars)",
                function_name,
                duration,
                len(result),
            )
        results[index] = (function_name, function_args, result, duration, is_error)

    # Start spinner for CLI mode (skip when TUI handles tool progress)
    spinner = None
    if (
        self.quiet_mode
        and not self.tool_progress_callback
        and self._should_start_quiet_spinner()
    ):
        face = random.choice(KawaiiSpinner.KAWAII_WAITING)
        spinner = KawaiiSpinner(
            f"{face} ⚡ running {num_tools} tools concurrently",
            spinner_type="dots",
            print_fn=self._print_fn,
        )
        spinner.start()

    try:
        max_workers = min(num_tools, _MAX_TOOL_WORKERS)
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers
        ) as executor:
            futures = []
            for i, (tc, name, args) in enumerate(parsed_calls):
                f = executor.submit(_run_tool, i, tc, name, args)
                futures.append(f)

            # Wait for all to complete (exceptions are captured inside _run_tool)
            concurrent.futures.wait(futures)
    finally:
        if spinner:
            # Build a summary message for the spinner stop
            completed = sum(1 for r in results if r is not None)
            total_dur = sum(r[3] for r in results if r is not None)
            spinner.stop(
                f"⚡ {completed}/{num_tools} tools completed in {total_dur:.1f}s total"
            )

    # ── Post-execution: display per-tool results ─────────────────────
    for i, (tc, name, args) in enumerate(parsed_calls):
        r = results[i]
        if r is None:
            # Shouldn't happen, but safety fallback
            function_result = (
                f"Error executing tool '{name}': thread did not return a result"
            )
            tool_duration = 0.0
        else:
            (
                function_name,
                function_args,
                function_result,
                tool_duration,
                is_error,
            ) = r

            if is_error:
                result_preview = (
                    function_result[:200]
                    if len(function_result) > 200
                    else function_result
                )
                logger.warning(
                    "Tool %s returned error (%.2fs): %s",
                    function_name,
                    tool_duration,
                    result_preview,
                )

            if self.tool_progress_callback:
                try:
                    self.tool_progress_callback(
                        "tool.completed",
                        function_name,
                        None,
                        None,
                        duration=tool_duration,
                        is_error=is_error,
                    )
                except Exception as cb_err:
                    logging.debug(f"Tool progress callback error: {cb_err}")

            if self.verbose_logging:
                logging.debug(
                    f"Tool {function_name} completed in {tool_duration:.2f}s"
                )
                logging.debug(
                    f"Tool result ({len(function_result)} chars): {function_result}"
                )

        # Print cute message per tool
        if self.quiet_mode:
            cute_msg = _get_cute_tool_message_impl(
                name, args, tool_duration, result=function_result
            )
            self._safe_print(f"  {cute_msg}")
        elif not self.quiet_mode:
            if self.verbose_logging:
                print(f"  ✅ Tool {i + 1} completed in {tool_duration:.2f}s")
                print(f"     Result: {function_result}")
            else:
                response_preview = (
                    function_result[: self.log_prefix_chars] + "..."
                    if len(function_result) > self.log_prefix_chars
                    else function_result
                )
                print(
                    f"  ✅ Tool {i + 1} completed in {tool_duration:.2f}s - {response_preview}"
                )

        self._current_tool = None
        self._touch_activity(f"tool completed: {name} ({tool_duration:.1f}s)")

        if self.tool_complete_callback:
            try:
                self.tool_complete_callback(tc.id, name, args, function_result)
            except Exception as cb_err:
                logging.debug(f"Tool complete callback error: {cb_err}")

        # Brain signal: emit tool_complete + agent_modifying for integration tracking
        try:
            emitter = get_signal_emitter()
            # tool_complete signature: (tool_name, result, success=True)
            _success = not ("error" in function_result.lower() and "success" not in function_result.lower())
            emitter.tool_complete(name, function_result, _success)
            # Check for file-modifying results and emit agent_modifying
            _file_path = _extract_file_path_from_result(function_result, name)
            if _file_path:
                # agent_modifying signature: (operation, path, details="")
                emitter.agent_modifying(
                    f"tool:{name}",
                    _file_path,
                    f"tool:{name} completed, file modified",
                )
        except Exception:
            pass  # Brain signals are best-effort

        # Save oversized results to file instead of destructive truncation
        function_result = _save_oversized_tool_result(name, function_result)

        # Discover subdirectory context files from tool arguments
        subdir_hints = self._subdirectory_hints.check_tool_call(name, args)
        if subdir_hints:
            function_result += subdir_hints

        # Append tool result message in order
        tool_msg = {
            "role": "tool",
            "content": function_result,
            "tool_call_id": tc.id,
        }
        messages.append(tool_msg)

    # ── Budget pressure injection ────────────────────────────────────
    budget_warning = self._get_budget_warning(api_call_count)
    if budget_warning and messages and messages[-1].get("role") == "tool":
        last_content = messages[-1]["content"]
        try:
            parsed = json.loads(last_content)
            if isinstance(parsed, dict):
                parsed["_budget_warning"] = budget_warning
                messages[-1]["content"] = json.dumps(parsed, ensure_ascii=False)
            else:
                messages[-1]["content"] = last_content + f"\n\n{budget_warning}"
        except (json.JSONDecodeError, TypeError):
            messages[-1]["content"] = last_content + f"\n\n{budget_warning}"
        if not self.quiet_mode:
            remaining = self.max_iterations - api_call_count
            tier = (
                "⚠️  WARNING"
                if remaining <= self.max_iterations * 0.1
                else "💡 CAUTION"
            )
            print(f"{self.log_prefix}{tier}: {remaining} iterations remaining")

def _execute_tool_calls_sequential(
    self,
    assistant_message,
    messages: list,
    effective_task_id: str,
    api_call_count: int = 0,
) -> None:
    """Execute tool calls sequentially (original behavior). Used for single calls or interactive tools."""
    for i, tool_call in enumerate(assistant_message.tool_calls, 1):
        # SAFETY: check interrupt BEFORE starting each tool.
        # If the user sent "stop" during a previous tool's execution,
        # do NOT start any more tools -- skip them all immediately.
        if self._interrupt_requested:
            remaining_calls = assistant_message.tool_calls[i - 1 :]
            if remaining_calls:
                self._vprint(
                    f"{self.log_prefix}⚡ Interrupt: skipping {len(remaining_calls)} tool call(s)",
                    force=True,
                )
            for skipped_tc in remaining_calls:
                skipped_name = skipped_tc.function.name
                skip_msg = {
                    "role": "tool",
                    "content": f"[Tool execution cancelled — {skipped_name} was skipped due to user interrupt]",
                    "tool_call_id": skipped_tc.id,
                }
                messages.append(skip_msg)
            break

        function_name = tool_call.function.name

        # Reset nudge counters when the relevant tool is actually used
        if function_name == "memory":
            self._turns_since_memory = 0
        elif function_name == "skill_manage":
            self._iters_since_skill = 0

        try:
            function_args = json.loads(tool_call.function.arguments)
        except json.JSONDecodeError as e:
            logging.warning(f"Unexpected JSON error after validation: {e}")
            function_args = {}
        if not isinstance(function_args, dict):
            function_args = {}

        if not self.quiet_mode:
            args_str = json.dumps(function_args, ensure_ascii=False)
            if self.verbose_logging:
                print(
                    f"  📞 Tool {i}: {function_name}({list(function_args.keys())})"
                )
                print(f"     Args: {args_str}")
            else:
                args_preview = (
                    args_str[: self.log_prefix_chars] + "..."
                    if len(args_str) > self.log_prefix_chars
                    else args_str
                )
                print(
                    f"  📞 Tool {i}: {function_name}({list(function_args.keys())}) - {args_preview}"
                )

        self._current_tool = function_name
        self._touch_activity(f"executing tool: {function_name}")

        if self.tool_progress_callback:
            try:
                preview = _build_tool_preview(function_name, function_args)
                self.tool_progress_callback(
                    "tool.started", function_name, preview, function_args
                )
            except Exception as cb_err:
                logging.debug(f"Tool progress callback error: {cb_err}")

        if self.tool_start_callback:
            try:
                self.tool_start_callback(tool_call.id, function_name, function_args)
            except Exception as cb_err:
                logging.debug(f"Tool start callback error: {cb_err}")

        # Checkpoint: snapshot working dir before file-mutating tools
        if (
            function_name in ("write_file", "patch")
            and self._checkpoint_mgr.enabled
        ):
            try:
                file_path = function_args.get("path", "")
                if file_path:
                    work_dir = self._checkpoint_mgr.get_working_dir_for_path(
                        file_path
                    )
                    self._checkpoint_mgr.ensure_checkpoint(
                        work_dir, f"before {function_name}"
                    )
            except Exception:
                pass  # never block tool execution

        # Checkpoint before destructive terminal commands
        if function_name == "terminal" and self._checkpoint_mgr.enabled:
            try:
                cmd = function_args.get("command", "")
                if _is_destructive_command(cmd):
                    cwd = function_args.get("workdir") or os.getenv(
                        "TERMINAL_CWD", os.getcwd()
                    )
                    self._checkpoint_mgr.ensure_checkpoint(
                        cwd, f"before terminal: {cmd[:60]}"
                    )
            except Exception:
                pass  # never block tool execution

        tool_start_time = time.time()

        if function_name == "todo":
            from tools.todo_tool import todo_tool as _todo_tool

            function_result = _todo_tool(
                todos=function_args.get("todos"),
                merge=function_args.get("merge", False),
                store=self._todo_store,
            )
            tool_duration = time.time() - tool_start_time
            if self.quiet_mode:
                self._vprint(
                    f"  {_get_cute_tool_message_impl('todo', function_args, tool_duration, result=function_result)}"
                )
        elif function_name == "session_search":
            if not self._session_db:
                function_result = json.dumps(
                    {"success": False, "error": "Session database not available."}
                )
            else:
                from tools.session_search_tool import (
                    session_search as _session_search,
                )

                function_result = _session_search(
                    query=function_args.get("query", ""),
                    role_filter=function_args.get("role_filter"),
                    limit=function_args.get("limit", 3),
                    db=self._session_db,
                    current_session_id=self.session_id,
                )
            tool_duration = time.time() - tool_start_time
            if self.quiet_mode:
                self._vprint(
                    f"  {_get_cute_tool_message_impl('session_search', function_args, tool_duration, result=function_result)}"
                )
        elif function_name == "memory":
            target = function_args.get("target", "memory")
            from tools.memory_tool import memory_tool as _memory_tool

            function_result = _memory_tool(
                action=function_args.get("action"),
                target=target,
                content=function_args.get("content"),
                old_text=function_args.get("old_text"),
                store=self._memory_store,
            )
            tool_duration = time.time() - tool_start_time
            if self.quiet_mode:
                self._vprint(
                    f"  {_get_cute_tool_message_impl('memory', function_args, tool_duration, result=function_result)}"
                )
        elif function_name == "clarify":
            from tools.clarify_tool import clarify_tool as _clarify_tool

            function_result = _clarify_tool(
                question=function_args.get("question", ""),
                choices=function_args.get("choices"),
                callback=self.clarify_callback,
            )
            tool_duration = time.time() - tool_start_time
            if self.quiet_mode:
                self._vprint(
                    f"  {_get_cute_tool_message_impl('clarify', function_args, tool_duration, result=function_result)}"
                )
        elif function_name == "delegate_task":
            from tools.delegate_tool import delegate_task as _delegate_task

            tasks_arg = function_args.get("tasks")
            if tasks_arg and isinstance(tasks_arg, list):
                spinner_label = f"🔀 delegating {len(tasks_arg)} tasks"
            else:
                goal_preview = (function_args.get("goal") or "")[:30]
                spinner_label = (
                    f"🔀 {goal_preview}" if goal_preview else "🔀 delegating"
                )
            spinner = None
            if (
                self.quiet_mode
                and not self.tool_progress_callback
                and self._should_start_quiet_spinner()
            ):
                face = random.choice(KawaiiSpinner.KAWAII_WAITING)
                spinner = KawaiiSpinner(
                    f"{face} {spinner_label}",
                    spinner_type="dots",
                    print_fn=self._print_fn,
                )
                spinner.start()
            self._delegate_spinner = spinner
            _delegate_result = None
            try:
                function_result = _delegate_task(
                    goal=function_args.get("goal"),
                    context=function_args.get("context"),
                    toolsets=function_args.get("toolsets"),
                    tasks=tasks_arg,
                    max_iterations=function_args.get("max_iterations"),
                    parent_agent=self,
                )
                _delegate_result = function_result
            finally:
                self._delegate_spinner = None
                tool_duration = time.time() - tool_start_time
                cute_msg = _get_cute_tool_message_impl(
                    "delegate_task",
                    function_args,
                    tool_duration,
                    result=_delegate_result,
                )
                if spinner:
                    spinner.stop(cute_msg)
                elif self.quiet_mode:
                    self._vprint(f"  {cute_msg}")
        elif self._memory_manager and self._memory_manager.has_tool(function_name):
            # Memory provider tools (hindsight_retain, honcho_search, etc.)
            # These are not in the tool registry — route through MemoryManager.
            spinner = None
            if self.quiet_mode and not self.tool_progress_callback:
                face = random.choice(KawaiiSpinner.KAWAII_WAITING)
                emoji = _get_tool_emoji(function_name)
                preview = (
                    _build_tool_preview(function_name, function_args)
                    or function_name
                )
                spinner = KawaiiSpinner(
                    f"{face} {emoji} {preview}",
                    spinner_type="dots",
                    print_fn=self._print_fn,
                )
                spinner.start()
            _mem_result = None
            try:
                function_result = self._memory_manager.handle_tool_call(
                    function_name, function_args
                )
                _mem_result = function_result
            except Exception as tool_error:
                function_result = json.dumps(
                    {"error": f"Memory tool '{function_name}' failed: {tool_error}"}
                )
                logger.error(
                    "memory_manager.handle_tool_call raised for %s: %s",
                    function_name,
                    tool_error,
                    exc_info=True,
                )
            finally:
                tool_duration = time.time() - tool_start_time
                cute_msg = _get_cute_tool_message_impl(
                    function_name, function_args, tool_duration, result=_mem_result
                )
                if spinner:
                    spinner.stop(cute_msg)
                elif self.quiet_mode:
                    self._vprint(f"  {cute_msg}")
        elif self.quiet_mode:
            spinner = None
            if not self.tool_progress_callback:
                face = random.choice(KawaiiSpinner.KAWAII_WAITING)
                emoji = _get_tool_emoji(function_name)
                preview = (
                    _build_tool_preview(function_name, function_args)
                    or function_name
                )
                spinner = KawaiiSpinner(
                    f"{face} {emoji} {preview}",
                    spinner_type="dots",
                    print_fn=self._print_fn,
                )
                spinner.start()
            _spinner_result = None
            try:
                function_result = handle_function_call(
                    function_name,
                    function_args,
                    effective_task_id,
                    tool_call_id=tool_call.id,
                    session_id=self.session_id or "",
                    enabled_tools=list(self.valid_tool_names)
                    if self.valid_tool_names
                    else None,
                )
                _spinner_result = function_result
            except Exception as tool_error:
                function_result = (
                    f"Error executing tool '{function_name}': {tool_error}"
                )
                logger.error(
                    "handle_function_call raised for %s: %s",
                    function_name,
                    tool_error,
                    exc_info=True,
                )
            finally:
                tool_duration = time.time() - tool_start_time
                cute_msg = _get_cute_tool_message_impl(
                    function_name,
                    function_args,
                    tool_duration,
                    result=_spinner_result,
                )
                if spinner:
                    spinner.stop(cute_msg)
                else:
                    self._vprint(f"  {cute_msg}")
        else:
            try:
                function_result = handle_function_call(
                    function_name,
                    function_args,
                    effective_task_id,
                    tool_call_id=tool_call.id,
                    session_id=self.session_id or "",
                    enabled_tools=list(self.valid_tool_names)
                    if self.valid_tool_names
                    else None,
                )
            except Exception as tool_error:
                function_result = (
                    f"Error executing tool '{function_name}': {tool_error}"
                )
                logger.error(
                    "handle_function_call raised for %s: %s",
                    function_name,
                    tool_error,
                    exc_info=True,
                )
            tool_duration = time.time() - tool_start_time

        result_preview = (
            function_result
            if self.verbose_logging
            else (
                function_result[:200]
                if len(function_result) > 200
                else function_result
            )
        )

        # Log tool errors to the persistent error log so [error] tags
        # in the UI always have a corresponding detailed entry on disk.
        _is_error_result, _ = _detect_tool_failure(function_name, function_result)
        if _is_error_result:
            logger.warning(
                "Tool %s returned error (%.2fs): %s",
                function_name,
                tool_duration,
                result_preview,
            )
        else:
            logger.info(
                "tool %s completed (%.2fs, %d chars)",
                function_name,
                tool_duration,
                len(function_result),
            )

        if self.tool_progress_callback:
            try:
                self.tool_progress_callback(
                    "tool.completed",
                    function_name,
                    None,
                    None,
                    duration=tool_duration,
                    is_error=_is_error_result,
                )
            except Exception as cb_err:
                logging.debug(f"Tool progress callback error: {cb_err}")

        self._current_tool = None
        self._touch_activity(
            f"tool completed: {function_name} ({tool_duration:.1f}s)"
        )

        if self.verbose_logging:
            logging.debug(f"Tool {function_name} completed in {tool_duration:.2f}s")
            logging.debug(
                f"Tool result ({len(function_result)} chars): {function_result}"
            )

        if self.tool_complete_callback:
            try:
                self.tool_complete_callback(
                    tool_call.id, function_name, function_args, function_result
                )
            except Exception as cb_err:
                logging.debug(f"Tool complete callback error: {cb_err}")

        # Save oversized results to file instead of destructive truncation
        function_result = _save_oversized_tool_result(
            function_name, function_result
        )

        # Discover subdirectory context files from tool arguments
        subdir_hints = self._subdirectory_hints.check_tool_call(
            function_name, function_args
        )
        if subdir_hints:
            function_result += subdir_hints

        tool_msg = {
            "role": "tool",
            "content": function_result,
            "tool_call_id": tool_call.id,
        }
        messages.append(tool_msg)

        if not self.quiet_mode:
            if self.verbose_logging:
                print(f"  ✅ Tool {i} completed in {tool_duration:.2f}s")
                print(f"     Result: {function_result}")
            else:
                response_preview = (
                    function_result[: self.log_prefix_chars] + "..."
                    if len(function_result) > self.log_prefix_chars
                    else function_result
                )
                print(
                    f"  ✅ Tool {i} completed in {tool_duration:.2f}s - {response_preview}"
                )

        if self._interrupt_requested and i < len(assistant_message.tool_calls):
            remaining = len(assistant_message.tool_calls) - i
            self._vprint(
                f"{self.log_prefix}⚡ Interrupt: skipping {remaining} remaining tool call(s)",
                force=True,
            )
            for skipped_tc in assistant_message.tool_calls[i:]:
                skipped_name = skipped_tc.function.name
                skip_msg = {
                    "role": "tool",
                    "content": f"[Tool execution skipped — {skipped_name} was not started. User sent a new message]",
                    "tool_call_id": skipped_tc.id,
                }
                messages.append(skip_msg)
            break

        if self.tool_delay > 0 and i < len(assistant_message.tool_calls):
            time.sleep(self.tool_delay)

    # ── Budget pressure injection ─────────────────────────────────
    # After all tool calls in this turn are processed, check if we're
    # approaching max_iterations. If so, inject a warning into the LAST
    # tool result's JSON so the LLM sees it naturally when reading results.
    budget_warning = self._get_budget_warning(api_call_count)
    if budget_warning and messages and messages[-1].get("role") == "tool":
        last_content = messages[-1]["content"]
        try:
            parsed = json.loads(last_content)
            if isinstance(parsed, dict):
                parsed["_budget_warning"] = budget_warning
                messages[-1]["content"] = json.dumps(parsed, ensure_ascii=False)
            else:
                messages[-1]["content"] = last_content + f"\n\n{budget_warning}"
        except (json.JSONDecodeError, TypeError):
            messages[-1]["content"] = last_content + f"\n\n{budget_warning}"
        if not self.quiet_mode:
            remaining = self.max_iterations - api_call_count
            tier = (
                "⚠️  WARNING"
                if remaining <= self.max_iterations * 0.1
                else "💡 CAUTION"
            )
            print(f"{self.log_prefix}{tier}: {remaining} iterations remaining")

    # ── Integration workflow hint injection ───────────────────
    # After all tool calls, check for active integration workflows
    # and inject the next-step hint into the last tool result.
    # This gives the agent real-time guidance during tool/skill integration.
    integration_hint = self._get_integration_hint()
    if integration_hint and messages and messages[-1].get("role") == "tool":
        last_content = messages[-1]["content"]
        try:
            parsed = json.loads(last_content)
            if isinstance(parsed, dict):
                parsed["_integration_hint"] = integration_hint
                messages[-1]["content"] = json.dumps(parsed, ensure_ascii=False)
            else:
                messages[-1]["content"] = last_content + f"\n\n{integration_hint}"
        except (json.JSONDecodeError, TypeError):
            messages[-1]["content"] = last_content + f"\n\n{integration_hint}"

def _get_budget_warning(self, api_call_count: int) -> Optional[str]:
    """Return a budget pressure string, or None if not yet needed.

    Two-tier system:
      - Caution (70%): nudge to consolidate work
      - Warning (90%): urgent, must respond now
    """
    if not self._budget_pressure_enabled or self.max_iterations <= 0:
        return None
    progress = api_call_count / self.max_iterations
    remaining = self.max_iterations - api_call_count
    if progress >= self._budget_warning_threshold:
        return (
            f"[BUDGET WARNING: Iteration {api_call_count}/{self.max_iterations}. "
            f"Only {remaining} iteration(s) left. "
            "Provide your final response NOW. No more tool calls unless absolutely critical.]"
        )
    if progress >= self._budget_caution_threshold:
        return (
            f"[BUDGET: Iteration {api_call_count}/{self.max_iterations}. "
            f"{remaining} iterations left. Start consolidating your work.]"
        )
    return None

def _get_integration_hint(self) -> Optional[str]:
    """Return integration workflow hint if active workflows exist.

    Checks signal_processor for active tool/skill integration workflows
    and returns the next-step hint from ArchitectureModel.
    """
    try:
        from agent.signal_processor import get_signal_processor
        sp = get_signal_processor()
        if sp is None:
            return None

        active_workflows = sp.get_active_workflows()
        if not active_workflows:
            return None

        arch = sp.get_architecture_model()
        if arch is None:
            return None

        for wf in active_workflows:
            if wf.completed:
                continue
            if wf.integration_type == "tool":
                progress = arch.detect_tool_integration_progress(wf.files_modified)
                hint = progress.get("next_hint")
                if hint:
                    return f"[INTEGRATION HINT: {wf.target_name} — {hint}]"
            elif wf.integration_type == "skill":
                progress = arch.detect_skill_integration_progress(wf.files_modified)
                hint = progress.get("next_hint")
                if hint:
                    return f"[INTEGRATION HINT: skill/{wf.target_name} — {hint}]"
        return None
    except Exception:
        return None

def _emit_context_pressure(self, compaction_progress: float, compressor) -> None:
    """Notify the user that context is approaching the compaction threshold.

    Args:
        compaction_progress: How close to compaction (0.0–1.0, where 1.0 = fires).
        compressor: The ContextCompressor instance (for threshold/context info).

    Purely user-facing — does NOT modify the message stream.
    For CLI: prints a formatted line with a progress bar.
    For gateway: fires status_callback so the platform can send a chat message.
    """
    from agent.display import (
        format_context_pressure,
        format_context_pressure_gateway,
    )

    threshold_pct = (
        compressor.threshold_tokens / compressor.context_length
        if compressor.context_length
        else 0.5
    )

    # CLI output — always shown (these are user-facing status notifications,
    # not verbose debug output, so they bypass quiet_mode).
    # Gateway users also get the callback below.
    if self.platform in (None, "cli"):
        line = format_context_pressure(
            compaction_progress=compaction_progress,
            threshold_tokens=compressor.threshold_tokens,
            threshold_percent=threshold_pct,
            compression_enabled=self.compression_enabled,
        )
        self._safe_print(line)

    # Gateway / external consumers
    if self.status_callback:
        try:
            msg = format_context_pressure_gateway(
                compaction_progress=compaction_progress,
                threshold_percent=threshold_pct,
                compression_enabled=self.compression_enabled,
            )
            self.status_callback("context_pressure", msg)
        except Exception:
            logger.debug("status_callback error in context pressure", exc_info=True)

def _handle_max_iterations(self, messages: list, api_call_count: int) -> str:
    """Request a summary when max iterations are reached. Returns the final response text."""
    print(
        f"⚠️  Reached maximum iterations ({self.max_iterations}). Requesting summary..."
    )

    summary_request = (
        "You've reached the maximum number of tool-calling iterations allowed. "
        "Please provide a final response summarizing what you've found and accomplished so far, "
        "without calling any more tools."
    )
    messages.append({"role": "user", "content": summary_request})

    try:
        # Build API messages, stripping internal-only fields
        # (finish_reason, reasoning) that strict APIs like Mistral reject with 422
        _needs_sanitize = self._should_sanitize_tool_calls()
        api_messages = []
        for msg in messages:
            api_msg = msg.copy()
            for internal_field in ("reasoning", "finish_reason"):
                api_msg.pop(internal_field, None)
            if _needs_sanitize:
                self._sanitize_tool_calls_for_strict_api(api_msg)
            api_messages.append(api_msg)

        effective_system = self._cached_system_prompt or ""
        if self.ephemeral_system_prompt:
            effective_system = (
                effective_system + "\n\n" + self.ephemeral_system_prompt
            ).strip()
        if effective_system:
            api_messages = [
                {"role": "system", "content": effective_system}
            ] + api_messages
        if self.prefill_messages:
            sys_offset = 1 if effective_system else 0
            for idx, pfm in enumerate(self.prefill_messages):
                api_messages.insert(sys_offset + idx, pfm.copy())

        summary_extra_body = {}
        _is_nous = "nousresearch" in self._base_url_lower
        if self._supports_reasoning_extra_body():
            if self.reasoning_config is not None:
                summary_extra_body["reasoning"] = self.reasoning_config
            else:
                summary_extra_body["reasoning"] = {
                    "enabled": True,
                    "effort": "medium",
                }
        if _is_nous:
            summary_extra_body["tags"] = ["product=drewgent-agent"]

        if self.api_mode == "codex_responses":
            codex_kwargs = self._build_api_kwargs(api_messages)
            codex_kwargs.pop("tools", None)
            summary_response = self._run_codex_stream(codex_kwargs)
            assistant_message, _ = self._normalize_codex_response(summary_response)
            final_response = (
                (assistant_message.content or "").strip()
                if assistant_message
                else ""
            )
        else:
            summary_kwargs = {
                "model": self.model,
                "messages": api_messages,
            }
            if self.max_tokens is not None:
                summary_kwargs.update(self._max_tokens_param(self.max_tokens))

            # Include provider routing preferences
            provider_preferences = {}
            if self.providers_allowed:
                provider_preferences["only"] = self.providers_allowed
            if self.providers_ignored:
                provider_preferences["ignore"] = self.providers_ignored
            if self.providers_order:
                provider_preferences["order"] = self.providers_order
            if self.provider_sort:
                provider_preferences["sort"] = self.provider_sort
            if provider_preferences:
                summary_extra_body["provider"] = provider_preferences

            if summary_extra_body:
                summary_kwargs["extra_body"] = summary_extra_body

            if self.api_mode == "anthropic_messages":
                from agent.anthropic_adapter import (
                    build_anthropic_kwargs as _bak,
                    normalize_anthropic_response as _nar,
                )

                _ant_kw = _bak(
                    model=self.model,
                    messages=api_messages,
                    tools=None,
                    max_tokens=self.max_tokens,
                    reasoning_config=self.reasoning_config,
                    is_oauth=self._is_anthropic_oauth,
                    preserve_dots=self._anthropic_preserve_dots(),
                )
                summary_response = self._anthropic_messages_create(_ant_kw)
                _msg, _ = _nar(
                    summary_response, strip_tool_prefix=self._is_anthropic_oauth
                )
                final_response = (_msg.content or "").strip()
            else:
                summary_response = self._ensure_primary_openai_client(
                    reason="iteration_limit_summary"
                ).chat.completions.create(**summary_kwargs)

                if (
                    summary_response.choices
                    and summary_response.choices[0].message.content
                ):
                    final_response = summary_response.choices[0].message.content
                else:
                    final_response = ""

        if final_response:
            if "<think>" in final_response:
                final_response = re.sub(
                    r"<think>.*?</think>\s*", "", final_response, flags=re.DOTALL
                ).strip()
            if final_response:
                messages.append({"role": "assistant", "content": final_response})
            else:
                final_response = (
                    "I reached the iteration limit and couldn't generate a summary."
                )
        else:
            # Retry summary generation
            if self.api_mode == "codex_responses":
                codex_kwargs = self._build_api_kwargs(api_messages)
                codex_kwargs.pop("tools", None)
                retry_response = self._run_codex_stream(codex_kwargs)
                retry_msg, _ = self._normalize_codex_response(retry_response)
                final_response = (
                    (retry_msg.content or "").strip() if retry_msg else ""
                )
            elif self.api_mode == "anthropic_messages":
                from agent.anthropic_adapter import (
                    build_anthropic_kwargs as _bak2,
                    normalize_anthropic_response as _nar2,
                )

                _ant_kw2 = _bak2(
                    model=self.model,
                    messages=api_messages,
                    tools=None,
                    is_oauth=self._is_anthropic_oauth,
                    max_tokens=self.max_tokens,
                    reasoning_config=self.reasoning_config,
                    preserve_dots=self._anthropic_preserve_dots(),
                )
                retry_response = self._anthropic_messages_create(_ant_kw2)
                _retry_msg, _ = _nar2(
                    retry_response, strip_tool_prefix=self._is_anthropic_oauth
                )
                final_response = (_retry_msg.content or "").strip()
            else:
                summary_kwargs = {
                    "model": self.model,
                    "messages": api_messages,
                }
                if self.max_tokens is not None:
                    summary_kwargs.update(self._max_tokens_param(self.max_tokens))
                if summary_extra_body:
                    summary_kwargs["extra_body"] = summary_extra_body

                summary_response = self._ensure_primary_openai_client(
                    reason="iteration_limit_summary_retry"
                ).chat.completions.create(**summary_kwargs)

                if (
                    summary_response.choices
                    and summary_response.choices[0].message.content
                ):
                    final_response = summary_response.choices[0].message.content
                else:
                    final_response = ""

            if final_response:
                if "<think>" in final_response:
                    final_response = re.sub(
                        r"<think>.*?</think>\s*",
                        "",
                        final_response,
                        flags=re.DOTALL,
                    ).strip()
                if final_response:
                    messages.append(
                        {"role": "assistant", "content": final_response}
                    )
                else:
                    final_response = "I reached the iteration limit and couldn't generate a summary."
            else:
                final_response = (
                    "I reached the iteration limit and couldn't generate a summary."
                )

    except Exception as e:
        logging.warning(f"Failed to get summary response: {e}")
        final_response = f"I reached the maximum iterations ({self.max_iterations}) but couldn't summarize. Error: {str(e)}"

    return final_response


