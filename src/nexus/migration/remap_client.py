# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Engine-backed chash_remap facts — the RDR-186 .6 client (nexus-146xx.6).

:class:`HttpRemapStore` is the write-through + read surface for the PG
``nexus.chash_remap`` table (the ``/v1/remap`` endpoints, engine ≥ v0.1.45),
replacing the local ``chash_remap.db`` WRITE path. The local
:class:`~nexus.migration.wire_reid.ChashRemapStore` is demoted to a read-only
MIGRATION SOURCE: :func:`seed_local_map` uploads its pre-existing facts once
(idempotent upsert), and :class:`CompositeReadMapStore` keeps them visible to
the read-side probes until the seed has landed — a pre-seed install must never
probe falsely clean.

Design inputs honored (pinned on nexus-146xx.6 by the .4/.5 stacked review):

* **Torn-read reconcile** — :meth:`HttpRemapStore.all_pairs` pages the
  ``/pairs`` endpoint across multiple independent transactions (no snapshot,
  unlike the SQLite ``fetchall()`` it replaces), so it brackets the paged read
  with total-count reads and raises :class:`RemapReadTornError` on any
  mismatch. Could-not-tell is LOUD; a silently short global view would let
  ``unreflected_stores`` under-report (the no-silent-fallback class).
* **Zero-count short-circuit** — ``total == 0`` answers ``[]`` in one round
  trip with no ``/pairs`` calls at all. This is a live read, not a cached
  verdict: it re-asks the engine every call and tracks the world both
  directions (the Gap-4 property, one level up). Any richer probe-before-fetch
  marker is deliberately NOT built here — see the Gap-4 caution on the bead;
  that design belongs with the .7 detect()/verify() calling convention.
* **Batch bound** — :meth:`record_batch` groups per source collection (the
  endpoint fixes ``source_collection`` per POST) and pages at
  ``QUOTAS.MAX_RECORDS_PER_WRITE`` (300), the cap the endpoint enforces.

RF-186-1: every surface here is raw facts or live counts — no verdict is read,
written, cached, or derivable server-side.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from nexus.db.limits import QUOTAS
from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin

if TYPE_CHECKING:
    from pathlib import Path

    from nexus.migration.wire_reid import ChashRemapStore, RemapEntry

_log = structlog.get_logger(__name__)

#: Page size for the paged /pairs read (the endpoint's MAX_PAGE).
_PAIRS_PAGE: int = 1000


class RemapReadTornError(RuntimeError):
    """The paged global read could not be reconciled against the total count.

    A row shifted past an OFFSET boundary by a concurrent write would be
    SILENTLY missing from the result; callers must treat this as
    could-not-tell (the probes' existing degraded branches), never as a
    clean short list.
    """


class HttpRemapStore(RefreshableHttpStoreMixin):
    """Thin HTTP client for the engine's ``/v1/remap`` endpoints.

    Endpoint/token resolution, tenant stamping, and 401 self-healing come
    from :class:`RefreshableHttpStoreMixin` (the f2qvx house pattern) —
    construct with no arguments in production to resolve via
    ``resolve_service_endpoint()``.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        # Test seam for exercising multi-page reads with small fixtures.
        self._page: int = _PAIRS_PAGE

    # ── write path (the .6 write-through) ───────────────────────────────────

    def record_batch(self, entries: list["RemapEntry"]) -> int:
        """Persist *entries* via the engine, grouped per source collection
        and paged at the endpoint's 300-entry cap.

        Each POST is one PG transaction server-side; paging splits a large
        batch into several. That is SAFE for the r2 ordering: the transform
        calls this strictly BEFORE the target write, so the map can only
        ever be AHEAD of the data it describes (over-recorded facts are
        harmless idempotent upserts on resume; under-recording is the bug
        the ordering forbids).

        Raises on any HTTP failure — a map fact that did not durably land
        must abort the batch before the target write (fail loud).
        """
        if not entries:
            return 0
        recorded = 0
        by_source: dict[str, list[RemapEntry]] = {}
        for entry in entries:
            by_source.setdefault(entry.source_collection, []).append(entry)
        cap = QUOTAS.MAX_RECORDS_PER_WRITE
        for source_collection, group in by_source.items():
            for start in range(0, len(group), cap):
                page = group[start:start + cap]
                result = self._post("/v1/remap/record_batch", {
                    "source_collection": source_collection,
                    "entries": [
                        {
                            "old_id": e.old_id,
                            "new_chash": e.new_chash,
                            "target_collection": e.target_collection,
                            "provenance": e.provenance,
                        }
                        for e in page
                    ],
                })
                recorded += int(result.get("recorded", len(page)))
        return recorded

    # ── leg operations ──────────────────────────────────────────────────────

    def rekey(self, orphan_policy: str = "drop") -> dict:
        """RDR-180 Item6 (nexus-jxizy.6): drive the per-tenant full-digest
        rekey. Idempotent engine-side (digest-mismatch predicate); returns
        the disposition + per-table counts envelope. A legacy-id collision
        (one old id, two digests) surfaces as the transport's HTTP-409
        error — never resolved silently."""
        return dict(self._post("/v1/remap/rekey", {"orphan_policy": orphan_policy}))

    def clear_leg(self, source_collection: str, target_collection: str) -> int:
        """Clear ONE leg's rows — the (source, target) PAIR is required by
        the endpoint (a wide clear would delete a co-resident sibling leg's
        claims). The CALLER owns the D2 whole-leg ordering discipline."""
        result = self._post("/v1/remap/clear_leg", {
            "source_collection": source_collection,
            "target_collection": target_collection,
        })
        return int(result["deleted"])

    def membership(
        self, source_collection: str, target_collection: str
    ) -> tuple[int, int]:
        """LIVE ``(mapped_total, present_count)`` for one leg — computed
        fresh by ``nexus.remap_membership()`` on every call. Converged iff
        equal (including 0 == 0: nothing owed); that interpretation is the
        caller's (bead .7), never stored anywhere."""
        result = self._get("/v1/remap/membership", {
            "source_collection": source_collection,
            "target_collection": target_collection,
        })
        return int(result["mapped_total"]), int(result["present_count"])

    # ── read path (the demotion's replacement reads) ────────────────────────

    def total_count(self, source_collection: str | None = None) -> int:
        """Total fact rows (optionally one source's) — one cheap round trip;
        the reconcile bracket and the zero-fact short-circuit input."""
        params = (
            {"source_collection": source_collection} if source_collection else None
        )
        return int(self._get("/v1/remap/count", params)["total"])

    def all_pairs(self) -> list[tuple[str, str]]:
        """Every ``(old_id, new_chash)`` fact — the cascade / unreflected
        global view, count-reconciled (see module docstring).

        Raises :class:`RemapReadTornError` when the bracket counts disagree
        or the collected rows do not match the total.

        ACCEPTED RESIDUAL (reviewer-146xx-6): the reconcile detects only
        SIZE-changing races. A same-cardinality content swap between the
        bracket reads (a concurrent ``clear_leg`` of N rows plus a
        concurrent ``record_batch`` of N rows) reconciles clean while the
        snapshot is torn. Migration writes and cascade reads are
        run-sequential within one converge, so the exposure is a concurrent
        OPERATOR action mid-read — narrow, and bounded by the facts being
        idempotent upserts; revisit if a genuine concurrent-operator
        workflow ever appears.
        """
        before = self.total_count()
        if before == 0:
            return []
        pairs: list[tuple[str, str]] = []
        offset = 0
        while True:
            page = self._get(
                "/v1/remap/pairs", {"limit": self._page, "offset": offset}
            )["pairs"]
            pairs.extend((row[0], row[1]) for row in page)
            if len(page) < self._page:
                break  # partial (or empty) page = exhaustion; saves a round trip
            offset += self._page
        after = self.total_count()
        if before != after or len(pairs) != after:
            raise RemapReadTornError(
                f"paged chash_remap read could not be reconciled: "
                f"count before={before}, after={after}, rows collected={len(pairs)} "
                "— a concurrent write moved rows across page boundaries; "
                "treat as could-not-tell and retry"
            )
        return pairs

    def entries_with_targets(
        self, source_collection: str
    ) -> dict[str, tuple[str, str]]:
        """``old_id → (new_chash, target_collection)`` for one source — the
        rollback / cross-model read shape (mirrors the local store)."""
        rows = self._get(
            "/v1/remap/entries", {"source_collection": source_collection}
        )["entries"]
        return {
            row["old_id"]: (row["new_chash"], row["target_collection"])
            for row in rows
        }

    def entries_for_collection(self, source_collection: str) -> dict[str, str]:
        """``old_id → new_chash`` for one source (mirrors the local store)."""
        return {
            old: new
            for old, (new, _target) in self.entries_with_targets(source_collection).items()
        }

    def source_collections(self) -> frozenset[str]:
        """Distinct source collections — the source-gone probe input."""
        return frozenset(
            self._get("/v1/remap/source_collections")["source_collections"]
        )

    def __enter__(self) -> "HttpRemapStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


#: Suffix stamped onto a local map file whose facts have verifiably reached
#: PG. The renamed file is the read-only migration/rollback SOURCE — content
#: untouched — but no code path ever opens it as a live fact source again.
SEEDED_SUFFIX: str = ".seeded"


def seed_local_map(local: "ChashRemapStore", engine: HttpRemapStore) -> int:
    """Upload *local*'s facts into PG (idempotent upserts on the natural key).

    The primitive under :func:`seed_and_quarantine` — production code calls
    the wrapper, never this directly: an un-quarantined seed re-run after a
    rollback's ``clear_leg`` would RESURRECT the cleared claims from the
    local file (the reseed-resurrection hazard, Hal directive 2026-07-18).
    Returns the number of facts uploaded.
    """
    entries = local.all_entries()
    if not entries:
        return 0
    recorded = engine.record_batch(entries)
    _log.info("remap_local_map_seeded", facts=len(entries), recorded=recorded)
    return len(entries)


def seed_and_quarantine(map_path: "Path", engine: HttpRemapStore) -> int:
    """ONE-TIME backfill of the pre-.6 local map into PG, then quarantine.

    After every page of the seed has committed (``record_batch`` raises on
    any failure, so returning at all means every POST was a 200/committed
    PG transaction), the file is renamed ``chash_remap.db`` →
    ``chash_remap.db.seeded``. One-time is BY CONSTRUCTION, not by memory:
    the renamed file no longer matches the live name, so neither a later
    converge (re-seed) nor the composite read probes consult it — a
    rollback's engine-side ``clear_leg`` (D2 absence-encoding) can never be
    silently undone by a re-upload of stale local facts. The file itself
    survives byte-intact as the read-only migration/rollback source.

    A crash between the last committed page and the rename leaves the file
    live: the NEXT converge re-seeds (harmless idempotent upserts — the
    engine already holds the facts) and then quarantines. The re-seed
    window is that single crash gap, never steady-state. (Resurrecting a
    CLEARED leg through this gap requires the compound precondition of a
    process crash inside the seed→rename window AND an operator rollback's
    ``clear_leg`` landing before the retry — accepted as documented,
    critic-146xx-6 Q2 residual.)

    No-ops (returns 0) when the live-named file does not exist — fresh
    installs and already-quarantined installs alike.
    """
    from nexus.migration.wire_reid import ChashRemapStore  # noqa: PLC0415 — deferred to avoid import cycle

    if not map_path.exists():
        return 0
    with ChashRemapStore(map_path) as local:
        seeded = seed_local_map(local, engine)
    quarantined = map_path.with_name(map_path.name + SEEDED_SUFFIX)
    map_path.rename(quarantined)
    _log.info(
        "remap_local_map_quarantined",
        facts=seeded,
        source=str(map_path),
        quarantined=str(quarantined),
    )
    return seeded


class CompositeReadMapStore:
    """Read-only union of engine facts and (optional) local legacy facts.

    The pre-seed window's correctness guard: an install whose facts still
    live only in ``chash_remap.db`` must not probe falsely clean against an
    empty engine table. Exposes exactly the two read shapes the probes and
    the cascade consume (``all_pairs``, ``source_collections``); identical
    facts collapse, conflicting ones surface downstream via
    ``_global_view``'s existing :class:`AmbiguousRemapError` — never
    silently resolved here.
    """

    def __init__(
        self, engine: HttpRemapStore, local: "ChashRemapStore | None"
    ) -> None:
        self._engine = engine
        self._local = local

    def all_pairs(self) -> list[tuple[str, str]]:
        seen: dict[tuple[str, str], None] = {}
        for pair in self._engine.all_pairs():
            seen[pair] = None
        if self._local is not None:
            for pair in self._local.all_pairs():
                seen[(pair[0], pair[1])] = None
        return list(seen)

    def source_collections(self) -> frozenset[str]:
        sources = self._engine.source_collections()
        if self._local is not None:
            sources = sources | self._local.source_collections()
        return sources
