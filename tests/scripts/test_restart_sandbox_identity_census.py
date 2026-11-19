# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The restart sandbox must bind no RDR-253 identity in the operator's tenant (found 2026-11-19).

The sandbox copies the operator's ``config.yml`` so its sessions' MCP servers and ``nx`` reach the operator's engine; since
8.5.0 each session's SessionStart hook and MCP identity lease then bound an identity there (rows i13-i23, names ``rs_*``).
Three things hold it shut, each tested here with no claude: the sandbox's session environment carries the two switches
(``NX_IDENTITY_BIND=0`` for the hook, ``NX_IDENTITY_LEASE=0`` for the lease), a census of the operator's identities
(``nx identity show --all``, read only) is taken before the sessions start and again after cleanup, and a new identity,
or a census that cannot be read, fails the run. ``nx`` is a stand-in script on PATH, as in test_restart_sandbox_work.py."""
from __future__ import annotations

import importlib.util
import stat
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


def test_check_fails_naming_the_new_ids_and_their_rows(census, tmp_path) -> None:
    nx, _ = fake_nx(tmp_path)
    failures, notes = census.check(nx, {"i1": "x", "i2": "y"})  # i10 and i13 are new against this baseline
    assert len(failures) == 1
    assert "the sandbox bound an identity on the operator's endpoint" in failures[0]
    assert "i10" in failures[0] and "i13" in failures[0] and "rs_nochan" in failures[0] and "i1 " not in failures[0]
    assert notes


def test_check_passes_when_nothing_is_new_and_says_what_it_examined(census, tmp_path) -> None:
    nx, _ = fake_nx(tmp_path)
    failures, notes = census.check(nx, census.parse(SHOW_ALL))
    assert failures == []
    assert any("4 identities before" in n and "4 after" in n and "0 new" in n for n in notes)


def test_check_fails_when_the_second_read_cannot_be_made(census, tmp_path) -> None:
    nx, _ = fake_nx(tmp_path, code=1)
    failures, _ = census.check(nx, {"i1": "x"})
    assert len(failures) == 1 and "could not be read" in failures[0]


# -- run.py is wired to it ---------------------------------------------------------------------------
def stub_census(run, monkeypatch, *reads):
    """Make run.py's identity census read *reads* in turn (a dict, or a string: the CensusError message to raise) instead
    of asking nx: the scenario tests that stub the sandbox out run no engine. Default: two empty lists."""
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


def test_a_scenario_that_leaves_the_operators_identities_as_it_found_them_passes(run, staged, monkeypatch) -> None:
    stub_census(run, monkeypatch, {"i1": "i1 bob"}, {"i1": "i1 bob"})
    ok, failures, _ = run.run_scenario("gate", "claude", _Args(), None)
    assert ok and failures == []


def test_a_scenario_whose_sessions_bound_an_identity_fails(run, staged, monkeypatch) -> None:
    stub_census(run, monkeypatch, {"i1": "i1 bob"}, {"i1": "i1 bob", "i24": "i24 rs_alpha (ephemeral) lapsed"})
    ok, failures, _ = run.run_scenario("gate", "claude", _Args(), None)
    assert not ok
    assert len(failures) == 1 and "the sandbox bound an identity on the operator's endpoint: i24" in failures[0]


def test_a_scenario_fails_when_the_list_cannot_be_read_after_the_run(run, staged, monkeypatch) -> None:
    stub_census(run, monkeypatch, {"i1": "i1 bob"}, "engine down")
    ok, failures, _ = run.run_scenario("gate", "claude", _Args(), None)
    assert not ok and len(failures) == 1 and "could not be read after the run" in failures[0]


def test_a_scenario_whose_list_cannot_be_read_before_the_sessions_start_never_starts_them(run, staged, monkeypatch) -> None:
    started: list[str] = []
    monkeypatch.setattr(run.Sandbox, "setup", lambda self, crs: started.append("setup"))
    stub_census(run, monkeypatch, "engine down")
    ok, failures, _ = run.run_scenario("gate", "claude", _Args(), None)
    assert not ok and started == []
    assert any("CensusError" in f and "engine down" in f for f in failures)
