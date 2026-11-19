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

import nexus.daemon.service_registry as sr
import nexus.daemon.storage_service_daemon as ssd
from nexus.daemon.service_registry import ProcessSweepResult, ServiceRegistry, service_identity, ttl_for_tier

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups and pid probes")

_INVISIBLE = 999_999_003
_OURS, _THEIRS = "pid:[4026531836]", "pid:[4026532221]"


@pytest.fixture(autouse=True)
def _namespaces(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_a_legacy_lease_whose_endpoint_cmdline_reads_empty_does_not_signal_its_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Linux reads /proc/<pid>/cmdline empty while a just-spawned child is still in execve, so an
    unreadable command line is no evidence the pid is ours: it is not signalled, and the stale lease
    still goes."""
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        monkeypatch.setattr(sr, "process_command", lambda _pid, *, platform=None: "")
        registry = _registry(tmp_path)
        registry.publish(service_identity(), endpoint={"pid": bystander.pid, "port": 1}, version="t", owner_token="tok")
        outcome = ssd.stop_storage_service(config_dir=tmp_path)
        assert bystander.poll() is None, "a pid whose command line could not be read was signalled"
        assert bystander.pid not in outcome.pids
        assert outcome.unreadable == (bystander.pid,)
        assert not outcome.already_stopped
        assert registry.records() == [], "the stale lease is still released"
    finally:
        bystander.kill()
        bystander.wait(timeout=10)


#: A bystander whose /proc cmdline reads empty for real, the way a just-spawned child's does while it is
#: still in execve (the kernel has switched to the new mm and not yet set its arg range): it writes NULs
#: over its own argument area through /proc/self/mem, checks the kernel now reports an all-NUL command
#: line, and says so on stdout. The CI failures of the test above (develop runs 42482720978,
#: 42728701104, 43811582701; returncode -15) were the old stop reading that window as "ours" and sending
#: the group SIGTERM; the window lasts microseconds, so this holds it open instead.
_BLANK_CMDLINE_BYSTANDER = r"""
import sys, time
with open("/proc/self/stat") as f:
    stat = f.read()
fields = stat[stat.rindex(")") + 2:].split()  # fields[0] is proc(5) field 3, so field n is fields[n - 3]
arg_start, arg_end = int(fields[48 - 3]), int(fields[49 - 3])  # proc(5) fields 48 arg_start, 49 arg_end
with open("/proc/self/mem", "r+b", buffering=0) as mem:
    mem.seek(arg_start)
    mem.write(b"\0" * (arg_end - arg_start))
with open("/proc/self/cmdline", "rb") as f:
    raw = f.read()
sys.stdout.write("blank\n" if raw and not raw.strip(b"\0") else "readable %r\n" % raw)
sys.stdout.flush()
time.sleep(60)
"""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc/<pid>/cmdline and /proc/self/mem are Linux")
def test_a_legacy_lease_whose_endpoint_cmdline_really_reads_empty_does_not_signal_its_group(tmp_path: Path) -> None:
    """The same case as the monkeypatched test above, through the real /proc read: no seam, a real
    process whose command line the kernel reports empty is not signalled and is reported unreadable."""
    bystander = subprocess.Popen(
        [sys.executable, "-c", _BLANK_CMDLINE_BYSTANDER], start_new_session=True,
        stdout=subprocess.PIPE, text=True, encoding="utf-8",
    )
    try:
        assert bystander.stdout is not None
        ready = bystander.stdout.readline().strip()
        assert ready == "blank", f"the bystander could not blank its own command line: {ready!r}"
        assert sr.process_command(bystander.pid) == "", "the product's /proc read must see the empty command line"
        registry = _registry(tmp_path)
        registry.publish(service_identity(), endpoint={"pid": bystander.pid, "port": 1}, version="t", owner_token="tok")
        outcome = ssd.stop_storage_service(config_dir=tmp_path)
        assert bystander.poll() is None, "a pid whose command line read empty was signalled"
        assert bystander.pid not in outcome.pids
        assert outcome.unreadable == (bystander.pid,)
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


def test_a_lease_pid_the_sweep_then_stopped_is_not_reported_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The lease check read the endpoint pid empty; the sweep then matched and stopped it.
    The stop reports it as stopped, not also as unidentified and maybe still running."""
    registry = _registry(tmp_path)
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
