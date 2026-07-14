# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Generic size, batch, and concurrency ceilings for the T2/T3 serving path.

These values historically originated as ChromaDB Cloud's free-tier quota
limits (see the now Chroma-scoped ``nexus.db.chroma_quotas``, RDR-005). RDR-
155 P4b retires the Chroma backend entirely; this module is the pgvector-
neutral home for the subset of those numbers that the live PG-serving path
(chunkers, ``http_vector_client``, T2/T3 ETL, catalog backfill, doctor /
collection CLI commands) reuses as generic paging / batching / size
ceilings — NOT as Chroma-imposed constraints. This reuse was already
pre-authorized by ``chroma_quotas``'s own docstring; this module simply
gives it a home that survives ``chroma_quotas.py``'s eventual deletion.

``nexus.db.chroma_quotas`` keeps the Chroma-specific ``QuotaValidator`` and
its error hierarchy, scoped to the retiring migration read leg
(``nexus.migration.chroma_read`` / ``vector_etl`` / ``collision_audit``);
those die together with that module in RDR-155 P4b (bead nexus-g37fr).

Values are frozen at the same 2026-02-28 free-tier snapshot as
``chroma_quotas.ChromaQuotas`` for continuity at the moment of rehoming
(bead nexus-rn3wo.2). This module has NO runtime dependency on
``chroma_quotas`` and must never gain one — that is the entire point of the
rehome: it must survive ``chroma_quotas.py``'s deletion.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceLimits:
    """Size, batch, and concurrency ceilings for the PG-serving path.

    Field-for-field mirror of ``chroma_quotas.ChromaQuotas`` at the point of
    rehoming — see this module's docstring for the historical Chroma-Cloud
    provenance of these numbers. Bump values here independently going
    forward; do not import them from ``chroma_quotas``.
    """
    # Data size
    MAX_EMBEDDING_DIMENSIONS: int = 4_096
    MAX_DOCUMENT_BYTES: int = 16_384
    SAFE_CHUNK_BYTES: int = 12_288  # Target cap for all chunkers (4KB below MAX_DOCUMENT_BYTES)
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


#: Singleton limits instance.
QUOTAS = ServiceLimits()
SAFE_CHUNK_BYTES: int = QUOTAS.SAFE_CHUNK_BYTES
MAX_QUERY_RESULTS: int = QUOTAS.MAX_QUERY_RESULTS
