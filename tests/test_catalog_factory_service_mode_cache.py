# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-5en9j: SERVICE-mode make_catalog_reader()/make_catalog_writer()
must share ONE process-lifetime HttpCatalogClient instead of constructing
(and immediately closing) one per call.

This was the LARGEST single reconstruction count in the nexus-53x7s
shakeout evidence (394x http_catalog_client.init in one run) -- larger
than any of the 8 T2Database substores that bead's first fix addressed.
Same design as mcp_infra._service_t2_write_locked: a process-lifetime
singleton, one lock held for the full call (not just checkout), with
reactive eviction on any call failure.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_around(monkeypatch):
    import nexus.catalog.factory as factory

    monkeypatch.setattr(factory, "_is_catalog_service_mode", lambda: True)
    factory.reset_shared_service_catalog_client_for_tests()
    yield
    factory.reset_shared_service_catalog_client_for_tests()


def _install_fake_client(monkeypatch, constructed: list) -> None:
    class _FakeHttpCatalogClient:
        def __init__(self, *_a, **_kw) -> None:
            constructed.append(self)
            self.closed = False

        def close(self) -> None:
            self.closed = True

        def by_doc_id(self, doc_id: str) -> str:
            return f"doc:{doc_id}"

    monkeypatch.setattr("nexus.catalog.http_catalog_client.HttpCatalogClient", _FakeHttpCatalogClient)


def test_reader_reuses_singleton_across_calls(monkeypatch) -> None:
    from nexus.catalog.factory import make_catalog_reader

    constructed: list = []
    _install_fake_client(monkeypatch, constructed)

    for _ in range(5):
        reader = make_catalog_reader()
        assert reader.by_doc_id("abc") == "doc:abc"

    assert len(constructed) == 1, "must share one HttpCatalogClient for the process lifetime"
    assert constructed[0].closed is False


def test_reader_close_is_noop_and_does_not_tear_down_shared_client(monkeypatch) -> None:
    from nexus.catalog.factory import make_catalog_reader

    constructed: list = []
    _install_fake_client(monkeypatch, constructed)

    reader1 = make_catalog_reader()
    reader1.by_doc_id("x")
    reader1.close()  # historical call-site pattern: close on every exit

    reader2 = make_catalog_reader()
    assert reader2.by_doc_id("y") == "doc:y"

    assert len(constructed) == 1, "close() on the shared handle must not close the underlying client"
    assert constructed[0].closed is False


def test_writer_and_reader_share_the_same_underlying_client(monkeypatch) -> None:
    from nexus.catalog.factory import make_catalog_reader, make_catalog_writer

    constructed: list = []
    _install_fake_client(monkeypatch, constructed)

    make_catalog_reader().by_doc_id("a")
    writer = make_catalog_writer()
    writer.close()

    assert len(constructed) == 1, "reader and writer must share one underlying client"


def test_call_failure_evicts_and_next_call_rebuilds(monkeypatch) -> None:
    from nexus.catalog.factory import make_catalog_reader

    constructed: list = []

    class _FlakyClient:
        def __init__(self, *_a, **_kw) -> None:
            constructed.append(self)
            self.closed = False
            self.calls = 0

        def close(self) -> None:
            self.closed = True

        def by_doc_id(self, doc_id: str) -> str:
            self.calls += 1
            raise ConnectionError("stale lease")

    monkeypatch.setattr("nexus.catalog.http_catalog_client.HttpCatalogClient", _FlakyClient)

    reader = make_catalog_reader()
    with pytest.raises(ConnectionError):
        reader.by_doc_id("x")

    assert len(constructed) == 1
    assert constructed[0].closed is True, "the failed client must be evicted (closed) immediately"

    class _WorkingClient:
        def __init__(self, *_a, **_kw) -> None:
            constructed.append(self)
            self.closed = False

        def close(self) -> None:
            self.closed = True

        def by_doc_id(self, doc_id: str) -> str:
            return f"doc:{doc_id}"

    monkeypatch.setattr("nexus.catalog.http_catalog_client.HttpCatalogClient", _WorkingClient)

    reader2 = make_catalog_reader()
    assert reader2.by_doc_id("y") == "doc:y"
    assert len(constructed) == 2, "the next call must rebuild against a fresh instance"


def test_reset_shared_service_catalog_client_for_tests_clears_singleton(monkeypatch) -> None:
    import nexus.catalog.factory as factory

    constructed: list = []
    _install_fake_client(monkeypatch, constructed)

    factory.make_catalog_reader().by_doc_id("a")
    assert factory._service_catalog_client is not None

    factory.reset_shared_service_catalog_client_for_tests()
    assert factory._service_catalog_client is None


# ── per-op timing split (nexus-jb4pp) ────────────────────────────────────────


def test_op_stats_keys_on_op_name(monkeypatch) -> None:
    """Pins that the counter keys on the OP NAME: the flush-grain
    attribution work turned on being able to say ``atomic_manifest_replace``
    ran 109 times where ``write_manifest_many`` should have run 29."""
    import nexus.catalog.factory as factory
    from nexus.catalog.factory import make_catalog_reader

    class _Client:
        def __init__(self, *_a, **_kw) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

        def by_doc_id(self, doc_id: str) -> str:
            return f"doc:{doc_id}"

    monkeypatch.setattr("nexus.catalog.http_catalog_client.HttpCatalogClient", _Client)
    factory.reset_shared_service_catalog_client_for_tests()
    factory._service_catalog_op_stats.clear()

    make_catalog_reader().by_doc_id("a")

    stats = factory.service_catalog_op_stats()
    assert "by_doc_id" in stats, "ops must be recorded under their own name"
    assert stats["by_doc_id"]["calls"] == 1

    factory.reset_shared_service_catalog_client_for_tests()
    factory._service_catalog_op_stats.clear()


def test_call_releases_lock_before_network_round_trip(monkeypatch) -> None:
    """nexus-u2u0n: ``_service_catalog_lock`` narrows to the client
    RESOLUTION only — it must not span the forwarded call's own network
    round trip. Proven with a 3-party barrier inside the round trip: under
    the OLD pre-narrowing behavior (lock held across the whole call) only
    one thread at a time could ever reach the barrier, and this test would
    time out via ``BrokenBarrierError`` rather than pass. Replaces the
    pre-narrowing ``test_op_stats_split_lock_wait_from_call``, which
    asserted the opposite (``lock_wait_s > 0.02`` under 3 serialized 0.05s
    calls) — pinning exactly the convoy this narrowing exists to remove.

    Also asserts ``lock_wait_s`` stays small regardless of how long the
    barrier rendezvous takes: the resolve step is in-process and released
    BEFORE the call (and its barrier wait) begins, so the two are no
    longer coupled.
    """
    import threading

    import nexus.catalog.factory as factory
    from nexus.catalog.factory import make_catalog_reader

    constructed: list = []
    barrier = threading.Barrier(3, timeout=5)

    class _ConcurrentClient:
        def __init__(self, *_a, **_kw) -> None:
            constructed.append(self)
            self.closed = False

        def close(self) -> None:
            self.closed = True

        def by_doc_id(self, doc_id: str) -> str:
            barrier.wait()  # only satisfied if all 3 calls are in flight together
            return f"doc:{doc_id}"

    monkeypatch.setattr(
        "nexus.catalog.http_catalog_client.HttpCatalogClient", _ConcurrentClient)
    factory.reset_shared_service_catalog_client_for_tests()
    factory._service_catalog_op_stats.clear()

    errors: list[Exception] = []

    def _call() -> None:
        try:
            make_catalog_reader().by_doc_id("a")
        except Exception as exc:  # noqa: BLE001 — captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=_call) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, (
        f"calls did not overlap within the barrier timeout — the lock is "
        f"still spanning the round trip: {errors}"
    )
    assert len(constructed) == 1

    stats = factory.service_catalog_op_stats()
    row = stats["by_doc_id"]
    assert row["calls"] == 3
    assert row["lock_wait_s"] < 1.0, (
        "lock wait must stay small — the barrier's rendezvous delay must "
        "never be attributable to the RESOLUTION lock, only to the "
        "forwarded call itself, which now runs outside it"
    )

    factory.reset_shared_service_catalog_client_for_tests()
    factory._service_catalog_op_stats.clear()


def test_concurrent_double_failure_does_not_double_close(monkeypatch) -> None:
    """T5(iii): two threads resolve the SAME client instance and both fail
    concurrently. Exactly one must win the CAS eviction and close it; the
    other must observe the slot already cleared and do nothing further —
    never a second ``close()`` on the same instance, never an eviction of
    whatever instance a third party has already rebuilt in its place."""
    import threading

    import nexus.catalog.factory as factory
    from nexus.catalog.factory import make_catalog_reader

    close_calls: list[int] = []
    barrier = threading.Barrier(2, timeout=5)

    class _FlakyClient:
        def __init__(self, *_a, **_kw) -> None:
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1
            close_calls.append(id(self))

        def by_doc_id(self, doc_id: str) -> str:
            barrier.wait()  # both threads fail together, same resolved instance
            raise ConnectionError("stale lease")

    monkeypatch.setattr("nexus.catalog.http_catalog_client.HttpCatalogClient", _FlakyClient)
    factory.reset_shared_service_catalog_client_for_tests()

    errors: list[Exception] = []

    def _call() -> None:
        try:
            make_catalog_reader().by_doc_id("x")
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
    assert factory._service_catalog_client is None

    factory.reset_shared_service_catalog_client_for_tests()


# ── nexus-0dpli: eviction must not abort a healthy in-flight sibling ────────


def test_domain_exception_does_not_evict(monkeypatch) -> None:
    """nexus-0dpli: a routine, documented business refusal
    (``IndexRunVerifyRefused``, the fence's own designed 409-shaped
    outcome) must propagate WITHOUT touching the shared client at all —
    eviction is reserved for genuine connectivity failures. Over-evicting
    on a routine outcome would abort every OTHER in-flight sibling call
    sharing the same instance for no reason."""
    import nexus.catalog.factory as factory
    from nexus.catalog.factory import make_catalog_reader
    from nexus.errors import IndexRunVerifyRefused

    close_calls: list[int] = []

    class _Client:
        def __init__(self, *_a, **_kw) -> None:
            pass

        def close(self) -> None:
            close_calls.append(1)

        def complete_index_run(self, doc_id: str) -> None:
            raise IndexRunVerifyRefused(
                doc_id=doc_id, referenced=3, present=2, missing=1, chunk_count=3,
            )

    monkeypatch.setattr("nexus.catalog.http_catalog_client.HttpCatalogClient", _Client)
    factory.reset_shared_service_catalog_client_for_tests()

    with pytest.raises(IndexRunVerifyRefused):
        make_catalog_reader().complete_index_run("doc-1")

    assert close_calls == [], (
        "a routine domain exception must never evict the shared client"
    )
    assert factory._service_catalog_client is not None, (
        "the still-healthy instance must remain installed for the next caller"
    )

    factory.reset_shared_service_catalog_client_for_tests()


def test_generic_exception_does_not_evict(monkeypatch) -> None:
    """Same as above for a plain, non-httpx exception (e.g. a ``ValueError``
    from client-side validation) — the trigger is connectivity-class only,
    not "any Exception"."""
    import nexus.catalog.factory as factory
    from nexus.catalog.factory import make_catalog_reader

    close_calls: list[int] = []

    class _Client:
        def __init__(self, *_a, **_kw) -> None:
            pass

        def close(self) -> None:
            close_calls.append(1)

        def by_doc_id(self, doc_id: str) -> str:
            raise ValueError("bad doc_id")

    monkeypatch.setattr("nexus.catalog.http_catalog_client.HttpCatalogClient", _Client)
    factory.reset_shared_service_catalog_client_for_tests()

    with pytest.raises(ValueError):
        make_catalog_reader().by_doc_id("x")

    assert close_calls == []
    assert factory._service_catalog_client is not None

    factory.reset_shared_service_catalog_client_for_tests()


def test_in_flight_sibling_survives_eviction_close_deferred_until_it_exits(
    monkeypatch,
) -> None:
    """nexus-0dpli, the CRITICAL fix: thread A is genuinely mid-call
    (parked on a rendezvous inside the transport double) sharing the
    SAME resolved instance as thread B, which fails with a genuine
    connectivity error and triggers eviction. Must hold ALL THREE
    properties:

    (i)   A's in-flight call completes successfully against its
          already-resolved instance — eviction never closes it out from
          under A (the empirically-reproduced use-after-close this bead
          exists to fix).
    (ii)  A caller resolving AFTER the eviction gets a FRESH instance
          (the slot is cleared immediately, not only once draining
          finishes).
    (iii) The evicted instance is closed EXACTLY ONCE, and only once A
          has released its own reference (i.e. after A exits) — never
          by B (who is not the last holder), never twice.
    """
    import threading

    import httpx

    import nexus.catalog.factory as factory
    from nexus.catalog.factory import make_catalog_reader

    constructed: list[int] = []
    close_calls: list[int] = []
    a_parked = threading.Event()
    release_a = threading.Event()

    class _Client:
        def __init__(self, *_a, **_kw) -> None:
            constructed.append(len(constructed) + 1)
            self.instance_id = len(constructed)

        def close(self) -> None:
            close_calls.append(self.instance_id)

        def by_doc_id(self, doc_id: str) -> str:
            # Thread A's call: park until told to continue, so it is
            # genuinely in-flight while B's eviction runs concurrently.
            a_parked.set()
            release_a.wait(5)
            return f"doc:{doc_id}"

        def complete_index_run(self, doc_id: str) -> None:
            # Thread B's call: a genuine connectivity failure.
            raise httpx.ConnectError("connection refused")

        def ping(self) -> str:
            return "pong"

    monkeypatch.setattr("nexus.catalog.http_catalog_client.HttpCatalogClient", _Client)
    factory.reset_shared_service_catalog_client_for_tests()

    a_result: dict = {}

    def thread_a() -> None:
        try:
            a_result["value"] = make_catalog_reader().by_doc_id("a")
        except Exception as exc:  # noqa: BLE001 — captured for the assertion below
            a_result["error"] = exc

    ta = threading.Thread(target=thread_a)
    ta.start()
    assert a_parked.wait(5), "thread A never entered its call"

    # Thread B resolves the SAME instance A is using (nothing has evicted
    # it yet) and fails with a connectivity error, triggering eviction.
    with pytest.raises(httpx.ConnectError):
        make_catalog_reader().complete_index_run("doc-2")

    # (iii) part 1: not closed yet — A is still in flight.
    assert close_calls == [], (
        "the shared instance was closed while a healthy sibling was still "
        "mid-call — this is the use-after-close bug nexus-0dpli fixes"
    )
    # (ii): the slot is cleared immediately, regardless of drainage.
    assert factory._service_catalog_client is None, (
        "the shared slot must be cleared immediately so new callers never "
        "resolve the doomed instance"
    )

    # (ii) continued: a caller arriving now must get a FRESH instance, not
    # A's (still in-flight, evicted-but-not-yet-closed) one.
    assert make_catalog_reader().ping() == "pong"
    assert len(constructed) == 2, "a post-eviction caller must build fresh"

    # Let A finish. Its call must succeed — (i).
    release_a.set()
    ta.join(5)
    assert a_result.get("value") == "doc:a", (
        f"thread A's in-flight call was aborted: {a_result}"
    )

    # (iii) part 2: NOW the old (instance 1) is closed, exactly once, and
    # the fresh instance (2) was never touched.
    assert close_calls == [1], (
        f"expected instance 1 closed exactly once after A exited, got {close_calls}"
    )

    factory.reset_shared_service_catalog_client_for_tests()
