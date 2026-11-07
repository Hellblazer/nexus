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
_OURS, _THEIRS = "pid:[4026531836]", "pid:[4026532221]"


@pytest.fixture(autouse=True)
def _namespaces(monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.daemon.service_registry as sr

    monkeypatch.setattr(sr, "own_pid_namespace", lambda: _OURS)


def _registry(cfg: Path) -> ServiceRegistry:
    return ServiceRegistry(dir=cfg, tier=ssd._REGISTRY_TIER, ttl=ttl_for_tier(ssd._REGISTRY_TIER))


def test_a_heartbeating_lease_whose_supervisor_is_invisible_is_kept_and_reported(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.publish(
        service_identity(), endpoint={"pid": _INVISIBLE + 1, "port": 1}, version="t",
        owner_token="tok", payload={"supervisor_pid": _INVISIBLE, "pid_ns": _THEIRS},
    )
    outcome = ssd.stop_storage_service(config_dir=tmp_path)
    assert outcome.invisible == (_INVISIBLE,)
    assert outcome.sweep_verified, "the sweep ran; an invisible supervisor is not an unread process table"
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


def test_a_killed_supervisor_in_our_own_namespace_is_not_called_invisible(tmp_path: Path) -> None:
    # kill -9, then a stop inside the lease TTL: the old outcome, not "another PID namespace".
    registry = _registry(tmp_path)
    registry.publish(
        service_identity(), endpoint={"pid": _INVISIBLE + 1, "port": 1}, version="t",
        owner_token="tok", payload={"supervisor_pid": _INVISIBLE, "pid_ns": _OURS},
    )
    outcome = ssd.stop_storage_service(config_dir=tmp_path)
    assert outcome.invisible == ()


def test_the_replacement_is_blocked_while_an_invisible_supervisor_runs() -> None:
    from nexus.daemon.replace_quiesce import blocked_message

    outcome = ssd.StopOutcome(pids=(), stubborn=(), source="none", lease_seen=True, invisible=(_INVISIBLE,))
    message = blocked_message(outcome, replacing="engine")
    assert message is not None and "not visible from here" in message and "No file was replaced" in message
    clean = ssd.StopOutcome(pids=(), stubborn=(), source="none")
    assert blocked_message(clean, replacing="engine") is None


def test_service_stop_exits_1_and_says_why_for_an_invisible_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from nexus.commands import daemon as daemon_mod

    outcome = ssd.StopOutcome(pids=(), stubborn=(), source="none", lease_seen=True, invisible=(_INVISIBLE,))
    monkeypatch.setattr(ssd, "stop_storage_service", lambda **kw: outcome)
    result = CliRunner().invoke(daemon_mod.service_group, ["stop", "--config-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "not visible from here" in result.output
    assert "already stopped" not in result.output
    assert "process table could not be checked" not in result.output


def test_a_live_local_pid_with_the_containers_number_is_not_signalled(tmp_path: Path) -> None:
    # The lease's supervisor pid is from another namespace; the same number here is a bystander.
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        registry = _registry(tmp_path)
        registry.publish(
            service_identity(), endpoint={"pid": bystander.pid, "port": 1}, version="t",
            owner_token="tok", payload={"supervisor_pid": bystander.pid, "pid_ns": _THEIRS},
        )
        outcome = ssd.stop_storage_service(config_dir=tmp_path)
        assert bystander.poll() is None, "a pid from another namespace was signalled here"
        assert outcome.invisible == (bystander.pid,)
        assert len(registry.records()) == 1
    finally:
        bystander.kill()
        bystander.wait(timeout=10)
