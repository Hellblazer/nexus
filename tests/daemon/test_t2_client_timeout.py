# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for T2Client RPC timeout (RDR-114 Step 4, nexus-wcs9).

RDR-114 Phase 1 Step 4 wires a per-RPC socket timeout on T2Client so a
hung daemon (UDS accepts but never replies) cannot stall a bridge
subprocess past Claude Code's hook deadline. The timeout fires on the
recv path and is translated to a typed ``RpcTimeoutError`` that is
deliberately NOT a subclass of ``ConnectionRefusedError`` or any
``OSError`` so the reconnect wrapper in Step 1 (nexus-wfko) can
distinguish "daemon hung" from "daemon gone."
"""
from __future__ import annotations

import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from nexus.daemon.t2_client import RpcTimeoutError, T2Client


@pytest.fixture
def short_sock_dir():
    """Yield a tempdir on a short path so UDS bind does not hit the
    macOS ~104-char path limit. ``tmp_path`` lives under
    ``/private/var/.../pytest-of-<user>/...`` which is already 80+
    chars before the test name is appended.
    """
    with tempfile.TemporaryDirectory(prefix="t114-", dir="/tmp") as d:
        yield Path(d)


# ---------------------------------------------------------------------------
# Constructor wiring
# ---------------------------------------------------------------------------


def test_default_rpc_timeout_is_5_seconds(tmp_path: Path) -> None:
    """Unset ``rpc_timeout_seconds`` defaults to 5.0."""
    client = T2Client(uds_path=tmp_path / "unused.sock")
    assert client._rpc_timeout_seconds == 5.0


def test_explicit_rpc_timeout_propagates(tmp_path: Path) -> None:
    """``T2Client(rpc_timeout_seconds=N)`` stores N verbatim."""
    client = T2Client(uds_path=tmp_path / "unused.sock", rpc_timeout_seconds=2.0)
    assert client._rpc_timeout_seconds == 2.0


# ---------------------------------------------------------------------------
# Exception hierarchy (the discriminant-preservation contract)
# ---------------------------------------------------------------------------


def test_rpc_timeout_error_is_not_a_connection_refused_error() -> None:
    """RpcTimeoutError must not be confusable with ConnectionRefusedError.

    Step 1's reconnect wrapper distinguishes hung-daemon (transient
    retry) from gone-daemon (re-discover) by exception class. Collapsing
    them into the same OSError branch would defeat the discriminant.
    """
    assert not issubclass(RpcTimeoutError, ConnectionRefusedError)


def test_rpc_timeout_error_is_not_an_oserror() -> None:
    """Defense in depth: any caller that catches OSError today should NOT
    accidentally swallow a timeout."""
    assert not issubclass(RpcTimeoutError, OSError)


def test_rpc_timeout_error_is_a_standalone_exception_class() -> None:
    """RpcTimeoutError sits in its own hierarchy under Exception."""
    assert issubclass(RpcTimeoutError, Exception)
    assert RpcTimeoutError is not Exception


def test_rpc_timeout_error_is_publicly_exported() -> None:
    """``from nexus.daemon.t2_client import RpcTimeoutError`` must work."""
    from nexus.daemon import t2_client

    assert hasattr(t2_client, "RpcTimeoutError")
    assert t2_client.RpcTimeoutError is RpcTimeoutError


# ---------------------------------------------------------------------------
# Behavioural contract: hung daemon trips the timeout on the recv path
# ---------------------------------------------------------------------------


def test_hung_daemon_raises_rpc_timeout_error(short_sock_dir: Path) -> None:
    """A fake daemon that accepts UDS but never sends hello_ack must
    cause the client to raise ``RpcTimeoutError`` after the configured
    timeout elapses.

    Uses a deliberately small timeout (0.4 s) so the test stays fast
    while still being well above the threading scheduling jitter floor.
    """
    sock_path = short_sock_dir / "hung.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)

    accepted: list[socket.socket] = []
    accept_done = threading.Event()

    def _accept_and_hold() -> None:
        try:
            c, _ = server.accept()
            accepted.append(c)
            accept_done.set()
            # Hold the connection open; never reply. The client should
            # time out on the hello_ack read.
            time.sleep(5.0)
        except OSError:
            pass

    t = threading.Thread(target=_accept_and_hold, daemon=True)
    t.start()

    client = T2Client(uds_path=sock_path, rpc_timeout_seconds=0.4)
    start = time.perf_counter()
    try:
        with pytest.raises(RpcTimeoutError):
            client.call("ping")
        elapsed = time.perf_counter() - start
        # Timeout fired roughly when expected. Generous upper bound for
        # CI scheduling jitter; floor confirms the timeout isn't 0.
        assert 0.3 <= elapsed <= 2.5, f"elapsed={elapsed:.3f}s"
    finally:
        for c in accepted:
            c.close()
        server.close()
        client.close()


def test_hung_daemon_timeout_does_not_collapse_into_connection_refused(
    short_sock_dir: Path,
) -> None:
    """The same hung-daemon scenario must NOT raise ConnectionRefusedError.

    Pins the discriminant: a caller's ``except ConnectionRefusedError``
    block must not silently catch a timeout.
    """
    sock_path = short_sock_dir / "hung2.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)

    accepted: list[socket.socket] = []

    def _accept_and_hold() -> None:
        try:
            c, _ = server.accept()
            accepted.append(c)
            time.sleep(5.0)
        except OSError:
            pass

    t = threading.Thread(target=_accept_and_hold, daemon=True)
    t.start()

    client = T2Client(uds_path=sock_path, rpc_timeout_seconds=0.3)
    try:
        # Catching the broader ConnectionRefusedError must NOT swallow
        # the timeout: pytest.raises asserts that RpcTimeoutError
        # propagates, NOT ConnectionRefusedError.
        with pytest.raises(RpcTimeoutError):
            client.call("ping")
    finally:
        for c in accepted:
            c.close()
        server.close()
        client.close()
