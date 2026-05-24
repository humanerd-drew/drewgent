"""
Drewgent core package — brain, providers, and runtime components.

This package provides Drewgent's Python-native brain layer (P-layer implementation)
and provider abstraction, extracted from the monolithic run_agent.py.

Modules:
    brain/       — 7-layer subsumption brain + P0 rules
    providers/   — LLM provider abstraction (OpenAI, Anthropic, Nous, Codex)
    runtime/     — Runtime components (stub, future extraction)

Usage:
    from core.brain import DrewgentBrain, Layer
    brain = DrewgentBrain("~/.drewgent")
    result = brain.enforce(Layer.P3_SENSORS, {"action": "rm -rf /", "tool": "terminal"})
"""

from core.brain.subsystem import (
    DrewgentBrain,
    Layer,
    Violation,
    EnforcementResult,
    LAYER_ORDER,
    LAYER_NAMES,
)
from core.brain.rules import (
    check_forbidden,
    check_rm_rf,
    check_secrets_in_content,
    check_file_write_safety,
    load_rules_from_vault,
)
from core.brain.vault_loader import (
    VaultDoc,
    load_vault_doc,
    parse_vault_doc,
    load_brain_rules_from_vault,
    get_p_layer_content,
)
from core.providers.base import ProviderClient, ProviderConfig
from core.providers.openai import OpenAIProvider, OpenAIProviderPool

__all__ = [
    # Brain
    "DrewgentBrain",
    "Layer",
    "Violation",
    "EnforcementResult",
    "LAYER_ORDER",
    "LAYER_NAMES",
    "check_forbidden",
    "check_rm_rf",
    "check_secrets_in_content",
    "check_file_write_safety",
    "load_rules_from_vault",
    "VaultDoc",
    "load_vault_doc",
    "parse_vault_doc",
    "load_brain_rules_from_vault",
    "get_p_layer_content",
    # Providers
    "ProviderClient",
    "ProviderConfig",
    "OpenAIProvider",
    "OpenAIProviderPool",
]

__version__ = "0.8.0"
__author__ = "humanerd / Drewgent"