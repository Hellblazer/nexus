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
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

LOCK_NAME = "upgrade-auto.lock"

#: The explicit "this run is unattended" signal (nexus-75w5d). ``nx upgrade --auto`` is unattended by
#: its flag; a PLAIN ``nx upgrade`` started by a plugin hook (the version-lockstep action) has no flag
#: and no deadline, so its starter sets this variable instead. Set at CLI start it makes the run
#: unattended (:mod:`nexus.upgrade_ladder.unattended`) and single-instance (this module's lock).
#: A bare string in the plugin script (stdlib-only, imports nothing from ``nexus``): keep the two equal.
UNATTENDED_ENV = "NX_UPGRADE_UNATTENDED"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def env_unattended(environ: Mapping[str, str] | None = None) -> bool:
    """True when ``NX_UPGRADE_UNATTENDED`` is set to a true value (``1``, ``true``, ``yes``, ``on``)."""
    env = os.environ if environ is None else environ
    return env.get(UNATTENDED_ENV, "").strip().lower() in _TRUE_VALUES


def _config_dir() -> Path:
    """``NEXUS_CONFIG_DIR`` else ``~/.config/nexus``: ``nexus.config.nexus_config_dir``
    restated with stdlib only, because ``nexus.config`` costs far more than this hook's whole import."""
    override = os.environ.get("NEXUS_CONFIG_DIR", "").strip()
    return Path(override) if override else Path.home() / ".config" / "nexus"


def lock_path(config_dir: Path | None = None) -> Path:
    return (config_dir if config_dir is not None else _config_dir()) / LOCK_NAME


def _is_upgrade_run(argv: list[str]) -> bool:
    """True for an ``upgrade`` subcommand that runs something (``--help`` runs nothing).

    The root group takes only flags, so the subcommand is the first token that is not
    one (the same reading ``nexus.upgrade_finish.upgrade_invocation`` makes)."""
    if next((a for a in argv if not a.startswith("-")), None) != "upgrade":
        return False
    return "--help" not in argv and "-h" not in argv


def argv_is_auto_upgrade(argv: list[str]) -> bool:
    """True when *argv* (without the program name) is a real ``nx upgrade --auto`` run."""
    return _is_upgrade_run(argv) and "--auto" in argv


def argv_is_unattended_upgrade(argv: list[str], environ: Mapping[str, str] | None = None) -> bool:
    """True for ``nx upgrade --auto`` and for any ``nx upgrade`` run with ``NX_UPGRADE_UNATTENDED``
    set (the lockstep action's detached child). These take the lock; an interactive one does not."""
    return _is_upgrade_run(argv) and ("--auto" in argv or env_unattended(environ))


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


#: The probe sleeps this long and tries once more before it answers "held" (nexus-75w5d, review B1):
#: the probe takes the lock for an instant, so two hooks probing together each see the other's grab.
_PROBE_RETRY_DELAY_S = 0.05


def auto_lock_held(config_dir: Path | None = None) -> bool:
    """True when a live process holds the lock: a probe that takes and drops it at once.
    A first "held" is tried once more after a short pause, so a concurrent probe's instant grab does not
    read as a holder. Fails open (``False``) on any error, so the hook spawns and the child decides."""
    import time  # noqa: PLC0415 — stdlib; only the probe sleeps

    for attempt in range(2):
        lock = acquire_auto_lock(config_dir)
        if not lock.held_by_other:
            lock.release()
            return False
        if attempt == 0:
            time.sleep(_PROBE_RETRY_DELAY_S)
    return True


#: How long a PLAIN unattended ``nx upgrade`` (the lockstep action's child) waits for a holder before it gives
#: up with exit 75 (nexus-75w5d). It waits, where ``--auto`` exits at once, because it does work ``--auto``
#: skips (plugin convergence, the engine install) and the SessionStart hook starts an ``--auto`` at the same
#: moment as the lockstep action: an instant refusal would lose that work to a run that only reads. An
#: ``--auto`` holder ends at its own time limit (default 900 s) at the latest, between rungs.
PLAIN_WAIT_S = 600.0
_PLAIN_POLL_S = 1.0


def acquire_auto_lock_waiting(timeout_s: float | None = None, config_dir: Path | None = None) -> AutoLock:
    """:func:`acquire_auto_lock`, polled once a second until the holder goes or *timeout_s* passes. Returns
    the last answer, so ``held_by_other`` is still true when the holder outlasted the wait."""
    import time  # noqa: PLC0415 — stdlib; only a waiter needs it

    deadline = time.monotonic() + (PLAIN_WAIT_S if timeout_s is None else timeout_s)
    lock = acquire_auto_lock(config_dir)
    while lock.held_by_other and time.monotonic() < deadline:
        time.sleep(_PLAIN_POLL_S)
        lock = acquire_auto_lock(config_dir)
    return lock
