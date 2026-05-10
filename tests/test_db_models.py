"""Tests for SQLAlchemy ORM models (no live DB required)."""

from __future__ import annotations

from datetime import date, datetime, timezone

from regulatory.models import DocumentType, NormalizedDocument, Severity


class TestDocumentFromNormalized:
    """Tests for Document.from_normalized() without a DB session."""

    def _make_normalized(self, **overrides: object) -> NormalizedDocument:
        """Build a minimal valid NormalizedDocument."""
        defaults: dict[str, object] = {
            "source_id": "openfda_drug",
            "source_url": "https://api.fda.gov/drug/enforcement.json?search=recall_number:X-001",
            "source_hash": "a" * 64,
            "jurisdiction": "US",
            "document_type": DocumentType.recall,
            "title": "Amoxicillin recall — subpotent",
            "product_names": ["Amoxicillin Capsules 500mg"],
            "active_ingredients": ["amoxicillin"],
            "active_ingredients_raw": ["amoxycillin"],
            "manufacturers": ["AfriPharma Ltd"],
            "marketing_authorization_holders": ["AfriPharma Ltd"],
            "severity": Severity.class_2,
            "date_published": date(2024, 1, 10),
            "date_effective": date(2024, 1, 1),
            "regions_affected": ["Nationwide", "Kenya"],
            "language": "en",
            "raw_text": "Full text of recall notice.",
            "raw_metadata": {"recall_number": "X-001"},
            "extracted_at": datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
        }
        defaults.update(overrides)
        return NormalizedDocument(**defaults)  # type: ignore[arg-type]

    def test_from_normalized_basic_fields(self) -> None:
        """from_normalized() maps all scalar fields correctly."""
        from regulatory.db.models import Document

        norm = self._make_normalized()
        doc = Document.from_normalized(norm)

        assert doc.source_id == "openfda_drug"
        assert doc.jurisdiction == "US"
        assert doc.document_type == "recall"
        assert doc.title == "Amoxicillin recall — subpotent"
        assert doc.severity == "class_2"
        assert doc.language == "en"
        assert doc.source_hash == "a" * 64

    def test_from_normalized_arrays(self) -> None:
        """from_normalized() preserves list fields."""
        from regulatory.db.models import Document

        norm = self._make_normalized()
        doc = Document.from_normalized(norm)

        assert doc.product_names == ["Amoxicillin Capsules 500mg"]
        assert doc.active_ingredients == ["amoxicillin"]
        assert doc.active_ingredients_raw == ["amoxycillin"]
        assert doc.manufacturers == ["AfriPharma Ltd"]
        assert doc.regions_affected == ["Nationwide", "Kenya"]

    def test_from_normalized_dates(self) -> None:
        """from_normalized() maps date fields."""
        from regulatory.db.models import Document

        norm = self._make_normalized()
        doc = Document.from_normalized(norm)

        assert doc.date_published == date(2024, 1, 10)
        assert doc.date_effective == date(2024, 1, 1)

    def test_from_normalized_raw_metadata(self) -> None:
        """from_normalized() preserves raw_metadata dict."""
        from regulatory.db.models import Document

        norm = self._make_normalized()
        doc = Document.from_normalized(norm)

        assert doc.raw_metadata == {"recall_number": "X-001"}

    def test_from_normalized_no_severity(self) -> None:
        """from_normalized() stores None when severity is None."""
        from regulatory.db.models import Document

        norm = self._make_normalized(severity=None)
        doc = Document.from_normalized(norm)

        assert doc.severity is None

    def test_from_normalized_no_document_id(self) -> None:
        """from_normalized() allows None document_id."""
        from regulatory.db.models import Document

        norm = self._make_normalized()
        doc = Document.from_normalized(norm)

        assert doc.document_id is None  # not set in defaults

    def test_model_classes_importable(self) -> None:
        """All ORM model classes import without error."""
        from regulatory.db.models import (
            Base,
            Document,
            DocumentVersion,
            FetchLog,
            Manufacturer,
        )

        assert Base is not None
        assert Document.__tablename__ == "documents"
        assert DocumentVersion.__tablename__ == "document_versions"
        assert FetchLog.__tablename__ == "fetch_log"
        assert Manufacturer.__tablename__ == "manufacturers"
