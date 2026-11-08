# SPDX-License-Identifier: AGPL-3.0-or-later
"""The spawn lock as a held resource (nexus-j738q limitation 4): ``ServiceRegistry.try_hold_spawn_lock``.

``nx daemon service pg-upgrade`` builds a new cluster while the storage service is stopped. Nothing used to stop a
concurrent ``nx daemon service start`` (another session, an autostart unit) from starting a supervisor on the
half-built cluster. The supervisor's own ``start()`` already serialises on ``<config>/<tier>_spawn.lock``; the
primitive now offers the same lock to a caller that must keep every start out for a while, so there is one lock and
one definition of its path (the RDR-149 gate: lifecycle behaviour lives in ``service_registry.py``).

Every test uses a real file lock in ``tmp_path``; the cross-process ones use a real child interpreter.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest

from nexus.daemon import storage_service_daemon as ssd
from nexus.daemon.service_registry import (
    ServiceRegistry,
    SpawnLockBusyError,
    spawn_lock_filename,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the pg-upgrade command, the only holder, is POSIX only")

TIERS = ("storage_service", "aspect_worker")

_PROBE = (
    "import os, sys\n"
    "from nexus import _locking\n"
    "fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT, 0o600)\n"
    "try:\n"
    "    _locking.lock_fd(fd, blocking=False)\n"
    "except BlockingIOError:\n"
    "    sys.exit(3)\n"
    "sys.exit(0)\n"
)


def _probe_exit(path: Path) -> int:
    """Exit status of a child that tries the lock without blocking: 0 free, 3 held."""
    return subprocess.run([sys.executable, "-I", "-c", _PROBE, str(path)], check=False, timeout=60).returncode


@pytest.mark.parametrize("tier", TIERS)
def test_the_lock_file_is_named_by_the_tier_in_the_registry_dir(tmp_path: Path, tier: str) -> None:
    registry = ServiceRegistry(dir=tmp_path, tier=tier)
    assert registry.spawn_lock_path() == tmp_path / f"{tier}_spawn.lock" == tmp_path / spawn_lock_filename(tier)


def test_the_supervisors_start_uses_the_primitives_lock_file() -> None:
    """One lock, one definition: the file the supervisor's ``start()`` takes is the one the primitive hands out."""
    assert ssd._SPAWN_LOCK_FILE == spawn_lock_filename(ssd._REGISTRY_TIER) == "storage_service_spawn.lock"


@pytest.mark.parametrize("tier", TIERS)
def test_a_hold_excludes_a_second_hold_in_this_process_and_a_child(tmp_path: Path, tier: str) -> None:
    registry = ServiceRegistry(dir=tmp_path, tier=tier)
    hold = registry.try_hold_spawn_lock()
    try:
        with pytest.raises(SpawnLockBusyError, match=str(registry.spawn_lock_path())):
            registry.try_hold_spawn_lock()
        assert _probe_exit(registry.spawn_lock_path()) == 3, "a child could take a lock that is held"
    finally:
        hold.release()
    assert _probe_exit(registry.spawn_lock_path()) == 0, "release did not free the lock"
    registry.try_hold_spawn_lock().release()


def test_release_is_idempotent_and_the_context_manager_releases(tmp_path: Path) -> None:
    registry = ServiceRegistry(dir=tmp_path, tier="storage_service")
    hold = registry.try_hold_spawn_lock()
    hold.release()
    hold.release()  # a second release must not close a descriptor that now belongs to someone else
    with registry.try_hold_spawn_lock():
        assert _probe_exit(registry.spawn_lock_path()) == 3
    assert _probe_exit(registry.spawn_lock_path()) == 0


def test_a_holder_that_dies_frees_the_lock(tmp_path: Path) -> None:
    registry = ServiceRegistry(dir=tmp_path, tier="storage_service")
    code = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from nexus.daemon.service_registry import ServiceRegistry\n"
        "h = ServiceRegistry(dir=Path(sys.argv[1]), tier='storage_service').try_hold_spawn_lock()\n"
        "print('held', flush=True)\n"
        "time.sleep(600)\n"
    )
    child = subprocess.Popen([sys.executable, "-I", "-c", code, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        with pytest.raises(SpawnLockBusyError):
            registry.try_hold_spawn_lock()
    finally:
        child.kill()
        child.wait(timeout=30)
    registry.try_hold_spawn_lock().release()


def test_a_supervisor_start_waits_for_a_held_lock_and_runs_when_it_is_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the hold: a real ``StorageServiceSupervisor.start()`` does not get past the lock while it is held."""
    sup = ssd.StorageServiceSupervisor(
        config_dir=tmp_path, binary_path=Path("/fake/nexus-service"), pg_port=1, service_port=0, creds={"NX_SERVICE_TOKEN": "t"},
        engine_liveness_scan=lambda _c, _b: [],
    )
    entered = threading.Event()
    monkeypatch.setattr(sup, "_start_locked", lambda **_kw: entered.set() or {})
    hold = ServiceRegistry(dir=tmp_path, tier="storage_service").try_hold_spawn_lock()
    starter = threading.Thread(target=sup.start, daemon=True)
    try:
        starter.start()
        assert not entered.wait(0.5), "start() ran while the spawn lock was held"
    finally:
        hold.release()
    assert entered.wait(30), "start() did not proceed after the lock was released"
    starter.join(timeout=30)
