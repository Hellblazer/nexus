# SPDX-License-Identifier: AGPL-3.0-or-later
"""Catalog write operations (nexus-mbm follow-up extraction 6/7).

Mutations that DON'T directly drive the event-sourcing dual-write
machinery — collection-on-document updates, supersession, alias,
delete, rename, generic field update — moved out of ``Catalog``
to a focused module. The architectural critique of PR #602
flagged that the original "writes belong on facade" justification
was over-applied: only the **registration** writes
(``register_owner`` / ``register`` / ``register_collection``)
genuinely call the projector and event-log machinery directly;
these other writes use the same flock + JSONL append helpers any
module could call.

Composed onto ``Catalog`` as ``self._writes``. Catalog's public
``update`` / ``delete_document`` / ``rename_collection`` /
``supersede_collection`` / ``set_alias`` /
``update_document_collection`` / ``update_documents_collection_batch``
are thin one-line delegates so the public API is unchanged.

The ``_cat_mod`` reference pattern is used here for the same
reason as in ``catalog_sync.py`` and ``catalog_links.py``: tests
that ``monkeypatch.setattr("nexus.catalog.catalog._FOO", ...)``
should propagate without needing to also patch this module.
"""
from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from nexus.catalog import catalog as _cat_mod
from nexus.catalog.events import (
    CollectionSupersededPayload as _CollectionSupersededPayload,
    DocumentAliasedPayload as _DocumentAliasedPayload,
    DocumentDeletedPayload as _DocumentDeletedPayload,
    DocumentRegisteredPayload as _DocumentRegisteredPayload,
)
from nexus.catalog.tumbler import DocumentRecord, Tumbler

if TYPE_CHECKING:
    from nexus.catalog.catalog import Catalog

_log = structlog.get_logger(__name__)


class _WriteOps:
    """Composed onto ``Catalog`` as ``self._writes``.

    Methods access state via ``self._cat.<attr>``: ``_db`` for SQL,
    ``_acquire_lock`` / ``_release_lock`` for the dir flock,
    ``_dir`` / ``_documents_path`` / ``_links_path`` /
    ``_events_path`` for canonical-truth files,
    ``_event_sourced_enabled`` / ``_projector`` /
    ``_write_to_event_log`` / ``_emit_shadow_event`` /
    ``_append_jsonl`` for the dual-write machinery.
    """

    def __init__(self, catalog: "Catalog") -> None:
        self._cat = catalog

    def _update_document_collection_locked(
        self, tumbler: str, new_collection: str,
    ) -> bool:
        """Read+validate+write the per-row re-point WITHOUT acquiring
        the flock or committing SQLite. Caller is responsible for both.

        Returns True on a write, False on the not-found or
        same-target idempotency short-circuits. Used by both the
        single-row :meth:`update_document_collection` (one acquire,
        one commit per call) and the batch
        :meth:`update_documents_collection_batch` (one acquire, one
        commit per N calls).
        """
        cat = self._cat
        from nexus.catalog.synthesizer import _owner_prefix_of  # noqa: PLC0415

        row = cat._db.execute(
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
            "metadata, source_mtime, source_uri, alias_of "
            "FROM documents WHERE tumbler = ?",
            (tumbler,),
        ).fetchone()
        if row is None:
            return False
        if (row[7] or "") == new_collection:
            return False

        meta_dict = json.loads(row[11]) if row[11] else {}
        event = _cat_mod._make_event(
            _DocumentRegisteredPayload(
                doc_id=row[0],
                owner_id=_owner_prefix_of(row[0]),
                content_type=row[4] or "",
                source_uri=row[13] or "",
                coll_id=new_collection,
                title=row[1] or "",
                source_mtime=float(row[12] or 0.0),
                indexed_at_doc=row[10] or "",
                tumbler=row[0],
                author=row[2] or "",
                year=int(row[3] or 0),
                file_path=row[5] or "",
                corpus=row[6] or "",
                physical_collection=new_collection,
                chunk_count=int(row[8] or 0),
                head_hash=row[9] or "",
                indexed_at=row[10] or "",
                alias_of=row[14] or "",
                meta=dict(meta_dict),
            ),
            v=0,
        )
        rec = {
            "tumbler": row[0],
            "title": row[1],
            "author": row[2],
            "year": row[3],
            "content_type": row[4],
            "file_path": row[5],
            "corpus": row[6],
            "physical_collection": new_collection,
            "chunk_count": row[8],
            "head_hash": row[9] or "",
            "indexed_at": row[10] or "",
            "meta": meta_dict,
            "source_mtime": row[12] or 0.0,
            "source_uri": row[13] or "",
            "alias_of": row[14] or "",
        }
        if cat._event_sourced_enabled:
            cat._write_to_event_log(event)
            cat._projector.apply(event)
            cat._append_jsonl(cat._documents_path, rec)
        else:
            cat._append_jsonl(cat._documents_path, rec)
            cat._db.execute(
                "UPDATE documents SET physical_collection = ? "
                "WHERE tumbler = ?",
                (new_collection, row[0]),
            )
            cat._emit_shadow_event(event)
        return True

    def update_document_collection(
        self, tumbler: str, new_collection: str,
    ) -> bool:
        """Re-point a single document's ``physical_collection``.

        Per-row analog of :meth:`rename_collection` for migrations
        where each document gets a different target (e.g. RDR-101
        Phase 6 ``nx catalog migrate-fallback``). Emits
        DocumentRegistered v: 0 with the new ``physical_collection``;
        the projector's INSERT OR REPLACE updates the SQLite row.

        Returns True if the doc was re-pointed, False if not found or
        already pointed at ``new_collection`` (idempotent).

        nexus-qpet.2: read + validate + construct payload all inside
        the lock so two concurrent re-points of the same tumbler
        resolve to a deterministic last-write-wins on ONE writer.

        Crash-window discipline (event-sourced mode) matches
        rename_collection: event -> projector apply -> JSONL append,
        with the SQLite commit last. A crash between projector apply
        and JSONL append leaves SQLite uncommitted and JSONL unwritten
        (both old). A crash between JSONL append and commit leaves
        JSONL ahead of SQLite; on rebuild-from-JSONL the new line
        wins; on rebuild-from-events the projector replays correctly.
        """
        cat = self._cat
        dir_fd = cat._acquire_lock()
        try:
            wrote = cat._update_document_collection_locked(
                tumbler, new_collection,
            )
            if wrote:
                cat._db.commit()
            return wrote
        finally:
            cat._release_lock(dir_fd)

    def update_documents_collection_batch(
        self, pairs: list[tuple[str, str]],
    ) -> int:
        """Re-point N documents' ``physical_collection`` in one
        flock + one SQLite commit (nexus-qpet.3).

        Each *pair* is ``(tumbler, new_collection)``. Returns the
        count of documents actually re-pointed (no-ops via not-found
        or same-target idempotency are excluded).

        Used by ``nx catalog migrate-fallback`` for the per-document
        re-point loop. Single-row callers should still use
        :meth:`update_document_collection` (which uses this method's
        helper internally so semantics match).
        """
        cat = self._cat
        if not pairs:
            return 0
        dir_fd = cat._acquire_lock()
        wrote_any = False
        updated = 0
        try:
            for tumbler, new_collection in pairs:
                if cat._update_document_collection_locked(
                    tumbler, new_collection,
                ):
                    updated += 1
                    wrote_any = True
            if wrote_any:
                cat._db.commit()
            return updated
        finally:
            cat._release_lock(dir_fd)

    def supersede_collection(
        self,
        old_name: str,
        new_name: str,
        *,
        reason: str = "",
    ) -> None:
        """Mark ``old_name`` as superseded by ``new_name``.

        Writes a CollectionSuperseded v: 0 event and updates the old
        collection's ``superseded_by`` / ``superseded_at`` columns. The
        new collection MUST already be registered (the docstring used
        to say "callers usually pair register_collection with
        supersede_collection"; that contract is now enforced).

        Raises ``ValueError`` when:
          - ``old_name`` is not registered (typo-on-explicit-action path)
          - ``new_name`` is not registered (would create a dangling
            ``superseded_by`` pointer that no foreign-key-style join
            can resolve)
          - ``old_name`` is already superseded (silently overwriting
            the previous supersession would orphan the prior
            CollectionSuperseded event in the log)

        Honors the ``_event_sourced_enabled`` split that the rest of the
        catalog writers use.
        """
        cat = self._cat
        from datetime import UTC, datetime  # noqa: PLC0415

        ts = datetime.now(UTC).isoformat()
        event = _cat_mod._make_event(
            _CollectionSupersededPayload(
                old_coll_id=old_name,
                new_coll_id=new_name,
                reason=reason,
                superseded_at=ts,
            ),
            v=0,
            ts=ts,
        )
        dir_fd = cat._acquire_lock()
        try:
            # nexus-qpet.2: re-validate inside the locked block. Two
            # concurrent supersedes of the same old_name now produce
            # one success + one ValueError rather than a silent
            # last-write-wins (the in-process projection was
            # last-writer determined; replay was order-deterministic
            # but operator-confusing).
            existing = cat.get_collection(old_name)
            if existing is None:
                raise ValueError(
                    f"supersede_collection: {old_name!r} not registered"
                )
            if existing.get("superseded_by"):
                raise ValueError(
                    f"supersede_collection: {old_name!r} is already "
                    f"superseded by {existing['superseded_by']!r}; "
                    f"refusing to chain a second supersede event"
                )
            if cat.get_collection(new_name) is None:
                raise ValueError(
                    f"supersede_collection: new {new_name!r} is not "
                    f"registered. Call register_collection({new_name!r}, ...) "
                    f"first so the projection has a row to point at."
                )
            if cat._event_sourced_enabled:
                cat._write_to_event_log(event)
                cat._projector.apply(event)
                cat._db.commit()
            else:
                # Legacy mode: SQLite is canonical, no JSONL backing.
                # Reuse the same ``ts`` as the event payload so the row
                # records exactly what the event records (deterministic
                # under replay-equality even in legacy mode).
                cat._db.execute(
                    "UPDATE collections SET superseded_by = ?, "
                    "superseded_at = ? WHERE name = ?",
                    (new_name, ts, old_name),
                )
                cat._db.commit()
                cat._emit_shadow_event(event)
        finally:
            cat._release_lock(dir_fd)

    def set_alias(self, tumbler: Tumbler, canonical: Tumbler) -> None:
        """Mark ``tumbler`` as an alias for ``canonical``.

        Intended for ``nx catalog dedupe-owners`` (nexus-tmbh). The
        aliased row stays in the catalog so external references continue
        to resolve. Refuses to create a self-alias (which would be a
        1-cycle). A pre-existing alias is overwritten — callers that
        need to preserve the old pointer should snapshot it first.

        No-op if ``tumbler`` is not a known document. JSONL truth is
        updated by appending a new document record with the alias
        populated so subsequent JSONL-driven rebuilds preserve the
        pointer (last-line-wins).
        """
        cat = self._cat
        if str(tumbler) == str(canonical):
            raise ValueError(f"self-alias rejected: {tumbler} → {canonical}")
        # Acquire the catalog directory flock so the JSONL append and
        # the shadow-event emit (which both have a "caller holds the
        # flock" contract) cannot race a concurrent writer. Pre-PR-F
        # this method was unlocked because it was JSONL+SQLite-only;
        # adding the shadow emit made the lock load-bearing.
        dir_fd = cat._acquire_lock()
        try:
            # Read current row (by raw tumbler — do not follow alias, we want
            # to update THIS row specifically).
            raw = cat.resolve(tumbler, follow_alias=False)
            if raw is None:
                return
            updated = DocumentRecord(
                tumbler=str(tumbler),
                title=raw.title,
                author=raw.author,
                year=raw.year,
                content_type=raw.content_type,
                file_path=raw.file_path,
                corpus=raw.corpus,
                physical_collection=raw.physical_collection,
                chunk_count=raw.chunk_count,
                head_hash=raw.head_hash,
                indexed_at=raw.indexed_at,
                meta=raw.meta,
                source_mtime=raw.source_mtime,
                alias_of=str(canonical),
            )
            event = _cat_mod._make_event(
                _DocumentAliasedPayload(
                    alias_doc_id=str(tumbler),
                    canonical_doc_id=str(canonical),
                ),
                v=0,
            )
            if cat._event_sourced_enabled:
                cat._write_to_event_log(event)
                cat._projector.apply(event)
                cat._db.commit()
                cat._append_jsonl(cat._documents_path, updated.__dict__)
            else:
                cat._db.execute(
                    "UPDATE documents SET alias_of = ? WHERE tumbler = ?",
                    (str(canonical), str(tumbler)),
                )
                cat._db.commit()
                # Append updated JSONL record so a future rebuild sees the alias.
                cat._append_jsonl(cat._documents_path, updated.__dict__)
                cat._emit_shadow_event(event)
        finally:
            cat._release_lock(dir_fd)

    def update(self, tumbler: Tumbler, **fields: object) -> None:
        cat = self._cat
        dir_fd = cat._acquire_lock()
        try:
            entry = cat.resolve(tumbler)
            if entry is None:
                raise KeyError(f"no document with tumbler {tumbler}")
            # Build updated record
            rec_dict = {
                "tumbler": str(entry.tumbler),
                "title": entry.title,
                "author": entry.author,
                "year": entry.year,
                "content_type": entry.content_type,
                "file_path": entry.file_path,
                "corpus": entry.corpus,
                "physical_collection": entry.physical_collection,
                "chunk_count": entry.chunk_count,
                "head_hash": entry.head_hash,
                "indexed_at": entry.indexed_at,
                # nexus-ga48: coerce ``None`` → ``{}`` at the source so
                # the downstream merge (line ~1830), event payload
                # (~1874), and SQL serialisation (~1909) all see a
                # dict shape. Pre-fix, a row whose SQLite ``metadata``
                # column held the literal ``'null'`` string decoded
                # back through resolve() as Python ``None``, which
                # then crashed in ``dict(None)`` at the merge or
                # event-payload sites — silently blocking any
                # ``update()`` on the 11 affected rows in Hal's
                # catalog. The boundary serialisation at line 1909
                # also gets ``or {}`` defence-in-depth.
                "meta": entry.meta or {},
                "source_mtime": entry.source_mtime,
                # RDR-096 P3.1: preserve source_uri across updates.
                # Without this carry-over, every update() call would
                # silently clobber source_uri with the column default,
                # erasing the URI persisted at register time.
                "source_uri": entry.source_uri,
                # Round-4 review (reviewer D): carry alias_of into
                # rec_dict so a caller passing ``update(t, alias_of="X")``
                # threads through both the event payload and the legacy
                # SQL VALUES list. Pre-fix both paths read from
                # ``entry.alias_of`` directly, silently dropping the
                # caller-supplied value.
                "alias_of": entry.alias_of or "",
            }
            # Merge meta dict rather than replace
            if "meta" in fields and isinstance(fields["meta"], dict):
                merged_meta = dict(rec_dict["meta"])
                merged_meta.update(fields["meta"])
                fields = dict(fields, meta=merged_meta)
            rec_dict.update(fields)
            # nexus-3e4s C1: always validate the final ``source_uri``,
            # not just when the caller passes it explicitly. Pre-fix
            # this block was gated on ``"source_uri" in fields`` and
            # the production hot path (catalog hook calls update() with
            # head_hash + physical_collection but no source_uri) never
            # exercised the guard. Re-derive only when source_uri or
            # file_path is being mutated; otherwise carry the existing
            # source_uri through but still run the guard so any
            # in-place row whose URI drifted out of the owner's tree
            # cannot be silently extended.
            owner_addr = entry.tumbler.owner_address()
            owner_repo_root = cat._owner_repo_root(owner_addr)
            if "source_uri" in fields or "file_path" in fields:
                rec_dict["source_uri"] = _cat_mod._normalize_source_uri(
                    rec_dict["source_uri"], rec_dict.get("file_path", ""),
                    repo_root=owner_repo_root,
                )
            cat._check_source_uri_in_repo_root(
                owner_addr, rec_dict["source_uri"],
            )
            event = _cat_mod._make_event(
                _DocumentRegisteredPayload(
                    doc_id=rec_dict["tumbler"],
                    owner_id=str(entry.tumbler.owner_address()),
                    content_type=rec_dict["content_type"],
                    source_uri=rec_dict.get("source_uri", ""),
                    coll_id=rec_dict["physical_collection"],
                    title=rec_dict["title"],
                    source_mtime=float(rec_dict.get("source_mtime", 0.0) or 0.0),
                    indexed_at_doc=rec_dict["indexed_at"],
                    tumbler=rec_dict["tumbler"],
                    author=rec_dict["author"],
                    year=int(rec_dict["year"] or 0),
                    file_path=rec_dict["file_path"],
                    corpus=rec_dict["corpus"],
                    physical_collection=rec_dict["physical_collection"],
                    chunk_count=int(rec_dict["chunk_count"] or 0),
                    head_hash=rec_dict["head_hash"],
                    indexed_at=rec_dict["indexed_at"],
                    alias_of=rec_dict["alias_of"],
                    meta=dict(rec_dict["meta"]),
                ),
                v=0,
            )
            if cat._event_sourced_enabled:
                # Phase 3 PR β — event-sourced update path. update() is
                # overloaded (source_uri rename, bib enrichment, etc.);
                # the lossless DocumentRegistered-with-post-update-state
                # captures everything via the projector's INSERT OR
                # REPLACE. Future Phase 3+ work may introduce
                # fine-grained DocumentRenamed/DocumentEnriched events
                # that capture intent rather than state.
                cat._write_to_event_log(event)
                cat._projector.apply(event)
                cat._db.commit()
                cat._append_jsonl(cat._documents_path, rec_dict)
            else:
                cat._append_jsonl(cat._documents_path, rec_dict)
                # Upsert SQLite. ``alias_of`` is included in the column
                # list because INSERT OR REPLACE on the tumbler PK
                # deletes the prior row before inserting; omitting the
                # column would let the new row carry the column default
                # (NULL) instead of the prior alias pointer, silently
                # severing the alias graph on every update().
                cat._db.execute(
                    "INSERT OR REPLACE INTO documents "
                    "(tumbler, title, author, year, content_type, file_path, "
                    "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
                    "metadata, source_mtime, source_uri, alias_of) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        rec_dict["tumbler"], rec_dict["title"], rec_dict["author"],
                        rec_dict["year"], rec_dict["content_type"], rec_dict["file_path"],
                        rec_dict["corpus"], rec_dict["physical_collection"],
                        rec_dict["chunk_count"], rec_dict["head_hash"],
                        rec_dict["indexed_at"],
                        json.dumps(rec_dict["meta"] or {}),
                        rec_dict.get("source_mtime", 0.0),
                        rec_dict.get("source_uri", ""),
                        rec_dict["alias_of"],
                    ),
                )
                cat._db.commit()
                cat._emit_shadow_event(event)
        finally:
            cat._release_lock(dir_fd)

    def rename_collection(self, old: str, new: str) -> int:
        """Re-point every document from ``physical_collection=old`` → ``new``.

        nexus-1ccq: `nx collection rename` cascade. JSONL is the source
        of truth, so for every matching row we append a new record with
        the updated ``physical_collection`` and also update the SQLite
        cache (one UPDATE, no per-row upsert). Rebuild-from-JSONL sees
        the later record and wins — append-only semantics preserved.
        Returns count renamed.
        """
        cat = self._cat
        dir_fd = cat._acquire_lock()
        try:
            # Include ``alias_of`` in the SELECT so the rename's shadow
            # emit can preserve it for any aliased document being
            # renamed. Pre-fix the SELECT omitted alias_of and the emit
            # hardcoded it to "", silently severing the alias graph
            # for any renamed alias row on replay.
            rows = cat._db.execute(
                "SELECT tumbler, title, author, year, content_type, file_path, "
                "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
                "metadata, source_mtime, source_uri, alias_of "
                "FROM documents WHERE physical_collection = ?",
                (old,),
            ).fetchall()
            from nexus.catalog.synthesizer import _owner_prefix_of as _opo
            for row in rows:
                # Preserve source_mtime + source_uri + alias_of across
                # the rename — JSONL is the rebuild source of truth, so
                # any column omitted here is reset to its default when
                # Catalog.rebuild() replays the log (review finding —
                # Reviewer B/C1, nexus-1ccq follow-up; RDR-096 P3.1
                # extended this to source_uri; meta-review extended it
                # to alias_of).
                rec = {
                    "tumbler": row[0],
                    "title": row[1],
                    "author": row[2],
                    "year": row[3],
                    "content_type": row[4],
                    "file_path": row[5],
                    "corpus": row[6],
                    "physical_collection": new,
                    "chunk_count": row[8],
                    "head_hash": row[9] or "",
                    "indexed_at": row[10] or "",
                    "meta": json.loads(row[11]) if row[11] else {},
                    "source_mtime": row[12] or 0.0,
                    "source_uri": row[13] or "",
                    "alias_of": row[14] or "",
                }
                if cat._event_sourced_enabled:
                    # Per-row event-source: write event, project to
                    # SQLite, append legacy JSONL. SQLite commit is
                    # batched at the end for efficiency.
                    meta_dict = json.loads(row[11]) if row[11] else {}
                    event = _cat_mod._make_event(
                        _DocumentRegisteredPayload(
                            doc_id=row[0],
                            owner_id=_opo(row[0]),
                            content_type=row[4] or "",
                            source_uri=row[13] or "",
                            coll_id=new,
                            title=row[1] or "",
                            source_mtime=float(row[12] or 0.0),
                            indexed_at_doc=row[10] or "",
                            tumbler=row[0],
                            author=row[2] or "",
                            year=int(row[3] or 0),
                            file_path=row[5] or "",
                            corpus=row[6] or "",
                            physical_collection=new,
                            chunk_count=int(row[8] or 0),
                            head_hash=row[9] or "",
                            indexed_at=row[10] or "",
                            alias_of=row[14] or "",
                            meta=dict(meta_dict),
                        ),
                        v=0,
                    )
                    cat._write_to_event_log(event)
                    cat._projector.apply(event)
                    cat._append_jsonl(cat._documents_path, rec)
                else:
                    cat._append_jsonl(cat._documents_path, rec)
            if not cat._event_sourced_enabled:
                cat._db.execute(
                    "UPDATE documents SET physical_collection = ? "
                    "WHERE physical_collection = ?",
                    (new, old),
                )
            cat._db.commit()
            # Shadow-emit one DocumentRegistered per renamed row with
            # the new physical_collection. The projector's INSERT OR
            # REPLACE makes the replay state converge on the new
            # collection name. Pre-fix this method emitted nothing,
            # so a replayed events.jsonl produced rows with the OLD
            # physical_collection, breaking the doctor's replay-equality
            # check. Emitting after db.commit() (same crash-window
            # discipline as unlink/bulk_unlink) keeps the event log
            # consistent with the durable SQLite state.
            #
            # Hoist the gate check above the per-row payload
            # construction: when shadow emit is OFF (the default), a
            # 10k-row rename should not pay the cost of building 10k
            # _DocumentRegisteredPayload objects only to discard them
            # in _emit_shadow_event's first line.
            # When event-sourced is ON the per-row write loop above
            # already emitted + projected each event; skip the
            # shadow-emit loop to avoid duplicate writes.
            if cat._shadow_emit_enabled and not cat._event_sourced_enabled:
                from nexus.catalog.synthesizer import _owner_prefix_of
                for row in rows:
                    meta_dict = json.loads(row[11]) if row[11] else {}
                    cat._emit_shadow_event(_cat_mod._make_event(
                        _DocumentRegisteredPayload(
                            doc_id=row[0],
                            # Use the synthesizer's helper for owner
                            # extraction so malformed tumblers (no dots)
                            # produce "" rather than the whole tumbler
                            # — matches synthesize_from_jsonl's contract.
                            owner_id=_owner_prefix_of(row[0]),
                            content_type=row[4] or "",
                            source_uri=row[13] or "",
                            coll_id=new,
                            title=row[1] or "",
                            source_mtime=float(row[12] or 0.0),
                            indexed_at_doc=row[10] or "",
                            tumbler=row[0],
                            author=row[2] or "",
                            year=int(row[3] or 0),
                            file_path=row[5] or "",
                            corpus=row[6] or "",
                            physical_collection=new,
                            chunk_count=int(row[8] or 0),
                            head_hash=row[9] or "",
                            indexed_at=row[10] or "",
                            alias_of=row[14] or "",
                            meta=dict(meta_dict),
                        ),
                        v=0,
                    ))
            return len(rows)
        finally:
            cat._release_lock(dir_fd)

    def delete_document(self, tumbler: Tumbler) -> bool:
        """Soft-delete a document: tombstone in JSONL, DELETE from SQLite.

        Links to/from this tumbler are preserved (RF-9: orphaned links intentional).
        Returns True if deleted, False if not found.
        """
        cat = self._cat
        dir_fd = cat._acquire_lock()
        try:
            entry = cat.resolve(tumbler)
            if entry is None:
                return False
            tombstone = {
                "tumbler": str(tumbler),
                "title": entry.title,
                "author": entry.author,
                "year": entry.year,
                "content_type": entry.content_type,
                "file_path": entry.file_path,
                "corpus": entry.corpus,
                "physical_collection": entry.physical_collection,
                "chunk_count": entry.chunk_count,
                "head_hash": entry.head_hash,
                "indexed_at": entry.indexed_at,
                "meta": entry.meta,
                "source_mtime": entry.source_mtime,
                "_deleted": True,
            }
            event = _cat_mod._make_event(
                _DocumentDeletedPayload(
                    doc_id=str(tumbler),
                    reason="catalog.delete_document",
                    tumbler=str(tumbler),
                ),
                v=0,
            )
            if cat._event_sourced_enabled:
                cat._write_to_event_log(event)
                cat._projector.apply(event)
                cat._db.commit()
                cat._append_jsonl(cat._documents_path, tombstone)
            else:
                cat._append_jsonl(cat._documents_path, tombstone)
                cat._db.execute(
                    "DELETE FROM documents WHERE tumbler = ?",
                    (str(tumbler),),
                )
                cat._db.commit()
                cat._emit_shadow_event(event)
            return True
        finally:
            cat._release_lock(dir_fd)

