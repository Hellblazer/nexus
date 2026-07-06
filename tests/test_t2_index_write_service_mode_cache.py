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
