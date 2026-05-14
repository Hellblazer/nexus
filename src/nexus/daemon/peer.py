"""Cross-platform peer-credential accessor for UDS sockets.

RDR-113 §Host-Trust Model: the daemon accepts connections only from
peer processes whose effective UID matches the daemon's UID. This module
isolates the platform-specific `getsockopt` dance behind a single
``read_peer_credentials`` entry point.

Linux ``SO_PEERCRED`` returns ``struct ucred = {pid, uid, gid}`` as three
32-bit ints (12 bytes). macOS ``LOCAL_PEERCRED`` returns ``struct xucred``
(76 bytes, natural-alignment) — no PID, but ``cr_uid`` plus a 16-slot
group array; ``cr_groups[0]`` is the effective GID.

Failures are loud: unsupported platform → ``NotImplementedError``;
non-UDS socket → ``ValueError``; ``cr_version != XUCRED_VERSION`` → ``OSError``.
No "accept with warning" fallback (RDR-113 §Failure Modes).
"""

from __future__ import annotations

import socket
import struct
import sys
from typing import NamedTuple

import structlog

_log = structlog.get_logger(__name__)


class PeerCredentials(NamedTuple):
    """Peer process identity over a UDS socket.

    ``pid`` is ``-1`` on macOS — ``xucred`` does not carry it. Callers
    that need PID-level identity must use Linux or solicit it over the
    application protocol.
    """

    pid: int
    uid: int
    gid: int


# Linux struct ucred = {pid_t pid, uid_t uid, gid_t gid}: signed pid (can be
# negative in odd corner cases), unsigned uid/gid (UIDs above 2^31 unpack as
# negative if we read them as signed — see nobody on some distros).
_LINUX_UCRED_FMT = "=iII"
_LINUX_UCRED_SIZE = struct.calcsize(_LINUX_UCRED_FMT)

# macOS struct xucred (sys/ucred.h, NGROUPS_MAX-ish flattened to 16):
#   u_int  cr_version;          // 4 bytes
#   uid_t  cr_uid;              // 4 bytes
#   short  cr_ngroups;          // 2 bytes + 2 pad
#   gid_t  cr_groups[16];       // 64 bytes
# Total: 76 bytes, native alignment, no struct padding beyond the explicit 2x.
_DARWIN_XUCRED_FMT = "=IIH2x16I"
_DARWIN_XUCRED_SIZE = struct.calcsize(_DARWIN_XUCRED_FMT)

# CPython does not expose SOL_LOCAL / LOCAL_PEERCRED in the socket module
# on macOS; fall back to the kernel ABI numbers from <sys/un.h>.
_DARWIN_SOL_LOCAL = getattr(socket, "SOL_LOCAL", 0)
_DARWIN_LOCAL_PEERCRED = getattr(socket, "LOCAL_PEERCRED", 0x001)

# Likewise SO_PEERCRED is absent on macOS-built CPython; use the Linux ABI
# value (17) so tests on either platform can patch sys.platform freely.
_LINUX_SO_PEERCRED = getattr(socket, "SO_PEERCRED", 17)

_XUCRED_VERSION = 0


def read_peer_credentials(sock: socket.socket) -> PeerCredentials:
    """Read the peer process credentials from a connected AF_UNIX socket.

    Args:
        sock: A connected ``AF_UNIX`` socket (one end of an accepted UDS
            connection or a ``socketpair`` half).

    Returns:
        ``PeerCredentials(pid, uid, gid)``. ``pid`` is ``-1`` on macOS.

    Raises:
        ValueError: ``sock`` is not an ``AF_UNIX`` socket.
        NotImplementedError: Running on a platform other than Linux or
            macOS.
        OSError: The kernel returned a peer-cred payload that fails the
            integrity check (e.g. macOS ``cr_version`` mismatch).
    """
    family = getattr(sock, "family", None)
    if family != socket.AF_UNIX:
        raise ValueError(
            "peer credentials only available on UDS (AF_UNIX) sockets; "
            f"got family={family!r}"
        )

    platform = sys.platform
    if platform.startswith("linux"):
        return _read_linux(sock)
    if platform == "darwin":
        return _read_darwin(sock)
    raise NotImplementedError(
        f"peer credentials not supported on platform {platform!r}; "
        "Linux and macOS only in v1"
    )


def _read_linux(sock: socket.socket) -> PeerCredentials:
    raw = sock.getsockopt(socket.SOL_SOCKET, _LINUX_SO_PEERCRED, _LINUX_UCRED_SIZE)
    pid, uid, gid = struct.unpack(_LINUX_UCRED_FMT, raw[:_LINUX_UCRED_SIZE])
    _log.info("peer_cred_read", platform="linux", pid=pid, uid=uid, gid=gid)
    return PeerCredentials(pid=pid, uid=uid, gid=gid)


def _read_darwin(sock: socket.socket) -> PeerCredentials:
    raw = sock.getsockopt(
        _DARWIN_SOL_LOCAL, _DARWIN_LOCAL_PEERCRED, _DARWIN_XUCRED_SIZE
    )
    if len(raw) < _DARWIN_XUCRED_SIZE:
        raise OSError(
            f"xucred payload too short: got {len(raw)} bytes, "
            f"expected {_DARWIN_XUCRED_SIZE}"
        )
    unpacked = struct.unpack(_DARWIN_XUCRED_FMT, raw[:_DARWIN_XUCRED_SIZE])
    version, uid, ngroups, *groups = unpacked
    if version != _XUCRED_VERSION:
        raise OSError(
            f"unexpected xucred version: got {version}, "
            f"expected {_XUCRED_VERSION}"
        )
    if ngroups < 1:
        raise OSError(
            f"xucred reports no groups (ngroups={ngroups}); "
            "cannot derive peer gid"
        )
    gid = groups[0]
    _log.info(
        "peer_cred_read",
        platform="darwin",
        uid=uid,
        gid=gid,
        ngroups=ngroups,
    )
    return PeerCredentials(pid=-1, uid=uid, gid=gid)
