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
(nexus-2x9xa, ``reapable(c)``) ships it would MOVE them to quarantine after the
grace window (30 days, then deleted 14 days later). Nothing at upgrade time repaired that.

The upgrade ladder (RDR-185) is the standing mechanism for a data transition
that must converge on every install: ``nx upgrade`` walks it after the
preconditions have brought the engine to the pinned tag, each rung detects,
converges idempotently, and records completion ONLY after a fresh
``verify()`` (RDR-142). The record lands in the engine's
``nexus.ladder_completions`` (per tenant, no client-side store), which is also
where the engine reaper reads it. Hence a rung, not a doctor row (a doctor
row repairs nothing and leaves no record the reaper can read) and not an
ad-hoc hook (no completion fact, no re-run discipline).

WHAT IT DOES.
  detect  : CHEAP, no census. ``detect()`` runs from ``nx upgrade --auto`` at
            every session start, ``nx upgrade --dry-run``, ``nx doctor`` and
            the root CLI's version-transition callout, so it costs at most
            one ledger read (and one small residual-note read when pending).
            Converged only when a completion is on file whose
            ``package_version`` equals the installed version. Otherwise
            pending, and the census belongs to ``converge()``.
  converge: under a cross-process lock, a residual recorded within 24 hours
            at the same package version is not even re-censused. Otherwise
            one census of every non-quarantine collection. A clean census
            completes. An UNCHANGED residual (same per-collection counts and
            package version as the last failed attempt, kept in a T2 note) is
            not retried. Otherwise
            ``backfill_manifest_for_collection(only_gapped=True)`` on each
            collection holding a legacy chunk, re-censusing between passes
            until the count reads zero or a pass makes no progress. The
            backfill never deletes a chunk. A backfill that RAISED is never
            remembered (it is not a residual): the next session start retries.
  verify  : a census that read every collection and found zero legacy and
            zero unclassified chunks; the one converge just took counts if it
            is under a minute old, so a clean tenant pays for one census, not
            two. A census that cannot run, or whose answer lacks the bucket
            keys, is unknown, and unknown is not reached (nexus-hdumg); a
            transient outage there defers instead of failing the upgrade.

The completion record carries a census summary in its ``detail``.

A tenant with no collections is converged when the catalog also holds no
manifest rows; an empty listing over a non-empty catalog is a listing failure
and defers.
The engine's reaper gate (``Rdr192BackfillGate``) reads at least as strictly as
this branch (it counts quarantine chunks, which the census skips; no chunk
means no manifest row, because of the manifest's foreign key), so a tenant
that is empty by it passes the gate with no completion record (nexus-wbfpw.73). The gate writes none: this rung stays the one recorder.

WHAT IT DOES NOT DO. It does not touch the ``superseded``, ``dead-owner`` or
``no-owner`` buckets: those are the reaper's input by design, not a backfill
target. It writes no ``.nxexp`` owner and never removes a chunk.

RESIDUAL. A legacy chunk the backfill cannot heal leaves the census above
zero. The rung DEFERS (it never raises): ``nx upgrade`` keeps running its
remaining steps (plugin lockstep, git hooks, install-mode record, ...), the
walk reports the collections and the remedy, ``nx doctor`` shows the row, NO
completion is recorded, and so, on a tenant with no earlier record, the reaper
stays refused until an operator resolves it. (On a tenant that already holds a
record from an older package version the old record still stands and the gate
is open; the reaper's own in-engine census is what covers that tenant.) It is
retried when the residual or the package version changes, after 24 hours, or on
demand with NX_RDR192_BACKFILL_RETRY=1. See :func:`_remedy` for what actually
heals each class (for most skipped classes no verb does, and the note's text is
hidden, so the user's own copy is needed).

THE REAPER GATE. :func:`rdr192_backfill_complete` and
:func:`require_rdr192_backfill_complete` are the client-side reads of the
completion fact. The engine side is ``Rdr192BackfillGate`` (Java,
``service/src/main/java/dev/nexus/service/db/``), keyed on the same
:data:`RUNG_NAME`; ``tests/upgrade/test_rdr192_manifest_backfill_rung.py``
pins the two literals equal.

THE RECORD IS AN ATTESTATION, NOT PROOF. The engine can compute this census
itself, and new legacy-shaped chunks can appear after a completion (RDR-223:
``store_put`` registers the document before the chunk write, so a client
killed between the two leaves a document-owned, manifest-less chunk). The gate
answers "the client verified a census at package version X"; the reaper bead
(nexus-2x9xa) must re-check the census in the engine per collection per pass
and treat this record as necessary, not sufficient.

COMPLETION IS PER TENANT AND WRITTEN BY WHOEVER RUNS ``nx upgrade`` AGAINST
THAT TENANT. A cloud tenant that no client has upgraded since this rung
shipped has no record and the gate stays closed: that is the intended
fail-safe, not a gap to paper over.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import structlog

from nexus.upgrade_ladder.completion import CompletionRecord
from nexus.upgrade_ladder.protocol import (
    CompletionLedger,
    ConvergeOutcome,
    ConvergeResult,
    ProgressReporter,
    RungStatus,
)
from nexus.upgrade_ladder.registry import RUNG_RDR192_MANIFEST_BACKFILL
from nexus.upgrade_ladder.runner import installed_package_version

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

#: The five buckets ``POST /v1/vectors/manifest-less-census`` reports in
#: ``totals`` (``VectorHandler``: "always carries all five bucket keys").
_BUCKETS = ("superseded", "legacy-unmanifested", "dead-owner", "no-owner", "unclassified")

#: T2 note holding the last failed attempt's residual fingerprint.
MEMO_PROJECT = "upgrade_ladder_state"
MEMO_TITLE = f"{RUNG_NAME}.residual"
_MEMO_TTL_DAYS = 30

#: Cap on the record ``detail`` (a TEXT column; a summary, not a dump).
_DETAIL_MAX = 600

#: A failed attempt's residual is not even re-censused for this long at the
#: same package version: a stuck tenant would otherwise pay one census request
#: per collection at every session start (nx upgrade --auto).
RESIDUAL_RETRY_AFTER = timedelta(hours=24)

#: ``NX_RDR192_BACKFILL_RETRY=1`` forces an immediate full retry (skips the
#: time gate and the unchanged-residual skip). For an operator who has just
#: re-put a stranded note and does not want to wait for the gate.
RETRY_ENV = "NX_RDR192_BACKFILL_RETRY"

#: verify() reuses the census converge just took, if it is at most this old.
#: That census IS independent evidence (a census read, not the backfill's own
#: return value); reading it a second time only doubled the cost of every
#: clean tenant at every package version.
_VERIFY_REUSE = timedelta(seconds=60)


class CensusUnavailable(RuntimeError):
    """The census could not be taken: engine unreachable, older than the
    census route, or an answer that lacks the positive signal. Distinct from
    a census that ran and found something."""


class BackfillIncompleteError(RuntimeError):
    """The RDR-192 legacy-unmanifested backfill has not completed on this
    tenant, so the reaper must not run."""


@dataclass(frozen=True)
class CollectionReading:
    """One collection's census totals. The two blocking buckets are required;
    the others feed the record's provenance summary."""

    collection: str
    legacy_unmanifested: int
    unclassified: int
    superseded: int = 0
    dead_owner: int = 0
    no_owner: int = 0
    scope_chunks: int = 0


@dataclass(frozen=True)
class CensusReading:
    """A completed census over the tenant. Having this object at all means
    the listing and every per-collection census SUCCEEDED and carried its
    positive signal; a census that could not run raises
    :class:`CensusUnavailable` instead."""

    collections: tuple[CollectionReading, ...]
    #: Collections the engine refused as quarantine (out of the census by
    #: construction) that the listing had not already marked as such.
    quarantine_skipped: int = 0

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
            for c in self.collections if c.legacy_unmanifested > 0
        ] + [
            f"{c.collection}: {c.unclassified} unclassified"
            for c in self.collections if c.unclassified > 0
        ]
        return "; ".join(parts)

    def fingerprint(self, package_version: str) -> str:
        """Identity of the residual: its per-collection counts and the
        package version. Two attempts with the same fingerprint would run the
        same backfill against the same data."""
        payload = json.dumps(
            {
                "v": package_version,
                "r": sorted(
                    (c.collection, c.legacy_unmanifested, c.unclassified)
                    for c in self.collections
                    if c.legacy_unmanifested or c.unclassified
                ),
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def summary(self) -> str:
        """One line for the completion record's ``detail``."""
        def total(attr: str) -> int:
            return sum(getattr(c, attr) for c in self.collections)

        text = (
            f"census: collections={len(self.collections)} "
            f"quarantine_skipped={self.quarantine_skipped} "
            f"chunks={total('scope_chunks')} "
            f"legacy-unmanifested={self.legacy_total} unclassified={self.unclassified_total} "
            f"superseded={total('superseded')} dead-owner={total('dead_owner')} "
            f"no-owner={total('no_owner')}"
        )
        return text[:_DETAIL_MAX]


CensusFn = Callable[[], CensusReading]
#: Backfill one collection for real, ``only_gapped``. Returns a
#: :class:`BackfillOutcome`, or a bare chunk count (no skip detail).
BackfillFn = Callable[[str], "BackfillOutcome | int"]
#: The completion record on file for this tenant, or None.
RecordFn = Callable[[], CompletionRecord | None]


#: BackfillResult skip counters worth surfacing, as (field, label). The
#: has_manifest counter is benign (the document is healthy) and omitted.
_SKIP_FIELDS = (
    ("docs_skipped_zero_chunks", "zero_chunks"),
    ("docs_skipped_no_t3", "no_t3_collection"),
    ("docs_skipped_phase3_no_index", "phase3_no_chunk_index"),
    ("docs_skipped_chash_divergent", "chash_divergent"),
    ("docs_skipped_fk_409", "fk_409"),
    ("docs_cross_collection_forward_owner_skipped", "cross_collection_forward_owner"),
    ("docs_reverse_multi_piece_skipped", "reverse_multi_piece"),
    ("docs_skipped_chunk_count_mismatch", "chunk_count_mismatch"),
)


@dataclass(frozen=True)
class BackfillOutcome:
    """What one collection's backfill did: rows written and, per skip class,
    how many documents it left alone (counts, because the backfill reports
    counts; the document ids are in its structlog warnings)."""

    chunks_written: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_result(cls, result: Any) -> "BackfillOutcome":
        skipped = {
            label: int(getattr(result, attr, 0) or 0)
            for attr, label in _SKIP_FIELDS
            if getattr(result, attr, 0)
        }
        return cls(chunks_written=int(result.chunks_written), skipped=skipped)


def _describe_skipped(skipped: dict[str, dict[str, int]]) -> str:
    parts = [
        f"{collection}: " + ", ".join(f"{label}={n}" for label, n in sorted(counts.items()))
        for collection, counts in sorted(skipped.items()) if counts
    ]
    return "The backfill skipped documents: " + "; ".join(parts) + "." if parts else ""


def _remedy(reading: CensusReading) -> str:
    """What an operator can actually do about the residual, per class present.

    Measured, not hoped for: ``nx t3 backfill-manifest`` is what this rung
    already ran, so repeating it cannot heal what it skipped (owner registered
    under another collection, no matching chunk, chash divergence, a matched
    chunk count above the registered one, several chunks at one position).
    ``nx catalog reconcile`` rebuilds file-indexed documents from a recorded
    content hash and does not apply to notes. The path RDR-192 Phase 1 used
    for the operator tenant's leftover live notes was a re-put (nexus-wbfpw.7),
    and ``tests/upgrade/test_rdr192_manifest_backfill_substrate.py`` proves it
    heals a stranded note end to end.

    The catch, stated in the text: a stranded note's text is hidden from
    ``nx store get`` and search (``live(c)``), and no ``nx`` verb reads it, so
    a re-put needs the user's own copy of the note."""
    lines: list[str] = []
    if reading.legacy_total:
        example = reading.legacy_collections[0].collection
        lines.append(
            "legacy-unmanifested: the backfill already ran and skipped these documents; "
            "running it again cannot heal them, and no verb does. Their text is hidden "
            "from `nx store get` and search and no `nx` verb reads it, so a fix needs "
            "your own copy of each note. `nx t3 census-manifest-less --collection "
            f"{example}` lists each chunk with its owner document's title. To heal one, "
            "re-put your copy under the same title into the collection named there, "
            f"for example `nx store put - --collection {example} --title '<title>'`: "
            "the new chunk is manifested under the same document and the old chunk is "
            "left to the reaper. A note you cannot re-put stays hidden."
        )
    if reading.unclassified_total:
        lines.append(
            "unclassified: the census could not place these chunks and no verb heals "
            "them. Send `nx t3 census-manifest-less --collection <collection> --json` "
            "to the maintainers."
        )
    lines.append(
        "`nx upgrade` retries when the residual or the package version changes, or "
        f"after 24 hours; set {RETRY_ENV}=1 to retry now. On a tenant with no prior "
        "completion record the reaper stays refused until the census reads zero."
    )
    return " ".join(lines)


# ── production seams ─────────────────────────────────────────────────────────


class _QuarantineRefusal(Exception):
    """The census route refused a collection as quarantine."""


def _default_census() -> CensusReading:
    """Census every non-quarantine collection via the engine's read-only
    manifest-less-census route. ``limit=1`` because only the collection-wide
    ``totals`` are read, which the route reports identically on every page."""
    from nexus.db import make_t3  # noqa: PLC0415 — deferred; keeps cold CLI start cheap and breaks an import cycle
    from nexus.db.http_vector_client import VectorServiceError  # noqa: PLC0415 — deferred; same reason
    from nexus.db.service_endpoint import ServiceEndpointUnresolvableError  # noqa: PLC0415 — deferred; same reason

    def reach(call: Callable[[], Any]) -> Any:
        """Run one engine-reaching call. Everything that stops the call from
        completing (an unresolvable endpoint, a malformed config.yml read
        while resolving it, a refused connection, a timeout, an HTTP error)
        means the census could not be taken, which is "unknown" and defers.
        Only this call is wrapped: judging what came back is not, so a bug in
        that stays loud."""
        try:
            return call()
        except VectorServiceError as exc:
            if exc.code == 404:
                raise CensusUnavailable(
                    "the connected engine predates the manifest-less-census route "
                    "(RDR-192 S2); `nx upgrade` converges the engine to the pinned tag first"
                ) from exc
            if exc.code == 400 and "quarantine" in str(exc).lower():
                # The engine's own refusal (VectorHandler.requireNotQuarantineCollection):
                # a quarantine sibling the listing carried no catalog row for.
                raise _QuarantineRefusal(str(exc)) from exc
            raise CensusUnavailable(f"the census request failed: {exc}") from exc
        except ServiceEndpointUnresolvableError as exc:
            # Say it cleanly: the exception's own text also explains a retired
            # Chroma path, which is noise to anyone reading `nx upgrade`.
            raise CensusUnavailable(
                "the engine could not be reached: no nexus-service endpoint is resolvable; "
                "start it with `nx daemon service start`"
            ) from exc
        except Exception as exc:  # noqa: BLE001 — see the docstring: any failure to reach the engine is "census unavailable"
            raise CensusUnavailable(
                f"the engine could not be reached: {type(exc).__name__}: {exc}"
            ) from exc

    client = reach(make_t3)
    rows = reach(lambda: client.list_collections(strict=True))
    readings: list[CollectionReading] = []
    quarantine_skipped = 0
    for row in rows:
        # Catalog-authoritative, never a name-prefix parse (RDR-204):
        # quarantine siblings are out of the census by construction.
        if row.get("lifecycle_state") == "quarantine":
            quarantine_skipped += 1
            continue
        name = row["name"]
        try:
            page = reach(lambda n=name: client.manifest_less_census(n, limit=1, offset=0))
        except _QuarantineRefusal:
            quarantine_skipped += 1
            _log.info("rdr192_census_skipped_quarantine", collection=name)
            continue
        readings.append(_parse_census_page(name, page, row))
    if not rows:
        _cross_check_empty_listing()
    return CensusReading(collections=tuple(readings), quarantine_skipped=quarantine_skipped)


def _parse_census_page(name: str, page: Any, listing_row: dict) -> CollectionReading:
    """Read one census answer, failing CLOSED. The route always returns all
    five bucket keys and ``scope_chunk_total``; an answer without them is not
    a clean census, it is no census (nexus-hdumg: verify asserts the presence
    of a positive signal, never the absence of a negative one)."""
    totals = page.get("totals") if isinstance(page, dict) else None
    scope = page.get("scope_chunk_total") if isinstance(page, dict) else None
    if not isinstance(totals, dict) or scope is None:
        raise CensusUnavailable(
            f"the census answer for {name} carries no totals/scope_chunk_total; "
            "not treating it as clean"
        )
    missing = [b for b in _BUCKETS if b not in totals]
    if missing:
        raise CensusUnavailable(
            f"the census answer for {name} lacks bucket key(s) {', '.join(missing)}; "
            "not treating it as clean"
        )
    scope_chunks = int(scope)
    stored = int(listing_row.get("stored_count", listing_row.get("count", 0)) or 0)
    if stored > 0 and scope_chunks == 0:
        raise CensusUnavailable(
            f"the census scope for {name} is empty while the listing shows {stored} "
            "chunk(s); the census read a different tenant or collection"
        )
    return CollectionReading(
        collection=name,
        legacy_unmanifested=int(totals["legacy-unmanifested"]),
        unclassified=int(totals["unclassified"]),
        superseded=int(totals["superseded"]),
        dead_owner=int(totals["dead-owner"]),
        no_owner=int(totals["no-owner"]),
        scope_chunks=scope_chunks,
    )


def _cross_check_empty_listing() -> None:
    """An empty collection listing is only 'nothing to census' if the catalog
    agrees. The catalog's live-document manifest row count is positive proof
    that chunks exist (the manifest FK requires the chunk row), so a positive
    count over an empty listing is a listing failure, not a clean tenant."""
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred; keeps cold CLI start cheap

    try:
        catalog = make_catalog_reader()
        stats = catalog.stats() if catalog is not None else {}
        manifest_rows = int((stats or {}).get("chunk_count", 0) or 0)
    except Exception as exc:  # noqa: BLE001 — cannot cross-check => cannot call the tenant empty
        raise CensusUnavailable(
            f"the collection listing is empty and the catalog could not confirm it: {exc}"
        ) from exc
    if manifest_rows > 0:
        raise CensusUnavailable(
            f"the collection listing is empty but the catalog holds {manifest_rows} manifest "
            "row(s); refusing to call the tenant empty"
        )


def _default_backfill(collection: str) -> BackfillOutcome:
    """The same call ``nx t3 backfill-manifest -c <collection> --no-dry-run
    --only-gapped`` makes. Returns rows written and the per-class skip counts."""
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred; keeps cold CLI start cheap
    from nexus.catalog.manifest_backfill import backfill_manifest_for_collection  # noqa: PLC0415 — deferred; same reason
    from nexus.db import make_t3  # noqa: PLC0415 — deferred; same reason

    catalog = make_catalog_reader()
    if catalog is None:
        # No catalog means no document to own a chunk: nothing is
        # backfillable, and the census still reads what it reads.
        return BackfillOutcome()
    result = backfill_manifest_for_collection(
        catalog, make_t3(), collection, dry_run=False, only_gapped=True,
    )
    return BackfillOutcome.from_result(result)


def _default_record() -> CompletionRecord | None:
    """This tenant's completion record for the rung, from the engine's
    ledger. Raises when the ledger cannot be read (the caller decides)."""
    from nexus.upgrade_ladder.http_store import HttpLadderStore  # noqa: PLC0415 — deferred; keeps cold CLI start cheap

    with HttpLadderStore() as store:
        return store.completions().get(RUNG_NAME)


class ResidualMemo(Protocol):
    """Durable, per-tenant note of the last failed attempt."""

    def load(self) -> dict[str, Any] | None: ...
    def save(self, note: dict[str, Any]) -> None: ...
    def clear(self) -> None: ...


class T2ResidualMemo:
    """The memo as a T2 memory note (engine-backed PG, per tenant). No local
    state file and no new table: T2 memory already carries operational state
    of this kind (``taxonomy_discover_health``). Every failure degrades to
    'no memo', which only costs a retry."""

    @contextlib.contextmanager
    def _store(self) -> Iterator[Any]:
        from nexus.db.t2.http_memory_store import HttpMemoryStore  # noqa: PLC0415 — deferred; keeps cold CLI start cheap

        store = HttpMemoryStore()
        try:
            yield store
        finally:
            store.close()

    def load(self) -> dict[str, Any] | None:
        try:
            with self._store() as store:
                row = store.get(project=MEMO_PROJECT, title=MEMO_TITLE)
            return json.loads(row["content"]) if row else None
        except Exception as exc:  # noqa: BLE001 — a memo that cannot be read is no memo
            _log.debug("rdr192_residual_memo_load_failed", error=str(exc))
            return None

    def save(self, note: dict[str, Any]) -> None:
        try:
            with self._store() as store:
                store.put(
                    MEMO_PROJECT, MEMO_TITLE, json.dumps(note, sort_keys=True),
                    tags="upgrade-ladder,rdr192", ttl=_MEMO_TTL_DAYS, agent="nx-upgrade",
                )
        except Exception as exc:  # noqa: BLE001 — losing the memo costs one retry, never correctness
            _log.warning("rdr192_residual_memo_save_failed", error=str(exc))

    def clear(self) -> None:
        try:
            with self._store() as store:
                store.delete(project=MEMO_PROJECT, title=MEMO_TITLE)
        except Exception as exc:  # noqa: BLE001 — a stale memo is harmless: it is keyed on the residual fingerprint
            _log.debug("rdr192_residual_memo_clear_failed", error=str(exc))


@contextlib.contextmanager
def _cross_process_lock() -> Iterator[bool]:
    """Non-blocking exclusive lock so concurrent session starts do not stack
    backfills. Yields False when another process holds it. The repo's shared
    primitive (:mod:`nexus._locking`) on a ``<name>.lock`` file beside the
    other config-dir locks; a holder that dies releases it with its fd."""
    from nexus._locking import lock_fd, unlock_fd  # noqa: PLC0415 — deferred; keeps cold CLI start cheap
    import nexus.config as _config  # noqa: PLC0415 — deferred; same reason. Module attribute, never a by-value import (nexus-78blw)

    directory = _config.nexus_config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(directory / "rdr192_manifest_backfill.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            lock_fd(fd, blocking=False)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            unlock_fd(fd)
    finally:
        os.close(fd)


# ── the rung ─────────────────────────────────────────────────────────────────


@dataclass
class Rdr192ManifestBackfillRung:
    """The ladder rung. Every collaborator is constructor injected; the
    defaults call the engine."""

    census_fn: CensusFn = _default_census
    backfill_fn: BackfillFn = _default_backfill
    #: The completion record on file (default: the engine's ledger).
    record_fn: RecordFn = _default_record
    installed_version_fn: Callable[[], str] = installed_package_version
    memo: ResidualMemo = field(default_factory=T2ResidualMemo)
    lock_factory: Callable[[], contextlib.AbstractContextManager[bool]] = _cross_process_lock
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC)  # noqa: E731 — a dataclass default seam, not a def
    max_passes: int = DEFAULT_MAX_PASSES
    name: str = RUNG_NAME
    _errors: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _skipped: dict[str, dict[str, int]] = field(default_factory=dict, init=False, repr=False)
    _verify_detail: str = field(default="", init=False, repr=False)
    _verify_deferred: bool = field(default=False, init=False, repr=False)
    _record_detail: str = field(default="", init=False, repr=False)
    _last_clean: tuple[CensusReading, datetime] | None = field(default=None, init=False, repr=False)

    # ── detect ───────────────────────────────────────────────────────────────

    def detect(self) -> RungStatus:
        """No census. Converged iff the completion on file was written at the
        installed package version; otherwise pending, and ``converge()`` takes
        the census.

        Cheap on purpose: this runs at every session start (``nx upgrade
        --auto``), from ``--dry-run`` and ``nx doctor``, and from the root
        CLI's version-transition callout. A completion is only ever written
        after a census read zero (``verify()``). Re-deriving when the package
        version changes decides WHEN this client looks again; it does not
        bound the record's authority (the engine gate ignores
        ``package_version`` and there is no revoke route), which is why the
        reaper must run its own census in the engine. An operator can
        re-derive on demand with ``nx t3 census-manifest-less --all
        --require-zero legacy-unmanifested``. An unreadable ledger is not a
        record."""
        version = self.installed_version_fn()
        record: CompletionRecord | None = None
        ledger_error = ""
        try:
            record = self.record_fn()
        except Exception as exc:  # noqa: BLE001 — an unreadable ledger is 'not recorded', reported as such
            ledger_error = f" (the completion ledger could not be read: {exc})"
        if record is not None and record.package_version == version:
            return RungStatus(applicable=True, converged=True)

        if record is not None:
            state = (
                f"completion recorded at package version {record.package_version}, "
                f"installed {version}; the census re-runs on `nx upgrade`"
            )
        else:
            state = f"no completion recorded; the census runs on `nx upgrade`{ledger_error}"
        note = self.memo.load()
        if note and note.get("detail"):
            state += f". Last attempt {note.get('at', '?')} left a residual: {note['detail']}"
        return RungStatus(applicable=True, converged=False, pending_detail=state)

    # ── converge ─────────────────────────────────────────────────────────────

    def converge(self, report: ProgressReporter) -> ConvergeResult:
        self._errors.clear()
        self._skipped.clear()
        self._last_clean = None
        try:
            with self.lock_factory() as acquired:
                if not acquired:
                    return ConvergeResult(
                        ConvergeOutcome.DEFERRED,
                        detail="another nx process is running the RDR-192 backfill; retried on the next `nx upgrade`",
                    )
                try:
                    return self._converge_locked(report)
                except CensusUnavailable as exc:
                    return ConvergeResult(ConvergeOutcome.DEFERRED, detail=str(exc))
        except OSError as exc:
            # An unwritable config dir cannot take the lock. A rung that cannot
            # run defers; it must not fail `nx upgrade` and skip its later steps.
            _log.warning("rdr192_backfill_lock_unavailable", error=str(exc))
            return ConvergeResult(
                ConvergeOutcome.DEFERRED,
                detail=f"the RDR-192 backfill could not take its lock ({type(exc).__name__}: {exc}); "
                "retried on the next `nx upgrade`",
            )

    def _residual_age(self, note: dict[str, Any]) -> timedelta | None:
        try:
            return self.now_fn() - datetime.fromisoformat(str(note["at"]))
        except (KeyError, ValueError, TypeError):
            return None

    def _converge_locked(self, report: ProgressReporter) -> ConvergeResult:
        version = self.installed_version_fn()
        force = os.environ.get(RETRY_ENV) == "1"
        note = self.memo.load()

        # A stuck tenant must not pay a census per collection at every session
        # start: within the gate, at the same package version, do not even look.
        if note and not force and note.get("version") == version:
            age = self._residual_age(note)
            if age is not None and age < RESIDUAL_RETRY_AFTER:
                detail = (
                    f"residual recorded {note.get('at')}; not re-examined before "
                    f"{RESIDUAL_RETRY_AFTER} have passed or the package version changes "
                    f"({RETRY_ENV}=1 retries now): {note.get('detail', '')}"
                )
                _log.warning("rdr192_backfill_residual_gated", detail=detail)
                return ConvergeResult(ConvergeOutcome.DEFERRED, detail=detail)

        reading = self.census_fn()
        if reading.clean:
            return self._completed(reading)

        if note and not force and note.get("fingerprint") == reading.fingerprint(version):
            # Same residual after the gate: renew the timestamp so the next
            # census is another gate away, and do not repeat the backfill.
            self.memo.save({**note, "at": self.now_fn().isoformat(timespec="seconds"), "version": version})
            detail = (
                f"unchanged since the last attempt ({note.get('at', '?')}), not retried: "
                f"{note.get('detail', reading.describe_residual())}"
            )
            _log.warning("rdr192_backfill_residual_unchanged", detail=detail)
            return ConvergeResult(ConvergeOutcome.DEFERRED, detail=detail)

        for pass_number in range(1, self.max_passes + 1):
            targets = reading.legacy_collections
            if not targets:
                break
            for target in targets:
                try:
                    outcome = self.backfill_fn(target.collection)
                except Exception as exc:  # noqa: BLE001 — one collection's failure must not stop the rest; the next census names what remains
                    self._errors[target.collection] = f"{type(exc).__name__}: {exc}"
                    _log.warning(
                        "rdr192_backfill_collection_failed",
                        collection=target.collection, error=str(exc),
                    )
                    continue
                if not isinstance(outcome, BackfillOutcome):
                    outcome = BackfillOutcome(chunks_written=int(outcome or 0))
                self._skipped[target.collection] = dict(outcome.skipped)
                report.emit(
                    "rdr192_backfill_collection_done",
                    collection=target.collection, pass_number=pass_number,
                    legacy_before=target.legacy_unmanifested,
                    manifest_rows_written=outcome.chunks_written,
                    skipped=outcome.skipped,
                )
            after = self.census_fn()
            progressed = after.legacy_total < reading.legacy_total
            reading = after
            if not progressed:
                break  # the residual is not this rung's to heal

        if reading.clean:
            return self._completed(reading)

        skipped = _describe_skipped(self._skipped)
        residual = (
            f"{reading.legacy_total} legacy-unmanifested and {reading.unclassified_total} "
            f"unclassified chunk(s) remain ({reading.describe_residual()})."
        )
        if self._errors:
            # A backfill that RAISED is not an unhealable residual: a transient
            # 503 or timeout must not be remembered, or the healable chunk stays
            # hidden until the next package version. Nothing is saved; the next
            # session start retries.
            errors = "".join(
                f" Backfill error in {name}: {err}." for name, err in sorted(self._errors.items())
            )
            detail = (
                f"{residual}{errors} A backfill error is usually transient; nothing is "
                f"remembered, so the next `nx upgrade` retries. {skipped}"
            ).strip()
            _log.warning("rdr192_backfill_errored", detail=detail)
            return ConvergeResult(ConvergeOutcome.DEFERRED, detail=detail)

        detail = f"{residual} {skipped} {_remedy(reading)}".replace("  ", " ")
        self.memo.save({
            "fingerprint": reading.fingerprint(version),
            "version": version,
            "at": self.now_fn().isoformat(timespec="seconds"),
            "detail": detail[:_DETAIL_MAX * 3],
        })
        _log.warning("rdr192_backfill_residual", detail=detail)
        return ConvergeResult(ConvergeOutcome.DEFERRED, detail=detail)

    def _completed(self, reading: CensusReading) -> ConvergeResult:
        self.memo.clear()
        self._last_clean = (reading, self.now_fn())
        return ConvergeResult(ConvergeOutcome.COMPLETED)

    # ── verify ───────────────────────────────────────────────────────────────

    def verify(self) -> bool:
        """A census that read every collection and found nothing to backfill.

        Reuses the clean census ``converge`` took moments ago instead of
        taking the same census again (it is a census read, independent of the
        backfill's own return values, and a clean tenant otherwise paid it
        twice per package version). Anything older than
        :data:`_VERIFY_REUSE`, and any second call, reads afresh.

        Unknown is not reached, and it is not a failure either: a census that
        cannot run returns False with the reason and marks the verdict as a
        deferral (:meth:`verify_deferred`), so a transient outage does not
        abort the rest of `nx upgrade`. Only a real non-zero residual is a
        plain refusal."""
        self._verify_deferred = False
        reading: CensusReading | None = None
        if self._last_clean is not None:
            cached, taken = self._last_clean
            self._last_clean = None
            if self.now_fn() - taken <= _VERIFY_REUSE:
                reading = cached
        if reading is None:
            try:
                reading = self.census_fn()
            except CensusUnavailable as exc:
                self._verify_deferred = True
                self._verify_detail = (
                    f"the RDR-192 census could not run, so completion is not "
                    f"recorded; retried on the next `nx upgrade`: {exc}"
                )
                return False
        if reading.clean:
            self._verify_detail = ""
            self._record_detail = reading.summary()
            return True
        self._verify_detail = (
            f"{reading.legacy_total} legacy-unmanifested and "
            f"{reading.unclassified_total} unclassified chunk(s) remain "
            f"({reading.describe_residual()}). {_remedy(reading)}"
        )
        return False

    def verify_deferred(self) -> bool:
        """True when the last ``verify()`` refused only because the census
        could not run (the runner reports DEFERRED, not VERIFY_FAILED)."""
        return self._verify_deferred

    def verify_detail(self) -> str:
        return self._verify_detail

    def record_detail(self) -> str:
        """The census summary the passing verify() read, for the durable
        record (the runner stores it in ``ladder_completions.detail``)."""
        return self._record_detail


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
