"""Source adapter registry.

Use ``@register_source`` to register adapters so the scheduler can discover
them without a hardcoded import list.

Example::

    from regulatory.ingestion.registry import register_source
    from regulatory.ingestion.base import RegulatorySource

    @register_source
    class MySource(RegulatorySource):
        source_id = "my_source"
        ...
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from regulatory.ingestion.base import RegulatorySource

log = structlog.get_logger(__name__)

_REGISTRY: dict[str, type[RegulatorySource]] = {}


def register_source(cls: type[RegulatorySource]) -> type[RegulatorySource]:
    """Class decorator that adds *cls* to the global source registry.

    Args:
        cls: A concrete subclass of :class:`~regulatory.ingestion.base.RegulatorySource`.

    Returns:
        The class unchanged (decorator pattern).

    Raises:
        ValueError: If a source with the same ``source_id`` is already registered.
    """
    source_id: str = getattr(cls, "source_id", "")
    if not source_id:
        raise ValueError(f"Cannot register {cls.__name__}: missing source_id attribute.")
    if source_id in _REGISTRY:
        raise ValueError(
            f"Source '{source_id}' already registered by {_REGISTRY[source_id].__name__}."
        )
    _REGISTRY[source_id] = cls
    log.debug("source_registered", source_id=source_id, cls=cls.__name__)
    return cls


def get_source(source_id: str) -> type[RegulatorySource]:
    """Look up a registered source class by its ``source_id``.

    Args:
        source_id: The unique identifier of the desired source.

    Returns:
        The adapter class (not an instance).

    Raises:
        KeyError: If *source_id* is not registered.
    """
    if source_id not in _REGISTRY:
        raise KeyError(
            f"No source registered with id '{source_id}'. Available: {sorted(_REGISTRY.keys())}"
        )
    return _REGISTRY[source_id]


def all_sources() -> dict[str, type[RegulatorySource]]:
    """Return a snapshot of the registry.

    Returns:
        Mapping of ``source_id`` → adapter class.
    """
    return dict(_REGISTRY)
