"""Abstract base class for all regulatory source adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime

from regulatory.models import DocumentRef, DocumentType, NormalizedDocument, RawDocument


class RegulatorySource(ABC):
    """Abstract adapter that every regulatory data source must implement.

    Subclasses are registered via the ``@register_source`` decorator so the
    scheduler can iterate them without hardcoded lists.

    Attributes:
        source_id: Unique machine-readable identifier, e.g. ``"openfda_drug"``.
        jurisdiction: ISO-3166 alpha-2 country code, ``"EU"``, or ``"GLOBAL"``.
        document_types: Which ``DocumentType`` values this source produces.
    """

    source_id: str
    jurisdiction: str
    document_types: list[DocumentType]

    @abstractmethod
    async def discover(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentRef]:
        """Yield lightweight document references newer than *since*.

        Args:
            since: Only yield documents published after this timestamp.
                ``None`` means fetch everything available.

        Yields:
            :class:`~regulatory.models.DocumentRef` instances for each
            document found.
        """
        # mypy requires the body to be yield-capable when the return type
        # is AsyncIterator.  The pragma keeps the abstract stub valid.
        raise NotImplementedError  # pragma: no cover
        yield  # make Python recognise this as an async generator  # type: ignore[misc]

    @abstractmethod
    async def fetch(self, ref: DocumentRef) -> RawDocument:
        """Download the full content for a document reference.

        Implementations should use the shared :class:`~regulatory.ingestion.http.HttpClient`
        so rate-limiting and retry logic is centralised.

        Args:
            ref: The reference returned by :meth:`discover`.

        Returns:
            A :class:`~regulatory.models.RawDocument` containing raw bytes
            and a pre-computed sha256 hash.
        """
        ...

    @abstractmethod
    def parse(self, raw: RawDocument) -> NormalizedDocument:
        """Extract structured fields from raw content.

        Args:
            raw: The raw document returned by :meth:`fetch`.

        Returns:
            A fully populated :class:`~regulatory.models.NormalizedDocument`.
        """
        ...
