"""Drewgent brain — 7-layer subsumption architecture."""

from .subsystem import (
    DrewgentBrain,
    Layer,
    Violation,
    EnforcementResult,
    LAYER_ORDER,
    LAYER_NAMES,
)
from .rules import (
    check_forbidden,
    check_rm_rf,
    check_secrets_in_content,
    check_file_write_safety,
    load_rules_from_vault,
)
from .vault_loader import (
    VaultDoc,
    load_vault_doc,
    parse_vault_doc,
    load_brain_rules_from_vault,
    get_p_layer_content,
)

__all__ = [
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
]