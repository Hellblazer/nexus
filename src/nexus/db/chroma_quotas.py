# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""ChromaDB Cloud quota constants, error hierarchy, and validator (RDR-005).

Single source of truth for all ChromaDB Cloud limits.
Source: https://docs.trychroma.com/cloud/quotas-limits
"""
from __future__ import annotations

from dataclasses import dataclass


# ── Quota constants ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ChromaQuotas:
    """All ChromaDB Cloud quota limits in one frozen dataclass.

    All values reflect the free-tier documented limits as of 2026-02-28.
    To accommodate an upgraded plan, instantiate with custom values and pass
    to QuotaValidator — see Alternative B in RDR-005 (deferred follow-on).
    """
    # Data size
    MAX_EMBEDDING_DIMENSIONS: int = 4_096
    MAX_DOCUMENT_BYTES: int = 16_384
    MAX_URI_BYTES: int = 256
    MAX_ID_BYTES: int = 128
    MAX_DB_NAME_BYTES: int = 128
    MAX_COLLECTION_NAME_BYTES: int = 128
    MAX_METADATA_KEY_BYTES: int = 36
    MAX_RECORD_METADATA_VALUE_BYTES: int = 4_096
    MAX_COLLECTION_METADATA_VALUE_BYTES: int = 256
    MAX_RECORD_METADATA_KEYS: int = 32
    MAX_COLLECTION_METADATA_KEYS: int = 32

    # Query & search
    MAX_QUERY_STRING_CHARS: int = 256
    MAX_WHERE_PREDICATES: int = 8
    MAX_QUERY_RESULTS: int = 300

    # Concurrency (per collection)
    MAX_CONCURRENT_READS: int = 10
    MAX_CONCURRENT_WRITES: int = 10

    # Scale
    MAX_RECORDS_PER_WRITE: int = 300
    MAX_RECORDS_PER_COLLECTION: int = 5_000_000
    MAX_COLLECTIONS_PER_ACCOUNT: int = 1_000_000


#: Singleton quota instance using free-tier limits.
QUOTAS = ChromaQuotas()


# ── Error hierarchy ───────────────────────────────────────────────────────────

class QuotaViolation(ValueError):
    """Base class for all ChromaDB Cloud quota violations.

    All subclasses carry ``field``, ``actual``, ``limit``, and ``hint``
    attributes for structured error reporting.
    """

    def __init__(self, field: str, actual: int | str, limit: int | str, hint: str = "") -> None:
        self.field = field
        self.actual = actual
        self.limit = limit
        self.hint = hint
        super().__init__(
            f"ChromaDB quota exceeded: {field} = {actual!r} exceeds limit {limit!r}"
            + (f". {hint}" if hint else "")
        )


class RecordTooLarge(QuotaViolation):
    """Raised when a record field (document, embedding, metadata) exceeds its size limit."""


class NameTooLong(QuotaViolation):
    """Raised when a name field (id, uri, collection name, db name, metadata key) is too long."""


class TooManyPredicates(QuotaViolation):
    """Raised when a ``where`` filter has more top-level predicates than allowed."""


class ResultsExceedLimit(QuotaViolation):
    """Raised when ``n_results`` exceeds the per-query result cap."""


class QueryStringTooLong(QuotaViolation):
    """Raised when a query string exceeds the character limit."""


# ── Validator ─────────────────────────────────────────────────────────────────

class QuotaValidator:
    """Pure validator for ChromaDB Cloud quota limits.

    No I/O, no ChromaDB imports — safe to instantiate and call in any context.
    All methods raise the appropriate ``QuotaViolation`` subclass on violation.
    """

    def __init__(self, quotas: ChromaQuotas = QUOTAS) -> None:
        self._q = quotas

    def validate_record(
        self,
        id: str,
        document: str,
        embedding: list[float] | None,
        metadata: dict,
        uri: str | None = None,
    ) -> None:
        """Validate a single record against all per-record quota limits.

        Raises the appropriate ``QuotaViolation`` subclass on the first
        violation found.  Call before any ``col.upsert()`` or ``col.add()``.
        """
        q = self._q

        # ID length
        id_bytes = len(id.encode())
        if id_bytes > q.MAX_ID_BYTES:
            raise NameTooLong(
                field="id",
                actual=id_bytes,
                limit=q.MAX_ID_BYTES,
                hint=f"Shorten the record ID (currently {id_bytes} bytes, max {q.MAX_ID_BYTES})",
            )

        # Document size
        doc_bytes = len(document.encode())
        if doc_bytes > q.MAX_DOCUMENT_BYTES:
            raise RecordTooLarge(
                field="document",
                actual=doc_bytes,
                limit=q.MAX_DOCUMENT_BYTES,
                hint=f"Chunk the document into smaller pieces (max {q.MAX_DOCUMENT_BYTES} bytes each)",
            )

        # URI length
        if uri is not None:
            uri_bytes = len(uri.encode())
            if uri_bytes > q.MAX_URI_BYTES:
                raise NameTooLong(
                    field="uri",
                    actual=uri_bytes,
                    limit=q.MAX_URI_BYTES,
                )

        # Embedding dimensions
        if embedding is not None:
            dims = len(embedding)
            if dims > q.MAX_EMBEDDING_DIMENSIONS:
                raise RecordTooLarge(
                    field="embedding_dimensions",
                    actual=dims,
                    limit=q.MAX_EMBEDDING_DIMENSIONS,
                    hint=f"Use a model with ≤{q.MAX_EMBEDDING_DIMENSIONS} dimensions",
                )

        # Metadata key count
        if len(metadata) > q.MAX_RECORD_METADATA_KEYS:
            raise RecordTooLarge(
                field="metadata_keys",
                actual=len(metadata),
                limit=q.MAX_RECORD_METADATA_KEYS,
            )

        # Per-key and per-value checks
        for key, value in metadata.items():
            key_bytes = len(str(key).encode())
            if key_bytes > q.MAX_METADATA_KEY_BYTES:
                raise NameTooLong(
                    field="metadata_key",
                    actual=key_bytes,
                    limit=q.MAX_METADATA_KEY_BYTES,
                    hint=f"Shorten metadata key {key!r} (max {q.MAX_METADATA_KEY_BYTES} bytes)",
                )
            if isinstance(value, str):
                val_bytes = len(value.encode())
                if val_bytes > q.MAX_RECORD_METADATA_VALUE_BYTES:
                    raise RecordTooLarge(
                        field=f"metadata_value[{key!r}]",
                        actual=val_bytes,
                        limit=q.MAX_RECORD_METADATA_VALUE_BYTES,
                    )

    def validate_collection_metadata(self, metadata: dict) -> None:
        """Validate collection-level metadata against Cloud limits."""
        q = self._q
        if len(metadata) > q.MAX_COLLECTION_METADATA_KEYS:
            raise RecordTooLarge(
                field="collection_metadata_keys",
                actual=len(metadata),
                limit=q.MAX_COLLECTION_METADATA_KEYS,
            )
        for key, value in metadata.items():
            if isinstance(value, str):
                val_bytes = len(value.encode())
                if val_bytes > q.MAX_COLLECTION_METADATA_VALUE_BYTES:
                    raise RecordTooLarge(
                        field=f"collection_metadata_value[{key!r}]",
                        actual=val_bytes,
                        limit=q.MAX_COLLECTION_METADATA_VALUE_BYTES,
                    )

    def validate_query(
        self,
        query_text: str | None,
        where: dict | None,
        n_results: int,
    ) -> None:
        """Validate query parameters before dispatching to ChromaDB.

        Raises ``ResultsExceedLimit`` if ``n_results > MAX_QUERY_RESULTS``.
        Raises ``TooManyPredicates`` if the ``where`` dict has too many top-level keys.
        Raises ``RecordTooLarge`` if ``query_text`` exceeds the character limit.

        Note on predicate counting: top-level keys are counted (interim approach).
        This is potentially permissive for compound ``$and``/``$or`` filters.
        Empirical API verification is pending (Open Question 3, RDR-005).
        """
        q = self._q

        if n_results > q.MAX_QUERY_RESULTS:
            raise ResultsExceedLimit(
                field="n_results",
                actual=n_results,
                limit=q.MAX_QUERY_RESULTS,
                hint=f"Reduce n_results to ≤{q.MAX_QUERY_RESULTS}",
            )

        if where is not None and len(where) > q.MAX_WHERE_PREDICATES:
            raise TooManyPredicates(
                field="where_predicates",
                actual=len(where),
                limit=q.MAX_WHERE_PREDICATES,
            )

        if query_text is not None and len(query_text) > q.MAX_QUERY_STRING_CHARS:
            raise QueryStringTooLong(
                field="query_text",
                actual=len(query_text),
                limit=q.MAX_QUERY_STRING_CHARS,
            )

    def validate_collection_name(self, name: str) -> None:
        """Validate a collection name against the 128-byte Cloud limit.

        The structural ChromaDB rules (3–63 chars, alphanumeric) are enforced
        separately by ``nexus.corpus.validate_collection_name``.  This method
        checks the Cloud-specific byte-length limit.
        """
        name_bytes = len(name.encode())
        if name_bytes > self._q.MAX_COLLECTION_NAME_BYTES:
            raise NameTooLong(
                field="collection_name",
                actual=name_bytes,
                limit=self._q.MAX_COLLECTION_NAME_BYTES,
                hint=f"Shorten the collection name (max {self._q.MAX_COLLECTION_NAME_BYTES} bytes)",
            )

    def validate_db_name(self, name: str) -> None:
        """Validate a database name against the 128-byte Cloud limit."""
        name_bytes = len(name.encode())
        if name_bytes > self._q.MAX_DB_NAME_BYTES:
            raise NameTooLong(
                field="db_name",
                actual=name_bytes,
                limit=self._q.MAX_DB_NAME_BYTES,
            )
