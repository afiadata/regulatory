"""Shared HTTP client with rate-limiting, retry, conditional GETs, and robots.txt.

All adapters should obtain an :class:`HttpClient` via :func:`get_http_client`
rather than constructing ``httpx.AsyncClient`` directly.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
import structlog
import yaml

log = structlog.get_logger(__name__)

USER_AGENT = "AfiaData Regulatory Monitor (contact: info@afiadata.org)"

# ---------------------------------------------------------------------------
# Token-bucket rate limiter (per-domain)
# ---------------------------------------------------------------------------


class TokenBucket:
    """Thread-safe token-bucket rate limiter.

    Args:
        rate: Tokens added per second.
        capacity: Maximum tokens the bucket can hold.
    """

    def __init__(self, rate: float, capacity: float) -> None:
        """Initialise the bucket full."""
        self.rate = rate
        self.capacity = capacity
        self._tokens = capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until one token is available, then consume it."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last_refill = now
            if self._tokens < 1:
                wait = (1 - self._tokens) / self.rate
                log.debug("rate_limit_wait", wait_seconds=round(wait, 2))
                await asyncio.sleep(wait)
                self._tokens = 0
            else:
                self._tokens -= 1


# ---------------------------------------------------------------------------
# Domain-level configuration (loaded from config/sources.yaml)
# ---------------------------------------------------------------------------


def _load_domain_config(config_path: str = "config/sources.yaml") -> dict[str, dict[str, float]]:
    """Load per-domain rate-limit settings from YAML.

    Args:
        config_path: Path to the YAML configuration file.

    Returns:
        Mapping of domain → ``{"rate": float, "capacity": float}``.
    """
    try:
        with open(config_path) as fh:
            raw: dict[str, object] = yaml.safe_load(fh) or {}
        domains: dict[str, dict[str, float]] = {}
        raw_domains = raw.get("domains") or {}
        for domain, cfg in (raw_domains if isinstance(raw_domains, dict) else {}).items():
            if isinstance(cfg, dict):
                domains[str(domain)] = {
                    "rate": float(cfg.get("rate", 1.0)),
                    "capacity": float(cfg.get("capacity", 5.0)),
                }
        return domains
    except FileNotFoundError:
        log.warning("sources_yaml_not_found", path=config_path)
        return {}


# ---------------------------------------------------------------------------
# Robots.txt cache
# ---------------------------------------------------------------------------


class RobotsCache:
    """Lazily fetches and caches ``robots.txt`` per domain.

    Args:
        client: The underlying ``httpx.AsyncClient`` to use for fetches.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        """Initialise an empty cache."""
        self._cache: dict[str, RobotFileParser] = {}
        self._client = client

    async def allowed(self, url: str) -> bool:
        """Return ``True`` if ``USER_AGENT`` is allowed to fetch *url*.

        Args:
            url: The URL we want to fetch.

        Returns:
            ``True`` if the fetch is permitted by ``robots.txt``.
        """
        parsed = urlparse(url)
        domain = f"{parsed.scheme}://{parsed.netloc}"
        if domain not in self._cache:
            robots_url = f"{domain}/robots.txt"
            rp = RobotFileParser()
            try:
                resp = await self._client.get(robots_url, timeout=10.0)
                rp.parse(resp.text.splitlines())
            except Exception as exc:  # noqa: BLE001
                log.warning("robots_fetch_failed", url=robots_url, error=str(exc))
                # robots.txt unreachable — parse a permissive stand-in so
                # can_fetch() returns True for all paths.
                rp.parse(["User-agent: *", "Allow: /"])
            self._cache[domain] = rp
        return self._cache[domain].can_fetch(USER_AGENT, url)


# ---------------------------------------------------------------------------
# Main HTTP client
# ---------------------------------------------------------------------------


class HttpClient:
    """Async HTTP client with rate-limiting, retry, conditional GET, and content hashing.

    Args:
        config_path: Path to ``sources.yaml`` for domain-level rate limits.
        max_retries: Maximum number of retries on 429 / 5xx responses.
        base_backoff: Initial backoff in seconds (doubled each retry).
    """

    _DEFAULT_RATE = 1.0  # 1 request/second
    _DEFAULT_CAPACITY = 5.0

    def __init__(
        self,
        config_path: str = "config/sources.yaml",
        max_retries: int = 3,
        base_backoff: float = 2.0,
    ) -> None:
        """Initialise the client and load domain configuration."""
        self._domain_cfg = _load_domain_config(config_path)
        self._buckets: dict[str, TokenBucket] = {}
        self._max_retries = max_retries
        self._base_backoff = base_backoff
        self._client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=30.0,
        )
        self._robots = RobotsCache(self._client)

    async def __aenter__(self) -> HttpClient:
        """Enter the async context manager."""
        return self

    async def __aexit__(self, *_: object) -> None:
        """Close the underlying httpx client."""
        await self._client.aclose()

    def _bucket_for(self, url: str) -> TokenBucket:
        """Return (or create) the rate-limit bucket for *url*'s domain."""
        domain = urlparse(url).netloc
        if domain not in self._buckets:
            cfg = self._domain_cfg.get(domain, {})
            self._buckets[domain] = TokenBucket(
                rate=cfg.get("rate", self._DEFAULT_RATE),
                capacity=cfg.get("capacity", self._DEFAULT_CAPACITY),
            )
        return self._buckets[domain]

    async def get(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response | None:
        """Perform a GET with rate-limiting, retry, and conditional headers.

        Args:
            url: Target URL.
            etag: Previous ``ETag`` value for conditional GET.
            last_modified: Previous ``Last-Modified`` value.
            params: Additional query parameters.

        Returns:
            The ``httpx.Response``, or ``None`` if the server returned 304
            (content unchanged).

        Raises:
            httpx.HTTPStatusError: After exhausting retries on 4xx/5xx.
        """
        if not await self._robots.allowed(url):
            log.warning("robots_disallowed", url=url)
            return None

        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        bucket = self._bucket_for(url)
        backoff = self._base_backoff

        for attempt in range(1, self._max_retries + 2):
            await bucket.acquire()
            try:
                resp = await self._client.get(url, headers=headers, params=params)

                if resp.status_code == 304:
                    log.debug("conditional_get_not_modified", url=url)
                    return None

                if resp.status_code == 429 or resp.status_code >= 500:
                    if attempt <= self._max_retries:
                        retry_after = float(resp.headers.get("Retry-After", backoff))
                        log.warning(
                            "retrying",
                            url=url,
                            status=resp.status_code,
                            attempt=attempt,
                            wait=retry_after,
                        )
                        await asyncio.sleep(retry_after)
                        backoff *= 2
                        continue
                    resp.raise_for_status()

                resp.raise_for_status()
                return resp

            except httpx.TimeoutException as exc:
                if attempt <= self._max_retries:
                    log.warning("timeout_retrying", url=url, attempt=attempt, error=str(exc))
                    await asyncio.sleep(backoff)
                    backoff *= 2
                else:
                    raise

        return None  # unreachable, but satisfies mypy

    @staticmethod
    def content_hash(content: bytes) -> str:
        """Compute the sha256 hex digest of *content*.

        Args:
            content: Raw bytes to hash.

        Returns:
            64-character hex string.
        """
        return hashlib.sha256(content).hexdigest()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_CLIENT: HttpClient | None = None


def get_http_client() -> HttpClient:
    """Return (or create) the module-level :class:`HttpClient` singleton.

    Returns:
        The shared :class:`HttpClient` instance.
    """
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = HttpClient()
    return _CLIENT
