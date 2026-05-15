# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for admin-RPC UDS-only enforcement — RDR-112 P1.6 (nexus-pce1.1).

Admin ops (those in ``_ADMIN_OPS``) must be rejected over TCP with a
``PermissionDenied`` error frame.  Only UDS connections may invoke them.

Covers:
  (a) Admin op ``admin_ping`` succeeds over UDS.
  (b) Admin op ``admin_ping`` is rejected over TCP with PermissionDenied
      error frame containing the op name.
  (c) Non-admin op ``memory.put`` + ``memory.get`` round-trip succeeds
      over both UDS and TCP.

All tests use port=0, tmp_path config_dir, real daemon (no mocks).
The ``admin_ping`` op is a test-scaffold registered in the dispatch table
when ``T2Daemon`` is constructed with ``enable_admin_ping=True``.
Production code never sets this flag; it exists only to give the test
suite an exercisable admin op before any real admin ops (``subspace_add``,
``apply_pending_migrations``) land in the dispatch table.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from nexus.daemon.t2_daemon import (
    DAEMON_PROTOCOL_VERSION,
    T2Daemon,
    _ADMIN_OPS,
    read_frame,
    write_frame,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _connect_uds(
    uds_path: Path,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_unix_connection(str(uds_path))


async def _connect_tcp(
    host: str, port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(host, port)


async def _handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    version: str = DAEMON_PROTOCOL_VERSION,
) -> dict[str, Any]:
    write_frame(writer, {"op": "hello", "protocol_version": version})
    await writer.drain()
    return await read_frame(reader)


async def _rpc(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    op: str,
    args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    write_frame(writer, {"op": op, "args": args or {}})
    await writer.drain()
    return await read_frame(reader)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    d = tmp_path / "config" / "nexus"
    d.mkdir(parents=True)
    return d


@pytest_asyncio.fixture()
async def admin_daemon(config_dir: Path):
    """Real daemon with admin_ping scaffold enabled; no T2Database (transport only)."""
    daemon = T2Daemon(config_dir=config_dir, enable_admin_ping=True)
    await daemon.start()
    yield daemon
    await daemon.stop()


# ---------------------------------------------------------------------------
# Sanity: _ADMIN_OPS includes admin_ping
# ---------------------------------------------------------------------------


def test_admin_ops_contains_admin_ping() -> None:
    """admin_ping must be in _ADMIN_OPS so dispatch-gate tests are meaningful."""
    assert "admin_ping" in _ADMIN_OPS


# ---------------------------------------------------------------------------
# (a) Admin op succeeds over UDS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_op_allowed_over_uds(admin_daemon: T2Daemon) -> None:
    """admin_ping succeeds when called over a UDS connection."""
    reader, writer = await _connect_uds(admin_daemon.uds_path)
    try:
        ack = await _handshake(reader, writer)
        assert ack.get("op") == "hello_ack"

        resp = await _rpc(reader, writer, op="admin_ping")
        assert "error" not in resp, f"Unexpected error: {resp}"
        assert resp.get("result") == {"ok": True}
    finally:
        writer.close()
        await writer.wait_closed()


# ---------------------------------------------------------------------------
# (b) Admin op rejected over TCP with PermissionDenied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_op_rejected_over_tcp(admin_daemon: T2Daemon) -> None:
    """admin_ping is rejected over TCP with a PermissionDenied error frame."""
    reader, writer = await _connect_tcp("127.0.0.1", admin_daemon.tcp_port)
    try:
        ack = await _handshake(reader, writer)
        assert ack.get("op") == "hello_ack"

        resp = await _rpc(reader, writer, op="admin_ping")
        assert "error" in resp, f"Expected error frame, got: {resp}"

        err = resp["error"]
        # Error frame shape: {"type": "PermissionDenied", "message": "..."}
        assert isinstance(err, dict), f"Error must be a dict, got: {type(err)}"
        assert err.get("type") == "PermissionDenied", (
            f"Expected type='PermissionDenied', got {err.get('type')!r}"
        )
        assert "admin_ping" in err.get("message", ""), (
            f"Op name 'admin_ping' must appear in error message: {err.get('message')!r}"
        )
        assert "UDS" in err.get("message", "") or "uds" in err.get("message", "").lower(), (
            f"Message must mention UDS transport: {err.get('message')!r}"
        )
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_admin_op_rejection_does_not_close_connection(admin_daemon: T2Daemon) -> None:
    """Rejecting an admin op over TCP leaves the connection open for further RPCs."""
    reader, writer = await _connect_tcp("127.0.0.1", admin_daemon.tcp_port)
    try:
        ack = await _handshake(reader, writer)
        assert ack.get("op") == "hello_ack"

        # Admin op rejected
        resp1 = await _rpc(reader, writer, op="admin_ping")
        assert "error" in resp1

        # Connection still alive — ping still works
        resp2 = await _rpc(reader, writer, op="ping")
        assert resp2.get("pong") is True
    finally:
        writer.close()
        await writer.wait_closed()


# ---------------------------------------------------------------------------
# (c) Non-admin op succeeds over both UDS and TCP
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def rpc_daemon(config_dir: Path):
    """Daemon with a real T2Database for memory.put / memory.get round-trip."""
    from nexus.db.t2 import T2Database

    db_path = config_dir / "memory.db"
    t2db = T2Database(db_path)
    daemon = T2Daemon(config_dir=config_dir, t2db=t2db, enable_admin_ping=True)
    await daemon.start()
    yield daemon
    await daemon.stop()
    t2db.close()


@pytest.mark.asyncio
async def test_non_admin_op_uds_roundtrip(rpc_daemon: T2Daemon) -> None:
    """memory.put + memory.get round-trip succeeds over UDS."""
    reader, writer = await _connect_uds(rpc_daemon.uds_path)
    try:
        await _handshake(reader, writer)

        put_resp = await _rpc(
            reader,
            writer,
            op="memory.put",
            args={"content": "hello uds", "project": "test", "title": "t"},
        )
        assert "error" not in put_resp, f"put error: {put_resp}"

        get_resp = await _rpc(
            reader,
            writer,
            op="memory.get",
            args={"project": "test", "title": "t"},
        )
        assert "error" not in get_resp, f"get error: {get_resp}"
        result = get_resp.get("result")
        assert result is not None
        assert "hello uds" in str(result)
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_non_admin_op_tcp_roundtrip(rpc_daemon: T2Daemon) -> None:
    """memory.put + memory.get round-trip succeeds over TCP."""
    reader, writer = await _connect_tcp("127.0.0.1", rpc_daemon.tcp_port)
    try:
        await _handshake(reader, writer)

        put_resp = await _rpc(
            reader,
            writer,
            op="memory.put",
            args={"content": "hello tcp", "project": "test", "title": "t2"},
        )
        assert "error" not in put_resp, f"put error: {put_resp}"

        get_resp = await _rpc(
            reader,
            writer,
            op="memory.get",
            args={"project": "test", "title": "t2"},
        )
        assert "error" not in get_resp, f"get error: {get_resp}"
        result = get_resp.get("result")
        assert result is not None
        assert "hello tcp" in str(result)
    finally:
        writer.close()
        await writer.wait_closed()


# ---------------------------------------------------------------------------
# (d) Future admin op (in _ADMIN_OPS but NOT in dispatch table)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_future_admin_op_rejected_over_tcp(rpc_daemon: T2Daemon) -> None:
    """A name listed in _ADMIN_OPS but absent from the dispatch table is
    still rejected over TCP — the gate must fire before dispatch-lookup,
    so a malicious peer cannot probe "this op exists" vs "permission denied"
    by transport.
    """
    reader, writer = await _connect_tcp("127.0.0.1", rpc_daemon.tcp_port)
    try:
        await _handshake(reader, writer)
        # subspace_add is in _ADMIN_OPS (forward-looking) but not in the
        # dispatch table yet (ships with nexus-x98k).
        resp = await _rpc(reader, writer, op="subspace_add", args={"yaml": "stub"})
        assert "error" in resp
        err = resp["error"]
        if isinstance(err, dict):
            assert err.get("type") == "PermissionDenied"
            assert "subspace_add" in err.get("message", "")
        else:
            # Some daemons may flatten error frames; accept either shape but
            # require the gate's signal in the message.
            assert "PermissionDenied" in str(err) or "requires UDS" in str(err)
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_future_admin_op_over_uds_is_unknown_op(rpc_daemon: T2Daemon) -> None:
    """The same future admin op over UDS clears the gate and falls through
    to the unknown-op fallback — confirming the gate is strictly transport-based,
    not table-presence-based.
    """
    reader, writer = await _connect_uds(rpc_daemon.uds_path)
    try:
        await _handshake(reader, writer)
        resp = await _rpc(reader, writer, op="subspace_add", args={"yaml": "stub"})
        assert "error" in resp
        err = resp["error"]
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
        # Must NOT be PermissionDenied on UDS
        assert "PermissionDenied" not in (err.get("type", "") if isinstance(err, dict) else "")
        assert "permission" not in msg.lower() or "unknown" in msg.lower()
    finally:
        writer.close()
        await writer.wait_closed()
