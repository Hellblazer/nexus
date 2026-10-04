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
   refused call.)
3. **Expire** — quarantine rows older than ``NX_GC_QUARANTINE_DAYS``
   (default 14) hard-delete. NO fraction floor here either (nexus-wbfpw.74,
   Sam 2026-10-03), matching the engine's own expiry: the engine function
   (``nexus.gc_expire_quarantine``) already keeps every chash the origin's
   manifest still references, so the one case a floor guarded (a manifest
   loss persisting for the whole grace window) is covered row by row, while
   a floor wedged every bulk quarantine, because one burst ages out as ~100%
   of the sibling's client rows. ``NX_GC_FLOOR_FRACTION`` / ``NX_GC_FORCE``
   govern the move into quarantine and nothing on this path.

First concrete piece of the RDR-156 soft-delete theme (nexus-70r3c).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
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


def restore_rereferenced_bounded_serverside(
    db: Any, quarantine_name: str, origin_name: str, row_limit: int = GC_RESTORE_ROW_LIMIT_DEFAULT,
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
    """
    fn = getattr(db, "gc_restore_rereferenced_bounded", None)
    if fn is None:
        return None
    total = 0
    max_iterations = _gc_loop_max_iterations(row_limit)
    remaining = None
    for _ in range(max_iterations):
        result = fn(quarantine_name, origin_name, row_limit)
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
    """A strict bounded quarantine drain stopped before ``remaining`` reached 0.

    Raised only when :func:`quarantine_orphans_bounded_serverside` is called with
    ``strict=True`` (``nx t3 gc``). Each engine batch commits on its own, so
    ``moved`` chunks were moved and audited before the stop and stay quarantined;
    re-running the move is safe. ``reason`` is one of ``"iteration cap"``,
    ``"no progress"`` or ``"batch failed"`` (the failing call is ``__cause__``);
    ``remaining`` is the engine's last answer (``None`` for a failed batch).
    """

    def __init__(self, reason: str, moved: int, remaining: int | None, batches: int) -> None:
        self.reason, self.moved, self.remaining, self.batches = reason, moved, remaining, batches
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


#: The probe that asks the engine which quarantine siblings hold an origin's
#: chunks (:func:`resolve_quarantine_siblings` with ``probe_engine=True``). A
#: restore DRY-RUN over a ``quarantined_at`` window whose ``after_chash`` is the
#: largest possible chash selects no row, so the engine resolves the sibling set
#: (the reaper's own name for the origin plus every quarantine collection
#: holding a chunk tagged ``origin_collection`` = the origin,
#: ``PgVectorRepository.resolveQuarantineSiblings``) and answers it without
#: moving, attaching or auditing anything. The client relies on exactly that
#: (an empty-window dry-run still answers ``quarantine_collections`` and writes
#: no ``gc_audit`` row), pinned by ``VectorHandlerQuarantineRestoreRouteTest``,
#: until nexus-wbfpw.64 moves the resolution into the engine's own expire and
#: restore-rereferenced routes and this probe is retired.
_SIBLING_PROBE_SINCE = "1970-01-01T00:00:00Z"
_SIBLING_PROBE_AFTER_CHASH = "f" * 64

#: Codes of a :class:`VectorServiceError` that mean the probe route cannot
#: answer for this origin or engine (an engine that predates the sibling
#: resolution, an origin the catalog does not register or does not hold live):
#: expected, logged at info. Any other failure is logged as a warning. Either
#: way the caller still has the name candidates and so never does nothing.
_SIBLING_PROBE_EXPECTED_CODES = frozenset({400, 404, 422})


def resolve_quarantine_siblings(
    db: Any, origin_name: str, *, primary: str | None = None, probe_engine: bool = False,
) -> list[str]:
    """Every quarantine collection that may hold chunks of *origin_name*
    (nexus-wbfpw.58, RDR-192 Phase 3 critique S3).

    The client's move names its sibling from the origin's catalog ROW
    (:func:`quarantine_collection_name`), and the row is mutable: catalog-044-3
    rewrote ``owner_id`` on repo collections after chunks had been moved, so the
    name derived today is not the name a chunk was moved into yesterday. A
    chunk the client moved carries no ``quarantined_by`` tag either, so the
    engine's own expiry (vectors-026) skips it and nothing but the client
    expires it. Expiry and re-reference that look only in the row-derived
    sibling therefore skip every such chunk, for good.

    The set is the union of (in this order, deduplicated):

    1. *primary*: the row-derived sibling (or the name the caller moves into),
       so nothing the caller did before this change stops being reached;
    2. ``quarantine-<origin name>``, the reaper's own name for the origin;
    3. only with ``probe_engine=True``: what the ENGINE resolves, the set
       ``nx t3 quarantine restore`` reaches, by asking the restore route for a
       dry-run over an empty selection (see :data:`_SIBLING_PROBE_SINCE`). That
       finds a sibling by the ``origin_collection`` tag the move wrote into the
       chunks, which no rename of the origin or of its owner can disturb.

    ``probe_engine`` defaults to ``False`` on purpose: the probe costs the
    engine an unindexed EXISTS scan of every quarantine collection of the
    tenant that is not named for the origin, plus one POST, so it does not
    belong on ``nx index repo``'s hot path (per collection, every run). A new
    caller must ask for it. ``nx t3 gc`` does; the indexer does not, and a chunk
    stranded under a name neither derived name reaches waits for ``nx t3 gc``.
    nexus-wbfpw.64 resolves siblings inside the engine's own expire and
    restore-rereferenced routes and retires this probe.

    Where the engine cannot answer (a ``db`` with no restore route, an engine
    that predates sibling resolution, an origin the restore refuses) steps 1 and
    2 stand alone, which covers the catalog-044 shape (the origin's name is the
    pre-rewrite name the chunks were moved under). The reason is logged; this
    never returns an empty list and never raises for an engine answer or a
    transport failure (a local-mode timeout arrives as a bare ``OSError``).
    """
    names: list[str] = [primary if primary is not None else quarantine_collection_name(origin_name),
                        f"{QUARANTINE_PREFIX}-{origin_name}"]
    probe = getattr(db, "gc_quarantine_restore", None) if probe_engine else None
    if probe is not None:
        from nexus.db.http_vector_client import VectorServiceError  # noqa: PLC0415 — deferred: keeps this module import-light

        try:
            page = probe(
                origin_name, quarantined_since=_SIBLING_PROBE_SINCE,
                after_chash=_SIBLING_PROBE_AFTER_CHASH, limit=1, dry_run=True, reattach=False,
            )
        # OSError too: without a managed endpoint the client re-raises a bare
        # URLError / ConnectionError / TimeoutError (all OSError), and a timeout
        # is the probe's likeliest failure (it is an unbounded scan).
        except (VectorServiceError, OSError) as exc:
            code = getattr(exc, "code", None)
            emit = _log.info if code in _SIBLING_PROBE_EXPECTED_CODES else _log.warning
            emit(
                "quarantine_sibling_probe_unavailable",
                collection=origin_name, code=code, error=str(exc),
                using="row-derived and reaper names only",
            )
        else:
            found = page.get("quarantine_collections") if isinstance(page, dict) else None
            if isinstance(found, list):
                names.extend(n for n in found if isinstance(n, str))
    return list(dict.fromkeys(names))


def restore_rereferenced_across_serverside(
    db: Any, siblings: list[str], origin_name: str, *, best_effort: bool = False,
) -> int | None:
    """:func:`restore_rereferenced_bounded_serverside` (then the unbounded call,
    as the indexer always has) over every sibling in *siblings*, total restored.
    ``None`` when the client object has no restore capability at all (the same
    signal the single-sibling wrappers give, and it cannot differ per sibling).

    With ``best_effort`` a :class:`VectorServiceError` or ``OSError`` (a
    local-mode transport failure) on any sibling after the
    first is logged (``quarantine_sibling_restore_failed``, naming the sibling)
    and skipped, so an extra sibling never costs the caller the first one or
    what follows; a failure on the first still raises."""
    from nexus.db.http_vector_client import VectorServiceError  # noqa: PLC0415 — deferred: keeps this module import-light

    total = 0
    for i, sibling in enumerate(siblings):
        try:
            restored = restore_rereferenced_bounded_serverside(db, sibling, origin_name)
            if restored is None:
                restored = restore_rereferenced_serverside(db, sibling, origin_name)
        except (VectorServiceError, OSError) as exc:  # OSError: local-mode transport failures arrive bare
            if not (best_effort and i):
                raise
            _log.warning(
                "quarantine_sibling_restore_failed",
                collection=origin_name, sibling=sibling, code=getattr(exc, "code", None), error=str(exc),
            )
            continue
        if restored is None:
            return None
        total += restored
    return total


def expire_quarantine_across_serverside(
    db: Any, siblings: list[str], origin_name: str, cutoff: str,
    *, best_effort: bool = False,
) -> tuple[int, int] | None:
    """:func:`expire_quarantine_serverside` over every sibling in *siblings*,
    summed ``(expired, refused)``; ``refused`` is the chunks the origin's
    manifest still references, kept. ``None`` when the client object has no
    expiry capability.

    With ``best_effort`` a :class:`VectorServiceError` or ``OSError`` (a
    local-mode transport failure) on any sibling after the
    first is logged (``quarantine_sibling_expire_failed``, naming the sibling)
    and skipped; a failure on the first still raises. A caller that must report
    each sibling's own outcome (``nx t3 gc``) loops over
    :func:`expire_quarantine_serverside` itself."""
    from nexus.db.http_vector_client import VectorServiceError  # noqa: PLC0415 — deferred: keeps this module import-light

    expired = refused = 0
    for i, sibling in enumerate(siblings):
        try:
            result = expire_quarantine_serverside(
                db, sibling, origin_name, cutoff,
            )
        except (VectorServiceError, OSError) as exc:  # OSError: local-mode transport failures arrive bare
            if not (best_effort and i):
                raise
            _log.warning(
                "quarantine_sibling_expire_failed",
                collection=origin_name, sibling=sibling, code=getattr(exc, "code", None), error=str(exc),
            )
            continue
        if result is None:
            return None
        expired += result[0]
        refused += result[1]
    return expired, refused


#: The request the client's expiry sends in place of a floor (nexus-wbfpw.74). The engine's own test is
#: ``v_expired >= p_floor_min_chunks AND v_frac > p_floor_fraction AND NOT p_force`` with ``v_frac`` in
#: (0, 1], so a fraction of 1.0 can never trip it. Chosen over ``force=True``: the engine records no
#: ``force`` in its ``gc_audit`` row either way, but ``force`` reads as an override of a floor that is not
#: there, and would also stay an operator-visible knob on a path that has no gate to override. The route
#: still takes both fields, so they are sent, pinned to values that make the floor inert.
_NO_EXPIRY_FLOOR_FRACTION = 1.0
_NO_EXPIRY_FLOOR_MIN_CHUNKS = 100


def expire_quarantine_serverside(
    db: Any, quarantine_name: str, origin_name: str, cutoff: str,
) -> tuple[int, int] | None:
    """The server-side grace-window expiry, with no fraction floor (see the
    module docstring, step 3). ``(expired, refused)`` — ``refused`` is the chunks
    the origin's manifest still references, kept — or ``None`` if the client has
    no expiry route."""
    fn = getattr(db, "gc_expire_quarantine", None)
    if fn is None:
        return None
    result = fn(
        quarantine_name, origin_name, cutoff,
        _NO_EXPIRY_FLOOR_FRACTION, _NO_EXPIRY_FLOOR_MIN_CHUNKS, False,
    )
    return int(result.get("expired", 0)), int(result.get("refused", 0))

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
