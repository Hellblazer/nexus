# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The restart sandbox must bind no RDR-253 identity in the operator's tenant (found 2026-11-19).

The sandbox copies the operator's ``config.yml`` so its sessions' MCP servers and ``nx`` reach the operator's engine; since
8.5.0 each session's SessionStart hook and MCP identity lease then bound an identity there (rows i13-i23, names ``rs_*``).
What holds it shut, each tested here with no claude: the sandbox's session environment carries the two switches
(``NX_IDENTITY_BIND=0`` for the hook, ``NX_IDENTITY_LEASE=0`` for the lease); the installed nx must honour the switch
before any session starts; each session id must read ``unknown_session`` afterwards (an identity that bound and then ended is
on no list); a list of the operator's identities (``nx identity show --all``) is read before and after and a new row that is
the sandbox's fails the run; and the sandbox's nx shim refuses the verbs that make an identity. All reads are read only.
``nx`` is a stand-in script on PATH, as in test_restart_sandbox_work.py."""
from __future__ import annotations

import importlib.util
import stat
import subprocess
import sys
from pathlib import Path

import pytest

E2E = Path(__file__).resolve().parents[1] / "e2e" / "restart-sessions"
RUN_PY = E2E / "run.py"
SIBLING = E2E / "identity_census.py"

SHOW_ALL = """bindings on constantine:
  i1 bob (ephemeral) live, generation 1, pid 15593, client 8.5.0
  i10 labrun_79e (ephemeral) live, generation 1, pid 92626, client 8.5.0
  i13 rs_nochan (ephemeral) lapsed, generation 1, pid 88275, client 8.5.0
  i2 elsinore (ephemeral) live, generation 1, pid 33591, client 8.5.0
live Claude processes with no binding:
  pid 59764 kestrel (claude-desktop), session 3db87a2e-ff4c-4629-a97a-c5913c16b5ed
"""


@pytest.fixture(scope="module")
def census():
    spec = importlib.util.spec_from_file_location("restart_sandbox_identity_census_test", SIBLING)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def run():
    spec = importlib.util.spec_from_file_location("restart_sandbox_run_census", RUN_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def fake_nx(tmp_path: Path, *, out: str = SHOW_ALL, code: int = 0, name: str = "nx") -> tuple[str, Path]:
    """A stand-in ``nx`` that prints *out*, exits *code* and logs its arguments."""
    log = tmp_path / f"{name}.log"
    nx = tmp_path / "bin" / name
    nx.parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / f"{name}.out").write_text(out)
    nx.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\ncat "{tmp_path / (name + ".out")}"\nexit {code}\n')
    nx.chmod(nx.stat().st_mode | stat.S_IXUSR)
    return str(nx), log


# -- the session environment -------------------------------------------------------------------
def test_the_sandbox_session_environment_switches_off_the_hook_bind_and_the_mcp_lease(run, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NX_IDENTITY_BIND", "1")  # an operator's own value must not leak in
    monkeypatch.setenv("NX_IDENTITY_LEASE", "1")
    sb = run.Sandbox(tmp_path, "nx-rs-test-census", "claude", "sonnet", run.SPECS["gate"])
    assert sb.env["NX_IDENTITY_BIND"] == "0" and sb.env["NX_IDENTITY_LEASE"] == "0"


def test_a_delegated_childs_token_and_seat_are_not_handed_to_the_sessions(run, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NX_IDENTITY_TOKEN", "/x/i9.token")
    monkeypatch.setenv("NX_SEAT", "alpha")
    sb = run.Sandbox(tmp_path, "nx-rs-test-census", "claude", "sonnet", run.SPECS["gate"])
    assert "NX_IDENTITY_TOKEN" not in sb.env and "NX_SEAT" not in sb.env


def test_an_operator_nx_override_is_not_handed_to_the_restart_script(run, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NX_RESTART_NX", "/opt/real/nx")  # the script would run it in place of the sandbox nx shim
    sb = run.Sandbox(tmp_path, "nx-rs-test-census", "claude", "sonnet", run.SPECS["gate"])
    assert "NX_RESTART_NX" not in sb.env


# -- parsing ------------------------------------------------------------------------------------
def test_the_census_reads_every_binding_and_none_of_the_unbound_process_lines(census) -> None:
    got = census.parse(SHOW_ALL)
    assert set(got) == {"i1", "i10", "i13", "i2"}
    assert "rs_nochan" in got["i13"]


def test_a_host_with_no_binding_is_an_empty_census_not_an_unreadable_one(census) -> None:
    assert census.parse("bindings on h:\n  (none)\nevery live Claude process on this host has a binding\n") == {}


@pytest.mark.parametrize("text", ["", "no engine\n", "Error: boom\n", "bindings on h:\n  something unexpected here\n"])
def test_output_that_is_not_the_binding_list_is_unreadable(census, text) -> None:
    with pytest.raises(census.CensusError):
        census.parse(text)


# -- reading, fail-closed -------------------------------------------------------------------------
def test_the_read_asks_nx_identity_show_all_and_nothing_else(census, tmp_path) -> None:
    nx, log = fake_nx(tmp_path)
    assert set(census.read(nx)) == {"i1", "i10", "i13", "i2"}
    assert log.read_text().splitlines() == ["identity show --all"], "one read-only call"


@pytest.mark.parametrize("kw", [{"code": 1}, {"out": "garbled\n"}, {"out": ""}])
def test_a_read_that_fails_or_does_not_parse_is_an_error_not_an_empty_census(census, tmp_path, kw) -> None:
    nx, _ = fake_nx(tmp_path, **kw)
    with pytest.raises(census.CensusError):
        census.read(nx)


def test_no_nx_to_ask_is_an_error(census) -> None:
    with pytest.raises(census.CensusError, match="no nx"):
        census.read("")


def test_a_read_that_times_out_is_an_error(census, tmp_path) -> None:
    nx = tmp_path / "bin" / "nx"
    nx.parent.mkdir()
    nx.write_text("#!/bin/sh\nsleep 30\n")
    nx.chmod(nx.stat().st_mode | stat.S_IXUSR)
    with pytest.raises(census.CensusError, match="did not answer"):
        census.read(str(nx), timeout=1)


# -- the comparison ---------------------------------------------------------------------------------
def test_new_identities_are_those_in_the_second_list_only_in_numeric_order(census) -> None:
    before = {"i1": "a", "i9": "b"}
    after = {"i1": "a", "i9": "b", "i24": "c", "i10": "d"}
    assert census.new_identities(before, after) == ["i10", "i24"]


def test_an_identity_that_left_the_list_is_not_new_and_equal_lists_have_none(census) -> None:
    assert census.new_identities({"i1": "a", "i2": "b"}, {"i1": "a"}) == []
    assert census.new_identities({}, {}) == []


def test_row_fields_read_name_kind_and_pid(census) -> None:
    assert census.row_fields("i13 rs_nochan (ephemeral) lapsed, generation 1, pid 88275, client 8.5.0") == ("rs_nochan", "ephemeral", 88275)
    assert census.row_fields("i30 child (delegated) live, generation 2, pid 7, client 8.5.0") == ("child", "delegated", 7)
    assert census.row_fields("something else") == ("", "", None)


BASE = {"i1": "i1 bob (ephemeral) live, generation 1, pid 15593, client 8.5.0"}
LIST = ("bindings on h:\n  i1 bob (ephemeral) live, generation 1, pid 15593, client 8.5.0\n"
        "  i30 rs_alpha (ephemeral) live, generation 1, pid 5001, client 8.5.0\n"
        "  i31 stranger (ephemeral) live, generation 1, pid 5002, client 8.5.0\n"
        "  i32 kid (delegated) live, generation 1, pid 5003, client 8.5.0\n"
        "  i33 other (ephemeral) lapsed, generation 1, pid 5004, client 8.5.0\n")


def test_a_new_row_with_a_sandbox_name_or_a_recorded_pid_fails_naming_it(census, tmp_path) -> None:
    nx, _ = fake_nx(tmp_path, out=LIST)
    failures, notes = census.check(nx, BASE, names={"rs_alpha"}, pids={5004})
    assert len(failures) == 1
    assert "the sandbox bound an identity on the operator's endpoint: i30, i33" in failures[0]
    assert "i31" not in failures[0] and "i32" not in failures[0]
    assert any("i31 stranger" in n and "not attributed" in n for n in notes), "an unattributed new row is a note naming the row"
    assert any("i32 kid" in n and "delegated" in n and "ignored" in n for n in notes)


def test_a_delegated_row_held_by_a_sandbox_pid_is_still_the_sandboxs(census, tmp_path) -> None:
    nx, _ = fake_nx(tmp_path, out=LIST)
    failures, _ = census.check(nx, BASE, names=set(), pids={5003})
    assert len(failures) == 1 and "i32" in failures[0]


def test_new_rows_that_are_nobody_the_sandbox_knows_are_notes_not_failures(census, tmp_path) -> None:
    nx, _ = fake_nx(tmp_path, out=LIST)
    failures, notes = census.check(nx, BASE, names={"rs_beta"}, pids={1})
    assert failures == []
    assert any("4 new" in n for n in notes) and sum("not attributed" in n for n in notes) == 3


def test_check_passes_when_nothing_is_new_and_says_what_it_examined(census, tmp_path) -> None:
    nx, _ = fake_nx(tmp_path)
    failures, notes = census.check(nx, census.parse(SHOW_ALL), names={"rs_nochan"})
    assert failures == []
    assert any("4 identities before" in n and "4 after" in n and "0 new" in n for n in notes)


def test_check_fails_when_the_second_read_cannot_be_made(census, tmp_path) -> None:
    nx, _ = fake_nx(tmp_path, code=1)
    failures, _ = census.check(nx, {"i1": "x"})
    assert len(failures) == 1 and "could not be read" in failures[0]


# -- the per-session check: the primary net ---------------------------------------------------------------
SID_A, SID_B = "aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000002"
UNKNOWN = (1, "Error: unknown_session: no identity has held session x")
BOUND = (0, '{"id": "i40", "state": "ended", "kind": "ephemeral", "name": "rs_alpha"}')


def sessions_nx(tmp_path: Path, answers: dict[str, tuple[int, str]]) -> tuple[str, Path]:
    """A stand-in ``nx`` that answers ``identity show --session <sid> --json`` per session id and logs every call."""
    log = tmp_path / "sessions.log"
    nx = tmp_path / "bin" / "nx"
    nx.parent.mkdir(parents=True, exist_ok=True)
    cases = "".join(f'  *"{sid}"*) echo \'{out}\'{"" if code == 0 else " >&2"}; exit {code};;\n' for sid, (code, out) in answers.items())
    nx.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\ncase "$*" in\n{cases}  *) echo "no such answer" >&2; exit 9;;\nesac\n')
    nx.chmod(nx.stat().st_mode | stat.S_IXUSR)
    return str(nx), log


def test_sessions_that_all_answer_unknown_session_pass_and_each_is_asked_read_only(census, tmp_path) -> None:
    nx, log = sessions_nx(tmp_path, {SID_A: UNKNOWN, SID_B: UNKNOWN})
    failures, notes = census.session_findings(nx, [SID_B, SID_A, SID_A], expect=True)
    assert failures == [] and any("2 session id(s)" in n for n in notes)
    assert sorted(log.read_text().splitlines()) == [f"identity show --session {SID_A} --json", f"identity show --session {SID_B} --json"]


def test_a_session_bound_to_an_identity_that_has_since_ended_fails_naming_it(census, tmp_path) -> None:
    """The case the list diff cannot see: a clean teardown ends the identity, so it is on no ``show --all`` list."""
    nx, _ = sessions_nx(tmp_path, {SID_A: UNKNOWN, SID_B: BOUND})
    failures, _ = census.session_findings(nx, [SID_A, SID_B], expect=True)
    assert len(failures) == 1
    assert "the sandbox bound an identity on the operator's endpoint" in failures[0]
    assert "bbbbbbbb" in failures[0] and "i40" in failures[0] and "ended" in failures[0] and "aaaaaaaa" not in failures[0]


@pytest.mark.parametrize("answer", [(1, "Error: boom"), (2, "No such command 'identity'"), (0, "")])
def test_any_other_answer_than_unknown_session_fails_closed(census, tmp_path, answer) -> None:
    nx, _ = sessions_nx(tmp_path, {SID_A: answer})
    failures, _ = census.session_findings(nx, [SID_A], expect=True)
    assert len(failures) == 1 and "aaaaaaaa" in failures[0]


def test_a_check_that_examined_no_session_is_a_failure_when_sessions_ran(census, tmp_path) -> None:
    nx, log = sessions_nx(tmp_path, {})
    failures, _ = census.session_findings(nx, [], expect=True)
    assert len(failures) == 1 and "no session id was collected" in failures[0]
    assert census.session_findings(nx, [], expect=False)[0] == [], "no session ran: nothing to ask"
    assert not log.exists()


def test_no_nx_to_ask_fails_when_there_are_sessions(census) -> None:
    failures, _ = census.session_findings("", [SID_A])
    assert len(failures) == 1 and "no nx" in failures[0]


def test_a_session_read_that_times_out_fails(census, tmp_path) -> None:
    nx = tmp_path / "bin" / "nx"
    nx.parent.mkdir()
    nx.write_text("#!/bin/sh\nsleep 30\n")
    nx.chmod(nx.stat().st_mode | stat.S_IXUSR)
    failures, _ = census.session_findings(str(nx), [SID_A], timeout=1)
    assert len(failures) == 1 and "not read" in failures[0]


def test_judge_runs_both_nets_and_returns_both_failures(census, tmp_path) -> None:
    nx = tmp_path / "bin" / "nx"
    nx.parent.mkdir()
    nx.write_text(f'#!/bin/sh\ncase "$*" in\n  "identity show --all") printf %s "$(cat {tmp_path}/list)";;\n'
                  f'  *"{SID_B}"*) echo \'{BOUND[1]}\'; exit 0;;\n  *) echo unknown_session >&2; exit 1;;\nesac\n')
    nx.chmod(nx.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "list").write_text(LIST)
    failures, notes = census.judge(str(nx), BASE, sids=[SID_A, SID_B], pids={5001}, names=set(), expect=True)
    assert len(failures) == 2
    assert any("session bbbbbbbb" in f for f in failures) and any("i30" in f for f in failures)
    assert any("2 session id(s)" in n for n in notes) and any("identity census:" in n and "new" in n for n in notes)


# -- the installed nx must honour the switch: the capability gate --------------------------------------------
def fake_python(tmp_path: Path, *, out: str = "NX_IDENTITY_BIND", code: int = 0, err: str = "") -> tuple[Path, Path]:
    """A stand-in interpreter that logs its arguments, then prints *out* and *err* and exits *code*."""
    log = tmp_path / "python.log"
    py = tmp_path / "gen" / "bin" / "python"
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\necho \'{out}\'\necho \'{err}\' >&2\nexit {code}\n')
    py.chmod(py.stat().st_mode | stat.S_IXUSR)
    return py, log


def entry_script(py: Path) -> str:
    """nexus's tool entry script (``<generation>/bin/nx``) and the generated wrapper in front of it, as install writes them."""
    entry = py.parent / "nx"
    entry.write_text(f"#!{py}\n# -*- coding: utf-8 -*-\n")
    pointer = py.parent.parent.parent / "current"
    pointer.symlink_to(py.parent.parent)
    wrapper = py.parent.parent.parent / "wrapper" / "nx"
    wrapper.parent.mkdir()
    wrapper.write_text(f'#!/bin/sh\n# Generated by nexus.\nNX_GEN="$(readlink "{pointer}")" || {{\n  exit 70\n}}\nexec "$NX_GEN/bin/nx" "$@"\n')
    wrapper.chmod(0o755)
    return str(wrapper)


def test_an_installed_nx_that_names_the_variable_is_no_gap_and_is_asked_in_isolated_mode(census, tmp_path) -> None:
    py, log = fake_python(tmp_path)
    assert census.bind_switch_gap(entry_script(py)) == ""
    [call] = log.read_text().splitlines()
    assert call.startswith("-I -c ") and "nexus.hooks.identity_bind" in call and "NX_IDENTITY_BIND_ENV" in call


def test_an_installed_nx_without_the_variable_is_a_loud_gap(census, tmp_path) -> None:
    py, _ = fake_python(tmp_path, out="", code=1, err="AttributeError: module 'nexus.hooks.identity_bind' has no attribute 'NX_IDENTITY_BIND_ENV'")
    gap = census.bind_switch_gap(entry_script(py))
    assert "installed nx predates NX_IDENTITY_BIND; the sandbox would bind identities on the operator's endpoint" in gap


@pytest.mark.parametrize("kw", [{"out": "", "code": 1, "err": "ModuleNotFoundError: No module named 'nexus'"}, {"out": "something else"}])
def test_an_installed_nx_that_cannot_be_asked_is_a_gap_too(census, tmp_path, kw) -> None:
    py, _ = fake_python(tmp_path, **kw)
    gap = census.bind_switch_gap(entry_script(py))
    assert gap and "could not ask the installed nx" in gap and "would bind identities" in gap


def test_a_direct_shebang_is_followed_without_the_wrapper(census, tmp_path) -> None:
    py, log = fake_python(tmp_path)
    entry = py.parent / "nx"
    entry.write_text(f"#!{py}\n")
    assert census.bind_switch_gap(str(entry)) == "" and log.exists()


@pytest.mark.parametrize("text", ["", "no shebang\n", "#!/bin/sh\necho not nexus's wrapper\n", "#!/no/such/python\n"])
def test_an_nx_whose_interpreter_cannot_be_found_is_a_gap(census, tmp_path, text) -> None:
    nx = tmp_path / "nx"
    nx.write_text(text)
    gap = census.bind_switch_gap(str(nx))
    assert "could not find the installed nx's interpreter" in gap and "would bind identities" in gap


def test_no_nx_at_all_is_a_gap(census) -> None:
    assert "no nx on PATH" in census.bind_switch_gap("")


def test_main_starts_no_session_and_exits_2_when_the_installed_nx_cannot_honour_the_switch(run, monkeypatch, capsys) -> None:
    started: list[str] = []
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "x")
    monkeypatch.setattr(run.sys, "argv", ["run.py"])
    monkeypatch.setattr(run.shutil, "which", lambda name, *a, **k: f"/fake/{name}")
    monkeypatch.setattr(run, "run_scenario", lambda *a, **k: started.append("scenario") or (True, [], []))
    monkeypatch.setattr(run, "load_script", lambda: started.append("load_script"))
    stub_census(run, monkeypatch, bind_gap="installed nx predates NX_IDENTITY_BIND; the sandbox would bind identities on the operator's endpoint")
    assert run.main() == 2
    assert started == [], "no scenario and no session"
    out = capsys.readouterr().out
    assert "RESTART SANDBOX UNVERIFIED" in out and "installed nx predates NX_IDENTITY_BIND" in out and "no session was started" in out


def test_main_goes_on_when_the_installed_nx_honours_the_switch(run, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "x")
    monkeypatch.setattr(run.sys, "argv", ["run.py"])
    monkeypatch.setattr(run.shutil, "which", lambda name, *a, **k: f"/fake/{name}")
    reached: list[str] = []

    class Stop(Exception):
        pass

    def load():
        reached.append("load_script")
        raise Stop

    monkeypatch.setattr(run, "load_script", load)
    stub_census(run, monkeypatch)
    with pytest.raises(Stop):
        run.main()
    assert reached == ["load_script"], "past the gate, the run proceeds"


# -- the shim refuses what makes an identity ---------------------------------------------------------------------
def shim(run, tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """The sandbox's nx shim in front of a stand-in real nx that logs every call it receives."""
    real_log = tmp_path / "real.log"
    real = tmp_path / "real-nx"
    real.write_text(f'#!/bin/sh\necho "$*" >> "{real_log}"\nexit 0\n')
    real.chmod(0o755)
    refused, identity = tmp_path / "upgrade.log", tmp_path / "identity.log"
    path = tmp_path / "nx"
    path.write_text(run.nx_shim_text(str(real), tmp_path / "marker", refused, identity))
    path.chmod(0o755)
    return path, real_log, refused, identity


def call(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(path), *args], capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("args", [
    ("identity", "create", "--created-by", "restart", "--", "rs_alpha"),
    ("identity", "promote", "--token-file", "/x/t", "--", "i9", "rs_alpha"),
    ("identity", "claim", "rs_alpha", "--take-over"), ("identity", "release", "rs_alpha"),
    ("identity", "rename", "a", "b"), ("identity", "purge", "i9"),
    ("identity", "work", "register", "--kind", "tmux", "--ref", "s"), ("identity", "work", "done", "--kind", "tmux", "--ref", "s"),
    ("--verbose", "identity", "create", "x"),
    ("identity", "alive"),  # reports gone holders to the engine: a write on the operator's identities
])
def test_the_shim_refuses_the_identity_writes_logs_them_and_never_reaches_the_real_nx(run, tmp_path, args) -> None:
    path, real_log, _, identity = shim(run, tmp_path)
    done = call(path, *args)
    assert done.returncode == run.REFUSED and "restart sandbox: nx identity" in done.stderr and "refused" in done.stderr
    assert identity.read_text().splitlines() == [" ".join(args)]
    assert not real_log.exists()
    for word in run.BANNED:  # the restart script logs this line: it must carry no failure word of the log check
        assert word not in done.stderr


@pytest.mark.parametrize("args", [
    ("identity", "show", "--session", SID_A, "--json"), ("identity", "show", "--", "rs_alpha"), ("identity", "show", "--all"),
    ("identity", "work", "list", "--session", SID_A, "--running", "--json"), ("identity", "work", "owners", "--kind", "tmux", "--ref", "s"),
    ("identity", "may-stop", "--kind", "tmux", "--ref", "s"), ("scratch", "list"), ("tuple", "directory", "rs_alpha"),
])
def test_the_shim_passes_reads_and_everything_else_to_the_real_nx(run, tmp_path, args) -> None:
    path, real_log, _, identity = shim(run, tmp_path)
    assert call(path, *args).returncode == 0
    assert real_log.read_text().splitlines() == [" ".join(args)]
    assert not identity.exists()


def test_the_shim_still_refuses_upgrade_into_its_own_log(run, tmp_path) -> None:
    path, real_log, refused, identity = shim(run, tmp_path)
    assert call(path, "upgrade", "--auto").returncode == run.REFUSED
    assert refused.read_text().splitlines() == ["upgrade --auto"] and not real_log.exists() and not identity.exists()


# -- run.py is wired to it ---------------------------------------------------------------------------
def stub_census(run, monkeypatch, *reads, real_sessions: bool = False, bind_gap: str = ""):
    """Make run.py's identity census read *reads* in turn (a dict, or a string: the CensusError message to raise) instead
    of asking nx: the scenario tests that stub the sandbox out run no engine. Default: two empty lists, every session reads
    unbound (*real_sessions* keeps the real per-session check) and the installed nx honours the switch (*bind_gap*)."""
    spec = importlib.util.spec_from_file_location("restart_sandbox_identity_census_stub", SIBLING)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sequence = iter(reads or ({}, {}))

    def read(nx, **_kw):
        got = next(sequence)
        if isinstance(got, str):
            raise module.CensusError(got)
        return got

    monkeypatch.setattr(module, "read", read)
    if not real_sessions:
        monkeypatch.setattr(module, "session_findings", lambda nx, sids, **kw: ([], []))
    monkeypatch.setattr(module, "bind_switch_gap", lambda nx, **kw: bind_gap)
    original = run._load_slash
    monkeypatch.setattr(run, "_load_slash", lambda name="slash_restart": module if name == "identity_census" else original(name))


@pytest.fixture
def staged(run, monkeypatch):
    """run_scenario with the sandbox stubbed (no tmux, no sessions, no engine): only the census can fail it."""
    monkeypatch.setattr(run.Sandbox, "setup", lambda self, crs: None)
    monkeypatch.setattr(run.Sandbox, "entries", lambda self: [])
    monkeypatch.setattr(run, "RUNNERS", {"gate": lambda sb, crs, a, failures, notes: None})
    monkeypatch.setattr(run, "cleanup", lambda sb, notes: [])
    monkeypatch.setattr(run, "upgrade_check", lambda sb: [])
    monkeypatch.setattr(run, "footprint_before", lambda: {})
    monkeypatch.setattr(run, "footprint_after", lambda *a, **k: {})


class _Args:
    keep = False
    model = "sonnet"


def row(n: int, name: str, pid: int, kind: str = "ephemeral", state: str = "lapsed") -> str:
    return f"i{n} {name} ({kind}) {state}, generation 1, pid {pid}, client 8.5.0"


BOB = {"i1": row(1, "bob", 15593, state="live")}


def test_a_scenario_that_leaves_the_operators_identities_as_it_found_them_passes(run, staged, monkeypatch) -> None:
    stub_census(run, monkeypatch, BOB, BOB)
    ok, failures, _ = run.run_scenario("gate", "claude", _Args(), None)
    assert ok and failures == []


def test_a_scenario_whose_sessions_bound_an_identity_named_for_one_of_them_fails(run, staged, monkeypatch) -> None:
    stub_census(run, monkeypatch, BOB, {**BOB, "i24": row(24, "rs_alpha", 1)})
    ok, failures, _ = run.run_scenario("gate", "claude", _Args(), None)
    assert not ok
    assert len(failures) == 1 and "the sandbox bound an identity on the operator's endpoint: i24" in failures[0]


def test_a_new_row_that_holds_a_pid_the_run_recorded_fails_whatever_its_name(run, staged, monkeypatch) -> None:
    monkeypatch.setattr(run.Sandbox, "entries", lambda self: [{"sessionId": "", "pid": 4242}])
    stub_census(run, monkeypatch, BOB, {**BOB, "i24": row(24, "whatever", 4242)})
    ok, failures, _ = run.run_scenario("gate", "claude", _Args(), None)
    assert not ok and len(failures) == 1 and "i24" in failures[0]


def test_a_new_row_that_is_nobody_the_run_knows_is_a_note_and_the_scenario_passes(run, staged, monkeypatch, capsys) -> None:
    stub_census(run, monkeypatch, BOB, {**BOB, "i24": row(24, "someone_else", 99, state="live")})
    ok, failures, _ = run.run_scenario("gate", "claude", _Args(), None)
    assert ok and failures == []
    assert "i24 someone_else" in capsys.readouterr().out


def test_a_scenario_fails_when_the_list_cannot_be_read_after_the_run(run, staged, monkeypatch) -> None:
    stub_census(run, monkeypatch, BOB, "engine down")
    ok, failures, _ = run.run_scenario("gate", "claude", _Args(), None)
    assert not ok and len(failures) == 1 and "could not be read after the run" in failures[0]


def test_a_scenario_whose_list_cannot_be_read_before_the_sessions_start_never_starts_them(run, staged, monkeypatch) -> None:
    started: list[str] = []
    monkeypatch.setattr(run.Sandbox, "setup", lambda self, crs: started.append("setup"))
    stub_census(run, monkeypatch, "engine down")
    ok, failures, _ = run.run_scenario("gate", "claude", _Args(), None)
    assert not ok and started == []
    assert any("CensusError" in f and "engine down" in f for f in failures)


def test_a_kept_scenario_is_still_checked_and_fails_on_a_new_sandbox_identity(run, staged, monkeypatch, tmp_path) -> None:
    """--keep skips cleanup but the census is read-only and runs with the sessions still up."""
    home = tmp_path / "kept"
    home.mkdir()
    monkeypatch.setattr(run.tempfile, "mkdtemp", lambda prefix="": str(home))
    stub_census(run, monkeypatch, BOB, {**BOB, "i24": row(24, "rs_alpha", 1, state="live")})
    keep = _Args()
    keep.keep = True
    run.UNVERIFIED.clear()
    ok, failures, _ = run.run_scenario("gate", "claude", keep, None)
    run.UNVERIFIED.clear()
    assert not ok and len(failures) == 1 and "i24" in failures[0]


def test_a_scenario_whose_session_id_reads_as_bound_fails_even_when_the_list_shows_nothing_new(run, staged, monkeypatch, tmp_path) -> None:
    """The ended identity: on no list, but its session id still resolves to it."""
    nx, log = sessions_nx(tmp_path, {SID_A: BOUND})
    real_which = run.shutil.which
    monkeypatch.setattr(run.shutil, "which", lambda name, *a, **k: nx if name == "nx" else real_which(name, *a, **k))
    monkeypatch.setattr(run.Sandbox, "entries", lambda self: [{"sessionId": SID_A, "pid": 4242}])
    stub_census(run, monkeypatch, BOB, BOB, real_sessions=True)
    ok, failures, _ = run.run_scenario("gate", "claude", _Args(), None)
    assert not ok and len(failures) == 1 and "aaaaaaaa" in failures[0] and "i40" in failures[0]
    assert log.read_text().splitlines() == [f"identity show --session {SID_A} --json"]


def test_a_scenario_whose_sessions_ran_but_left_no_session_id_fails(run, staged, monkeypatch) -> None:
    monkeypatch.setattr(run, "RUNNERS", {"gate": lambda sb, crs, a, failures, notes: sb.before.update({"rs_alpha": {}})})
    stub_census(run, monkeypatch, BOB, BOB, real_sessions=True)
    ok, failures, _ = run.run_scenario("gate", "claude", _Args(), None)
    assert not ok and any("no session id was collected" in f for f in failures)
