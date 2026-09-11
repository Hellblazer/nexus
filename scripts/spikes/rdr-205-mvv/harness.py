#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-205 Phase 3 Step 3 (bead nexus-em75s.16) — MVV run 2, engine-direct legs.

The RDR-110 ten-worker work-stealing harness, written as code for the
first time (RDR-110 is ``abandoned``, carries a "do not implement
against this design" tombstone, and targeted the retired SQLite+Chroma
substrate — this harness runs against RDR-205's Postgres tuple space
instead, over the real ``HttpTupleStore`` client, on a TEST-ONLY
template loaded via ``NX_TUPLE_TEMPLATE_DIR`` so nothing test-shaped
ships in engine resources).

Two legs, both against a developer engine this script boots itself
(the same ``tests/_engine_substrate.ensure_engine`` machinery the unit
suite uses — a hermetic bundled Postgres 17 + the ``build-gate-jar.sh``
stamped service JAR):

1. **Audit-shape leg** (``spike/mvv-run2/queue``): ``--tuples`` work
   items, ``--workers`` concurrent claimants draining them via blocking
   ``in_``/``ack``. Verifies the May MVV audit shape verbatim (RDR-205
   Test Plan): N consumed, zero claimed, zero available, exactly N
   ``claim`` and N ``ack`` log rows, zero ``expire`` rows, no tuple with
   two claim ids. Records four of the five metrics: oldest-unclaimed
   age (sampled through the drain), empty claims after wake (a claim
   call that returned nothing while, at the moment it started, tuples
   were still believed live — a genuine lost race, not the expected
   final "no more work" empties every worker's last call produces), and
   claim transaction duration (client-observed ``in_``/``inp`` latency
   — network plus server transaction, an upper bound on the raw DB
   transaction time research-3 measured directly over psql).

2. **Lease-expiry probe** (``spike/mvv-run2/lease-probe``): one claim
   with a 1 s lease, deliberately never acked, past its lease, then a
   second claimant reclaims it. Verifies the lease-then-sweep
   availability rule and gives the "lease expiries" metric a genuine
   non-zero data point rather than a trivially-always-zero one (the
   audit-shape leg's own expire count is required to be zero by the
   audit shape itself).

3. **Bloat leg** (``spike/mvv-run2/bloat``): the fifth metric, dead-
   tuple ratio against the per-table ``autovacuum_vacuum_scale_factor
   = 0.01`` (``tuples-001-baseline.xml``), sampled throughout up to one
   million claim-release cycles. The client API has no direct
   unconsume/reopen primitive, so this uses ``claim, nack`` (which
   resets claim state for reclaim, counting an attempt) on a SMALL
   FIXED pool of pre-written rows as the reopen analog to research-3's
   own "claim, ack, reopen" methodology (T2 `nexus/rdr-110-revival-
   brief-2026-09-09` and RDR-205 §Key Discoveries) — same churn shape
   (repeated non-HOT updates concentrated on a fixed row set), reached
   through the client surface alone. Bounded by wall clock
   (``--bloat-max-seconds``); the actual cycle count reached is what
   gets recorded, never a target.

Dead-tuple ratio and last-autovacuum are read directly from
``pg_stat_user_tables`` over the substrate's own bundled Postgres (the
engine-substrate ``state`` dict carries the PG coordinates for exactly
this — nexus-20890's precedent, "a test can query the substrate's
schema DIRECTLY"), not through any client operation (none exists for
those fields). Claim-log counts and the "no tuple with two claim ids"
check are read the same way, since no client operation surfaces the
claim log either.

Usage::

    uv run python scripts/spikes/rdr-205-mvv/harness.py \\
        --out /tmp/mvv-run2-summary.json

    # Quick dev loop, skipping the long bloat leg:
    uv run python scripts/spikes/rdr-205-mvv/harness.py --skip-bloat

    # The real run, bounded to 20 minutes of bloat-leg wall clock,
    # detached so its progress can be polled from a log file:
    nohup uv run python scripts/spikes/rdr-205-mvv/harness.py \\
        --bloat-max-seconds 1200 --out /tmp/mvv-run2-summary.json \\
        > /tmp/mvv-run2-harness.log 2>&1 &
    tail -f /tmp/mvv-run2-harness.log

Reproducible: fixed worker counts, seeded nonce generation from a
caller-supplied ``--run-id`` (default: a fresh UUID4, printed so a run
can be correlated against its own log), and this file's own template
committed alongside it under ``templates/``.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SPIKE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SPIKE_DIR.parents[2]
_TEMPLATE_DIR = _SPIKE_DIR / "templates"

# NX_TUPLE_TEMPLATE_DIR must be set BEFORE the engine JVM boots -- templates
# load once at engine boot (TemplateRegistry.loadAtBoot). ensure_engine()'s
# _boot() builds the child process env as `{**os.environ, ...}`, so setting
# this on our own process env before calling it is sufficient.
os.environ.setdefault("NX_TUPLE_TEMPLATE_DIR", str(_TEMPLATE_DIR))

# This is a dev-checkout process, so every HTTP write trips
# nexus.db.service_endpoint.guard_production_write unless opted in
# (nexus-a2qhz). Every store this script ever constructs is pointed at
# `state["base_url"]` -- the 127.0.0.1 engine THIS SAME PROCESS just
# booted via ensure_engine() a few lines below, never a resolved/exported
# production endpoint -- so the opt-in is genuine and unconditional, the
# pytest suite's own `_exempt_pytest_from_production_write_guard` shape
# for a script that is not pytest.
os.environ.setdefault(
    "NX_ALLOW_PROD_WRITE",
    "nexus-em75s.16 RDR-205 MVV run 2 harness -- every write targets the "
    "hermetic engine substrate this same process boots via "
    "tests._engine_substrate.ensure_engine(), never a resolved production "
    "endpoint.",
)

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# The bloat leg alone makes on the order of a million writes; each one
# fires guard_production_write's ACCEPTED-opt-in WARNING (by design --
# "auditable after the fact", see its docstring). That design point is
# for a long-lived process making occasional dev-checkout writes, not a
# tight loop -- left at its default this drowns the harness's own
# progress log in identical lines. This is log-volume management for
# OUR run only (this process's own structlog configuration), not a
# change to the guard: it still evaluates and still requires the opt-in
# above on every call, it is simply not rendered past ERROR here.
import logging  # noqa: E402
import structlog  # noqa: E402

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR))

from tests._engine_substrate import ensure_engine, mint_test_tenant  # noqa: E402

from nexus.db.t2.http_tuple_store import HttpTupleStore  # noqa: E402

_QUEUE_SUBSPACE = "spike/mvv-run2/queue"
_LEASE_PROBE_SUBSPACE = "spike/mvv-run2/lease-probe"
_BLOAT_SUBSPACE = "spike/mvv-run2/bloat"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


class _Counter:
    """A plain lock-protected counter -- used instead of `nonlocal` across
    thread-target closures, which reads awkwardly under a mutating `+=`."""

    def __init__(self, start: int = 0) -> None:
        self._lock = threading.Lock()
        self._n = start

    def increment(self, n: int = 1) -> int:
        with self._lock:
            self._n += n
            return self._n

    def decrement(self, n: int = 1) -> int:
        with self._lock:
            self._n -= n
            return self._n

    def value(self) -> int:
        with self._lock:
            return self._n


# ── engine boot ──────────────────────────────────────────────────────────


@dataclass
class Engine:
    state: dict
    tenant: str
    token: str
    store: HttpTupleStore
    version_body: dict
    head_sha: str


#: Optional escape hatch (unset by default): point ``ensure_engine()`` at a
#: PRIVATE copy of an already-stamped jar instead of the shared
#: ``service/target/nexus-service-1.0-SNAPSHOT.jar`` path every sibling
#: worktree's ``mvnw-leased.sh`` build rewrites. `_boot()`'s own freshness
#: check correctly refuses to launch while that shared path is mid-rewrite
#: ("a service BUILD IS IN PROGRESS") -- a real safety property this
#: harness does not want to weaken -- but it means a harness run can queue
#: behind every sibling's test invocation indefinitely on a busy box. A
#: copy taken from ``scripts/build-gate-jar.sh``'s OWN cache directory
#: (``gate_jar_cache_store`` writes it once, atomically, and every cache
#: HIT only ever reads from it -- never rewritten in place) is exactly as
#: fresh as a normal boot would use, just immune to the race. Set this to
#: that copy's path to skip the wait entirely.
_PRIVATE_JAR_ENV = "NX_MVV2_PRIVATE_JAR"


def boot_engine() -> Engine:
    print(f"[boot] NX_TUPLE_TEMPLATE_DIR={os.environ['NX_TUPLE_TEMPLATE_DIR']}", flush=True)
    private_jar = os.environ.get(_PRIVATE_JAR_ENV)
    if private_jar:
        jar_path = Path(private_jar)
        if not jar_path.is_file():
            raise RuntimeError(f"{_PRIVATE_JAR_ENV}={private_jar!r} is not a file")
        import tests._engine_substrate as _engine_substrate_mod
        import tests.db._service_fixture as _service_fixture_mod
        _engine_substrate_mod._JAR = jar_path
        # jar_freshness_skip_reason() checks build_in_progress_reason()
        # FIRST, UNCONDITIONALLY (nexus-06fu4: "nothing about this jar's
        # current bytes is meaningful" while a build holds the lease) --
        # a global guard against launching the SHARED service/target jar
        # while some sibling's build is mid-rewrite of that exact file.
        # jar_path here is an independent, already-complete copy (taken
        # from build-gate-jar.sh's own cache, written once and only ever
        # read from on a hit), so the hazard that guard exists to prevent
        # cannot occur regardless of what any concurrent build is doing
        # to the shared path -- this process alone stops consulting the
        # shared lease for its OWN boot decision.
        _service_fixture_mod.build_in_progress_reason = lambda *a, **k: None
        print(f"[boot] {_PRIVATE_JAR_ENV} set -- booting from a private jar copy "
              f"(bypasses the shared build-lease race): {jar_path}", flush=True)
    state = ensure_engine()
    tenant, token = mint_test_tenant(state)
    store = HttpTupleStore(base_url=state["base_url"], _token=token)
    registry = store.registry()
    sources = registry.get("sources", [])
    assert any("directory:" in s for s in sources), (
        f"NX_TUPLE_TEMPLATE_DIR did not load -- registry sources={sources!r}"
    )
    template_names = sorted(t.get("name", "") for t in registry.get("templates", []))
    print(f"[boot] registry sources={sources} templates={template_names}", flush=True)

    import httpx

    version_resp = httpx.get(f"{state['base_url']}/version", timeout=10.0)
    version_body = version_resp.json() if version_resp.status_code == 200 else {}
    head_sha = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    print(f"[boot] engine /version={version_body} worktree_head={head_sha}", flush=True)
    return Engine(state=state, tenant=tenant, token=token, store=store,
                  version_body=version_body, head_sha=head_sha)


# ── direct-PG helpers (no client operation surfaces the claim log or
#    pg_stat_user_tables -- nexus-20890's precedent: query the substrate's
#    own bundled Postgres directly) ──────────────────────────────────────


def pg_query(state: dict, sql: str) -> list[list[str]]:
    psql = str(Path(state["pg_bin"]) / "psql")
    proc = subprocess.run(
        [psql, "-h", "127.0.0.1", "-p", str(state["pg_port"]),
         "-U", state["pg_user"], "-d", state["pg_dbname"],
         "-v", "ON_ERROR_STOP=1", "-t", "-A", "-F", "|", "-c", sql],
        capture_output=True, text=True, check=True,
    )
    rows = [line.split("|") for line in proc.stdout.splitlines() if line.strip()]
    return rows


def claim_log_counts(state: dict, subspace: str) -> dict[str, int]:
    rows = pg_query(
        state,
        f"SELECT transition, count(*) FROM nexus.tuple_claim_log "
        f"WHERE subspace = '{subspace}' GROUP BY transition;",
    )
    return {r[0]: int(r[1]) for r in rows}


def two_claim_id_tuples(state: dict, subspace: str) -> int:
    rows = pg_query(
        state,
        "SELECT count(*) FROM ("
        "  SELECT tuple_id FROM nexus.tuple_claim_log"
        f"  WHERE subspace = '{subspace}' AND transition = 'claim'"
        "  GROUP BY tuple_id HAVING count(DISTINCT claim_id) > 1"
        ") x;",
    )
    return int(rows[0][0]) if rows else 0


def tuples_table_stats(state: dict) -> dict[str, object]:
    rows = pg_query(
        state,
        "SELECT n_live_tup, n_dead_tup, "
        "coalesce(last_autovacuum::text, ''), coalesce(last_autoanalyze::text, '') "
        "FROM pg_stat_user_tables WHERE schemaname = 'nexus' AND relname = 'tuples';",
    )
    if not rows:
        return {"n_live_tup": 0, "n_dead_tup": 0, "last_autovacuum": None, "last_autoanalyze": None}
    live, dead, autovac, autoan = rows[0]
    return {
        "n_live_tup": int(live), "n_dead_tup": int(dead),
        "last_autovacuum": autovac or None, "last_autoanalyze": autoan or None,
    }


# ── leg 1: audit-shape drain ─────────────────────────────────────────────


@dataclass
class AuditLegResult:
    n_tuples: int
    n_workers: int
    consumed_total: int
    empty_claims_after_wake: int
    claim_durations_s: list[float]
    oldest_unclaimed_age_samples: list[float]
    subspace_stats_final: dict
    claim_log_counts: dict[str, int]
    two_claim_id_tuple_count: int
    elapsed_s: float


def run_audit_leg(store: HttpTupleStore, state: dict, run_id: str,
                   n_tuples: int, n_workers: int, lease_s: int,
                   claim_timeout_s: float) -> AuditLegResult:
    print(f"[audit] writing {n_tuples} tuples into {_QUEUE_SUBSPACE}", flush=True)
    for i in range(n_tuples):
        store.out(
            _QUEUE_SUBSPACE, {"queue": "work"}, {"seq": str(i)},
            body=f"item-{i}", nonce=f"{run_id}-audit-{i}",
        )

    remaining = _Counter(n_tuples)
    empty_claims = _Counter()
    durations: list[float] = []
    durations_lock = threading.Lock()
    age_samples: list[float] = []
    stop_sampler = threading.Event()

    def sampler() -> None:
        while not stop_sampler.is_set():
            rows = store.rdp(_QUEUE_SUBSPACE, {"queue": "work"}, n=n_tuples)
            unclaimed = [r for r in rows if r.claim_state is None and r.consumed_at is None]
            if unclaimed:
                oldest = min(unclaimed, key=lambda r: r.created_at or "")
                created = _parse_ts(oldest.created_at)
                if created is not None:
                    age_samples.append((_now_utc() - created).total_seconds())
            else:
                break
            stop_sampler.wait(0.1)

    def worker(idx: int) -> int:
        # Every call below is made ONLY while `remaining.value() > 0` --
        # i.e. some tuple in the subspace is still believed unclaimed by
        # everyone. A `None` return here is therefore always a genuine
        # lost race to another claimant (or a candidate momentarily locked
        # by one, under SKIP LOCKED) -- the "empty claims after wake"
        # metric -- never the expected, uninteresting "no more work at
        # all" case, which this loop structurally cannot reach: it stops
        # BEFORE making a call once `remaining` hits zero.
        claimant = f"mvv2-audit-{run_id}-w{idx}"
        consumed = 0
        while remaining.value() > 0:
            t0 = time.monotonic()
            got = store.in_(
                _QUEUE_SUBSPACE, {"queue": "work"},
                claimant=claimant, lease_s=lease_s, timeout_s=claim_timeout_s,
            )
            dt = time.monotonic() - t0
            with durations_lock:
                durations.append(dt)
            if got is None:
                empty_claims.increment()
                continue
            row, claim_id = got
            store.ack(claim_id, claimant)
            consumed += 1
            remaining.decrement()
        return consumed

    t_start = time.monotonic()
    sampler_thread = threading.Thread(target=sampler, daemon=True)
    sampler_thread.start()

    results: list[int] = [0] * n_workers
    threads = []
    for i in range(n_workers):
        def _run(i=i) -> None:
            results[i] = worker(i)
        th = threading.Thread(target=_run)
        th.start()
        threads.append(th)
    for th in threads:
        th.join()
    stop_sampler.set()
    sampler_thread.join(timeout=2.0)
    elapsed = time.monotonic() - t_start

    final_stats = store.subspace_stats(_QUEUE_SUBSPACE)
    return AuditLegResult(
        n_tuples=n_tuples, n_workers=n_workers,
        consumed_total=sum(results),
        empty_claims_after_wake=empty_claims.value(),
        claim_durations_s=durations,
        oldest_unclaimed_age_samples=age_samples,
        subspace_stats_final={
            "total": final_stats.total, "available": final_stats.available,
            "claimed": final_stats.claimed, "dead": final_stats.dead,
            "consumed": final_stats.consumed,
        },
        claim_log_counts=claim_log_counts(state, _QUEUE_SUBSPACE),
        two_claim_id_tuple_count=two_claim_id_tuples(state, _QUEUE_SUBSPACE),
        elapsed_s=elapsed,
    )


# ── leg 2: lease-expiry probe ────────────────────────────────────────────


@dataclass
class LeaseProbeResult:
    first_claim_id: str
    reclaimed: bool
    reclaim_wait_s: float
    expire_log_rows: int


def run_lease_probe(store: HttpTupleStore, state: dict, run_id: str) -> LeaseProbeResult:
    print(f"[lease-probe] writing one tuple into {_LEASE_PROBE_SUBSPACE}", flush=True)
    store.out(
        _LEASE_PROBE_SUBSPACE, {"queue": "work"}, {"seq": "0"},
        body="lease-probe", nonce=f"{run_id}-lease-probe",
    )
    got = store.inp(_LEASE_PROBE_SUBSPACE, {"queue": "work"},
                     claimant=f"mvv2-lease-probe-{run_id}-a", lease_s=1)
    assert got is not None, "lease probe: first claim found nothing"
    _row, claim_id = got

    t0 = time.monotonic()
    deadline = t0 + 20.0
    reclaimed = None
    while time.monotonic() < deadline:
        time.sleep(0.5)
        reclaimed = store.inp(_LEASE_PROBE_SUBSPACE, {"queue": "work"},
                               claimant=f"mvv2-lease-probe-{run_id}-b", lease_s=30)
        if reclaimed is not None:
            break
    wait_s = time.monotonic() - t0
    if reclaimed is not None:
        _row2, claim_id2 = reclaimed
        store.ack(claim_id2, f"mvv2-lease-probe-{run_id}-b")

    counts = claim_log_counts(state, _LEASE_PROBE_SUBSPACE)
    return LeaseProbeResult(
        first_claim_id=claim_id, reclaimed=reclaimed is not None,
        reclaim_wait_s=wait_s, expire_log_rows=counts.get("expire", 0),
    )


# ── leg 3: bloat ─────────────────────────────────────────────────────────


@dataclass
class BloatSample:
    elapsed_s: float
    cycles: int
    n_live_tup: int
    n_dead_tup: int
    dead_ratio: float
    last_autovacuum: str | None


@dataclass
class BloatLegResult:
    row_pool_size: int
    n_workers: int
    cycles_completed: int
    elapsed_s: float
    stopped_reason: str
    samples: list[BloatSample]


def run_bloat_leg(store: HttpTupleStore, state: dict, run_id: str, *,
                   row_pool_size: int, n_workers: int, max_cycles: int,
                   max_seconds: float, sample_interval_s: float,
                   log_path: Path) -> BloatLegResult:
    print(f"[bloat] writing {row_pool_size} fixed rows into {_BLOAT_SUBSPACE}", flush=True)
    for i in range(row_pool_size):
        store.out(
            _BLOAT_SUBSPACE, {"queue": "work"}, {"seq": str(i)},
            body=f"slot-{i}", nonce=f"{run_id}-bloat-{i}",
        )

    stop_event = threading.Event()
    t_start = time.monotonic()
    stopped_reason = "max_cycles"
    counter = _Counter()

    def worker(idx: int) -> None:
        claimant = f"mvv2-bloat-{run_id}-w{idx}"
        while not stop_event.is_set():
            got = store.inp(_BLOAT_SUBSPACE, {"queue": "work"}, claimant=claimant, lease_s=30)
            if got is None:
                continue
            _row, claim_id = got
            store.nack(claim_id, claimant)
            counter.increment()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_workers)]
    for th in threads:
        th.start()

    samples: list[BloatSample] = []
    log_fh = open(log_path, "a")
    try:
        while True:
            elapsed = time.monotonic() - t_start
            c = counter.value()
            stats = tuples_table_stats(state)
            live = stats["n_live_tup"]
            dead = stats["n_dead_tup"]
            ratio = dead / (live + dead) if (live + dead) else 0.0
            sample = BloatSample(
                elapsed_s=elapsed, cycles=c, n_live_tup=live, n_dead_tup=dead,
                dead_ratio=ratio, last_autovacuum=stats["last_autovacuum"],
            )
            samples.append(sample)
            line = (f"[bloat] t={elapsed:7.1f}s cycles={c:>9} live={live:>6} "
                    f"dead={dead:>6} dead_ratio={ratio:.4f} "
                    f"last_autovacuum={stats['last_autovacuum']}")
            print(line, flush=True)
            log_fh.write(line + "\n")
            log_fh.flush()
            if c >= max_cycles:
                stopped_reason = "max_cycles"
                break
            if elapsed >= max_seconds:
                stopped_reason = "max_seconds"
                break
            time.sleep(sample_interval_s)
    finally:
        stop_event.set()
        for th in threads:
            th.join(timeout=10.0)
        log_fh.close()

    return BloatLegResult(
        row_pool_size=row_pool_size, n_workers=n_workers,
        cycles_completed=counter.value(), elapsed_s=time.monotonic() - t_start,
        stopped_reason=stopped_reason, samples=samples,
    )


# ── reporting ────────────────────────────────────────────────────────────


def _pctile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(p * (len(s) - 1)))))
    return s[k]


def summarize(engine: Engine, audit: AuditLegResult, lease_probe: LeaseProbeResult,
              bloat: BloatLegResult | None) -> dict:
    durations = audit.claim_durations_s
    return {
        "engine": {
            "worktree_head_sha": engine.head_sha,
            "version_endpoint": engine.version_body,
            "tenant": engine.tenant,
        },
        "audit_shape_leg": {
            "n_tuples": audit.n_tuples,
            "n_workers": audit.n_workers,
            "elapsed_s": audit.elapsed_s,
            "verification": {
                "consumed_total": audit.consumed_total,
                "expected": audit.n_tuples,
                "subspace_stats_final": audit.subspace_stats_final,
                "claim_log_counts": audit.claim_log_counts,
                "two_claim_id_tuple_count": audit.two_claim_id_tuple_count,
                "audit_shape_holds": (
                    audit.consumed_total == audit.n_tuples
                    and audit.subspace_stats_final["available"] == 0
                    and audit.subspace_stats_final["claimed"] == 0
                    and audit.subspace_stats_final["consumed"] == audit.n_tuples
                    and audit.claim_log_counts.get("claim", 0) == audit.n_tuples
                    and audit.claim_log_counts.get("ack", 0) == audit.n_tuples
                    and audit.claim_log_counts.get("expire", 0) == 0
                    and audit.two_claim_id_tuple_count == 0
                ),
            },
            "metrics": {
                "claim_transaction_duration_s": {
                    "note": "client-observed in_()/inp() round-trip latency "
                            "(network + server transaction) -- an upper bound "
                            "on the raw DB transaction time research-3 measured "
                            "directly over psql (p50 3.96ms, p99 6.01ms). "
                            "CAVEAT: a parked in_(timeout_s=...) call that finds "
                            "nothing claimable blocks for up to the full "
                            "claim-timeout before returning None, so p99/max can "
                            "be dominated by that parking latency rather than by "
                            "transaction time -- see empty_claims_after_wake's "
                            "count for how many of the calls in this sample "
                            "parked (run 2: 9 of 209 calls parked the full 5s "
                            "timeout, giving p99=5018.8ms/max=5022.5ms; p50 is "
                            "the representative successful-claim latency).",
                    "n": len(durations),
                    "mean": statistics.mean(durations) if durations else 0.0,
                    "p50": _pctile(durations, 0.50),
                    "p99": _pctile(durations, 0.99),
                    "max": max(durations) if durations else 0.0,
                },
                "oldest_unclaimed_age_s": {
                    "note": "age of the oldest available (unclaimed, "
                            "unconsumed) tuple in the queue, sampled every "
                            "100ms through the drain via a non-blocking rdp()",
                    "n_samples": len(audit.oldest_unclaimed_age_samples),
                    "max": max(audit.oldest_unclaimed_age_samples) if audit.oldest_unclaimed_age_samples else 0.0,
                    "mean": statistics.mean(audit.oldest_unclaimed_age_samples) if audit.oldest_unclaimed_age_samples else 0.0,
                },
                "empty_claims_after_wake": {
                    "note": "a blocking in_() call that returned None while, "
                            "at the moment it started, some tuple in the "
                            "subspace was still believed unconsumed by "
                            "everyone -- a genuine lost race to another "
                            "claimant (or a SKIP LOCKED miss on a candidate "
                            "another claimant momentarily held). The worker "
                            "loop stops BEFORE calling again once nothing "
                            "remains, so this count structurally excludes "
                            "the uninteresting 'no more work at all' case.",
                    "count": audit.empty_claims_after_wake,
                },
            },
        },
        "lease_expiry_probe": {
            "reclaimed_after_lease_lapse": lease_probe.reclaimed,
            "reclaim_wait_s": lease_probe.reclaim_wait_s,
            "expire_log_rows": lease_probe.expire_log_rows,
        },
        "bloat_leg": None if bloat is None else {
            "row_pool_size": bloat.row_pool_size,
            "n_workers": bloat.n_workers,
            "cycles_completed": bloat.cycles_completed,
            "elapsed_s": bloat.elapsed_s,
            "stopped_reason": bloat.stopped_reason,
            "per_table_autovacuum_vacuum_scale_factor": 0.01,
            "note": "one claim+nack per cycle on a FIXED row pool (the "
                    "client API has no unconsume/reopen primitive, so nack "
                    "-- which resets claim state for reclaim and counts an "
                    "attempt -- stands in for research-3's raw-SQL "
                    "'claim, ack, reopen' methodology; same churn shape, "
                    "reached through the client surface alone)",
            "first_sample": None if not bloat.samples else {
                "elapsed_s": bloat.samples[0].elapsed_s,
                "n_live_tup": bloat.samples[0].n_live_tup,
                "n_dead_tup": bloat.samples[0].n_dead_tup,
                "dead_ratio": bloat.samples[0].dead_ratio,
            },
            "last_sample": None if not bloat.samples else {
                "elapsed_s": bloat.samples[-1].elapsed_s,
                "n_live_tup": bloat.samples[-1].n_live_tup,
                "n_dead_tup": bloat.samples[-1].n_dead_tup,
                "dead_ratio": bloat.samples[-1].dead_ratio,
                "last_autovacuum": bloat.samples[-1].last_autovacuum,
            },
            "max_dead_ratio_observed": max((s.dead_ratio for s in bloat.samples), default=0.0),
            "samples": [
                {
                    "elapsed_s": s.elapsed_s, "cycles": s.cycles,
                    "n_live_tup": s.n_live_tup, "n_dead_tup": s.n_dead_tup,
                    "dead_ratio": s.dead_ratio, "last_autovacuum": s.last_autovacuum,
                }
                for s in bloat.samples
            ],
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-id", default=uuid.uuid4().hex[:12])
    ap.add_argument("--tuples", type=int, default=200)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--lease-seconds", type=int, default=60)
    ap.add_argument("--claim-timeout-seconds", type=float, default=5.0)
    ap.add_argument("--skip-bloat", action="store_true")
    ap.add_argument("--bloat-row-pool", type=int, default=1000)
    ap.add_argument("--bloat-workers", type=int, default=20)
    ap.add_argument("--bloat-max-cycles", type=int, default=1_000_000)
    ap.add_argument("--bloat-max-seconds", type=float, default=600.0)
    ap.add_argument("--bloat-sample-interval-seconds", type=float, default=3.0)
    ap.add_argument("--out", type=Path, default=_SPIKE_DIR / "last-run-summary.json")
    args = ap.parse_args()

    print(f"[main] run_id={args.run_id}", flush=True)
    engine = boot_engine()

    audit = run_audit_leg(
        engine.store, engine.state, args.run_id,
        n_tuples=args.tuples, n_workers=args.workers,
        lease_s=args.lease_seconds, claim_timeout_s=args.claim_timeout_seconds,
    )
    print(f"[audit] done: consumed={audit.consumed_total}/{audit.n_tuples} "
          f"empty_claims_after_wake={audit.empty_claims_after_wake} "
          f"elapsed={audit.elapsed_s:.2f}s", flush=True)

    lease_probe = run_lease_probe(engine.store, engine.state, args.run_id)
    print(f"[lease-probe] reclaimed={lease_probe.reclaimed} "
          f"wait={lease_probe.reclaim_wait_s:.2f}s "
          f"expire_rows={lease_probe.expire_log_rows}", flush=True)

    bloat = None
    if not args.skip_bloat:
        bloat_log = args.out.with_suffix(".bloat.log")
        bloat = run_bloat_leg(
            engine.store, engine.state, args.run_id,
            row_pool_size=args.bloat_row_pool, n_workers=args.bloat_workers,
            max_cycles=args.bloat_max_cycles, max_seconds=args.bloat_max_seconds,
            sample_interval_s=args.bloat_sample_interval_seconds,
            log_path=bloat_log,
        )
        print(f"[bloat] done: cycles={bloat.cycles_completed} "
              f"elapsed={bloat.elapsed_s:.1f}s reason={bloat.stopped_reason}", flush=True)

    summary = summarize(engine, audit, lease_probe, bloat)
    args.out.write_text(json.dumps(summary, indent=2, default=str))
    print(f"[main] summary written to {args.out}", flush=True)
    print(json.dumps({k: v for k, v in summary.items() if k != "bloat_leg"}, indent=2, default=str))
    if bloat is not None:
        print(json.dumps({"bloat_leg": {k: v for k, v in summary["bloat_leg"].items() if k != "samples"}}, indent=2, default=str))
    return 0 if summary["audit_shape_leg"]["verification"]["audit_shape_holds"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
