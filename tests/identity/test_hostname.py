# SPDX-License-Identifier: AGPL-3.0-or-later
"""The host a binding names (GitHub issue 1679): a key that survives the network changing the hostname, and the holder
check and SessionStart policy that read it. Tenant-free: the machine is a ``FakeProcesses`` table, the engine is
``tests/identity/fake_engine.py`` (the engine-backed rows are in ``test_session_start_bind.py``)."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from nexus.identity import hostname
from nexus.identity.binder import LaunchFacts, SessionBinder
from nexus.identity.holder import GONE, OTHER_HOST, check_holder, normalise_instant, read_registry_entry, this_host
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
    assert this_host() == "plain-host" and hostname.machine_names() == ("plain-host",)


def test_a_machine_answers_to_its_key_and_its_current_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    key = hostname.host_key("machine-id", MACHINE_ID)
    _machine(monkeypatch, stable=key, name="box-1")
    assert hostname.machine_names() == (key, "box-1")
    assert hostname.local_names(object(), key) == (key, "box-1")
    assert hostname.local_names(object(), "box-1") == ("box-1",)        # only the machine's own current name widens
    assert hostname.local_names(object(), "somebody-elses") == ("somebody-elses",)


# ── the holder check ────────────────────────────────────────────────────────


def _holder(host: str, pid: int = 500) -> dict[str, Any]:
    return {"host": host, "pid": pid, "started_at": pid * 1000, "proc_start": T0}


def test_the_holder_check_calls_a_dead_pid_gone_whatever_the_hostname_became(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The issue: a holder recorded on day one, the hostname different on day two. The key is the same, so the holder is
    on this machine and its dead pid is found gone."""
    key = hostname.host_key("ioplatformuuid", MAC_UUID)
    _machine(monkeypatch, stable=key, name="ip-10-122-0-102.ec2.internal")
    recorded = _holder(this_host())
    _machine(monkeypatch, stable=key, name="ip-10-122-0-105.ec2.internal")
    procs = FakeProcesses(tmp_path / "sessions", host=this_host())
    assert check_holder(recorded, procs=procs).state == GONE


def test_a_row_written_under_the_hostname_still_counts_as_this_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Rows written before the key carry ``gethostname()``: this upgrade must not strand every live seat once more."""
    key = hostname.host_key("ioplatformuuid", MAC_UUID)
    _machine(monkeypatch, stable=key, name="box-1")
    procs = FakeProcesses(tmp_path / "sessions", host=this_host())
    assert check_holder(_holder("box-1"), procs=procs).state == GONE


@pytest.mark.parametrize("holder_host", [
    pytest.param(hostname.host_key("ioplatformuuid", "5B2D6B0E-0C93-5D1B-9E7A-3F6B2A9C41D8"), id="another machine's key"),
    pytest.param("ip-10-122-0-102.ec2.internal", id="a name this machine no longer has"),
    pytest.param("some-other-box", id="another machine's hostname"),
])
def test_a_holder_on_another_machine_is_judged_by_its_lease_alone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, holder_host: str) -> None:
    """The dangerous direction: this machine has no process 500 and no registry file for it, which is what 'gone' would
    read, but the holder is not on this machine."""
    _machine(monkeypatch, stable=hostname.host_key("ioplatformuuid", MAC_UUID), name="box-1")
    procs = FakeProcesses(tmp_path / "sessions", host=this_host())
    assert check_holder(_holder(holder_host), procs=procs).state == OTHER_HOST


# ── SessionStart ────────────────────────────────────────────────────────────


class _Day:
    """One machine over several days against one engine: the registry files and engine persist, the hostname changes."""

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
    assert any("is held by pid 100" in n for n in out.notices)


def test_the_ephemeral_of_a_refused_session_keeps_the_display_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """GitHub issue 1679, second defect: the held path made its ephemeral with ``display_name=None``."""
    day = _Day(monkeypatch, tmp_path, stable=hostname.host_key("ioplatformuuid", MAC_UUID))
    day.hostname_is("box-1")
    elsewhere = hostname.host_key("ioplatformuuid", "5B2D6B0E-0C93-5D1B-9E7A-3F6B2A9C41D8")
    day.engine.stage(kind="ephemeral", state="live", generation=2, display_name="canon-chat-70", holder={
        "host": elsewhere, "pid": 100, "started_at": 100_000, "proc_start": T0, "session_id": "sid-a"})
    out = day.start(101, "resume", "sid-a", os_start=T1)          # Claude records no name for this process
    assert out.action == "ephemeral"
    assert day.engine.ids_called("create_ephemeral")[-1][0] == "canon-chat-70"


def test_a_refused_seat_gives_its_ephemeral_the_session_name_never_the_alias(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    day = _Day(monkeypatch, tmp_path, stable=hostname.host_key("ioplatformuuid", MAC_UUID))
    day.hostname_is("box-1")
    elsewhere = hostname.host_key("ioplatformuuid", "5B2D6B0E-0C93-5D1B-9E7A-3F6B2A9C41D8")
    day.engine.stage(kind="seat", name="alpha", state="live", generation=1, holder={
        "host": elsewhere, "pid": 100, "started_at": 100_000, "proc_start": T0, "session_id": "sid-a"})
    out = day.start(101, "startup", "sid-b", name="alpha", os_start=T1)
    assert out.action == "ephemeral"
    assert day.engine.ids_called("create_ephemeral")[-1][0] is None


@pytest.mark.skipif(sys.platform not in ("darwin", "win32"), reason="a Linux box may be a container with no usable machine-id")
def test_the_real_probe_finds_a_key_on_this_machine() -> None:
    """The one test that reads the real machine: macOS ``ioreg`` and the Windows registry value are always there."""
    key = hostname.stable_host()
    assert key is not None and key.startswith(hostname.KEY_PREFIX) and key == hostname.stable_host()
