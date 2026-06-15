"""Drewgent override of hermes_cli.gateway — gateway label is ai.drewgent.* not ai.hermes.*

Strategy: this module is a PROXY that re-exports everything from the real
hermes_cli.gateway, but with our `get_launchd_label()` patched AND
`find_gateway_pids()` returning our gateway's PID. We use `importlib` to
load the real module and rebind this module's __dict__ to mirror it, then
patch the entries we care about.

Activation: requires PYTHONPATH to include ~/.drewgent/customize so this
module is found first by the import machinery.

Note: real hermes_cli.gateway transitively imports gateway.session which
has a Python 3.11 forward-reference bug (`MessageEvent`). We wrap the real
import in try/except so that proxying still exposes our overrides, but
broken re-exports are silently skipped.
"""
import importlib.util
import os
import subprocess
import sys

# Load the real hermes_cli.gateway as a separate module
_REAL_HERMES_PKG = os.path.expanduser("~/.hermes/hermes-agent")
_spec = importlib.util.spec_from_file_location(
    "_real_hermes_cli_gateway",
    os.path.join(_REAL_HERMES_PKG, "hermes_cli", "gateway.py"),
)
assert _spec is not None and _spec.loader is not None
_real = importlib.util.module_from_spec(_spec)
sys.modules["_real_hermes_cli_gateway"] = _real
try:
    _spec.loader.exec_module(_real)
except Exception as _e:
    # Real hermes_cli.gateway has Python 3.11 forward-ref bug.
    # Fall through with partial module — our overrides below still register.
    sys.stderr.write(f"[drewgent-customize] warn: real hermes_cli.gateway "
                     f"import failed ({type(_e).__name__}: {str(_e)[:80]}). "
                     f"Overrides will register but re-exports may be incomplete.\n")
    _real = None

# Make this proxy module expose everything from _real (if import succeeded)
_this = sys.modules[__name__]
if _real is not None:
    for _name in dir(_real):
        if not _name.startswith("_"):
            setattr(_this, _name, getattr(_real, _name))

# --- Override: get_launchd_label ---
def get_launchd_label() -> str:
    """Drewgent uses ai.drewgent.gateway (not ai.hermes.gateway)."""
    return "ai.drewgent.gateway"

# --- Override: find_gateway_pids ---
# Why: hermes's _get_service_pids() parses `launchctl list <label>` as
# tab-separated text, but macOS Sonoma+ returns plist-format JSON.
# We re-implement to handle both formats AND to look up our label.
def find_gateway_pids(
    exclude_pids: set | None = None,
    all_profiles: bool = False,
) -> list:
    """Find PIDs of running gateway processes (Drewgent version)."""
    excluded = set(exclude_pids or set())
    pids: list = []

    # Strategy: query launchd for our gateway label directly.
    for label in ("ai.drewgent.gateway", "ai.hermes.gateway"):
        try:
            result = subprocess.run(
                ["launchctl", "list", label],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if result.returncode != 0:
            continue
        out = result.stdout
        # Try text format first: "PID\tStatus\tLabel"
        for line in out.strip().splitlines():
            parts = line.split("\t") if "\t" in line else line.split()
            if len(parts) >= 3 and parts[2].strip() == label:
                try:
                    pid = int(parts[0])
                    if pid > 0 and pid not in excluded:
                        pids.append(pid)
                except ValueError:
                    pass
                break
        else:
            # Plist format: look for "PID" = NNN;
            import re
            m = re.search(r'"PID"\s*=\s*(\d+)\s*;', out)
            if m:
                try:
                    pid = int(m.group(1))
                    if pid > 0 and pid not in excluded:
                        pids.append(pid)
                except ValueError:
                    pass
    return pids

# Rebind on _real (if available) so hermes's internal references resolve
if _real is not None:
    _real.get_launchd_label = get_launchd_label
    _real.find_gateway_pids = find_gateway_pids

# Rebind on this module so direct imports get the overrides
setattr(_this, "get_launchd_label", get_launchd_label)
setattr(_this, "find_gateway_pids", find_gateway_pids)

# Register this proxy under the canonical name so the rest of hermes
# resolves to it.
sys.modules["hermes_cli.gateway"] = _this
