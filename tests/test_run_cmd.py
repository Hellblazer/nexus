# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx run`` (RDR-240 Technical Design 3; nexus-qo7ie.31).

``nx run <name>`` calls ``run_procedure`` and nothing else, prints the JSON result on
standard output, and shows an error as its kind and detail on standard error with a
non-zero exit. ``nx run --list`` lists the library's procedures with their header summary.
A fake ``run_script`` stands in for the runtime.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

import nexus.lua_runtime as rt
from nexus.cli import main
from nexus.lua_runtime import library
from nexus.lua_runtime.errors import ScriptBusyError, ScriptError, ScriptLimitError


@pytest.fixture(autouse=True)
def _switch_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(library.REPO_ROOT_ENV, raising=False)


def _install(monkeypatch: pytest.MonkeyPatch, fn: object) -> None:
    monkeypatch.setattr(rt, "run_script", fn, raising=False)


def _run(*argv: str, input: str | None = None):
    return CliRunner().invoke(main, ["run", *argv], input=input)


def test_the_result_is_json_on_standard_output(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, dict]] = []

    def fake(name: str, args: dict) -> object:
        seen.append((name, args))
        return {"id": "240-research-3", "text": "é", "n": [1, None]}

    _install(monkeypatch, fake)
    result = _run("research_add")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"id": "240-research-3", "text": "é", "n": [1, None]}
    assert "é" in result.stdout
    assert seen == [("research_add", {})]


def test_arguments_come_from_a_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[dict] = []
    _install(monkeypatch, lambda name, args: seen.append(args) or "ok")
    f = tmp_path / "args.json"
    f.write_text(json.dumps({"rdr": 240, "finding": "é"}), encoding="utf-8")
    result = _run("research_add", "--args-file", str(f))
    assert result.exit_code == 0, result.output
    assert seen == [{"rdr": 240, "finding": "é"}]


def test_arguments_come_from_standard_input_with_a_dash(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict] = []
    _install(monkeypatch, lambda name, args: seen.append(args) or "ok")
    assert _run("gate_record", "-", input='{"bead": "nexus-x"}').exit_code == 0
    assert _run("gate_record", "--args-file", "-", input='{"bead": "nexus-y"}').exit_code == 0
    assert seen == [{"bead": "nexus-x"}, {"bead": "nexus-y"}]


@pytest.mark.parametrize(
    ("payload", "needle"),
    [("[1, 2]", "JSON object"), ('"s"', "JSON object"), ("{bad", "not valid JSON"), ("", "not valid JSON")],
)
def test_arguments_that_are_not_a_json_object_are_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, payload: str, needle: str
) -> None:
    _install(monkeypatch, lambda name, args: pytest.fail("must not run"))
    result = _run("x", "-", input=payload)
    assert result.exit_code == 2
    assert needle in result.output


def test_a_missing_arguments_file_is_a_usage_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install(monkeypatch, lambda name, args: pytest.fail("must not run"))
    result = _run("x", "--args-file", str(tmp_path / "nope.json"))
    assert result.exit_code == 2


def test_an_arguments_path_that_is_a_directory_is_a_usage_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install(monkeypatch, lambda name, args: pytest.fail("must not run"))
    result = _run("x", "--args-file", str(tmp_path))
    assert result.exit_code == 2
    assert "cannot read the arguments" in result.output


def test_arguments_nested_past_the_parser_are_a_usage_error_not_a_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, lambda name, args: pytest.fail("must not run"))
    result = _run("x", "-", input="[" * 200_000)
    assert result.exit_code == 2
    assert "not valid JSON" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_number_in_the_arguments_is_a_usage_error(monkeypatch: pytest.MonkeyPatch, constant: str) -> None:
    _install(monkeypatch, lambda name, args: pytest.fail("must not run"))
    result = _run("x", "-", input='{"n": %s}' % constant)
    assert result.exit_code == 2
    assert constant in result.output


def test_a_second_positional_other_than_a_dash_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, lambda name, args: pytest.fail("must not run"))
    assert _run("x", "file.json").exit_code == 2


@pytest.mark.parametrize(
    ("exc", "line"),
    [
        (ScriptError("args", "rdr: missing required argument"), "args: rdr: missing required argument"),
        (ScriptError("static_check", "verb-ungranted: memory.get (line 4)"), "static_check: verb-ungranted: memory.get (line 4)"),
        (ScriptLimitError("wall_time", 60.0), "limit: wall_time (60.0) reached"),
        (ScriptBusyError("4 workers run"), "busy: 4 workers run"),
        (library.LibraryError("not_found", "no procedure x"), "name: not_found: no procedure x"),
    ],
)
def test_an_error_prints_its_kind_and_detail_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, line: str
) -> None:
    def fake(name: str, args: dict) -> object:
        raise exc

    _install(monkeypatch, fake)
    result = _run("x")
    assert result.exit_code == 1
    assert result.stdout == ""
    assert line in result.stderr


def test_without_a_runtime_it_says_so_and_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(rt, "run_script", raising=False)
    result = _run("x")
    assert result.exit_code == 1
    assert "unavailable: " in result.stderr


def test_a_name_is_required_unless_listing() -> None:
    result = _run()
    assert result.exit_code == 2
    assert "NAME" in result.output or "name" in result.output


def test_list_shows_every_shipped_procedure_with_its_summary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # outside a repo
    result = _run("--list")
    assert result.exit_code == 0, result.output
    for p in library.procedures(cwd=None):
        assert p.name in result.stdout and p.summary in result.stdout
    assert "research_add" in result.stdout and "gate_record" in result.stdout


def test_list_does_not_run_anything(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    _install(monkeypatch, lambda name, args: pytest.fail("must not run"))
    assert _run("--list").exit_code == 0


def test_list_with_a_name_is_a_usage_error() -> None:
    assert _run("--list", "research_add").exit_code == 2


def test_list_says_the_repo_root_is_off_and_how_to_turn_it_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    result = _run("--list")
    assert "NX_PROCEDURES_REPO_ROOT=1" in result.stderr
    monkeypatch.setenv(library.REPO_ROOT_ENV, "1")
    assert "NX_PROCEDURES_REPO_ROOT" not in _run("--list").stderr


def test_list_marks_a_repo_procedure_when_the_root_is_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import subprocess

    top = tmp_path / "r"
    (top / ".nexus" / "procedures").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=top, check=True, capture_output=True, timeout=60)
    (top / ".nexus" / "procedures" / "mine.lua").write_bytes(
        b"-- procedure: mine\n-- summary: my own\n-- args: none\n-- grants: none\nreturn 1\n"
    )
    monkeypatch.chdir(top)
    monkeypatch.setenv(library.REPO_ROOT_ENV, "1")
    result = _run("--list")
    assert result.exit_code == 0, result.output
    (line,) = [ln for ln in result.stdout.splitlines() if ln.startswith("mine")]
    assert "my own" in line and "(repo)" in line


def test_list_survives_a_repo_probe_that_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from nexus.lua_runtime.header import RepoProbeError

    def boom(cwd: object) -> None:
        if cwd is None:
            return None  # no directory, no probe: the real probe_repo answers this without git
        raise RepoProbeError("git did not run")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(library.REPO_ROOT_ENV, "1")
    monkeypatch.setattr(library, "probe_repo", boom)
    result = _run("--list")
    assert result.exit_code == 0
    assert "research_add" in result.stdout
    assert "repo procedures not listed" in result.stderr and "git did not run" in result.stderr
