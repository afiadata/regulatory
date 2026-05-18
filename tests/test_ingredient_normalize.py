"""Unit tests for the INN ingredient normalizer."""
from __future__ import annotations

import pytest

from regulatory.risk.ingredient_normalize import normalize_ingredient


def test_strips_calcium_suffix() -> None:
    assert normalize_ingredient("ATORVASTATIN CALCIUM") == "atorvastatin"


def test_strips_hydrochloride() -> None:
    assert normalize_ingredient("Ciprofloxacin Hydrochloride") == "ciprofloxacin"


def test_strips_sodium() -> None:
    assert normalize_ingredient("DICLOFENAC SODIUM") == "diclofenac"


def test_strips_hcl_abbreviation() -> None:
    assert normalize_ingredient("metformin HCl") == "metformin"


def test_strips_maleate() -> None:
    assert normalize_ingredient("TIMOLOL MALEATE") == "timolol"


def test_strips_fumarate() -> None:
    assert normalize_ingredient("BISOPROLOL FUMARATE") == "bisoprolol"


def test_preserves_multi_word_ingredients() -> None:
    """Hyphenated combination stays unchanged — hyphen is not a salt boundary."""
    assert normalize_ingredient("artemether-lumefantrine") == "artemether-lumefantrine"


def test_preserves_combination_drugs() -> None:
    """Slash-delimited combination drugs are NOT split."""
    assert normalize_ingredient("amoxicillin/clavulanic acid") == "amoxicillin/clavulanic acid"


def test_preserves_parenthetical() -> None:
    """Parenthetical qualifier is preserved as-is."""
    assert normalize_ingredient("insulin (regular human)") == "insulin (regular human)"


def test_strips_only_one_suffix() -> None:
    """Only the last suffix is stripped, not chained stripping."""
    # "succinate" stripped → "metoprolol", not further stripped
    assert normalize_ingredient("Metoprolol Succinate") == "metoprolol"


def test_idempotent_simple() -> None:
    result = normalize_ingredient("atorvastatin")
    assert normalize_ingredient(result) == result


def test_idempotent_after_strip() -> None:
    """normalize(normalize(x)) == normalize(x) after suffix removal."""
    result = normalize_ingredient("ATORVASTATIN CALCIUM")
    assert normalize_ingredient(result) == result


def test_empty_string() -> None:
    assert normalize_ingredient("") == ""


def test_whitespace_only() -> None:
    assert normalize_ingredient("   ") == ""


def test_type_error_on_none() -> None:
    with pytest.raises(TypeError):
        normalize_ingredient(None)  # type: ignore[arg-type]


def test_collapses_internal_whitespace() -> None:
    """Internal runs of whitespace are collapsed but no suffix is stripped if the
    result is not a salt form (benzalkonium chloride is the INN, not benzalkonium
    + counterion)."""
    assert normalize_ingredient("BENZALKONIUM  CHLORIDE") == "benzalkonium chloride"


def test_strips_sulfate() -> None:
    assert normalize_ingredient("ZINC SULFATE") == "zinc"


def test_benzalkonium_chloride_preserved() -> None:
    """benzalkonium chloride is the INN — 'chloride' is not stripped."""
    assert normalize_ingredient("BENZALKONIUM CHLORIDE") == "benzalkonium chloride"
