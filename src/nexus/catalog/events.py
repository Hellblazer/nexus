# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""RDR-101 Phase 1: typed event schemas for the catalog event log.

The catalog/T3 architecture is an append-only event log; SQLite (catalog)
and T3 (vector index) are deterministic projections. This module defines
the event envelope and the 12 typed payloads listed in RDR-101 §"Event
log".

Envelope (RF-101-2): ``{type, v, payload, ts}``. ``v`` is the schema
version of the payload: 0 is reserved for synthesized v: 0 events that
project the existing JSONL state; 1 is the native write path. The
projector dispatches on ``(type, v)`` pairs; unknown pairs log a
structured warning and skip.

Payloads are frozen dataclasses with explicit fields so projector code
can pattern-match on attribute presence rather than dict-key probing.
``Event.from_dict`` filters unknown payload keys for forward compat —
the projector never crashes on a future field — but ``Event.from_dict``
keeps unknown event types as opaque dicts so the projector sees them and
emits the warning RF-101-2 specifies.

UUID7 generation uses the stdlib ``uuid.uuid7()`` on Python 3.14+ and
``uuid7-standard`` (``uuid7.create()``) on 3.13 per the chunk-id rule
deliverable (``docs/rdr/post-mortem/rdr-101-chunk-id-rule.md``). The
older ``uuid7`` package on PyPI uses pre-RFC 9562 encoding and is
explicitly NOT what we want.

The bib-related ``DocumentEnrichedPayload`` schema version literals
follow the bib disposition deliverable
(``docs/rdr/post-mortem/rdr-101-bib-disposition.md``): ``bib-s2-v1``
and ``bib-openalex-v1`` for Semantic Scholar / OpenAlex enrichment;
``scholarly-paper-v1`` for aspect-extraction enrichment.

This module is write-side only; the projector in
``catalog/projector.py`` consumes events.
"""

from __future__ import annotations

import dataclasses
import sys
import uuid
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any

# ── UUID7 generator ──────────────────────────────────────────────────────

if sys.version_info >= (3, 14):
    def _uuid7() -> uuid.UUID:
        return uuid.uuid7()  # type: ignore[attr-defined]
else:
    import uuid7 as _uuid7_pkg

    def _uuid7() -> uuid.UUID:
        return _uuid7_pkg.create()


def new_doc_id() -> str:
    """Fresh UUID7 (RFC 9562) for ``DocumentRegistered`` (RF-101-1)."""
    return str(_uuid7())


def new_chunk_id() -> str:
    """Fresh UUID7 for ``ChunkIndexed`` per the chunk-id rule deliverable.

    Note: Phase 1 synthesis (``ChunkIndexed`` v: 0 from existing T3 state)
    copies the existing Chroma natural ID verbatim and does NOT call this
    factory. Use this only for native v: 1 writes.
    """
    return str(_uuid7())


def now_ts() -> str:
    """ISO-8601 UTC timestamp suitable for the envelope ``ts`` field."""
    return datetime.now(timezone.utc).isoformat()


# ── Event type names ─────────────────────────────────────────────────────

TYPE_OWNER_REGISTERED = "OwnerRegistered"
TYPE_COLLECTION_CREATED = "CollectionCreated"
TYPE_COLLECTION_SUPERSEDED = "CollectionSuperseded"
TYPE_DOCUMENT_REGISTERED = "DocumentRegistered"
TYPE_DOCUMENT_RENAMED = "DocumentRenamed"
TYPE_DOCUMENT_ALIASED = "DocumentAliased"
TYPE_DOCUMENT_ENRICHED = "DocumentEnriched"
TYPE_DOCUMENT_DELETED = "DocumentDeleted"
TYPE_CHUNK_INDEXED = "ChunkIndexed"
TYPE_CHUNK_ORPHANED = "ChunkOrphaned"
TYPE_LINK_CREATED = "LinkCreated"
TYPE_LINK_DELETED = "LinkDeleted"

ALL_EVENT_TYPES: frozenset[str] = frozenset(
    {
        TYPE_OWNER_REGISTERED,
        TYPE_COLLECTION_CREATED,
        TYPE_COLLECTION_SUPERSEDED,
        TYPE_DOCUMENT_REGISTERED,
        TYPE_DOCUMENT_RENAMED,
        TYPE_DOCUMENT_ALIASED,
        TYPE_DOCUMENT_ENRICHED,
        TYPE_DOCUMENT_DELETED,
        TYPE_CHUNK_INDEXED,
        TYPE_CHUNK_ORPHANED,
        TYPE_LINK_CREATED,
        TYPE_LINK_DELETED,
    }
)

# DocumentEnriched payload schema versions (RDR-101 Phase 0 bib disposition).
SCHEMA_BIB_S2_V1 = "bib-s2-v1"
SCHEMA_BIB_OPENALEX_V1 = "bib-openalex-v1"
SCHEMA_SCHOLARLY_PAPER_V1 = "scholarly-paper-v1"


# ── Typed payloads ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OwnerRegisteredPayload:
    """Owner registration. ``owner_id`` is the tumbler prefix (e.g. ``1.7``).

    RF-101-4 keeps tumblers as the user-facing identity; ``owner_id`` here
    is the same string already stored in ``owners.jsonl``. RDR-101 §Entities
    uses ``uuid7 owner_id`` in the ER diagram, but the migration path keeps
    tumbler-as-owner-id through Phase 5 to avoid breaking every catalog
    caller; the field type is a string and Phase 1 never parses it.
    """

    owner_id: str
    name: str
    owner_type: str  # "repo" | "curator"
    repo_root: str = ""
    repo_hash: str = ""
    description: str = ""


@dataclass(frozen=True, slots=True)
class CollectionCreatedPayload:
    """A new ChromaDB collection.

    ``coll_id`` is the canonical identifier. Per RDR-101 §"Collection naming
    and invariants" the canonical form is
    ``<content_type>__<owner_id>__<embedding_model>@<model_version>``; legacy
    grandfathered names (e.g. ``code__ART-8c2e74c0``) are also valid coll_id
    values and Phase 1 stores them verbatim. The four split-out fields make
    the invariants pattern-matchable at projection time without parsing the
    composite name.
    """

    coll_id: str
    owner_id: str
    content_type: str
    embedding_model: str
    model_version: str
    name: str = ""  # display name; defaults to coll_id


@dataclass(frozen=True, slots=True)
class CollectionSupersededPayload:
    """One collection replaced by another (re-embed, rename, grandfather)."""

    old_coll_id: str
    new_coll_id: str
    reason: str = ""


@dataclass(frozen=True, slots=True)
class DocumentRegisteredPayload:
    """A new document registered in the catalog.

    ``doc_id`` is a fresh UUID7 (RF-101-1). ``source_uri`` is the canonical
    URI; for files it is the ``file://`` form. ``coll_id`` records which
    collection the document's chunks live in.

    The trailing block of fields preserves the existing tumbler-keyed
    ``documents`` row schema during the Phase 1 transition: v: 0 events
    synthesized from ``documents.jsonl`` populate them so the projector
    can produce a SQLite state byte-equal to today's ``Catalog.rebuild()``
    output (replay-equality test). v: 1 native writes (Phase 3+) leave
    them empty and rely on canonical fields plus separate projections
    (Provenance, Frecency, Aspect) for the rest. Phase 5 drops the
    legacy columns from the SQLite schema; the events keep them so the
    log is replayable into older schemas during a downgrade.
    """

    doc_id: str
    owner_id: str
    content_type: str
    source_uri: str
    coll_id: str
    title: str = ""
    source_mtime: float = 0.0
    indexed_at_doc: str = ""
    # ── Legacy tumbler-keyed schema fields (Phase 1 transition) ────────────
    tumbler: str = ""
    author: str = ""
    year: int = 0
    file_path: str = ""
    corpus: str = ""
    physical_collection: str = ""
    chunk_count: int = 0
    head_hash: str = ""
    indexed_at: str = ""
    alias_of: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DocumentRenamedPayload:
    """Same-owner rename of a document's source URI.

    Cross-owner moves use ``DocumentDeleted + DocumentRegistered`` per
    RDR-101 §"File rename (cross-owner)".

    ``tumbler`` is the legacy join key the v: 0 projector uses to find
    the SQLite row to UPDATE. Pre-fix the projector used ``doc_id`` as
    the WHERE clause; with Phase 2's ``mint_doc_id=True`` that's a
    UUID7 and the tumbler-keyed schema would silently drop the rename.
    Mirrors the same fix already in ``DocumentDeletedPayload``. Optional
    with empty default so v: 1 native writes don't have to populate it.
    """

    doc_id: str
    new_source_uri: str
    tumbler: str = ""


@dataclass(frozen=True, slots=True)
class DocumentAliasedPayload:
    """Mark ``alias_doc_id`` as an alias of ``canonical_doc_id``.

    Aliases survive forever in the projection; ``Catalog.resolve()`` walks
    the alias chain transparently. T3 chunks for the alias are not removed.
    """

    alias_doc_id: str
    canonical_doc_id: str


@dataclass(frozen=True, slots=True)
class DocumentEnrichedPayload:
    """Aspect / bibliographic enrichment of a document.

    ``schema_version`` selects the projection rule. The payload dict's
    keys depend on the schema version:

    - ``bib-s2-v1``: ``{semantic_scholar_id, doi, year, authors, venue,
      citation_count, references}``
    - ``bib-openalex-v1``: ``{openalex_id, doi, year, authors, venue,
      citation_count, references}``
    - ``scholarly-paper-v1``: a structured aspect record (RDR-089).

    The projector reads the payload, picks columns to write onto
    ``Document`` (bib fields) or onto a separate ``Aspect`` row, and
    leaves unknown keys alone (forward compat).
    """

    doc_id: str
    schema_version: str
    payload: dict[str, Any] = field(default_factory=dict)
    enriched_at: str = ""


@dataclass(frozen=True, slots=True)
class DocumentDeletedPayload:
    """Soft-delete tombstone. Chunks remain in T3 until ``nx t3 gc`` collects.

    ``tumbler`` is the legacy join key the v: 0 projector uses to find the
    SQLite row to delete. When ``mint_doc_id=False`` (Phase 1 doctor verb)
    ``doc_id`` IS the tumbler and the projector can use either; when
    ``mint_doc_id=True`` (Phase 2 ``synthesize-log``) ``doc_id`` is a
    UUID7 and ``tumbler`` is the only join key the tumbler-keyed schema
    can match. Optional with empty default so v: 1 native writes don't
    have to populate it.
    """

    doc_id: str
    reason: str = ""
    tumbler: str = ""


@dataclass(frozen=True, slots=True)
class ChunkIndexedPayload:
    """One chunk written to T3.

    ``chunk_id`` is the Chroma natural ID. For native v: 1 writes it is a
    fresh UUID7 (per the chunk-id rule deliverable). For synthesized v: 0
    events emitted from existing T3 state, the legacy
    ``f"{content_hash[:16]}_{chunk_index}"`` shape is copied verbatim.
    Either way the projector treats ``chunk_id`` as opaque.

    ``synthesized_orphan`` flags chunks that the Phase 2
    ``synthesize-log --chunks`` walker could not resolve to a document
    via the source_path → source_uri → tumbler → doc_id chain or via
    the title fallback (Phase 0 ``CHROMA_IDENTITY_FIELD`` pattern).
    Such chunks are emitted with ``doc_id=""`` and
    ``synthesized_orphan=True`` so the doctor verb (PR δ) can report
    them rather than the GC silently collecting them after the
    orphan window.
    """

    chunk_id: str
    chash: str
    doc_id: str
    coll_id: str
    position: int
    content_hash: str = ""
    embedded_at: str = ""
    synthesized_orphan: bool = False


@dataclass(frozen=True, slots=True)
class ChunkOrphanedPayload:
    """A chunk has been (or is about to be) deleted from T3.

    Per RF-101-3, ``ChunkOrphaned`` is emitted exclusively by ``nx t3 gc``
    immediately before the corresponding Chroma ``delete()`` call.
    """

    chunk_id: str
    reason: str = ""


@dataclass(frozen=True, slots=True)
class LinkCreatedPayload:
    """A directed link between two documents.

    ``from_doc`` and ``to_doc`` are tumbler strings during Phase 1
    (matching the existing ``links.jsonl`` ``from_t``/``to_t`` shape).
    Phase 3+ may carry ``doc_id`` UUID7 values; the projector chooses the
    join column at read time.

    ``created_at`` and ``meta`` preserve fields the existing
    ``links.jsonl`` carries so v: 0 synthesis can reproduce the
    tumbler-keyed ``links`` SQLite row.
    """

    from_doc: str
    to_doc: str
    link_type: str
    span_chash: str = ""
    creator: str = ""
    from_span: str = ""  # legacy positional span (RDR-053); empty for chash-spanned links
    to_span: str = ""
    created_at: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LinkDeletedPayload:
    """Tombstone for a link. Composite key ``(from_doc, to_doc, link_type)``."""

    from_doc: str
    to_doc: str
    link_type: str
    reason: str = ""


_PAYLOAD_CLASSES: dict[str, type] = {
    TYPE_OWNER_REGISTERED: OwnerRegisteredPayload,
    TYPE_COLLECTION_CREATED: CollectionCreatedPayload,
    TYPE_COLLECTION_SUPERSEDED: CollectionSupersededPayload,
    TYPE_DOCUMENT_REGISTERED: DocumentRegisteredPayload,
    TYPE_DOCUMENT_RENAMED: DocumentRenamedPayload,
    TYPE_DOCUMENT_ALIASED: DocumentAliasedPayload,
    TYPE_DOCUMENT_ENRICHED: DocumentEnrichedPayload,
    TYPE_DOCUMENT_DELETED: DocumentDeletedPayload,
    TYPE_CHUNK_INDEXED: ChunkIndexedPayload,
    TYPE_CHUNK_ORPHANED: ChunkOrphanedPayload,
    TYPE_LINK_CREATED: LinkCreatedPayload,
    TYPE_LINK_DELETED: LinkDeletedPayload,
}


def payload_class(event_type: str) -> type | None:
    """Return the payload dataclass for ``event_type``, or None if unknown."""
    return _PAYLOAD_CLASSES.get(event_type)


# ── Envelope ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Event:
    """Envelope for an event log entry: ``{type, v, payload, ts}``.

    ``type`` is one of the ``TYPE_*`` constants.

    ``v`` is the schema version for ``payload``: 0 for synthesized events
    (Phase 1 v: 0 projector path that materializes the existing JSONL log
    as events), 1 for native writes from Phase 3 onward.

    ``payload`` is one of the typed ``*Payload`` dataclasses, or, when an
    event arrived with an unknown ``type``, a raw dict. The projector
    handles both shapes.

    ``ts`` is an ISO-8601 UTC timestamp, populated at append time.
    """

    type: str
    v: int
    payload: Any
    ts: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSONL writing.

        Frozen-dataclass payloads are converted via ``dataclasses.asdict``
        so they round-trip through JSON. Dict payloads pass through.
        """
        if dataclasses.is_dataclass(self.payload):
            payload_dict = dataclasses.asdict(self.payload)
        elif isinstance(self.payload, dict):
            payload_dict = self.payload
        else:
            raise TypeError(
                f"Event.payload must be a dataclass or dict, got {type(self.payload).__name__}"
            )
        return {
            "type": self.type,
            "v": self.v,
            "payload": payload_dict,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        """Parse a JSONL line back into an Event.

        For known event types, the payload dict is filtered to declared
        fields and constructed into the typed dataclass; unknown payload
        keys are dropped silently (forward-compat). For unknown event
        types, the payload is preserved as a raw dict so the projector
        can emit the RF-101-2 unknown-(type, v) warning and skip.

        Defensive against malformed line shapes:
        - ``v`` that does not coerce to int falls back to 0 (synthesized);
          a hard error here would propagate out of ``EventLog.replay`` and
          abort the whole iterator on a single bad line.
        - ``payload`` that is not a dict (list, int, null, etc.) is
          treated as an empty dict so ``payload_raw.keys()`` does not
          raise ``AttributeError``. The resulting Event has an empty
          payload, which the projector handles via its unknown-key path.
        """
        type_ = d["type"]
        try:
            v = int(d.get("v", 0))
        except (TypeError, ValueError):
            v = 0
        payload_raw = d.get("payload")
        if not isinstance(payload_raw, dict):
            payload_raw = {}
        ts = d.get("ts", "")

        cls_ = payload_class(type_)
        if cls_ is None:
            return cls(type=type_, v=v, payload=dict(payload_raw), ts=ts)

        valid_fields = {f.name for f in fields(cls_)}
        filtered = {k: payload_raw[k] for k in payload_raw.keys() & valid_fields}
        return cls(type=type_, v=v, payload=cls_(**filtered), ts=ts)


# ── Convenience constructors ─────────────────────────────────────────────


def make_event(payload: Any, *, v: int = 0, ts: str | None = None) -> Event:
    """Build an Event envelope from a typed payload dataclass.

    Looks up the type name from ``payload``'s class so callers don't have
    to repeat themselves. ``v`` defaults to ``0`` (synthesized / Phase 1
    schema): every v: 0 dispatch handler is implemented, so the default
    is the safe, observable path. Pass ``v=1`` once a Phase 3+ writer is
    wired against a v: 1 dispatch handler that does something other than
    raise — until then, v: 1 events have no projector landing site and
    the projector raises ``NotImplementedError`` to surface the gap.

    ``ts`` defaults to now.
    """
    type_ = _CLASS_TO_TYPE.get(type(payload))
    if type_ is None:
        raise ValueError(
            f"Unknown payload class: {type(payload).__name__}. "
            f"Use one of: {sorted(c.__name__ for c in _CLASS_TO_TYPE)}"
        )
    return Event(type=type_, v=v, payload=payload, ts=ts or now_ts())


_CLASS_TO_TYPE: dict[type, str] = {v: k for k, v in _PAYLOAD_CLASSES.items()}
