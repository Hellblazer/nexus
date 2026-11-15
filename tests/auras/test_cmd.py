# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx aura check`` and ``nx aura list`` (RDR-249 Technical Design 2 and 3; bead nexus-mdbds.10.2).

The registry is the CLI's one engine seam (``aura_cmd._templates``); it is replaced here so the
command's output, exit codes and skip reporting are tested without an engine. The real engine's
registry is read in ``test_registry_engine.py``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from nexus.commands import aura_cmd
from nexus.commands.aura_cmd import aura_group
from nexus.config import nexus_config_dir
from nexus.lua_runtime import library

from ._support import TD1_EXAMPLE, minimal
from .test_check import BOARD, BOARD_CI, HEADER


@pytest.fixture
def shipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "shipped"
    root.mkdir()
    monkeypatch.setattr(library, "shipped_root", lambda: root)
    monkeypatch.delenv(library.REPO_ROOT_ENV, raising=False)
    monkeypatch.delenv("NX_AURAS_OFF", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NEXUS_REPO_ROOT", raising=False)
    return root


def _registry(monkeypatch: pytest.MonkeyPatch, templates: list[dict[str, Any]] | None, why: str = "") -> list[int]:
    calls: list[int] = []

    def fake() -> tuple[list[dict[str, Any]] | None, str]:
        calls.append(1)
        return templates, why

    monkeypatch.setattr(aura_cmd, "_templates", fake)
    return calls


def _proc(root: Path, name: str, args: str) -> None:
    (root / f"{name}.lua").write_text(HEADER.format(name=name, args=args), encoding="ascii")


def _td1(root: Path) -> None:
    _proc(root, "ci_red_notice", "sha:string, outcome:string")
    (root / "ci_develop_red.aura.toml").write_text(TD1_EXAMPLE, encoding="utf-8")


def _run(*argv: str):
    return CliRunner().invoke(aura_group, list(argv))


# -- check -----------------------------------------------------------------------------------------


def test_check_passes_a_clean_aura_against_the_registry(shipped: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _td1(shipped)
    _registry(monkeypatch, [BOARD, BOARD_CI])
    res = _run("check")
    assert res.exit_code == 0, res.output
    assert "ci_develop_red" in res.output and "pass" in res.output
    assert "; skipped" not in res.output and "  skipped" not in res.output
    assert "0 with skipped checks" in res.output


def test_check_without_the_engine_runs_the_offline_checks_and_names_each_skipped_one(
    shipped: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _td1(shipped)
    _registry(monkeypatch, None, "the engine is unreachable (test)")
    res = _run("check")
    assert res.exit_code == 0, res.output
    assert "skipped" in res.output and "dictionary" in res.output
    assert "the engine is unreachable (test)" in res.output
    # a skipped check is never shown as a plain pass
    line = next(ln for ln in res.output.splitlines() if ln.startswith("ci_develop_red"))
    assert "skipped" in line


def test_check_without_the_engine_still_refuses_what_needs_no_engine(
    shipped: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _proc(shipped, "ci_red_notice", "sha:string")  # outcome is mapped but not declared
    (shipped / "ci_develop_red.aura.toml").write_text(TD1_EXAMPLE, encoding="utf-8")
    _registry(monkeypatch, None, "down")
    res = _run("check")
    assert res.exit_code == 1
    assert "args.outcome" in res.output and "REFUSED" in res.output
    assert "skipped" in res.output and "dictionary" in res.output


def test_check_refuses_a_field_the_dictionary_lacks_and_exits_non_zero(
    shipped: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _td1(shipped)
    _registry(monkeypatch, [BOARD, {**BOARD_CI, "dimensions": {"from": {}, "kind": {}}}])
    res = _run("check")
    assert res.exit_code == 1
    assert "trigger.match.outcome" in res.output and "args.sha" in res.output


def test_check_names_the_file_and_key_of_a_parse_refusal(shipped: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (shipped / "bad.aura.toml").write_text('zzz = "x"\n', encoding="utf-8")
    _registry(monkeypatch, [BOARD])
    res = _run("check")
    assert res.exit_code == 1
    assert "bad" in res.output and "zzz" in res.output and "parse" in res.output


def test_check_does_not_read_the_registry_when_no_aura_parsed(shipped: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (shipped / "bad.aura.toml").write_text('zzz = "x"\n', encoding="utf-8")
    calls = _registry(monkeypatch, [BOARD])
    _run("check")
    assert calls == []


def test_check_with_a_name_checks_that_aura_only(shipped: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _td1(shipped)
    (shipped / "bad.aura.toml").write_text('zzz = "x"\n', encoding="utf-8")
    _registry(monkeypatch, [BOARD, BOARD_CI])
    res = _run("check", "ci_develop_red")
    assert res.exit_code == 0, res.output
    assert "bad" not in res.output


def test_check_with_an_unknown_name_says_why_and_exits_non_zero(shipped: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _registry(monkeypatch, [BOARD])
    res = _run("check", "ghost")
    assert res.exit_code == 1
    assert "ghost" in res.output and "not_found" in res.output


def test_check_with_no_auras_says_so_and_exits_zero(shipped: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _registry(monkeypatch, [BOARD])
    res = _run("check")
    assert res.exit_code == 0
    assert "no aura files" in res.output.lower()


def test_check_refuses_two_auras_that_share_a_threads_block(shipped: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _proc(shipped, "some_proc", "none")
    for n in ("one", "two"):
        (shipped / f"{n}.aura.toml").write_text(minimal(n).replace("a block", "same"), encoding="utf-8")
    _registry(monkeypatch, [BOARD])
    res = _run("check")
    assert res.exit_code == 1
    assert res.output.count("display.threads") == 2


# -- list ------------------------------------------------------------------------------------------


def test_list_shows_every_aura_off_at_the_default_level(shipped: Path) -> None:
    _td1(shipped)
    res = _run("list")
    assert res.exit_code == 0, res.output
    row = next(ln for ln in res.output.splitlines() if ln.startswith("ci_develop_red"))
    assert "board/ci/nexus-develop--done" in row and "kind=sha" in row and "outcome=failure" in row
    assert "off" in row and "default" in row


def test_list_shows_on_and_the_level_that_decided_it(shipped: Path) -> None:
    _td1(shipped)
    cfg = nexus_config_dir() / "config.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("auras:\n  ci_develop_red: on\n", encoding="utf-8")
    row = next(ln for ln in _run("list").output.splitlines() if ln.startswith("ci_develop_red"))
    assert " on " in row and "global" in row


def test_list_reads_the_repo_level_and_the_env_level(shipped: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _td1(shipped)
    cfg = nexus_config_dir() / "config.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("auras:\n  ci_develop_red: on\n", encoding="utf-8")
    (Path.cwd() / ".nexus.yml").write_text("auras:\n  ci_develop_red: off\n", encoding="utf-8")
    row = next(ln for ln in _run("list").output.splitlines() if ln.startswith("ci_develop_red"))
    assert " off " in row and "repo" in row
    monkeypatch.setenv("NX_AURAS_OFF", "all")
    row = next(ln for ln in _run("list").output.splitlines() if ln.startswith("ci_develop_red"))
    assert " off " in row and "env" in row


def test_list_json_carries_the_same_fields(shipped: Path) -> None:
    _td1(shipped)
    res = _run("list", "--json")
    assert res.exit_code == 0, res.output
    [row] = json.loads(res.output)
    assert row["name"] == "ci_develop_red" and row["root"] == "shipped"
    assert row["trigger"] == {"subspace": "board/ci/nexus-develop--done", "match": {"kind": "sha", "outcome": "failure"}}
    assert (row["state"], row["level"]) == ("off", "default")
    assert row["summary"].startswith("A red develop sha")
    assert row["error"] is None


def test_list_shows_a_refused_file_with_its_refusal(shipped: Path) -> None:
    (shipped / "bad.aura.toml").write_text('zzz = "x"\n', encoding="utf-8")
    res = _run("list")
    assert res.exit_code == 0, res.output
    row = next(ln for ln in res.output.splitlines() if ln.startswith("bad"))
    assert "refused" in row and "zzz" in row
    [j] = json.loads(_run("list", "--json").output)
    assert j["trigger"] is None and "zzz" in j["error"]


def test_list_with_a_bad_config_value_names_the_file_and_key_and_exits_non_zero(shipped: Path) -> None:
    _td1(shipped)
    cfg = nexus_config_dir() / "config.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("auras:\n  ci_develop_red: maybe\n", encoding="utf-8")
    res = _run("list")
    assert res.exit_code == 1
    assert "auras.ci_develop_red" in res.output and "config.yml" in res.output
