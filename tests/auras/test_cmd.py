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
from nexus.db.t2.http_tuple_store import HttpTupleStore
from nexus.lua_runtime import library
from nexus.lua_runtime.header import RepoProbeError

from ._support import TD1_EXAMPLE, minimal
from .test_check import BOARD, BOARD_CI, HEADER
from .test_load import _git, needs_git


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
    [row] = json.loads(res.stdout)
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
    [j] = json.loads(_run("list", "--json").stdout)
    assert j["trigger"] is None and "zzz" in j["error"]


def test_list_with_a_bad_config_value_names_the_file_and_key_and_exits_non_zero(shipped: Path) -> None:
    _td1(shipped)
    cfg = nexus_config_dir() / "config.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("auras:\n  ci_develop_red: maybe\n", encoding="utf-8")
    res = _run("list")
    assert res.exit_code == 1
    assert "auras.ci_develop_red" in res.output and "config.yml" in res.output


# -- review fixes (nexus-mdbds.10.2) -------------------------------------------------------------------------------

NOTE = "repo root not scanned (NX_PROCEDURES_REPO_ROOT unset): {n} aura file(s) not checked"


@pytest.fixture
def repo_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo with a shipped root of its own, the repo-root switch off, the working directory at its top."""
    top = tmp_path / "work" / "myrepo"
    top.mkdir(parents=True)
    _git(top, "init", "-q", "-b", "main")
    root = tmp_path / "shipped"
    root.mkdir()
    monkeypatch.setattr(library, "shipped_root", lambda: root)
    monkeypatch.delenv(library.REPO_ROOT_ENV, raising=False)
    monkeypatch.delenv("NX_AURAS_OFF", raising=False)
    monkeypatch.delenv("NEXUS_REPO_ROOT", raising=False)
    monkeypatch.chdir(top)
    return top


def _repo_aura(top: Path, name: str, text: str | None = None) -> None:
    root = top / ".nexus" / "procedures"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.aura.toml").write_text(minimal(name) if text is None else text, encoding="utf-8")


def _stdout_rows(res) -> list[dict[str, Any]]:
    return json.loads(res.stdout)


# -- 3: a misspelled name fails open, so it is warned about --------------------------------------------------------


def _config(text: str) -> Path:
    cfg = nexus_config_dir() / "config.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(text, encoding="utf-8")
    return cfg


def test_list_warns_about_a_configured_name_that_matches_no_aura(shipped: Path) -> None:
    _td1(shipped)
    cfg = _config("auras:\n  ci_develop_red: on\n  ci_develop_redd: on\n")
    res = _run("list")
    assert res.exit_code == 0
    assert f"warning: {cfg}: auras.ci_develop_redd names no aura" in res.stderr
    assert "ci_develop_redd" not in res.stdout


def test_list_warns_about_an_env_name_that_matches_no_aura(shipped: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _td1(shipped)
    monkeypatch.setenv("NX_AURAS_OFF", "ci_develop_reds")
    res = _run("list")
    assert "warning: NX_AURAS_OFF names no aura: ci_develop_reds" in res.stderr
    row = next(ln for ln in res.stdout.splitlines() if ln.startswith("ci_develop_red"))
    assert "default" in row  # the typo turned nothing off, and the warning says so


@pytest.mark.parametrize("value", ["ALL", "All"])
def test_an_uppercase_all_is_warned_about_as_an_unknown_name(
    shipped: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    _td1(shipped)
    monkeypatch.setenv("NX_AURAS_OFF", value)
    assert f"NX_AURAS_OFF names no aura: {value}" in _run("list").stderr


def test_the_exact_word_all_is_not_warned_about(shipped: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _td1(shipped)
    monkeypatch.setenv("NX_AURAS_OFF", "all")
    assert "warning" not in _run("list").stderr


def test_the_warning_goes_to_stderr_so_json_stays_parseable(shipped: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _td1(shipped)
    monkeypatch.setenv("NX_AURAS_OFF", "typo")
    res = _run("list", "--json")
    assert [r["name"] for r in _stdout_rows(res)] == ["ci_develop_red"]
    assert "typo" in res.stderr


def test_check_warns_too_even_when_there_is_no_aura_file(shipped: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _registry(monkeypatch, [BOARD])
    monkeypatch.setenv("NX_AURAS_OFF", "typo")
    res = _run("check")
    assert res.exit_code == 0
    assert "NX_AURAS_OFF names no aura: typo" in res.stderr


# -- 4: the aura files and the config are read from one repo root ---------------------------------------------------


@needs_git
def test_list_from_a_subdirectory_reads_the_repo_root_config_and_aura_files(
    repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(library.REPO_ROOT_ENV, "1")
    _repo_aura(repo_dir, "mine")
    (repo_dir / ".nexus.yml").write_text("auras:\n  mine: on\n", encoding="utf-8")
    sub = repo_dir / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    row = next(ln for ln in _run("list").stdout.splitlines() if ln.startswith("mine"))
    assert " on " in row and "repo" in row


@needs_git
def test_the_probe_is_computed_once_and_shared(repo_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(library.REPO_ROOT_ENV, "1")
    _repo_aura(repo_dir, "mine")
    calls: list[int] = []
    real = aura_cmd.probe_repo

    def counting(cwd):
        calls.append(1)
        return real(cwd)

    monkeypatch.setattr(aura_cmd, "probe_repo", counting)
    monkeypatch.setattr(library, "probe_repo", lambda cwd: pytest.fail("the library probed again"))
    assert _run("list").exit_code == 0
    assert calls == [1]


# -- 5: the repo root is off ----------------------------------------------------------------------------------------


@needs_git
def test_check_says_how_many_repo_aura_files_it_did_not_scan_and_counts_a_skip(
    repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo_aura(repo_dir, "mine")
    _repo_aura(repo_dir, "yours")
    _td1(library.shipped_root())
    _registry(monkeypatch, [BOARD, BOARD_CI])
    res = _run("check")
    assert res.exit_code == 0, res.output
    assert NOTE.format(n=2) in res.stdout.splitlines()
    assert "1 checked, 0 refused, 1 with skipped checks." in res.stdout  # the unscanned root is the one skip


@needs_git
def test_list_says_it_on_stderr(repo_dir: Path) -> None:
    _repo_aura(repo_dir, "mine")
    res = _run("list", "--json")
    assert NOTE.format(n=1) in res.stderr
    assert _stdout_rows(res) == []


@needs_git
def test_no_note_when_the_switch_is_on_or_the_root_holds_no_aura_file(
    repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch, [BOARD])
    (repo_dir / ".nexus" / "procedures").mkdir(parents=True)
    (repo_dir / ".nexus" / "procedures" / "x.lua").write_text("return 1\n", encoding="utf-8")
    assert "not scanned" not in _run("check").output  # no *.aura.toml files
    _repo_aura(repo_dir, "mine")
    monkeypatch.setenv(library.REPO_ROOT_ENV, "1")
    assert "not scanned" not in _run("check").output  # switch on: scanned, not skipped


# -- 6: paths that were only read, never run ------------------------------------------------------------------------


def test_the_real_registry_read_failing_skips_the_dictionary_checks_and_closes_the_store(
    shipped: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _td1(shipped)
    monkeypatch.setenv("NX_SERVICE_URL", "http://127.0.0.1:9")  # nothing listens on the discard port
    monkeypatch.setenv("NX_SERVICE_TOKEN", "unused")
    closed: list[int] = []
    real_close = HttpTupleStore.close
    monkeypatch.setattr(HttpTupleStore, "close", lambda self: (closed.append(1), real_close(self))[1])
    res = _run("check")
    assert res.exit_code == 0, res.output
    assert "pass; skipped: dictionary" in res.stdout
    assert "skipped dictionary: the engine registry could not be read" in res.stdout
    assert closed == [1]


def test_a_probe_that_cannot_answer_with_the_repo_root_on_is_an_error_not_a_traceback(
    shipped: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(library.REPO_ROOT_ENV, "1")

    def boom(cwd):
        raise RepoProbeError("git rev-parse exited 128: dubious ownership")

    monkeypatch.setattr(aura_cmd, "probe_repo", boom)
    for argv in (("list",), ("check",)):
        res = _run(*argv)
        assert res.exit_code == 1
        assert "cannot tell where the repository is" in res.stderr and "dubious ownership" in res.stderr


def test_a_probe_that_cannot_answer_with_the_repo_root_off_still_lists(
    shipped: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _td1(shipped)

    def boom(cwd):
        raise RepoProbeError("no git")

    monkeypatch.setattr(aura_cmd, "probe_repo", boom)
    assert _run("list").exit_code == 0


@pytest.mark.parametrize("seam", ["enumerate_auras", "check_auras"])
def test_a_repo_probe_error_from_the_enumeration_or_the_checks_is_reported(
    shipped: Path, monkeypatch: pytest.MonkeyPatch, seam: str
) -> None:
    _td1(shipped)
    _registry(monkeypatch, [BOARD, BOARD_CI])

    def boom(*a, **kw):
        raise RepoProbeError("git rev-parse did not run (test)")

    monkeypatch.setattr(aura_cmd, seam, boom)
    res = _run("check")
    assert res.exit_code == 1
    assert "cannot tell where the repository is" in res.stderr and "(test)" in res.stderr


@needs_git
def test_a_repo_aura_that_reuses_a_shipped_auras_threads_block_collides_with_it(
    repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shipped_root = library.shipped_root()
    _proc(shipped_root, "some_proc", "none")
    (shipped_root / "one.aura.toml").write_text(minimal("one").replace("a block", "same"), encoding="utf-8")
    monkeypatch.setenv(library.REPO_ROOT_ENV, "1")
    _repo_aura(repo_dir, "two", minimal("two").replace("a block", "same"))
    _registry(monkeypatch, [BOARD])
    res = _run("check")
    assert res.exit_code == 1
    assert res.stdout.count("display.threads") == 2
    assert "one [shipped]: REFUSED" in res.stdout and "two [repo]: REFUSED" in res.stdout


# -- RDR-252 Step 4 part A (nexus-igea9.7): the warning level of `nx aura check` ----------------------------------------


def _owner_aura_file(root: Path, group: str = "elsewhere") -> None:
    _proc(root, "some_proc", "none")
    (root / "mine.aura.toml").write_text(
        minimal("mine").replace('subspace = "board/topic"', 'subspace = "board/ci/nexus-develop--owner"')
        + f'\n[load]\ngroup = "{group}"\n',
        encoding="utf-8",
    )


def _group_reader(monkeypatch: pytest.MonkeyPatch, reader) -> None:
    monkeypatch.setattr(aura_cmd, "_group_reader", lambda: reader)


def test_check_prints_a_serving_group_warning_and_still_exits_zero(shipped: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.auras.load_gate import GroupRecord  # noqa: PLC0415

    _owner_aura_file(shipped)
    _registry(monkeypatch, None, "no engine")
    _group_reader(monkeypatch, lambda n: GroupRecord(n, serves=("other",)))
    res = _run("check")
    assert res.exit_code == 0, res.output
    assert "mine [shipped]: pass; warning: serving_group" in res.output
    assert "warning serving_group load.group" in res.output
    assert "1 checked, 0 refused, 1 with skipped checks, 1 with warnings." in res.output


def test_check_says_skipped_when_the_group_cannot_be_read(shipped: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _owner_aura_file(shipped)
    _registry(monkeypatch, None, "no engine")
    from nexus.auras.load_gate import GroupUnreadable  # noqa: PLC0415

    def down(_name):
        raise GroupUnreadable("the group client is unavailable")

    _group_reader(monkeypatch, down)
    res = _run("check")
    assert res.exit_code == 0, res.output
    assert "skipped serving_group: the group 'elsewhere' could not be read" in res.output
    assert "warning" not in res.output.replace("with warnings", "")


def test_check_says_skipped_when_the_credential_cannot_be_minted(shipped: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RDR-252 Step 4 round 1 (B2): the real reader and the real groups client, over a bearer that raises the way a
    cloud install with its engine down or its credential revoked does. One Skipped line, exit zero, no traceback."""
    import httpx  # noqa: PLC0415
    from nexus import groups  # noqa: PLC0415
    from nexus.db.data_token import DataTokenMintError  # noqa: PLC0415

    _owner_aura_file(shipped)
    _registry(monkeypatch, None, "no engine")

    def dead() -> str:
        raise DataTokenMintError("mint refused: the credential was revoked", status=401)

    sent: list[httpx.Request] = []
    http = httpx.Client(transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(500)))
    monkeypatch.setattr(groups.GroupsClient, "from_config", classmethod(lambda cls, **_k: cls("http://engine", dead, http=http)))
    res = _run("check")
    assert res.exit_code == 0, res.output
    assert res.exception is None, repr(res.exception)
    assert "skipped serving_group: the group 'elsewhere' could not be read" in res.output
    assert sent == []


def test_check_is_quiet_about_the_serving_group_when_it_matches(shipped: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.auras.load_gate import GroupRecord  # noqa: PLC0415

    _owner_aura_file(shipped, group="push-train")
    _registry(monkeypatch, None, "no engine")
    _group_reader(monkeypatch, lambda n: GroupRecord(n, serves=("nexus-develop",)))
    res = _run("check")
    assert "serving_group" not in res.output and "with warnings" not in res.output
