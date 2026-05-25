# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-128 P1 (nexus-kg8sj): route indexer T2 writes through the daemon.

`t2_index_write` funnels an indexer T2 write through the T2 daemon
(`T2Client`) when it is reachable, so the `nx index repo` process never
opens `memory.db` directly and cannot strand its single WAL writer slot.
When the daemon is unreachable it falls back to a direct `T2Database` so
indexing still works (degraded, logged).

Reachability is decided by an up-front `database.hello()` probe (not by
catching an error out of `write_fn`, which some best-effort writers
swallow). `write_fn` runs against exactly one writer — never re-run — so
there is no double-write risk.
"""
from __future__ import annotations

from pathlib import Path

import pytest


class _FakeDatabaseProxy:
    """Stand-in for ``T2Client.database`` with a ``hello()`` probe."""

    def __init__(self, reachable: bool) -> None:
        self._reachable = reachable

    def hello(self) -> dict:
        if not self._reachable:
            from nexus.daemon.t2_client import T2DaemonNotReachableError
            raise T2DaemonNotReachableError("daemon down")
        return {"daemon_schema_version": "9.9.9"}


def test_routes_through_daemon_client_when_reachable(monkeypatch) -> None:
    import nexus.daemon.t2_client as t2c

    class _FakeClient:
        def __init__(self) -> None:
            self.closed = False
            self.database = _FakeDatabaseProxy(reachable=True)

        def close(self) -> None:
            self.closed = True

    fake = _FakeClient()
    monkeypatch.setattr(t2c, "make_t2_client", lambda **_kw: fake)

    # If we fell through to the direct path, this would blow up the test.
    import nexus.mcp_infra as mi
    monkeypatch.setattr(
        mi, "default_db_path",
        lambda: pytest.fail("must not open a direct T2Database when daemon is reachable"),
    )

    from nexus.mcp_infra import t2_index_write

    received: list[object] = []
    t2_index_write(received.append)

    assert received == [fake], "write_fn must run against the daemon client"
    assert fake.closed is True, "client must be closed after the write"


def test_falls_back_to_direct_t2database_when_unreachable(
    monkeypatch, tmp_path: Path,
) -> None:
    import nexus.daemon.t2_client as t2c

    class _DeadClient:
        def __init__(self) -> None:
            self.database = _FakeDatabaseProxy(reachable=False)
            self.closed = False

        def close(self) -> None:
            self.closed = True

    dead = _DeadClient()
    monkeypatch.setattr(t2c, "make_t2_client", lambda **_kw: dead)

    import nexus.mcp_infra as mi
    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(mi, "default_db_path", lambda: db_path)

    from nexus.mcp_infra import t2_index_write

    seen_types: list[str] = []

    def _write(db) -> None:  # noqa: ANN001
        seen_types.append(type(db).__name__)
        # Single store call; runs only on the direct writer here.
        db.chash_index.upsert_many(chashes=["x1", "x2"], collection="code__c")

    t2_index_write(_write)

    # Probe failed → write_fn ran ONCE, against the direct T2Database only.
    assert seen_types == ["T2Database"]
    assert dead.closed is True, "unreachable client must be closed after probe"

    # And the fallback write actually landed.
    from nexus.db.t2 import T2Database

    with T2Database(db_path) as db:
        assert db.chash_index.lookup("x1"), "fallback write did not persist"
        assert db.chash_index.lookup("x2")


def test_non_unreachable_error_propagates_no_fallback(
    monkeypatch, tmp_path: Path,
) -> None:
    """A daemon-side error that is NOT unreachability must propagate, not
    silently fall back to a direct write (which could double-write or mask
    a real bug)."""
    import nexus.daemon.t2_client as t2c

    class _Client:
        def __init__(self) -> None:
            self.database = _FakeDatabaseProxy(reachable=True)

        def close(self) -> None:
            pass

    monkeypatch.setattr(t2c, "make_t2_client", lambda **_kw: _Client())

    import nexus.mcp_infra as mi
    monkeypatch.setattr(
        mi, "default_db_path",
        lambda: pytest.fail("must not fall back on a non-unreachable error"),
    )

    from nexus.mcp_infra import t2_index_write

    def _write(db) -> None:  # noqa: ANN001
        raise RuntimeError("daemon-side RPC error")

    with pytest.raises(RuntimeError, match="daemon-side RPC error"):
        t2_index_write(_write)


def test_dual_write_calls_upsert_many_exactly_once() -> None:
    """The batch dual-write must be ONE store call (one daemon RPC when
    routed), not one per chunk — pins the batching, not just end-state."""
    from nexus.db.t2.chash_index import dual_write_chash_index

    class _SpyChash:
        def __init__(self) -> None:
            self.many = 0
            self.single = 0
            self.seen: tuple | None = None

        def upsert_many(self, *, chashes, collection) -> None:  # noqa: ANN001
            self.many += 1
            self.seen = (list(chashes), collection)

        def upsert(self, *, chash, collection) -> None:  # noqa: ANN001
            self.single += 1

    spy = _SpyChash()
    metas = [{"chunk_text_hash": f"h{i}"} for i in range(5)]
    dual_write_chash_index(spy, "code__c", [f"d{i}" for i in range(5)], metas)

    assert spy.many == 1, "must batch into a single upsert_many call"
    assert spy.single == 0, "must NOT loop per-row upsert"
    assert spy.seen == (["h0", "h1", "h2", "h3", "h4"], "code__c")


def test_chash_hook_delegates_to_t2_index_write(monkeypatch) -> None:
    """chash_dual_write_batch_hook routes through t2_index_write (the daemon
    path), not a raw t2_ctx/T2Database open."""
    import nexus.mcp_infra as mi

    calls: list[object] = []
    monkeypatch.setattr(mi, "t2_index_write", lambda write_fn: calls.append(write_fn))
    monkeypatch.setattr(
        mi, "t2_ctx",
        lambda: pytest.fail("hook must route via t2_index_write, not t2_ctx"),
    )

    mi.chash_dual_write_batch_hook(
        doc_ids=["d1"],
        collection="code__c",
        contents=["x"],
        embeddings=None,
        metadatas=[{"chunk_text_hash": "h1"}],
    )

    assert len(calls) == 1, "chash hook must delegate exactly one write to t2_index_write"
