"""SQLAlchemy async session factory.

Usage::

    async with get_session() as session:
        result = await session.execute(select(Document))
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

log = structlog.get_logger(__name__)

_ENGINE = None
_SESSION_FACTORY: async_sessionmaker[AsyncSession] | None = None


def _get_engine() -> object:
    """Return (or create) the async SQLAlchemy engine.

    Reads ``DATABASE_URL`` from the environment and converts a ``postgresql://``
    prefix to ``postgresql+asyncpg://`` automatically.

    Returns:
        The ``AsyncEngine`` instance.

    Raises:
        RuntimeError: If ``DATABASE_URL`` is not set.
    """
    global _ENGINE
    if _ENGINE is None:
        url = os.environ.get("DATABASE_URL", "")
        if not url:
            raise RuntimeError("DATABASE_URL environment variable is not set.")
        # Convert sync URL to async driver
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        _ENGINE = create_async_engine(url, echo=False, pool_pre_ping=True)
        log.debug("db_engine_created", url=url.split("@")[-1])  # log host only, not creds
    return _ENGINE


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return (or create) the session factory."""
    global _SESSION_FACTORY
    if _SESSION_FACTORY is None:
        from sqlalchemy.ext.asyncio import AsyncEngine

        engine = _get_engine()
        assert isinstance(engine, AsyncEngine)
        _SESSION_FACTORY = async_sessionmaker(engine, expire_on_commit=False)
    return _SESSION_FACTORY


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a database session, committing on success and rolling back on error.

    Yields:
        An open :class:`sqlalchemy.ext.asyncio.AsyncSession`.
    """
    factory = _get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
