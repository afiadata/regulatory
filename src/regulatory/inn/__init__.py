"""INN (International Nonproprietary Name) normalization utilities."""

from __future__ import annotations

import json
from pathlib import Path

_LOOKUP: dict[str, str] | None = None


def _load_lookup() -> dict[str, str]:
    """Load the INN lookup table from JSON, caching the result.

    Returns:
        Mapping of lowercase alias → canonical INN string.
    """
    global _LOOKUP
    if _LOOKUP is None:
        path = Path(__file__).parent / "inn_lookup.json"
        raw: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
        # Strip the comment key if present
        _LOOKUP = {k: v for k, v in raw.items() if not k.startswith("_")}
    return _LOOKUP


def normalize(name: str) -> str:
    """Return the canonical INN for *name*, or *name* unchanged if unknown.

    Normalisation is case-insensitive and strips surrounding whitespace.

    Args:
        name: Raw active-ingredient name as found in the source.

    Returns:
        Canonical INN string, or the original *name* if not in the lookup.
    """
    lookup = _load_lookup()
    key = name.strip().lower()
    return lookup.get(key, name.strip())


def normalize_list(names: list[str]) -> tuple[list[str], list[str]]:
    """Normalize a list of ingredient names.

    Args:
        names: Raw ingredient names.

    Returns:
        Tuple of ``(normalized, raw)`` where *normalized* contains canonical
        INN values and *raw* is the original list unchanged.
    """
    raw = [n.strip() for n in names if n.strip()]
    normalized = [normalize(n) for n in raw]
    return normalized, raw
