# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Generic size, batch, and concurrency ceilings for the T2/T3 serving path.

These values historically originated as ChromaDB Cloud's free-tier quota
limits (RDR-005), and this module was created at rn3wo.2 to give the subset
the live PG-serving path actually reuses — chunkers, ``http_vector_client``,
T2/T3 ETL, catalog backfill, doctor / collection CLI commands — a home that
would survive Chroma's removal. It did: ``nexus.db.chroma_quotas`` was
DELETED at RDR-155 P4b P3 (2026-07-25) and this module needed one import
line changed, because it never depended on it.

They are generic paging / batching / size ceilings here, NOT Chroma-imposed
constraints. The Chroma-specific half — ``QuotaValidator`` and its error
hierarchy — died with that module and has no replacement: T3Database.put
enforces MAX_DOCUMENT_BYTES from here directly, and the chunkers cap at
SAFE_CHUNK_BYTES upstream.

Values are frozen at the 2026-02-28 free-tier snapshot for continuity at the
moment of rehoming. Bump them here independently going forward; there is no
longer anywhere else they could come from.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceLimits:
    """Size, batch, and concurrency ceilings for the PG-serving path.

    Was a field-for-field mirror of the deleted ``ChromaQuotas`` at the point
    of rehoming — see this module's docstring for the historical Chroma-Cloud
    provenance of these numbers. This is now their only definition.
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
