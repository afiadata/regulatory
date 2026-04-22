"""Anthropic client wrapper with response caching.

Responses are cached by content hash (sha256) to avoid re-billing for the
same PDF or prompt. Cache is in-memory for the process lifetime; a Redis or
Postgres-backed cache can be wired in later.
"""

from __future__ import annotations

import os
from typing import Any

import anthropic
import structlog

log = structlog.get_logger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-6"


class AnthropicClient:
    """Thin wrapper around the Anthropic SDK with in-process response caching.

    Args:
        api_key: Anthropic API key. Falls back to ``ANTHROPIC_API_KEY`` env var.
        model: Claude model ID to use for completions.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
    ) -> None:
        """Initialise the client and empty response cache."""
        self.model = model
        self.client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
        self._cache: dict[str, Any] = {}
        log.debug("anthropic_client_init", model=model)

    def get_cached_response(self, key: str) -> Any | None:
        """Return a cached response for *key*, or ``None`` if not found.

        Args:
            key: Cache key (typically sha256 of the source content).

        Returns:
            Cached value, or ``None``.
        """
        return self._cache.get(key)

    def cache_response(self, key: str, value: Any) -> None:
        """Store *value* under *key* in the response cache.

        Args:
            key: Cache key.
            value: Value to cache.
        """
        self._cache[key] = value

    def simple_complete(self, prompt: str, max_tokens: int = 1024) -> str:
        """Run a simple text completion.

        Args:
            prompt: User message text.
            max_tokens: Maximum tokens in the response.

        Returns:
            Assistant response text.
        """
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text if response.content else ""


_CLIENT: AnthropicClient | None = None


def get_anthropic_client() -> AnthropicClient:
    """Return (or create) the module-level :class:`AnthropicClient` singleton.

    Returns:
        The shared :class:`AnthropicClient` instance.
    """
    global _CLIENT
    if _CLIENT is None:
        model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
        _CLIENT = AnthropicClient(model=model)
    return _CLIENT
