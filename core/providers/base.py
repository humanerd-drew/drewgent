"""
Provider abstraction layer for Drewgent.

Handles creation, lifecycle, and credential refresh for all LLM providers
(OpenAI, Anthropic, Nous, Codex). Extracted from run_agent.py AIAgent class.
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class ProviderConfig:
    """Configuration for a provider client."""

    api_key: Optional[str] = None
    base_url: Optional[str] = None
    max_retries: int = 3
    timeout: float = 120.0
    extra_headers: dict[str, str] = field(default_factory=dict)


class ProviderClient(ABC):
    """
    Abstract base class for LLM provider clients.

    All provider implementations must:
    - Implement create_client() — returns a configured client instance
    - Implement close_client(client, reason) — clean up the client
    - Implement refresh_credentials() — refresh auth tokens if needed
    """

    _openai_client_lock: threading.RLock = field(
        default_factory=threading.RLock,
        repr=False,
    )

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config
        self._client: Optional[Any] = None
        self._shared: bool = False

    @abstractmethod
    def create_client(self) -> Any:
        """Create and return a provider client instance."""
        ...

    @abstractmethod
    def close_client(self, client: Any, *, reason: str) -> None:
        """Close a provider client, releasing resources."""
        ...

    def is_closed(self, client: Any) -> bool:
        """Check if a client has been closed."""
        if client is None:
            return True
        try:
            return getattr(client, "_closed", False) or getattr(client, "closed", False)
        except Exception:
            return False

    @abstractmethod
    def refresh_credentials(self, *, force: bool = False) -> bool:
        """
        Refresh authentication credentials.

        Returns True if refresh succeeded, False otherwise.
        """
        ...

    def force_close_tcp_sockets(self, client: Any) -> int:
        """
        Force-close TCP sockets for a client.

        Returns number of sockets closed.
        """
        closed = 0
        try:
            sync_async_clients = getattr(client, "_sync_clients", [])
            if sync_async_clients:
                for sc in sync_async_clients:
                    try:
                        if hasattr(sc, "_client"):
                            http_client = sc._client
                            if hasattr(http_client, "_transport"):
                                transport = http_client._transport
                                if hasattr(transport, "_pool"):
                                    pool = transport._pool
                                    if hasattr(pool, "_connections"):
                                        for conn in list(pool._connections):
                                            try:
                                                conn.close()
                                                closed += 1
                                            except Exception:
                                                pass
                    except Exception:
                        pass
        except Exception as e:
            logger.debug("force_close_tcp_sockets: %s", e)
        return closed

    def openai_client_lock(self) -> threading.RLock:
        """Get the shared OpenAI client lock."""
        return ProviderClient._openai_client_lock