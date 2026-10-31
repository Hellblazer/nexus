# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The single-instance lock of ``nx upgrade --auto`` (nexus-75w5d).

The SessionStart hook starts a detached ``nx upgrade --auto`` per session. On
2026-10-31 nineteen of them ran at once for over an hour, each walking the
cloud catalog, because the only cross-process lock in the path (the RDR-192
rung's) is taken inside ``converge()``, after every rung's ``detect()`` has
already run. This lock is taken first: at the top of the ``nx`` root group,
before the finish pass, before any rung is asked anything. A second ``--auto``
process finds it held and exits 0 at once; the hook probes it before it forks.

**One OS lock on ``<config>/upgrade-auto.lock``** (:mod:`nexus._locking`, the
repo's one primitive: ``flock`` / ``msvcrt.locking``). The kernel drops it when
the holder dies, so a stale pid file cannot wedge it: that is what "stale-pid
tolerant" means here. The holder's pid is written into the file for a human
reading it; nothing decides on it.

Stdlib only plus ``nexus._locking`` (itself stdlib only): the hook imports this
on every session start and must stay import-light.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

LOCK_NAME = "upgrade-auto.lock"


def _config_dir() -> Path:
    """``NEXUS_CONFIG_DIR`` else ``~/.config/nexus``: ``nexus.config.nexus_config_dir``
    restated with stdlib only, because ``nexus.config`` costs far more than this hook's whole import."""
    override = os.environ.get("NEXUS_CONFIG_DIR", "").strip()
    return Path(override) if override else Path.home() / ".config" / "nexus"


def lock_path(config_dir: Path | None = None) -> Path:
    return (config_dir if config_dir is not None else _config_dir()) / LOCK_NAME


def argv_is_auto_upgrade(argv: list[str]) -> bool:
    """True when *argv* (without the program name) is a real ``nx upgrade --auto`` run.

    The root group takes only flags, so the subcommand is the first token that is not
    one (the same reading ``nexus.upgrade_finish.upgrade_invocation`` makes). ``--help``
    runs nothing, so it is not an auto run."""
    if next((a for a in argv if not a.startswith("-")), None) != "upgrade":
        return False
    return "--auto" in argv and "--help" not in argv and "-h" not in argv


@dataclass
class AutoLock:
    """The outcome of :func:`acquire_auto_lock`. ``held_by_other`` is the only state that stops a run."""

    fd: int | None
    held_by_other: bool
    path: Path

    def release(self) -> None:
        """Drop the lock (idempotent). The kernel does the same at process exit."""
        fd, self.fd = self.fd, None
        if fd is None:
            return
        from nexus._locking import unlock_fd  # noqa: PLC0415 — deferred: only a holder needs it

        try:
            unlock_fd(fd)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def holder_pid(self) -> int | None:
        """The pid the holder wrote, or ``None`` (unreadable, or this process holds it)."""
        try:
            text = self.path.read_text(encoding="utf-8").strip()
            return int(text) if text.isdigit() else None
        except (OSError, ValueError):
            return None


def acquire_auto_lock(config_dir: Path | None = None) -> AutoLock:
    """Try to take the lock without blocking.

    ``held_by_other=True``: a live process holds it; the caller exits 0. Otherwise the
    caller runs: either it now holds the lock (``fd`` set), or the lock could not be
    created at all (a read-only config dir): then ``fd`` is ``None`` and the run goes on
    unlocked, because a lock problem must never stop the upgrade."""
    from nexus._locking import lock_fd  # noqa: PLC0415 — deferred, see the module docstring

    path = lock_path(config_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError:
        return AutoLock(fd=None, held_by_other=False, path=path)
    try:
        lock_fd(fd, blocking=False)
    except BlockingIOError:
        os.close(fd)
        return AutoLock(fd=None, held_by_other=True, path=path)
    except OSError:
        os.close(fd)
        return AutoLock(fd=None, held_by_other=False, path=path)
    try:
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.lseek(fd, 0, os.SEEK_SET)  # msvcrt unlocks from the current position
    except OSError:
        pass  # the pid is for a human; the lock is what counts
    return AutoLock(fd=fd, held_by_other=False, path=path)


def auto_lock_held(config_dir: Path | None = None) -> bool:
    """True when a live process holds the lock: a probe that takes and drops it at once.
    Fails open (``False``) on any error, so the hook spawns and the child decides."""
    lock = acquire_auto_lock(config_dir)
    if lock.held_by_other:
        return True
    lock.release()
    return False
