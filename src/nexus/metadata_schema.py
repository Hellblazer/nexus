# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Single source of truth for T3 chunk-row metadata (nexus-40t).

A chunk row carries the CHUNK keys of ``tables/chunk-metadata-keys.toml`` (``chunk_text_hash``,
``embedding_model``) and nothing else (RDR-236 Phase 4 Step 1, nexus-6pfgb.65). Every write the client
builds funnels through :func:`make_chunk_metadata` / :func:`normalize`, which drop any other key, and
:func:`validate` is the strict check that refuses one.

A document's own facts (title, tags, bibliography, provenance, lifecycle) live on its catalog entry; a
chunk's position in a document (spans, page, section labels) lives on the catalog manifest row. A chunker
returns those position fields beside the chunk row (:func:`position_fields`), and the manifest builder
reads them from there.

The function is **idempotent**: re-running it on output is a no-op.

Also see ``src/nexus/db/limits.py`` for the upstream quota that this module keeps us under.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import structlog

from nexus.tables import chunk_metadata_keys as _key_table

_log = structlog.get_logger(__name__)

__all__ = [
    "ALLOWED_TOP_LEVEL",
    "CONTENT_TYPES",
    "MAX_SAFE_TOP_LEVEL_KEYS",
    "MetadataSchemaError",
    "POSITION_FIELDS",
    "aligned_positions",
    "prepared_positions",
    "REWRITE_OWNED_KEYS",
    "make_chunk_metadata",
    "normalize",
    "position_fields",
    "positions_of_row",
    "rewrite_delete_keys",
    "validate",
]


# ── Schema ──────────────────────────────────────────────────────────────────

#: Top-level keys a client sends on a T3 chunk row: the CHUNK rows of ``tables/chunk-metadata-keys.toml``
#: (``chunk_text_hash`` and ``embedding_model``), the two keys the chunk's text and its embedding determine, so
#: every document that holds the chunk agrees on them. Everything else a chunk row once carried is a fact about
#: a document or a position in one: it lives on the catalog document (title, tags, bib, ttl, provenance) or on
#: the manifest row (spans, labels), and a shared row's copy was only its last writer's (RDR-236, Phase 4
#: Step 1, nexus-6pfgb.65). The engine's own ROW keys (quarantine, origin collection) are stamped there, never
#: sent. The table is the key list; none is kept here.
ALLOWED_TOP_LEVEL: frozenset[str] = _key_table.keys_of("CHUNK")

#: Allowed content_type values. Replaces the old overlapping pair
#: ``(store_type, category)``.
CONTENT_TYPES: frozenset[str] = frozenset({"code", "pdf", "markdown", "prose"})

#: Safety margin below the historical 32-key cap (:data:`~nexus.db.limits.
#: QUOTAS.MAX_RECORD_METADATA_KEYS`). Any write producing more than this
#: many keys raises :class:`MetadataSchemaError`. A chunk row carries the CHUNK keys of the key table
#: (two today), so the cap is a bound no client write approaches.
MAX_SAFE_TOP_LEVEL_KEYS: int = 32

#: Keys a FULL-REWRITE writer owns (nexus-y8xjh): every key it sends. The engine merges chunk metadata instead
#: of replacing it (nexus-w94eo), so a key a rewrite dropped would otherwise keep its stale stored value. With
#: the CHUNK-only schema both keys are always sent, so :func:`rewrite_delete_keys` names none for a client
#: batch; the retired document and position keys a pre-Step-1 row still holds are removed by the Step 3 sweep,
#: not by one document's re-index (the row is shared).
REWRITE_OWNED_KEYS: frozenset[str] = ALLOWED_TOP_LEVEL


def rewrite_delete_keys(metadatas: list[dict[str, Any]]) -> list[str]:
    """Return the ``delete_keys`` a full-rewrite writer sends with *metadatas*.

    A key in :data:`REWRITE_OWNED_KEYS` that is absent from ANY row of the batch
    is named. The engine strips named keys from each row's STORED metadata before
    merging the incoming row on top, so a row that does carry the key still
    writes it; only the rows that dropped it lose the stale value. This restores
    the pre-nexus-w94eo replace semantics for the keys the writer owns, without
    touching enrichment keys another writer set.

    Only for writers whose dict is the complete intended state of the row (the
    batch indexers). The streaming PDF stub is NOT one: its dict is deliberately
    partial and the post-pass fills the rest, so it must never send these.
    """
    absent: set[str] = set()
    for m in metadatas:
        absent |= REWRITE_OWNED_KEYS - m.keys()
    return sorted(absent)


#: Primitive value types accepted by ChromaDB metadata.
_PRIMITIVE_TYPES: tuple[type, ...] = (str, int, float, bool, type(None))


# ── Error ───────────────────────────────────────────────────────────────────


class MetadataSchemaError(ValueError):
    """Raised when a metadata dict violates :mod:`nexus.metadata_schema`."""


# ── Normalise ───────────────────────────────────────────────────────────────


def normalize(raw: dict[str, Any], *, content_type: str | None = None) -> dict[str, Any]:
    """Return a canonical metadata dict for a T3 chunk row: the CHUNK keys of *raw* and nothing else.

    1. When *content_type* is given, validate it against :data:`CONTENT_TYPES`. It is a writer's routing
       argument, not a key: a chunk row no longer carries it (RDR-236 Phase 4 Step 1; the content type is
       a fact about the document and lives on the catalog document).
    2. Keep only the keys in :data:`ALLOWED_TOP_LEVEL`. Every other key a caller sent (a document key, a
       position key, a legacy key, anything outside the key table) is dropped silently.

    The function never raises on unknown keys. The companion :func:`validate` performs the strict
    post-write check, and the function is idempotent.
    """
    if content_type is not None and content_type not in CONTENT_TYPES:
        raise ValueError(
            f"invalid content_type {content_type!r}; "
            f"expected one of {sorted(CONTENT_TYPES)}"
        )
    return {k: v for k, v in raw.items() if k in ALLOWED_TOP_LEVEL}


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


#: Content type a chunk written straight to a collection (a note) is stamped
#: with, by collection prefix. The ONE copy: ``T3Database.put`` and the note writer share it.
_PREFIX_CONTENT_TYPES: tuple[tuple[str, str], ...] = (
    ("code__", "code"),
    ("docs__", "prose"),
    ("rdr__", "markdown"),
    ("knowledge__", "prose"),
)


def chunk_content_type_for_collection(collection: str) -> str:
    """The chunk ``content_type`` for *collection*'s prefix; anything unlisted is ``prose``."""
    for prefix, content_type in _PREFIX_CONTENT_TYPES:
        if collection.startswith(prefix):
            return content_type
    return "prose"


# ── Position fields (RDR-236 Phase 4 Step 0, nexus-6pfgb.65) ────────────────

#: The POSITION keys of ``tables/chunk-metadata-keys.toml``: what one manifest position determines (a span in
#: the document, a page, a section label). They belong to the ``(doc, position)`` manifest row, not to the
#: shared chunk row; a test holds this tuple equal to the table.
POSITION_FIELDS: tuple[str, ...] = (
    "chunk_start_char",
    "chunk_end_char",
    "line_start",
    "line_end",
    "page_number",
    "section_title",
    "section_type",
)


def position_fields(
    *,
    chunk_start_char: int = 0,
    chunk_end_char: int = 0,
    line_start: int = 0,
    line_end: int = 0,
    page_number: int = 0,
    section_title: str = "",
    section_type: str = "",
) -> dict[str, Any]:
    """A chunker's position fields as their own dict, with :func:`make_chunk_metadata`'s defaults.

    A chunker returns this beside its chunk row. The manifest builder
    (``nexus.mcp_infra._manifest_chunk_rows``) reads positions from it, so it does not depend on the row
    carrying them. Until Step 1 the row still carries them too: the chunker passes the same dict to the
    factory as ``**positions``.
    """
    return {
        "chunk_start_char": chunk_start_char,
        "chunk_end_char": chunk_end_char,
        "line_start": line_start,
        "line_end": line_end,
        "page_number": page_number,
        "section_title": section_title,
        "section_type": section_type,
    }


def positions_of_row(meta: dict[str, Any]) -> dict[str, Any]:
    """The position fields a STORED row carries, plus its ``chunk_index`` when it has one.

    For a caller whose chunk metadata is a stored legacy row (the manifest heal and the backfill read T3
    rows; a re-embed hands the stored metadatas on). A row written since RDR-236 Phase 4 Step 1 carries
    none of these keys, and the result is then empty: :func:`nexus.mcp_infra._manifest_chunk_rows` sends no
    label keys for such a position, so the row's manifest entry stays unlabelled (legacy) rather than
    becoming an authoritative empty label. A producer that has its own position dict passes that instead.
    """
    out = {k: meta[k] for k in POSITION_FIELDS if k in meta}
    if "chunk_index" in meta:
        out["chunk_index"] = meta["chunk_index"]
    return out


def prepared_positions(prepared: "list[tuple]") -> "list[dict[str, Any]]":
    """The position dict of each ``(id, text, meta, positions)`` tuple a document chunker returned."""
    return [p[3] for p in prepared]


def aligned_positions(
    metadatas: "list[dict[str, Any]]", positions: "list[dict[str, Any]] | None",
) -> "list[dict[str, Any]]":
    """*positions* checked against *metadatas* (same length), or read off the rows when a caller handed none.

    The "none given" leg is for a caller whose metadatas are stored rows (a re-embed's store chains, the
    legacy hooks): :func:`positions_of_row` reads what the row carries, which for a row written since
    RDR-236 Phase 4 Step 1 is nothing.
    """
    if positions is None:
        return [positions_of_row(m) for m in metadatas]
    if len(positions) != len(metadatas):
        raise ValueError(f"{len(positions)} position dicts for {len(metadatas)} chunk rows")
    return positions


def make_chunk_metadata(
    *,
    content_type: str,
    chunk_text_hash: str,
    embedding_model: str,
) -> dict[str, Any]:
    """Build a chunk row's metadata: the CHUNK keys, routed through :func:`normalize`.

    A chunk row carries ``chunk_text_hash`` and ``embedding_model`` and nothing else (RDR-236 Phase 4
    Step 1, nexus-6pfgb.65). The document's title, tags, category, bibliographic fields, provenance and
    lifecycle live on its catalog entry; a chunk's spans, page and section labels live on its manifest row
    (the chunkers return them beside the row, :func:`position_fields`). The factory takes no argument for
    them, so a caller cannot write one by accident. *content_type* is validated and not written.

    Indexers never build chunk metadata dicts by hand: route through this factory.
    """
    return normalize(
        {"chunk_text_hash": chunk_text_hash, "embedding_model": embedding_model},
        content_type=content_type,
    )


# ── Expiry helper (replaces the dropped ``expires_at`` field) ────────────────


def chunk_ttl_days(value: Any) -> int | float | None:
    """A chunk row's ``ttl_days`` as a TTL that can lapse, else ``None`` (read it as permanent).

    A row's ``ttl_days`` is whatever a writer stored. Anything that is not a plain positive number
    (absent, ``0``, negative, a string, a list, a bool, NaN) or is past ``timedelta``'s range
    (``10**10``, ``inf``, ``1e300``) is not a TTL that has lapsed, and no reader may abort on it
    (nexus-6pfgb.15, nexus-njn1a). Every reader that does date arithmetic on a chunk row's
    ``ttl_days`` goes through here, so the rule is written once.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value <= 0 or value > timedelta.max.days:  # NaN, non-positive, past timedelta
        return None
    return value


def ttl_listing_label(ttl_days: Any, indexed_at: str) -> str:
    """The TTL column of a ``store list`` row: ``expires YYYY-MM-DD``, or ``permanent`` when the row
    carries no TTL that can lapse (see :func:`chunk_ttl_days`) or no ``indexed_at`` to count from."""
    days = chunk_ttl_days(ttl_days)
    if days is None or not indexed_at:
        return "permanent"
    try:
        return f"expires {(datetime.fromisoformat(indexed_at) + timedelta(days=days)).date().isoformat()}"
    except OverflowError:  # an expiry date past datetime's range (about 2.9M days on): it never lapses
        return "permanent"
    except ValueError:  # an unparseable stamp: the TTL is real but cannot be dated
        return f"ttl {days}d"


def is_expired(metadata: dict[str, Any], *, now_iso: str) -> bool:
    """Return ``True`` when *metadata* has elapsed its TTL.

    Replaces the previous ``where=expires_at < now`` filter. Computes
    expiry from ``indexed_at + ttl_days`` Python-side.

    ``None``/absent ``ttl_days`` is the canonical permanent sentinel
    (RDR-194 D5, nexus-tk070.p6b — matching ``nexus.memory.ttl_days`` /
    ``nexus.plans.ttl_days`` / ``nexus.frecency.ttl_days``'s CHECK-enforced
    contract). ``T3Database.put`` (and, until its retirement, ``HttpVectorClient.put``; fix-pass,
    nexus-24rof) REJECT an explicit ``ttl_days=0`` loudly at write
    time — 0 can no longer be freshly written by either write path. A
    ``ttl_days == 0`` reaching this function is therefore always LEGACY
    data (written before that fix, or by a caller outside those two
    methods — code/prose/pdf indexers route through
    :func:`make_chunk_metadata` directly and never validate ``ttl_days``,
    by design, see that factory's own comment). This function still
    treats a legacy ``0`` as permanent — reads are not writes, and there
    is no population-wide rewrite path for T3 chunk metadata (unlike the
    SQL ``nexus.frecency`` table's CHECK-backed migration) — but now logs
    a warning each time, making the legacy-compat path observable instead
    of fully silent. ``not ttl`` remains the single guard for both ``None``
    and ``0``; the warning is a side effect layered in front of it, not a
    logic change.
    """
    ttl = metadata.get("ttl_days")
    if ttl == 0:
        _log.warning(
            "frecency_legacy_ttl_days_zero_treated_as_permanent",
            hint=(
                "ttl_days=0 predates the None-sentinel migration (RDR-194 D5, "
                "nexus-tk070.p6b/nexus-24rof) and is legacy-compat-tolerated as "
                "permanent on read; T3Database.put rejects "
                "0 on write, so this metadata was written before that fix (or "
                "by a caller outside those two methods)"
            ),
        )
    days = chunk_ttl_days(ttl)
    if days is None:
        return False
    indexed_at = metadata.get("indexed_at", "")
    if not indexed_at:
        return False
    try:
        idx_dt = datetime.fromisoformat(indexed_at)
        now_dt = datetime.fromisoformat(now_iso)
        return (now_dt - idx_dt) >= timedelta(days=days)
    except (TypeError, ValueError, OverflowError):  # an unparseable or mixed naive/aware stamp, a ttl past timedelta
        return False
