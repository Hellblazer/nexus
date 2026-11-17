# SPDX-License-Identifier: AGPL-3.0-or-later
"""The client-side holder check (RDR-253 "A holder that no longer exists"): a registry file naming the holder's pid
with the holder's startedAt, then a pid probe whose OS start time is compared with the binding's proc_start, both as
UTC instants to the second. Tenant-free: the machine is a FakeProcesses table except for the one test of the real
``ps`` probe."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from nexus.identity import holder as holder_mod
from nexus.identity.holder import (
    GONE, OTHER_HOST, RUNNING, UNKNOWN, check_holder, holder_process, normalise_instant, os_start_time,
    read_registry_entry,
)
from tests.identity.fakes import HOST, FakeProcesses, ps_style

T0 = "2026-11-17T08:00:00Z"


def _holder(pid: int = 500, started_at: int = 500_000, proc_start: str | None = T0, host: str = HOST) -> dict:
    return {"host": host, "pid": pid, "started_at": started_at, "proc_start": proc_start}


@pytest.fixture
def procs(tmp_path: Path) -> FakeProcesses:
    return FakeProcesses(tmp_path / "sessions")


def test_a_running_holder_with_a_matching_start_is_not_gone(procs: FakeProcesses) -> None:
    procs.start(500, T0, started_at=500_000)
    assert check_holder(_holder(), procs=procs).state == RUNNING


def test_no_registry_file_is_gone(procs: FakeProcesses) -> None:
    """The process exited cleanly, or the machine rebooted (--revive: no registry files at all)."""
    verdict = check_holder(_holder(), procs=procs)
    assert verdict.state == GONE and "no registry file" in verdict.reason


def test_a_crashed_holder_leaves_its_file_and_is_still_gone(procs: FakeProcesses) -> None:
    """A crashed process leaves its registry file behind; the pid probe, not file presence, decides."""
    procs.start(500, T0, started_at=500_000)
    procs.stop(500, keep_registry=True)
    assert (procs.sessions_dir / "500.json").exists()
    verdict = check_holder(_holder(), procs=procs)
    assert verdict.state == GONE and "not running" in verdict.reason


def test_a_pid_reused_after_a_crash_with_a_different_start_time_is_gone(procs: FakeProcesses) -> None:
    """The holder crashed; another process took pid 500 and Claude wrote a registry file for it with the SAME
    startedAt by coincidence of nothing: the binding's proc_start is what tells them apart."""
    procs.start(500, "2026-11-17T09:30:00Z", started_at=500_000)
    verdict = check_holder(_holder(proc_start=T0), procs=procs)
    assert verdict.state == GONE and "reused" in verdict.reason


def test_an_in_place_restart_changes_started_at_and_is_gone(procs: FakeProcesses) -> None:
    """/restart keeps the pid and the OS start time and writes a new startedAt (253-research-10 (1))."""
    procs.start(500, T0, started_at=777_000)
    verdict = check_holder(_holder(started_at=500_000), procs=procs)
    assert verdict.state == GONE and "restarted" in verdict.reason


def test_a_matching_start_time_in_utc_is_not_gone_whatever_the_zone_of_the_test_process(
        procs: FakeProcesses, monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry's procStart is UTC; ``ps`` prints local time. Both are compared as UTC instants, so a process
    whose local time differs from UTC is not found gone."""
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    procs.start(500, T0, started_at=500_000)
    procs.write_registry(500, 500_000, T0)
    assert read_registry_entry(500, procs.sessions_dir).proc_start == T0
    assert check_holder(_holder(), procs=procs).state == RUNNING


def test_the_real_ps_probe_reads_this_process_in_utc_under_any_local_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real ``ps`` against the test's own pid: ``os_start_time`` forces TZ=UTC, so the answer is the same
    instant in any zone, and equals the process start time Python computes from the OS."""
    if os.name != "posix":
        pytest.skip("ps")
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    tokyo = os_start_time(os.getpid())
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    la = os_start_time(os.getpid())
    assert tokyo is not None and tokyo == la
    utc_ps = subprocess.run(["ps", "-o", "lstart=", "-p", str(os.getpid())], capture_output=True, text=True,
                            env={**os.environ, "TZ": "UTC", "LC_ALL": "C"}).stdout
    assert tokyo == normalise_instant(utc_ps)


def test_a_start_time_that_cannot_be_read_is_unknown_never_gone(procs: FakeProcesses) -> None:
    procs.start(500, T0, started_at=500_000)
    procs.os_start = lambda pid: None  # type: ignore[method-assign]
    assert check_holder(_holder(), procs=procs).state == UNKNOWN


def test_windows_is_an_explicit_refusal_to_compare_start_times() -> None:
    """No ps on Windows: the probe says so by answering None, and the check then answers unknown for a running
    holder rather than guessing. A missing file or a dead pid is still gone there (no start time needed)."""
    assert os_start_time(1234, platform="win32") is None


def test_a_holder_on_another_host_is_judged_by_its_lease_alone(procs: FakeProcesses) -> None:
    verdict = check_holder(_holder(host="elsewhere"), procs=procs)
    assert verdict.state == OTHER_HOST


def test_a_binding_with_no_start_time_is_running_when_the_pid_is(procs: FakeProcesses) -> None:
    procs.start(500, T0, started_at=500_000)
    assert check_holder(_holder(proc_start=None), procs=procs).state == RUNNING


def test_holder_process_is_the_four_fields_the_engine_compares() -> None:
    held = _holder() | {"session_id": "s", "pane": "p"}
    assert holder_process(held) == {"host": HOST, "pid": 500, "started_at": 500_000, "proc_start": T0}


def test_normalise_instant_reads_ps_and_iso_forms() -> None:
    assert normalise_instant(ps_style(T0)) == T0
    assert normalise_instant("Fri Nov  3 05:06:07 2026") == "2026-11-03T05:06:07Z"  # padded day, as ps prints it
    assert normalise_instant("2026-11-17T08:00:00Z") == T0
    assert normalise_instant("2026-11-17T00:00:00-08:00") == T0
    assert normalise_instant("") is None and normalise_instant(None) is None and normalise_instant("not a time") is None


def test_the_one_liveness_probe_is_pid_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default ProcessTable answers liveness through service_registry.pid_alive and nothing else."""
    seen: list[int] = []
    import nexus.daemon.service_registry as reg

    monkeypatch.setattr(reg, "pid_alive", lambda pid, **kw: seen.append(pid) or True)
    assert holder_mod.Processes(sessions_dir=Path("/nonexistent"), host=HOST).alive(321) is True
    assert seen == [321]
