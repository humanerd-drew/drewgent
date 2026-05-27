"""Shared fixtures for the drewgent-agent test suite."""

import asyncio
import os
import signal
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _isolate_drewgent_home(tmp_path, monkeypatch):
    """Redirect DREW_HOME to a temp dir so tests never write to ~/.drewgent/."""
    fake_home = tmp_path / "drewgent_test"
    fake_home.mkdir()
    (fake_home / "sessions").mkdir()
    (fake_home / "cron").mkdir()
    (fake_home / "memories").mkdir()
    (fake_home / "skills").mkdir()
    monkeypatch.setenv("DREW_HOME", str(fake_home))
    try:
        import drewgent_constants as _drewgent_constants
        monkeypatch.setattr(
            _drewgent_constants,
            "get_drewgent_home",
            lambda: Path(os.environ.get("DREW_HOME", str(fake_home))),
        )
    except Exception:
        pass
    try:
        import gateway.status as _gateway_status
        monkeypatch.setattr(
            _gateway_status,
            "get_drewgent_home",
            lambda: Path(os.environ.get("DREW_HOME", str(fake_home))),
        )
    except Exception:
        pass
    # Reset plugin singleton so tests don't leak plugins from ~/.drewgent/plugins/
    try:
        import drewgent_cli.plugins as _plugins_mod
        monkeypatch.setattr(_plugins_mod, "_plugin_manager", None)
    except Exception:
        pass
    # Tests should not inherit the agent's current gateway/messaging surface.
    # Individual tests that need gateway behavior set these explicitly.
    monkeypatch.delenv("DREW_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("DREW_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("DREW_SESSION_CHAT_NAME", raising=False)
    monkeypatch.delenv("DREW_GATEWAY_SESSION", raising=False)


@pytest.fixture()
def tmp_dir(tmp_path):
    """Provide a temporary directory that is cleaned up automatically."""
    return tmp_path


@pytest.fixture()
def mock_config():
    """Return a minimal drewgent config dict suitable for unit tests."""
    return {
        "model": "test/mock-model",
        "toolsets": ["terminal", "file"],
        "max_turns": 10,
        "terminal": {
            "backend": "local",
            "cwd": "/tmp",
            "timeout": 30,
        },
        "compression": {"enabled": False},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "command_allowlist": [],
    }


# ── Global test timeout ─────────────────────────────────────────────────────
# Kill any individual test that takes longer than 30 seconds.
# Prevents hanging tests (subprocess spawns, blocking I/O) from stalling the
# entire test suite.

def _timeout_handler(signum, frame):
    raise TimeoutError("Test exceeded 30 second timeout")

@pytest.fixture(autouse=True)
def _ensure_current_event_loop(request):
    """Provide a default event loop for sync tests that call get_event_loop().

    Python 3.11+ no longer guarantees a current loop for plain synchronous tests.
    A number of gateway tests still use asyncio.get_event_loop().run_until_complete(...).
    Ensure they always have a usable loop without interfering with pytest-asyncio's
    own loop management for @pytest.mark.asyncio tests.
    """
    if request.node.get_closest_marker("asyncio") is not None:
        yield
        return

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None

    created = loop is None or loop.is_closed()
    if created:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    try:
        yield
    finally:
        if created and loop is not None:
            try:
                loop.close()
            finally:
                asyncio.set_event_loop(None)


@pytest.fixture(autouse=True)
def _enforce_test_timeout():
    """Kill any individual test that takes longer than 30 seconds.
    SIGALRM is Unix-only; skip on Windows."""
    if sys.platform == "win32":
        yield
        return
    old = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(30)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, old)


# ── Common GatewayRunner mock fixture ────────────────────────────────────────
# Every test that creates a mock runner via object.__new__(GatewayRunner)
# needs the same set of attributes that __init__ normally populates.
# This fixture replaces the boilerplate that was repeated across 8+ test files.


@pytest.fixture
def mock_gateway_runner():
    """
    Return a fully-stubbed GatewayRunner instance (bypasses __init__).

    Use this in gateway tests instead of manually doing:
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(...)
        runner._running_agents = {}
        runner._running_agents_ts = {}
        runner._pending_messages = {}
        runner._pending_approvals = {}
        runner._voice_mode = {}
        runner._background_tasks = set()
        runner._is_user_authorized = lambda _source: True
        runner._task_manager = AsyncMock()
        runner._session_manager = MagicMock()
        runner.hooks = AsyncMock()
        runner.session_store = MagicMock()
        runner.session_store.get_or_create_session = MagicMock()
        runner.session_store._generate_session_key.return_value = "telegram:dm:12345"

    Returns a real object instance (not a MagicMock), so attribute access
    and dict operations work as they would on the real GatewayRunner.
    """
    from unittest.mock import AsyncMock, MagicMock

    from gateway.run import GatewayRunner, GatewayConfig
    from gateway.config import GatewayConfig, Platform, PlatformConfig

    runner = object.__new__(GatewayRunner)

    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="test-token")}
    )

    class _FakeAdapter:
        def __init__(self):
            self.sent = []
            self._pending_messages = {}  # must be dict, not MagicMock

        async def send(self, chat_id, text, *a, **kw):
            self.sent.append((chat_id, text))
            return None

        async def send_photo(self, chat_id, photo_path, caption="", *a, **kw):
            self.sent.append((chat_id, f"[photo] {caption}"))
            return None

        async def send_typing(self, chat_id, *a, **kw):
            return None

        async def update_message(self, chat_id, message_id, text, *a, **kw):
            return None

        async def delete_message(self, chat_id, message_id, *a, **kw):
            return None

        def clear_sent(self):
            self.sent.clear()

        # Methods used by sentinel race guard tests
        def get_pending_message(self, session_key):
            return self._pending_messages.get(session_key)

        def has_pending_interrupt(self, session_key):
            return False

    runner.adapters = {Platform.TELEGRAM: _FakeAdapter()}
    runner.delivery_router = MagicMock()

    runner._running_agents = {}
    runner._running_agents_ts = {}  # staleness eviction reads this
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._voice_mode = {}
    runner._background_tasks = set()

    runner._is_user_authorized = lambda _source: True

    runner._task_manager = AsyncMock()
    runner._session_manager = MagicMock()
    runner.hooks = AsyncMock()

    runner.session_store = MagicMock()
    # get_or_create_session must return a SessionEntry-like mock with
    # .session_key matching _running_agents key format
    _fake_entry = MagicMock()
    _fake_entry.session_key = "agent:main:telegram:dm:12345"
    runner.session_store.get_or_create_session = MagicMock(return_value=_fake_entry)
    # _session_key_for_source tries this first; must return a real str
    # Must return the SAME key that build_session_key produces, so
    # _running_agents and _running_agents_ts use the same key format.
    # build_session_key for telegram DM chat_id=12345 → "agent:main:telegram:dm:12345"
    runner.session_store._generate_session_key.return_value = "agent:main:telegram:dm:12345"

    return runner
