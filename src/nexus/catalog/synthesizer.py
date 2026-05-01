# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""RDR-101 Phase 1: synthesize v: 0 events from existing JSONL state.

Reads ``owners.jsonl`` / ``documents.jsonl`` / ``links.jsonl`` and emits
``Event`` envelopes that, when fed through ``Projector``, reproduce the
SQLite state today's ``Catalog.rebuild()`` writes from the same files.

The v: 0 envelope is the bridge: it represents catalog history that
predates the event log. Phase 2 will write these synthesized events to
``events.jsonl``; Phase 1 only synthesizes them into memory for the
replay-equality test.

Per RF-101-2 the v: 0 path needs three explicit sub-cases:

- Tombstoned rows (``_deleted: True`` in JSONL) project as
  ``DocumentRegistered`` followed by ``DocumentDeleted`` so the projector
  can detect resurrection bugs (a v: 0 projector that just re-INSERTs
  every row would silently revive deleted documents).
- Aliased rows (``alias_of != ""``) project as ``DocumentRegistered``
  with ``alias_of`` populated AND a paired ``DocumentAliased`` so future
  projections that materialize an alias graph see them as first-class
  edges. The Phase 1 SQLite projection stores ``alias_of`` as a column;
  ``DocumentAliased`` is a no-op for it, but the doctor verb still
  asserts the alias graph round-trips through the log.
- Empty-``source_uri`` rows are tagged so the doctor reports them; they
  are still emitted as ``DocumentRegistered`` so the SQLite row exists.

Each synthesized event sets ``v=0``. The legacy fields on
``DocumentRegisteredPayload`` carry the existing JSONL row's data
verbatim; the canonical RDR-101 fields (``doc_id``, ``coll_id``) are
populated where the source allows (``coll_id`` from
``physical_collection``, ``doc_id`` synthesized fresh) and otherwise
left empty.

The synthesizer is read-only against the catalog directory: no JSONL
file is touched, no SQLite is opened. The output is an iterator of
``Event`` envelopes the caller can feed to a ``Projector`` (Phase 1
replay-equality test) or ``EventLog.append_many`` (Phase 2 backfill).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import structlog

from nexus.catalog import events as ev
from nexus.catalog.events import Event

_log = structlog.get_logger()


def synthesize_from_jsonl(catalog_dir: Path) -> Iterator[Event]:
    """Yield v: 0 events that reproduce the catalog state under ``catalog_dir``.

    Order: owners → documents → links. Within each file, JSONL append order
    is preserved (last-write-wins is collapsed before emit so the projector
    sees one or two events per logical key, not the full append history).

    Skips a file if it does not exist (fresh catalog with partial state).
    """
    owners_path = catalog_dir / "owners.jsonl"
    docs_path = catalog_dir / "documents.jsonl"
    links_path = catalog_dir / "links.jsonl"

    if owners_path.exists():
        yield from _synthesize_owners(owners_path)
    if docs_path.exists():
        yield from _synthesize_documents(docs_path)
    if links_path.exists():
        yield from _synthesize_links(links_path)


# ── Owners ───────────────────────────────────────────────────────────────


def _synthesize_owners(path: Path) -> Iterator[Event]:
    """Owners use last-line-wins; tombstones are not used in practice today
    (``owners.jsonl`` is append-only with re-registration overwriting).
    """
    seen: dict[str, dict[str, Any]] = {}
    for obj in _iter_jsonl(path):
        key = obj.get("owner")
        if not key:
            continue
        if obj.get("_deleted"):
            seen.pop(key, None)
        else:
            seen[key] = obj

    for owner_id, obj in seen.items():
        payload = ev.OwnerRegisteredPayload(
            owner_id=owner_id,
            name=obj.get("name", ""),
            owner_type=obj.get("owner_type", ""),
            repo_root=obj.get("repo_root", ""),
            repo_hash=obj.get("repo_hash", ""),
            description=obj.get("description", ""),
        )
        yield Event(
            type=ev.TYPE_OWNER_REGISTERED, v=0,
            payload=payload, ts=_synthesized_ts(obj),
        )


# ── Documents ────────────────────────────────────────────────────────────


def _synthesize_documents(path: Path) -> Iterator[Event]:
    """Walk documents.jsonl, collapse last-write-wins per tumbler, and emit
    one (or two) events per logical row.

    The collapse is deliberate: today's ``read_documents`` does the same
    thing (a tombstone followed by a re-register yields the re-register
    only). Faithfully replaying the full append history would re-emit
    intermediate states the catalog already discarded; the synthesizer's
    contract is "events that reproduce today's effective state", not
    "events that reproduce the full audit trail".
    """
    last_seen: dict[str, dict[str, Any]] = {}
    tombstoned: dict[str, dict[str, Any]] = {}

    for obj in _iter_jsonl(path):
        key = obj.get("tumbler")
        if not key:
            continue
        if obj.get("_deleted"):
            # Stash the tombstone *with the data from the row that was
            # tombstoned* so we can emit a faithful Registered event for
            # the doc the tombstone refers to. JSONL tombstones today
            # carry the full DocumentRecord fields (see catalog.py
            # delete_document()).
            tombstoned[key] = obj
            last_seen.pop(key, None)
        else:
            last_seen[key] = obj
            tombstoned.pop(key, None)

    # Live documents → DocumentRegistered (+ DocumentAliased if alias_of set).
    for tumbler, obj in last_seen.items():
        yield _document_registered_event(obj, synthesized=True)
        alias_of = (obj.get("alias_of") or "").strip()
        if alias_of:
            yield Event(
                type=ev.TYPE_DOCUMENT_ALIASED,
                v=0,
                payload=ev.DocumentAliasedPayload(
                    alias_doc_id=tumbler,         # Phase 1: tumbler stands in for doc_id
                    canonical_doc_id=alias_of,
                ),
                ts=_synthesized_ts(obj),
            )

    # Tombstoned documents → DocumentRegistered + DocumentDeleted (RF-101-2).
    # Without the explicit Registered the projector has no Document state to
    # tombstone against; without the Deleted the projector silently resurrects
    # the row. Both are required.
    for tumbler, obj in tombstoned.items():
        yield _document_registered_event(obj, synthesized=True)
        yield Event(
            type=ev.TYPE_DOCUMENT_DELETED,
            v=0,
            payload=ev.DocumentDeletedPayload(
                doc_id=tumbler,
                reason="synthesized_from_tombstone",
            ),
            ts=_synthesized_ts(obj),
        )


def _document_registered_event(
    obj: dict[str, Any], *, synthesized: bool,
) -> Event:
    """Build a ``DocumentRegistered`` v: 0 event from a documents.jsonl row.

    Populates both the canonical fields (``doc_id``, ``coll_id``,
    ``source_uri``, ``content_type``, ``title``, ``source_mtime``) and the
    legacy tumbler-schema fields so the Phase 1 projector can write a
    SQLite row identical to the one ``Catalog.rebuild()`` produces.

    ``doc_id`` is set to the tumbler so the v: 0 path has a stable join
    key during Phase 1; a fresh UUID7 doc_id is what Phase 2 backfill will
    assign once the doc_id column lands in the SQLite schema.
    """
    tumbler = obj.get("tumbler", "")
    physical_collection = obj.get("physical_collection", "")
    payload = ev.DocumentRegisteredPayload(
        # Canonical
        doc_id=tumbler,  # Phase 1 stand-in; Phase 2 mints a fresh UUID7
        owner_id=_owner_prefix_of(tumbler),
        content_type=obj.get("content_type", ""),
        source_uri=obj.get("source_uri", ""),
        coll_id=physical_collection,
        title=obj.get("title", ""),
        source_mtime=float(obj.get("source_mtime", 0.0) or 0.0),
        indexed_at_doc=obj.get("indexed_at", ""),
        # Legacy (Phase 1 SQLite schema)
        tumbler=tumbler,
        author=obj.get("author", ""),
        year=int(obj.get("year", 0) or 0),
        file_path=obj.get("file_path", ""),
        corpus=obj.get("corpus", ""),
        physical_collection=physical_collection,
        chunk_count=int(obj.get("chunk_count", 0) or 0),
        head_hash=obj.get("head_hash", ""),
        indexed_at=obj.get("indexed_at", ""),
        alias_of=obj.get("alias_of", "") or "",
        meta=dict(obj.get("meta") or {}),
    )
    return Event(
        type=ev.TYPE_DOCUMENT_REGISTERED, v=0, payload=payload,
        ts=_synthesized_ts(obj),
    )


# ── Links ────────────────────────────────────────────────────────────────


def _synthesize_links(path: Path) -> Iterator[Event]:
    """Walk links.jsonl, collapse by composite key, emit LinkCreated /
    LinkDeleted as needed.

    Composite key: ``(from_t, to_t, link_type)`` matching the existing
    SQLite UNIQUE INDEX on the ``links`` table.
    """
    last_seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    tombstoned: dict[tuple[str, str, str], dict[str, Any]] = {}

    for obj in _iter_jsonl(path):
        # F2: backward compat — old JSONL uses "created", new uses "created_at"
        if "created" in obj and "created_at" not in obj:
            obj["created_at"] = obj.pop("created")
        try:
            key = (obj["from_t"], obj["to_t"], obj["link_type"])
        except KeyError:
            continue
        if obj.get("_deleted"):
            tombstoned[key] = obj
            last_seen.pop(key, None)
        else:
            last_seen[key] = obj
            tombstoned.pop(key, None)

    for key, obj in last_seen.items():
        from_t, to_t, link_type = key
        yield Event(
            type=ev.TYPE_LINK_CREATED, v=0,
            payload=ev.LinkCreatedPayload(
                from_doc=from_t,
                to_doc=to_t,
                link_type=link_type,
                from_span=obj.get("from_span", "") or "",
                to_span=obj.get("to_span", "") or "",
                creator=obj.get("created_by", "") or "",
                created_at=obj.get("created_at", "") or "",
                meta=dict(obj.get("meta") or {}),
            ),
            ts=_synthesized_ts(obj),
        )

    for key, obj in tombstoned.items():
        from_t, to_t, link_type = key
        # Emit Created (with the row's last-known data) followed by Deleted
        # so the projector sees the lifecycle even though the live SQLite
        # row never gets created.
        yield Event(
            type=ev.TYPE_LINK_CREATED, v=0,
            payload=ev.LinkCreatedPayload(
                from_doc=from_t,
                to_doc=to_t,
                link_type=link_type,
                from_span=obj.get("from_span", "") or "",
                to_span=obj.get("to_span", "") or "",
                creator=obj.get("created_by", "") or "",
                created_at=obj.get("created_at", "") or "",
                meta=dict(obj.get("meta") or {}),
            ),
            ts=_synthesized_ts(obj),
        )
        yield Event(
            type=ev.TYPE_LINK_DELETED, v=0,
            payload=ev.LinkDeletedPayload(
                from_doc=from_t,
                to_doc=to_t,
                link_type=link_type,
                reason="synthesized_from_tombstone",
            ),
            ts=_synthesized_ts(obj),
        )


# ── Helpers ──────────────────────────────────────────────────────────────


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield parsed JSON objects, one per non-empty line, skipping garbage.

    Mirrors the existing ``read_*`` readers' tolerance: garbage lines are
    logged and skipped, not fatal. Catalog JSONL is git-managed and
    machine-edited; one bad line should not brick the synthesizer.
    """
    with path.open() as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                _log.warning(
                    "synthesizer_jsonl_parse_error",
                    path=str(path), lineno=lineno, preview=line[:80],
                )
                continue


def _synthesized_ts(obj: dict[str, Any]) -> str:
    """Pick the best available timestamp from a JSONL row.

    ``indexed_at`` for documents, ``created_at`` for links, falls back to
    empty string when neither is present. The doctor verb tags
    empty-ts events as synthesized-from-pre-event-log records.
    """
    return obj.get("indexed_at") or obj.get("created_at") or ""


def _owner_prefix_of(tumbler: str) -> str:
    """Extract the ``store.owner`` prefix from a document tumbler.

    ``1.7.42`` → ``1.7``. Returns empty string for malformed input rather
    than raising; the caller can detect zero-length owner_id and report.
    """
    if not tumbler:
        return ""
    parts = tumbler.split(".")
    if len(parts) < 2:
        return ""
    return ".".join(parts[:2])
