# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-128 P3 (nexus-sbxbe.3) routing enablers.

Three small substrate additions let the remaining writers route through
the daemon:

* ``t2_index_write`` now RETURNS ``write_fn``'s result, so writers that
  need the value (aspect_worker ``claim_batch`` rows, ``rename_collection_
  cascade`` counts) can route, not just fire-and-forget writers.
* ``T2Client`` grows facade-parity passthroughs (``expire``,
  ``rename_collection_cascade``) so a ``write_fn`` body calls them
  uniformly on a routed client or a direct ``T2Database``.
* The daemon dispatches ``database.expire`` so the SessionEnd flush hook
  reaches the daemon instead of opening ``memory.db`` directly.
"""
from __future__ import annotations

from pathlib import Path

import pytest


class _FakeDatabaseProxy:
    def __init__(self, reachable: bool) -> None:
        self._reachable = reachable

    def hello(self) -> dict:
        if not self._reachable:
            from nexus.daemon.t2_client import T2DaemonNotReachableError
            raise T2DaemonNotReachableError("daemon down")
        return {"daemon_schema_version": "9.9.9"}


def test_t2_index_write_returns_value_routed(monkeypatch) -> None:
    """The routed path returns write_fn's result (RDR-128 P3)."""
    import nexus.daemon.t2_client as t2c

    class _FakeClient:
        def __init__(self) -> None:
            self.database = _FakeDatabaseProxy(reachable=True)
            self.closed = False

        def close(self) -> None:
            self.closed = True

    fake = _FakeClient()
    monkeypatch.setattr(t2c, "make_t2_client", lambda **_kw: fake)
    import nexus.mcp_infra as mi
    monkeypatch.setattr(
        mi, "default_db_path",
        lambda: pytest.fail("must not open a direct T2Database when reachable"),
    )

    from nexus.mcp_infra import t2_index_write

    result = t2_index_write(lambda db: ["row1", "row2"])
    assert result == ["row1", "row2"], "routed path must return write_fn's value"
    assert fake.closed is True


def test_t2_index_write_returns_value_fallback(monkeypatch, tmp_path: Path) -> None:
    """The direct-fallback path also returns write_fn's result."""
    import nexus.daemon.t2_client as t2c

    class _DeadClient:
        def __init__(self) -> None:
            self.database = _FakeDatabaseProxy(reachable=False)

        def close(self) -> None:
            pass

    monkeypatch.setattr(t2c, "make_t2_client", lambda **_kw: _DeadClient())
    import nexus.mcp_infra as mi
    monkeypatch.setattr(mi, "default_db_path", lambda: tmp_path / "memory.db")

    from nexus.mcp_infra import t2_index_write

    result = t2_index_write(lambda db: type(db).__name__)
    assert result == "T2Database", "fallback path must return write_fn's value"


def test_t2client_expire_forwards_to_database_op() -> None:
    """T2Client.expire forwards to the ``database.expire`` RPC op."""
    from nexus.daemon.t2_client import T2Client

    client = T2Client(skip_handshake=True)
    captured: list[tuple] = []
    client.call = lambda op, *a, **kw: captured.append((op, a, kw)) or 7  # type: ignore[assignment]

    result = client.expire(90)
    assert captured == [("database.expire", (90,), {})]
    assert result == 7


def test_t2client_rename_cascade_forwards_to_database_op() -> None:
    """T2Client.rename_collection_cascade forwards to the database op."""
    from nexus.daemon.t2_client import T2Client

    client = T2Client(skip_handshake=True)
    captured: list[tuple] = []
    client.call = lambda op, *a, **kw: captured.append((op, a, kw)) or {"memory": 3}  # type: ignore[assignment]

    result = client.rename_collection_cascade(old="a", new="b")
    assert captured == [("database.rename_collection_cascade", (), {"old": "a", "new": "b"})]
    assert result == {"memory": 3}


def test_daemon_dispatch_table_includes_database_expire(tmp_path: Path) -> None:
    """The daemon dispatches ``database.expire`` (RDR-128 P3) so the
    SessionEnd flush can route its TTL sweep."""
    from nexus.daemon.t2_daemon import _build_dispatch_table
    from nexus.db.t2 import T2Database

    with T2Database(tmp_path / "memory.db") as db:
        table = _build_dispatch_table(db)

    assert "database.expire" in table, (
        "expire must be dispatchable so session_end can route it"
    )
    assert "database.rename_collection_cascade" in table
    assert "database.hello" in table
