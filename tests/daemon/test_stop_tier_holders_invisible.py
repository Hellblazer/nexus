# SPDX-License-Identifier: AGPL-3.0-or-later
"""stop_tier_holders keeps the record of a pid it cannot see while that record still heartbeats (nexus-dyfsw).

A holder in another PID namespace (a container sharing the config directory) has a pid that is
absent here; relinquishing its fresh lease would let a second holder start beside it.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

import nexus.daemon.service_registry as sr
from nexus.daemon.service_registry import ServiceRegistry, stop_tier_holders

_TIER = "test_tier_dyfsw"
_PID = 999_999_001  # absent on this box


@pytest.fixture(autouse=True)
def _absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sr, "pid_running", lambda pid: False)


def _publish(cfg: Path, *, at: float) -> ServiceRegistry:
    registry = ServiceRegistry(dir=cfg, tier=_TIER, clock=lambda: at, ttl=5.0)
    registry.publish("default", endpoint={"pid": _PID}, version="t", owner_token="tok")
    return ServiceRegistry(dir=cfg, tier=_TIER, ttl=5.0)


def _stop(cfg: Path):
    return stop_tier_holders(dir=cfg, tier=_TIER, is_ours=lambda cmd: True, command=lambda pid: "")


def test_a_heartbeating_record_of_an_invisible_pid_is_kept(tmp_path: Path) -> None:
    registry = _publish(tmp_path, at=time.time())
    result = _stop(tmp_path)
    assert result.invisible == (_PID,)
    assert result.stopped == () and result.survivors == ()
    assert len(registry.records()) == 1


def test_a_stale_record_of_an_absent_pid_is_dropped_as_before(tmp_path: Path) -> None:
    registry = _publish(tmp_path, at=time.time() - 3600)
    result = _stop(tmp_path)
    assert result.invisible == ()
    assert registry.records() == []
