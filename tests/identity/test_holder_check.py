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


@pytest.mark.skipif(os.name != "posix", reason="the POSIX ps probe: argv_off_cwd finds no ps on Windows")
def test_ps_is_run_under_utc_so_a_local_zone_never_reaches_the_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``ps`` stand-in that prints local time (Pacific, 8 hours behind) unless the probe's environment says
    ``TZ=UTC``: the probe must say it, and the instant it returns is the UTC one. Without the variable this reads
    ``2026-11-17T00:00:00Z`` and finds every holder gone."""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    seen: list[dict] = []

    class _Done:
        def __init__(self, out: str) -> None:
            self.stdout = out

    def fake_ps(argv, *, timeout, env, **kw):
        seen.append(env)
        local = env.get("TZ") != "UTC"
        return _Done("Tue Nov 17 00:00:00 2026\n" if local else ps_style(T0) + "\n")

    assert os_start_time(500, platform="darwin", run=fake_ps) == T0
    assert seen[0]["TZ"] == "UTC" and seen[0]["LC_ALL"] == "C"


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


def test_windows_reads_the_process_creation_time_as_a_fixed_utc_instant() -> None:
    """No ps on Windows: the start time is the process creation FILETIME, floored to the second, the same instant on
    every call (``process_age_seconds`` moves; this does not), so the pending file, ``proc_start`` and the in-place
    ``/restart`` rebind work there."""
    unix = 1_794_902_400 + 0.7                         # 2026-11-17T08:00:00Z plus a fraction
    filetime = int((unix + 11_644_473_600) * 10_000_000)

    class _Api:
        def open_process(self, pid): return (7, 0) if pid == 1234 else (None, 5)
        def close(self, handle): pass
        def creation_filetime(self, handle): return filetime

    assert os_start_time(1234, platform="win32", win_api=_Api()) == T0
    assert os_start_time(1234, platform="win32", win_api=_Api()) == T0
    assert os_start_time(999, platform="win32", win_api=_Api()) is None


def test_a_holder_registered_under_another_claude_config_dir_is_never_judged_gone_by_this_one(
        procs: FakeProcesses, tmp_path: Path) -> None:
    """A Claude under another CLAUDE_CONFIG_DIR writes its registry file somewhere this reader does not look: an absent
    file proves nothing. Gone only when the holder's own token file names THIS registry directory; unknown otherwise,
    including a holder with no token file at all."""
    from nexus.identity.token_file import TokenFile, token_path, write_token_file

    d = tmp_path / "identity"

    def token(registry_dir):
        write_token_file(token_path(d, 500, 500_000), TokenFile(
            identity="i1", generation=1, token=None, expires_at=None, session_id="s", pid=500, started_at=500_000,
            proc_start=T0, host=HOST, registry_dir=registry_dir))

    assert check_holder(_holder(), procs=procs, identity_dir=d).state == UNKNOWN              # no token file
    token(str(tmp_path / "other-profile" / "sessions"))
    assert check_holder(_holder(), procs=procs, identity_dir=d).state == UNKNOWN              # another profile's
    token(str(procs.sessions_dir))
    assert check_holder(_holder(), procs=procs, identity_dir=d).state == GONE                 # this reader's own


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
