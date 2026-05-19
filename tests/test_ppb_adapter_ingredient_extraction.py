"""Regression tests for PPB Kenya adapter ingredient extraction."""
from __future__ import annotations

from regulatory.sources.ppb_ke_alerts import _parse_inn_cell


def test_single_ingredient() -> None:
    normalized, raw = _parse_inn_cell("Paracetamol 500mg")
    assert normalized == ["acetaminophen"]
    assert raw == ["Paracetamol 500mg"]


def test_combination_drug_with_and() -> None:
    """'and' inside a single-line combination name is preserved as one ingredient."""
    normalized, raw = _parse_inn_cell("Ibuprofen and Paracetamol")
    assert len(normalized) == 1
    assert "and" not in normalized
    assert raw == ["Ibuprofen and Paracetamol"]


def test_combination_drug_with_slash() -> None:
    """Slash-delimited combinations stay as one ingredient."""
    normalized, raw = _parse_inn_cell("Amoxicillin/Clavulanic Acid")
    assert len(normalized) == 1
    assert raw == ["Amoxicillin/Clavulanic Acid"]


def test_combination_drug_with_plus() -> None:
    """Plus-delimited combinations stay as one ingredient."""
    normalized, raw = _parse_inn_cell("Ferrous Sulphate + Folic Acid 200/0.4mg")
    assert len(normalized) == 1
    assert raw == ["Ferrous Sulphate + Folic Acid 200/0.4mg"]


def test_stopword_and_on_own_line_is_dropped() -> None:
    """'and' on its own newline (multi-product recall separator) is silently dropped."""
    cell = "Paracetamol 250mg\nand\nParacetamol 125mg Suppository"
    normalized, raw = _parse_inn_cell(cell)
    assert "and" not in normalized
    assert "and" not in raw
    assert len(normalized) == 2
    assert normalized[0] == "acetaminophen"
    assert normalized[1] == "acetaminophen"


def test_multiple_ingredients_semicolon_separated() -> None:
    normalized, raw = _parse_inn_cell("Amoxicillin 500mg;Clavulanate 125mg")
    assert len(normalized) == 2
    assert "and" not in normalized


def test_stopwords_dropped_from_both_lists() -> None:
    """Both normalized and raw omit the stopword token."""
    cell = "Metronidazole 200mg\nor\nMetronidazole 400mg"
    normalized, raw = _parse_inn_cell(cell)
    assert "or" not in normalized
    assert "or" not in raw
    assert len(normalized) == 2


def test_empty_cell() -> None:
    normalized, raw = _parse_inn_cell("")
    assert normalized == []
    assert raw == []


def test_whitespace_only_cell() -> None:
    normalized, raw = _parse_inn_cell("   \n  \n  ")
    assert normalized == []
    assert raw == []
