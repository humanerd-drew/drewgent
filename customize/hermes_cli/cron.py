"""Drewgent override of hermes_cli.cron — proxies the real cron module but
re-exports find_gateway_pids from our gateway override, so that
`hermes cron list`'s health check finds our `ai.drewgent.gateway`.

Activation: requires PYTHONPATH to include ~/.drewgent/customize so this
module is found first by the import machinery.

Note: real hermes_cli.cron transitively imports gateway.session which has
a Python 3.11 forward-reference bug. We wrap the real import in
try/except so that our overrides still register.
"""
import importlib.util
import os
import sys

_REAL_HERMES = os.path.expanduser("~/.hermes/hermes-agent")
_spec = importlib.util.spec_from_file_location(
    "_real_hermes_cli_cron",
    os.path.join(_REAL_HERMES, "hermes_cli", "cron.py"),
)
assert _spec is not None and _spec.loader is not None
_real_cron = importlib.util.module_from_spec(_spec)
sys.modules["_real_hermes_cli_cron"] = _real_cron
try:
    _spec.loader.exec_module(_real_cron)
except Exception as _e:
    sys.stderr.write(f"[drewgent-customize] warn: real hermes_cli.cron "
                     f"import failed ({type(_e).__name__}: {str(_e)[:80]}). "
                     f"Overrides will register but re-exports may be incomplete.\n")
    _real_cron = None

# Re-export everything from real cron (if import succeeded)
_this = sys.modules[__name__]
if _real_cron is not None:
    for _name in dir(_real_cron):
        if not _name.startswith("_"):
            setattr(_this, _name, getattr(_real_cron, _name))

# Load our gateway override and rebind find_gateway_pids on real cron
_gw_spec = importlib.util.spec_from_file_location(
    "hermes_cli.gateway",
    os.path.join(os.path.dirname(__file__), "gateway.py"),
)
assert _gw_spec is not None and _gw_spec.loader is not None
_gw_mod = importlib.util.module_from_spec(_gw_spec)
sys.modules["hermes_cli.gateway"] = _gw_mod
_gw_spec.loader.exec_module(_gw_mod)

# Rebind find_gateway_pids in real cron module (so cron.py's lazy import
# `from hermes_cli.gateway import find_gateway_pids` gets our version too).
if _real_cron is not None:
    _real_cron.find_gateway_pids = _gw_mod.find_gateway_pids

# Register this proxy under the canonical name
sys.modules["hermes_cli.cron"] = _this
