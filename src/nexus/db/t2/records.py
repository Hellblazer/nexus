# SPDX-License-Identifier: AGPL-3.0-or-later
"""Substrate-neutral T2 record types (nexus-i711w Stage 2, Phase 0).

WHY THIS MODULE EXISTS. The Http* stores that SURVIVE the SQLite retirement
imported their value types from the SQLite stores that do NOT — so deleting the
stores would have broken the twins at IMPORT TIME, before a single behavioural
test ran. T2 [21098]'s partition listed the twins under SURVIVE and the stores
under DELETE without noting the dependency between them; measuring the surface
before deleting is what surfaced it.

These are plain records and pure helpers with no connection, no substrate and no
I/O, so they belong to neither store. Same shape and same reason as
``taxonomy_compute.py``, which the RDR-158 P1 move extracted from
``catalog_taxonomy`` for exactly this purpose.

The originating modules RE-EXPORT these names, so the ~65 test files and the
in-tree callers that import them from the old paths keep working until those
modules are deleted.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AspectRecord:
    """A single document's extracted aspects.

    JSON-shaped fields (``experimental_datasets``,
    ``experimental_baselines``, ``extras``) are typed as Python
    list / dict here; the store handles serialization on write and
    deserialization on read.

    ``doc_id`` (RDR-108 Phase 1c): catalog tumbler identity for the
    source document.  After the PK migration, this is the primary key;
    ``collection`` and ``source_path`` are retained as denorm cache
    columns.  Empty string on legacy rows written before the migration.
    """

    collection: str
    source_path: str
    problem_formulation: str | None
    proposed_method: str | None
    experimental_datasets: list[str] = field(default_factory=list)
    experimental_baselines: list[str] = field(default_factory=list)
    experimental_results: str | None = None
    extras: dict = field(default_factory=dict)
    confidence: float | None = None
    extracted_at: str = ""
    model_version: str = ""
    extractor_name: str = ""
    # RDR-096 P2.1: persistent URI identity. ``None`` on legacy rows
    # written before P2.1 ships; populated for all writes after.
    source_uri: str | None = None
    # RDR-108 Phase 1c: catalog tumbler identity. Empty string on legacy
    # rows written before the PK migration.
    doc_id: str = ""
    # RDR-109 Phase 5: salient sentences (attention-guided-v1 extractor).
    # Empty list when the extractor was not run or returned no candidates.
    salient_sentences: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class QueueRow:
    """A claimed queue row passed to the worker. Frozen because the
    worker holds it across the extract → upsert → mark_done sequence
    without mutation.

    ``content`` is the document text captured at enqueue time. The
    MCP ``store_put`` path passes ``content=<full text>`` (the only
    moment the text is in scope before T3 commits); CLI ingest paths
    pass ``content=""`` because chunk-level scope only; those rows
    rely on the worker re-reading ``source_path`` from disk at
    extraction time. The worker prefers ``content`` over file read
    when non-empty.

    ``doc_id`` (nexus-tdgc / RDR-101 Phase 4) is the catalog identity
    of the source document. Captured at enqueue time so the worker
    can build a ``doc_id_lookup`` for the chroma reader without a
    second catalog round-trip. Empty string for legacy rows
    enqueued before the column was added; the worker treats empty
    ``doc_id`` as "fall back to source_path".
    """

    collection: str
    source_path: str
    content_hash: str
    content: str
    retry_count: int
    doc_id: str = ""


@dataclass
class HighlightRecord:
    """A document's DEVONthink-sourced highlight + mention notes (RDR-139 Layer E).

    ``doc_id`` is the catalog tumbler of the source document. ``source_uri`` is
    the ``x-devonthink-item://<uuid>`` identity. ``highlights_md`` /
    ``mentions_md`` are the markdown blobs from ``extract_record_highlights`` /
    ``extract_record_mentions`` (either may be empty).
    """

    doc_id: str
    source_uri: str
    collection: str
    highlights_md: str
    mentions_md: str
    ingested_at: str


#: The relevance_log retention horizon — THE single source for the sweep's
#: default. Rehomed from the deleted SQLite ``telemetry.py`` (nexus-i711w
#: Stage 2 sub-stage A); surviving consumers are ``T2Database.trim_telemetry``
#: and ``nx memory expire``. ``HttpTelemetryStore.expire_relevance_log``'s
#: literal ``days: int = 90`` default mirrors this number.
RELEVANCE_LOG_RETENTION_DAYS: int = 90


def _safe_json_list(s: str | None) -> list:
    if not s:
        return []
    try:
        v = json.loads(s)
    except (ValueError, TypeError):
        return []
    return v if isinstance(v, list) else []


def _safe_json_dict(s: str | None) -> dict:
    if not s:
        return {}
    try:
        v = json.loads(s)
    except (ValueError, TypeError):
        return {}
    return v if isinstance(v, dict) else {}


# Rehomed from the deleted SQLite memory_store.py (nexus-i711w Stage 2
# sub-stage A3): pure text escaping with no substrate. Its SQLite
# consumers (CatalogStore's FTS path, catalog_db) died with the terminal
# i711w deletion; it survives via the nexus.db.t2 re-export with tests
# pinning the escaping contract (tests/test_t2.py).
_FTS5_SPECIAL = set('-:()\'"^~.*+/,;?!#@$%&|\\<>[]{}=')


def _sanitize_fts5(query: str) -> str:
    """Escape a user-supplied query for FTS5 MATCH.

    Splits on whitespace and wraps any token that contains FTS5 special
    characters in double quotes, with internal double-quotes escaped as '""'.
    Plain tokens (letters and digits only) are passed through unchanged so
    that FTS5 AND-of-terms semantics and boolean operators (AND, OR, NOT)
    still work for well-formed queries.
    """
    tokens = query.split()
    parts: list[str] = []
    for token in tokens:
        if any(ch in _FTS5_SPECIAL for ch in token):
            escaped = token.replace('"', '""')
            parts.append(f'"{escaped}"')
        else:
            parts.append(token)
    return " ".join(parts)
