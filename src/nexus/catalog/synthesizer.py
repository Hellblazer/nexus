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


def synthesize_from_jsonl(
    catalog_dir: Path, *, mint_doc_id: bool = False,
) -> Iterator[Event]:
    """Yield v: 0 events that reproduce the catalog state under ``catalog_dir``.

    Order: owners → documents → links. Within each file, JSONL append order
    is preserved (last-write-wins is collapsed before emit so the projector
    sees one or two events per logical key, not the full append history).

    Skips a file if it does not exist (fresh catalog with partial state).

    ``mint_doc_id`` controls how ``DocumentRegisteredPayload.doc_id`` is
    populated:

    - ``False`` (Phase 1 default): ``doc_id`` is set to the tumbler. This
      keeps ``synthesize_from_jsonl`` driving the Phase 1 doctor verb's
      replay-equality test, where the projector reads ``payload.tumbler``
      to write the SQLite tumbler-keyed row anyway.
    - ``True`` (Phase 2 ``nx catalog synthesize-log``): ``doc_id`` is a
      fresh UUID7 per RDR-101 §Migration / Phase 1. The original tumbler
      is preserved in ``payload.tumbler`` so the projector's v: 0 path
      still writes a tumbler-keyed SQLite row, and the doc_id is carried
      in the event log for Phase 3+ canonical use.

    Aliased rows (``DocumentAliased`` events) follow the same rule: when
    ``mint_doc_id=True``, ``alias_doc_id`` and ``canonical_doc_id`` are
    the freshly-minted UUID7s for the alias and canonical rows
    respectively, so the alias graph in the event log is doc_id-keyed
    even though the catalog-side schema still tracks aliases by tumbler.
    """
    owners_path = catalog_dir / "owners.jsonl"
    docs_path = catalog_dir / "documents.jsonl"
    links_path = catalog_dir / "links.jsonl"

    if owners_path.exists():
        yield from _synthesize_owners(owners_path)

    # ``DocumentAliased`` references both the alias's and the canonical's
    # doc_id. When mint_doc_id is True we have to mint per-tumbler doc_ids
    # before emitting aliases so the alias graph stays consistent. Build
    # the tumbler→doc_id map up front (one full read of documents.jsonl);
    # subsequent emits read from the map.
    tumbler_to_doc_id: dict[str, str]
    if mint_doc_id and docs_path.exists():
        tumbler_to_doc_id = _build_tumbler_to_doc_id(docs_path)
    else:
        tumbler_to_doc_id = {}

    if docs_path.exists():
        yield from _synthesize_documents(
            docs_path,
            tumbler_to_doc_id=tumbler_to_doc_id,
            mint_doc_id=mint_doc_id,
        )
    if links_path.exists():
        yield from _synthesize_links(links_path)


def _build_tumbler_to_doc_id(docs_path: Path) -> dict[str, str]:
    """Walk documents.jsonl once and mint a fresh UUID7 doc_id per tumbler.

    A tumbler that appears multiple times in the JSONL (re-register,
    tombstone+re-register, alias rewrite) gets one consistent doc_id —
    the first occurrence wins. Subsequent rewrites update other fields
    on the same logical document, so the doc_id must stay constant
    across them.
    """
    mapping: dict[str, str] = {}
    for obj in _iter_jsonl(docs_path):
        tumbler = obj.get("tumbler")
        if not tumbler:
            continue
        if tumbler not in mapping:
            mapping[tumbler] = ev.new_doc_id()
    return mapping


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


def _synthesize_documents(
    path: Path,
    *,
    tumbler_to_doc_id: dict[str, str] | None = None,
    mint_doc_id: bool = False,
) -> Iterator[Event]:
    """Walk documents.jsonl, collapse last-write-wins per tumbler, and emit
    one (or two) events per logical row.

    The collapse is deliberate: today's ``read_documents`` does the same
    thing (a tombstone followed by a re-register yields the re-register
    only). Faithfully replaying the full append history would re-emit
    intermediate states the catalog already discarded; the synthesizer's
    contract is "events that reproduce today's effective state", not
    "events that reproduce the full audit trail".

    ``tumbler_to_doc_id`` and ``mint_doc_id`` are passed through from the
    public ``synthesize_from_jsonl``; see its docstring for the
    contract.
    """
    last_seen: dict[str, dict[str, Any]] = {}
    tombstoned: dict[str, dict[str, Any]] = {}
    mapping = tumbler_to_doc_id or {}

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
        yield _document_registered_event(
            obj, mapping=mapping, mint_doc_id=mint_doc_id,
        )
        alias_of = (obj.get("alias_of") or "").strip()
        if alias_of:
            # Resolve both endpoints through the mapping when minting
            # so the alias graph in the event log uses the canonical
            # doc_ids, not raw tumblers.
            alias_doc_id = mapping.get(tumbler, tumbler) if mint_doc_id else tumbler
            canonical_doc_id = (
                mapping.get(alias_of, alias_of) if mint_doc_id else alias_of
            )
            yield Event(
                type=ev.TYPE_DOCUMENT_ALIASED,
                v=0,
                payload=ev.DocumentAliasedPayload(
                    alias_doc_id=alias_doc_id,
                    canonical_doc_id=canonical_doc_id,
                ),
                ts=_synthesized_ts(obj),
            )

    # Tombstoned documents → DocumentRegistered + DocumentDeleted (RF-101-2).
    # Without the explicit Registered the projector has no Document state to
    # tombstone against; without the Deleted the projector silently resurrects
    # the row. Both are required.
    for tumbler, obj in tombstoned.items():
        yield _document_registered_event(
            obj, mapping=mapping, mint_doc_id=mint_doc_id,
        )
        deleted_doc_id = (
            mapping.get(tumbler, tumbler) if mint_doc_id else tumbler
        )
        yield Event(
            type=ev.TYPE_DOCUMENT_DELETED,
            v=0,
            payload=ev.DocumentDeletedPayload(
                doc_id=deleted_doc_id,
                reason="synthesized_from_tombstone",
            ),
            ts=_synthesized_ts(obj),
        )


def _document_registered_event(
    obj: dict[str, Any],
    *,
    mapping: dict[str, str] | None = None,
    mint_doc_id: bool = False,
) -> Event:
    """Build a ``DocumentRegistered`` v: 0 event from a documents.jsonl row.

    Populates both the canonical fields (``doc_id``, ``coll_id``,
    ``source_uri``, ``content_type``, ``title``, ``source_mtime``) and the
    legacy tumbler-schema fields so the Phase 1 projector can write a
    SQLite row identical to the one ``Catalog.rebuild()`` produces.

    ``doc_id`` rule:

    - When ``mint_doc_id=False`` (default, Phase 1 doctor verb):
      ``doc_id`` is set to the tumbler so the v: 0 projector has a
      stable join key.
    - When ``mint_doc_id=True`` (Phase 2 ``synthesize-log`` verb):
      ``doc_id`` is the freshly-minted UUID7 from ``mapping``; the
      tumbler stays in ``payload.tumbler``.
    """
    mapping = mapping or {}
    tumbler = obj.get("tumbler", "")
    physical_collection = obj.get("physical_collection", "")
    if mint_doc_id and tumbler in mapping:
        doc_id = mapping[tumbler]
    else:
        doc_id = tumbler
    payload = ev.DocumentRegisteredPayload(
        # Canonical
        doc_id=doc_id,
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


# ── T3 chunk synthesis (Phase 2 PR β) ────────────────────────────────────


_CHUNK_PAGE_SIZE = 300  # matches the ChromaDB Cloud 300-row limit


def synthesize_t3_chunks(
    client: Any,
    document_events: list[Event],
) -> Iterator[Event]:
    """Walk every collection on ``client`` and emit one ``ChunkIndexed``
    v: 0 event per chunk.

    ``document_events`` is the list of ``DocumentRegistered`` events the
    document-side synthesizer just produced; this function reads
    ``payload.source_uri`` / ``payload.title`` / ``payload.coll_id`` /
    ``payload.doc_id`` to build reverse maps that resolve each T3 chunk's
    ``source_path`` to a doc_id.

    Resolution priority per chunk:

    1. ``source_path`` exact match against ``source_uri`` after the
       canonical ``file://`` prefix (the catalog stores file-scheme URIs
       as ``file:///abs/path``; T3 chunks store the bare path).
    2. ``title`` exact match (the Phase 0 ``CHROMA_IDENTITY_FIELD``
       fallback for ``knowledge__*`` collections that legitimately have
       empty source_uri rows in the catalog).
    3. None — emit ``ChunkIndexed`` with ``doc_id=""`` and
       ``synthesized_orphan=True`` so the Phase 2 doctor coverage
       check (PR δ) can report the orphan rather than the GC silently
       collecting it after the orphan window.

    The walker uses paginated ``col.get(limit=300, offset=...)`` so a
    multi-thousand-chunk collection does not exceed the ChromaDB Cloud
    page limit. Chunks with empty / missing chash metadata are still
    emitted (chash is empty in the event) — the corresponding catalog
    rows existed before the chash_index landed (RDR-086) and would
    otherwise vanish from the synthesized log.
    """
    # Build reverse maps once.
    source_uri_to_doc_id: dict[str, str] = {}
    title_to_doc_id: dict[str, str] = {}
    coll_to_doc_ids: dict[str, set[str]] = {}
    for ev_obj in document_events:
        if ev_obj.type != ev.TYPE_DOCUMENT_REGISTERED:
            continue
        p = ev_obj.payload
        if p.source_uri:
            source_uri_to_doc_id[p.source_uri] = p.doc_id
        if p.title:
            # Last-write-wins: a title collision means the synthesized
            # event log can only resolve to one of them; the doctor
            # verb will surface the ambiguity downstream.
            title_to_doc_id[p.title] = p.doc_id
        if p.coll_id:
            coll_to_doc_ids.setdefault(p.coll_id, set()).add(p.doc_id)

    try:
        collections = list(client.list_collections())
    except Exception as exc:
        _log.warning("synthesizer_t3_list_collections_failed", error=str(exc))
        return

    for col in collections:
        coll_name = getattr(col, "name", None) or str(col)
        try:
            yield from _synthesize_collection_chunks(
                col, coll_name,
                source_uri_to_doc_id=source_uri_to_doc_id,
                title_to_doc_id=title_to_doc_id,
            )
        except Exception as exc:
            _log.warning(
                "synthesizer_t3_collection_walk_failed",
                collection=coll_name, error=str(exc),
            )
            continue


def _synthesize_collection_chunks(
    col: Any,
    coll_name: str,
    *,
    source_uri_to_doc_id: dict[str, str],
    title_to_doc_id: dict[str, str],
) -> Iterator[Event]:
    """Paginate one collection and yield ``ChunkIndexed`` per chunk."""
    offset = 0
    while True:
        page = col.get(limit=_CHUNK_PAGE_SIZE, offset=offset, include=["metadatas"])
        ids = page.get("ids") or []
        metadatas = page.get("metadatas") or []
        if not ids:
            break
        for chunk_id, meta in zip(ids, metadatas):
            meta = meta or {}
            source_path = meta.get("source_path") or ""
            chunk_title = meta.get("title") or ""
            chash = meta.get("chunk_text_hash") or ""
            content_hash = meta.get("content_hash") or ""
            chunk_index = int(meta.get("chunk_index", 0) or 0)
            embedded_at = meta.get("indexed_at") or meta.get("embedded_at") or ""

            # 1. source_path → file://source_path → source_uri map.
            doc_id = ""
            if source_path:
                candidate = (
                    source_path
                    if source_path.startswith(("file://", "chroma://", "https://", "http://", "x-devonthink-item://"))
                    else f"file://{source_path}"
                )
                doc_id = source_uri_to_doc_id.get(candidate, "")
            # 2. Title fallback (CHROMA_IDENTITY_FIELD pattern).
            if not doc_id and chunk_title:
                doc_id = title_to_doc_id.get(chunk_title, "")
            orphan = not doc_id

            yield Event(
                type=ev.TYPE_CHUNK_INDEXED,
                v=0,
                payload=ev.ChunkIndexedPayload(
                    chunk_id=chunk_id,
                    chash=chash,
                    doc_id=doc_id,
                    coll_id=coll_name,
                    position=chunk_index,
                    content_hash=content_hash,
                    embedded_at=embedded_at,
                    synthesized_orphan=orphan,
                ),
                ts=embedded_at,
            )

        if len(ids) < _CHUNK_PAGE_SIZE:
            break
        offset += _CHUNK_PAGE_SIZE
