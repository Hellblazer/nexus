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


_OURS, _THEIRS = "pid:[4026531836]", "pid:[4026532221]"


@pytest.fixture(autouse=True)
def _absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sr, "pid_running", lambda pid: False)
    monkeypatch.setattr(sr, "own_pid_namespace", lambda: _OURS)


def _publish(cfg: Path, *, at: float, ns: str | None = _THEIRS) -> ServiceRegistry:
    registry = ServiceRegistry(dir=cfg, tier=_TIER, clock=lambda: at, ttl=5.0)
    registry.publish(
        "default", endpoint={"pid": _PID}, version="t", owner_token="tok",
        payload={"pid_ns": ns} if ns else {"pid_ns": ""},
    )
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


def test_an_absent_pid_from_our_own_namespace_is_a_dead_holder(tmp_path: Path) -> None:
    # A SIGKILLed holder whose lease has not aged out yet: dropped as before, never "invisible".
    registry = _publish(tmp_path, at=time.time(), ns=_OURS)
    result = _stop(tmp_path)
    assert result.invisible == ()
    assert registry.records() == []


def test_a_lease_with_no_namespace_recorded_is_judged_as_before(tmp_path: Path) -> None:
    # An older client, macOS or Windows records none: the pid rule of before applies.
    registry = _publish(tmp_path, at=time.time(), ns=None)
    assert _stop(tmp_path).invisible == ()
    assert registry.records() == []


def test_publish_records_the_publishers_namespace(tmp_path: Path) -> None:
    registry = ServiceRegistry(dir=tmp_path, tier=_TIER, ttl=5.0)
    record = registry.publish("default", endpoint={"pid": 1}, version="t", owner_token="tok")
    assert record.payload["pid_ns"] == _OURS


def test_a_linux_lease_read_where_there_are_no_pid_namespaces_is_other(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # macOS or Windows reading a Linux container's lease through a shared directory.
    monkeypatch.setattr(sr, "own_pid_namespace", lambda: None)
    monkeypatch.setattr(sr.sys, "platform", "darwin")
    registry = _publish(tmp_path, at=time.time())
    assert _stop(tmp_path).invisible == (_PID,)
    assert len(registry.records()) == 1


def test_a_linux_reader_that_cannot_read_its_namespace_keeps_the_old_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sr, "own_pid_namespace", lambda: None)
    monkeypatch.setattr(sr.sys, "platform", "linux")
    registry = _publish(tmp_path, at=time.time())
    assert _stop(tmp_path).invisible == ()
    assert registry.records() == []
