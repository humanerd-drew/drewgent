"""
OpenAI provider implementation for Drewgent.

Handles OpenAI client creation, streaming, and credential management.
Extracted from run_agent.py AIAgent._create_openai_client and related methods.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from openai import OpenAI

from core.providers.base import ProviderClient, ProviderConfig

logger = logging.getLogger(__name__)


class OpenAIProvider(ProviderClient):
    """
    OpenAI API provider.

    Handles client creation for:
    - Direct OpenAI API (api.openai.com)
    - OpenAI-compatible endpoints (openrouter, vllm, local, etc.)
    """

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self._client: Optional[OpenAI] = None

    def create_client(self) -> OpenAI:
        """Create an OpenAI client from config."""
        kwargs: dict[str, Any] = {"timeout": self._config.timeout}

        if self._config.api_key:
            kwargs["api_key"] = self._config.api_key

        if self._config.base_url:
            kwargs["base_url"] = self._config.base_url

        if self._config.extra_headers:
            kwargs["default_headers"] = self._config.extra_headers

        return OpenAI(**kwargs)

    def close_client(self, client: OpenAI, *, reason: str) -> None:
        """Close OpenAI client and all underlying HTTP connections."""
        if client is None:
            return

        try:
            # Close TCP sockets
            sockets_closed = self.force_close_tcp_sockets(client)
            logger.debug(
                "Closed %d TCP sockets for OpenAI client (%s)",
                sockets_closed,
                reason,
            )
        except Exception as e:
            logger.warning("Error closing OpenAI client: %s", e)

        self._client = None

    def refresh_credentials(self, *, force: bool = False) -> bool:
        """
        OpenAI doesn't use refresh tokens — just verify API key is valid.

        In practice, this is a no-op for OpenAI. Credentials are fresh
        as long as API key is set.
        """
        return self._config.api_key is not None

    def is_direct_openai_url(self, base_url: Optional[str] = None) -> bool:
        """Check if base_url is api.openai.com."""
        url = base_url or self._config.base_url or ""
        return "api.openai.com" in url

    def is_openrouter_url(self, base_url: Optional[str] = None) -> bool:
        """Check if base_url is openrouter.ai."""
        url = base_url or self._config.base_url or ""
        return "openrouter.ai" in url

    def is_azure_url(self, base_url: Optional[str] = None) -> bool:
        """Check if base_url is Azure OpenAI."""
        url = base_url or self._config.base_url or ""
        return (
            "openai.azure.com" in url
            or "cognitiveservices" in url
            or ".azure.com" in url
        )

    def is_github_copilot_url(self, base_url: Optional[str] = None) -> bool:
        """Check if base_url is GitHub Copilot."""
        url = base_url or self._config.base_url or ""
        return "githubcopilot.com" in url or "api.github.com" in url

    def build_request_kwargs(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        stream: bool = False,
        base_url: Optional[str] = None,
        **extra_kwargs,
    ) -> dict[str, Any]:
        """
        Build kwargs dict for OpenAI chat completions API call.

        Returns a kwargs dict ready to pass to client.chat.completions.create().
        """
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = extra_kwargs.pop("tool_choice", "auto")

        # Remove None values
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        kwargs.update(extra_kwargs)

        _apply_rate_limit_headers(kwargs, base_url or self._config.base_url or "")

        return kwargs


def _apply_rate_limit_headers(kwargs: dict[str, Any], base_url: str) -> None:
    """
    Apply provider-specific rate limit headers to request kwargs.

    This modifies kwargs in-place, adding extra_headers for certain providers.
    """
    # Add provider-specific tweaks
    if "openrouter" in base_url:
        kwargs.setdefault("extra_headers", {})
        # OpenRouter requires specific headers for some models
        pass

    if "githubcopilot" in base_url:
        kwargs.setdefault("extra_headers", {})
        kwargs["extra_headers"]["X-GitHub-Token"] = "PLACEHOLDER"


class OpenAIProviderPool:
    """
    Shared pool of OpenAI provider clients.

    Used by AIAgent to manage multiple provider instances
    (primary, request, fallback) without creating duplicates.
    """

    def __init__(self) -> None:
        self._primary: Optional[OpenAIProvider] = None
        self._request: Optional[OpenAIProvider] = None
        self._lock = threading.RLock()

    def get_or_create_primary(self, config: ProviderConfig) -> OpenAIProvider:
        """Get or create the primary provider client."""
        with self._lock:
            if self._primary is None:
                self._primary = OpenAIProvider(config)
            return self._primary

    def get_or_create_request(self, config: ProviderConfig) -> OpenAIProvider:
        """Get or create the request-scoped provider client."""
        with self._lock:
            if self._request is None:
                self._request = OpenAIProvider(config)
            return self._request

    def close_all(self) -> None:
        """Close all provider clients in the pool."""
        with self._lock:
            if self._primary:
                try:
                    self._primary.close_client(self._primary._client, reason="pool_close")
                except Exception:
                    pass
                self._primary = None
            if self._request:
                try:
                    self._request.close_client(self._request._client, reason="pool_close")
                except Exception:
                    pass
                self._request = None