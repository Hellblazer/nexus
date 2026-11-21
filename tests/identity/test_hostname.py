# SPDX-License-Identifier: AGPL-3.0-or-later
"""The host a binding names (GitHub issue 1679): a key that survives the network changing the hostname, and the holder
check and SessionStart policy that read it. Tenant-free: the machine is a ``FakeProcesses`` table, the engine is
``tests/identity/fake_engine.py`` (the engine-backed rows are in ``test_session_start_bind.py``)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest
import structlog.testing

from nexus.identity import hostname
from nexus.identity.delegated import check_dispatcher, delegated_token_path
from nexus.identity.binder import LaunchFacts, SessionBinder
from nexus.identity.holder import GONE, OTHER_HOST, RUNNING, Processes, check_holder, normalise_instant, read_registry_entry, this_host
from nexus.identity.ops import CallerFacts, IdentityEnv, _holder_line, judge
from nexus.identity.resolver import _by_registry
from nexus.identity.sweep import sweep_host
from nexus.identity.token_file import TokenFile, token_path, write_token_file
from nexus.identity.work import _process_gone
from nexus.mcp.identity_lease import IdentityLease
from tests._module_seam import setattr_in
from tests.identity.fake_engine import FakeEngine
from tests.identity.fakes import FakeProcesses

T0 = "2026-11-17T08:00:00Z"
T1 = "2026-11-17T09:00:00Z"
MAC_UUID = "5B2D6B0E-0C93-5D1B-9E7A-3F6B2A9C41D7"
MACHINE_ID = "0123456789abcdef0123456789abcdef"


def _machine(monkeypatch: pytest.MonkeyPatch, *, stable: str | None, name: str) -> None:
    """This machine as the code sees it: the stable key (None: the probe finds no identifier) and the ``gethostname()``."""
    monkeypatch.setattr(hostname, "_stable_host_cached", lambda: stable)
    setattr_in(monkeypatch, hostname, "socket.gethostname", lambda: name)


# ── the key ─────────────────────────────────────────────────────────────────


def _mac_ioreg(uuid: str) -> Any:
    class _Out:
        stdout = f'+-o Root  <class IORegistryEntry>\n  {{\n    "IOPlatformUUID" = "{uuid}"\n    "IOPlatformSerialNumber" = "X"\n  }}\n'
    return lambda *a, **k: _Out()


def test_the_macos_key_comes_from_the_hardware_uuid_and_hides_it() -> None:
    key = hostname.stable_host(platform="darwin", run=_mac_ioreg(MAC_UUID))
    assert key == hostname.host_key("ioplatformuuid", MAC_UUID) and key.startswith(hostname.KEY_PREFIX)
    assert MAC_UUID.lower() not in key.lower() and len(key) == len(hostname.KEY_PREFIX) + 32


def test_a_failed_or_garbled_ioreg_gives_no_key() -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise OSError("no ioreg")
    assert hostname.stable_host(platform="darwin", run=boom) is None
    assert hostname.stable_host(platform="darwin", run=_mac_ioreg("not-a-uuid")) is None


def _linux(text: str | None, *, ns: str = hostname.INIT_PID_NS) -> dict[str, Any]:
    def read(path: Path) -> str:
        if text is None:
            raise FileNotFoundError(path)
        return text
    return {"platform": "linux", "read": read, "pid_ns": lambda: ns}


def test_the_linux_key_comes_from_machine_id_in_the_initial_pid_namespace() -> None:
    key = hostname.stable_host(**_linux(MACHINE_ID + "\n"))
    assert key == hostname.host_key("machine-id", MACHINE_ID)


@pytest.mark.parametrize("kwargs", [
    pytest.param(_linux(MACHINE_ID, ns="pid:[4026532999]"), id="a container's pid namespace shares the host's machine-id file"),
    pytest.param(_linux(""), id="empty machine-id"),
    pytest.param(_linux("uninitialized\n"), id="uninitialized machine-id"),
    pytest.param(_linux("0" * 32), id="all-zero machine-id"),
    pytest.param(_linux("ABC"), id="malformed machine-id"),
    pytest.param(_linux(None), id="no machine-id file"),
    pytest.param({"platform": "freebsd14"}, id="an OS with no probe"),
])
def test_an_identifier_two_process_tables_could_share_gives_no_key(kwargs: dict[str, Any]) -> None:
    """A false same-host verdict lets this machine's pid probe judge a holder on another: no key is better than that one."""
    assert hostname.stable_host(**kwargs) is None


def test_the_windows_key_comes_from_machine_guid() -> None:
    guid = "6f1c1d0a-3b6e-4f0a-8c36-1f0a9d7e2b11"
    assert hostname.stable_host(platform="win32", windows_guid=lambda: guid) == hostname.host_key("machineguid", guid)
    assert hostname.stable_host(platform="win32", windows_guid=lambda: None) is None


def test_two_machines_never_share_a_key_unless_they_share_an_identifier() -> None:
    """Distinct identifiers give distinct keys, one identifier gives one key (case and padding do not matter), and the
    source of the identifier is part of the key, so a Linux machine-id can never equal a Mac's UUID."""
    other = "5B2D6B0E-0C93-5D1B-9E7A-3F6B2A9C41D8"
    keys = {hostname.host_key("ioplatformuuid", MAC_UUID), hostname.host_key("ioplatformuuid", other),
            hostname.host_key("machine-id", MAC_UUID), hostname.host_key("machineguid", MAC_UUID)}
    assert len(keys) == 4
    assert hostname.host_key("ioplatformuuid", MAC_UUID) == hostname.host_key("ioplatformuuid", f" {MAC_UUID.lower()}\n")


def test_this_host_does_not_follow_the_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    key = hostname.host_key("ioplatformuuid", MAC_UUID)
    _machine(monkeypatch, stable=key, name="ip-10-122-0-102.ec2.internal")
    before = this_host()
    _machine(monkeypatch, stable=key, name="ip-10-122-0-105.ec2.internal")
    assert this_host() == before == key


def test_with_no_key_this_host_is_the_hostname_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    _machine(monkeypatch, stable=None, name="plain-host")
    assert this_host() == "plain-host" and hostname.listing_names("plain-host") == ("plain-host",)


def test_listing_asks_under_the_key_the_hostname_and_the_hosts_the_token_files_record(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A listing is not a judgement: it may ask under every name the machine is known by, including one it no longer has."""
    key = hostname.host_key("machine-id", MACHINE_ID)
    _machine(monkeypatch, stable=key, name="box-1")
    assert hostname.listing_names(key) == (key, "box-1")
    assert hostname.listing_names("somebody-elses") == ("somebody-elses",)
    _token(tmp_path / "identity", 500, host="box-0")
    assert hostname.listing_names(key, tmp_path / "identity") == (key, "box-1", "box-0")


# ── locality ────────────────────────────────────────────────────────────────


def _holder(host: str, pid: int = 500) -> dict[str, Any]:
    return {"host": host, "pid": pid, "started_at": pid * 1000, "proc_start": T0}


def _token(idir: Path, pid: int, *, host: str, procs: FakeProcesses | None = None, identity: str = "i1") -> None:
    """The token file this machine's hook wrote when it bound (pid, pid*1000) under *host*."""
    write_token_file(token_path(idir, pid, pid * 1000), TokenFile(
        identity=identity, generation=1, token=None, expires_at=None, session_id="s", pid=pid, started_at=pid * 1000,
        proc_start=T0, host=host, registry_dir=str(procs.sessions_dir) if procs else None))


def test_a_key_is_local_only_when_it_is_this_machines(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    key, other = hostname.host_key("ioplatformuuid", MAC_UUID), hostname.host_key("ioplatformuuid", MAC_UUID[:-1] + "8")
    _machine(monkeypatch, stable=key, name="box-1")
    _token(tmp_path / "identity", 500, host=other)        # even a token file naming another key proves nothing
    assert hostname.holder_is_local(_holder(key), host=key)
    assert not hostname.holder_is_local(_holder(other), host=key, identity_dir=tmp_path / "identity")


def test_a_name_that_is_no_key_is_local_only_on_proof(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    key = hostname.host_key("ioplatformuuid", MAC_UUID)
    _machine(monkeypatch, stable=key, name="ubuntu")
    idir = tmp_path / "identity"
    assert not hostname.holder_is_local(_holder("ubuntu"), host=key, identity_dir=idir), "the current hostname alone is not proof"
    assert not hostname.holder_is_local(_holder("ubuntu"), host=key), "no identity dir, no proof"
    _token(idir, 500, host="ubuntu")
    assert hostname.holder_is_local(_holder("ubuntu"), host=key, identity_dir=idir), "this machine's own token file is"
    assert not hostname.holder_is_local(_holder("ubuntu", pid=501), host=key, identity_dir=idir), "another pid's is not"
    assert not hostname.holder_is_local(_holder("elsewhere"), host=key, identity_dir=idir), "a file that records another host is not"
    assert hostname.holder_is_local(_holder("ubuntu", pid=502), host=key, process=(502, 502_000)), "the asking process itself is"
    assert not hostname.holder_is_local(_holder("ubuntu", pid=502), host=key, process=(502, 1)), "a process with another startedAt is not"


# ── the holder check ────────────────────────────────────────────────────────


def test_the_holder_check_calls_a_dead_pid_gone_whatever_the_hostname_became(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The issue: a holder recorded on day one, the hostname different on day two. The key is the same, so the holder is
    on this machine and its dead pid is found gone."""
    key = hostname.host_key("ioplatformuuid", MAC_UUID)
    _machine(monkeypatch, stable=key, name="ip-10-122-0-102.ec2.internal")
    recorded = _holder(this_host())
    _machine(monkeypatch, stable=key, name="ip-10-122-0-105.ec2.internal")
    procs = FakeProcesses(tmp_path / "sessions", host=this_host())
    assert check_holder(recorded, procs=procs).state == GONE


def test_a_row_under_a_vanished_name_this_machine_wrote_is_judged_here(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The row says ``.102``, the machine is ``.105`` now, and the hook's own token file for that process records ``.102``."""
    key = hostname.host_key("ioplatformuuid", MAC_UUID)
    _machine(monkeypatch, stable=key, name="ip-10-122-0-105.ec2.internal")
    procs = FakeProcesses(tmp_path / "sessions", host=this_host())
    idir = tmp_path / "identity"
    _token(idir, 500, host="ip-10-122-0-102.ec2.internal", procs=procs)
    assert check_holder(_holder("ip-10-122-0-102.ec2.internal"), procs=procs, identity_dir=idir).state == GONE
    procs.start(500, T0, started_at=500_000)
    assert check_holder(_holder("ip-10-122-0-102.ec2.internal"), procs=procs, identity_dir=idir).state == RUNNING


@pytest.mark.parametrize("holder_host", [
    pytest.param(hostname.host_key("ioplatformuuid", "5B2D6B0E-0C93-5D1B-9E7A-3F6B2A9C41D8"), id="another machine's key"),
    pytest.param("ip-10-122-0-102.ec2.internal", id="a name this machine no longer has, no proof"),
    pytest.param("box-1", id="a hostname equal to this machine's, no proof"),
])
def test_a_holder_on_another_machine_is_judged_by_its_lease_alone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, holder_host: str) -> None:
    """The dangerous direction: this machine has no process 500 and no registry file for it, which is what 'gone' would
    read, but the holder is not on this machine."""
    _machine(monkeypatch, stable=hostname.host_key("ioplatformuuid", MAC_UUID), name="box-1")
    procs = FakeProcesses(tmp_path / "sessions", host=this_host())
    assert check_holder(_holder(holder_host), procs=procs, identity_dir=tmp_path / "identity").state == OTHER_HOST


def test_a_delegated_dispatcher_under_a_vanished_name_is_judged_by_its_own_token_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _machine(monkeypatch, stable=hostname.host_key("ioplatformuuid", MAC_UUID), name="ip-105")
    procs = FakeProcesses(tmp_path / "sessions", host=this_host())
    idir = tmp_path / "identity"
    holder = {"host": "ip-102", "pid": 700, "started_at": 700_000, "proc_start": T0}
    assert check_dispatcher(holder, procs=procs, identity_dir=idir, identity="i9").state == OTHER_HOST
    write_token_file(delegated_token_path(idir, "i9"), TokenFile(
        identity="i9", generation=1, token=None, expires_at=None, session_id=None, pid=700, started_at=700_000,
        proc_start=T0, host="ip-102"))
    assert check_dispatcher(holder, procs=procs, identity_dir=idir, identity="i9").state == GONE
    assert check_dispatcher(holder, procs=procs, identity_dir=idir, identity="i8").state == OTHER_HOST


def test_the_sweep_finds_and_records_a_dead_holder_under_a_vanished_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Reverting the listing (the engine asked under the current name only) or the proof (the row judged as another
    host) leaves the dead holder unrecorded."""
    key = hostname.host_key("ioplatformuuid", MAC_UUID)
    _machine(monkeypatch, stable=key, name="ip-105")
    procs = FakeProcesses(tmp_path / "sessions", host=this_host())
    idir = tmp_path / "identity"
    engine = FakeEngine()
    held = engine.stage(kind="ephemeral", state="live", generation=1, holder={
        "host": "ip-102", "pid": 500, "started_at": 500_000, "proc_start": T0, "session_id": "s"})
    stranger = engine.stage(kind="ephemeral", state="live", generation=1, holder={
        "host": "ip-102", "pid": 501, "started_at": 501_000, "proc_start": T0, "session_id": "t"})
    _token(idir, 500, host="ip-102", procs=procs)                 # this machine wrote the first; nothing proves the second
    report = sweep_host(engine, procs, host=this_host(), own=None, identity_dir=idir)
    assert report.recorded_gone == [held] and report.other_host == 1
    assert engine.bound(held)["state"] == "lapsed" and engine.bound(stranger)["state"] == "live"


def test_the_resolver_finds_the_identity_of_a_process_bound_under_a_vanished_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    key = hostname.host_key("ioplatformuuid", MAC_UUID)
    _machine(monkeypatch, stable=key, name="ip-105")
    procs = FakeProcesses(tmp_path / "sessions", host=this_host())
    procs.start(500, T0, started_at=500_000, session_id="sid-x")
    idir = tmp_path / "identity"
    engine = FakeEngine()
    mine = engine.stage(kind="ephemeral", state="live", generation=1, holder={
        "host": "ip-102", "pid": 500, "started_at": 500_000, "proc_start": T0, "session_id": "old"})
    _token(idir, 500, host="ip-102", procs=procs)
    assert _by_registry(engine, "sid-x", 500, procs.sessions_dir, this_host(), tmp_path / "nowhere") is None
    found = _by_registry(engine, "sid-x", 500, procs.sessions_dir, this_host(), idir)
    assert found is not None and found.identity == mine


# ── work rows ───────────────────────────────────────────────────────────────


def test_a_labrun_ref_naming_the_short_hostname_is_judged_on_a_keyed_machine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """lab-remote writes ``<gethostname() up to the first dot>:<pid>:<started>``; ``procs.host`` is the key now."""
    key = hostname.host_key("ioplatformuuid", MAC_UUID)
    _machine(monkeypatch, stable=key, name="constantine.local")
    procs = FakeProcesses(tmp_path / "sessions", host=this_host())
    assert _process_gone(procs, this_host(), "constantine", 999, start=None, began=1.0) == (True, True)
    assert _process_gone(procs, this_host(), "somebody-elses", 999, start=None, began=1.0) == (False, False)
    assert _process_gone(procs, this_host(), key, 999, start=None, began=None) == (True, True)


# ── the lease ───────────────────────────────────────────────────────────────


def _lease(tmp_path: Path, procs: FakeProcesses) -> Any:
    return IdentityLease(client_factory=lambda: FakeEngine(), config_dir=tmp_path / "config", claude_pid=100, procs=procs,
                         clock=lambda: 1_000.0, monotonic=lambda: 1_000.0)


def test_the_lease_refreshes_under_the_host_its_own_token_file_records(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The engine compares ``host`` as a string on refresh and fork: a process speaks under the host its own binding
    records (its token file is named by its own pid and startedAt), a pre-key hostname or a key alike."""
    key = hostname.host_key("ioplatformuuid", MAC_UUID)
    _machine(monkeypatch, stable=key, name="ip-105")
    procs = FakeProcesses(tmp_path / "sessions", host=this_host())
    procs.start(100, T0)
    idir = tmp_path / "config" / "identity"
    lease = _lease(tmp_path, procs)
    assert lease._process_evidence()["host"] == key, "no token file: the machine's own name"
    _token(idir, 100, host="ip-102", procs=procs)
    assert lease._process_evidence()["host"] == "ip-102"
    _token(idir, 100, host=key, procs=procs)
    assert lease._process_evidence()["host"] == key


def test_a_lease_built_under_a_failed_probe_speaks_the_key_once_the_probe_recovers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The MCP server's Processes was built while ``ioreg`` failed (a sandbox, a transient); the hook bound the row under the
    key. The probe recovers on its retry: the lease's evidence host follows the file and the machine, not the construction."""
    key = hostname.host_key("ioplatformuuid", MAC_UUID)
    _machine(monkeypatch, stable=None, name="constantine.local")
    FakeProcesses(tmp_path / "sessions", host="x").start(100, T0)         # the registry file of pid 100
    procs = Processes(sessions_dir=tmp_path / "sessions")                 # the real class, default host
    assert procs.host == "constantine.local"
    lease = _lease(tmp_path, procs)
    assert lease._process_evidence()["host"] == "constantine.local"
    _machine(monkeypatch, stable=key, name="constantine.local")           # the probe recovered
    assert procs.host == key and lease._process_evidence()["host"] == key
    _token(tmp_path / "config" / "identity", 100, host=key, procs=procs)
    assert lease._process_evidence()["host"] == key


def test_a_hostname_that_looks_like_a_key_is_never_read_as_one(monkeypatch: pytest.MonkeyPatch) -> None:
    _machine(monkeypatch, stable=None, name="nxh1-0123456789abcdef0123456789abcdef")
    assert not hostname.is_key(this_host()) and this_host() == "host-nxh1-0123456789abcdef0123456789abcdef"
    assert hostname.holder_is_local({"host": "nxh1-0123456789abcdef0123456789abcdef", "pid": 1, "started_at": 1},
                                    host=this_host()) is False


def test_the_operator_commands_judge_a_delegated_dispatcher_under_a_vanished_name_as_the_sweep_does(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``nx identity alive`` and ``release`` go through ``ops.judge``: it must pass the identity dir and id to the dispatcher
    check, or it reads a pre-key dispatcher as another host's while the sweep records it gone."""
    _machine(monkeypatch, stable=hostname.host_key("ioplatformuuid", MAC_UUID), name="ip-105")
    procs = FakeProcesses(tmp_path / "sessions", host=this_host())
    idir = tmp_path / "identity"
    write_token_file(delegated_token_path(idir, "i9"), TokenFile(
        identity="i9", generation=1, token=None, expires_at=None, session_id=None, pid=700, started_at=700_000,
        proc_start=T0, host="ip-102"))
    env = IdentityEnv(client=FakeEngine(), procs=procs, identity_dir=idir, environ={},
                      caller=lambda: CallerFacts(claude_pid=0, ancestors=()))
    view = {"id": "i9", "kind": "delegated",
            "holder": {"host": "ip-102", "pid": 700, "started_at": 700_000, "proc_start": T0}}
    assert judge(env, view).state == GONE


# ── the probe ───────────────────────────────────────────────────────────────


def _reset_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hostname, "_probe", {"key": None, "failed_at": None, "warned": False})


def test_a_failed_probe_is_retried_once_then_not_asked_for_a_minute_and_a_success_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_probe(monkeypatch)
    answers = iter([None, None, None, None, "nxh1-late"])
    calls = []
    monkeypatch.setattr(hostname, "stable_host", lambda: (calls.append(1), next(answers))[1])
    clock = [100.0]
    setattr_in(monkeypatch, hostname, "time.monotonic", lambda: clock[0])
    assert hostname._stable_host_cached() is None and len(calls) == 2, "one retry at once"
    assert hostname._stable_host_cached() is None and len(calls) == 2, "then a short negative cache, not the process's life"
    clock[0] += hostname.PROBE_RETRY_S + 1
    assert hostname._stable_host_cached() is None and len(calls) == 4
    clock[0] += hostname.PROBE_RETRY_S + 1
    assert hostname._stable_host_cached() == "nxh1-late" and hostname._stable_host_cached() == "nxh1-late" and len(calls) == 5


def test_a_missing_key_on_macos_is_logged_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_probe(monkeypatch)
    monkeypatch.setattr(hostname, "stable_host", lambda: None)
    setattr_in(monkeypatch, hostname, "sys.platform", "darwin")
    clock = [0.0]
    setattr_in(monkeypatch, hostname, "time.monotonic", lambda: clock[0])
    with structlog.testing.capture_logs() as logs:
        hostname._stable_host_cached()
        clock[0] += hostname.PROBE_RETRY_S + 1
        hostname._stable_host_cached()
    assert [e["event"] for e in logs].count("host_key_unavailable") == 1


def test_an_unreadable_machine_id_never_raises() -> None:
    def garbage(path: Path) -> str:
        raise UnicodeDecodeError("ascii", b"\xff", 0, 1, "ordinal not in range")
    assert hostname.stable_host(**{**_linux(None), "read": garbage}) is None


def test_an_empty_ioreg_dictionary_gives_no_key() -> None:
    """A sandbox that denies IOKit makes ``ioreg`` exit 0 with an empty dictionary."""
    class _Out:
        stdout = "+-o Root  <class IORegistryEntry>\n  {\n  }\n"
    assert hostname.stable_host(platform="darwin", run=lambda *a, **k: _Out()) is None


def test_the_show_line_names_the_hostname_with_the_key_in_parentheses(monkeypatch: pytest.MonkeyPatch) -> None:
    key = hostname.host_key("ioplatformuuid", MAC_UUID)
    _machine(monkeypatch, stable=key, name="constantine.local")
    line = _holder_line({"holder": {"pid": 7, "host": key, "proc_start": T0}})
    assert f"on constantine.local ({key})," in line


def test_a_person_reads_the_hostname_with_the_key_in_parentheses(monkeypatch: pytest.MonkeyPatch) -> None:
    key, other = hostname.host_key("ioplatformuuid", MAC_UUID), hostname.host_key("ioplatformuuid", MAC_UUID[:-1] + "8")
    _machine(monkeypatch, stable=key, name="constantine.local")
    assert hostname.describe_host(key) == f"constantine.local ({key})"
    assert hostname.describe_host(other) == f"another machine ({other})"
    assert hostname.describe_host("ip-102") == "ip-102"


@pytest.mark.skipif(sys.platform == "linux" and not hostname.stable_host(), reason="inside a container or a machine with no usable machine-id")
def test_the_real_probe_finds_a_key_on_this_machine() -> None:
    """The one test that reads the real machine. macOS ``ioreg``, the Windows registry value and a Linux host's machine-id
    are all there; a Linux process in a container (or with no machine-id) skips, and the skip is itself asserted below."""
    key = hostname.stable_host()
    assert key is not None and key.startswith(hostname.KEY_PREFIX) and key == hostname.stable_host()


def test_the_real_probe_skip_on_linux_is_a_container_or_no_machine_id() -> None:
    """Non-vacuity: a Linux run may skip the test above only where the probe's own refusal reasons hold."""
    if sys.platform != "linux" or hostname.stable_host() is not None:
        return
    in_container = os.readlink("/proc/self/ns/pid") != hostname.INIT_PID_NS
    no_id = not any(p.is_file() and p.read_bytes().strip() not in (b"", b"uninitialized") for p in hostname._MACHINE_ID_FILES)
    assert in_container or no_id, "a keyless Linux host that is neither a container nor missing its machine-id is a probe bug"


# ── SessionStart ────────────────────────────────────────────────────────────


class _Day:
    """One machine over several days against one engine: the registry files, token files and engine persist, the hostname
    (and, for an upgrade, the stable key) changes."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, stable: str | None) -> None:
        self.monkeypatch, self.stable = monkeypatch, stable
        self.engine = FakeEngine()
        self.procs = FakeProcesses(tmp_path / "sessions", host="unset")
        self.idir = tmp_path / "identity"

    def hostname_is(self, name: str) -> None:
        _machine(self.monkeypatch, stable=self.stable, name=name)
        self.procs.host = this_host()

    def start(self, pid: int, source: str, session_id: str, *, name: str = "", os_start: str = T0) -> Any:
        self.procs.start(pid, os_start, session_id=session_id, name=name)
        binder = SessionBinder(client=self.engine, procs=self.procs, identity_dir=self.idir, sleep=lambda s: None)
        facts = LaunchFacts(source=source, session_id=session_id, host=this_host(), claude_pid=pid,
                            proc_start=normalise_instant(os_start), client_version="9.9.9-test")
        entry = read_registry_entry(pid, self.procs.sessions_dir)
        assert entry is not None
        facts.with_registry(entry)
        return binder.bind(facts)


def test_a_resumed_session_keeps_its_identity_when_the_hostname_changed_and_its_holder_died(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """GitHub issue 1679: session c4d96bda held an identity on day one; the restart ran on a machine whose hostname the
    network had changed, found its own old binding 'on another host' and bound a new identity."""
    day = _Day(monkeypatch, tmp_path, stable=hostname.host_key("ioplatformuuid", MAC_UUID))
    day.hostname_is("ip-10-122-0-102.ec2.internal")
    first = day.start(100, "startup", "sid-a", name="canon-chat-70")
    assert first.action == "ephemeral"
    day.procs.stop(100, keep_registry=True)                 # the restart killed it; its registry file stayed
    day.hostname_is("ip-10-122-0-105.ec2.internal")
    second = day.start(101, "resume", "sid-a", name="canon-chat-70", os_start=T1)
    assert (second.action, second.identity) == ("bound", first.identity), second.notices
    assert second.generation == 2 and day.engine.bound(first.identity)["holder"]["pid"] == 101
    assert len(day.engine.ids_called("create_ephemeral")) == 1, "the resume made no second identity"


def test_a_live_pre_upgrade_session_keeps_its_identity_across_a_clear_after_the_hostname_changed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Bound under a hostname before the key existed; the network moved on and the client was upgraded while the session ran.
    The row says ``.102`` and the machine is ``.105`` (key K): the hook's own token file proves the name was this machine's,
    so ``/clear`` finds its own binding and speaks under the recorded host. (The fake engine has no ``clear``; the real
    engine's is the engine row in test_session_start_bind.)"""
    day = _Day(monkeypatch, tmp_path, stable=None)
    day.hostname_is("ip-10-122-0-102.ec2.internal")
    first = day.start(100, "startup", "sid-a", name="x")
    day.stable = hostname.host_key("ioplatformuuid", MAC_UUID)
    day.hostname_is("ip-10-122-0-105.ec2.internal")
    binder = SessionBinder(client=day.engine, procs=day.procs, identity_dir=day.idir, sleep=lambda s: None)
    facts = LaunchFacts(source="clear", session_id="sid-b", host=this_host(), claude_pid=100, proc_start=normalise_instant(T0))
    facts.with_registry(read_registry_entry(100, day.procs.sessions_dir))
    own = binder._own_identity(facts)
    assert own is not None and own["id"] == first.identity, "found under the vanished name"
    assert binder._as_holder(facts, own).host == "ip-10-122-0-102.ec2.internal"


def test_a_pre_upgrade_dead_holder_after_a_hostname_change_rebinds_the_same_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    day = _Day(monkeypatch, tmp_path, stable=None)
    day.hostname_is("ip-10-122-0-102.ec2.internal")
    first = day.start(100, "startup", "sid-a", name="x")
    day.procs.stop(100, keep_registry=True)
    day.stable = hostname.host_key("ioplatformuuid", MAC_UUID)
    day.hostname_is("ip-10-122-0-105.ec2.internal")
    second = day.start(101, "resume", "sid-a", name="x", os_start=T1)
    assert (second.action, second.identity) == ("bound", first.identity), second.notices
    assert day.engine.bound(first.identity)["holder"]["host"] == this_host(), "the new holder is recorded under the key"


def test_a_live_lease_on_another_machine_still_refuses_the_seat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The holder's pid is not in this machine's table, and a name change must not read that as 'gone' for a holder that
    belongs to a machine with another key."""
    day = _Day(monkeypatch, tmp_path, stable=hostname.host_key("ioplatformuuid", MAC_UUID))
    day.hostname_is("ip-10-122-0-105.ec2.internal")
    elsewhere = hostname.host_key("ioplatformuuid", "5B2D6B0E-0C93-5D1B-9E7A-3F6B2A9C41D8")
    held = day.engine.stage(kind="ephemeral", state="live", generation=2, holder={
        "host": elsewhere, "pid": 100, "started_at": 100_000, "proc_start": T0, "session_id": "sid-a"})
    out = day.start(101, "resume", "sid-a", os_start=T1)
    assert out.action == "ephemeral" and out.identity != held
    assert day.engine.bound(held)["holder"]["host"] == elsewhere and day.engine.bound(held)["generation"] == 2
    assert any("is held by pid 100" in n and f"another machine ({elsewhere})" in n for n in out.notices)


def test_two_machines_with_one_hostname_do_not_judge_each_others_holders(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Machine B has no key and is called ``ubuntu``; this machine has a key and is also called ``ubuntu``. B's live seat
    is not this machine's to judge by its pid table."""
    day = _Day(monkeypatch, tmp_path, stable=hostname.host_key("ioplatformuuid", MAC_UUID))
    day.hostname_is("ubuntu")
    held = day.engine.stage(kind="ephemeral", state="live", generation=2, holder={
        "host": "ubuntu", "pid": 100, "started_at": 100_000, "proc_start": T0, "session_id": "sid-a"})
    out = day.start(101, "resume", "sid-a", os_start=T1)
    assert out.action == "ephemeral" and out.identity != held
    assert day.engine.bound(held)["generation"] == 2 and day.engine.bound(held)["holder"]["pid"] == 100


def test_the_ephemeral_of_a_refused_session_keeps_its_own_display_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """GitHub issue 1679, second defect: the held path made its ephemeral with ``display_name=None``."""
    day = _Day(monkeypatch, tmp_path, stable=hostname.host_key("ioplatformuuid", MAC_UUID))
    day.hostname_is("box-1")
    elsewhere = hostname.host_key("ioplatformuuid", "5B2D6B0E-0C93-5D1B-9E7A-3F6B2A9C41D8")
    day.engine.stage(kind="ephemeral", state="live", generation=2, display_name="canon-chat-70", holder={
        "host": elsewhere, "pid": 100, "started_at": 100_000, "proc_start": T0, "session_id": "sid-a"})
    out = day.start(101, "resume", "sid-a", os_start=T1)          # Claude records no name for this process
    assert out.action == "ephemeral"
    assert day.engine.ids_called("create_ephemeral")[-1][0] == "canon-chat-70"


def test_another_sessions_ephemeral_name_and_a_seats_display_name_are_not_copied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    day = _Day(monkeypatch, tmp_path, stable=hostname.host_key("ioplatformuuid", MAC_UUID))
    day.hostname_is("box-1")
    elsewhere = hostname.host_key("ioplatformuuid", "5B2D6B0E-0C93-5D1B-9E7A-3F6B2A9C41D8")
    day.engine.stage(kind="ephemeral", state="live", generation=2, display_name="someone-elses-name", holder={
        "host": elsewhere, "pid": 100, "started_at": 100_000, "proc_start": T0, "session_id": "sid-z"})
    day.engine.sessions["sid-q"] = ("i1", 2)                      # sid-q resumes an identity another session holds
    out = day.start(101, "resume", "sid-q", name="my-own-name", os_start=T1)
    assert out.action == "ephemeral"
    assert day.engine.ids_called("create_ephemeral")[-1][0] == "my-own-name"
    day.engine.stage(kind="seat", name="alpha", state="live", generation=1, display_name="seat-display", holder={
        "host": elsewhere, "pid": 102, "started_at": 102_000, "proc_start": T0, "session_id": "sid-s"})
    out = day.start(103, "startup", "sid-b", name="alpha", os_start=T1)
    assert out.action == "ephemeral"
    assert day.engine.ids_called("create_ephemeral")[-1][0] is None, "neither the seat's alias nor its display name"
