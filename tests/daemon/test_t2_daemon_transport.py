# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for T2 daemon transport layer — RDR-112 P1.1 (nexus-61x6).

Covers:
  (a) two-client connect via UDS
  (b) two-client connect via TCP
  (c) UDS-permission rejection (peer with foreign uid simulated)
  (d) handshake version mismatch
  (e) graceful SIGTERM shutdown unlinks discovery file

All tests use port=0 for dynamic TCP allocation and tmp_path for UDS paths
and config dirs. No hardcoded ports or paths.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import struct
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio

from nexus.daemon.t2_daemon import (
    DAEMON_PROTOCOL_VERSION,
    T2Daemon,
    ProtocolError,
    read_frame,
    write_frame,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _connect_uds(uds_path: Path) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open an asyncio UDS connection to the given path."""
    return await asyncio.open_unix_connection(str(uds_path))


async def _connect_tcp(host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open an asyncio TCP connection."""
    return await asyncio.open_connection(host, port)


async def _handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    version: str = DAEMON_PROTOCOL_VERSION,
) -> dict[str, Any]:
    """Send hello frame and read the daemon's hello_ack response."""
    write_frame(writer, {"op": "hello", "protocol_version": version})
    await writer.drain()
    return await read_frame(reader)


async def _ping(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> dict[str, Any]:
    """Send ping and read pong."""
    write_frame(writer, {"op": "ping"})
    await writer.drain()
    return await read_frame(reader)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    """Isolated config directory for each test."""
    d = tmp_path / "config" / "nexus"
    d.mkdir(parents=True)
    return d


@pytest_asyncio.fixture()
async def running_daemon(config_dir: Path):
    """Start a T2Daemon, yield it running, then stop it.

    Returns the daemon instance so tests can inspect uds_path, tcp_port etc.
    """
    daemon = T2Daemon(config_dir=config_dir)
    await daemon.start()
    yield daemon
    await daemon.stop()


# ---------------------------------------------------------------------------
# (a) Two-client connect via UDS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_clients_uds(running_daemon: T2Daemon) -> None:
    """Two simultaneous UDS clients both receive ping responses."""
    async def _client() -> dict[str, Any]:
        reader, writer = await _connect_uds(running_daemon.uds_path)
        try:
            ack = await _handshake(reader, writer)
            assert ack.get("op") == "hello_ack"
            pong = await _ping(reader, writer)
            return pong
        finally:
            writer.close()
            await writer.wait_closed()

    results = await asyncio.gather(_client(), _client())
    for pong in results:
        assert pong.get("pong") is True
        assert "version" in pong
        assert "start_time" in pong


# ---------------------------------------------------------------------------
# (b) Two-client connect via TCP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_clients_tcp(running_daemon: T2Daemon) -> None:
    """Two simultaneous TCP clients both receive ping responses."""
    async def _client() -> dict[str, Any]:
        reader, writer = await _connect_tcp("127.0.0.1", running_daemon.tcp_port)
        try:
            ack = await _handshake(reader, writer)
            assert ack.get("op") == "hello_ack"
            pong = await _ping(reader, writer)
            return pong
        finally:
            writer.close()
            await writer.wait_closed()

    results = await asyncio.gather(_client(), _client())
    for pong in results:
        assert pong.get("pong") is True


# ---------------------------------------------------------------------------
# (c) UDS-permission rejection (peer with foreign uid simulated)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uds_peer_uid_rejection(config_dir: Path) -> None:
    """A UDS peer whose UID differs from the daemon's UID is rejected.

    nexus-04zd: the contract is that the daemon writes a typed error frame
    naming the rejected UID before closing the connection. A bare
    connection-close (no frame) used to be an acceptable outcome here,
    which silently passed if the peer-cred check were ever skipped via a
    regression. We now hard-require the error frame.

    S-7: the monkey patch targets ``nexus.daemon.peer.read_peer_credentials``
    at source. t2_daemon imports the ``peer`` module and calls
    ``peer.read_peer_credentials(...)``, so the source-attribute patch is
    what the call site resolves through.
    """
    from nexus.daemon.peer import PeerCredentials

    daemon = T2Daemon(config_dir=config_dir)
    await daemon.start()
    try:
        foreign_uid = os.geteuid() + 1
        fake_creds = PeerCredentials(pid=9999, uid=foreign_uid, gid=9999)

        with patch(
            "nexus.daemon.peer.read_peer_credentials",
            return_value=fake_creds,
        ):
            reader, writer = await _connect_uds(daemon.uds_path)
            try:
                write_frame(writer, {"op": "hello", "protocol_version": DAEMON_PROTOCOL_VERSION})
                await writer.drain()
                response = await asyncio.wait_for(read_frame(reader), timeout=2.0)
                assert "error" in response, (
                    f"daemon must write a typed error frame for foreign-UID "
                    f"rejection; got {response!r}"
                )
                err_str = response["error"]
                # Error frame is currently a bare string; tolerate either
                # legacy string shape or a typed dict shape.
                err_text = err_str.lower() if isinstance(err_str, str) else (
                    err_str.get("message", "").lower() + err_str.get("type", "").lower()
                )
                assert "uid" in err_text or "reject" in err_text, (
                    f"error frame must name 'uid' or 'reject'; got {response!r}"
                )
            finally:
                writer.close()
                await writer.wait_closed()
    finally:
        await daemon.stop()


# ---------------------------------------------------------------------------
# (d) Handshake version mismatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_version_mismatch_rejected(running_daemon: T2Daemon) -> None:
    """A client with a mismatched protocol_version receives an error and is disconnected.

    nexus-04zd: the daemon contract is that an error frame is written
    naming the version mismatch before the connection closes. A bare
    close (no frame) is NOT an acceptable substitute; the test used to
    silently pass on that path, which masked any regression where the
    daemon failed to produce the error frame.
    """
    reader, writer = await _connect_uds(running_daemon.uds_path)
    try:
        write_frame(writer, {"op": "hello", "protocol_version": "99.99"})
        await writer.drain()
        response = await asyncio.wait_for(read_frame(reader), timeout=2.0)
        assert "error" in response, (
            f"daemon must write a typed error frame for version mismatch; "
            f"got {response!r}"
        )
        err = response["error"]
        err_text = err.lower() if isinstance(err, str) else (
            err.get("message", "").lower() + err.get("type", "").lower()
        )
        assert "version" in err_text or "protocol" in err_text, (
            f"error frame must name 'version' or 'protocol'; got {response!r}"
        )
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_version_match_accepted(running_daemon: T2Daemon) -> None:
    """Correct protocol_version produces a hello_ack."""
    reader, writer = await _connect_uds(running_daemon.uds_path)
    try:
        ack = await _handshake(reader, writer)
        assert ack["op"] == "hello_ack"
        assert ack["daemon_protocol_version"] == DAEMON_PROTOCOL_VERSION
    finally:
        writer.close()
        await writer.wait_closed()


# ---------------------------------------------------------------------------
# (e) Graceful SIGTERM shutdown unlinks discovery file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sigterm_unlinks_discovery_file(config_dir: Path) -> None:
    """SIGTERM triggers graceful drain and unlinks the discovery file."""
    daemon = T2Daemon(config_dir=config_dir)
    await daemon.start()

    discovery_path = daemon.discovery_path
    assert discovery_path.exists(), "discovery file must exist after start"

    # Trigger graceful shutdown directly (simulates SIGTERM handler)
    await daemon.stop()

    assert not discovery_path.exists(), "discovery file must be unlinked after stop"


@pytest.mark.asyncio
async def test_run_until_signal_wakes_on_signal_event(config_dir: Path) -> None:
    """run_until_signal returns when ``stop_event`` is set by the signal path.

    Exercises the full signal-handler wiring: ``run_until_signal`` installs
    the SIGTERM/SIGINT handlers, blocks on ``_stop_event.wait()``, and
    returns after ``stop()`` (which sets the event and drains). A real
    SIGTERM cannot easily be sent from inside a single test process without
    affecting pytest itself, but this covers the same wake path by setting
    the underlying event directly via ``stop()`` from a peer task.
    """
    daemon = T2Daemon(config_dir=config_dir)
    await daemon.start()

    discovery_path = daemon.discovery_path

    async def _trigger_stop() -> None:
        # Yield enough times for run_until_signal to enter its wait, then
        # call stop() — which is also the SIGTERM handler's action.
        for _ in range(20):
            await asyncio.sleep(0.005)
        await daemon.stop()

    trigger_task = asyncio.create_task(_trigger_stop())
    try:
        await daemon.run_until_signal()  # must return cleanly after stop()
    finally:
        await trigger_task

    assert not discovery_path.exists(), (
        "discovery file must be unlinked after run_until_signal returns"
    )


# ---------------------------------------------------------------------------
# UDS socket permissions (RDR-113)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uds_socket_chmod_0600(running_daemon: T2Daemon) -> None:
    """UDS socket must have mode 0o600 (RDR-113 §Proposed Solution §1)."""
    mode = running_daemon.uds_path.stat().st_mode & 0o777
    assert mode == 0o600, f"Expected 0o600 got 0o{mode:o}"


@pytest.mark.asyncio
async def test_tcp_bound_to_loopback(running_daemon: T2Daemon) -> None:
    """TCP listener must bind to 127.0.0.1, not 0.0.0.0 (RDR-113 §Proposed Solution §2)."""
    assert running_daemon.tcp_host == "127.0.0.1"


# ---------------------------------------------------------------------------
# Discovery file content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_file_content(running_daemon: T2Daemon) -> None:
    """Discovery file carries all required fields (RDR-112 §6)."""
    data = json.loads(running_daemon.discovery_path.read_text())
    assert "uds_path" in data
    assert "tcp_host" in data
    assert "tcp_port" in data
    assert "daemon_version" in data
    assert "pid" in data
    assert "start_time" in data
    assert "subspace_schema_digest" in data
    assert data["tcp_host"] == "127.0.0.1"
    assert data["pid"] == os.getpid()


# ---------------------------------------------------------------------------
# Wire-frame round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wire_frame_roundtrip() -> None:
    """Length-prefixed JSON frames survive a full encode/decode cycle."""
    # Create a simple in-memory test via a socketpair
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        r_reader, r_writer = await asyncio.open_unix_connection(sock=left)
        l_reader, l_writer = await asyncio.open_unix_connection(sock=right)

        payload = {"op": "ping", "nested": {"x": 42}}
        write_frame(r_writer, payload)
        await r_writer.drain()

        received = await read_frame(l_reader)
        assert received == payload
    finally:
        r_writer.close()
        l_writer.close()
        await r_writer.wait_closed()
        await l_writer.wait_closed()


# ---------------------------------------------------------------------------
# Frame-length guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_frame_rejects_oversized_length() -> None:
    """A 4-byte length header announcing >16MiB must raise ProtocolError before readexactly blocks."""
    import struct as _struct

    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        l_reader, l_writer = await asyncio.open_unix_connection(sock=left)
        r_reader, r_writer = await asyncio.open_unix_connection(sock=right)

        # Announce a 4 GiB payload; do not actually send it.
        r_writer.write(_struct.pack(">I", 0xFFFFFFFF))
        await r_writer.drain()

        with pytest.raises(ProtocolError, match="exceeds maximum"):
            await asyncio.wait_for(read_frame(l_reader), timeout=2.0)
    finally:
        r_writer.close()
        l_writer.close()
        try:
            await r_writer.wait_closed()
        except Exception:
            pass
        try:
            await l_writer.wait_closed()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Spawn-lock prevents double bind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_lock_prevents_double_start(config_dir: Path) -> None:
    """A second daemon start attempt with the same config_dir fails loudly."""
    d1 = T2Daemon(config_dir=config_dir)
    await d1.start()
    try:
        d2 = T2Daemon(config_dir=config_dir)
        with pytest.raises(RuntimeError, match="[Ll]ock|[Aa]lready|[Rr]unning"):
            await d2.start()
    finally:
        await d1.stop()
