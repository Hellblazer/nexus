# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Single source of truth for T3 record metadata (nexus-40t).

Every write into ChromaDB funnels through :func:`normalize` so the key
set stays bounded and Chroma Cloud's 32-key ``MAX_RECORD_METADATA_KEYS``
quota is never breached. Every ``upsert``/``update`` then calls
:func:`validate` as a last-line guard that refuses to silently lose
fields.

Design:
  * :data:`ALLOWED_TOP_LEVEL` — the canonical schema. Keys are either
    read by a ``where=`` filter, consumed by scoring, or displayed to
    the user.
  * **Cargo keys** (``pdf_subject``, ``ast_chunked``, ``session_id``,
    etc.) are dropped by :func:`normalize` — they were written but
    never read.
  * **Consolidated keys**: the four ``git_*`` fields are packed into a
    single ``git_meta`` JSON string so that provenance is preserved at
    the cost of one metadata slot instead of four.

The function is **idempotent**: re-running it on output is a no-op.
Post-pass enrichers rely on this so they can merge additions with an
existing row without accreting keys.

Also see ``src/nexus/db/chroma_quotas.py`` for the upstream Chroma
quota that this module keeps us under.
"""
from __future__ import annotations

import json
from typing import Any

__all__ = [
    "ALLOWED_TOP_LEVEL",
    "CONTENT_TYPES",
    "MAX_SAFE_TOP_LEVEL_KEYS",
    "MetadataSchemaError",
    "make_chunk_metadata",
    "normalize",
    "validate",
]


# ── Schema ──────────────────────────────────────────────────────────────────

#: Top-level keys allowed on any T3 record. Populated by auditing every
#: ``where=`` filter, every ``meta.get(...)`` / ``metadata[...]`` read,
#: and every display formatter in the codebase.
ALLOWED_TOP_LEVEL: frozenset[str] = frozenset({
    # Identity (5)
    "source_path",
    "content_hash",
    "chunk_text_hash",
    "chunk_index",
    "chunk_count",
    # Spans (5)
    "chunk_start_char",
    "chunk_end_char",
    "line_start",
    "line_end",
    "page_number",
    # Display / user-facing (6) — ``source_title`` collapsed into
    # ``title`` (consumers already used ``source_title or title`` as a
    # fallback chain; one canonical field is simpler).
    "title",
    "source_author",
    "section_title",
    "section_type",
    "tags",
    "category",
    # Routing (4)
    "content_type",
    "store_type",
    "corpus",
    "embedding_model",
    # Bibliographic (filtered via where=bib_year; displayed in results) (4)
    "bib_year",
    "bib_authors",
    "bib_venue",
    "bib_citation_count",
    # Lifecycle / scoring (5) — ``expires_at`` removed; expiry is
    # derived from ``indexed_at + ttl_days`` Python-side. ``ttl_days=0``
    # is the "permanent" sentinel.
    "indexed_at",
    "ttl_days",
    "frecency_score",
    "source_agent",
    "session_id",
    # Consolidated provenance — JSON string, opaque to filters (1)
    "git_meta",
})

#: Allowed content_type values. Replaces the old overlapping pair
#: ``(store_type, category)``.
CONTENT_TYPES: frozenset[str] = frozenset({"code", "pdf", "markdown", "prose"})

#: Safety margin below Chroma's 32-key cap (:data:`~nexus.db.chroma_quotas.
#: QUOTAS.MAX_RECORD_METADATA_KEYS`). Any write producing more than this
#: many keys raises :class:`MetadataSchemaError`. The canonical schema
#: defined by :data:`ALLOWED_TOP_LEVEL` sits at 30 keys after the
#: ``source_title``/``expires_at`` removal — 2 keys of headroom above
#: that for future additions before the validation guard fires.
MAX_SAFE_TOP_LEVEL_KEYS: int = 31

#: Git provenance sub-keys — packed into ``git_meta`` as a JSON string.
_GIT_FIELD_MAP: dict[str, str] = {
    "git_project_name": "project",
    "git_branch": "branch",
    "git_commit_hash": "commit",
    "git_remote_url": "remote",
}

#: Bibliographic slots. All four are dropped together when every value
#: is the placeholder (``0`` or ``""``) — without ``--enrich`` they are
#: pure cargo eating metadata budget (nexus-2my fix #2). When at least
#: one slot is populated the full set rides along so the search/display
#: contract stays uniform.
_BIB_FIELDS: tuple[str, ...] = (
    "bib_year", "bib_authors", "bib_venue", "bib_citation_count",
)

#: Primitive value types accepted by ChromaDB metadata.
_PRIMITIVE_TYPES: tuple[type, ...] = (str, int, float, bool, type(None))


# ── Error ───────────────────────────────────────────────────────────────────


class MetadataSchemaError(ValueError):
    """Raised when a metadata dict violates :mod:`nexus.metadata_schema`."""


# ── Normalise ───────────────────────────────────────────────────────────────


def normalize(raw: dict[str, Any], *, content_type: str) -> dict[str, Any]:
    """Return a canonical metadata dict for a T3 record.

    Operations (in order):

    1. Validate ``content_type`` against :data:`CONTENT_TYPES`.
    2. Unpack any existing ``git_meta`` JSON blob so that subsequent
       updates to individual ``git_*`` keys still take effect (idempotent
       round-trip).
    3. Pack every populated ``git_*`` field into a single ``git_meta``
       JSON string (omit entirely when all four are empty, saving a slot).
    4. Drop cargo keys — anything outside :data:`ALLOWED_TOP_LEVEL`.
       Then drop the four ``bib_*`` slots together when every value is
       the placeholder (``0`` / ``""``) — consistent with the
       git_meta-omitted-when-empty pattern.
    5. Inject ``content_type`` so routing code has a single canonical
       field to read.

    The function never raises on unknown keys (cargo is silently dropped).
    The companion :func:`validate` performs the strict post-write check.
    """
    if content_type not in CONTENT_TYPES:
        raise ValueError(
            f"invalid content_type {content_type!r}; "
            f"expected one of {sorted(CONTENT_TYPES)}"
        )

    working = dict(raw)

    # Step 2: unpack existing git_meta so downstream updates can merge.
    if (blob := working.pop("git_meta", None)) and isinstance(blob, str):
        try:
            decoded = json.loads(blob)
        except json.JSONDecodeError:
            decoded = {}
        for raw_key, short_key in _GIT_FIELD_MAP.items():
            if short_key in decoded and raw_key not in working:
                working[raw_key] = decoded[short_key]

    # Step 3: repack git_* → git_meta.
    git_payload = {
        short_key: working.pop(raw_key)
        for raw_key, short_key in _GIT_FIELD_MAP.items()
        if working.get(raw_key)
    }
    # Remove any git_* keys that were zero/empty but still present.
    for raw_key in _GIT_FIELD_MAP:
        working.pop(raw_key, None)

    # Step 4: drop cargo.
    normalised = {k: v for k, v in working.items() if k in ALLOWED_TOP_LEVEL}

    # Step 4b: drop the bib_* placeholder set when every slot is empty.
    # When ``--enrich`` is off the indexer writes the four bib_* keys
    # with ``0`` / ``""`` defaults; without this filter they consume four
    # metadata slots for no payload (nexus-2my fix #2). Mirrors the
    # git_meta-omitted-when-empty pattern.
    if not any(normalised.get(field) for field in _BIB_FIELDS):
        for field in _BIB_FIELDS:
            normalised.pop(field, None)

    # Step 3 (cont.): write git_meta only when at least one field has
    # a truthy value.
    if git_payload:
        normalised["git_meta"] = json.dumps(
            git_payload, sort_keys=True, separators=(",", ":")
        )

    # Step 5: stamp content_type.
    normalised["content_type"] = content_type

    return normalised


# ── Validate ────────────────────────────────────────────────────────────────


def validate(metadata: dict[str, Any]) -> None:
    """Raise :class:`MetadataSchemaError` if *metadata* is not writeable.

    Enforced invariants:
      * Key count ≤ :data:`MAX_SAFE_TOP_LEVEL_KEYS`.
      * Every key lives in :data:`ALLOWED_TOP_LEVEL`.
      * Every value is a Chroma-primitive (``str``, ``int``, ``float``,
        ``bool``, or ``None``).

    Runs in the T3 write path (``upsert_chunks_with_embeddings``,
    ``update_chunks``). A violation fails the write loudly rather than
    letting Chroma Cloud silently drop the keys that happen to sort last.
    """
    if len(metadata) > MAX_SAFE_TOP_LEVEL_KEYS:
        raise MetadataSchemaError(
            f"too many metadata keys: {len(metadata)} > "
            f"{MAX_SAFE_TOP_LEVEL_KEYS} (Chroma cap 32). "
            f"Keys: {sorted(metadata.keys())}"
        )

    unknown = set(metadata) - ALLOWED_TOP_LEVEL
    if unknown:
        raise MetadataSchemaError(
            f"unknown metadata keys: {sorted(unknown)}. "
            f"Add to ALLOWED_TOP_LEVEL or route them through normalize()."
        )

    for key, value in metadata.items():
        if not isinstance(value, _PRIMITIVE_TYPES):
            raise MetadataSchemaError(
                f"non-primitive metadata value for {key!r}: "
                f"{type(value).__name__} — "
                f"ChromaDB only accepts str/int/float/bool/None"
            )


# ── Factory ─────────────────────────────────────────────────────────────────


def make_chunk_metadata(
    *,
    content_type: str,
    # Identity (always required)
    source_path: str,
    chunk_index: int,
    chunk_count: int,
    chunk_text_hash: str,
    content_hash: str,
    indexed_at: str,
    embedding_model: str,
    store_type: str,
    corpus: str = "",
    # Position (required where meaningful, default 0 elsewhere)
    chunk_start_char: int = 0,
    chunk_end_char: int = 0,
    line_start: int = 0,
    line_end: int = 0,
    page_number: int = 0,
    # Display
    title: str = "",
    source_author: str = "",
    section_title: str = "",
    section_type: str = "",
    tags: str = "",
    category: str = "",
    # Bibliographic (defaults dropped together by normalize when all-empty)
    bib_year: int = 0,
    bib_authors: str = "",
    bib_venue: str = "",
    bib_citation_count: int = 0,
    # Lifecycle
    ttl_days: int = 0,
    frecency_score: float = 0.0,
    source_agent: str = "nexus-indexer",
    session_id: str = "",
    # Provenance — flat git_* keys; normalize() packs them into git_meta JSON
    git_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a complete chunk metadata dict and route through
    :func:`normalize` so it's safe to write directly to T3.

    Every :data:`ALLOWED_TOP_LEVEL` key gets a value (either explicit
    or a documented default). Bib placeholders are dropped together
    when all-empty (see :func:`normalize`); ``git_meta`` is packed
    from the optional ``git_meta`` dict (flat ``{"project": ...}``
    short keys or ``{"git_project_name": ...}`` long keys both work).

    Indexers should never build chunk metadata dicts by hand — route
    through this factory so adding a new ``ALLOWED_TOP_LEVEL`` key
    is a single edit, not seven separate indexer changes.
    """
    raw: dict[str, Any] = {
        "source_path": source_path,
        "content_hash": content_hash,
        "chunk_text_hash": chunk_text_hash,
        "chunk_index": chunk_index,
        "chunk_count": chunk_count,
        "chunk_start_char": chunk_start_char,
        "chunk_end_char": chunk_end_char,
        "line_start": line_start,
        "line_end": line_end,
        "page_number": page_number,
        "title": title,
        "source_author": source_author,
        "section_title": section_title,
        "section_type": section_type,
        "tags": tags,
        "category": category,
        "store_type": store_type,
        "corpus": corpus,
        "embedding_model": embedding_model,
        "bib_year": bib_year,
        "bib_authors": bib_authors,
        "bib_venue": bib_venue,
        "bib_citation_count": bib_citation_count,
        "ttl_days": ttl_days,
        "indexed_at": indexed_at,
        "frecency_score": frecency_score,
        "source_agent": source_agent,
        "session_id": session_id,
    }
    if git_meta:
        # Accept both short keys ({"project", "branch", ...}) and long
        # keys ({"git_project_name", ...}) — normalize() repacks either.
        for k, v in git_meta.items():
            if k in _GIT_FIELD_MAP:
                raw[k] = v
            elif f"git_{k}_name" in _GIT_FIELD_MAP:
                raw[f"git_{k}_name"] = v
            elif k in {"project", "branch", "commit", "remote"}:
                # Short-key form from existing call sites.
                long = {v: k for k, v in _GIT_FIELD_MAP.items()}[k]
                raw[long] = v
            else:
                raw[k] = v  # let normalize handle / drop unknown
    return normalize(raw, content_type=content_type)


# ── Expiry helper (replaces the dropped ``expires_at`` field) ────────────────


def is_expired(metadata: dict[str, Any], *, now_iso: str) -> bool:
    """Return ``True`` when *metadata* has elapsed its TTL.

    Replaces the previous ``where=expires_at < now`` filter. Computes
    expiry from ``indexed_at + ttl_days`` Python-side. ``ttl_days == 0``
    is the permanent sentinel — never expires regardless of indexed_at.
    """
    ttl = metadata.get("ttl_days", 0)
    if not ttl or ttl <= 0:
        return False
    indexed_at = metadata.get("indexed_at", "")
    if not indexed_at:
        return False
    from datetime import datetime, timedelta
    try:
        idx_dt = datetime.fromisoformat(indexed_at)
        now_dt = datetime.fromisoformat(now_iso)
    except (TypeError, ValueError):
        return False
    return (now_dt - idx_dt) >= timedelta(days=ttl)
