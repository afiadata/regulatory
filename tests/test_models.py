"""Tests for NormalizedDocument.normalized_content_hash()."""

from __future__ import annotations

from datetime import date, datetime, timezone

from regulatory.models import DocumentType, NormalizedDocument, Severity


def _make_doc(**overrides: object) -> NormalizedDocument:
    """Return a minimal valid NormalizedDocument with stable defaults."""
    defaults: dict[str, object] = {
        "source_id": "sahpra_recalls",
        "source_url": "https://www.sahpra.org.za/document/halaven-eribulin/",
        "source_hash": "a" * 64,
        "jurisdiction": "ZA",
        "document_type": DocumentType.recall,
        "document_id": "REG123",
        "title": "Halaven (eribulin)",
        "product_names": ["Halaven"],
        "active_ingredients": ["eribulin"],
        "manufacturers": ["Eisai Ltd"],
        "severity": Severity.class_2,
        "date_published": date(2026, 5, 1),
        "raw_metadata": {"batch_numbers": ["B001", "B002"]},
        "extracted_at": datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return NormalizedDocument(**defaults)  # type: ignore[arg-type]


class TestNormalizedContentHash:
    """Tests for NormalizedDocument.normalized_content_hash()."""

    def test_stable_across_raw_text_diff(self) -> None:
        """Identical parsed fields but different raw_text/extracted_at → same hash."""
        doc_a = _make_doc(
            raw_text="Full HTML page content with nonce abc123",
            extracted_at=datetime(2026, 5, 10, 9, 0, 0, tzinfo=timezone.utc),
        )
        doc_b = _make_doc(
            raw_text="Full HTML page content with nonce xyz789",
            extracted_at=datetime(2026, 5, 11, 14, 0, 0, tzinfo=timezone.utc),
        )
        assert doc_a.normalized_content_hash() == doc_b.normalized_content_hash()

    def test_changes_on_severity_change(self) -> None:
        """Changing severity produces a different hash."""
        doc_a = _make_doc(severity=Severity.class_2)
        doc_b = _make_doc(severity=Severity.class_3)
        assert doc_a.normalized_content_hash() != doc_b.normalized_content_hash()

    def test_stable_across_manufacturer_order(self) -> None:
        """Reordering manufacturers list does not change the hash (sorted before hashing)."""
        doc_a = _make_doc(manufacturers=["Eisai Ltd", "Cipla"])
        doc_b = _make_doc(manufacturers=["Cipla", "Eisai Ltd"])
        assert doc_a.normalized_content_hash() == doc_b.normalized_content_hash()

    def test_changes_on_manufacturer_set_change(self) -> None:
        """Adding a manufacturer produces a different hash."""
        doc_a = _make_doc(manufacturers=["Eisai Ltd"])
        doc_b = _make_doc(manufacturers=["Eisai Ltd", "Cipla"])
        assert doc_a.normalized_content_hash() != doc_b.normalized_content_hash()

    def test_includes_raw_metadata(self) -> None:
        """Changing a value in raw_metadata produces a different hash."""
        doc_a = _make_doc(raw_metadata={"batch_numbers": ["B001"]})
        doc_b = _make_doc(raw_metadata={"batch_numbers": ["B001", "B002"]})
        assert doc_a.normalized_content_hash() != doc_b.normalized_content_hash()

    def test_returns_64_char_hex(self) -> None:
        """Result is a 64-character lowercase hex string (SHA-256)."""
        h = _make_doc().normalized_content_hash()
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)
