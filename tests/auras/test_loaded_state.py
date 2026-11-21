# SPDX-License-Identifier: AGPL-3.0-or-later
"""The loaded-aura state file and ``nx aura list``'s reading of it (RDR-252 Step 4 part A, bead nexus-igea9.7, item 6
and 7; RDR "Two additions to the aura surface").

The MCP server writes ``<config>/auras/loaded-<claude pid>.<startedAt>.json`` at load and on an identity change, and
removes it on exit. ``nx aura list`` reads the file of its nearest Claude ancestor and drops a file whose holder
RDR-253's holder check finds gone. The process table is ``tests/identity/fakes.FakeProcesses``; no engine, no tenant.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from nexus.auras import loaded_state as ls
from nexus.auras.session import state_writer
from nexus.commands import aura_cmd
from nexus.commands.aura_cmd import aura_group
from nexus.identity.holder import this_host
from nexus.identity.hostname import host_key
from nexus.identity.token_file import TokenFile, token_path, write_token_file
from nexus.lua_runtime import library
from nexus.mcp import identity_lease
from tests.identity.fakes import HOST, FakeProcesses
from tests.identity.test_lease_schedule import HEARTBEAT_S, _file, _setup

from ._support import minimal
from .test_load import _GatedHarness

START = "2026-11-17T08:00:00Z"
OTHER_START = "2026-11-17T09:30:00Z"


@pytest.fixture
def procs(tmp_path: Path) -> FakeProcesses:
    return FakeProcesses(tmp_path / "sessions")


@pytest.fixture
def adir(tmp_path: Path) -> Path:
    return tmp_path / "auras"


def _state(pid: int = 4242, started_at: int = 4242000, *, identity: str | None = "id-a", loaded: str | None = "id-a",
           proc_start: str | None = START, server_pid: int = 9000, **auras: ls.AuraState) -> ls.LoadedState:
    return ls.LoadedState(
        server_pid=server_pid, host=HOST, pid=pid, started_at=started_at, proc_start=proc_start,
        session_id="sid", identity=identity, loaded_identity=loaded,
        auras=auras or {"ci_develop_red": ls.AuraState(True, "loaded", "push-train")},
    )


def _put(adir: Path, state: ls.LoadedState) -> Path:
    path = ls.state_path(adir, state.pid, state.started_at)
    ls.write_state(path, state)
    return path


# -- the file ----------------------------------------------------------------------------------------------------------


def test_the_file_is_named_by_the_claude_pid_and_started_at_and_round_trips(adir: Path) -> None:
    state = _state()
    path = _put(adir, state)
    assert path.name == "loaded-4242.4242000.json"
    assert ls.parse_state_name(path) == (4242, 4242000)
    assert ls.parse_state_name(adir / "other.json") is None
    got = ls.read_state(path)
    assert got is not None and got.auras == state.auras and got.identity == "id-a" and got.pid == 4242
    assert got.loaded_identity == "id-a" and got.server_pid == 9000


def test_a_short_write_does_not_truncate_the_state_file(adir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """nexus-igea9.7 round 1: os.write may write fewer bytes than asked; the file is published whole."""
    from tests._module_seam import setattr_in  # noqa: PLC0415

    real_write = os.write
    chunks: list[int] = []

    def three_at_a_time(fd: int, data: Any) -> int:
        n = real_write(fd, bytes(data)[:3])
        chunks.append(n)
        return n

    setattr_in(monkeypatch, ls, "os.write", three_at_a_time)
    state = _state()
    path = _put(adir, state)
    assert len(chunks) > 3, "the writer had to come back for the rest"
    got = ls.read_state(path)
    assert got is not None and got.auras == state.auras and got.identity == "id-a"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_the_file_is_private_to_the_user(adir: Path) -> None:
    path = _put(adir, _state())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_an_unreadable_or_malformed_file_reads_as_none(adir: Path) -> None:
    adir.mkdir()
    bad = adir / "loaded-1.2.json"
    for text in ("", "not json", "[]", json.dumps({"pid": "x"}), json.dumps({"pid": 1, "started_at": 2})):
        bad.write_text(text, encoding="utf-8")
        assert ls.read_state(bad) is None, text
    assert ls.read_state(adir / "absent.json") is None


def test_state_files_lists_only_state_files(adir: Path) -> None:
    _put(adir, _state(pid=10, started_at=1))
    _put(adir, _state(pid=11, started_at=2))
    (adir / "readme.txt").write_text("x", encoding="utf-8")
    assert [p.name for p in ls.state_files(adir)] == ["loaded-10.1.json", "loaded-11.2.json"]
    assert ls.state_files(adir / "nowhere") == []


def test_remove_deletes_only_the_file_this_server_wrote(adir: Path) -> None:
    path = _put(adir, _state(server_pid=9000))
    assert ls.remove_own(path, server_pid=9001) is False and path.exists(), "a successor's file stays"
    assert ls.remove_own(path, server_pid=9000) is True and not path.exists()
    assert ls.remove_own(path, server_pid=9000) is False


# -- the holder check ------------------------------------------------------------------------------------------------


def test_a_file_whose_holder_is_gone_is_removed_and_a_running_one_stays(adir: Path, procs: FakeProcesses) -> None:
    procs.start(4242, START, started_at=4242000)
    live = _put(adir, _state(pid=4242, started_at=4242000))
    dead = _put(adir, _state(pid=5000, started_at=5000000))      # no registry file, no process
    removed = ls.clean_gone(adir, procs)
    assert removed == [dead] and live.exists() and not dead.exists()


def test_a_pid_with_another_started_at_is_gone_which_covers_an_in_place_restart(adir: Path, procs: FakeProcesses) -> None:
    procs.start(4242, START, started_at=4242999)                  # /restart: same pid, a new startedAt
    old = _put(adir, _state(pid=4242, started_at=4242000))
    assert ls.clean_gone(adir, procs) == [old] and not old.exists()


def test_a_reused_pid_with_another_os_start_time_is_gone(adir: Path, procs: FakeProcesses) -> None:
    procs.start(4242, OTHER_START, started_at=4242000)            # the pid now belongs to a later process
    old = _put(adir, _state(pid=4242, started_at=4242000, proc_start=START))
    assert ls.clean_gone(adir, procs) == [old]


def test_a_process_whose_start_time_cannot_be_read_is_not_removed(adir: Path, procs: FakeProcesses) -> None:
    procs.start(4242, START, started_at=4242000)
    procs.os_start = lambda _pid: None                             # type: ignore[method-assign]
    path = _put(adir, _state(pid=4242, started_at=4242000))
    assert ls.clean_gone(adir, procs) == [] and path.exists()


def test_a_holder_registered_under_another_config_dir_is_not_judged_gone_by_this_readers_registry(
    adir: Path, procs: FakeProcesses, tmp_path: Path,
) -> None:
    """nexus-igea9.7 round 1 (S3): the reader's registry is one directory. A session registered under another
    CLAUDE_CONFIG_DIR has no file here, which proves nothing, so its live state file must stay (sweep.py and
    binder.py pass the identity dir for exactly this)."""
    procs.procs[6000] = START                                      # alive, but its registry file is elsewhere
    idir = tmp_path / "identity"
    write_token_file(
        token_path(idir, 6000, 6000000),
        TokenFile(identity="id-x", generation=1, token="t", expires_at=None, session_id="s", pid=6000, started_at=6000000,
                  proc_start=START, host=HOST, registry_dir=str(tmp_path / "other-claude" / "sessions")),
    )
    path = _put(adir, _state(pid=6000, started_at=6000000))
    assert ls.clean_gone(adir, procs, identity_dir=idir) == [] and path.exists()
    got = ls.view(adir, claude_pid=None, procs=procs, current_identity=None, identity_dir=idir)
    assert [s.pid for s in got.listed] == [6000] and path.exists()
    procs.stop(6000)                                               # the process itself exits: now it is gone
    assert ls.clean_gone(adir, procs, identity_dir=idir) == [path]


def test_a_file_from_another_host_is_left_alone(adir: Path, procs: FakeProcesses) -> None:
    other = _state(pid=7, started_at=7)
    other = ls.LoadedState(**{**other.__dict__, "host": "elsewhere"})
    path = _put(adir, other)
    assert ls.clean_gone(adir, procs) == [] and path.exists()


def test_a_file_under_a_lost_hostname_is_cleaned_when_this_machines_registry_proves_its_process_was_local(adir: Path, procs: FakeProcesses) -> None:
    """A state file written before the host key existed, by a process with no token file, names a hostname the machine may
    have lost. This machine's own registry file for that pid and startedAt (a crash leaves it behind) proves the process
    was local; no registry file (a clean exit) proves nothing, and a key of another machine is never local."""
    procs.start(4242, START, started_at=4242000)
    procs.stop(4242, keep_registry=True)                          # crashed: its registry file stays
    crashed = _put(adir, ls.LoadedState(**{**_state(pid=4242, started_at=4242000).__dict__, "host": "ip-102"}))
    exited = _put(adir, ls.LoadedState(**{**_state(pid=4343, started_at=4343000).__dict__, "host": "ip-102"}))
    procs.start(4545, START, started_at=4545000)
    procs.stop(4545, keep_registry=True)
    foreign = _put(adir, ls.LoadedState(**{**_state(pid=4545, started_at=4545000).__dict__,
                                           "host": host_key("machine-id", "ab" * 16)}))
    removed = ls.clean_gone(adir, procs)
    assert crashed in removed and not crashed.exists()
    assert exited.exists(), "no registry file: nothing proves it was this machine's"
    assert foreign.exists(), "a key that is not this machine's is another machine's, whatever this registry holds"


# -- the view `nx aura list` shows -------------------------------------------------------------------------------------


def test_a_bash_child_reads_its_own_servers_file_and_ignores_a_dead_ones(adir: Path, procs: FakeProcesses) -> None:
    procs.start(4242, START, started_at=4242000)
    procs.start(4343, START, started_at=4343000)                  # another live session's Claude
    _put(adir, _state(pid=4242, started_at=4242000, identity="mine", loaded="mine"))
    _put(adir, _state(pid=4343, started_at=4343000, identity="theirs", loaded="theirs"))
    ghost = _put(adir, _state(pid=5000, started_at=5000000, identity="ghost", loaded="ghost"))
    got = ls.view(adir, claude_pid=4242, procs=procs, current_identity="mine")
    assert got.claimed and got.state is not None and got.state.identity == "mine"
    assert got.mismatch is None and got.removed == (ghost,) and not ghost.exists()


def test_with_no_claude_ancestor_the_files_are_listed_and_none_is_claimed(adir: Path, procs: FakeProcesses) -> None:
    procs.start(4242, START, started_at=4242000)
    _put(adir, _state(pid=4242, started_at=4242000))
    got = ls.view(adir, claude_pid=None, procs=procs, current_identity=None)
    assert not got.claimed and got.state is None and [s.pid for s in got.listed] == [4242]


def test_an_ancestor_with_no_file_claims_nothing_and_is_not_an_error(adir: Path, procs: FakeProcesses) -> None:
    procs.start(4242, START, started_at=4242000)
    got = ls.view(adir, claude_pid=4242, procs=procs, current_identity="id-a")
    assert got.claimed and got.state is None and got.mismatch is None


def test_a_cross_seat_resume_reports_the_mismatch_until_a_reconnect(adir: Path, procs: FakeProcesses) -> None:
    """The auras were loaded for id-a; the process now resolves to id-b."""
    procs.start(4242, START, started_at=4242000)
    path = _put(adir, _state(pid=4242, started_at=4242000, identity="id-a", loaded="id-a"))
    got = ls.view(adir, claude_pid=4242, procs=procs, current_identity="id-b")
    assert got.mismatch is not None and "id-a" in got.mismatch and "id-b" in got.mismatch
    assert "reconnect" in got.mismatch.lower()
    # the server's heartbeat saw the switch and rewrote `identity`; the loaded identity is what the auras were built for
    ls.write_state(path, _state(pid=4242, started_at=4242000, identity="id-b", loaded="id-a"))
    assert ls.view(adir, claude_pid=4242, procs=procs, current_identity="id-b").mismatch is not None
    # a reconnect loads again for id-b
    ls.write_state(path, _state(pid=4242, started_at=4242000, identity="id-b", loaded="id-b"))
    assert ls.view(adir, claude_pid=4242, procs=procs, current_identity="id-b").mismatch is None


def test_a_session_that_loaded_unbound_and_is_now_bound_is_told_to_reconnect(adir: Path, procs: FakeProcesses) -> None:
    procs.start(4242, START, started_at=4242000)
    _put(adir, _state(pid=4242, started_at=4242000, identity=None, loaded=None))
    got = ls.view(adir, claude_pid=4242, procs=procs, current_identity="id-a")
    assert got.mismatch is not None and "reconnect" in got.mismatch.lower()
    assert ls.view(adir, claude_pid=4242, procs=procs, current_identity=None).mismatch is None


def test_a_resolver_that_cannot_say_reports_no_mismatch(adir: Path, procs: FakeProcesses) -> None:
    procs.start(4242, START, started_at=4242000)
    _put(adir, _state(pid=4242, started_at=4242000, identity="id-a", loaded="id-a"))
    assert ls.view(adir, claude_pid=4242, procs=procs, current_identity=None).mismatch is None


# -- nx aura list ------------------------------------------------------------------------------------------------------


@pytest.fixture
def shipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "shipped"
    root.mkdir()
    monkeypatch.setattr(library, "shipped_root", lambda: root)
    monkeypatch.delenv(library.REPO_ROOT_ENV, raising=False)
    monkeypatch.delenv("NX_AURAS_OFF", raising=False)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "config.yml").write_text("auras:\n  gated: on\n  plain: on\n  idle: off\n", encoding="utf-8")
    for name, extra in (("gated", '\n[load]\ngroup = "push-train"\n'), ("plain", ""), ("idle", "")):
        text = minimal(name).replace('threads = "a block"', f'threads = "block {name}"') + extra
        (root / f"{name}.aura.toml").write_text(text, encoding="utf-8")
    return root


def _inputs(monkeypatch: pytest.MonkeyPatch, adir: Path, procs: FakeProcesses, *, pid: int | None, identity: str | None) -> None:
    monkeypatch.setattr(aura_cmd, "_loaded_inputs", lambda: (adir, procs, pid, identity, None))


def _list(*argv: str) -> Any:
    return CliRunner().invoke(aura_group, ["list", *argv])


def test_list_shows_loaded_or_not_and_the_group_that_decided_it(
    shipped: Path, monkeypatch: pytest.MonkeyPatch, adir: Path, procs: FakeProcesses,
) -> None:
    procs.start(4242, START, started_at=4242000)
    _put(adir, _state(
        pid=4242, started_at=4242000, identity="id-a", loaded="id-a",
        gated=ls.AuraState(False, "not_a_member", "push-train"), plain=ls.AuraState(True, "loaded", None),
    ))
    _inputs(monkeypatch, adir, procs, pid=4242, identity="id-a")
    res = _list()
    assert res.exit_code == 0, res.output
    rows = {ln.split()[0]: ln for ln in res.output.splitlines() if ln.split() and ln.split()[0] in {"gated", "plain", "idle"}}
    assert "not loaded" in rows["gated"] and "push-train" in rows["gated"] and "not_a_member" in rows["gated"]
    assert "loaded" in rows["plain"] and "not loaded" not in rows["plain"]
    assert "loaded" not in rows["idle"], "an aura that is off has no load state to show"


def test_list_json_carries_the_load_state_per_aura(
    shipped: Path, monkeypatch: pytest.MonkeyPatch, adir: Path, procs: FakeProcesses,
) -> None:
    procs.start(4242, START, started_at=4242000)
    _put(adir, _state(
        pid=4242, started_at=4242000, identity="id-a", loaded="id-a",
        gated=ls.AuraState(False, "group_disbanded", "push-train"), plain=ls.AuraState(True, "loaded", None),
    ))
    _inputs(monkeypatch, adir, procs, pid=4242, identity="id-a")
    rows = {r["name"]: r for r in json.loads(_list("--json").stdout)}
    assert rows["gated"]["loaded"] == {"loaded": False, "reason": "group_disbanded", "group": "push-train"}
    assert rows["plain"]["loaded"] == {"loaded": True, "reason": "loaded", "group": None}
    assert rows["idle"]["loaded"] is None


def test_list_says_to_reconnect_when_the_load_could_not_read_the_engine(
    shipped: Path, monkeypatch: pytest.MonkeyPatch, adir: Path, procs: FakeProcesses,
) -> None:
    """The engine was down at start: the load is final for this server's life, so a reader needs the way out."""
    procs.start(4242, START, started_at=4242000)
    _put(adir, _state(pid=4242, started_at=4242000, gated=ls.AuraState(False, "memberships_unreadable", "push-train"),
                      plain=ls.AuraState(True, "loaded", None)))
    _inputs(monkeypatch, adir, procs, pid=4242, identity="id-a")
    rows = {ln.split()[0]: ln for ln in _list().output.splitlines() if ln.split() and ln.split()[0] in {"gated", "plain"}}
    assert "memberships_unreadable" in rows["gated"] and "reconnect" in rows["gated"] and "/mcp" in rows["gated"]
    assert "reconnect" not in rows["plain"]
    shown = {r["name"]: r for r in json.loads(_list("--json").stdout)}
    assert shown["gated"]["loaded"] == {"loaded": False, "reason": "memberships_unreadable", "group": "push-train"}, "--json is unchanged"


def test_list_with_no_claude_ancestor_lists_the_state_files_on_stderr_without_claiming_one(
    shipped: Path, monkeypatch: pytest.MonkeyPatch, adir: Path, procs: FakeProcesses,
) -> None:
    """nexus-igea9.7 round 1 (SB1): bead item 6, "with no Claude ancestor it lists the files without claiming one"."""
    procs.start(4242, START, started_at=4242000)
    procs.start(4343, START, started_at=4343000)
    _put(adir, _state(
        pid=4242, started_at=4242000, identity="id-a", loaded="id-a",
        gated=ls.AuraState(False, "not_a_member", "push-train"), plain=ls.AuraState(True, "loaded", None),
    ))
    _put(adir, _state(pid=4343, started_at=4343000, identity=None, loaded=None, gated=ls.AuraState(False, "unbound", "push-train")))
    _inputs(monkeypatch, adir, procs, pid=None, identity=None)
    res = _list()
    assert res.exit_code == 0, res.output
    err = res.stderr
    assert "no Claude ancestor" in err and "loaded-4242.4242000.json" in err and "loaded-4343.4343000.json" in err
    assert "identity id-a" in err and "no identity" in err
    assert "gated: not loaded: not_a_member (group push-train)" in err and "plain: loaded" in err
    assert "gated: not loaded: unbound (group push-train)" in err
    assert "not_a_member" not in res.stdout, "the table on stdout stays the table, and claims no file"
    rows = {r["name"]: r for r in json.loads(_list("--json").stdout)}
    assert all(r["loaded"] is None for r in rows.values()), "--json claims no file either"
    assert "no Claude ancestor" not in _inputs_and_list(monkeypatch, adir, procs, pid=4242).stderr, (
        "a caller that has an ancestor claims its own file and does not list the others")


def _inputs_and_list(monkeypatch: pytest.MonkeyPatch, adir: Path, procs: FakeProcesses, *, pid: int) -> Any:
    _inputs(monkeypatch, adir, procs, pid=pid, identity="id-a")
    return _list()


def test_list_reports_an_identity_mismatch_and_says_to_reconnect(
    shipped: Path, monkeypatch: pytest.MonkeyPatch, adir: Path, procs: FakeProcesses,
) -> None:
    procs.start(4242, START, started_at=4242000)
    _put(adir, _state(pid=4242, started_at=4242000, identity="id-a", loaded="id-a"))
    _inputs(monkeypatch, adir, procs, pid=4242, identity="id-b")
    res = _list()
    assert res.exit_code == 0, res.output
    assert "reconnect" in res.stderr.lower() and "id-a" in res.stderr and "id-b" in res.stderr
    assert "reconnect" not in res.stdout.lower(), "the table on stdout stays the table"


def test_list_json_stays_parseable_when_it_reports_a_mismatch(
    shipped: Path, monkeypatch: pytest.MonkeyPatch, adir: Path, procs: FakeProcesses,
) -> None:
    procs.start(4242, START, started_at=4242000)
    _put(adir, _state(pid=4242, started_at=4242000, identity="id-a", loaded="id-a"))
    _inputs(monkeypatch, adir, procs, pid=4242, identity="id-b")
    res = _list("--json")
    assert isinstance(json.loads(res.stdout), list) and "reconnect" in res.stderr.lower()


def test_list_with_no_state_file_says_nothing_about_loading(
    shipped: Path, monkeypatch: pytest.MonkeyPatch, adir: Path, procs: FakeProcesses,
) -> None:
    procs.start(4242, START, started_at=4242000)
    _inputs(monkeypatch, adir, procs, pid=4242, identity="id-a")
    rows = {r["name"]: r for r in json.loads(_list("--json").stdout)}
    assert all(r["loaded"] is None for r in rows.values())


def test_list_says_so_when_an_on_group_gated_aura_has_no_state_file(
    shipped: Path, monkeypatch: pytest.MonkeyPatch, adir: Path, procs: FakeProcesses,
) -> None:
    """A Desktop server that never bound, or a server that never started, writes no file: the gated aura is on and
    loads nowhere, and the line must say so (RDR "Load by membership", bead item 3)."""
    procs.start(4242, START, started_at=4242000)
    _inputs(monkeypatch, adir, procs, pid=4242, identity="id-a")
    rows = {ln.split()[0]: ln for ln in _list().output.splitlines() if ln.split() and ln.split()[0] in {"gated", "plain", "idle"}}
    assert "not loaded" in rows["gated"] and "no state file" in rows["gated"] and "/mcp" in rows["gated"]
    assert "not loaded" not in rows["plain"] and "not loaded" not in rows["idle"], "only a group-gated aura that is on"
    assert all(r["loaded"] is None for r in json.loads(_list("--json").stdout)), "--json keeps its shape"
    _put(adir, _state(pid=4242, started_at=4242000, identity="id-a", loaded="id-a", gated=ls.AuraState(True, "loaded", "push-train")))
    again = {ln.split()[0]: ln for ln in _list().output.splitlines() if ln.split() and ln.split()[0] == "gated"}
    assert "no state file" not in again["gated"]


def test_list_removes_the_state_file_of_a_dead_server(
    shipped: Path, monkeypatch: pytest.MonkeyPatch, adir: Path, procs: FakeProcesses,
) -> None:
    ghost = _put(adir, _state(pid=5000, started_at=5000000))
    _inputs(monkeypatch, adir, procs, pid=None, identity=None)
    assert _list().exit_code == 0
    assert not ghost.exists()


def test_the_real_inputs_name_the_config_dir_and_do_not_raise(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    from nexus import session as nx_session  # noqa: PLC0415
    from nexus.identity import token_header  # noqa: PLC0415

    monkeypatch.setattr(nx_session, "find_immediate_claude_pid", lambda: 4242)
    monkeypatch.setattr(nx_session, "_command_name_of", lambda pid: "claude" if pid == 4242 else "bash")
    monkeypatch.setattr(token_header, "current", lambda **_k: TokenFile(
        identity="id-x", generation=1, token="t", expires_at=None, session_id="s", pid=4242, started_at=1, proc_start=None))
    directory, procs, pid, identity, idir = aura_cmd._loaded_inputs()
    assert directory == tmp_path / "cfg" / "auras"
    assert idir == tmp_path / "cfg" / "identity", "the holder check reads the token files of the same config dir"
    assert (pid, identity) == (4242, "id-x"), "the Claude ancestor's pid and the identity of its token"
    assert callable(getattr(procs, "alive", None)), "the real process table"
    monkeypatch.setattr(nx_session, "_command_name_of", lambda pid: "bash")          # an ancestor that is not Claude
    monkeypatch.setattr(token_header, "current", lambda **_k: None)                  # and no usable token
    _dir, _procs, pid, identity, _idir = aura_cmd._loaded_inputs()
    assert (pid, identity) == (None, None)
    def broken() -> int:
        raise OSError("process table unreadable")

    monkeypatch.setattr(nx_session, "find_immediate_claude_pid", broken)
    assert aura_cmd._loaded_inputs()[2:4] == (None, None), "a process that cannot be read is no ancestor and no identity"


# -- the heartbeat and the loader: an identity change rewrites the file ---------------------------------------------------


def test_the_lease_tells_its_listeners_when_it_adopts_another_identity(tmp_path: Path) -> None:
    now = [1_000.0]
    lease, _engine, _ = _setup(tmp_path, now)
    seen: list[str] = []
    remove = identity_lease.add_change_listener(seen.append)
    broken = identity_lease.add_change_listener(lambda _i: (_ for _ in ()).throw(RuntimeError("listener bug")))
    try:
        _file(tmp_path, "tok-0", expires_at=now[0] + 3600)
        assert lease.tick().adopted
        assert seen == ["i1"], "the first bind of a server that started unbound is a change"
        now[0] += HEARTBEAT_S
        lease.tick()
        assert seen == ["i1"], "a renewal of the same identity is not"
        write_token_file(
            token_path(tmp_path / "config" / "identity", 100, 100_000),
            TokenFile(identity="i2", generation=4, token="tok-i2", expires_at=now[0] + 3600, session_id="sid-1", pid=100,
                      started_at=100_000, proc_start=START),
        )
        now[0] += HEARTBEAT_S
        assert lease.tick().adopted
        assert seen == ["i1", "i2"]
    finally:
        remove()
        broken()
    assert identity_lease._IDENTITY_LISTENERS == []


def test_the_loader_rewrites_the_state_with_the_new_identity_and_keeps_the_loaded_one() -> None:
    h = _GatedHarness(identity="id-a")
    [(pid, first)] = h.states
    assert (first.identity, first.loaded_identity) == ("id-a", "id-a")
    h.loader.note_identity("id-b")
    h.loader.note_identity("id-b")                          # the same identity again writes nothing
    assert len(h.states) == 2 and h.states[1][0] == pid
    second = h.states[1][1]
    assert (second.identity, second.loaded_identity) == ("id-b", "id-a")
    assert second.auras == first.auras


def test_a_note_before_any_load_writes_nothing_and_a_failing_sink_is_harmless() -> None:
    from nexus.auras.session import AuraLoader  # noqa: PLC0415
    from nexus.mcp.subscriptions import SubscriptionSet  # noqa: PLC0415

    from .test_load import _SID, auras_mod_parse, _GATED  # noqa: PLC0415

    written: list = []
    loader = AuraLoader(
        _SID, SubscriptionSet(session_id=_SID), discover=lambda: auras_mod_parse(_GATED, "gated"), is_on=lambda _n: True,
        entry=lambda _sid: ("tamsin", 100), registered_owner=lambda: 100, make_runtime=lambda a: a, attach=lambda _r: None,
        identity=lambda: "id-a", memberships=lambda _i: ["push-train"], read_group=lambda n: None,
        state_sink=lambda pid, state: written.append(state),
    )
    loader.note_identity("id-b")                                  # no load has happened: remembered, nothing written
    assert written == [] and not loader.loaded
    h = _GatedHarness(identity="id-a")
    h.loader._state_sink = lambda _pid, _state: (_ for _ in ()).throw(OSError("disk full"))   # noqa: SLF001
    h.loader.note_identity("id-b")                                                              # does not raise


def test_the_state_writer_names_the_holder_from_the_registry_and_removes_the_file_at_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, procs: FakeProcesses,
) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(procs.sessions_dir.parent))
    procs.start(4242, START, started_at=4242000)
    exits: list[tuple] = []
    write = state_writer("sid-1", register_exit=lambda fn, *a, **k: exits.append((fn, a, k)))
    write(4242, ls.LoadedState(identity="id-a", loaded_identity="id-a", auras={"x": ls.AuraState(True, "loaded", None)}))
    path = tmp_path / "cfg" / "auras" / "loaded-4242.4242000.json"
    got = ls.read_state(path)
    assert got is not None and (got.pid, got.started_at, got.session_id, got.server_pid) == (4242, 4242000, "sid-1", os.getpid())
    assert got.host == this_host() and got.proc_start == START, "the holder is named the way the holder check reads it"
    assert len(exits) == 1
    write(4242, got)                                         # a rewrite registers no second exit hook
    assert len(exits) == 1
    fn, args, kwargs = exits[0]
    fn(*args, **kwargs)
    assert not path.exists()
    write(9999, got)                                         # no registry file for that pid: nothing is written
    assert not (tmp_path / "cfg" / "auras" / "loaded-9999.json").exists()
