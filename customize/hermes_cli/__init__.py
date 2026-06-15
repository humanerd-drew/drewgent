"""Drewgent customization of hermes_cli package.

The real hermes_cli/__init__.py defines __version__ and __release_date__.
We proxy that file, then explicitly register our overrides for hermes_cli.gateway
and hermes_cli.cron so that downstream `from hermes_cli.gateway import X` and
`from hermes_cli.cron import X` resolve to our customized versions.
"""
import importlib.util
import os
import sys

_REAL_HERMES = os.path.expanduser("~/.hermes/hermes-agent")
_init_spec = importlib.util.spec_from_file_location(
    "_real_hermes_cli",
    os.path.join(_REAL_HERMES, "hermes_cli", "__init__.py"),
)
assert _init_spec is not None and _init_spec.loader is not None
_real_init = importlib.util.module_from_spec(_init_spec)
sys.modules["_real_hermes_cli"] = _real_init
_init_spec.loader.exec_module(_real_init)

# Re-export everything from the real __init__ at package level
for _name in dir(_real_init):
    if not _name.startswith("_"):
        globals()[_name] = getattr(_real_init, _name)

# Register the real hermes_cli package under the canonical name
sys.modules["hermes_cli"] = _real_init

# Now load our hermes_cli.gateway override and register it under
# hermes_cli.gateway (so the real hermes_cli package sees our override
# when its internal code does `from hermes_cli.gateway import find_gateway_pids`).
_gw_spec = importlib.util.spec_from_file_location(
    "hermes_cli.gateway",
    os.path.join(os.path.dirname(__file__), "gateway.py"),
)
assert _gw_spec is not None and _gw_spec.loader is not None
_gw_mod = importlib.util.module_from_spec(_gw_spec)
sys.modules["hermes_cli.gateway"] = _gw_mod
_gw_spec.loader.exec_module(_gw_mod)

# Re-bind inside _real_init so any code that did
# `from hermes_cli.gateway import find_gateway_pids` BEFORE our override
# was registered now gets our version too.
import importlib
_real_init.find_gateway_pids = _gw_mod.find_gateway_pids
_real_init.get_launchd_label = _gw_mod.get_launchd_label
