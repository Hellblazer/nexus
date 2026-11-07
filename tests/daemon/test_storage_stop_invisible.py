# SPDX-License-Identifier: AGPL-3.0-or-later
"""The storage-service stop and a supervisor it cannot see, or an endpoint pid that is not ours (nexus-dyfsw).

A supervisor in another PID namespace sharing the config directory heartbeats a lease whose pid
is absent here; a legacy lease's endpoint pid may have been reused by an unrelated process.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import nexus.daemon.storage_service_daemon as ssd
from nexus.daemon.service_registry import ServiceRegistry, service_identity, ttl_for_tier

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups and pid probes")

_INVISIBLE = 999_999_003


def _registry(cfg: Path) -> ServiceRegistry:
    return ServiceRegistry(dir=cfg, tier=ssd._REGISTRY_TIER, ttl=ttl_for_tier(ssd._REGISTRY_TIER))


def test_a_heartbeating_lease_whose_supervisor_is_invisible_is_kept_and_reported(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.publish(
        service_identity(), endpoint={"pid": _INVISIBLE + 1, "port": 1}, version="t",
        owner_token="tok", payload={"supervisor_pid": _INVISIBLE},
    )
    outcome = ssd.stop_storage_service(config_dir=tmp_path)
    assert outcome.invisible == (_INVISIBLE,)
    assert outcome.pids == () and not outcome.already_stopped
    assert len(registry.records()) == 1, "the lease of a supervisor alive elsewhere is kept"


def test_a_legacy_lease_naming_someone_elses_pid_does_not_signal_its_group(tmp_path: Path) -> None:
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        registry = _registry(tmp_path)
        registry.publish(service_identity(), endpoint={"pid": bystander.pid, "port": 1}, version="t", owner_token="tok")
        outcome = ssd.stop_storage_service(config_dir=tmp_path)
        assert bystander.poll() is None, "the reused pid's process group was signalled"
        assert bystander.pid not in outcome.pids
        assert registry.records() == [], "the stale lease is still released"
    finally:
        bystander.kill()
        bystander.wait(timeout=10)
