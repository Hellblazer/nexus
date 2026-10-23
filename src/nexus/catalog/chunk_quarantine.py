# SPDX-License-Identifier: AGPL-3.0-or-later
"""Chunk quarantine — soft delete for the orphan GC (nexus-xukbj).

Instead of hard-deleting orphan chunks (or refusing over-floor sweeps with
a recurring warning — the nexus-mr89x nag), the GC MOVES orphans to a
sibling collection named ``quarantine-<origin collection name>``
(see :func:`quarantine_collection_name`). The ``quarantine-`` prefix is in NO search corpus, so quarantined chunks are
excluded from every retrieval surface by construction — no filters, no
metadata-update primitive, no schema change.

Lifecycle per GC pass (wired in ``indexer._prune_deleted_files``):

1. **Restore** — quarantined chashes that are referenced by the manifest
   again (a heal re-referenced them, or content returned) copy back to the
   origin collection and leave quarantine. Chash-keyed upsert = idempotent.
2. **Quarantine** — this pass's orphans move over with their embeddings
   (no re-embed), stamped ``quarantined_at`` + ``origin_collection`` at add
   time. The move carries the GC family's fraction floor
   (``NX_GC_FLOOR_FRACTION``, nexus-wbfpw.52), judged by the ENGINE under its
   sweep gate on the whole reapable set (see :class:`GcFloor`): a pass that
   would move more than the floor admits is refused, counted and audited
   rather than moved, and ``NX_GC_FORCE=1`` overrides it. The engine echoes
   the floor it applied; an older engine ignores the fields and moves
   unguarded, which :attr:`GcFloor.engine_applied` reports so the caller can
   say so. (The nexus-mr89x refusal nag this module retired was a client-side
   floor that re-warned on every pass; a refusal here is one audit row per
   refused state: a repeat within the hour writes none.)
3. **Expire** — quarantine rows older than ``NX_GC_QUARANTINE_DAYS``
   (default 14) hard-delete. NO fraction floor here either (nexus-wbfpw.74,
   Sam 2026-10-03), matching the engine's own expiry: the engine function
   (``nexus.gc_expire_quarantine``) already keeps every chash the origin's
   manifest still references, so the one case a floor guarded (a manifest
   loss persisting for the whole grace window) is covered row by row, while
   a floor wedged every bulk quarantine, because one burst ages out as ~100%
   of the sibling's client rows. ``NX_GC_FLOOR_FRACTION`` / ``NX_GC_FORCE``
   govern the move into quarantine and nothing on this path.
   ``NX_GC_QUARANTINE_DAYS`` governs this client expiry only. For a
   ``quarantine-knowledge__*`` sibling a newer engine also expires the client-moved
   rows, on its own ``NX_REAPER_CLIENT_QUARANTINE_RETENTION_DAYS`` (default 14),
   whatever ``NX_GC_QUARANTINE_DAYS`` says; it never touches a code, docs or rdr
   sibling, so a longer client value is honoured there and only there.
   The client expiry is a bounded, keyed drain (RDR-222 Amendment A1, nexus-1579m;
   :func:`expire_quarantine_bounded_serverside`): pages of ``GC_EXPIRE_ROW_LIMIT_DEFAULT``
   rows, each its own request key, so one expiry too large for the edge's ~30 s deadline
   no longer rolls back whole.

First concrete piece of the RDR-156 soft-delete theme (nexus-70r3c).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

QUARANTINE_PREFIX = "quarantine"

#: Days a quarantined chunk survives before the expiry pass hard-deletes it.
QUARANTINE_DAYS_DEFAULT = 14

#: Server-side ceiling on a ``gc_audit`` sample/chash list, mirrored from the
#: engine's own cap (``CatalogRepository.GC_AUDIT_MAX_CHASHES`` = 5000,
#: applied client-request-side by ``VectorHandler.clampSampleLimit`` and
#: server-side by ``gc_quarantine_orphans``'s own ``LEAST(...,5000)``,
#: catalog-033-2). A caller-supplied ``sample_limit`` above this is silently
#: clamped by the engine either way, so requesting less than this ceiling
#: only throws away forensic detail for free — nexus-brxnp: the production
#: investigation that root-caused the restore-clobber bug (T2
#: ``nexus/debug-u6d93-brxnp``) was starved by a 41,032-row quarantine pass
#: sampling only its first 20 chashes.
GC_AUDIT_MAX_CHASHES = 5000

_WRITE_BATCH = 300  # ChromaCloud MAX_RECORDS_PER_WRITE; safe everywhere


def is_quarantine_sibling_name(name: str) -> bool:
    """True when *name* is a quarantine sibling: its first ``__`` segment
    starts with ``quarantine-`` (the shape :func:`quarantine_collection_name`
    mints). The client never registers one: the engine's GC function
    registers the sibling from the origin's row when it first moves a
    chunk into it, and its first segment is not a content type, so
    deriving registration fields from the name would fail loud on a
    collection that already exists (nexus-ny7j4: ``nx collection re-embed``
    on ``quarantine-code__1-41__voyage-code-3__v1`` refused with
    ``unknown content_type 'quarantine-code'``).
    """
    from nexus.collection_shape import collection_attributes  # noqa: PLC0415 — deferred, keeps this module import-light

    # The one sanctioned name parser for the quarantine flag (RDR-204: no
    # new raw parse sites; tests/test_collection_name_parse_census.py).
    return collection_attributes({"name": name}).quarantine


def quarantine_collection_name(origin: str) -> str:
    """``code__nexus-1-1__voyage-code-3__v1`` -> its quarantine sibling
    ``quarantine-code__nexus-1-1__voyage-code-3__v1``.

    The origin's content-type stays IN the prefix segment (review 4cb743be
    C3: dropping it collided every same-owner/model origin onto one
    sibling, cross-contaminating the expiry floor), so each origin owns a
    distinct sibling. The ``quarantine-*`` prefix matches no search corpus,
    which is what keeps quarantined chunks out of every retrieval surface.
    These names are deliberately outside the strict conformance enum —
    creation passes ``strict=False`` (system-internal collections).
    """
    # RDR-204 Phase 3 THE REPOINT (nexus-ft04v.26, item 6), class (d):
    # prefers *origin*'s catalog row (authoritative, Gap 1 -- a row that
    # disagrees with the name wins), reading content_type/owner_id/
    # embedding_model off it and the name's own v<n> segment via the
    # regex-based model_version_for_collection_name (not carried by the
    # row cache; safe to still read from the name because model_version
    # is the value the CALLER chose when it rendered/found this name, not
    # an independent catalog fact that can drift the way content_type/
    # owner/model can).
    #
    # Live-tested against the real GC integration suite
    # (tests/test_rdr191_gc_serverside_prune.py): `origin` frequently has
    # NO row there (a fixture collection that exists in T3 with chunks
    # but was never registered), so unlike the read helpers this
    # deliberately does NOT fail loud on a missing row -- one
    # unregistered collection must not abort the whole GC sweep, and "the
    # RDR's backfill-and-doctor class... chunk_quarantine's sibling
    # minting IF IT MUST" is the coordinator's own named exception (ruling
    # 2026-09-08) for exactly this case. Falls to
    # split_candidate_collection_name (the shared candidate-string
    # primitive, still counted by the census, never a raw split) --
    # preserving the historical "preserve the WHOLE tail unsplit" contract
    # exactly (the docstring's C3 note: model/version must survive intact
    # for a conformant name, which the whole-remainder convention already
    # guarantees without decomposing further).
    from nexus.corpus import (  # noqa: PLC0415 — deferred to avoid import cycle (nexus.corpus)
        model_version_for_collection_name,
        split_candidate_collection_name,
    )
    from nexus.mcp_infra import get_collection_row  # noqa: PLC0415 — deferred to avoid import cycle (nexus.mcp_infra)

    row = get_collection_row(origin)
    if row is not None:
        version = model_version_for_collection_name(origin) or "v1"
        return (
            f"{QUARANTINE_PREFIX}-{row['content_type']}__{row['owner_id']}"
            f"__{row['embedding_model']}__{version}"
        )
    first, rest = split_candidate_collection_name(origin)
    if not first:
        return f"{QUARANTINE_PREFIX}-x__{origin}"
    return f"{QUARANTINE_PREFIX}-{first}__{rest}"


def quarantine_days() -> int:
    raw = os.environ.get("NX_GC_QUARANTINE_DAYS", "")
    if not raw:
        return QUARANTINE_DAYS_DEFAULT
    try:
        val = int(raw)
    except ValueError:
        val = -1
    if val < 0:
        _log.warning(
            "gc_quarantine_days_invalid",
            raw=raw, using=QUARANTINE_DAYS_DEFAULT,
        )
        return QUARANTINE_DAYS_DEFAULT
    return val


def now_stamp() -> str:
    """The GC quarantine timestamp format: ``2026-08-10T12:00:00Z``.

    Shared by the client-side stamp (:func:`quarantine_orphans`'s ``now``)
    and the RDR-191 server-side path (:func:`quarantine_orphans_serverside`
    passes this same shape through as ``quarantined_at``/compares against
    it as ``cutoff`` in :func:`expire_quarantine_serverside`) so both
    remain lexicographically comparable — see the catalog-023 changelog
    header for why that matters.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── RDR-191 Phase 1: server-side prune (catalog-023) ────────────────────────
#
# These three ``*_serverside`` wrappers try the engine's anti-join GC route
# (``HttpVectorClient.gc_quarantine_orphans`` / ``gc_restore_rereferenced`` /
# ``gc_expire_quarantine``) and return ``None`` when ``db`` has no such method
# — local/in-memory mode, where the InMemoryVectorClient unit-test double has
# no server to route to. That branch is permanent: it is about CAPABILITY, not
# engine version, so no floor bump ever retires it.
#
# The 404 branch that used to sit alongside it is GONE (retired at
# REQUIRED_ENGINE_VERSION (0, 1, 70), the tag that ships catalog-023's
# routes, by ``TestGcServersideFallbackDoesNotOutliveItsRoute`` in
# ``tests/test_engine_version.py``). It is unreachable at this floor in both
# directions, verified before deleting: a local box converges its engine to
# ``REQUIRED_ENGINE_VERSION`` on any ordinary ``nx`` command
# (``upgrade_finish.converge_engine``, unattended), and a cloud client
# refuses a below-identity managed engine outright (GH #1402) rather than
# reaching a route call. A VectorServiceError from these calls is now a real
# failure and propagates, which is the point.
#
# Callers (``indexer._prune_deleted_files``) fall back to the client-side
# fetch-diff-copy-delete path on ``None``. That client-side implementation
# stays — retiring it is RDR-191 Phase 5, a separate and much larger change.
#
# A ``None`` return is a ROUTE-UNAVAILABLE signal, never "nothing to do" —
# the caller must not mistake it for a zero-orphan result.

@dataclass
class GcFloor:
    """The optional fraction floor a caller hands the engine's move route (nexus-wbfpw.52).

    ``fraction`` and ``min_chunks`` are the GC family's (``NX_GC_FLOOR_FRACTION``,
    default 0.25, and 100); the engine refuses the move when the collection's WHOLE
    reapable set is at least ``min_chunks`` and strictly more than ``fraction`` of
    every stored chunk, judged under its sweep gate in the same call as the move.
    ``force`` is the operator override (``NX_GC_FORCE=1``): the engine skips the
    judgement.

    ``engine_applied`` is the OUTCOME, written by the ``*_serverside`` wrappers from
    the engine's own response: ``None`` until a response was read, ``True`` when the
    engine echoed the floor (it judged it, or skipped it on ``force``), ``False``
    when it did not (an older engine ignores the fields and moves unguarded). A
    caller whose move had to be guarded reads it after the call and says so.
    """

    fraction: float
    min_chunks: int
    force: bool = False
    engine_applied: bool | None = None

    def request_fields(self) -> dict[str, Any]:
        """The three additive request fields."""
        return {
            "floor_fraction": self.fraction,
            "floor_min_chunks": self.min_chunks,
            "force": self.force,
        }


class GcFloorRefused(Exception):
    """The engine refused a move over its fraction floor: nothing moved in the refused call.

    ``reapable`` and ``total`` are the engine's own counts (the whole reapable set and
    every stored chunk, judged under its sweep gate); ``moved`` is what earlier batches
    of the same drain had already moved and committed (0 when the first batch was the
    one refused, which is the usual case since every batch lowers the ratio).
    """

    def __init__(self, reapable: int | None, total: int | None, floor: GcFloor, moved: int = 0) -> None:
        self.reapable, self.total, self.floor, self.moved = reapable, total, floor, moved
        super().__init__(
            f"the engine refused the move: {reapable} reapable of {total} stored chunk(s) is over the "
            f"floor ({floor.fraction:.0%} from {floor.min_chunks} reapable chunks up)"
        )


def _floor_echoed(result: dict) -> bool:
    """Whether a move response carries the engine's floor echo (``floor.given`` is true).

    Only an engine that honours the request fields writes ``floor`` at all; an older one
    answers without the key, and a floor-aware engine answering a request that carried
    no floor writes ``{"given": false}``, which is not an echo of THIS request's floor.
    """
    echo = result.get("floor")
    return isinstance(echo, dict) and echo.get("given") is True


def _read_floor(result: dict, floor: GcFloor | None, moved_before: int) -> None:
    """Record the echo on *floor* and raise :class:`GcFloorRefused` when the engine refused."""
    if floor is None:
        return
    echoed = _floor_echoed(result)
    # Called on floor-bearing responses only; the AND keeps the flag false if a caller ever reads more than one.
    floor.engine_applied = echoed if floor.engine_applied is None else (floor.engine_applied and echoed)
    if echoed and result.get("refused") is True:
        reapable, total = result.get("reapable_count"), result.get("total_count")
        raise GcFloorRefused(
            int(reapable) if reapable is not None else None,
            int(total) if total is not None else None,
            floor, moved_before,
        )


def quarantine_orphans_serverside(
    db: Any, collection_name: str, quarantine_name: str,
    quarantined_at: str, sample_limit: int = 20, *, floor: GcFloor | None = None,
) -> tuple[int, list[dict]] | None:
    """Try the server-side anti-join move. ``(moved, sample)`` or ``None``
    if the route is unavailable (caller falls back to :func:`quarantine_orphans`).

    With *floor* (nexus-wbfpw.52) the three floor fields ride the request, the engine's
    echo is recorded on ``floor.engine_applied``, and an engine refusal raises
    :class:`GcFloorRefused`. Without it the call is the unchanged one."""
    fn = getattr(db, "gc_quarantine_orphans", None)
    if fn is None:
        return None
    if floor is None:
        result = fn(collection_name, quarantine_name, quarantined_at, sample_limit)
    else:
        result = fn(collection_name, quarantine_name, quarantined_at, sample_limit, **floor.request_fields())
        _read_floor(result, floor, 0)
    return int(result.get("moved", 0)), list(result.get("sample") or [])


def restore_rereferenced_serverside(db: Any, quarantine_name: str, origin_name: str) -> int | None:
    """Try the server-side re-reference restore. Restored count, or ``None``
    if the route is unavailable (caller falls back to :func:`restore_rereferenced`)."""
    fn = getattr(db, "gc_restore_rereferenced", None)
    if fn is None:
        return None
    return fn(quarantine_name, origin_name)


@dataclass
class SiblingResolution:
    """What the engine said about an origin's quarantine siblings, read off the FIRST page of a bounded drain
    that asked (``resolve_siblings``; nexus-wbfpw.64). One instance may be shared by the restore and the expiry of one
    collection: the move between them writes only the caller's own sibling, so the first answer holds for both.

    ``status`` is how the engine answered, and tells an engine that has the field from one that does not by the
    response, never by version:

    * ``"unasked"``: no page has asked yet.
    * ``"resolved"``: the engine answered ``quarantine_collections``; ``names`` is that list (the reaper's own name for
      the origin when registered, then every other quarantine collection holding a chunk tagged with the origin).
    * ``"timed_out"``: the engine has the field but its read ran into its statement bound
      (``sibling_resolution: "timed_out"``). The list is unknown, and asking again by any scan would hit the same bound.
    * ``"unanswered"``: the page was answered with neither key. Every supported engine (v0.1.163 and later, the
      ``REQUIRED_ENGINE_VERSION`` floor) answers one of the two. A managed endpoint below the floor is refused before any request, but a local
      engine not yet restarted after a client upgrade still answers this way, as does a defect in the engine or on
      the wire. The sibling list is unknown, and no scan is run to find it.
    * ``"unknown"``: the first page committed but its answer was lost, so nothing can be said; it is asked again.
    """

    status: str = "unasked"
    names: list[str] = field(default_factory=list)

    def wants_asking(self) -> bool:
        return self.status in ("unasked", "unknown")

    def read(self, result: Any) -> None:
        """Record the answer of a page that asked. Only a dict answer can carry either key."""
        if not isinstance(result, dict):
            self.status = "unanswered"
        elif result.get("committed_unknown_counts"):
            self.status = "unknown"
        elif isinstance(result.get("quarantine_collections"), list):
            self.status = "resolved"
            self.names = [n for n in result["quarantine_collections"] if isinstance(n, str)]
        elif result.get("sibling_resolution") == "timed_out":
            self.status = "timed_out"
        else:
            self.status = "unanswered"


def _warn_resolution_timed_out(origin_name: str) -> None:
    _log.warning(
        "quarantine_sibling_resolution_timed_out",
        collection=origin_name,
        using="row-derived and reaper names only; a sibling stranded under another name waits for the next run",
    )


def _warn_resolution_unanswered(origin_name: str) -> None:
    _log.warning(
        "quarantine_sibling_resolution_unanswered",
        collection=origin_name,
        using="row-derived and reaper names only; the engine answered neither quarantine_collections nor "
              "sibling_resolution (an engine below v0.1.163, such as a local one not restarted since a client "
              "upgrade, or a defect), so a sibling stranded under another name is not reached",
    )


def siblings_after_first_page(
    db: Any, origin_name: str, primary: str, resolution: SiblingResolution,
) -> list[str]:
    """The siblings to go on with once the first page (which was *primary*'s) has answered, *primary* first.

    ``resolved``: the engine's list, with no derived guess added (the engine knows whether the reaper's name is
    registered). Anything else is the two derived names (:func:`resolve_quarantine_siblings`), with a warning for a
    ``timed_out`` engine and for an ``unanswered`` one (a local engine below the floor, or a defect: see
    :class:`SiblingResolution`). No other scan is run to find a sibling the engine did not name. A gc that goes on
    with the derived names expires fewer chunks, never more: the row-derived name is always in a resolved list, and
    ``quarantine-<origin>`` is in it whenever it is registered (unregistered, it holds no chunk)."""
    if resolution.status == "resolved":
        return list(dict.fromkeys([primary, *resolution.names]))
    if resolution.status == "timed_out":
        _warn_resolution_timed_out(origin_name)
    elif resolution.status == "unanswered":
        _warn_resolution_unanswered(origin_name)
    return resolve_quarantine_siblings(origin_name, primary=primary)


#: Absolute ceiling on drain-loop iterations for BOTH bounded GC loops
#: (nexus-e8h5x review round 2, code-review SIGNIFICANT finding): a
#: server-side bug, or a collection under enough concurrent write pressure
#: that ``remaining`` never genuinely reaches zero, must not spin either
#: loop forever. See :func:`_gc_loop_max_iterations` for the derivation.
GC_LOOP_MAX_ITERATIONS_CEILING = 200

#: Floor on drain-loop iterations, independent of row_limit (nexus-e8h5x
#: review round 2): a caller passing a small row_limit against a
#: genuinely small collection (e.g. a test) must not be refused
#: prematurely.
GC_LOOP_MIN_ITERATIONS_FLOOR = 20


def _gc_loop_max_iterations(row_limit: int) -> int:
    """How many bounded-batch calls a drain loop may make before it gives
    up and logs a WARNING instead of looping forever (nexus-e8h5x review
    round 2).

    Derived from ``row_limit``, not one bare constant: a caller with a
    SMALLER ``row_limit`` needs proportionally MORE, smaller batches to
    drain the same population, so a cap independent of ``row_limit`` would
    refuse a legitimately large drain using a conservative ``row_limit``
    long before it finishes. At catalog-037's own measured throughput
    (``knowledge__1-1``, 5,831 rows in 22.3s -- about 261 rows/s; see
    :data:`GC_RESTORE_ROW_LIMIT_DEFAULT`'s docstring), 200 batches of the
    2,000-row default drains up to 400,000 rows -- about 9.75x the largest
    population ever observed in either direction (``code__1-1``, 41,032
    rows, 2026-09-16 owner-1.1 incident) -- in about 26 minutes worst case
    (400,000 rows / 261 rows/s). Floored at
    :data:`GC_LOOP_MIN_ITERATIONS_FLOOR` so a small ``row_limit`` is never
    cut off early, and capped at :data:`GC_LOOP_MAX_ITERATIONS_CEILING` so
    no ``row_limit``, however small, makes either loop's worst-case
    wall-clock time unbounded.
    """
    return min(
        GC_LOOP_MAX_ITERATIONS_CEILING,
        max(GC_LOOP_MIN_ITERATIONS_FLOOR, 400_000 // max(row_limit, 1)),
    )


#: Batch size for both bounded drain loops (nexus-e8h5x; review round 2
#: adds the measurement this lacked at first cut, and shares one default
#: between the two directions -- see below). catalog-037's own changeset
#: header measured ``knowledge__1-1``: 5,831 rows in 22.3s, about 261
#: rows/s. At that rate 2,000 rows/batch takes about 7.7s, well inside the
#: ~30s edge deadline and comfortably under both
#: ``DEFAULT_GC_QUARANTINE_BOUNDED_STATEMENT_TIMEOUT_MS`` and
#: ``DEFAULT_GC_RESTORE_BOUNDED_STATEMENT_TIMEOUT_MS``'s 25s bound
#: (``PgSession``). No restore-direction production measurement exists
#: independently of the quarantine one; the two SQL functions share a
#: schema, an N7 collision guard, and an audit-row shape, so there is no
#: reason to expect a materially different per-row cost, and one shared
#: default keeps the two loops' worst-case behaviour easy to reason about
#: together (see :func:`_gc_loop_max_iterations`).
GC_RESTORE_ROW_LIMIT_DEFAULT = 2000
GC_QUARANTINE_ROW_LIMIT_DEFAULT = 2000
#: Page size of the bounded client expiry (RDR-222 Amendment A1, nexus-1579m): the shared GC default.
#: The engine clamps a page at 5,000 distinct chashes, the ``gc_audit`` chash ceiling.
GC_EXPIRE_ROW_LIMIT_DEFAULT = 2000


def restore_rereferenced_bounded_serverside(
    db: Any, quarantine_name: str, origin_name: str, row_limit: int = GC_RESTORE_ROW_LIMIT_DEFAULT,
    *, resolution: SiblingResolution | None = None,
) -> int | None:
    """Try the server-side BOUNDED restore (nexus-e8h5x), looping until
    drained. Total restored count, or ``None`` if the bounded route is
    unavailable (caller falls back to :func:`restore_rereferenced_serverside`'s
    unbounded call — same capability-sensing convention as this module's
    other ``*_serverside`` wrappers: a ``None`` here is about the CLIENT
    object's capability, e.g. the in-memory unit-test double, never about
    whether there was anything to restore).

    Mirrors :func:`quarantine_orphans_bounded_serverside`'s identical loop
    shape for the opposite direction (nexus-e8h5x review round 2 wired
    that sibling — the engine's bounded quarantine route, catalog-037/
    nexus-a6mon, had shipped with no client caller at all until then).

    One call's response omitting ``remaining`` means an OLDER engine that
    already has the ``/gc/restore-rereferenced`` route but does not
    recognize ``row_limit`` — its permissive JSON body parsing silently
    ignores the unknown field and performs the UNBOUNDED restore anyway, so
    the returned ``restored`` count is already the FULL total and the loop
    stops after that one call; this is detected from the response SHAPE,
    never inferred from an engine-version comparison, so it needs no
    ``REQUIRED_ENGINE_VERSION`` floor bump to stay correct.

    The loop gives up after :func:`_gc_loop_max_iterations` batches
    (nexus-e8h5x review round 2): a server-side bug or persistent
    concurrent-write pressure that keeps ``remaining`` positive forever
    must not spin this loop forever either. That path logs a structured
    WARNING naming the collection and the still-outstanding ``remaining``
    and returns the partial total — never raises, since a partial drain is
    still real, useful progress.

    With *resolution* (nexus-wbfpw.64) the FIRST page also asks the engine for the origin's quarantine siblings
    (``resolve_siblings``) and *resolution* records the answer (:meth:`SiblingResolution.read`); later pages do not
    ask. Without it the call is exactly what it was, so a client object that does not take the keyword is never sent it.
    """
    fn = getattr(db, "gc_restore_rereferenced_bounded", None)
    if fn is None:
        return None
    total = 0
    max_iterations = _gc_loop_max_iterations(row_limit)
    remaining = None
    asking = resolution is not None
    for _ in range(max_iterations):
        if asking:
            result = fn(quarantine_name, origin_name, row_limit, resolve_siblings=True)
            resolution.read(result)  # type: ignore[union-attr]  # asking implies resolution is set
            asking = False
        else:
            result = fn(quarantine_name, origin_name, row_limit)
        if result.get("committed_unknown_counts"):
            # RDR-222: the batch committed (the ledger says so) but its response was lost and its
            # stored body is unavailable, so its count is unknown: stop and say so, never loop on
            # a guessed ``remaining``.
            _log.warning(
                "gc_restore_batch_committed_counts_unknown",
                quarantine_collection=quarantine_name, collection=origin_name, restored_before=total,
            )
            return total
        total += int(result.get("restored", 0))
        remaining = result.get("remaining")
        if remaining is None:
            # Older engine: ignored row_limit, already did the whole thing.
            return total
        if int(remaining) <= 0:
            return total
    _log.warning(
        "gc_restore_bounded_loop_iteration_cap_reached",
        quarantine_collection=quarantine_name, collection=origin_name,
        iterations=max_iterations, remaining=remaining, row_limit=row_limit,
    )
    return total


class BoundedDrainIncomplete(Exception):
    """A strict bounded drain stopped before it finished: the quarantine move or the client expiry.

    Raised only when :func:`quarantine_orphans_bounded_serverside` (``kind="quarantine"``, the default)
    or :func:`expire_quarantine_bounded_serverside` (``kind="expire"``) is called with ``strict=True``
    (``nx t3 gc``). Each engine batch (page) commits on its own, so ``moved`` is the number of rows the
    drain moved (quarantine) or expired (expire) before the stop; those stay moved or deleted and
    audited, and re-running the verb is safe. ``reason`` is one of ``"iteration cap"``, ``"no progress"``,
    ``"batch failed"`` (quarantine) or ``"page failed"`` (expire); a failing call is ``__cause__``, and for
    an expire page whose outcome the ledger could not settle it carries a note naming the request key.
    ``remaining`` is the engine's last answer for a quarantine drain (``None`` for a failed batch) and
    always ``None`` for an expire drain, whose pages answer ``more`` and no count. ``batches`` counts the
    batches or pages that were sent.
    """

    def __init__(
        self, reason: str, moved: int, remaining: int | None, batches: int, *, kind: str = "quarantine",
    ) -> None:
        self.reason, self.moved, self.remaining, self.batches, self.kind = reason, moved, remaining, batches, kind
        if kind == "expire":
            super().__init__(
                f"bounded expire drain stopped ({reason}) after {batches} page(s): {moved} expired"
            )
        else:
            super().__init__(
                f"bounded quarantine drain stopped ({reason}) after {batches} batch(es): "
                f"{moved} moved, remaining={remaining}"
            )


def quarantine_orphans_bounded_serverside(
    db: Any, collection_name: str, quarantine_name: str, quarantined_at: str,
    sample_limit: int = 20, row_limit: int = GC_QUARANTINE_ROW_LIMIT_DEFAULT,
    *, strict: bool = False, floor: GcFloor | None = None,
) -> tuple[int, list[dict]] | None:
    """Try the server-side BOUNDED quarantine sweep (nexus-e8h5x review
    round 2). The engine route (catalog-037/nexus-a6mon,
    ``gc_quarantine_orphans_bounded``, shipped in engine-service-v0.1.124/
    125) had NO client caller anywhere in this tree until this function —
    :func:`quarantine_orphans_serverside`'s unbounded call was the only
    path the indexer ever drove, so the original a6mon incident (a
    41,032-row ``code__1-1`` quarantine call cut mid-transaction at the
    ~30s edge deadline) was still fully reproducible end-to-end. Same loop
    shape as :func:`restore_rereferenced_bounded_serverside` for the
    opposite direction: loops on ``remaining`` until drained, detects an
    older engine that has the route but ignores ``row_limit`` from the
    response SHAPE (an absent ``remaining`` key), and gives up after
    :func:`_gc_loop_max_iterations` batches with a structured WARNING
    (never a raise) naming the collection and the still-outstanding
    ``remaining``.

    Returns the total ``(moved, sample)`` across every batch (``sample``
    accumulated up to ``sample_limit`` total, not just the first batch's),
    or ``None`` if the bounded route is unavailable at the client-capability
    level (caller falls back to :func:`quarantine_orphans_serverside`'s
    unbounded call).

    ``strict=True`` (``nx t3 gc``, nexus-wbfpw.18) turns every way the drain can
    end short of ``remaining == 0`` into :class:`BoundedDrainIncomplete`: the
    iteration cap with ``remaining > 0``, a batch that moved nothing while
    ``remaining > 0`` (a stuck engine would otherwise be polled to the cap), and a
    batch that raises after earlier batches already committed (the first batch's
    own failure is re-raised as is, nothing having moved). Without it (the
    indexer's end-of-run prune) the behaviour is unchanged: a warning, never a raise.

    ``floor`` (nexus-wbfpw.52) rides the FIRST batch's request only. The engine judges it
    under its sweep gate on the whole reapable set, at the cost of a whole-collection count
    inside the bounded statement, and only the first batch of a drain can be refused: each
    later batch lowers the reapable count and the total equally, so the ratio only falls.
    A refusal raises :class:`GcFloorRefused` (``moved`` is 0), strict mode included: it is
    the engine's decision, not a stalled drain. The echo is read from the first response
    alone and recorded on ``floor.engine_applied``; an engine that ignores the fields (no
    echo) moves as it always did, and the caller reads the flag to say so.
    """
    fn = getattr(db, "gc_quarantine_orphans_bounded", None)
    if fn is None:
        return None
    total_moved = 0
    sample: list[dict] = []
    max_iterations = _gc_loop_max_iterations(row_limit)
    remaining = None
    batches = 0
    for _ in range(max_iterations):
        # The floor rides the FIRST batch only (nexus-wbfpw.52): the engine judges it on a
        # whole-collection count, which is real work inside the 25 s statement bound and the
        # exclusive gate, and a later batch cannot be refused (each batch lowers the reapable
        # count and the total equally, so the ratio only falls). Only a floor-bearing response
        # is read: a later batch's ``{"given": false}`` is not an echo of this request.
        floor_bearing = floor is not None and batches == 0
        extra = floor.request_fields() if floor_bearing else {}
        try:
            result = fn(collection_name, quarantine_name, quarantined_at, sample_limit, row_limit, **extra)
        except Exception as exc:  # noqa: BLE001 — strict mode re-labels a mid-drain failure; otherwise re-raised unchanged
            if strict and batches:
                raise BoundedDrainIncomplete("batch failed", total_moved, None, batches) from exc
            raise
        batches += 1
        if result.get("committed_unknown_counts"):
            # RDR-222: the batch committed (the ledger says so) but its response was lost and its
            # stored body is unavailable, so its count and ``remaining`` are unknown. A strict drain
            # cannot claim it finished; the other stops and reports what it counted.
            _log.warning(
                "gc_quarantine_batch_committed_counts_unknown",
                collection=collection_name, quarantine_collection=quarantine_name, moved_before=total_moved,
            )
            if strict:
                raise BoundedDrainIncomplete("batch committed, counts unknown", total_moved, None, batches)
            return total_moved, sample
        if floor_bearing:
            _read_floor(result, floor, total_moved)
        batch_moved = int(result.get("moved", 0))
        total_moved += batch_moved
        if len(sample) < sample_limit:
            batch_sample = list(result.get("sample") or [])
            sample.extend(batch_sample[: sample_limit - len(sample)])
        remaining = result.get("remaining")
        if remaining is None:
            # Older engine: ignored row_limit, already did the whole thing.
            return total_moved, sample
        if int(remaining) <= 0:
            return total_moved, sample
        if strict and batch_moved <= 0:
            raise BoundedDrainIncomplete("no progress", total_moved, int(remaining), batches)
    if strict:
        raise BoundedDrainIncomplete("iteration cap", total_moved, int(remaining), batches)
    _log.warning(
        "gc_quarantine_bounded_loop_iteration_cap_reached",
        collection=collection_name, quarantine_collection=quarantine_name,
        iterations=max_iterations, remaining=remaining, row_limit=row_limit,
    )
    return total_moved, sample


def resolve_quarantine_siblings(origin_name: str, *, primary: str | None = None) -> list[str]:
    """The quarantine collections *origin_name*'s chunks may be in when the engine has not named them
    (nexus-wbfpw.58, RDR-192 Phase 3 critique S3): *primary*, the row-derived sibling (or the name the caller
    moves into), then ``quarantine-<origin name>``, the reaper's own name for the origin, deduplicated.

    The client's move names its sibling from the origin's catalog ROW (:func:`quarantine_collection_name`), and
    the row is mutable: catalog-044-3 rewrote ``owner_id`` on repo collections after chunks had been moved, so the
    name derived today is not the name a chunk was moved into yesterday. A chunk the client moved carries no
    ``quarantined_by`` tag either, so the engine's own expiry (vectors-026) skips it and nothing but the client
    expires it. These two names reach the catalog-044 shape (the origin's name is the pre-rewrite name the chunks
    were moved under); a sibling under a third name is reached only through what the engine resolves from the
    chunks' ``origin_collection`` tag, which the expire and restore-rereferenced routes do on the first page of a
    drain (``resolve_siblings``; :class:`SiblingResolution`, :func:`siblings_after_first_page`).

    This is the list a caller goes on with when that resolution did not give one: a lookup that ran into the
    engine's bound, an answer that carried neither key, a handle with no bounded route to ask. It sends nothing."""
    names: list[str] = [primary if primary is not None else quarantine_collection_name(origin_name),
                        f"{QUARANTINE_PREFIX}-{origin_name}"]
    return list(dict.fromkeys(names))


def restore_rereferenced_across_serverside(
    db: Any, siblings: list[str], origin_name: str, *, best_effort: bool = False,
    resolution: SiblingResolution | None = None,
) -> int | None:
    """:func:`restore_rereferenced_bounded_serverside` (then the unbounded call,
    as the indexer always has) over every sibling in *siblings*, total restored.
    ``None`` when the client object has no restore capability at all (the same
    signal the single-sibling wrappers give, and it cannot differ per sibling).

    With ``best_effort`` a :class:`VectorServiceError` or ``OSError`` (a
    local-mode transport failure) on any sibling after the
    first is logged (``quarantine_sibling_restore_failed``, naming the sibling)
    and skipped, so an extra sibling never costs the caller the first one or
    what follows; a failure on the first still raises.

    *siblings* are the names the caller derived, the first being the sibling it moves into. With *resolution*
    (nexus-wbfpw.64) the first sibling's first page asks the engine for the origin's siblings, and when the engine
    answers them they replace the derived names after the first (so the index path reaches a sibling stranded under a
    name the catalog row no longer derives, with no scan of its own). An engine that does not answer leaves the derived
    names alone, with a warning when it answered neither key. A *resolution* already ``resolved`` (by an earlier call for the same collection) is used without asking
    again."""
    from nexus.db.http_vector_client import VectorServiceError  # noqa: PLC0415 — deferred: keeps this module import-light

    total = 0
    queue = list(siblings)
    if resolution is not None and resolution.status == "resolved":
        queue = siblings_after_first_page(db, origin_name, queue[0], resolution)
    ask = resolution if (resolution is not None and resolution.wants_asking()) else None
    i = 0
    while i < len(queue):
        sibling = queue[i]
        first_ask = ask if i == 0 else None
        try:
            restored = restore_rereferenced_bounded_serverside(db, sibling, origin_name, resolution=first_ask)
            if restored is None:
                restored = restore_rereferenced_serverside(db, sibling, origin_name)
        except (VectorServiceError, OSError) as exc:  # OSError: local-mode transport failures arrive bare
            if not (best_effort and i):
                raise
            _log.warning(
                "quarantine_sibling_restore_failed",
                collection=origin_name, sibling=sibling, code=getattr(exc, "code", None), error=str(exc),
            )
            i += 1
            continue
        if restored is None:
            return None
        total += restored
        if first_ask is not None and first_ask.status == "resolved":
            queue = siblings_after_first_page(db, origin_name, queue[0], first_ask)
        elif first_ask is not None and first_ask.status == "timed_out":
            _warn_resolution_timed_out(origin_name)
        elif first_ask is not None and first_ask.status == "unanswered":
            _warn_resolution_unanswered(origin_name)
        i += 1
    return total


def expire_quarantine_across_serverside(
    db: Any, siblings: list[str], origin_name: str, cutoff: str,
    *, best_effort: bool = False, resolution: SiblingResolution | None = None,
) -> tuple[int, int | None] | None:
    """:func:`expire_quarantine_serverside` (non-strict) over every sibling in *siblings*, summed
    ``(expired, refused)``; ``refused`` is the chunks the origin's manifest still references, kept. A
    sibling whose ``refused`` is unknown (``None``: its drain failed before any page answered it, or
    the pages were sent without ``count_refused``) adds nothing to the sum, and the sum is ``None``
    when no sibling's is known. ``None`` altogether when the client object has no expiry capability.

    The bounded loop (:func:`expire_quarantine_bounded_serverside`, non-strict) never raises a page
    outcome, the first page's included, so with it every sibling's expiry runs and logs its own
    result. Two carve-outs propagate out of this function: a response-shape defect (``ValueError`` /
    ``TypeError`` from a parsed body with the wrong fields; the indexer re-raises it and the prune pass
    ends) and ``ProductionWriteGuardError`` (a stop-and-verify refusal; the indexer's catch-all ends
    only this collection's siblings). Only a client object with no bounded method falls back to the unbounded call, and
    there ``best_effort`` keeps its meaning: a :class:`VectorServiceError` or ``OSError`` (a
    local-mode transport failure) on any sibling after the first is logged
    (``quarantine_sibling_expire_failed``, naming the sibling) and skipped, and a failure on the
    first still raises. A caller that must report each sibling's own outcome (``nx t3 gc``) loops
    over :func:`expire_quarantine_serverside` itself.

    *resolution* works as it does for :func:`restore_rereferenced_across_serverside` (nexus-wbfpw.64): one
    already ``resolved`` for this collection is used as it stands; otherwise the first sibling's first page asks."""
    from nexus.db.http_vector_client import VectorServiceError  # noqa: PLC0415 — deferred: keeps this module import-light

    expired = 0
    refused: int | None = None
    queue = list(siblings)
    if resolution is not None and resolution.status == "resolved":
        queue = siblings_after_first_page(db, origin_name, queue[0], resolution)
    ask = resolution if (resolution is not None and resolution.wants_asking()) else None
    i = 0
    while i < len(queue):
        sibling = queue[i]
        first_ask = ask if i == 0 else None
        try:
            result = expire_quarantine_serverside(
                db, sibling, origin_name, cutoff, resolution=first_ask,
            )
        except (VectorServiceError, OSError) as exc:  # OSError: local-mode transport failures arrive bare
            if not (best_effort and i):
                raise
            _log.warning(
                "quarantine_sibling_expire_failed",
                collection=origin_name, sibling=sibling, code=getattr(exc, "code", None), error=str(exc),
            )
            i += 1
            continue
        if result is None:
            return None
        expired += result[0]
        if result[1] is not None:
            refused = (refused or 0) + result[1]
        if first_ask is not None and first_ask.status == "resolved":
            queue = siblings_after_first_page(db, origin_name, queue[0], first_ask)
        elif first_ask is not None and first_ask.status == "timed_out":
            _warn_resolution_timed_out(origin_name)
        elif first_ask is not None and first_ask.status == "unanswered":
            _warn_resolution_unanswered(origin_name)
        i += 1
    return expired, refused


#: The request the client's expiry sends in place of a floor (nexus-wbfpw.74). The engine's own test is
#: ``v_expired >= p_floor_min_chunks AND v_frac > p_floor_fraction AND NOT p_force`` with ``v_frac`` in
#: (0, 1], so a fraction of 1.0 can never trip it. Chosen over ``force=True``: the engine records no
#: ``force`` in its ``gc_audit`` row either way, but ``force`` reads as an override of a floor that is not
#: there, and would also stay an operator-visible knob on a path that has no gate to override. The route
#: still takes both fields, so they are sent, pinned to values that make the floor inert.
_NO_EXPIRY_FLOOR_FRACTION = 1.0
_NO_EXPIRY_FLOOR_MIN_CHUNKS = 100


def expire_quarantine_bounded_serverside(
    db: Any, quarantine_name: str, origin_name: str, cutoff: str,
    row_limit: int = GC_EXPIRE_ROW_LIMIT_DEFAULT, *, strict: bool = False,
    resolution: SiblingResolution | None = None,
) -> tuple[int, int | None, bool] | None:
    """The server-side BOUNDED client expiry (RDR-222 Amendment A1, nexus-1579m), looping pages until the
    sibling holds no more expirable rows. ``(expired, refused, counts_complete)``, or ``None`` if the
    client object has no bounded expiry method (caller falls back to the unbounded call, as the other
    ``*_bounded_serverside`` wrappers do).

    Each page is one keyed engine request (``HttpVectorClient.gc_expire_quarantine_bounded``): its
    deletes commit on their own, a lost response is resolved through the request ledger, and no page
    uses anything an earlier one answered (every page recomputes ``more`` from live state). Every page
    sends the inert floor (:data:`_NO_EXPIRY_FLOOR_FRACTION`) and the same *cutoff*, computed once by the
    caller. ``expired`` sums the pages. ``refused`` is the first non-null answer: the chunks the origin's
    manifest still references, kept; ``None`` when no page gave one. ``counts_complete`` is false when a
    page committed with its count lost, or a later page failed, so ``expired`` is a lower bound.

    Outcome per page (Technical Design 7.5), non-strict / ``strict`` (``nx t3 gc``):

    * ``more`` false: done. Neither ``more`` nor ``row_limit`` in the answer: an engine that predates the
      bounded form ran the unbounded expire and its answer is the total. ``row_limit`` without ``more`` is a
      response-shape defect (:class:`ValueError`).
    * No progress (``more`` with nothing expired, or ``floor_refused``): a warning / :class:`BoundedDrainIncomplete`.
    * A page committed with unknown counts: the drain continues in both modes.
    * The iteration cap with ``more`` still true: a warning / :class:`BoundedDrainIncomplete`.
    * The first page raises (:class:`VectorServiceError` including a refused request key, ``OSError`` or
      ``http.client.HTTPException``): a warning and ``(0, None, False)`` / the error re-raised unchanged.
    * A later page raises one of those: a warning and the totals so far with ``counts_complete`` false /
      :class:`BoundedDrainIncomplete` ``("page failed")`` with the error as ``__cause__`` (a page the ledger
      could not settle carries a note naming its request key; the page may have committed).

    Non-strict (the indexer's prune) never raises a page outcome, so one sibling's expiry cannot cut
    short the siblings after it, with two carve-outs. Anything outside that error set propagates in both
    modes: a response-shape defect (a parsed body with the wrong fields: ``ValueError``, ``TypeError``) is a
    data defect, and :class:`~nexus.db.service_endpoint.ProductionWriteGuardError` is a stop-and-verify
    refusal. A 200 whose body does not parse, or is not UTF-8, is not a defect but an ambiguous outcome:
    the client polls the ledger for it (``_keyed_sweep_post``), and an unsettled one arrives here as a ``VectorServiceError``.

    ``count_refused`` is sent only for a strict drain, until a page has answered it: the indexer discards
    ``refused``, and counting it costs the engine a sibling-wide scan inside the 25 s bound.

    With *resolution* (nexus-wbfpw.64) the first page also asks the engine for the origin's quarantine siblings
    (``resolve_siblings``) and *resolution* records the answer; a first page that raises records nothing, and the
    next call asks again. Later pages never ask. Without it nothing extra is sent.
    """
    import http.client  # noqa: PLC0415 — deferred: keeps this module import-light

    from nexus.db.http_vector_client import VectorServiceError  # noqa: PLC0415 — deferred: keeps this module import-light

    fn = getattr(db, "gc_expire_quarantine_bounded", None)
    if fn is None:
        return None
    expired = 0
    refused: int | None = None
    counts_complete = True
    pages = 0
    max_iterations = _gc_loop_max_iterations(row_limit)
    for _ in range(max_iterations):
        extra = {"resolve_siblings": True} if (resolution is not None and pages == 0) else {}
        try:
            result = fn(
                quarantine_name, origin_name, cutoff,
                _NO_EXPIRY_FLOOR_FRACTION, _NO_EXPIRY_FLOOR_MIN_CHUNKS, False, row_limit,
                count_refused=strict and refused is None, **extra,
            )
        except (VectorServiceError, OSError, http.client.HTTPException) as exc:
            if strict:
                if not pages:
                    raise
                raise BoundedDrainIncomplete("page failed", expired, None, pages, kind="expire") from exc
            _log.warning(
                "gc_expire_bounded_page_failed",
                collection=origin_name, quarantine_collection=quarantine_name, page=pages + 1,
                expired_before=expired, error=str(exc)[:300], notes=list(getattr(exc, "__notes__", [])),
            )
            return expired, refused, False
        pages += 1
        if extra:
            resolution.read(result)  # type: ignore[union-attr]  # extra is only set when resolution is
        if result.get("committed_unknown_counts"):
            # RDR-222: the page committed (the ledger says so) but its response was lost and its stored body
            # is unavailable, so its count and ``more`` are unknown. The next page recomputes both from live
            # state, so the drain goes on; only ``expired`` becomes a lower bound.
            counts_complete = False
            _log.warning(
                "gc_expire_batch_committed_counts_unknown",
                collection=origin_name, quarantine_collection=quarantine_name, expired_before=expired,
            )
            continue
        page_expired = int(result.get("expired", 0))
        expired += page_expired
        if refused is None and result.get("refused") is not None:
            refused = int(result["refused"])
        more = result.get("more")
        if more is None:
            if result.get("row_limit") is not None:
                # A bounded engine always answers row_limit. A body that carries it but lost `more` is not
                # an older engine's complete answer; reading it as one would report a partial drain as done.
                raise ValueError(
                    f"expire-quarantine answer for {quarantine_name} has row_limit but no 'more': {result!r}"
                )
            # Older engine (neither key): ignored row_limit, already did the whole thing.
            return expired, refused, counts_complete
        if result.get("floor_refused") or (more and page_expired <= 0):
            if strict:
                raise BoundedDrainIncomplete("no progress", expired, None, pages, kind="expire")
            _log.warning(
                "gc_expire_bounded_no_progress",
                collection=origin_name, quarantine_collection=quarantine_name, page=pages,
                floor_refused=bool(result.get("floor_refused")), expired=expired,
            )
            return expired, refused, counts_complete
        if not more:
            return expired, refused, counts_complete
    if strict:
        raise BoundedDrainIncomplete("iteration cap", expired, None, pages, kind="expire")
    _log.warning(
        "gc_expire_bounded_loop_iteration_cap_reached",
        collection=origin_name, quarantine_collection=quarantine_name,
        iterations=max_iterations, row_limit=row_limit, expired=expired,
    )
    return expired, refused, counts_complete


def expire_quarantine_serverside(
    db: Any, quarantine_name: str, origin_name: str, cutoff: str, *, strict: bool = False,
    resolution: SiblingResolution | None = None,
) -> tuple[int, int | None, bool] | None:
    """The server-side grace-window expiry, with no fraction floor (see the module docstring, step 3).
    ``(expired, refused, counts_complete)`` — ``refused`` is the chunks the origin's manifest still
    references, kept (``None`` when unknown), ``counts_complete`` false when ``expired`` is a lower
    bound — or ``None`` if the client has no expiry route.

    Tries the bounded, keyed loop first (:func:`expire_quarantine_bounded_serverside`; ``strict`` is
    ``nx t3 gc``'s), and falls back to the unbounded call for a client object that lacks it (the
    in-memory double): a ``None`` from the loop is about the CLIENT object's capability, never about
    whether there was anything to expire.

    *resolution* is passed to the bounded loop (nexus-wbfpw.64); the unbounded fallback never asks."""
    bounded = expire_quarantine_bounded_serverside(
        db, quarantine_name, origin_name, cutoff, strict=strict, resolution=resolution,
    )
    if bounded is not None:
        return bounded
    fn = getattr(db, "gc_expire_quarantine", None)
    if fn is None:
        return None
    result = fn(
        quarantine_name, origin_name, cutoff,
        _NO_EXPIRY_FLOOR_FRACTION, _NO_EXPIRY_FLOOR_MIN_CHUNKS, False,
    )
    return int(result.get("expired", 0)), int(result.get("refused", 0)), True

# _fetch_full, _upsert_full, quarantine_orphans, restore_rereferenced,
# and expire_quarantine (the client-side fetch-diff-copy-delete quarantine
# lifecycle) are DELETED here (RDR-191 Phase 6, nexus-o8dil.33, 2026-08-15)
# — their one caller, indexer._prune_deleted_files' client-side fallback
# sweep, is retired alongside them (see that function's own docstring for
# the full rationale: the manifest-chunk FK, catalog-029, makes the
# completeness apparatus this lifecycle existed to prove correct
# unreachable by construction). The *_serverside siblings above
# (quarantine_orphans_serverside / restore_rereferenced_serverside /
# expire_quarantine_serverside, RDR-191 Phase 1, catalog-023) are the ONLY
# quarantine lifecycle left — every currently supported deployment has
# their routes (REQUIRED_ENGINE_VERSION is far past catalog-023).
