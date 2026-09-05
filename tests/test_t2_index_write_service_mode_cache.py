# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-53x7s: t2_index_write's SERVICE-mode branch must reuse a single
process-lifetime T2Database instead of constructing (and closing, tearing
down every httpx.Client connection pool) one per call.

Prior behavior: every call opened `with T2Database(...) as db:`, which
constructs 8 fresh Http*Store instances (each owning its own httpx.Client)
and closes all of them on exit -- defeating keep-alive pooling. Measured in
a live shakeout: 387 per-document hook calls produced 387x the connection-
init log noise and inflated hook wall-time to ~13x the actual upload time.

Design (post stacked-review correction, 2026-07-05): a TTL-bounded cache was
tried first and rejected -- each Http*Store bakes its base_url/token in at
construction and never re-reads them, so a TTL window doesn't track the
thing that actually rotates (the storage_service lease) and only bounds
staleness to an unrelated clock, while introducing a close-while-in-use
race for concurrent callers. The fix is a process-lifetime singleton,
checked out and used under one lock (so a concurrent caller can never
close() an instance still in flight), with reactive invalidation: any
write_fn failure evicts the cached instance so the next call resolves a
fresh endpoint, mirroring the recover-on-error pattern already used by
http_token_store/http_scratch_store.
"""
from __future__ import annotations

import pytest

# nexus-m20mf P2: pytest fixtures must be visible as MODULE-level names to be
# discovered for this file's tests, unlike this file's other cross-module
# helpers (deliberately deferred inside each test body below) — importing
# `fake_service` only inside a function would leave it invisible to pytest's
# fixture collection.
from tests.db.test_refreshable_client import fake_service  # noqa: F401 — used as a fixture, not called directly


@pytest.fixture(autouse=True)
def _reset_around(monkeypatch):
    import nexus.mcp_infra as mi
    from nexus.db.storage_mode import StorageBackend

    monkeypatch.setattr("nexus.db.storage_mode.storage_backend_for", lambda _kind: StorageBackend.SERVICE)
    mi.reset_singletons()
    yield
    mi.reset_singletons()


def test_service_mode_reuses_singleton_across_calls(monkeypatch) -> None:
    import nexus.mcp_infra as mi

    constructed = []

    class _FakeT2Database:
        def __init__(self, *_a, **_kw) -> None:
            constructed.append(self)
            self.closed = False

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("nexus.db.t2.T2Database", _FakeT2Database)

    for _ in range(5):
        mi.t2_index_write(lambda db: db)

    assert len(constructed) == 1, "must reuse one T2Database for the process lifetime"
    assert constructed[0].closed is False, "cached instance must not be closed between successful calls"


def test_service_mode_evicts_and_rebuilds_after_write_fn_error(monkeypatch) -> None:
    """A write_fn failure evicts the cached instance so the next call
    resolves a fresh endpoint (reactive recovery against a rotated
    storage_service lease), instead of retrying the same broken
    connections for the rest of the process's lifetime."""
    import nexus.mcp_infra as mi

    constructed = []

    class _FakeT2Database:
        def __init__(self, *_a, **_kw) -> None:
            constructed.append(self)
            self.closed = False

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("nexus.db.t2.T2Database", _FakeT2Database)

    def _boom(_db):
        raise ConnectionError("stale lease")

    with pytest.raises(ConnectionError):
        mi.t2_index_write(_boom)

    assert len(constructed) == 1
    assert constructed[0].closed is True, "the failed instance must be evicted (closed) immediately"

    mi.t2_index_write(lambda db: db)

    assert len(constructed) == 2, "the next call must rebuild against a fresh instance"
    assert constructed[1].closed is False


def test_reset_singletons_clears_service_t2_singleton(monkeypatch) -> None:
    import nexus.mcp_infra as mi

    class _FakeT2Database:
        def __init__(self, *_a, **_kw) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("nexus.db.t2.T2Database", _FakeT2Database)

    mi.t2_index_write(lambda db: db)
    assert mi._service_t2_db is not None

    mi.reset_singletons()
    assert mi._service_t2_db is None


# ── CAS narrowing (nexus-ldab2) ──────────────────────────────────────────────


def test_write_fn_releases_lock_before_running(monkeypatch) -> None:
    """nexus-ldab2: ``_service_t2_lock`` narrows to the singleton
    RESOLUTION only — it must not span ``write_fn``'s own (potentially
    slow / network-bound) execution. Proven with a 3-party barrier inside
    write_fn: under the OLD pre-narrowing behavior (lock held across the
    whole call) only one thread at a time could ever reach the barrier,
    and this test would time out via ``BrokenBarrierError`` instead of
    passing."""
    import threading

    import nexus.mcp_infra as mi

    constructed: list = []
    barrier = threading.Barrier(3, timeout=5)

    class _FakeT2Database:
        def __init__(self, *_a, **_kw) -> None:
            constructed.append(self)
            self.closed = False

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("nexus.db.t2.T2Database", _FakeT2Database)

    def _write(_db):
        barrier.wait()  # only satisfied if all 3 calls are in flight together

    errors: list[Exception] = []

    def _call() -> None:
        try:
            mi.t2_index_write(_write)
        except Exception as exc:  # noqa: BLE001 — captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=_call) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, (
        f"calls did not overlap within the barrier timeout — the lock is "
        f"still spanning write_fn: {errors}"
    )
    assert len(constructed) == 1


def test_concurrent_double_failure_does_not_double_close(monkeypatch) -> None:
    """T5(iii): two threads resolve the SAME T2Database instance and both
    fail concurrently. Exactly one must win the CAS eviction and close it;
    the other must observe the slot already cleared and do nothing
    further — never a second ``close()`` on the same instance."""
    import threading

    import nexus.mcp_infra as mi

    close_calls: list[int] = []
    barrier = threading.Barrier(2, timeout=5)

    class _FlakyT2Database:
        def __init__(self, *_a, **_kw) -> None:
            pass

        def close(self) -> None:
            close_calls.append(id(self))

    monkeypatch.setattr("nexus.db.t2.T2Database", _FlakyT2Database)

    def _boom(_db):
        barrier.wait()  # both threads fail together, same resolved instance
        raise ConnectionError("stale lease")

    errors: list[Exception] = []

    def _call() -> None:
        try:
            mi.t2_index_write(_boom)
        except ConnectionError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 2, "both concurrent calls must raise"
    assert len(close_calls) == 1, (
        f"the shared instance must be closed exactly once, got {len(close_calls)}"
    )
    assert mi._service_t2_db is None


def test_op_stats_key_on_explicit_op_name(monkeypatch) -> None:
    """nexus-ldab2: ``service_t2_op_stats()`` mirrors
    ``catalog.factory.service_catalog_op_stats()`` — differentiated by the
    caller-supplied ``op`` label, since most ``write_fn`` callables are
    anonymous lambdas with no useful ``__name__``."""
    import nexus.mcp_infra as mi

    class _FakeT2Database:
        def __init__(self, *_a, **_kw) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("nexus.db.t2.T2Database", _FakeT2Database)
    mi.reset_service_t2_op_stats()

    mi.t2_index_write(lambda db: db, op="taxonomy_assign")
    mi.t2_index_write(lambda db: db)  # default bucket

    stats = mi.service_t2_op_stats()
    assert stats["taxonomy_assign"]["calls"] == 1
    assert stats["t2_write"]["calls"] == 1

    mi.reset_service_t2_op_stats()
    assert mi.service_t2_op_stats() == {}


# ── nexus-0dpli: eviction must not abort a healthy in-flight sibling ────────


def test_domain_exception_does_not_evict(monkeypatch) -> None:
    """nexus-0dpli: a routine business exception (e.g. an aspect-queue
    conflict surfaced as a plain ``ValueError``) must propagate WITHOUT
    touching the shared ``T2Database`` singleton — eviction is reserved
    for genuine connectivity failures. Over-evicting here is worse than
    on the catalog side: ``T2Database.close()`` tears down SEVEN
    independent substores' clients, so a routine failure in ONE substore
    would otherwise abort every OTHER concurrent, healthy caller sharing
    the same singleton."""
    import nexus.mcp_infra as mi

    close_calls: list[int] = []

    class _FakeT2Database:
        def __init__(self, *_a, **_kw) -> None:
            pass

        def close(self) -> None:
            close_calls.append(1)

    monkeypatch.setattr("nexus.db.t2.T2Database", _FakeT2Database)

    def _routine_failure(_db):
        raise ValueError("routine business validation failure")

    with pytest.raises(ValueError):
        mi.t2_index_write(_routine_failure)

    assert close_calls == [], (
        "a routine domain exception must never evict the shared T2Database"
    )
    assert mi._service_t2_db is not None, (
        "the still-healthy instance must remain installed for the next caller"
    )


def test_in_flight_sibling_survives_eviction_close_deferred_until_it_exits(
    monkeypatch,
) -> None:
    """nexus-0dpli, the CRITICAL fix: thread A is genuinely mid-``write_fn``
    (parked on a rendezvous) sharing the SAME resolved ``T2Database`` as
    thread B, which fails with a genuine connectivity error and triggers
    eviction. Must hold ALL THREE properties:

    (i)   A's in-flight call completes successfully against its
          already-resolved instance — eviction never closes it out from
          under A.
    (ii)  A caller resolving AFTER the eviction gets a FRESH instance
          (the slot is cleared immediately).
    (iii) The evicted instance is closed EXACTLY ONCE, only once A has
          released its own reference (i.e. after A exits) — never by B.
    """
    import threading

    import nexus.mcp_infra as mi

    constructed: list[int] = []
    close_calls: list[int] = []
    a_parked = threading.Event()
    release_a = threading.Event()

    class _FakeT2Database:
        def __init__(self, *_a, **_kw) -> None:
            constructed.append(len(constructed) + 1)
            self.instance_id = len(constructed)

        def close(self) -> None:
            close_calls.append(self.instance_id)

    monkeypatch.setattr("nexus.db.t2.T2Database", _FakeT2Database)

    a_result: dict = {}

    def _a_write(db) -> str:
        a_parked.set()
        release_a.wait(5)
        return f"a-done-on-{db.instance_id}"

    def _b_write(_db):
        raise ConnectionError("connection reset")

    def thread_a() -> None:
        try:
            a_result["value"] = mi.t2_index_write(_a_write)
        except Exception as exc:  # noqa: BLE001 — captured for the assertion below
            a_result["error"] = exc

    ta = threading.Thread(target=thread_a)
    ta.start()
    assert a_parked.wait(5), "thread A never entered write_fn"

    # Thread B resolves the SAME instance A is using and fails with a
    # connectivity error, triggering eviction.
    with pytest.raises(ConnectionError):
        mi.t2_index_write(_b_write)

    # (iii) part 1: not closed yet — A is still in flight.
    assert close_calls == [], (
        "the shared T2Database was closed while a healthy sibling was "
        "still mid-write_fn — the use-after-close bug nexus-0dpli fixes"
    )
    # (ii): the slot is cleared immediately, regardless of drainage.
    assert mi._service_t2_db is None, (
        "the shared slot must be cleared immediately so new callers never "
        "resolve the doomed instance"
    )

    # (ii) continued: a caller arriving now must get a FRESH instance.
    mi.t2_index_write(lambda db: db)
    assert len(constructed) == 2, "a post-eviction caller must build fresh"

    # Let A finish. Its call must succeed — (i).
    release_a.set()
    ta.join(5)
    assert a_result.get("value") == "a-done-on-1", (
        f"thread A's in-flight call was aborted: {a_result}"
    )

    # (iii) part 2: NOW the old (instance 1) is closed, exactly once, and
    # the fresh instance (2) was never touched.
    assert close_calls == [1], (
        f"expected instance 1 closed exactly once after A exited, got {close_calls}"
    )


# ── nexus-m20mf P2: nx_answer's five happy-path contexts share one T2 ──────
#
# Design record: T2 nexus/design-nexus-m20mf-single-t2-transport. Before this
# bead, nx_answer's plan-match/price-table/run-start/record-run/run-outcome
# sites each opened `with _t2_ctx() as db:`, a FRESH T2Database (8 Http*Store
# constructions each) per context — 40 constructions for one call. They now
# route through `_t2_index_write`, so a happy-path call should resolve the
# ONE process-lifetime singleton at every site.


class _CountingT2Database:
    """Minimal T2Database stand-in: exposes ``.plans``/``.telemetry`` as
    MagicMocks (never touches a real store) and records its own
    construction/close for the assertions below."""

    def __init__(self) -> None:
        from unittest.mock import MagicMock

        self.plans = MagicMock()
        self.telemetry = MagicMock()
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_nx_answer_happy_path_uses_one_shared_t2_instance(monkeypatch) -> None:
    """nexus-m20mf P2 regression pin: nx_answer's five happy-path T2
    contexts (plan match, price table, run_start, record_run,
    run_outcome) must resolve the SAME shared T2Database, not a fresh
    construction per context. Before this bead: 5. After: 1."""
    from unittest.mock import AsyncMock, MagicMock, patch

    import nexus.mcp_infra as _infra
    import nexus.plans.runner as _runner
    from nexus.plans.cost_estimate import OperatorPriceTable
    from nexus.plans.runner import PlanResult
    from tests.test_nx_answer import _make_match

    constructed: list[_CountingT2Database] = []

    def _make_counting_t2(*_a, **_kw) -> _CountingT2Database:
        db = _CountingT2Database()
        constructed.append(db)
        return db

    monkeypatch.setattr("nexus.db.t2.T2Database", _make_counting_t2)

    match = _make_match(confidence=0.75)
    run_result = PlanResult(steps=[{"text": "The final answer."}])

    with (
        patch("nexus.plans.matcher.plan_match", return_value=[match]),
        patch.object(_infra, "get_t1_plan_cache",
                     return_value=MagicMock(is_available=False)),
        patch("nexus.plans.cost_estimate.get_cached_price_table",
              return_value=OperatorPriceTable({})),
        patch("nexus.mcp.core.scratch", MagicMock()),
        patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
    ):
        from nexus.mcp.core import nx_answer
        result = await nx_answer("what is projection quality?")

    assert "final answer" in result.lower()
    assert len(constructed) == 1, (
        f"expected exactly one shared T2Database across the happy path's "
        f"five contexts (plan_match, price_table, run_start, record_run, "
        f"run_outcome); got {len(constructed)}"
    )
    assert constructed[0].closed is False, (
        "the shared singleton must survive a successful call, not be "
        "closed at a context boundary the way _t2_ctx's fresh instances were"
    )


@pytest.mark.asyncio
async def test_plan_match_t1_failure_does_not_evict_the_shared_t2_singleton(
    monkeypatch,
) -> None:
    """nexus-m20mf P2 non-negotiable rule, pinned: the write_fn passed to
    ``_t2_index_write`` for the plan-match site holds ONLY the
    ``db.plans`` attribute access. ``get_t1_plan_cache``'s T1 reach runs
    OUTSIDE that closure, on purpose — ``_service_t2_write_locked``
    evicts the shared singleton on a connectivity-classified failure
    from write_fn, and a fat closure wrapping the T1 call would let a
    T1-only outage evict a healthy T2 singleton out from under
    concurrent siblings. Falsified by reverting the hoist: a T1 failure
    would then originate INSIDE the closure and this test would see the
    singleton closed."""
    from unittest.mock import patch

    import nexus.mcp_infra as _infra

    constructed: list[_CountingT2Database] = []

    def _make_counting_t2(*_a, **_kw) -> _CountingT2Database:
        db = _CountingT2Database()
        constructed.append(db)
        return db

    monkeypatch.setattr("nexus.db.t2.T2Database", _make_counting_t2)

    def _t1_unreachable(*_a, **_kw):
        raise ConnectionError("T1 unreachable")

    with patch.object(_infra, "get_t1_plan_cache", side_effect=_t1_unreachable):
        from nexus.mcp.core import nx_answer
        result = await nx_answer("what is projection quality?")

    assert "Error during plan match" in result
    assert len(constructed) == 1, (
        "the db.plans closure itself must still succeed once, before the "
        "T1 call (outside the closure) raises"
    )
    assert constructed[0].closed is False, (
        "a T1 connectivity failure OUTSIDE the write_fn closure must never "
        "evict the shared T2Database singleton"
    )
    assert _infra._service_t2_db is constructed[0], (
        "the singleton slot must remain installed after a non-T2 failure"
    )


def test_shared_singleton_401_remints_without_eviction(fake_service, monkeypatch) -> None:
    """nexus-ig3qe, through the shared instance (nexus-m20mf P2 risk,
    design record §4 RDR-005 data-token lease): a stale data token 401
    against one of the shared T2Database's stores must still self-heal
    via the existing per-store re-mint/retry — ``httpx.HTTPStatusError``
    (a 401) is never classified as a connectivity error
    (``nexus.retry._is_connectivity_error`` only recognizes transport-
    layer failures), so ``_service_t2_write_locked`` never sees a reason
    to evict and the shared instance is reused across the retry, not
    rebuilt."""
    import nexus.mcp_infra as mi
    import tests.db.test_refreshable_client as _rc
    from tests.db.test_refreshable_client import _MINT_CREDENTIAL, _make_echo_store

    monkeypatch.setenv("NX_MINT_TOKEN", _MINT_CREDENTIAL)
    _rc._reset_fake_service_state()

    instances: list[object] = []

    class _FakeT2WithEcho:
        def __init__(self, *_a, **_kw) -> None:
            self.plans = _make_echo_store()
            self.closed = False
            instances.append(self)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("nexus.db.t2.T2Database", _FakeT2WithEcho)

    baseline = mi.t2_index_write(lambda db: db.plans.echo_post("call 1"), op="plan_match")
    assert baseline == {"echo": {"value": "call 1"}}
    assert _rc._MINT_CALLS == 1

    _rc._MINTED_DATA_TOKEN = "revoked-elsewhere"  # simulate an out-of-band rotation

    result = mi.t2_index_write(lambda db: db.plans.echo_post("call 2"), op="plan_match")

    assert result == {"echo": {"value": "call 2"}}
    assert _rc._MINT_CALLS == 2, "expected exactly one re-mint, not zero and not a loop"
    assert len(instances) == 1, (
        "the shared T2Database singleton must be reused across the "
        "401-then-remint retry, never rebuilt"
    )
    assert mi._service_t2_db is instances[0]
    assert instances[0].closed is False


# ── nexus-m20mf P2 round 2 (critique [24578] + code review [24580]) ────────
#
# The first cut's plan_match/price_table/record_run closures returned a
# bare store reference and did their REAL T2 work (matcher.plan_match's
# library.get_plan/increment_match_metrics; get_cached_price_table's
# query_nx_answer_runs; _nx_answer_record_run's record_nx_answer_run POST)
# OUTSIDE the closure -- released from _service_t2_write_locked's refcount
# before that work even started, so neither a genuine failure there nor a
# concurrent sibling's eviction were ever attributed to the right call.
# core.py now wraps the WHOLE call in each closure. The three tests below
# verify the ACTUAL, differing outcome per site: plan_match's real T2
# calls have no internal swallow, so a connectivity failure genuinely
# evicts; price_table's and record_run's callees each have their OWN
# documented "never raises" contract that swallows the failure before
# _service_t2_write_locked's classifier can see it, so eviction does NOT
# trigger there -- a real, described residual, not silently claimed fixed.


@pytest.mark.asyncio
async def test_plan_match_connectivity_error_evicts_the_shared_t2_singleton(
    monkeypatch,
) -> None:
    """nexus-m20mf P2 round 2: matcher.plan_match's real T2 traffic
    (HttpPlanLibrary.get_plan / increment_match_metrics) now runs INSIDE
    the _t2_index_write closure. Neither call has an internal broad
    except, so a genuine connectivity failure there propagates to
    _service_t2_write_locked's classifier and evicts the shared
    singleton -- exactly like run_start/run_outcome already did."""
    from unittest.mock import MagicMock, patch

    import nexus.mcp_infra as _infra

    constructed: list[_CountingT2Database] = []

    def _make_counting_t2(*_a, **_kw) -> _CountingT2Database:
        db = _CountingT2Database()
        constructed.append(db)
        return db

    monkeypatch.setattr("nexus.db.t2.T2Database", _make_counting_t2)

    def _plan_match_connectivity_failure(*_a, **_kw):
        raise ConnectionError("plan library unreachable")

    with patch.object(_infra, "get_t1_plan_cache",
                       return_value=MagicMock(is_available=False)), \
         patch("nexus.plans.matcher.plan_match",
               side_effect=_plan_match_connectivity_failure):
        from nexus.mcp.core import nx_answer
        result = await nx_answer("what is projection quality?")

    assert "Error during plan match" in result
    assert len(constructed) == 1, (
        "the cache-populate closure (a bare db.plans access) still "
        "succeeds once before the plan_match closure fails"
    )
    assert constructed[0].closed is True, (
        "a genuine connectivity failure INSIDE the plan_match closure "
        "must evict the shared T2Database singleton -- the concrete "
        "gap critique [24578] found"
    )
    assert _infra._service_t2_db is None


def test_price_table_connectivity_error_is_swallowed_not_evicted(monkeypatch) -> None:
    """Documented residual (critique [24578]): get_cached_price_table ->
    build_operator_price_table has its OWN
    `except Exception: return OperatorPriceTable({})` around
    telemetry_store.query_nx_answer_runs -- an intentional "never raises,
    degrade to empty table" contract other callers rely on. Wrapping the
    whole call in _t2_index_write's closure is structurally correct (the
    real HTTP work now runs inside the refcount window), but a
    connectivity failure there is swallowed by that inner except before
    _service_t2_write_locked's classifier ever sees it, so eviction does
    NOT trigger from a failure at this specific site. This test pins that
    reality rather than asserting the eviction the literal closure-purity
    fix does not, by itself, deliver here."""
    from unittest.mock import MagicMock

    import nexus.mcp_infra as mi
    from nexus.plans.cost_estimate import get_cached_price_table

    class _FakeT2Database:
        def __init__(self, *_a, **_kw) -> None:
            self.telemetry = MagicMock()
            self.telemetry.query_nx_answer_runs.side_effect = ConnectionError(
                "telemetry store unreachable",
            )
            self.closed = False

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("nexus.db.t2.T2Database", _FakeT2Database)

    price_table = mi.t2_index_write(
        lambda db: get_cached_price_table(db.telemetry, force_refresh=True),
        op="price_table",
    )

    assert price_table.price_for("anything") == (
        None, None, "unpriceable(no-history, not-a-known-operator)",
    ), (
        "build_operator_price_table degrades to an empty table on the "
        "connectivity failure, exactly as it does for any other query "
        "failure -- never a fabricated price"
    )
    assert mi._service_t2_db is not None, (
        "the swallowed failure never reaches _service_t2_write_locked's "
        "classifier, so the singleton is NOT evicted here -- the "
        "documented residual, not a claimed fix"
    )
    assert mi._service_t2_db.closed is False


def test_record_run_connectivity_error_is_swallowed_not_evicted(monkeypatch) -> None:
    """Documented residual (critique [24578]): _nx_answer_record_run has
    its OWN `except Exception: _warn_telemetry_drop(...)` around the real
    db.telemetry.record_nx_answer_run POST -- "best-effort telemetry,
    must not crash caller," and this helper is called from several OTHER
    nx_answer sites this bead does not touch. Wrapping the whole call in
    _t2_index_write's closure is structurally correct, but that internal
    swallow means a connectivity failure here is caught before
    _service_t2_write_locked's classifier can see it -- eviction does NOT
    trigger from a failure at this specific site, same shape as price_table
    above."""
    from unittest.mock import MagicMock

    import nexus.mcp_infra as mi
    from nexus.mcp.core import _nx_answer_record_run

    class _FakeT2Database:
        def __init__(self, *_a, **_kw) -> None:
            self.telemetry = MagicMock()
            self.telemetry.record_nx_answer_run.side_effect = ConnectionError(
                "telemetry store unreachable",
            )
            self.closed = False

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("nexus.db.t2.T2Database", _FakeT2Database)

    mi.t2_index_write(
        lambda db: _nx_answer_record_run(
            db.telemetry, question="q", plan_id=1, matched_confidence=0.9,
            step_count=1, final_text="answer", step_records=[],
            duration_ms=10, trace=True,
        ),
        op="record_run",
    )  # must not raise -- the internal swallow absorbs it

    assert mi._service_t2_db is not None, (
        "the swallowed failure never reaches _service_t2_write_locked's "
        "classifier, so the singleton is NOT evicted here -- the "
        "documented residual, not a claimed fix"
    )
    assert mi._service_t2_db.closed is False


@pytest.mark.asyncio
async def test_run_outcome_connectivity_error_evicts_mid_call_and_record_run_still_lands(
    monkeypatch,
) -> None:
    """critique round 2 (T2 code-review-nexus-m20mf-round2 [24605]): no
    prior test proved eviction-and-rebuild works MID one nx_answer call
    -- the plan_match eviction test above aborts the whole call (its
    failure propagates out of nx_answer entirely), and the generic infra
    tests (test_service_mode_evicts_and_rebuilds_after_write_fn_error)
    only prove it across two SEPARATE top-level t2_index_write calls.

    run_outcome (Step 5, `_nx_answer_record_outcome`) is the site that
    closes this gap: it fires BEFORE record_run (Step 6) in the SAME
    nx_answer call, and `_nx_answer_record_outcome`'s own
    `except Exception: ... warning(...)` swallows whatever
    `_t2_index_write` raises -- but `_service_t2_write_locked` classifies
    and evicts INSIDE `_t2_index_write`, before that outer swallow ever
    runs. So the singleton is genuinely evicted, the call still
    completes (nothing aborts), and record_run -- the very next
    `_t2_index_write` call in the same invocation -- must resolve a
    FRESH instance and land its write."""
    from unittest.mock import AsyncMock, MagicMock, patch

    import nexus.mcp_infra as _infra
    import nexus.plans.runner as _runner
    from nexus.plans.runner import PlanResult, StepRecord
    from tests.test_nx_answer import _make_match

    constructed: list[_CountingT2Database] = []

    def _make_counting_t2(*_a, **_kw) -> _CountingT2Database:
        db = _CountingT2Database()
        constructed.append(db)
        if len(constructed) == 1:
            # Only the FIRST (run_outcome's) instance fails -- simulates
            # "the connection this singleton held just broke." Every
            # later site must see a rebuilt, healthy instance.
            db.plans.increment_run_outcome.side_effect = ConnectionError(
                "connection reset mid-call",
            )
        return db

    monkeypatch.setattr("nexus.db.t2.T2Database", _make_counting_t2)

    match = _make_match(confidence=0.9)
    run_result = PlanResult(
        steps=[{"text": "The final answer."}],
        step_records=[
            StepRecord(step_index=0, operator="query", source="sql", cost_usd=0.0),
        ],
    )

    with (
        patch("nexus.plans.matcher.plan_match", return_value=[match]),
        patch.object(_infra, "get_t1_plan_cache",
                     return_value=MagicMock(is_available=False)),
        patch("nexus.mcp.core.scratch", MagicMock()),
        patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
    ):
        from nexus.mcp.core import nx_answer
        result = await nx_answer("what is projection quality?")

    assert "final answer" in result.lower(), (
        "run_outcome's own try/except swallows the connectivity error -- "
        "the call must complete normally despite the mid-call eviction"
    )
    assert len(constructed) == 2, (
        "the first instance's run_outcome failure must evict the shared "
        "singleton, forcing record_run (the next _t2_index_write call in "
        "the SAME nx_answer call) to build a second, fresh instance"
    )
    assert constructed[0].closed is True, "the failed first instance must be evicted"
    assert constructed[1].closed is False, "the fresh second instance must survive the call"
    assert constructed[1].telemetry.record_nx_answer_run.called, (
        "record_run must land against the freshly rebuilt singleton -- "
        "this is the recovery-WITHIN-one-call proof the prior tests lacked"
    )
    assert _infra._service_t2_db is constructed[1]
