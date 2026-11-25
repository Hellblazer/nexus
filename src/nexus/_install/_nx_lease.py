# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The generation lease, the interpreter's half.

COPIED into a generation's ``site-packages`` by whichever builder made it
(``install_generation.sh`` or ``generation_core.build_generation``, both through
``lease_core.install_lease``), and loaded by that generation's ``_nx_lease.pth``
at every interpreter start. It is never imported as part of nexus: it must work
with nothing installed, in an interpreter nobody asked to be slow.

WHAT IT DOES. It finds its generation as ``realpath(sys.prefix)`` (through
``current`` the prefix is the unresolved link), opens ``<generation>/.nx-lease``
read-only without creating it, requires its first line to be ``nx-lease 1``, and
on POSIX takes a shared ``flock`` that the kernel drops when the process dies.
``gc`` (``lease_core.probe``) tries the exclusive lock: refused means a live
interpreter started from that generation, whatever its argv says. On Windows it
holds the open handle and nothing more: Python's ``open`` gives no delete share,
so the file cannot be removed while any interpreter holds it, and gc's probe
(``CreateFileW`` at share mode 0) fails against it.

WHAT IT MUST NEVER DO. Print, raise, or slow the start. Everything is inside one
``try/except Exception``; the imports are ``os``, ``sys``, ``time`` (a builtin
that site has already loaded) and ``fcntl`` on POSIX, and
``tests/test_install_lease_hook.py`` pins that set and the ``sys.modules`` delta.
The descriptor is a raw int in a module global: a file object in a local would be
closed by the garbage collector and the lease dropped without a sound.

KNOWN LIMITS. ``python -S`` skips ``.pth`` and takes no lease. On NFS ``flock`` is
emulated with byte-range locks, which a fork child does not inherit; that degrades
to the census alone, which is today's guarantee.
"""
import os
import sys
import time

if sys.platform != "win32":
    import fcntl

#: The lease descriptor, held for the life of the process. -1: none taken.
_FD = -1

_HEADER = b"nx-lease 1"
#: A gc probe holds the exclusive lock for microseconds; retry for about this long.
_RETRIES = 10
_PAUSE = 0.002


def _open(path):
    # On Windows a gc probe holds the file at share mode 0 for a moment, and an
    # open that meets it is refused (PermissionError): retry as the POSIX lock does.
    for attempt in range(_RETRIES + 1):
        try:
            return os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except PermissionError:
            if sys.platform != "win32" or attempt == _RETRIES:
                raise
            time.sleep(_PAUSE)


def _take():
    global _FD
    if _FD >= 0:
        return
    fd = _open(os.path.join(os.path.realpath(sys.prefix), ".nx-lease"))
    keep = False
    try:
        if os.read(fd, 32).split(b"\n", 1)[0].rstrip(b"\r") != _HEADER:
            return
        if sys.platform != "win32":
            for attempt in range(_RETRIES + 1):
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if attempt == _RETRIES:
                        return
                    time.sleep(_PAUSE)
        _FD = fd
        keep = True
    finally:
        if not keep:
            os.close(fd)


try:
    _take()
except Exception:  # noqa: BLE001 -- the hook must never raise into an interpreter start
    pass
