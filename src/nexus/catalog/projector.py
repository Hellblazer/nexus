# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""RDR-101 Phase 1: project events to catalog SQLite state.

Phase 1 targets the *existing* ``CatalogDB`` schema (owners + documents +
documents_fts + links — the tumbler-keyed pre-Phase-5 layout). Replay
equality is the binding test: feeding a synthesizer's v: 0 event stream
into ``Projector`` produces a SQLite state byte-for-byte equal to what
``Catalog.rebuild()`` writes from the same JSONL files. If the two
disagree, the projector or the synthesizer is wrong — never an
acceptable runtime divergence.

Dispatch is on ``(type, v)`` per RF-101-2: ``(DocumentRegistered, 0)`` is
the legacy projection rule, ``(DocumentRegistered, 1)`` is the canonical
post-Phase-3 rule. Phase 1 implements the v: 0 paths fully and the v: 1
paths as no-ops with a structured warning, since no production writer
emits v: 1 yet (Phase 3 ships those). Unknown ``(type, v)`` pairs log
``event_log_unknown_dispatch`` and skip — this is deliberate forward
compat: the projector running against a future log version must not
crash, it must surface the new event type so an operator can decide.

Idempotency: re-projecting the same event sequence is a no-op past the
first run. ``DocumentRegistered`` uses ``INSERT OR REPLACE`` keyed on
``tumbler`` so re-applying overwrites with identical data.
``LinkCreated`` uses ``INSERT OR REPLACE`` against the UNIQUE INDEX on
``(from_tumbler, to_tumbler, link_type)`` so a follow-up event for the
same composite key OVERWRITES the prior row's spans + metadata (the
writer's merge path emits a single LinkCreated event carrying the
final merged metadata, not two separate Created+Updated events; the
projector converges SQLite on whatever the most recent event says).

Why state-snapshot ``LinkCreated`` rather than intent-preserving
``LinkUpdated``? Three reasons. First, the projector contract is
"replay equals direct write" (RF-101-2); a state-snapshot makes
convergence trivial — one INSERT OR REPLACE handler covers create,
merge, and re-merge alike. A LinkUpdated split would need two handlers
plus a "first vs subsequent" classification on every event, expanding
the test surface. Second, the writer's merge logic
(``Catalog._link_unlocked``) already computes the FINAL merged metadata
before emitting; an intent-preserving event would force the projector
to recompute the merge against current SQLite state, duplicating the
writer's logic in two places. Third, intent reconstruction (was this
event a fresh create or a merge?) is an audit/forensics concern that
can be derived offline by replaying the log up to and past the event
in question — the live SQLite never needs that distinction. The
trade-off (lossier event log) is bounded: the metadata payload still
carries the FINAL ``co_discovered_by`` list, so the merge participants
are recoverable. PR γ's ``TestLongLinkMutationSequence`` and
``TestBulkUnlinkRenameInterleaving`` lock down replay-equality across
long mutation sequences that exercise this path.
``DocumentDeleted`` issues ``DELETE FROM documents WHERE tumbler = ?``;
re-deleting a missing row is a no-op.

The projector does NOT manage the SQLite schema or migrations — it
takes a constructed ``CatalogDB`` and writes to whatever schema is
already there. The caller (``rebuild_via_log()``, ``nx catalog doctor
--replay-equality`` in PR C) constructs a fresh ``CatalogDB`` against
an ephemeral path so replay-equality has a clean slate.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

import structlog

from nexus.catalog import events as ev
from nexus.catalog.catalog_db import CatalogDB
from nexus.catalog.events import Event

_log = structlog.get_logger()


class Projector:
    """Apply events to a ``CatalogDB``. Idempotent and dispatch-on-(type, v)."""

    def __init__(self, db: CatalogDB) -> None:
        self._db = db

    # ── Public API ───────────────────────────────────────────────────────

    def apply(self, event: Event) -> None:
        """Apply one event. Unknown (type, v) pairs are logged and skipped."""
        handler = _DISPATCH.get((event.type, event.v))
        if handler is None:
            _log.warning(
                "event_log_unknown_dispatch",
                type=event.type,
                v=event.v,
                ts=event.ts,
            )
            return
        handler(self, event.payload)

    def apply_all(self, events: Iterable[Event], *, commit: bool = True) -> int:
        """Apply a stream of events; return the count actually applied.

        ``CatalogDB.commit()`` is called once at the end so a long
        replay is one transaction (atomic against external readers and
        ~50x faster than per-event commits on the live host catalog).

        Pass ``commit=False`` when the caller wraps this call in its
        own ``CatalogDB.transaction()`` block (e.g.
        ``Catalog._ensure_consistent`` event-sourced rebuild) — the
        outer ``with`` block commits or rolls back atomically and a
        nested commit would finalize the transaction prematurely,
        defeating the rollback fence.
        """
        applied = 0
        for event in events:
            self.apply(event)
            applied += 1
        if commit:
            self._db.commit()
        return applied

    # ── v: 0 handlers (synthesized from existing JSONL state) ────────────

    def _v0_owner_registered(self, payload: Any) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO owners "
            "(tumbler_prefix, name, owner_type, repo_hash, description, repo_root) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                payload.owner_id,
                payload.name,
                payload.owner_type,
                payload.repo_hash,
                payload.description,
                payload.repo_root,
            ),
        )

    def _v0_owner_deleted(self, payload: Any) -> None:
        # nexus-o6aa.9.4: dedupe-driven owner retirement under
        # NEXUS_EVENT_SOURCED=1. Re-deleting a missing row is a no-op.
        # Documents under this owner come out via their own
        # DocumentDeleted events; we do not cascade here because the
        # projection contract is one event = one row mutation.
        if not payload.owner_id:
            # nexus-o6aa.9.8: malformed event (hand-emitted, synthesis
            # bug, or a future v: 1 path that didn't populate the
            # field). Surface it in the structured log rather than
            # silently absorbing — replay should be observable.
            _log.warning(
                "projector_owner_deleted_no_owner_id",
                payload=str(payload),
            )
            return

        # nexus-o6aa.9.8: protocol invariant — OwnerDeleted MUST be
        # preceded by DocumentDeleted events for every document under
        # the owner. The projection contract has no FK CASCADE; if a
        # caller emits OwnerDeleted while documents under the prefix
        # still exist in SQLite, the orphaned rows point at a
        # non-existent owner. Surface this so a hand-emitted or
        # synthesis-driven OwnerDeleted that skipped its DocumentDeleted
        # prerequisites lands as a loud structured warning rather than
        # a silently corrupted projection.
        descendants = self._db.execute(
            "SELECT COUNT(*) FROM documents WHERE tumbler LIKE ?",
            (f"{payload.owner_id}.%",),
        ).fetchone()
        descendant_count = descendants[0] if descendants else 0
        if descendant_count > 0:
            _log.warning(
                "projector_owner_deleted_with_extant_documents",
                owner_id=payload.owner_id,
                descendant_count=descendant_count,
                note=(
                    "OwnerDeleted projected while documents under the "
                    "owner prefix still exist; protocol requires "
                    "DocumentDeleted for every descendant first. "
                    "Orphaned document rows now point at a non-existent "
                    "owner."
                ),
            )

        self._db.execute(
            "DELETE FROM owners WHERE tumbler_prefix = ?",
            (payload.owner_id,),
        )

    def _v0_document_registered(self, payload: Any) -> None:
        # The existing schema is tumbler-keyed; v: 0 synthesis stuffs the
        # tumbler into both ``payload.tumbler`` (legacy) and ``payload.doc_id``
        # (canonical-stand-in). Prefer the legacy slot for clarity.
        tumbler = payload.tumbler or payload.doc_id
        if not tumbler:
            _log.warning("projector_document_registered_no_tumbler",
                         payload=payload)
            return
        self._db.execute(
            "INSERT OR REPLACE INTO documents "
            "(tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, "
            "indexed_at, metadata, source_mtime, alias_of, source_uri) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tumbler,
                payload.title,
                payload.author,
                payload.year,
                payload.content_type,
                payload.file_path,
                payload.corpus,
                payload.physical_collection or payload.coll_id,
                payload.chunk_count,
                payload.head_hash,
                # ``indexed_at`` is the legacy column; ``indexed_at_doc`` is
                # the canonical name. v: 0 synthesis populates both from the
                # JSONL ``indexed_at`` field — prefer the legacy slot to
                # match the column.
                payload.indexed_at or payload.indexed_at_doc,
                json.dumps(payload.meta),
                payload.source_mtime,
                payload.alias_of,
                payload.source_uri,
            ),
        )

    def _v0_document_aliased(self, payload: Any) -> None:
        # Two emit paths produce DocumentAliased events that the Phase 1
        # SQLite schema (``documents.alias_of`` column) needs to track:
        #
        # 1. Synthesize-from-JSONL emits DocumentRegistered (with
        #    ``alias_of`` populated) FOLLOWED BY DocumentAliased. The
        #    DocumentRegistered's INSERT OR REPLACE already wrote the
        #    correct alias_of column; the DocumentAliased UPDATE here
        #    is idempotent on that row.
        # 2. Shadow-emit ``Catalog.set_alias`` emits ONLY
        #    DocumentAliased (no preceding DocumentRegistered for the
        #    aliased row). Without an UPDATE here, replaying that log
        #    leaves the projected SQLite row with empty alias_of,
        #    breaking the alias graph.
        #
        # The UPDATE is keyed on ``alias_doc_id`` which equals the
        # tumbler when ``mint_doc_id=False`` (the case for both
        # shadow-emit and the Phase 1 doctor verb). For
        # ``mint_doc_id=True`` synthesize-log paths the alias_doc_id is
        # a UUID7 that no tumbler-keyed row matches, so the UPDATE is a
        # safe no-op (and the alias_of column was already set by the
        # preceding DocumentRegistered's INSERT OR REPLACE).
        if not payload.alias_doc_id or not payload.canonical_doc_id:
            return
        # Refuse self-alias: ``Catalog.set_alias`` rejects this with
        # ``ValueError`` at the public API; a hand-emitted bad event
        # would slip past that guard and leave alias_of pointing at
        # the row's own tumbler — a 1-cycle in the alias graph that
        # ``Catalog.resolve_alias``'s cycle protection then has to
        # detect at every read. Bail out here so the projector is the
        # second line of defense.
        if payload.alias_doc_id == payload.canonical_doc_id:
            return
        self._db.execute(
            "UPDATE documents SET alias_of = ? WHERE tumbler = ?",
            (payload.canonical_doc_id, payload.alias_doc_id),
        )

    def _v0_document_renamed(self, payload: Any) -> None:
        # v: 0 synthesis never emits this event today (renames before the
        # event log existed are flattened into the ``last_seen`` snapshot
        # by ``synthesizer._synthesize_documents``). Kept registered so
        # dispatch on (DocumentRenamed, 0) doesn't trigger the
        # unknown-dispatch warning if someone hand-emits one.
        #
        # Like ``_v0_document_deleted``, prefer ``payload.tumbler`` for
        # the v: 0 schema's tumbler-keyed UPDATE — falling back to
        # ``payload.doc_id`` when tumbler is empty. Pre-fix the WHERE
        # clause used ``doc_id`` directly, which silently no-oped when
        # Phase 2 / Phase 3 paths emit a UUID7 doc_id against the
        # tumbler-keyed schema.
        tumbler = getattr(payload, "tumbler", "") or payload.doc_id
        if not tumbler or not payload.new_source_uri:
            return
        self._db.execute(
            "UPDATE documents SET source_uri = ? WHERE tumbler = ?",
            (payload.new_source_uri, tumbler),
        )

    def _v0_document_deleted(self, payload: Any) -> None:
        # Prefer the legacy ``tumbler`` field for the v: 0 schema's
        # tumbler-keyed DELETE. ``doc_id`` is a UUID7 when the Phase 2
        # synthesizer ran with ``mint_doc_id=True``; using it as the
        # WHERE clause would silently no-op the deletion. When
        # ``tumbler`` is empty (e.g. v: 1 native writes that haven't
        # populated it yet, or hand-emitted events), fall back to
        # ``doc_id`` for back-compat with the Phase 1 doctor verb path.
        tumbler = payload.tumbler or payload.doc_id
        if not tumbler:
            return
        self._db.execute(
            "DELETE FROM documents WHERE tumbler = ?",
            (tumbler,),
        )

    def _v0_document_enriched(self, payload: Any) -> None:
        # Enrichment merges the payload dict into ``documents.metadata``.
        # The current rebuild path already round-trips ``meta`` verbatim,
        # so the v: 0 enrichment data lives in
        # ``DocumentRegisteredPayload.meta`` for the synthesized-state
        # case; this event is a no-op for v: 0 synthesis. Phase 3+ uses
        # v: 1 ``DocumentEnriched`` to write structured Aspect rows.
        return

    def _v0_owner_or_collection_noop(self, payload: Any) -> None:
        # Reserved for future no-op event types in the
        # owner-or-collection family. Phase 6 split CollectionCreated
        # and CollectionSuperseded out into their own materializing
        # handlers (``_v0_collection_created`` /
        # ``_v0_collection_superseded``).
        return

    def _v0_collection_created(self, payload: Any) -> None:
        """Materialize a row in the ``collections`` projection.

        ``legacy_grandfathered`` is read from the payload (nexus-7m8n).
        Pre-7m8n events without the field fall back to a live regex
        evaluation via :func:`nexus.corpus.is_conformant_collection_name`,
        accepting that those replays are coupled to whatever regex
        ships at replay time. New writes freeze the value on the event
        so future regex changes cannot drift projected rows.

        Idempotent via ``INSERT OR REPLACE`` on the ``name`` PK.
        """
        if not payload.coll_id:
            return
        # nexus-7m8n: payload field is the source of truth when set;
        # ``None`` (older event without the field) falls back to the
        # live regex.
        payload_legacy = getattr(payload, "legacy_grandfathered", None)
        if payload_legacy is None:
            from nexus.corpus import is_conformant_collection_name  # noqa: PLC0415
            legacy = 0 if is_conformant_collection_name(payload.coll_id) else 1
        else:
            legacy = 1 if payload_legacy else 0
        self._db.execute(
            "INSERT OR REPLACE INTO collections "
            "(name, content_type, owner_id, embedding_model, model_version, "
            "display_name, legacy_grandfathered, superseded_by, "
            "superseded_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, "
            "COALESCE((SELECT superseded_by FROM collections WHERE name = ?), ''), "
            "COALESCE((SELECT superseded_at FROM collections WHERE name = ?), ''), "
            # ``created_at`` prefers an existing row's value (re-register
            # preserves the first-registration timestamp); falls back to
            # ``payload.created_at`` for fresh inserts. Pre-amendment
            # events without the field default to empty string.
            "COALESCE((SELECT created_at FROM collections WHERE name = ?), ?))",
            (
                payload.coll_id,
                payload.content_type,
                payload.owner_id,
                payload.embedding_model,
                payload.model_version,
                payload.name or payload.coll_id,
                legacy,
                payload.coll_id,
                payload.coll_id,
                payload.coll_id,
                getattr(payload, "created_at", "") or "",
            ),
        )

    def _v0_collection_superseded(self, payload: Any) -> None:
        """Mark the old collection's row as superseded; emit no new row.

        ``new_coll_id`` is expected to have its own ``CollectionCreated``
        already (or to follow shortly); the projector does not
        bootstrap a row for it from the supersede event alone, which
        would lose the canonical fields (``content_type``, ``owner_id``,
        ``embedding_model``, ``model_version``).

        If the old name has no row, the UPDATE is a safe no-op; the
        rename verb's caller is responsible for surfacing
        "unknown old collection" errors before the event is appended.

        ``superseded_at`` is read from the payload so replay is
        deterministic. Pre-payload-field events (synthesized before
        ``CollectionSupersededPayload.superseded_at`` was added) fall
        back to "" (the SQLite column default), mirroring the
        ``_v0_collection_created`` pattern for ``created_at``.
        nexus-qpet.1: the prior fallback to ``datetime.now(UTC)`` was
        non-deterministic; each replay drifted. Production today
        always populates the field, so the fallback is dead code in
        practice; the empty-string default keeps replay-equality
        invariant if a future synthesizer ever emits older shapes.
        """
        if not payload.old_coll_id or not payload.new_coll_id:
            return
        ts = getattr(payload, "superseded_at", "") or ""
        self._db.execute(
            "UPDATE collections SET superseded_by = ?, superseded_at = ? "
            "WHERE name = ?",
            (payload.new_coll_id, ts, payload.old_coll_id),
        )

    def _v0_chunk_indexed(self, payload: Any) -> None:
        # Phase 1 SQLite has no ``chunks`` table. The chunk count is
        # already on ``documents.chunk_count`` from
        # ``DocumentRegistered``. No-op here; Phase 5 SQLite gains a
        # chunks table and this handler will materialise rows.
        return

    def _v0_chunk_orphaned(self, payload: Any) -> None:
        return  # symmetric to chunk_indexed

    def _v0_link_created(self, payload: Any) -> None:
        # Use INSERT OR REPLACE on the (from_tumbler, to_tumbler,
        # link_type) UNIQUE INDEX so a second LinkCreated event for the
        # same composite key OVERWRITES the prior row's spans + metadata
        # rather than being silently dropped. The legacy
        # ``Catalog._link_unlocked`` merge path emits a LinkCreated with
        # the FINAL merged metadata after computing it from the existing
        # row; the projector's job is to converge SQLite on that final
        # state. INSERT OR IGNORE would have silently dropped the merge.
        #
        # SQLite implements OR REPLACE by deleting the conflicting row
        # and inserting a fresh one, which assigns a new ``id`` integer.
        # The doctor verb's links snapshot already excludes ``id`` (it is
        # not part of the projection contract per RF-101-2), so the new
        # ``id`` does not break replay-equality.
        if not payload.from_doc or not payload.to_doc or not payload.link_type:
            return
        self._db.execute(
            "INSERT OR REPLACE INTO links "
            "(from_tumbler, to_tumbler, link_type, from_span, to_span, "
            "created_by, created_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                payload.from_doc,
                payload.to_doc,
                payload.link_type,
                payload.from_span,
                payload.to_span,
                payload.creator,
                payload.created_at,
                json.dumps(payload.meta),
            ),
        )

    def _v0_link_deleted(self, payload: Any) -> None:
        if not payload.from_doc or not payload.to_doc or not payload.link_type:
            return
        self._db.execute(
            "DELETE FROM links "
            "WHERE from_tumbler = ? AND to_tumbler = ? AND link_type = ?",
            (payload.from_doc, payload.to_doc, payload.link_type),
        )

    # ── v: 1 handlers (Phase 3+ native writes) ───────────────────────────
    #
    # Phase 1 does not write the new SQLite schema (Document.doc_id
    # column, Chunk table, etc.) — that lands in Phase 3. The v: 1
    # handlers raise so a stray v: 1 emission lands LOUDLY rather than
    # being silently absorbed into a no-op. Pre-fix the handlers logged
    # a warning and returned, which combined with the
    # ``make_event(..., v=1)`` default to produce a trap: any caller
    # forgetting ``v=0`` would silently lose the event. Dispatch on
    # (type, 1) now aborts the writer mid-mutation, which leaves
    # SQLite + the legacy JSONL un-touched (the writer holds the flock
    # and has not yet committed at that point) and gives the operator a
    # stack trace pointing at the wrong-version site.

    def _v1_unsupported(self, payload: Any) -> None:
        raise NotImplementedError(
            f"v: 1 projector handler not implemented for "
            f"{type(payload).__name__}; native v: 1 writes land in "
            f"RDR-101 Phase 3+. Pass v=0 to make_event() until the "
            f"matching v: 1 handler ships."
        )


# Dispatch table built once at import. Maps (type, v) → bound-method names.
# Constructed from the Projector class so the names stay in sync with the
# methods even after refactoring.
def _build_dispatch() -> dict[tuple[str, int], Any]:
    return {
        # v: 0 — synthesized from existing JSONL
        (ev.TYPE_OWNER_REGISTERED, 0):       Projector._v0_owner_registered,
        (ev.TYPE_OWNER_DELETED, 0):          Projector._v0_owner_deleted,
        (ev.TYPE_COLLECTION_CREATED, 0):     Projector._v0_collection_created,
        (ev.TYPE_COLLECTION_SUPERSEDED, 0):  Projector._v0_collection_superseded,
        (ev.TYPE_DOCUMENT_REGISTERED, 0):    Projector._v0_document_registered,
        (ev.TYPE_DOCUMENT_RENAMED, 0):       Projector._v0_document_renamed,
        (ev.TYPE_DOCUMENT_ALIASED, 0):       Projector._v0_document_aliased,
        (ev.TYPE_DOCUMENT_ENRICHED, 0):      Projector._v0_document_enriched,
        (ev.TYPE_DOCUMENT_DELETED, 0):       Projector._v0_document_deleted,
        (ev.TYPE_CHUNK_INDEXED, 0):          Projector._v0_chunk_indexed,
        (ev.TYPE_CHUNK_ORPHANED, 0):         Projector._v0_chunk_orphaned,
        (ev.TYPE_LINK_CREATED, 0):           Projector._v0_link_created,
        (ev.TYPE_LINK_DELETED, 0):           Projector._v0_link_deleted,
        # v: 1 — Phase 3 ships these
        (ev.TYPE_OWNER_REGISTERED, 1):       Projector._v1_unsupported,
        (ev.TYPE_OWNER_DELETED, 1):          Projector._v1_unsupported,
        (ev.TYPE_COLLECTION_CREATED, 1):     Projector._v1_unsupported,
        (ev.TYPE_COLLECTION_SUPERSEDED, 1):  Projector._v1_unsupported,
        (ev.TYPE_DOCUMENT_REGISTERED, 1):    Projector._v1_unsupported,
        (ev.TYPE_DOCUMENT_RENAMED, 1):       Projector._v1_unsupported,
        (ev.TYPE_DOCUMENT_ALIASED, 1):       Projector._v1_unsupported,
        (ev.TYPE_DOCUMENT_ENRICHED, 1):      Projector._v1_unsupported,
        (ev.TYPE_DOCUMENT_DELETED, 1):       Projector._v1_unsupported,
        (ev.TYPE_CHUNK_INDEXED, 1):          Projector._v1_unsupported,
        (ev.TYPE_CHUNK_ORPHANED, 1):         Projector._v1_unsupported,
        (ev.TYPE_LINK_CREATED, 1):           Projector._v1_unsupported,
        (ev.TYPE_LINK_DELETED, 1):           Projector._v1_unsupported,
    }


_DISPATCH: dict[tuple[str, int], Any] = _build_dispatch()
