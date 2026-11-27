# SPDX-License-Identifier: AGPL-3.0-or-later
"""The TTL sweep behind ``nx store expire`` (nexus-6pfgb.15): :func:`sweep_expired` and the report it returns.

Moved out of ``http_vector_client.py`` (nexus-sby49, file-size ratchet) so the sweep can name each note it
retires and judge a ``--dry-run`` by the same rules. ``HttpVectorClient.expire_report`` calls it;
``nexus.db.http_vector_client`` re-exports the report types, so existing imports keep working.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from nexus.db.retired_rows import skip_retired_rows

if TYPE_CHECKING:
    from nexus.db.http_vector_client import HttpVectorClient

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RetiredNote:
    """One note a TTL sweep retired (or would retire): the collection, its catalog *tumbler* and *title*, the
    instant its TTL ran out (*expires_at*, ISO-8601), and the *basis* that judged it: ``"catalog"`` (its own
    catalog TTL, nexus-6pfgb.15) or ``"chunk row"`` (a note put before the catalog held TTLs, judged by the row
    it wrote itself)."""

    collection: str
    tumbler: str
    title: str
    expires_at: str
    basis: str = "catalog"


@dataclass(frozen=True)
class ExpireReport:
    """What one TTL sweep did (nexus-6pfgb.15).

    *chunks_deleted* is the engine's own count of chunks removed (a chunk another live note
    still holds is kept and not counted). *notes_tombstoned* counts the notes that lapsed on
    their own catalog TTL and were retired, whether or not their chunks went with them.
    *failures* are ``(tumbler, reason)`` for a lapsed note that could not be retired; it is left
    live and whole, and its chunks are not deleted. *row_ttl_unattributed* counts chunks whose
    row TTL lapsed but that several notes hold, or that one note holds whose TTL the catalog does
    not record and whose own write the row is not, so the row (the last writer's) cannot say which
    note expires: they are left alone. *catalog_ttl_stale* are ``(tumbler, title)`` of lapsed notes
    left live because a chunk row naming the note records a later put than its catalog TTL keys do
    (a re-put by a client older than nexus-6pfgb.5, or by a writer that bypasses the catalog hook,
    nexus-6pfgb.30); a re-put with this client, or ``nx store delete --title``, settles each.

    *retired* names each note the sweep retired, or, when *dry_run*, would retire (8.5.8 shakeout: a run that
    names none cannot be audited). *orphan_chunks* counts the chunks with no live note whose row TTL lapsed;
    they are deleted with the rest. *dry_run* says nothing was written: *chunks_deleted* is then 0 and
    *candidate_chunks* counts the chunks the real run would send to the engine's delete, which keeps any
    chunk another live note still holds, so it can delete fewer.
    """

    chunks_deleted: int
    notes_tombstoned: int
    failures: tuple[tuple[str, str], ...] = ()
    row_ttl_unattributed: int = 0
    catalog_ttl_stale: tuple[tuple[str, str], ...] = ()
    retired: tuple[RetiredNote, ...] = ()
    orphan_chunks: int = 0
    candidate_chunks: int = 0
    dry_run: bool = False
    retired_collections_skipped: tuple[str, ...] = ()
    """Collections the sweep did not read because the registry lists them retired (RDR-229 R7): the engine refuses a
    read of one by label when it has no live successor, and it can still hold chunks. Their rows are not swept."""


class ExpireIncompleteError(RuntimeError):
    """A lapsed note could not be retired. Carries the sweep's :class:`ExpireReport`."""

    def __init__(self, report: ExpireReport) -> None:
        self.report = report
        named = "; ".join(f"{t}: {why}" for t, why in report.failures)
        super().__init__(
            f"{len(report.failures)} lapsed note(s) were not retired and stay live ({named}); "
            f"the sweep deleted {report.chunks_deleted} chunk(s) and retired "
            f"{report.notes_tombstoned} note(s). Run it again once the cause is fixed."
        )


def sweep_expired(client: HttpVectorClient, *, dry_run: bool = False) -> ExpireReport:
    """Delete all expired entries from ``knowledge__*`` collections.

    *dry_run* runs every judgement below (the catalog TTL, the row scan, the stale-TTL guard with its row
    reads) and writes nothing: no note is retracted or tombstoned, no manifest touched, no chunk deleted. The
    report then names each note the real run would retire, the chunks it would send to the engine's delete,
    and the notes it could not judge or would hold, by the same code the real run takes (8.5.8 shakeout: a
    pre-list that read chunk-row expiry missed a note the real run retired by its catalog TTL).

    WHOSE TTL (nexus-6pfgb.15). A chunk row is one per chunk text, so its ``ttl_days`` and
    ``indexed_at`` are the last writer's: of two notes with the same text, whichever was put
    last decided what this sweep saw for both. Each note is now judged by its own catalog
    entry (:mod:`nexus.catalog.note_ttl`): ``meta.ttl_days`` counted from ``meta.put_at``,
    both written at the note's own put (nexus-6pfgb.5). ``ttl_days`` absent or ``0`` (the
    retraction value a permanent re-put writes) is permanent; the catalog's ``0`` is never
    handed to :func:`~nexus.metadata_schema.is_expired`. ``put_at``, not the entry's
    ``indexed_at``, is the clock: the engine refreshes ``indexed_at`` on every manifest
    write, heals and backfills included, which would push a TTL later each time.

    Each lapsed note goes through the note delete path of ``nx store delete --title``
    (:func:`~nexus.catalog.store_hook.reap_lapsed_note_documents`: manifest retracted, then
    the document tombstoned, all or nothing per note), and only then are its chunks sent to
    :meth:`batch_delete`, whose engine anti-join keeps any chunk another live note still
    holds. The count returned is the engine's, so a lapsed note whose chunk a co-owner keeps
    is tombstoned and adds 0.

    The chunk-row scan below remains for what the catalog cannot judge: a chunk with no
    live owner (an orphan) goes, as before (nexus-wbfpw.10). A chunk with exactly one live
    owner that carries no TTL keys of its own (put before they existed) is judged by the row
    only when the row is that note's own write: its ``catalog_doc_id`` is the owner's tumbler and its
    ``title`` the owner's (:func:`~nexus.catalog.note_ttl.row_written_by`; the title because an
    ``.nxexp`` import keeps the source box's stamp while the destination mints its own tumblers). A row is the last writer's and is not
    rewritten when a co-owner leaves, so a legacy note left alone by the sweep that retired its
    keyed co-owner would otherwise be judged by that co-owner's lapsed TTL on the next sweep. A
    row that names another writer, or none, is counted in ``row_ttl_unattributed`` and left
    alone. A chunk several notes hold, or one note that does have catalog keys, is never judged
    by the row (the catalog pass above decides it), and a row that lapsed over several notes
    without keys is counted too.

    THE CATALOG'S KEYS CAN BE STALE (nexus-6pfgb.30). ``meta.ttl_days``/``put_at`` are written only by
    the put path of a client that has nexus-6pfgb.5. A re-put by an older client, or by any writer
    that bypasses the catalog hook (the ``.nxexp`` import), rewrites the chunk row and leaves the keys
    as they were, so a note its owner made permanent could look lapsed. Before a lapsed note is
    retired its own chunk rows are read (:meth:`_note_rows`; those stamped with the note's tumbler
    or carrying its title, :func:`~nexus.catalog.note_ttl.row_names_note`); one whose ``indexed_at``
    is later than the catalog's ``put_at`` plus five minutes records a put the catalog missed, and the
    note is NOT retired: it is reported in ``catalog_ttl_stale``
    (:func:`~nexus.catalog.note_ttl.catalog_ttl_stale`). Since RDR-236 Phase 4 Step 1 (nexus-6pfgb.65)
    that is the only sign: a row's TTL that differs from the catalog's, with an ``indexed_at`` that is
    not later than that, or none, holds nothing, because current clients write no ``indexed_at`` or
    ``ttl_days`` to a row and a note's TTL is the catalog's. (An entry with no readable ``put_at`` keeps
    the earlier rule: a differing row TTL counts, and the entry's own ``indexed_at`` is the clock.) Rows
    that cannot be read fail closed: the note stays and
    is reported in ``failures``. A row another note wrote last says nothing about this note, so a
    permanent re-put by an older client that a co-owner of the same text then overwrote is not seen.

    The rest of this docstring is the row scan's history.

    nexus-h8rf6.5: was missing entirely — ``nx store expire`` crashed
    with ``AttributeError`` in service mode. T3Database parity: this
    method's pre-filter is now IDENTICAL to
    :meth:`nexus.db.t3.T3Database.expire`'s own ``{"ttl_days": {"$gt":
    0}}`` (nexus-tk070.p6b, RDR-194 D5), not merely equivalent to it —
    the two implementations used to diverge in SPELLING only
    (historical: range operators landed later server-side, nexus-4l80g,
    so this method used ``$ne: 0`` as a numeric-comparison workaround
    that predated ``$gt`` and simply never got retrofitted once ``$gt``
    arrived), and this pass retires that divergence.

    WHY ``$ne: 0`` COULD NOT SIMPLY BECOME ``$ne: null`` (the
    NULL-sentinel's literal spelling) when RDR-194 D5 retired frecency's
    ``ttl_days == 0`` permanent sentinel in favor of ``ttl_days is
    None`` — traced empirically against
    ``PgVectorRepository.appendWherePredicate``: ``$ne`` binds
    ``String.valueOf(operand)``, and ``String.valueOf(None)`` in the
    JSON-decoded Java map is the literal Java ``null`` reference, whose
    ``String.valueOf`` is the four-character STRING ``"null"`` — so
    ``$ne: null`` would render as ``metadata->>'k' IS DISTINCT FROM
    'null'`` (a text-literal compare), which is TRUE for every row
    including genuinely-null ones (``metadata->>'k'`` extracts SQL NULL
    for a JSON null, and ``NULL IS DISTINCT FROM 'null'`` is true) —
    vacuously matching everything, not excluding permanent rows at all.
    ``$gt: 0`` sidesteps this entirely: its numeric-operand path is
    ``jsonb_typeof(metadata->'k') = 'number' AND
    (metadata->>'k')::numeric > 0``, which structurally excludes JSON
    null, an absent key, and any non-numeric value — exactly "give me
    TTL-bearing candidates, excluding the permanent sentinel," under
    EITHER the historical (``0``) or current (``None``) sentinel
    spelling, with no further translation needed for the flip.

    BEFORE/AFTER ROW SET (the subtlest part of this migration — verified,
    not assumed). Old ``{"ttl_days": {"$ne": 0}}`` was NULL-inclusive
    (documented in the server's own javadoc): it INCLUDED rows with an
    absent/null ``ttl_days`` in the fetched candidate set, relying on
    ``is_expired`` (below) to reject them downstream as not-expired. New
    ``{"ttl_days": {"$gt": 0}}`` EXCLUDES them at the query layer
    instead — same end result (nothing with a null/absent/non-positive
    ``ttl_days`` is ever deleted, whether by the old two-step
    fetch-then-reject or the new single-step exclusion), fewer rows
    fetched. For every row shape that exists in this repo's data today
    (absent key, explicit ``0``, explicit positive) the two predicates
    select the IDENTICAL fetched set — ``0`` and ``NULL`` both fail
    ``$gt: 0`` exactly as ``0`` alone failed the old ``$ne: 0`` (a
    legacy on-disk chunk metadata row still carrying ``ttl_days: 0``
    from before this migration — a client-side, unstructured JSON
    field with no CHECK possible, deliberately NOT rewritten by this
    migration's SQL changeset, which touched only the SQL
    ``nexus.frecency`` table (since dropped, nexus-6pfgb.42) — is excluded by ``$gt: 0`` exactly as it
    was excluded by ``$ne: 0``, so no behavior change for legacy rows
    either). The only row shape where the two predicates would ever
    diverge — a row explicitly written as JSON null — IS now producible:
    the p6b fix-pass (nexus-24rof) made ``T3Database.put`` (and the
    since-retired ``HttpVectorClient.put``) pass ``None`` through for "permanent" (rejecting
    an explicit ``ttl_days <= 0`` loudly), while ``make_chunk_metadata``
    keeps its ``0`` factory default for the indexer pipelines that have
    no --ttl concept. Both null-shaped and 0-shaped permanent rows are
    excluded from expiry by ``$gt: 0`` identically, so the divergence
    stays behavior-neutral here.

    ``is_expired`` (the authoritative Python-side check, same as T3)
    already treats a falsy ``ttl_days`` (``0``, ``None``, or absent)
    identically as "not expired" — see its own docstring for why this
    needed no logic change, only documentation, to be forward-compatible
    with the ``None`` sentinel.

    Expired IDs are accumulated per collection BEFORE deleting —
    deleting mid-pagination would shift offsets and skip rows. Server
    errors propagate (a swallowed failure would report "0 expired"
    while expired rows survive).

    nexus-o8dil.5 (RDR-191 P2, CRITICAL fix): ``nx store put --ttl``
    unconditionally registers a catalog manifest row for the note
    (``ttl_days`` never gates catalog registration —
    ``commands/store.py``'s ``put_cmd``). ``PgVectorRepository#delete``'s
    anti-join (F10c fix) refuses to delete a chash any LIVE manifest
    row still references — including the TTL note's own manifest row,
    which nothing retracted. Pre-fix (this method's original shape)
    that meant a TTL-lapsed note's chunk survived forever: no later GC
    pass reclaims it (``nx t3 gc`` treats any manifest reference,
    dangling or not, as "keep"), while this method still reported
    "Expired N entries" — a silent permanent leak with a false-success
    signal, violating the project's fail-loud-on-correctness rule.
    Fixed by retracting the manifest BEFORE deleting the chunk
    (:func:`nexus.catalog.store_hook.reap_catalog_manifest_for_chashes`
    — the same tombstone-then-delete order ``nx store delete`` uses,
    best-effort and silent per-chash on failure/ambiguity) and by
    reporting :meth:`batch_delete`'s ACTUAL deleted count rather than
    ``len(expired_ids)``: if a chash is genuinely shared with another
    still-live document, the reap correctly leaves it alone, the
    anti-join correctly refuses to delete it, and the returned total
    now correctly excludes it instead of lying about it.

    Returns the total number of chunks ACTUALLY deleted (may be less
    than the number of TTL-lapsed rows found, in the rare case one
    genuinely shares content with another still-live document).

    nexus-wbfpw.10 (RDR-192 Step 5 amendment, Sam's ruling): the where-scan
    below sets ``include_non_live=True`` -- expiry removes a TTL-lapsed
    stored note whether or not it currently has a live manifest owner. A
    live(c)-filtered scan would leave an unowned TTL-lapsed chunk
    permanently un-swept (no later GC pass reclaims it either, per this
    method's own docstring above), the exact silent-leak shape this
    method exists to close.
    """
    from nexus.catalog import note_ttl  # noqa: PLC0415 — deferred to avoid import cycle
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid import cycle
    from nexus.db import http_vector_client as hvc  # noqa: PLC0415 — the module owns _post (tests patch it there)
    from nexus.catalog.store_hook import (  # noqa: PLC0415 — deferred to avoid import cycle
        live_note_owners,
        reap_catalog_manifest_for_chashes,
        reap_lapsed_note_documents,
    )
    from nexus.db.limits import QUOTAS  # noqa: PLC0415 — command-local import (db.limits)
    from nexus.metadata_schema import chunk_row_expires_at, is_expired  # noqa: PLC0415 — circular-dep avoidance (metadata_schema)

    now = datetime.now(UTC)
    now_iso = now.isoformat()
    # nexus-tk070.p6b (RDR-194 D5): $gt 0, not $ne 0 — see this method's
    # own docstring for the full derivation (the $ne:null translation
    # this flip might suggest is vacuous server-side; $gt 0 is the
    # correct, already-precedented predicate, matching T3Database.expire).
    ttl_where = {"ttl_days": {"$gt": 0}}
    page_limit = QUOTAS.MAX_RECORDS_PER_WRITE
    total = 0
    candidates = 0
    orphans = 0
    notes = 0
    unattributed = 0
    retired: list[RetiredNote] = []
    failures: list[tuple[str, str]] = []
    ttl_stale: list[tuple[str, str]] = []
    # Opened on first use, so a sweep with no knowledge collection never touches the catalog.
    reader_box: list[Any] = []

    def catalog_reader():
        if not reader_box:
            reader_box.append(make_catalog_reader())
        return reader_box[0]

    try:
        inventory, retired_skipped = skip_retired_rows(
            client.list_collections(routing=True),  # reads name and content_type only (nexus-mjmjl)
            site="store_expire",
        )
        for entry in inventory:
            name = entry.get("name", "")
            # RDR-204 Phase 3 repoint (nexus-ft04v.26), class (c): `entry`
            # already carries `content_type` when a catalog row backs this
            # name (list_collections() joins it -- the SAME fetch this
            # loop already made, never a second lookup, and never
            # nexus.corpus's name-parsing primitives). A collection with
            # no row is simply skipped this pass -- a background TTL
            # sweep must not guess, matching T3Database.expire's
            # identical filter.
            if entry.get("content_type") != "knowledge":
                continue
            expired_row_ids: list[str] = []
            expired_row_meta: dict[str, dict] = {}  # the lapsed rows' metadata, for row_written_by
            offset = 0
            while True:
                result = hvc._post(
                    "/v1/vectors/get",
                    {
                        "collection": name,
                        "where": ttl_where,
                        "include": ["metadatas"],
                        "limit": page_limit,
                        "offset": offset,
                        "include_non_live": True,
                    },
                    tenant=client._tenant,
                )
                page_ids = result.get("ids", []) or []
                metas = result.get("metadatas", []) or []
                for doc_id, meta in zip(page_ids, metas):
                    if isinstance(meta, dict) and is_expired(meta, now_iso=now_iso):
                        expired_row_ids.append(doc_id)
                        expired_row_meta[doc_id] = meta
                offset += len(page_ids)
                if len(page_ids) < page_limit:
                    break  # last page (short or empty)

            reader = catalog_reader()
            # Which of the row-lapsed chunks the row may judge (see this method's docstring).
            targets: dict[str, None] = {}
            legacy_ids: list[str] = []
            legacy_notes: dict[str, RetiredNote] = {}  # keyless notes the row judges, by tumbler
            if expired_row_ids and reader is None:
                targets.update(dict.fromkeys(expired_row_ids))  # no catalog (SQLite opt-out): as before
                orphans += len(expired_row_ids)
            elif expired_row_ids:
                owners_by = live_note_owners(reader, name, expired_row_ids)
                for chash in expired_row_ids:
                    owners = owners_by.get(chash, [])
                    if not owners:
                        targets[chash] = None  # an orphan: nothing live to protect (nexus-wbfpw.10)
                        orphans += 1
                    elif len(owners) == 1 and (
                        owners[0].content_type == "knowledge" and not owners[0].file_path
                        and not note_ttl.has_own_keys(owners[0].meta)
                        and note_ttl.row_written_by(
                            owners[0].tumbler, expired_row_meta.get(chash), owner_title=owners[0].title)
                    ):
                        # one note, no TTL of its own on record, and the row is that note's own write
                        legacy_ids.append(chash)
                        targets[chash] = None
                        at = chunk_row_expires_at(expired_row_meta.get(chash) or {})
                        legacy_notes.setdefault(str(owners[0].tumbler), RetiredNote(
                            name, str(owners[0].tumbler), owners[0].title or "",
                            at.isoformat() if at is not None else "", "chunk row"))
                    elif not all(note_ttl.has_own_keys(o.meta) for o in owners):
                        # the row is the last writer's, and the catalog cannot say it was this owner
                        unattributed += 1
                    # else: every owner has its own TTL on record; the catalog pass below decides

            # The notes whose own TTL has lapsed, each retired through the note delete path.
            reaped = (
                reap_lapsed_note_documents(
                    name, now=now, row_reader=lambda ids, _n=name: client._note_rows(_n, ids), dry_run=dry_run)
                if reader is not None else None
            )
            held: set[str] = set()
            if reaped is not None:
                notes += len(reaped.documents)
                expiry = dict(reaped.expiries)
                retired.extend(
                    RetiredNote(name, tumbler, title, expiry.get(tumbler, "")) for tumbler, title in reaped.documents)
                failures.extend(reaped.failures)
                ttl_stale.extend(reaped.stale)
                held = set(reaped.held)
                targets.update(dict.fromkeys(c for c in reaped.chashes if c not in held))

            retired.extend(legacy_notes.values())
            candidates += len(targets)
            if dry_run:
                continue
            if legacy_ids:
                reap_catalog_manifest_for_chashes(legacy_ids, expected_collection=name)
            if targets:
                total += client.batch_delete(name, list(targets))
    finally:
        if reader_box and reader_box[0] is not None:
            reader_box[0].close()
    if notes or unattributed or failures or ttl_stale:
        _log.info(
            "store_expire_swept", notes_tombstoned=notes, chunks_deleted=total,
            row_ttl_unattributed=unattributed, failures=len(failures), catalog_ttl_stale=len(ttl_stale),
            dry_run=dry_run,
        )
    return ExpireReport(
        total, 0 if dry_run else notes, tuple(failures), unattributed, tuple(ttl_stale),
        retired=tuple(retired), orphan_chunks=orphans, candidate_chunks=candidates, dry_run=dry_run,
        retired_collections_skipped=retired_skipped,
    )
