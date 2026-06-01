# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-141 P1 (RED): version-skew double-writer at the t2_index_write A1 boundary.

`t2_index_write` decides reachability with an up-front `database.hello()`
probe. The probe can fail two ways, with OPPOSITE single-writer implications:

* `T2DaemonNotReachableError` — no daemon writer exists. Degrading to a
  direct `T2Database` is safe (the RDR-128 documented availability fallback).
* `T2SchemaVersionMismatchError` — a stale-version daemon is ALIVE, holds the
  spawn lock, and is actively serving writes. Opening a direct `T2Database`
  here produces a SECOND live writer on `memory.db` — the version-skew
  double-writer this RDR closes.

The fix (RDR-141): split the conflated `except`. The unreachable arm is
unchanged; the version-mismatch arm RE-ASSERTS the supervisor (reap the stale
daemon, spawn a current one) via `mcp_infra._reassert_t2_daemon` and retries
through the fresh daemon, falling back to a direct write only when the
re-assert cannot produce a reachable current daemon.

These tests are RED against the conflated `except` (which opens a direct
writer on mismatch and has no `_reassert_t2_daemon` seam) and GREEN once P2
splits the arms.
"""
from __future__ import annotations

from pathlib import Path

import pytest


class _MismatchThenOkProxy:
    """`database` proxy: first `hello()` raises version-mismatch, then OK.

    Models the stale daemon being reaped + a current daemon spawned: the
    second client (post-re-assert) probes a healthy current daemon.
    """

    def __init__(self) -> None:
        self.calls = 0

    def hello(self) -> dict:
        self.calls += 1
        if self.calls == 1:
            from nexus.daemon.t2_client import T2SchemaVersionMismatchError

            raise T2SchemaVersionMismatchError(
                client_version="5.6.0", daemon_version="5.4.4"
            )
        return {"daemon_schema_version": "9.9.9"}


def _no_direct_open(mi, monkeypatch) -> None:
    """Fail the test if the direct-T2Database fallback path is taken."""
    monkeypatch.setattr(
        mi,
        "default_db_path",
        lambda: pytest.fail(
            "version-skew double-writer: opened a direct T2Database while a "
            "stale daemon is alive — the re-assert path must be taken instead"
        ),
    )


def test_version_mismatch_reasserts_supervisor_not_direct_writer(
    monkeypatch, tmp_path: Path,
) -> None:
    """On schema-version mismatch, t2_index_write re-asserts the supervisor
    and routes through the fresh daemon — it must NOT open a direct writer
    while the stale daemon is alive."""
    import nexus.daemon.t2_client as t2c
    import nexus.mcp_infra as mi

    proxy = _MismatchThenOkProxy()

    class _Store:
        def write(self) -> str:
            return "wrote-through-daemon"

    class _Client:
        def __init__(self) -> None:
            self.database = proxy
            self.store = _Store()
            self.closed = False

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(t2c, "make_t2_client", lambda **_kw: _Client())

    # The re-assert seam: simulate a successful reap+spawn of a current daemon.
    reassert_calls: list[str] = []

    def _fake_reassert() -> str:
        reassert_calls.append("called")
        return "reachable"

    monkeypatch.setattr(mi, "_reassert_t2_daemon", _fake_reassert, raising=False)

    # If the mismatch arm degrades to a direct writer, this fails the test.
    _no_direct_open(mi, monkeypatch)

    from nexus.mcp_infra import t2_index_write

    result = t2_index_write(lambda db: db.store.write())

    assert reassert_calls == ["called"], "must re-assert the supervisor on version mismatch"
    assert proxy.calls == 2, "must re-probe the fresh daemon after re-assert"
    assert result == "wrote-through-daemon", "write must route through the current daemon"


def test_unreachable_arm_still_degrades_to_direct(
    monkeypatch, tmp_path: Path,
) -> None:
    """The daemon-unreachable arm is unchanged: degrade to a direct
    T2Database and never invoke the supervisor re-assert."""
    import nexus.daemon.t2_client as t2c
    import nexus.mcp_infra as mi

    class _DeadProxy:
        def hello(self) -> dict:
            from nexus.daemon.t2_client import T2DaemonNotReachableError

            raise T2DaemonNotReachableError("daemon down")

    class _DeadClient:
        def __init__(self) -> None:
            self.database = _DeadProxy()
            self.closed = False

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(t2c, "make_t2_client", lambda **_kw: _DeadClient())

    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(mi, "default_db_path", lambda: db_path)

    reassert_calls: list[str] = []
    monkeypatch.setattr(
        mi,
        "_reassert_t2_daemon",
        lambda: reassert_calls.append("called") or "reachable",
        raising=False,
    )

    from nexus.mcp_infra import t2_index_write

    seen: list[object] = []

    def _write(db) -> str:  # noqa: ANN001
        seen.append(type(db).__name__)
        return "direct-write-ok"

    result = t2_index_write(_write)

    assert reassert_calls == [], "unreachable arm must NOT re-assert the supervisor"
    assert result == "direct-write-ok"
    assert seen == ["T2Database"], "unreachable arm must write through a direct T2Database"
