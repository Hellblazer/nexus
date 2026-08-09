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
