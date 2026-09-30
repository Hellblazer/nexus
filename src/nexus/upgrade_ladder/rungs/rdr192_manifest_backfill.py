# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-192 (nexus-wbfpw.41): census and backfill legacy-unmanifested chunks on
every install, and record that it happened.

WHY THIS IS A RUNG. RDR-192 Phase 2 made ``live(c)`` the engine's one liveness
predicate: a chunk with no live manifest owner is invisible to search and get.
Phase 1 censused and backfilled the legacy class (a note stored before
nexus-b6enc, so it has a catalog document but never got a manifest row) by
hand, on the operator tenant only (nexus-wbfpw.6/.7/.32). Every other
install took the engine with the client pin, got ``live(c)`` at once, and was
never censused: its legacy notes went dark, and once the reaper
(nexus-2x9xa, ``reapable(c)``) ships it would DELETE them after the grace
window. Nothing at upgrade time repaired that.

The upgrade ladder (RDR-185) is the standing mechanism for a data transition
that must converge on every install: ``nx upgrade`` walks it after the
preconditions have brought the engine to the pinned tag, each rung detects
from live state, converges idempotently, and records completion ONLY after a
fresh ``verify()`` (RDR-142). The record lands in the engine's
``nexus.ladder_completions`` (per tenant, no client-side store), which is also
where the engine reaper reads it. Hence a rung, not a doctor row (a doctor
row repairs nothing and leaves no record the reaper can read) and not an
ad-hoc hook (no completion fact, no re-run discipline).

WHAT IT DOES.
  detect  : converged at once when the completion is already on file (no
            census: ``nx upgrade --auto`` walks the ladder at every session
            start, see :meth:`Rdr192ManifestBackfillRung.detect`). Otherwise a
            read-only census of every non-quarantine collection; pending when
            any collection holds a ``legacy-unmanifested`` (or an
            ``unclassified``) chunk. A census that cannot run is PENDING with
            the reason, never converged.
  converge: ``backfill_manifest_for_collection(only_gapped=True)`` on each
            collection with a legacy chunk, re-censusing between passes until
            the count reads zero or a pass makes no progress (bounded by
            ``max_passes``). The backfill is document-driven and idempotent
            (``write_manifest`` is an atomic replace); it never deletes a
            chunk. The engine being unreachable, or older than the census
            route, DEFERS (non-fatal, retried at the next ``nx upgrade``).
  verify  : a FRESH census that read every collection and found zero legacy
            and zero unclassified chunks. Unknown (census failed) is not
            reached.

A tenant with no collections is converged: the census succeeded and found
nothing, and that record is what lets the reaper run there at all.

WHAT IT DOES NOT DO. It does not touch the ``superseded``, ``dead-owner`` or
``no-owner`` buckets: those are the reaper's input by design, not a backfill
target. It writes no ``.nxexp`` owner and never removes a chunk.

RESIDUAL. A legacy chunk the backfill cannot heal (its owner is registered
under a different collection, a chash divergence, a zero-chunk match) leaves
the census above zero. ``verify()`` then refuses, the walk reports
``verify-failed`` with the collections and the remedy, and NO completion is
recorded, so the reaper stays refused on that tenant until an operator
resolves it. That is the designed safe outcome; it is not silent.

THE REAPER GATE. :func:`rdr192_backfill_complete` and
:func:`require_rdr192_backfill_complete` are the client-side reads of the
completion fact. The engine side is ``Rdr192BackfillGate`` (Java,
``service/src/main/java/dev/nexus/service/db/``), keyed on the same
:data:`RUNG_NAME`; ``tests/upgrade/test_rdr192_manifest_backfill_rung.py``
pins the two literals equal.

COMPLETION IS PER TENANT AND WRITTEN BY WHOEVER RUNS ``nx upgrade`` AGAINST
THAT TENANT. A cloud tenant that no client has upgraded since this rung
shipped has no record and the gate stays closed: that is the intended
fail-safe, not a gap to paper over.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from nexus.upgrade_ladder.protocol import (
    CompletionLedger,
    ConvergeOutcome,
    ConvergeResult,
    ProgressReporter,
    RungStatus,
)
from nexus.upgrade_ladder.registry import RUNG_RDR192_MANIFEST_BACKFILL

_log = structlog.get_logger(__name__)

#: The ladder rung name. ``Rdr192BackfillGate.RUNG_NAME`` (Java) is the same
#: literal; a test pins the two equal.
RUNG_NAME = RUNG_RDR192_MANIFEST_BACKFILL

#: Backfill passes per converge call. Each pass backfills every collection
#: still holding a legacy chunk and re-censuses; the loop also ends the first
#: time a pass fails to lower the legacy total. The backfill is one
#: document-driven sweep, so one pass heals everything it can; a second pass
#: exists for a collection whose first pass skipped a document a sibling
#: write then unblocked.
DEFAULT_MAX_PASSES = 3

_REMEDY = (
    "Run `nx t3 census-manifest-less --all` to list them with their owners, "
    "then `nx t3 backfill-manifest -c <collection> --no-dry-run --only-gapped`; "
    "a skipped document is reported per class. The reaper stays refused on "
    "this tenant until the census reads zero."
)


class CensusUnavailable(RuntimeError):
    """The census could not be taken: engine unreachable, or it predates the
    census route. Distinct from a census that ran and found something."""


class BackfillIncompleteError(RuntimeError):
    """The RDR-192 legacy-unmanifested backfill has not completed on this
    tenant, so the reaper must not run."""


@dataclass(frozen=True)
class CollectionReading:
    """One collection's census totals for the two blocking buckets."""

    collection: str
    legacy_unmanifested: int
    unclassified: int


@dataclass(frozen=True)
class CensusReading:
    """A completed census over the tenant. Having this object at all means
    the listing and every per-collection census SUCCEEDED; a census that
    could not run raises :class:`CensusUnavailable` instead."""

    collections: tuple[CollectionReading, ...]

    @property
    def legacy_total(self) -> int:
        return sum(c.legacy_unmanifested for c in self.collections)

    @property
    def unclassified_total(self) -> int:
        return sum(c.unclassified for c in self.collections)

    @property
    def legacy_collections(self) -> tuple[CollectionReading, ...]:
        return tuple(c for c in self.collections if c.legacy_unmanifested > 0)

    @property
    def clean(self) -> bool:
        return self.legacy_total == 0 and self.unclassified_total == 0

    def describe_residual(self) -> str:
        parts = [
            f"{c.collection}: {c.legacy_unmanifested} legacy-unmanifested"
            for c in self.legacy_collections
        ] + [
            f"{c.collection}: {c.unclassified} unclassified"
            for c in self.collections if c.unclassified > 0
        ]
        return "; ".join(parts)


CensusFn = Callable[[], CensusReading]
#: Backfill one collection for real, ``only_gapped``; returns chunks written.
BackfillFn = Callable[[str], int]


def _default_census() -> CensusReading:
    """Census every non-quarantine collection via the engine's read-only
    manifest-less-census route. ``limit=1`` because only the collection-wide
    ``totals`` are read, which the route reports identically on every page."""
    from nexus.db import make_t3  # noqa: PLC0415 — deferred; keeps cold CLI start cheap and breaks an import cycle
    from nexus.db.http_vector_client import VectorServiceError  # noqa: PLC0415 — deferred; same reason

    def reach(call: Callable[[], Any]) -> Any:
        """Run one engine-reaching call. Everything that stops the call from
        completing (an unresolvable endpoint, a malformed config.yml read
        while resolving it, a refused connection, a timeout, an HTTP error)
        means the census could not be taken, which is "unknown" and defers.
        Only this call is wrapped: parsing what came back is not, so a bug in
        that parsing stays loud."""
        try:
            return call()
        except VectorServiceError as exc:
            if exc.code == 404:
                raise CensusUnavailable(
                    "the connected engine predates the manifest-less-census route "
                    "(RDR-192 S2); `nx upgrade` converges the engine to the pinned tag first"
                ) from exc
            raise CensusUnavailable(f"the census request failed: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 — see the docstring: any failure to reach the engine is "census unavailable"
            raise CensusUnavailable(
                f"the engine could not be reached: {type(exc).__name__}: {exc}"
            ) from exc

    client = reach(make_t3)
    rows = reach(lambda: client.list_collections(strict=True))
    readings: list[CollectionReading] = []
    for row in rows:
        # Catalog-authoritative, never a name-prefix parse (RDR-204):
        # quarantine siblings are out of the census by construction.
        if row.get("lifecycle_state") == "quarantine":
            continue
        name = row["name"]
        totals = reach(lambda n=name: client.manifest_less_census(n, limit=1, offset=0)).get("totals") or {}
        readings.append(CollectionReading(
            collection=name,
            legacy_unmanifested=int(totals.get("legacy-unmanifested", 0)),
            unclassified=int(totals.get("unclassified", 0)),
        ))
    return CensusReading(collections=tuple(readings))


def _default_backfill(collection: str) -> int:
    """The same call ``nx t3 backfill-manifest -c <collection> --no-dry-run
    --only-gapped`` makes. Returns the chunk manifest rows written."""
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred; keeps cold CLI start cheap
    from nexus.catalog.manifest_backfill import backfill_manifest_for_collection  # noqa: PLC0415 — deferred; same reason
    from nexus.db import make_t3  # noqa: PLC0415 — deferred; same reason

    catalog = make_catalog_reader()
    if catalog is None:
        # No catalog means no document to own a chunk: nothing is
        # backfillable, and the census still reads what it reads.
        return 0
    result = backfill_manifest_for_collection(
        catalog, make_t3(), collection, dry_run=False, only_gapped=True,
    )
    return result.chunks_written


def _recorded_on_file() -> bool:
    """True when the engine's ledger holds this tenant's completion record.
    A ledger that cannot be read is 'not recorded' (never an exception): the
    caller then takes the census, which reports the outage itself."""
    try:
        return RUNG_NAME in _verified_rungs(None)
    except Exception as exc:  # noqa: BLE001 — unreadable ledger is not a record; detect() falls through to the census
        _log.debug("rdr192_backfill_recorded_probe_failed", error=str(exc))
        return False


@dataclass
class Rdr192ManifestBackfillRung:
    """The ladder rung. Seams (``census_fn``, ``backfill_fn``) are constructor
    injected; the defaults call the engine."""

    census_fn: CensusFn = _default_census
    backfill_fn: BackfillFn = _default_backfill
    #: Is the completion already on file? Default reads the engine's ledger.
    recorded_fn: Callable[[], bool] = _recorded_on_file
    max_passes: int = DEFAULT_MAX_PASSES
    name: str = RUNG_NAME
    _errors: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _verify_detail: str = field(default="", init=False, repr=False)

    # ── detect ───────────────────────────────────────────────────────────────

    def detect(self) -> RungStatus:
        """Recorded means converged, without a census.

        ``nx upgrade --auto`` runs at every SessionStart and the runner calls
        ``detect()`` on every walk, so a census here would cost one engine
        request per collection each session: measured 2026-09-27 on the
        operator tenant at p50 23 ms and max 4.4 s per collection over 95
        collections. A completion is only ever written after a fresh census
        read zero (``verify()``), and the producers of new legacy chunks were
        closed by RDR-192 Steps 3a/3b, so the record is trusted here. That is
        a deliberate departure from re-deriving on every walk; an operator
        can re-derive on demand with ``nx t3 census-manifest-less --all
        --require-zero legacy-unmanifested``. An unreadable ledger is not a
        record: it falls through to the census."""
        if self._recorded():
            return RungStatus(applicable=True, converged=True)
        try:
            reading = self.census_fn()
        except CensusUnavailable as exc:
            return RungStatus(
                applicable=True, converged=False,
                pending_detail=f"cannot take the RDR-192 census: {exc}",
            )
        if reading.clean:
            return RungStatus(applicable=True, converged=True)
        return RungStatus(
            applicable=True, converged=False,
            pending_detail=(
                f"{reading.legacy_total} legacy-unmanifested and "
                f"{reading.unclassified_total} unclassified chunk(s) — "
                f"{reading.describe_residual()}"
            ),
        )

    def _recorded(self) -> bool:
        try:
            return bool(self.recorded_fn())
        except Exception as exc:  # noqa: BLE001 — an injected probe that raises is 'not recorded' too
            _log.debug("rdr192_backfill_recorded_probe_failed", error=str(exc))
            return False

    # ── converge ─────────────────────────────────────────────────────────────

    def converge(self, report: ProgressReporter) -> ConvergeResult:
        self._errors.clear()
        try:
            reading = self.census_fn()
            for pass_number in range(1, self.max_passes + 1):
                targets = reading.legacy_collections
                if not targets:
                    break
                for target in targets:
                    try:
                        written = self.backfill_fn(target.collection)
                    except Exception as exc:  # noqa: BLE001 — one collection's failure must not stop the rest; verify() re-censuses and names what remains
                        self._errors[target.collection] = f"{type(exc).__name__}: {exc}"
                        _log.warning(
                            "rdr192_backfill_collection_failed",
                            collection=target.collection, error=str(exc),
                        )
                        continue
                    report.emit(
                        "rdr192_backfill_collection_done",
                        collection=target.collection, pass_number=pass_number,
                        legacy_before=target.legacy_unmanifested,
                        manifest_rows_written=written,
                    )
                after = self.census_fn()
                if after.legacy_total >= reading.legacy_total:
                    reading = after
                    break  # no progress: the residual is not this rung's to heal
                reading = after
        except CensusUnavailable as exc:
            return ConvergeResult(ConvergeOutcome.DEFERRED, detail=str(exc))
        return ConvergeResult(ConvergeOutcome.COMPLETED)

    # ── verify ───────────────────────────────────────────────────────────────

    def verify(self) -> bool:
        """A fresh census that read every collection and found nothing to
        backfill. Unknown is not reached: a census that cannot run returns
        False with the reason, it never falls back to converge's self-report."""
        try:
            reading = self.census_fn()
        except CensusUnavailable as exc:
            self._verify_detail = (
                f"the RDR-192 census could not run, so completion is not "
                f"recorded: {exc}"
            )
            return False
        if reading.clean:
            self._verify_detail = ""
            return True
        failed = "".join(
            f" Backfill error in {name}: {err}." for name, err in sorted(self._errors.items())
        )
        self._verify_detail = (
            f"{reading.legacy_total} legacy-unmanifested and "
            f"{reading.unclassified_total} unclassified chunk(s) remain "
            f"({reading.describe_residual()}).{failed} {_REMEDY}"
        )
        return False

    def verify_detail(self) -> str:
        return self._verify_detail


# ── the reaper's gate (client side) ──────────────────────────────────────────


def rdr192_backfill_complete(ledger: CompletionLedger | None = None) -> bool:
    """True when this tenant has a verified completion record for the RDR-192
    backfill rung. A ledger that cannot be read answers False: the reaper
    fails closed, never open. ``ledger`` defaults to the engine-backed
    ``HttpLadderStore`` for the current tenant."""
    try:
        return RUNG_NAME in _verified_rungs(ledger)
    except Exception as exc:  # noqa: BLE001 — a gate that cannot read its fact is closed
        _log.warning("rdr192_backfill_gate_unreadable", error=str(exc))
        return False


def require_rdr192_backfill_complete(ledger: CompletionLedger | None = None) -> None:
    """Raise :class:`BackfillIncompleteError` unless the backfill completed on
    this tenant. The call every client-driven destructive path for
    manifest-less chunks makes before it deletes one."""
    try:
        verified = _verified_rungs(ledger)
    except Exception as exc:  # noqa: BLE001 — fail closed, and say why
        raise BackfillIncompleteError(
            f"cannot confirm the RDR-192 manifest backfill completed on this tenant "
            f"(reading the completion ledger failed: {exc}); refusing to reap manifest-less chunks."
        ) from exc
    if RUNG_NAME not in verified:
        raise BackfillIncompleteError(
            f"the RDR-192 manifest backfill has not completed on this tenant "
            f"(no verified '{RUNG_NAME}' completion); refusing to reap manifest-less chunks. "
            f"Run `nx upgrade` to census and backfill legacy-unmanifested chunks first."
        )


def _verified_rungs(ledger: CompletionLedger | None) -> frozenset[str]:
    if ledger is not None:
        return ledger.verified_rungs()
    from nexus.upgrade_ladder.http_store import HttpLadderStore  # noqa: PLC0415 — deferred; keeps cold CLI start cheap

    with HttpLadderStore() as store:
        return store.verified_rungs()
