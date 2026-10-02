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
   time. NO safety floor here: the move is recoverable, so mass supersede
   churn from a big ``git pull`` proceeds silently instead of warning
   forever (the nexus-mr89x refusal nag this module retires).
3. **Expire** — quarantine rows older than ``NX_GC_QUARANTINE_DAYS``
   (default 14) hard-delete. The mr89x safety floor applies HERE only: a
   mass hard-delete surviving a full grace window means a manifest defect
   persisted for weeks — the one case that should still be loud.

First concrete piece of the RDR-156 soft-delete theme (nexus-70r3c).
"""
from __future__ import annotations

import os
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

def quarantine_orphans_serverside(
    db: Any, collection_name: str, quarantine_name: str,
    quarantined_at: str, sample_limit: int = 20,
) -> tuple[int, list[dict]] | None:
    """Try the server-side anti-join move. ``(moved, sample)`` or ``None``
    if the route is unavailable (caller falls back to :func:`quarantine_orphans`)."""
    fn = getattr(db, "gc_quarantine_orphans", None)
    if fn is None:
        return None
    result = fn(collection_name, quarantine_name, quarantined_at, sample_limit)
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
    *, strict: bool = False,
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
        try:
            result = fn(collection_name, quarantine_name, quarantined_at, sample_limit, row_limit)
        except Exception as exc:  # noqa: BLE001 — strict mode re-labels a mid-drain failure; otherwise re-raised unchanged
            if strict and batches:
                raise BoundedDrainIncomplete("batch failed", total_moved, None, batches) from exc
            raise
        batches += 1
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


def expire_quarantine_serverside(
    db: Any, quarantine_name: str, origin_name: str, cutoff: str,
    *, floor_fraction: float, floor_min_chunks: int, force: bool = False,
) -> tuple[int, int] | None:
    """Try the server-side grace-window expiry. ``(expired, refused)`` or
    ``None`` if the route is unavailable (caller falls back to
    :func:`expire_quarantine`)."""
    fn = getattr(db, "gc_expire_quarantine", None)
    if fn is None:
        return None
    result = fn(quarantine_name, origin_name, cutoff, floor_fraction, floor_min_chunks, force)
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
