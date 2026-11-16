# SPDX-License-Identifier: AGPL-3.0-or-later
"""A legacy lease pid the stop could not identify, then stopped by the sweep (nexus-dyfsw follow-up)."""
from __future__ import annotations

from pathlib import Path

import nexus.daemon.service_registry as sr
import nexus.daemon.storage_service_daemon as ssd
from nexus.daemon.service_registry import ProcessSweepResult, ServiceRegistry, service_identity, ttl_for_tier


def test_a_lease_pid_the_sweep_then_stopped_is_not_reported_unreadable(tmp_path: Path, monkeypatch) -> None:
    """The lease check read the endpoint pid empty; the sweep then matched and stopped it.
    The stop reports it as stopped, not also as unidentified and maybe still running."""
    registry = ServiceRegistry(dir=tmp_path, tier=ssd._REGISTRY_TIER, ttl=ttl_for_tier(ssd._REGISTRY_TIER))
    registry.publish(service_identity(), endpoint={"pid": 4242, "port": 1}, version="t", owner_token="tok")
    monkeypatch.setattr(sr, "process_command", lambda _pid, *, platform=None: "")
    monkeypatch.setattr(ssd, "_pid_is_running", lambda _pid: True)
    monkeypatch.setattr(
        sr, "sweep_matching_processes",
        lambda *_a, **_k: ProcessSweepResult(available=True, error=None, found=((4242, "nexus-service"),), stubborn=()),
    )
    outcome = ssd.stop_storage_service(config_dir=tmp_path)
    assert outcome.pids == (4242,)
    assert outcome.unreadable == ()
