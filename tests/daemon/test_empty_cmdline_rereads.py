# SPDX-License-Identifier: AGPL-3.0-or-later
"""An empty command-line re-read is never grounds to signal (nexus-dyfsw follow-up).

On Linux ``/proc/<pid>/cmdline`` reads empty while a just-spawned process is still in
``execve``, so a pid reused between a process-table snapshot and the signal can read
empty. The process-table sweep and the upgrade's survivor sweep re-read a matched pid
before signalling it; an empty re-read is reported, never signalled.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import nexus.daemon.service_registry as sr
import nexus.daemon.storage_service_daemon as ssd
import nexus.upgrade_finish as uf
from nexus.daemon.binary_lifecycle import well_known_binary_path
from nexus.daemon.service_registry import (
    ProcessSweepResult,
    ServiceRegistry,
    service_identity,
    storage_service_member_check,
    storage_service_stack_matcher,
    sweep_matching_processes,
    ttl_for_tier,
)


def test_the_sweep_reports_a_running_pid_whose_reread_is_empty(tmp_path: Path) -> None:
    cfg = tmp_path / "nexus"
    rows = [(214, 60, f"{well_known_binary_path(cfg)} -Xmx1g")]
    terminated: list[list[int]] = []
    with patch("nexus.daemon.service_registry.all_process_rows", return_value=rows), \
         patch("nexus.daemon.service_registry.process_command", side_effect=lambda _pid: ""), \
         patch("nexus.daemon.service_registry.pid_running", return_value=True), \
         patch("nexus.daemon.service_registry.terminate_pids",
               side_effect=lambda pids, **_: terminated.append(list(pids)) or []):
        result = sweep_matching_processes(
            storage_service_stack_matcher(cfg), member_check=storage_service_member_check(cfg),
        )
    assert terminated == [], "a pid whose command line re-read empty was signalled"
    assert result.pids == ()
    assert result.unreadable == (214,)


def test_the_sweep_drops_an_exited_pid_whose_reread_is_empty(tmp_path: Path) -> None:
    cfg = tmp_path / "nexus"
    rows = [(214, 60, f"{well_known_binary_path(cfg)} -Xmx1g")]
    with patch("nexus.daemon.service_registry.all_process_rows", return_value=rows), \
         patch("nexus.daemon.service_registry.process_command", side_effect=lambda _pid: ""), \
         patch("nexus.daemon.service_registry.pid_running", return_value=False), \
         patch("nexus.daemon.service_registry.terminate_pids") as term:
        result = sweep_matching_processes(
            storage_service_stack_matcher(cfg), member_check=storage_service_member_check(cfg),
        )
    term.assert_not_called()
    assert result.pids == () and result.unreadable == ()


def test_the_upgrade_survivor_sweep_leaves_an_empty_reread_running_and_names_it(tmp_path: Path) -> None:
    cfg = tmp_path / "nexus"
    before = [(196, f"nx daemon service start --foreground --config-dir {cfg}")]
    with patch.object(uf, "_pid_alive", return_value=True), \
         patch.object(uf, "process_state", return_value="S"), \
         patch.object(uf, "process_command", side_effect=lambda _pid: ""), \
         patch.object(uf, "terminate_pids") as term:
        note, refused = uf._sweep_surviving_stack_detail(cfg, before)
    term.assert_not_called()
    assert refused == []
    assert "UNVERIFIED" in note and "196" in note


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
