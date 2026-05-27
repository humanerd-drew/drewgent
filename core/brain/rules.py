"""
Drewgent P0-brainstem Rules — Python implementation.

This module converts the vault .neuron files into Python patterns
for fast runtime enforcement. Each rule has:
  - rule_token: unique identifier matching the .neuron file name
  - severity: CRITICAL, HIGH, MEDIUM
  - patterns: list of regexes or check functions
  - message: user-facing explanation

P0 (brainstem) rules are absolute — they cannot be overridden by any
higher layer (P1-P6). Every dangerous action must pass these checks first.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

# ─────────────────────────────────────────────────────────────────────────────
# Rule definitions
# Each tuple: (pattern_or_callable, rule_token, severity, message)
# ─────────────────────────────────────────────────────────────────────────────

_FORBIDDEN_PATTERNS: list[tuple[str | Any, str, str, str]] = [
    # 禁rm_rf_root — never delete root directory or system paths
    (
        r"rm\s+-rf\s+/\*",
        "禁rm_rf_root",
        "CRITICAL",
        "rm -rf on root directory is forbidden. Use safer alternatives.",
    ),
    (
        r"rm\s+-rf\s+~",
        "禁rm_rf_root",
        "CRITICAL",
        "rm -rf on home directory is forbidden.",
    ),
    # 禁blind_write — always read file before writing
    (
        "blind_write",
        "禁blind_write",
        "HIGH",
        "File write without prior read is forbidden. Use patch or read-then-write.",
    ),
    # 禁secrets_in_code — API keys must be environment variables
    (
        r'sk-[a-zA-Z0-9]{20,}',
        "禁secrets_in_code",
        "CRITICAL",
        "Hardcoded secret (sk-, ghp-, password=) found in code. Use os.getenv().",
    ),
    (
        r'ghp_[a-zA-Z0-9]{36,}',
        "禁secrets_in_code",
        "CRITICAL",
        "Hardcoded GitHub token found in code.",
    ),
    (
        r'(?i)(api[_-]?key|password|secret|token)\s*=\s*["\'][a-zA-Z0-9+/=]{16,}',
        "禁secrets_in_code",
        "CRITICAL",
        "Hardcoded credential found in code.",
    ),
    # 禁console_log_production — no console.log in production
    (
        r'console\.log\s*\(',
        "禁console_log_production",
        "MEDIUM",
        "console.log in production code is forbidden. Use logging module.",
    ),
    # 禁auto_validate — dangerous ops need pre-validation
    (
        r"(?<!_)\brm\s+(-rf|-r\s+-f)\s",
        "禁auto_validate",
        "HIGH",
        "Dangerous rm command detected. Pre-validation hook required before execution.",
    ),
    (
        r"chmod\s+777",
        "禁auto_validate",
        "HIGH",
        "chmod 777 detected. Use minimal permissions.",
    ),
    (
        r"sudo\s+",
        "禁auto_validate",
        "HIGH",
        "sudo command detected. Verify necessity and escalate safely.",
    ),
]


def check_forbidden(action: str, context: dict[str, Any] = None) -> Optional[Any]:
    """
    Check if an action violates P0 brainstem rules.

    Args:
        action: The string action to check
        context: Additional context (tool, args, etc.)

    Returns:
        Violation object if blocked, None if allowed
    """
    # Import here to avoid circular
    from dataclasses import dataclass

    ctx = context or {}

    for pattern, rule_token, severity, message in _FORBIDDEN_PATTERNS:
        if isinstance(pattern, str):
            if re.search(pattern, action):
                # Check for blind write
                if rule_token == "禁blind_write":
                    if not ctx.get("_file_read_before_write"):
                        return _make_violation(rule_token, severity, message, action, ctx)
                    continue
                return _make_violation(rule_token, severity, message, action, ctx)

    return None


def _make_violation(
    rule_token: str,
    severity: str,
    message: str,
    action: str,
    ctx: dict[str, Any],
) -> Any:
    """Create a Violation dataclass without importing at module level."""
    # Late import to avoid issues
    try:
        from core.brain.subsystem import Layer, Violation

        layer = Layer.P0_BRAINSTEM
    except ImportError:
        # Fallback if brain module not available yet
        layer = None

    return _LazyViolation(
        rule_token=rule_token,
        severity=severity,
        message=message,
        action=action,
        context=ctx,
        layer=layer,
    )


class _LazyViolation:
    """Deferred Violation — created without importing Violation at module level."""

    __slots__ = ("rule_token", "severity", "message", "action", "context", "_layer")

    def __init__(
        self,
        rule_token: str,
        severity: str,
        message: str,
        action: str,
        context: dict[str, Any],
        layer: Any,
    ) -> None:
        self.rule_token = rule_token
        self.severity = severity
        self.message = message
        self.action = action
        self.context = context
        self._layer = layer

    @property
    def layer(self) -> Any:
        if self._layer is None:
            try:
                from drewgent.brain.subsystem import Layer

                self._layer = Layer.P0_BRAINSTEM
            except ImportError:
                pass
        return self._layer

    def __str__(self) -> str:
        return f"[{self.severity}] {self.rule_token}: {self.message}"


# ─────────────────────────────────────────────────────────────────────────────
# Specific check functions for common violations
# ─────────────────────────────────────────────────────────────────────────────


def check_rm_rf(command: str) -> Optional[_LazyViolation]:
    """Check for dangerous rm -rf commands."""
    dangerous = [r"rm\s+-rf\s+/\*", r"rm\s+-rf\s+/\s", r"rm\s+-rf\s+~"]
    for p in dangerous:
        if re.search(p, command):
            return _make_violation(
                "禁rm_rf_root",
                "CRITICAL",
                f"Dangerous rm command: {command[:50]}",
                command,
                {},
            )
    return None


def check_secrets_in_content(content: str) -> list[_LazyViolation]:
    """Scan content for hardcoded secrets."""
    violations = []
    secret_patterns = [
        r"sk-[a-zA-Z0-9]{20,}",
        r"ghp_[a-zA-Z0-9]{36,}",
        r"xAI-[a-zA-Z0-9]{20,}",
        r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][a-zA-Z0-9+/=]{16,}['\"]",
    ]
    for pattern in secret_patterns:
        for match in re.finditer(pattern, content):
            violations.append(
                _make_violation(
                    "禁secrets_in_code",
                    "CRITICAL",
                    f"Hardcoded secret found: {match.group()[:30]}",
                    content,
                    {"offset": match.start()},
                )
            )
    return violations


def check_file_write_safety(path: str, existing_content: Optional[str]) -> Optional[_LazyViolation]:
    """Check if a file write has proper pre-read safety."""
    if existing_content is None:
        return _make_violation(
            "禁blind_write",
            "HIGH",
            f"File write to {path} without prior read is forbidden. Read existing content first.",
            f"write_file({path})",
            {"path": path},
        )
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Vault integration helpers
# ─────────────────────────────────────────────────────────────────────────────


def load_rules_from_vault(vault_path: str | Path) -> list[str]:
    """
    Load P0 rule tokens from vault .neuron files.

    Returns list of rule_token strings (e.g., ["禁rm_rf_root", "禁blind_write", ...])
    """
    vault = Path(vault_path).expanduser()
    p0_dir = vault / "P0-brainstem"

    if not p0_dir.exists():
        return [rule_token for _, rule_token, _, _ in _FORBIDDEN_PATTERNS]

    rules = []
    for neuron_file in p0_dir.rglob("*.neuron"):
        # Extract rule_token from filename: 禁xxx.neuron → 禁xxx
        name = neuron_file.name
        if name.startswith("禁"):
            rule_token = name.replace(".neuron", "")
            rules.append(rule_token)

    return rules if rules else [rule_token for _, rule_token, _, _ in _FORBIDDEN_PATTERNS]