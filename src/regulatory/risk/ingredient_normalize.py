"""INN ingredient name normalization for cross-source joins.

Converts free-text ingredient strings to a stable lowercase lookup key by:
1. Lowercasing and stripping surrounding whitespace.
2. Collapsing runs of whitespace to a single space.
3. Stripping exactly one trailing salt/ester/base suffix (longest match first).

Deliberately conservative: does not split combination drugs (e.g.
"amoxicillin/clavulanic acid" stays whole), does not tokenize, and does not
perform partial-match or phonetic lookups. The result is a deterministic key
suitable for equi-joins and set membership tests.
"""

from __future__ import annotations

import re

_SUFFIXES: tuple[str, ...] = (
    # Longest variants first so "hydrochloride" wins over "chloride" if that
    # were ever a suffix (it isn't, but the ordering prevents partial-strip).
    " hydrochloride",
    " dihydrochloride",
    " monohydrochloride",
    " sesquihydrate",
    " monohydrate",
    " dihydrate",
    " anhydrous",
    " hcl",
    " sodium",
    " disodium",
    " trisodium",
    " potassium",
    " calcium",
    " magnesium",
    " zinc",
    " aluminum",
    " aluminium",
    " ammonium",
    " sulfate",
    " sulphate",
    " bisulfate",
    " disulfate",
    " phosphate",
    " diphosphate",
    " citrate",
    " trisodium citrate",
    " acetate",
    " diacetate",
    " succinate",
    " hemisuccinate",
    " tartrate",
    " bitartrate",
    " hemitartrate",
    " maleate",
    " fumarate",
    " hemifumarate",
    " mesylate",
    " mesilate",
    " tosylate",
    " benzoate",
    " valerate",
    " stearate",
    " palmitate",
    " propionate",
    " gluconate",
    " lactate",
    " oxalate",
    " bromide",
    " iodide",
    " fluoride",
    " nitrate",
    " nitrite",
    " carbonate",
    " bicarbonate",
)


def normalize_ingredient(name: str) -> str:
    """Return a normalized INN lookup key for *name*.

    Lowercases, collapses whitespace, and strips exactly one trailing
    salt/ester suffix. Safe to call on already-normalized strings
    (idempotent).

    Args:
        name: Raw ingredient name (any case, any whitespace).

    Returns:
        Normalized lowercase string, or ``""`` if *name* is empty.

    Raises:
        TypeError: If *name* is not a string.
    """
    if not isinstance(name, str):
        raise TypeError(f"Expected str, got {type(name).__name__!r}")
    if not name:
        return ""
    normalized = re.sub(r"\s+", " ", name.lower().strip())
    for suffix in _SUFFIXES:
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)].rstrip()
            break
    return normalized
