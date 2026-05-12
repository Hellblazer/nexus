# SPDX-License-Identifier: AGPL-3.0-or-later
"""SQLite-projection consistency + rebuild (nexus-mbm extraction 5/5).

Owns the RDR-104 incremental rebuild machinery: the consistency
marker, the offset/header-hash checkpoint that drives the
five-way dispatch in :meth:`_SyncOps._ensure_consistent`
(empty-delta / bootstrap / invalidated / incremental /
corruption-escalation), and the JSONL-defrag/compact
maintenance verbs.

Composed onto ``Catalog`` as ``self._sync`` (T2Database-style
facade pattern). Public ``Catalog.rebuild`` / ``defrag`` /
``compact`` are thin delegates so the public API is unchanged;
the underscore-prefixed methods stay reachable via
``cat._ensure_consistent`` etc. for in-package callers and
existing tests.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from nexus.catalog.tumbler import read_documents, read_links, read_owners

# nexus-mbm: ``catalog`` is loaded by the time ``catalog_sync`` is
# imported (the import lives inside ``Catalog.__init__``). Reference
# the constants and module-level helpers through the module object so
# tests that ``monkeypatch.setattr("nexus.catalog.catalog._FOO", ...)``
# propagate here without re-importing — direct ``from`` imports would
# bind to the original value at load time.
from nexus.catalog import catalog as _cat_mod

if TYPE_CHECKING:
    from nexus.catalog.catalog import Catalog

_log = structlog.get_logger(__name__)


class _SyncOps:
    """Composed onto ``Catalog`` as ``self._sync``.

    Methods read catalog state via ``self._cat.<attr>`` —
    ``_db`` for SQL, ``_dir`` / ``_owners_path`` /
    ``_documents_path`` / ``_links_path`` / ``_events_path`` for
    canonical-truth files, ``_acquire_lock`` / ``_release_lock``
    for the directory flock, ``_event_sourced_enabled`` /
    ``_projector`` / ``_shadow_emit_enabled`` for the rebuild's
    five-way dispatch. The ``_last_consistency_mtime`` /
    ``bootstrap_fallback_active`` / ``degraded`` flags live on
    the Catalog instance and are read/written through ``cat``.
    """

    def __init__(self, catalog: "Catalog") -> None:
        self._cat = catalog

    def _read_consistency_marker(self) -> float:
        """Return the persisted ``_last_consistency_mtime`` or 0.0.

        nexus-wehp: stored inside the catalog SQLite as a row in the
        ``_meta`` table (created by ``CatalogDB._SCHEMA_SQL``, so reads
        never issue DDL that would race a concurrent transaction). A
        fresh SQLite cache (no row) returns 0.0, which forces a rebuild
        and preserves the pre-fix invariant that a fresh cache always
        projects from the canonical state. Read failures fall back to
        0.0 (worst case = pre-fix rebuild).
        """
        cat = self._cat
        try:
            row = cat._db.execute(
                "SELECT value FROM _meta WHERE key = ?",
                ("last_consistency_mtime",),
            ).fetchone()
            if row is None:
                return 0.0
            return float(row[0])
        except (sqlite3.OperationalError, ValueError, TypeError):
            return 0.0

    def _projection_counts(self) -> tuple[int, int]:
        """Return (document_count, link_count) for the heartbeat summary.

        Read-only; used by the post-rebuild summary line so operators
        can see the size of what they just rebuilt. Tolerates errors
        and returns ``(0, 0)`` on failure — the summary is informational
        and must never mask a real rebuild result.
        """
        cat = self._cat
        try:
            doc_row = cat._db.execute(
                "SELECT COUNT(*) FROM documents"
            ).fetchone()
            link_row = cat._db.execute(
                "SELECT COUNT(*) FROM links"
            ).fetchone()
            return (int(doc_row[0]) if doc_row else 0,
                    int(link_row[0]) if link_row else 0)
        except Exception:
            return (0, 0)

    def _write_consistency_marker(self, mtime: float) -> None:
        """Persist the highest successfully-projected canonical mtime.

        nexus-wehp: stored inside the catalog SQLite. Tolerates write
        failures silently — failing to update the marker means the next
        process will re-do the rebuild, which is correctness-preserving
        (the rebuild is idempotent at the projection level).

        RDR-104 critic Critical #2 fix: this write MUST live inside the
        same transaction as the projector writes. Pre-fix it called
        ``cat._db.commit()`` independently, which created an asymmetric
        failure window: a crash AFTER the marker commit but BEFORE the
        outer transaction's projection writes committed (or while the
        outer ``with cat._conn:`` rolled back) advanced the marker
        without advancing the projection. The next ``_ensure_consistent``
        run would observe the new marker, conclude "nothing to do", and
        permanently skip the events that should have been applied —
        silent corruption with no recovery path.

        Caller contract: this method is invoked from inside an active
        ``CatalogDB.transaction()`` block. The connection-as-context-manager
        commits the marker write atomically with the projection writes
        on successful exit; rolls both back together on any exception.
        Do NOT call ``cat._db.commit()`` here.
        """
        cat = self._cat
        try:
            cat._db.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                ("last_consistency_mtime", f"{mtime}"),
            )
        except sqlite3.OperationalError:
            # RDR-104 Round 2 Significant #1: the OperationalError
            # swallow is intentional. Marker-write failure is rare
            # (transient SQLite lock contention is the most likely
            # cause), idempotent re-replay corrects the next run, and
            # propagating would degrade the catalog (``degraded=True``)
            # for a recoverable cause. The in-memory mirror
            # ``cat._last_consistency_mtime`` is assigned post-
            # ``with`` so this instance still short-circuits its own
            # subsequent rebuilds; the next process reads the un-
            # advanced DB row and re-rebuilds, which is correct.
            pass

    def _write_offset_marker(
        self, *, offset: int, header_hash: str, window: int,
    ) -> None:
        """Persist the three RDR-104 incremental marker rows atomically.

        RDR-104 Step 2: writes ``last_applied_event_offset``,
        ``last_applied_event_header_hash``, and
        ``last_applied_event_header_window`` to ``_meta``. All three
        rows must commit together with the projector writes (and the
        ``last_consistency_mtime`` row from
        ``_write_consistency_marker``) so the marker is consistent
        with the projection state.

        Caller contract: this method is invoked from inside an active
        ``CatalogDB.transaction()`` block. The connection-as-context-
        manager commits all four marker rows atomically with the
        projection writes on successful exit; rolls them all back
        together on any exception. Do NOT call ``cat._db.commit()``
        here — see ``_write_consistency_marker`` for the same atomicity
        contract.

        Tolerates ``sqlite3.OperationalError`` for the same reasoning
        as ``_write_consistency_marker``: rare transient lock
        contention is corrected by the next idempotent re-replay.
        """
        cat = self._cat
        try:
            cat._db.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                (_cat_mod._META_KEY_LAST_OFFSET, str(offset)),
            )
            cat._db.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                (_cat_mod._META_KEY_HEADER_HASH, header_hash),
            )
            cat._db.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                (_cat_mod._META_KEY_HEADER_WINDOW, str(window)),
            )
        except sqlite3.OperationalError:
            # See _write_consistency_marker for the rationale on the
            # silent OperationalError swallow (RDR-104 Round 2 #1).
            pass

    def _read_offset_marker(self) -> tuple[int, str, int] | None:
        """Return ``(offset, header_hash, window)`` or ``None``.

        RDR-104 Step 2: returns ``None`` when any of the three rows is
        missing OR when the offset / window string is unparseable as
        ``int``. The orchestrator (Step 3) treats ``None`` as the
        bootstrap signal and falls through to full rebuild.

        Returning a partial tuple would let the orchestrator act on
        inconsistent metadata; full rebuild is the correctness-
        preserving fallback.
        """
        cat = self._cat
        try:
            rows = cat._db.execute(
                "SELECT key, value FROM _meta WHERE key IN (?, ?, ?)",
                (
                    _cat_mod._META_KEY_LAST_OFFSET,
                    _cat_mod._META_KEY_HEADER_HASH,
                    _cat_mod._META_KEY_HEADER_WINDOW,
                ),
            ).fetchall()
        except sqlite3.OperationalError:
            return None
        by_key = {key: value for key, value in rows}
        if (
            _cat_mod._META_KEY_LAST_OFFSET not in by_key
            or _cat_mod._META_KEY_HEADER_HASH not in by_key
            or _cat_mod._META_KEY_HEADER_WINDOW not in by_key
        ):
            return None
        try:
            offset = int(by_key[_cat_mod._META_KEY_LAST_OFFSET])
            window = int(by_key[_cat_mod._META_KEY_HEADER_WINDOW])
        except (TypeError, ValueError):
            return None
        return (offset, by_key[_cat_mod._META_KEY_HEADER_HASH], window)

    def _ensure_consistent(self) -> None:
        """Rebuild SQLite from the canonical truth when its mtime has advanced.

        With ``NEXUS_EVENT_SOURCED=1`` the canonical truth is
        ``events.jsonl`` (the event log IS the state per RDR-101 §"Core
        invariants"); the rebuild path replays the log through
        ``Projector.apply_all`` so a cross-process write that landed on
        events.jsonl gets re-projected into this process's SQLite cache.
        With the gate OFF the legacy JSONL files (owners/documents/links)
        remain canonical and the rebuild reads them directly.

        **Bootstrap guardrail.** When the gate is on but the legacy
        JSONL holds substantially more documents than events.jsonl
        carries DocumentRegistered events, we are looking at a freshly-
        flipped catalog whose log is sparse against the legacy state:
        the event-sourced rebuild would DELETE every legacy row and
        replay only the few new events, silently wiping the catalog.
        Refuse the event-sourced path in that scenario (fall through to
        legacy + emit a structured warning). The synthesize-log
        migration verb that historically populated the log was retired
        post Phase 5b (nexus-iftc).

        **Atomicity.** The DELETE+replay sequence runs inside
        ``CatalogDB.transaction()`` so a malformed event, a
        ``NotImplementedError`` from the v: 1 projector path, or an
        ``OperationalError`` mid-replay rolls back to the pre-DELETE
        state instead of leaving SQLite empty.

        Sets ``degraded`` flag on failure so callers can surface the stale
        state rather than silently serving outdated data (nexus-f2vp).

        Storage review S-4: skips the rebuild when no canonical file has
        been written since the last successful rebuild. For a large
        catalog this eliminates the O(entries) parse cost on every
        ``Catalog()`` construction — the MCP server instantiates one
        per tool call.
        """
        cat = self._cat
        try:
            # Track all canonical-truth sources for mtime detection so a
            # rebuild kicks in regardless of which path produced the
            # write. With the gate OFF, legacy JSONL is canonical; with
            # the gate ON, events.jsonl is canonical but legacy JSONL is
            # still written (back-compat) and a bootstrap catalog may
            # have JSONL data with an empty events.jsonl.
            paths_with_mtime: list[tuple[Path, float]] = []
            current_mtime = 0.0
            for p in (
                cat._owners_path,
                cat._documents_path,
                cat._links_path,
                cat._events_path,
            ):
                if p.exists():
                    m = p.stat().st_mtime
                    paths_with_mtime.append((p, m))
                    current_mtime = max(current_mtime, m)
            if current_mtime <= cat._last_consistency_mtime and not cat.degraded:
                return
            trigger = _cat_mod._trigger_file_label(
                paths_with_mtime, cat._last_consistency_mtime,
            )

            use_event_log = (
                cat._event_sourced_enabled
                and cat._events_path.exists()
                and cat._events_path.stat().st_size > 0
            )
            # nexus-1sy5: once the offset marker is established, the
            # bootstrap guardrail has already passed at least once —
            # ``_write_offset_marker`` is only reached from rebuild
            # branches that ran after the guardrail accepted the event
            # log. Skip the O(N) ``covers_legacy`` scan in that
            # steady state; it would otherwise dominate every post-
            # write rebuild dispatch (~838 ms on a 460K-event log) and
            # cap the RDR-104 incremental fast path well above its
            # <100 ms target. The marker check is a single
            # `SELECT key, value FROM _meta` and short-circuits before
            # the scan so the perf path is microseconds.
            marker_established = (
                use_event_log and cat._read_offset_marker() is not None
            )
            if (
                use_event_log
                and not marker_established
                and not cat._event_log_covers_legacy()
            ):
                # Bootstrap guardrail: events.jsonl is non-empty but
                # the legacy JSONL has materially more documents than
                # the event log carries DocumentRegistered events for.
                # Refuse to wipe the legacy rows; fall through to the
                # legacy rebuild and flag the state so operators see
                # it via ``nx catalog doctor`` (not just structlog).
                # nexus-iftc retired the synthesize-log migration
                # verb; the warning now points operators at the
                # ``nx catalog setup`` rebuild path.
                cat.bootstrap_fallback_active = True
                _log.warning(
                    "catalog_event_log_incomplete_falling_back_to_legacy",
                    catalog_dir=str(cat._dir),
                    note=(
                        "events.jsonl is non-empty but has fewer "
                        "DocumentRegistered events than documents.jsonl "
                        "has rows. ES writes are landing in the log "
                        "but reads come from legacy JSONL; replay "
                        "equality is silently broken. The synthesize-log "
                        "and t3-backfill-doc-id remediation verbs were "
                        "retired post Phase 5b (nexus-iftc). Restore by "
                        "deleting the catalog directory and re-running "
                        "'nx catalog setup' to bootstrap from current "
                        "T3 state."
                    ),
                )
                use_event_log = False
            else:
                cat.bootstrap_fallback_active = False
            if use_event_log:
                # RDR-104 Step 3: five-way dispatch over the event-
                # sourced rebuild paths.
                #
                #   (a) empty-delta fast path — events.jsonl unchanged,
                #       only the mtime row advances. Mandatory inside
                #       transaction() for the 4.24.4 atomicity contract.
                #   (b) bootstrap full rebuild — no offset marker yet;
                #       DELETE + replay from offset 0 and write all
                #       four marker rows.
                #   (c) invalidated full rebuild — header-hash drift
                #       OR window-size mismatch; same as bootstrap.
                #   (d) incremental — marker valid, delta non-empty;
                #       replay_from(stored_offset, limit_offset=eof)
                #       inside transaction() with apply_all(commit=
                #       False), then write all four marker rows.
                #   (e) corruption escalation — bounded iterator yields
                #       zero events from a non-empty range; escalate
                #       to (c) WITHOUT advancing the marker.
                #
                # The bulk_load_documents FTS5 fence is preserved on
                # the full-rebuild path only — the per-event projector
                # writes there number in the hundreds of thousands and
                # need the trigger-drop-and-rebuild idiom. Incremental
                # writes are bounded by the delta size (typically <100
                # events) so the per-row trigger overhead is
                # unmeasurable.
                from nexus.catalog.event_log import EventLog
                _log.debug(
                    "catalog_consistency_rebuild_event_sourced",
                    mtime=current_mtime,
                )

                eof_offset_now = cat._events_path.stat().st_size
                stored = cat._read_offset_marker()
                header_hash_now: str | None = None

                # Empty-delta fast path (Round 1 Significant #4 / Round 2 #4).
                # eof_offset_now == stored_offset means events.jsonl has not
                # been appended to since the last successful rebuild. mtime
                # ticked elsewhere (a legacy JSONL write, owners.jsonl, etc.)
                # so we landed in the rebuild branch but there is nothing to
                # replay. Advance only last_consistency_mtime — inside a
                # transaction() for the 4.24.4 atomicity contract.
                if stored is not None and stored[0] == eof_offset_now:
                    def _summary_empty(elapsed: float) -> str:
                        return (
                            f"  Catalog: rebuild triggered by {trigger} — "
                            f"empty delta → mtime-only marker advance "
                            f"in {elapsed:.1f}s"
                        )
                    with _cat_mod._rebuild_heartbeat(
                        "advancing consistency marker",
                        summary_builder=_summary_empty,
                    ):
                        with cat._db.transaction():
                            cat._write_consistency_marker(current_mtime)
                else:
                    # Decide bootstrap / invalidated / incremental.
                    do_full = False
                    invalidation: str | None = None
                    if stored is None:
                        do_full = True
                        invalidation = "bootstrap"
                    else:
                        stored_offset, stored_hash, stored_window = stored
                        if stored_window != _cat_mod._HEADER_HASH_BYTES:
                            do_full = True
                            invalidation = "window-size mismatch"
                        else:
                            header_hash_now = _cat_mod._compute_header_hash(
                                cat._events_path,
                            )
                            if stored_hash != header_hash_now:
                                do_full = True
                                invalidation = "header-hash drift"

                    if not do_full:
                        # Incremental path: replay only the bytes in
                        # [stored_offset, eof_offset_now). The bounded
                        # form is mandatory for concurrent-appender
                        # safety (Round 2 Critical #1) — without it, a
                        # writer landing between the stat() above and
                        # the iterator's read window would extend the
                        # iterator past eof_offset_now, the marker we
                        # then persist (eof_offset_now, the pre-append
                        # snapshot) would be stale below the actual
                        # applied-event tail, and the empty-delta fast
                        # path would never settle for that range.
                        stored_offset, stored_hash, stored_window = stored
                        delta_events = list(
                            EventLog(cat._dir).replay_from(
                                stored_offset,
                                limit_offset=eof_offset_now,
                            )
                        )
                        if not delta_events and stored_offset < eof_offset_now:
                            # Round 3 Significant #2: zero events from a
                            # non-empty delta range is the corruption
                            # signal. Escalate to full rebuild WITHOUT
                            # advancing the marker so the recovery is
                            # idempotent under retry.
                            do_full = True
                            invalidation = (
                                "incremental corruption "
                                "(zero events from non-empty delta)"
                            )
                        else:
                            replayed_count = len(delta_events)

                            def _summary_incremental(elapsed: float) -> str:
                                docs, links = cat._projection_counts()
                                return (
                                    f"  Catalog: rebuild triggered by "
                                    f"{trigger} — replayed "
                                    f"{replayed_count:,} events "
                                    f"incrementally → {docs:,} docs, "
                                    f"{links:,} links in {elapsed:.1f}s"
                                )

                            with _cat_mod._rebuild_heartbeat(
                                "applying incremental delta",
                                summary_builder=_summary_incremental,
                            ):
                                with cat._db.transaction():
                                    # commit=False mirrors the full-
                                    # rebuild path. A nested commit()
                                    # would defeat the rollback fence
                                    # and re-introduce the 4.24.4
                                    # ordering hazard (Round 3
                                    # Significant #3).
                                    cat._projector.apply_all(
                                        iter(delta_events), commit=False,
                                    )
                                    cat._write_consistency_marker(
                                        current_mtime,
                                    )
                                    cat._write_offset_marker(
                                        offset=eof_offset_now,
                                        header_hash=stored_hash,
                                        window=stored_window,
                                    )

                    if do_full:
                        # Bootstrap, invalidated, or escalated-corruption
                        # full rebuild. The bulk_load_documents FTS5
                        # fence is preserved here because the per-event
                        # projector writes can number in the hundreds of
                        # thousands; without the fence each replayed
                        # INSERT queues per-row hash entries that SQLite
                        # cannot merge mid-transaction (15-20 min COMMIT
                        # on a hot catalog).
                        if header_hash_now is None:
                            header_hash_now = _cat_mod._compute_header_hash(
                                cat._events_path,
                            )
                        event_count = _cat_mod._count_lines(cat._events_path)
                        invalidation_label = invalidation

                        def _summary_full(elapsed: float) -> str:
                            docs, links = cat._projection_counts()
                            qualifier = (
                                f" ({invalidation_label} → full rebuild)"
                                if invalidation_label
                                and invalidation_label != "bootstrap"
                                else ""
                            )
                            return (
                                f"  Catalog: rebuild triggered by "
                                f"{trigger} — replayed "
                                f"{event_count:,} events → {docs:,} "
                                f"docs, {links:,} links in "
                                f"{elapsed:.1f}s{qualifier}"
                            )

                        with _cat_mod._rebuild_heartbeat(
                            "rebuilding projection",
                            summary_builder=_summary_full,
                        ):
                            # RDR-108 Phase 3 (nexus-bdag):
                            # ``document_chunks`` is FK-bound to
                            # ``documents`` with ON DELETE CASCADE.
                            # The DELETE+replay rebuild below would
                            # cascade-wipe the manifest, but the
                            # projector does not re-emit ``ChunkIndexed``
                            # rows during replay (the manifest is
                            # populated by the post-store batch hook,
                            # not by the event log yet). Disable FK
                            # enforcement around the rebuild so the
                            # cascade doesn't fire; INSERTs restore
                            # valid references and we re-enable FK
                            # afterwards. PRAGMA foreign_keys is a no-op
                            # within a transaction so it must run BEFORE
                            # ``transaction()`` opens its block.
                            cat._db._conn.execute("PRAGMA foreign_keys=OFF")
                            try:
                                with cat._db.transaction() as conn:
                                    with cat._db.bulk_load_documents():
                                        conn.execute("DELETE FROM links")
                                        conn.execute("DELETE FROM documents")
                                        conn.execute("DELETE FROM owners")
                                        # Step 0 (Critical #1): see the
                                        # earlier comment for the
                                        # rationale and why the COALESCE
                                        # in ``_v0_collection_created``
                                        # is retained for the
                                        # degraded-path retry case
                                        # (Round 3 Significant #1).
                                        conn.execute("DELETE FROM collections")
                                        # commit=False — the transaction
                                        # context owns the commit
                                        # boundary; a nested commit()
                                        # would defeat the rollback
                                        # fence.
                                        cat._projector.apply_all(
                                            EventLog(cat._dir).replay(),
                                            commit=False,
                                        )
                                    # 4.24.4 atomicity contract: marker
                                    # writes happen INSIDE the same
                                    # transaction as the projection writes.
                                    # The transaction context commits all
                                    # rows atomically on success, rolls
                                    # them all back together on any
                                    # exception. Pre-4.24.4 the marker
                                    # write lived OUTSIDE this block in
                                    # _write_consistency_marker()'s own
                                    # commit(), a refactoring hazard.
                                    cat._write_consistency_marker(current_mtime)
                                    cat._write_offset_marker(
                                        offset=eof_offset_now,
                                        header_hash=header_hash_now,
                                        window=_cat_mod._HEADER_HASH_BYTES,
                                    )
                            finally:
                                cat._db._conn.execute("PRAGMA foreign_keys=ON")
            else:
                owners = read_owners(cat._owners_path) if cat._owners_path.exists() else {}
                documents = read_documents(cat._documents_path) if cat._documents_path.exists() else {}
                links_dict = read_links(cat._links_path) if cat._links_path.exists() else {}
                _log.debug("catalog_consistency_rebuild", mtime=current_mtime)
                # Pre-rebuild sizes (the bulk dicts are about to be
                # truncated and reloaded). _summary captures these by
                # closure so the post-rebuild line reports what just
                # got loaded.
                n_owners = len(owners)
                n_docs = len(documents)
                n_links = len(links_dict)

                def _summary(elapsed: float) -> str:
                    return (
                        f"  Catalog: rebuild triggered by {trigger} — "
                        f"loaded {n_owners:,} owners, {n_docs:,} docs, "
                        f"{n_links:,} links in {elapsed:.1f}s"
                    )

                with _cat_mod._rebuild_heartbeat(
                    "rebuilding projection", summary_builder=_summary,
                ):
                    # RDR-104 critic Critical #2 fix: pass current_mtime so
                    # the marker write happens INSIDE rebuild's transaction
                    # block, atomic with the projection writes.
                    cat._db.rebuild(
                        owners, documents, list(links_dict.values()),
                        consistency_mtime=current_mtime,
                    )
            # In-memory mirror of the persisted marker. The DB write is
            # already inside the rebuild transaction (event-sourced and
            # legacy paths both); this assignment exists so subsequent
            # in-process construction short-circuits without a SELECT.
            cat._last_consistency_mtime = current_mtime
            cat.degraded = False
        except Exception as exc:
            _log.warning("catalog_consistency_rebuild_failed", error=str(exc), exc_info=True)
            cat.degraded = True

    def _event_log_covers_legacy(self) -> bool:
        """Return True when events.jsonl plausibly covers documents.jsonl.

        Bootstrap guardrail for ``_ensure_consistent``: refuses to
        DELETE-and-rebuild from a sparse event log when the legacy
        JSONL still holds the majority of the catalog's content (e.g.
        an operator just flipped ``NEXUS_EVENT_SOURCED=1`` on a
        populated catalog and the first event-sourced write produced
        a one-event log).

        Cheap O(N) line counts on both files. ``documents.jsonl`` may
        contain duplicates (last-line-wins on rebuild) and tombstones
        (``_deleted=True`` markers) — count distinct non-tombstoned
        tumblers as the canonical row count. ``events.jsonl`` may
        contain tombstones too (DocumentDeleted events) — count
        DocumentRegistered events minus DocumentDeleted as the
        replayed-document count. A 5% slop tolerance avoids tripping
        on a single in-flight write or one-event drift.
        """
        cat = self._cat
        if not cat._documents_path.exists():
            return True
        try:
            registered: set[str] = set()
            tombstoned: set[str] = set()
            with cat._documents_path.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    tumbler = rec.get("tumbler")
                    if not tumbler:
                        continue
                    if rec.get("_deleted"):
                        tombstoned.add(tumbler)
                    else:
                        registered.add(tumbler)
            legacy_doc_count = len(registered - tombstoned)
            if legacy_doc_count == 0:
                return True

            from nexus.catalog import events as _ev
            # Net document registrations: DocumentRegistered − DocumentDeleted.
            # Can go negative (a dedupe-only event stream against a
            # legacy catalog produces only DocumentDeleted), which the
            # ``>= threshold`` check below relies on to fall through to
            # legacy. RDR-101 Phase 3 follow-up C (nexus-o6aa.9.8):
            # renamed from ``event_doc_count`` to make the negative
            # values intentional rather than surprising.
            net_registered = 0
            with cat._events_path.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    t = obj.get("type")
                    if t == _ev.TYPE_DOCUMENT_REGISTERED:
                        net_registered += 1
                    elif t == _ev.TYPE_DOCUMENT_DELETED:
                        net_registered -= 1

            # RDR-101 Phase 3 follow-up B (nexus-o6aa.9.7): floor the
            # threshold at 1. ``int(1 * 0.95) == 0`` and ``0 >= 0`` is
            # True, so a 1-document legacy catalog with a non-empty-but-
            # ``DocumentRegistered``-free ``events.jsonl`` (e.g. a
            # ChunkIndexed-only log from a partial Phase 2 synthesis,
            # or a dedupe-only event stream that pushes
            # ``event_doc_count`` to 0) used to bypass the guardrail
            # and silently wipe the single legacy row. The floor
            # guarantees a real DocumentRegistered must exist in the
            # log before ES rebuild is allowed at the smallest catalog
            # sizes.
            threshold = max(1, int(legacy_doc_count * 0.95))
            return net_registered >= threshold
        except Exception:
            # On any unexpected failure, refuse the event-sourced
            # rebuild (safer to fall through to legacy than to wipe).
            # nexus-8g79.6: surface at WARNING — pre-fix this returned
            # False with no log, so a persistent error silently
            # downgraded every startup to the slower legacy rebuild
            # with no operator signal.
            _log.warning(
                "should_use_event_sourced_rebuild_failed",
                exc_info=True,
            )
            return False

    def rebuild(self) -> None:
        """Rebuild SQLite from JSONL. Called at startup and after git pull."""
        cat = self._cat
        dir_fd = cat._acquire_lock()
        try:
            owners = read_owners(cat._owners_path) if cat._owners_path.exists() else {}
            documents = read_documents(cat._documents_path) if cat._documents_path.exists() else {}
            links_dict = read_links(cat._links_path) if cat._links_path.exists() else {}
            cat._db.rebuild(owners, documents, list(links_dict.values()))
        finally:
            cat._release_lock(dir_fd)

    def _defrag_unlocked(self) -> dict[str, int]:
        """Core defrag logic — caller must hold the lock."""
        cat = self._cat
        removed = {}
        for path in [cat._owners_path, cat._documents_path, cat._links_path]:
            if not path.exists():
                continue
            original_lines = sum(1 for line in path.open() if line.strip())
            seen: dict[str, str] = {}
            with path.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "owner" in obj:
                        key = obj["owner"]
                    elif "tumbler" in obj:
                        key = obj["tumbler"]
                    elif "from_t" in obj:
                        key = f"{obj['from_t']}|{obj['to_t']}|{obj['link_type']}"
                    else:
                        continue
                    seen[key] = line
            with path.open("w") as f:
                for line in seen.values():
                    f.write(line + "\n")
            removed[path.name] = original_lines - len(seen)
            # Rebuild SQLite from defragged JSONL to stay consistent
        owners = read_owners(cat._owners_path) if cat._owners_path.exists() else {}
        documents = read_documents(cat._documents_path) if cat._documents_path.exists() else {}
        links_dict = read_links(cat._links_path) if cat._links_path.exists() else {}
        cat._db.rebuild(owners, documents, list(links_dict.values()))
        return removed

    def defrag(self) -> dict[str, int]:
        """Deduplicate JSONL files — keep latest version of each live record.

        Removes duplicate overwrites but preserves tombstones (deletion markers).
        This is the safe compaction: no history is lost, deleted tumblers remain
        reserved, and the version record is intact for forensic purposes.
        Returns count of lines removed per file.
        """
        cat = self._cat
        dir_fd = cat._acquire_lock()
        try:
            return cat._defrag_unlocked()
        finally:
            cat._release_lock(dir_fd)

    def compact(self) -> dict[str, int]:
        """Full compaction: deduplicate AND remove tombstones.

        This erases deletion history — tombstoned tumblers are no longer
        visible in the JSONL (though they remain reserved via owner next_seq).
        Use defrag() for safe compaction that preserves tombstones.
        """
        cat = self._cat
        dir_fd = cat._acquire_lock()
        try:
            removed = {}
            for path, reader in [
                (cat._owners_path, read_owners),
                (cat._documents_path, read_documents),
                (cat._links_path, read_links),
            ]:
                if not path.exists():
                    continue
                original_lines = sum(1 for line in path.open() if line.strip())
                records = reader(path)
                with path.open("w") as f:
                    for record in records.values():
                        f.write(json.dumps(record.__dict__, default=str) + "\n")
                new_lines = len(records)
                removed[path.name] = original_lines - new_lines
            # Rebuild SQLite from compacted JSONL
            owners = read_owners(cat._owners_path) if cat._owners_path.exists() else {}
            documents = read_documents(cat._documents_path) if cat._documents_path.exists() else {}
            links_dict = read_links(cat._links_path) if cat._links_path.exists() else {}
            cat._db.rebuild(owners, documents, list(links_dict.values()))
            return removed
        finally:
            cat._release_lock(dir_fd)
