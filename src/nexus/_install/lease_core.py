# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The generation lease: what a builder writes and what gc asks.

WHY. ``census_core`` finds a live process by the literal ``<tools>/gen-<stamp>/``
string in its argv or exe. It cannot see a process started through
``tools/current/...`` (its argv names ``current``, and after a flip that is a
different generation), one that rewrote its title, or one whose argv was cut. On
Windows ``rmtree`` removes every file that is not open before ``python.exe``
refuses, so a generation kept as "in use" is already gutted. The lease keys on the
real tree instead: every interpreter a generation starts holds a lock on
``<generation>/.nx-lease`` until it exits, and gc asks the kernel.

THE GUARANTEE. gc never deletes a generation while a live interpreter that
started from it holds its lease. That covers generations built by this release or
later and the processes started from them. A generation built before this has no
lease file and the census alone guards it, exactly as before; backfilling one
would protect nothing, because no running process holds a lock on a file created
after it started.

THE PIECES. ``_nx_lease.py`` is the interpreter's half (copied into the
generation's ``site-packages`` and loaded by ``_nx_lease.pth``). This module is the
builder's half (:func:`install_lease`, :func:`self_test`) and gc's half
(:func:`probe`, :func:`acquire`). Stdlib-only and importing nothing from nexus,
like its siblings; ``gc_core`` and ``generation_core`` reach it through
``_sibling``. Pinned by ``tests/test_install_lease_core_is_bootstrap_safe.py``.

STATES, as :func:`probe` answers them:

* ``absent``       no ``.nx-lease``: a generation built before the lease existed
* ``held``         a live interpreter holds it
* ``free``         nobody does
* ``unsupported``  the filesystem has no lock (ENOLCK, ENOSYS, EOPNOTSUPP, ENOTSUP)
* ``error:<what>`` anything else, including an unknown lease version
  (``error:version``); gc keeps the generation for that run

On POSIX the probe opens ``O_RDWR`` (NFS's ``flock`` emulation wants a write
descriptor for an exclusive lock) and tries ``LOCK_EX|LOCK_NB``. On Windows it
opens with ``CreateFileW`` at share mode 0: a sharing violation is ``held``. The
reap-time open asks for delete access and shares only ``FILE_SHARE_DELETE``, so it
keeps every other opener out and the reap can unlink the lease file through it as
its LAST step. Nothing is deleted by closing the handle: a reap that stops early
leaves the lease file, and the surviving generation stays leased.
"""
from __future__ import annotations

import errno
import os
import subprocess
import sys
import threading
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover -- Windows
    fcntl = None  # type: ignore[assignment]

#: Restated rather than imported (see ``gc_core.GC_USAGE_EXIT``); pinned equal.
LEASE_USAGE_EXIT = 64

LEASE_NAME = ".nx-lease"
#: The first line of ``.nx-lease``. An unknown version is ``error:version``.
LEASE_HEADER = "nx-lease 1"
HOOK_NAME = "_nx_lease.py"
PTH_NAME = "_nx_lease.pth"
#: One guarded line. ``site`` execs a ``.pth`` line that starts with ``import``
#: and prints a traceback if it raises (and drops the rest of the file), so the
#: body is a string run inside its own ``try``: a missing or broken hook is silent.
PTH_TEXT = 'import os; exec("try:\\n import _nx_lease\\nexcept Exception:\\n pass")\n'

STATE_ABSENT = "absent"
STATE_HELD = "held"
STATE_FREE = "free"
STATE_UNSUPPORTED = "unsupported"
STATE_ERROR = "error"
STATES = (STATE_ABSENT, STATE_HELD, STATE_FREE, STATE_UNSUPPORTED, STATE_ERROR)

_UNSUPPORTED = frozenset(
    getattr(errno, name) for name in ("ENOLCK", "ENOSYS", "EOPNOTSUPP", "ENOTSUP") if hasattr(errno, name)
)

# Windows error numbers (winerror), restated so the logic reads on any host.
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_SHARING_VIOLATION = 32
_ERROR_LOCK_VIOLATION = 33


def _is_nt(platform: str | None) -> bool:
    return (platform if platform is not None else sys.platform).startswith("win")


def lease_path(gen: Path | str) -> Path:
    return Path(gen) / LEASE_NAME


def state_kind(state: str) -> str:
    """``error:EACCES`` -> ``error``; the other states are their own kind."""
    return STATE_ERROR if state.startswith("error") else state


# ---------------------------------------------------------------------------
# Writing the lease (the builders)
# ---------------------------------------------------------------------------


def site_packages(gen: Path | str) -> Path | None:
    """``Lib/site-packages`` (Windows) or ``lib/pythonX.Y/site-packages``, globbed
    rather than asked of the venv's interpreter: the layout is fixed and this must
    work before anything in the venv can run. None when there is neither."""
    gen = Path(gen)
    for pattern in ("Lib/site-packages", "lib/python*/site-packages"):
        for hit in sorted(gen.glob(pattern)):
            if hit.is_dir():
                return hit
    return None


def _write_atomic(path: Path, data: bytes) -> None:
    """Whole file or none: a reader (the hook of an interpreter starting now, gc)
    never sees a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def hook_source() -> Path:
    """The hook beside this file: each builder copies it from its OWN ``_install``
    directory, so builder and hook are one version whatever the wheel ships."""
    return Path(__file__).resolve().parent / HOOK_NAME


def install_lease(gen: Path | str) -> Path:
    """Write the hook module, the ``.pth``, then ``.nx-lease``, in that order.

    The lease file is what gc keys on and it goes last, so a build killed part
    way is an UNLEASED tree (judged by the census, as before), never a leased one
    with no hook behind it. The caller writes the receipt after this returns.
    Raises ``OSError`` when the files cannot be written, or when *gen* has no
    ``site-packages``; returns the ``site-packages`` directory used."""
    gen = Path(gen)
    site = site_packages(gen)
    if site is None:
        raise OSError(f"no site-packages under {gen}")
    _write_atomic(site / HOOK_NAME, hook_source().read_bytes())
    _write_atomic(site / PTH_NAME, PTH_TEXT.encode("ascii"))
    if not _has_valid_lease(gen):
        # Never replace a lease file that is already valid: the replacement is a new
        # inode, every interpreter holding the old one is orphaned, and gc would probe
        # the new one and read free.
        _write_atomic(lease_path(gen), (LEASE_HEADER + "\n").encode("ascii"))
    return site


def _has_valid_lease(gen: Path) -> bool:
    try:
        head = lease_path(gen).read_bytes()[:64]
    except OSError:
        return False
    return head.split(b"\n", 1)[0].rstrip(b"\r") == LEASE_HEADER.encode("ascii")


def _venv_python(gen: Path, platform: str | None) -> Path:
    return gen / ("Scripts/python.exe" if _is_nt(platform) else "bin/python")


def self_test(gen: Path | str, *, platform: str | None = None, timeout: float = 20.0) -> str:
    """Start the generation's own interpreter as a child, and probe while it runs.

    ``held`` means the hook works end to end. Anything else means this generation
    is leased on paper only (a broken ``.pth``, a filesystem without locks): every
    lease would read ``free`` and behaviour would equal today's, invisibly. Returns
    the probe's state, or ``unverified: <why>`` when no child could be started."""
    gen = Path(gen)
    python = _venv_python(gen, platform)
    if not python.is_file():
        return f"unverified: no interpreter at {python}"
    code = "import sys; sys.stdout.write('ready\\n'); sys.stdout.flush(); sys.stdin.read()"
    try:
        proc = subprocess.Popen(  # noqa: S603 -- the generation's own interpreter
            [str(python), "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",  # the child writes one ASCII word
        )
    except OSError as exc:
        return f"unverified: {exc}"
    watchdog = threading.Timer(timeout, proc.kill)
    watchdog.daemon = True
    watchdog.start()
    try:
        if proc.stdout.readline().strip() != "ready":
            return "unverified: the interpreter did not start"
        return probe(gen, platform=platform)
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                stream.close()


def install_and_verify(gen: Path | str, *, platform: str | None = None) -> list[str]:
    """What a builder calls: :func:`install_lease`, then :func:`self_test`. Returns
    the lines to print. NEVER raises and never fails the build: an unleased or
    unverified generation is guarded by the census as it was before the lease."""
    gen = Path(gen)
    try:
        install_lease(gen)
    except OSError as exc:
        return [f"install_generation: WARNING: {gen.name} is not leased ({exc}); the process census alone guards it"]
    try:
        state = self_test(gen, platform=platform)
    except Exception as exc:  # noqa: BLE001 -- the check must never fail the build it checks
        state = f"unverified: {type(exc).__name__}: {exc}"
    if state == STATE_HELD:
        return ["install_generation: lease: verified"]
    return [
        f"install_generation: WARNING: the lease on {gen.name} could not be verified "
        f"(a live interpreter read {state}, not held); the process census alone guards it"
    ]


# ---------------------------------------------------------------------------
# Asking the lease (gc)
# ---------------------------------------------------------------------------


class Hold:
    """The exclusive hold :func:`acquire` returns. :meth:`release` is idempotent.

    Releasing removes nothing, on any platform. The Windows reap unlinks ``.nx-lease``
    itself, through this handle and only after everything else is gone, so a reap that
    stops early (a locked file, an exception) leaves the lease file in place. The tree
    it leaves is half-deleted and receipt-less, and the lease file does not make it
    safe: nothing holding a lease can exist (the hold was exclusive), and whatever
    blocked the delete holds none, so only the census guards that tree until the next
    run retries it."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def release(self) -> None:
        fd, self._fd = self._fd, -1
        if fd >= 0:
            os.close(fd)

    def __enter__(self) -> "Hold":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def _classify(exc: OSError) -> str:
    winerror = getattr(exc, "winerror", None)
    if winerror in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND):
        return STATE_ABSENT
    if winerror in (_ERROR_SHARING_VIOLATION, _ERROR_LOCK_VIOLATION):
        return STATE_HELD
    if winerror is not None:
        return f"error:win{winerror}"
    if exc.errno in _UNSUPPORTED:
        return STATE_UNSUPPORTED
    if exc.errno in (errno.ENOENT, errno.ENOTDIR):
        return STATE_ABSENT
    return "error:" + errno.errorcode.get(exc.errno or 0, str(exc.errno))


def _header_ok(fd: int) -> bool:
    head = os.read(fd, 64)
    return head.split(b"\n", 1)[0].rstrip(b"\r") == LEASE_HEADER.encode("ascii")


def _win_open(path: str, for_delete: bool) -> int:
    """Open *path* at share mode 0 (``CreateFileW``) and return a C-runtime file
    descriptor that owns the handle. With *for_delete* the open also asks for DELETE
    access and shares only ``FILE_SHARE_DELETE``: still exclusive against any reader
    or writer, and the caller may unlink the file through it. Raises ``OSError`` (with
    ``winerror``) when another handle is open: that IS the held answer. This is the one place ctypes
    is used, and only gc reaches it, never an interpreter start."""
    import ctypes  # noqa: PLC0415 -- Windows gc only
    import msvcrt  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    generic_read, delete = 0x80000000, 0x00010000
    open_existing, normal, share_delete = 3, 0x80, 0x4
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = k32.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    handle = create(
        path, generic_read | (delete if for_delete else 0), share_delete if for_delete else 0, None,
        open_existing, normal, None,
    )
    if handle in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except OSError:
        k32.CloseHandle(wintypes.HANDLE(handle))
        raise


def acquire(
    gen: Path | str, *, platform: str | None = None, for_delete: bool = False,
) -> tuple[str, Hold | None]:
    """Try to take the lease exclusively. ``(state, hold)``.

    ``free`` returns a :class:`Hold` that keeps everyone else out until released.
    Every other state returns ``None``. *for_delete* (Windows only; the reap) opens
    with delete access so the reap can unlink ``.nx-lease`` through the hold."""
    path = str(lease_path(gen))
    if _is_nt(platform):
        try:
            os.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            return STATE_ABSENT, None  # no ctypes for a generation built before leases
        except OSError as exc:
            return _classify(exc), None  # access denied is an error, not an absence
        try:
            fd = _win_open(path, for_delete)
        except OSError as exc:
            return _classify(exc), None
        if not for_delete:  # the reap-time open trusts the plan-time probe's reading of the header
            try:
                if not _header_ok(fd):
                    os.close(fd)
                    return "error:version", None
            except OSError as exc:
                os.close(fd)
                return _classify(exc), None
        return STATE_FREE, Hold(fd)

    try:
        fd = os.open(path, os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        return _classify(exc), None
    try:
        try:
            header_ok = _header_ok(fd)
        except OSError as exc:  # EIO on a failing disk, ESTALE on NFS: an error, not a crash of the sweep
            os.close(fd)
            return _classify(exc), None
        if not header_ok:
            os.close(fd)
            return "error:version", None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return STATE_HELD, None
            return _classify(exc), None
    except BaseException:
        os.close(fd)
        raise
    return STATE_FREE, Hold(fd)


def probe(gen: Path | str, platform: str | None = None) -> str:
    """The lease state of *gen* (see the module docstring). Takes the exclusive
    lock and drops it again, so call it only for a generation that would
    otherwise be reaped: for a moment a starting interpreter would fail to take
    its shared lock (the hook retries for ~20 ms)."""
    state, hold = acquire(gen, platform=platform)
    if hold is not None:
        hold.release()
    return state


# ---------------------------------------------------------------------------
# CLI: the shell builder's way in (no Python quoted inside bash)
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    """``lease_core.py install <generation>``: write the lease and verify it,
    printing the result lines on stdout. Exits 0 whatever the outcome (a build is
    never failed by its lease); a bad command line exits ``LEASE_USAGE_EXIT``."""
    if len(argv) == 2 and argv[0] == "install":
        for line in install_and_verify(argv[1]):
            sys.stdout.write(line + "\n")
        return 0
    sys.stderr.write("usage: lease_core.py install <generation>\n")
    return LEASE_USAGE_EXIT


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
