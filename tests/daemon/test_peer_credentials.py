"""Tests for nexus.daemon.peer — cross-platform peer-credential accessor.

RDR-113 §Risks mitigation. Linux SO_PEERCRED returns `struct ucred` (3i).
macOS LOCAL_PEERCRED returns `struct xucred` (cr_version, cr_uid,
cr_ngroups, cr_groups[16]) = 76 bytes with natural alignment.

Fail-loud on all error paths — no fallback (RDR-113 §Failure Modes).
"""

from __future__ import annotations

import os
import socket
import struct
import sys
from unittest.mock import patch

import pytest


# -- Linux path -------------------------------------------------------------


def test_linux_parses_ucred_struct():
    """Linux SO_PEERCRED returns three 32-bit ints (pid, uid, gid)."""
    from nexus.daemon import peer

    packed = struct.pack("3i", 12345, 1001, 1001)
    fake_sock = _make_uds_sock_mock(packed)

    with patch.object(sys, "platform", "linux"):
        creds = peer.read_peer_credentials(fake_sock)

    assert creds.pid == 12345
    assert creds.uid == 1001
    assert creds.gid == 1001


def test_linux_uses_so_peercred_constants():
    """Linux path must use SOL_SOCKET + SO_PEERCRED."""
    from nexus.daemon import peer

    packed = struct.pack("3i", 1, 2, 3)
    fake_sock = _make_uds_sock_mock(packed, capture=True)

    with patch.object(sys, "platform", "linux"):
        peer.read_peer_credentials(fake_sock)

    level, optname, _ = fake_sock.last_getsockopt
    assert level == socket.SOL_SOCKET
    assert optname == getattr(socket, "SO_PEERCRED", 17)


# -- macOS path -------------------------------------------------------------


def _pack_xucred(uid: int, version: int = 0, ngroups: int = 1, gid: int = 20) -> bytes:
    """Pack a minimal valid xucred byte string (76 bytes)."""
    groups = [gid] + [0] * 15
    return struct.pack("=IIH2x16I", version, uid, ngroups, *groups)


def test_macos_parses_xucred_struct():
    """macOS LOCAL_PEERCRED returns xucred (76 bytes); extract uid + gid."""
    from nexus.daemon import peer

    packed = _pack_xucred(uid=501, ngroups=1, gid=20)
    fake_sock = _make_uds_sock_mock(packed)

    with patch.object(sys, "platform", "darwin"):
        creds = peer.read_peer_credentials(fake_sock)

    # macOS xucred does NOT carry pid; pid field is -1 sentinel.
    assert creds.pid == -1
    assert creds.uid == 501
    assert creds.gid == 20


def test_macos_rejects_bad_xucred_version():
    """cr_version != XUCRED_VERSION (0) must fail loud."""
    from nexus.daemon import peer

    packed = _pack_xucred(uid=501, version=99)
    fake_sock = _make_uds_sock_mock(packed)

    with patch.object(sys, "platform", "darwin"):
        with pytest.raises(OSError, match="xucred version"):
            peer.read_peer_credentials(fake_sock)


def test_macos_short_payload_raises():
    """xucred payload shorter than 76 bytes must fail loud."""
    from nexus.daemon import peer

    fake_sock = _make_uds_sock_mock(b"\x00" * 10)

    with patch.object(sys, "platform", "darwin"):
        with pytest.raises(OSError, match="too short"):
            peer.read_peer_credentials(fake_sock)


def test_macos_zero_ngroups_raises():
    """xucred with ngroups=0 cannot derive gid; fail loud per RDR-113."""
    from nexus.daemon import peer

    packed = _pack_xucred(uid=501, ngroups=0, gid=0)
    fake_sock = _make_uds_sock_mock(packed)

    with patch.object(sys, "platform", "darwin"):
        with pytest.raises(OSError, match="ngroups"):
            peer.read_peer_credentials(fake_sock)


def test_linux_parses_large_uid_above_signed_int_max():
    """UIDs > 2^31 (e.g. nobody=4294967294) must not unpack as negative."""
    from nexus.daemon import peer

    packed = struct.pack("=iII", 12345, 4294967294, 4294967294)
    fake_sock = _make_uds_sock_mock(packed)

    with patch.object(sys, "platform", "linux"):
        creds = peer.read_peer_credentials(fake_sock)

    assert creds.uid == 4294967294
    assert creds.gid == 4294967294


def test_macos_uses_local_peercred_constants():
    """macOS path uses SOL_LOCAL=0 + LOCAL_PEERCRED=0x001 (numeric fallback)."""
    from nexus.daemon import peer

    packed = _pack_xucred(uid=501)
    fake_sock = _make_uds_sock_mock(packed, capture=True)

    with patch.object(sys, "platform", "darwin"):
        peer.read_peer_credentials(fake_sock)

    level, optname, _ = fake_sock.last_getsockopt
    assert level == getattr(socket, "SOL_LOCAL", 0)
    assert optname == getattr(socket, "LOCAL_PEERCRED", 0x001)


# -- Error paths ------------------------------------------------------------


def test_tcp_socket_rejected():
    """Non-UDS sockets must raise ValueError."""
    from nexus.daemon import peer

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp_sock:
        with pytest.raises(ValueError, match="UDS"):
            peer.read_peer_credentials(tcp_sock)


def test_unsupported_platform_raises():
    """Unsupported platforms fail loud, no fallback."""
    from nexus.daemon import peer

    fake_sock = _make_uds_sock_mock(b"")

    with patch.object(sys, "platform", "win32"):
        with pytest.raises(NotImplementedError, match="win32"):
            peer.read_peer_credentials(fake_sock)


def test_freebsd_unsupported():
    """BSD variants also unsupported in v1."""
    from nexus.daemon import peer

    fake_sock = _make_uds_sock_mock(b"")

    with patch.object(sys, "platform", "freebsd13"):
        with pytest.raises(NotImplementedError):
            peer.read_peer_credentials(fake_sock)


# -- Integration (real UDS socketpair) -------------------------------------


@pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"),
    reason="peer-cred only supported on Linux/macOS",
)
def test_integration_real_uds_socketpair_returns_real_uid():
    """End-to-end on a real AF_UNIX socketpair: returned UID matches geteuid."""
    from nexus.daemon import peer

    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        creds = peer.read_peer_credentials(a)
        assert creds.uid == os.geteuid()
        # On Linux pid is the peer pid; on macOS pid is -1 sentinel.
        if sys.platform == "linux":
            assert creds.pid == os.getpid()
    finally:
        a.close()
        b.close()


# -- Helpers ----------------------------------------------------------------


class _SockMock:
    """Minimal duck-typed UDS socket for tests."""

    def __init__(self, payload: bytes, capture: bool = False):
        self._payload = payload
        self._capture = capture
        self.last_getsockopt: tuple[int, int, int] | None = None
        self.family = socket.AF_UNIX

    def getsockopt(self, level: int, optname: int, buflen: int) -> bytes:
        if self._capture:
            self.last_getsockopt = (level, optname, buflen)
        return self._payload


def _make_uds_sock_mock(payload: bytes, capture: bool = False) -> _SockMock:
    return _SockMock(payload, capture=capture)
