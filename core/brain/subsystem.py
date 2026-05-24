"""
Drewgent Brain — Python implementation of the 7-layer subsumption architecture.

This module provides the core brain subsystem that enforces Drewgent's
governance rules and manages the P-layer hierarchy.

Layer hierarchy (bottom = highest priority):
    P0-BRAINSTEM  → CRITICAL: never-do rules (survival)
    P1-LIMBIC     → values: tone, persona, communication
    P2-HIPPOCAMPUS → memory: context persistence, wiki
    P3-SENSORS    → input: tool/skill routing, triggers
    P4-CORTEX     → growth: learning, pattern recognition
    P5-EGO        → identity: self-model, integration decisions
    P6-PREFRONTAL  → strategy: long-term planning

Each layer overrides lower layers when there's a conflict.
P0 (brainstem) always wins — it cannot be overridden by any higher layer.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Optional

# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────


class Layer(Enum):
    """Drewgent's 7-layer brain hierarchy."""

    P0_BRAINSTEM = auto()    # CRITICAL: absolute prohibitions
    P1_LIMBIC = auto()       # values: tone, persona
    P2_HIPPOCAMPUS = auto()  # memory: context, wiki
    P3_SENSORS = auto()      # input: tools, skills
    P4_CORTEX = auto()       # growth: learning
    P5_EGO = auto()          # identity: self-model
    P6_PREFRONTAL = auto()   # strategy: planning


LAYER_ORDER = [
    Layer.P0_BRAINSTEM,
    Layer.P1_LIMBIC,
    Layer.P2_HIPPOCAMPUS,
    Layer.P3_SENSORS,
    Layer.P4_CORTEX,
    Layer.P5_EGO,
    Layer.P6_PREFRONTAL,
]

LAYER_NAMES = {
    Layer.P0_BRAINSTEM: "P0-brainstem",
    Layer.P1_LIMBIC: "P1-limbic",
    Layer.P2_HIPPOCAMPUS: "P2-hippocampus",
    Layer.P3_SENSORS: "P3-sensors",
    Layer.P4_CORTEX: "P4-cortex",
    Layer.P5_EGO: "P5-ego",
    Layer.P6_PREFRONTAL: "P6-prefrontal",
}


@dataclass
class Violation:
    """Represents a brain rule violation."""

    rule_token: str
    severity: str  # CRITICAL, HIGH, MEDIUM, LOW
    message: str
    action: str
    context: dict[str, Any]
    layer: Layer

    def __str__(self) -> str:
        return f"[{self.severity}] {self.rule_token}: {self.message}"


@dataclass
class EnforcementResult:
    """Result of a brain enforcement check."""

    allowed: bool
    violation: Optional[Violation] = None
    layer: Optional[Layer] = None
    reason: Optional[str] = None

    @classmethod
    def ok(cls) -> EnforcementResult:
        return cls(allowed=True)

    @classmethod
    def blocked(cls, violation: Violation) -> EnforcementResult:
        return cls(allowed=False, violation=violation, layer=violation.layer)


# ─────────────────────────────────────────────────────────────────────────────
# Brain Subsystem
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _LayerState:
    """Internal state for each brain layer."""

    loaded: bool = False
    content: str = ""
    last_updated: Optional[str] = None


class DrewgentBrain:
    """
    Drewgent's 7-layer subsumption brain — Python implementation.

    This class coordinates the brain's layers, enforces P0 rules,
    and provides layer-specific context to the agent.

    Usage:
        brain = DrewgentBrain(vault_path="~/.drewgent")
        result = brain.enforce(Layer.P3_SENSORS, {"action": "rm -rf /", "tool": "terminal"})
        if not result.allowed:
            raise ViolationError(result.violation)
    """

    def __init__(self, vault_path: str | Path) -> None:
        self._vault = Path(vault_path).expanduser()
        self._layers: dict[Layer, _LayerState] = {
            layer: _LayerState() for layer in LAYER_ORDER
        }
        self._lock = threading.RLock()
        self._rules_loaded = False
        self._p0_rules: list[Any] = []  # set after rules.py loads

    # ── Public API ───────────────────────────────────────────────────────────

    def enforce(self, layer: Layer, context: dict[str, Any]) -> EnforcementResult:
        """
        Enforce brain rules for a given layer.

        Args:
            layer: The brain layer context (determines which rules apply)
            context: Action context (action, tool, args, etc.)

        Returns:
            EnforcementResult — allowed=True or blocked with Violation
        """
        # P0 (brainstem) always checked regardless of layer
        p0_violation = self._check_p0_rules(context)
        if p0_violation:
            return EnforcementResult.blocked(p0_violation)

        # Higher layers enforced based on context type
        if context.get("action"):
            return self._enforce_layer_action(layer, context)

        return EnforcementResult.ok()

    def check_violation(self, action: str, context: dict[str, Any] = None) -> Optional[Violation]:
        """
        Quick check — is this action forbidden by P0?

        Args:
            action: The string action to check (e.g., "rm -rf /")
            context: Additional context dict

        Returns:
            Violation if forbidden, None if allowed
        """
        ctx = context or {}
        ctx["action"] = action
        result = self.enforce(Layer.P0_BRAINSTEM, ctx)
        return result.violation

    def get_prompt_injection(self, layer: Layer) -> str:
        """
        Get the system prompt fragment for a given layer.

        Returns layer-specific guidance for injection into the system prompt.
        """
        self._ensure_layer_loaded(layer)
        state = self._layers[layer]
        return state.content

    def get_self_model(self) -> str:
        """Get P5-Ego self-model content."""
        self._ensure_layer_loaded(Layer.P5_EGO)
        return self._layers[Layer.P5_EGO].content

    def get_soul(self) -> str:
        """Get P1-Limbic SOUL content (Drewgent's voice/identity)."""
        self._ensure_layer_loaded(Layer.P1_LIMBIC)
        return self._layers[Layer.P1_LIMBIC].content

    def reload(self, layer: Optional[Layer] = None) -> None:
        """
        Reload brain layers from vault.

        Args:
            layer: Specific layer to reload, or None for all
        """
        with self._lock:
            if layer is None:
                for l_ in LAYER_ORDER:
                    self._layers[l_].loaded = False
            else:
                self._layers[layer].loaded = False

    # ── Private ─────────────────────────────────────────────────────────────

    def _check_p0_rules(self, context: dict[str, Any]) -> Optional[Violation]:
        """Check P0 brainstem rules. Returns Violation if blocked."""
        action = context.get("action", "")
        if not action:
            return None

        # Lazy import to avoid circular
        try:
            from core.brain.rules import check_forbidden
        except ImportError:
            return None

        return check_forbidden(action, context)

    def _enforce_layer_action(self, layer: Layer, context: dict[str, Any]) -> EnforcementResult:
        """Enforce rules specific to a layer's action type."""
        action = context.get("action", "")

        # Tool execution check (P3-SENSORS)
        if layer == Layer.P3_SENSORS and context.get("tool"):
            violation = self._check_tool_rules(action, context)
            if violation:
                return EnforcementResult.blocked(violation)

        # Writing rules (P1-LIMBIC)
        if context.get("writing") and context.get("forbidden_patterns"):
            violation = self._check_writing_rules(context)
            if violation:
                return EnforcementResult.blocked(violation)

        return EnforcementResult.ok()

    def _check_tool_rules(self, action: str, context: dict[str, Any]) -> Optional[Violation]:
        """Check rules for dangerous tool operations."""
        tool = context.get("tool", "")

        # 禁auto_validate — dangerous ops need pre-validation
        dangerous_patterns = [
            r"rm\s+-rf",
            r"chmod\s+777",
            r"sudo\s+",
            r"drop\s+(table|database)",
        ]
        for pattern in dangerous_patterns:
            if re.search(pattern, action):
                if not context.get("_validated"):
                    return Violation(
                        rule_token="禁auto_validate",
                        severity="CRITICAL",
                        message=f"Dangerous operation '{action}' requires pre-validation hook before execution",
                        action=action,
                        context=context,
                        layer=Layer.P0_BRAINSTEM,
                    )
        return None

    def _check_writing_rules(self, context: dict[str, Any]) -> Optional[Violation]:
        """Check writing style forbidden patterns (P1-Limbic)."""
        content = context.get("content", "")
        patterns = context.get("forbidden_patterns", [])

        for pattern in patterns:
            if re.search(pattern, content):
                return Violation(
                    rule_token="禁writing_style",
                    severity="MEDIUM",
                    message=f"Content matches forbidden pattern: {pattern}",
                    action="write",
                    context=context,
                    layer=Layer.P1_LIMBIC,
                )
        return None

    def _ensure_layer_loaded(self, layer: Layer) -> None:
        """Load layer content from vault if not already loaded."""
        state = self._layers[layer]
        if state.loaded:
            return

        with self._lock:
            if state.loaded:  # double-check
                return
            layer_name = LAYER_NAMES[layer]
            # Map layer to vault directory
            vault_dirs = {
                Layer.P0_BRAINSTEM: self._vault / "P0-brainstem",
                Layer.P1_LIMBIC: self._vault / "P1-limbic",
                Layer.P2_HIPPOCAMPUS: self._vault / "P2-hippocampus",
                Layer.P3_SENSORS: self._vault / "P3-sensors",
                Layer.P4_CORTEX: self._vault / "P4-cortex",
                Layer.P5_EGO: self._vault / "P5-ego",
                Layer.P6_PREFRONTAL: self._vault / "P6-prefrontal",
            }
            vault_dir = vault_dirs.get(layer, self._vault)
            content = self._load_layer_content(layer, vault_dir)
            state.content = content
            state.loaded = True

    def _load_layer_content(self, layer: Layer, vault_dir: Path) -> str:
        """Load content from vault directory for a given layer."""
        layer_file_map = {
            Layer.P0_BRAINSTEM: "brain/rules.md",
            Layer.P1_LIMBIC: "persona/SOUL.md",
            Layer.P2_HIPPOCAMPUS: "memories/SCHEMA.md",
            Layer.P3_SENSORS: "gateway/drewgent-architecture-dataflow.md",
            Layer.P4_CORTEX: "growth/INTEGRATION_PROTOCOL.md",
            Layer.P5_EGO: "SELF_MODEL.md",
            Layer.P6_PREFRONTAL: "plans/growth-2026.md",
        }
        rel_path = layer_file_map.get(layer, "")
        if not rel_path:
            return ""

        full_path = vault_dir / rel_path
        if not full_path.exists():
            return self._fallback_content(layer)

        try:
            return full_path.read_text(encoding="utf-8")
        except Exception:
            return self._fallback_content(layer)

    def _fallback_content(self, layer: Layer) -> str:
        """Return minimal fallback content for a layer when vault is unavailable."""
        fallbacks = {
            Layer.P0_BRAINSTEM: "[P0-brainstem] CRITICAL rules loaded. No override allowed.",
            Layer.P1_LIMBIC: "[P1-limbic] Drewgent voice: direct, curious, pragmatic.",
            Layer.P2_HIPPOCAMPUS: "[P2-hippocampus] Memory and context management active.",
            Layer.P3_SENSORS: "[P3-sensors] Tool routing and skill dispatch enabled.",
            Layer.P4_CORTEX: "[P4-cortex] Growth and learning systems active.",
            Layer.P5_EGO: "[P5-ego] Drewgent identity: self-improving agent.",
            Layer.P6_PREFRONTAL: "[P6-prefrontal] Strategic planning and long-term goals.",
        }
        return fallbacks.get(layer, "")

    def __repr__(self) -> str:
        loaded = sum(1 for s in self._layers.values() if s.loaded)
        return f"DrewgentBrain(vault={self._vault}, layers_loaded={loaded}/7)"