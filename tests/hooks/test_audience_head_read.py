# SPDX-License-Identifier: AGPL-3.0-or-later
"""The strictness keys' ``HEAD`` read, its case table, the index check and the session-root rule (nexus-sby49.8, RDR-259 Decision 1).

Real repositories, and a recording ``git`` on ``PATH`` (``tests/hooks/git_shim.py``) so each case also says how many git
processes ran: exactly one (``cat-file --batch``) whenever the read runs at all, whatever git answers. Both byte copies run every case.
The corpus (``tests/fixtures/audience_corpus.json``) holds the answer rows; this file holds the process counts, the exit-code
cases a corpus layout cannot stage (a hung or failing git) and the index check.
"""
from __future__ import annotations

import json
import os
import sys
import pathlib

import pytest

from tests._module_seam import setattr_in
from tests.hooks.git_shim import GitShim, init_repo, path_without_git, run_git


PROJECT = ".nexus/project.json"
HEAD = ".nexus/project.json at HEAD"


def _j(**settings) -> str:
    return json.dumps({"version": 1, **settings})


@pytest.fixture
def audience(audience_copy):
    return audience_copy


@pytest.fixture
def shim(tmp_path, monkeypatch) -> GitShim:
    return GitShim(tmp_path)  # built before the repos; installed by each test once its repo exists


def _set_wt(repo: pathlib.Path, body: str | None) -> None:
    f = repo / PROJECT
    if body is None:
        f.unlink()
    else:
        f.write_text(body, encoding="utf-8")


def _p(repo) -> dict:
    return {"cwd": str(repo)}


def _probs(s) -> list[tuple[str, str]]:
    return sorted((p.file, p.key) for p in s.problems)


# ---------------------------------------------------------------------------
# The cases of the table
# ---------------------------------------------------------------------------


def test_a_committed_strict_survives_a_working_tree_that_dropped_it(audience, tmp_path, shim, monkeypatch):
    repo = init_repo(tmp_path / "r", {PROJECT: _j(subagent_git="strict")})
    _set_wt(repo, _j())  # the working copy no longer says strict
    shim.install(monkeypatch)
    assert audience.guard_mode(_p(repo)) == "seatbelt" and shim.calls() == [], "the default reads no HEAD"
    assert audience.guard_mode(_p(repo), head=True) == "strict"
    assert shim.verbs() == ["cat-file"]
    assert audience.read_settings(_p(repo), head=True).sources["subagent_git"] == "project (HEAD)"


def test_a_deleted_working_copy_is_still_strict_from_head(audience, tmp_path, shim, monkeypatch):
    repo = init_repo(tmp_path / "r", {PROJECT: _j(subagent_git="strict")})
    _set_wt(repo, None)
    shim.install(monkeypatch)
    assert audience.guard_mode(_p(repo), head=True) == "strict", "the index names the file, so HEAD is read"
    assert shim.verbs() == ["cat-file"]


def test_a_strict_working_tree_needs_no_git_unless_the_report_asks_for_it(audience, tmp_path, shim, monkeypatch):
    repo = init_repo(tmp_path / "r", {PROJECT: _j(subagent_git="strict")})
    shim.install(monkeypatch)
    assert audience.guard_mode(_p(repo), head=True) == "strict" and shim.calls() == []
    audience.read_settings(_p(repo), head=True, full=True)
    assert shim.verbs() == ["cat-file"], "show and check read HEAD whatever the working tree says"


def test_a_commit_that_lacks_the_file_is_absence_not_strict_and_runs_one_process(audience, tmp_path, shim, monkeypatch):
    repo = init_repo(tmp_path / "r", {"README": "x\n"})
    (repo / ".nexus").mkdir()
    (repo / PROJECT).write_text(_j(), encoding="utf-8")
    shim.install(monkeypatch)
    s = audience.read_settings(_p(repo), head=True)
    assert s.values["subagent_git"] == "seatbelt" and s.problems == []
    assert shim.verbs() == ["cat-file"]


def test_an_unborn_head_is_no_head_copy(audience, tmp_path, shim, monkeypatch):
    repo = init_repo(tmp_path / "r", {PROJECT: _j()}, commit=False)
    shim.install(monkeypatch)
    s = audience.read_settings(_p(repo), head=True)
    assert s.values["subagent_git"] == "seatbelt" and s.problems == []
    assert shim.verbs() == ["cat-file"]


@pytest.mark.parametrize("blob,what", [
    ("{not json", "invalid"), ("[1]", "not an object"), ('"x"', "not an object"),
    pytest.param(_j() + " " * 70000, "over 64 KiB", id="over-64-KiB"),  # a 70 KB test id overflows Windows' environment variable
])
def test_a_committed_file_that_cannot_be_used_is_strict_and_reported(audience, tmp_path, shim, monkeypatch, blob, what):
    repo = init_repo(tmp_path / "r", {PROJECT: blob})
    _set_wt(repo, _j())
    shim.install(monkeypatch)
    s = audience.read_settings(_p(repo), head=True)
    assert s.values["subagent_git"] == "strict", what
    assert _probs(s) == [(HEAD, "")] and s.problems[0].forced_strict


def test_the_text_keys_come_from_the_working_tree_never_from_head(audience, tmp_path, shim, monkeypatch):
    repo = init_repo(tmp_path / "r", {PROJECT: _j(preflight="old.sh", brief_questions=["old?"], subagent_git="strict")})
    _set_wt(repo, _j(preflight="new.sh", brief_questions=["new?"]))
    shim.install(monkeypatch)
    s = audience.read_settings(_p(repo), head=True)
    assert s.values["preflight"] == "new.sh" and s.values["brief_questions"] == ["new?"] and s.values["subagent_git"] == "strict"


def test_advice_block_is_the_union_of_head_the_working_tree_and_the_user_file(audience, tmp_path, shim, monkeypatch):
    repo = init_repo(tmp_path / "r", {PROJECT: _j(advice_block=["pattern_kill"])})
    _set_wt(repo, _j(advice_block=["whole_disk_scan"]))
    f = audience.HOME / ".config" / "nexus" / "conexus.json"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"advice_block": ["shared_glob_delete"]}), encoding="utf-8")
    shim.install(monkeypatch)
    assert audience.advice_blocked(_p(repo), head=True) == frozenset({"pattern_kill", "whole_disk_scan", "shared_glob_delete"})
    assert audience.advice_blocked(_p(repo)) == frozenset({"whole_disk_scan", "shared_glob_delete"})


def test_the_text_of_a_problem_in_the_committed_file_is_reported_only_for_the_strictness_keys(audience, tmp_path, shim, monkeypatch):
    repo = init_repo(tmp_path / "r", {PROJECT: _j(subagent_git="loose", preflight="a\nb", zzz=1)})
    _set_wt(repo, _j())
    shim.install(monkeypatch)
    s = audience.read_settings(_p(repo), head=True)
    assert _probs(s) == [(HEAD, "subagent_git")], "preflight and the unknown key are the working tree's to report"
    assert s.values["subagent_git"] == "strict"


# ---------------------------------------------------------------------------
# When git runs at all
# ---------------------------------------------------------------------------


def test_no_working_file_and_an_index_that_does_not_name_it_runs_no_git(audience, tmp_path, shim, monkeypatch):
    repo = init_repo(tmp_path / "r", {"README": "x\n"})
    shim.install(monkeypatch)
    s = audience.read_settings(_p(repo), head=True, full=True)
    assert s.values["subagent_git"] == "seatbelt" and s.problems == [] and shim.calls() == []


def test_the_index_alone_can_make_the_read_happen(audience, tmp_path, shim, monkeypatch):
    repo = init_repo(tmp_path / "r", {PROJECT: _j(subagent_git="strict")}, commit=False)
    run_git(repo, "add", PROJECT)
    _set_wt(repo, None)  # staged, never committed, working copy gone: only the index names it
    shim.install(monkeypatch)
    assert audience.guard_mode(_p(repo), head=True) == "seatbelt"  # an unborn HEAD gives nothing
    assert shim.verbs() == ["cat-file"], "the index named it, so git was asked"


def test_exit_128_is_no_head_copy_and_not_strict(audience, tmp_path, shim, monkeypatch):
    repo = tmp_path / "r"
    (repo / ".git").mkdir(parents=True)  # a .git that is not a repository: cat-file exits 128
    (repo / ".nexus").mkdir()
    (repo / PROJECT).write_text(_j(), encoding="utf-8")
    shim.install(monkeypatch)
    s = audience.read_settings(_p(repo), head=True)
    assert s.values["subagent_git"] == "seatbelt" and s.problems == []
    assert shim.verbs() == ["cat-file"]


# ---------------------------------------------------------------------------
# git itself failing
# ---------------------------------------------------------------------------


def test_git_that_cannot_run_is_strict_when_a_working_file_exists_and_silent_when_none_could(audience, tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "r", {PROJECT: _j()})
    plain = init_repo(tmp_path / "plain", {"README": "x\n"})
    path_without_git(monkeypatch, tmp_path)
    s = audience.read_settings(_p(repo), head=True)
    assert s.values["subagent_git"] == "strict" and _probs(s) == [(HEAD, "")] and s.problems[0].forced_strict
    quiet = audience.read_settings(_p(plain), head=True)
    assert quiet.values["subagent_git"] == "seatbelt" and quiet.problems == []


def test_git_that_cannot_run_is_strict_when_only_the_index_names_the_file(audience, tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "r", {PROJECT: _j(subagent_git="strict")})
    _set_wt(repo, None)
    path_without_git(monkeypatch, tmp_path)
    s = audience.read_settings(_p(repo), head=True)
    assert s.values["subagent_git"] == "strict" and _probs(s) == [(HEAD, "")]


def test_a_two_second_timeout_is_strict_and_reported(audience, tmp_path, shim, monkeypatch):
    repo = init_repo(tmp_path / "r", {PROJECT: _j()})
    shim.install(monkeypatch)
    shim.mode("sleep")
    monkeypatch.setattr(audience, "GIT_TIMEOUT_S", 0.5)
    s = audience.read_settings(_p(repo), head=True)
    assert s.values["subagent_git"] == "strict" and _probs(s) == [(HEAD, "")] and s.problems[0].forced_strict
    assert "timed out" in s.problems[0].what


def test_the_timeout_constant_is_two_seconds():
    import importlib.util  # noqa: PLC0415

    path = pathlib.Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts" / "_audience.py"
    spec = importlib.util.spec_from_file_location("_audience_const", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.GIT_TIMEOUT_S == 2


def test_any_other_nonzero_exit_is_strict_and_reported(audience, tmp_path, shim, monkeypatch):
    repo = init_repo(tmp_path / "r", {PROJECT: _j()})
    shim.install(monkeypatch)
    shim.mode("fail7")
    s = audience.read_settings(_p(repo), head=True)
    assert s.values["subagent_git"] == "strict" and _probs(s) == [(HEAD, "")]
    assert shim.verbs() == ["cat-file"]


def test_a_cat_file_failure_is_strict_and_reported(audience, tmp_path, shim, monkeypatch):
    repo = init_repo(tmp_path / "r", {PROJECT: _j()})
    shim.install(monkeypatch)
    shim.mode("catfail")
    s = audience.read_settings(_p(repo), head=True)
    assert s.values["subagent_git"] == "strict" and _probs(s) == [(HEAD, "")]
    assert shim.verbs() == ["cat-file"], "one process, whatever git answered"


def test_git_output_that_is_not_what_cat_file_prints_is_strict_and_reported(audience, tmp_path, shim, monkeypatch):
    repo = init_repo(tmp_path / "r", {PROJECT: _j()})
    shim.install(monkeypatch)
    for mode in ("junk", "cut"):  # exit 0 with nonsense, and a blob header whose body is shorter than it says
        shim.mode(mode)
        s = audience.read_settings(_p(repo), head=True)
        assert s.values["subagent_git"] == "strict" and _probs(s) == [(HEAD, "")] and s.problems[0].forced_strict, mode
    assert shim.verbs() == ["cat-file", "cat-file"], "one process per read"


def test_a_directory_at_the_path_is_strict_and_reported(audience, tmp_path, shim, monkeypatch):
    repo = init_repo(tmp_path / "r", {PROJECT + "/inner": "x\n"})
    import shutil  # noqa: PLC0415

    shutil.rmtree(repo / PROJECT)
    (repo / PROJECT).write_text(_j(), encoding="utf-8")  # a working file, so the committed copy is asked for
    shim.install(monkeypatch)
    s = audience.read_settings(_p(repo), head=True)
    assert s.values["subagent_git"] == "strict" and _probs(s) == [(HEAD, "")] and "not a file" in s.problems[0].what
    assert shim.verbs() == ["cat-file"]


def test_a_submodule_at_the_path_is_strict_and_reported(audience, tmp_path, shim, monkeypatch):
    repo = init_repo(tmp_path / "r", {"README": "x\n"})
    run_git(repo, "update-index", "--add", "--cacheinfo", "160000," + run_git(repo, "rev-parse", "HEAD").strip() + "," + PROJECT)
    run_git(repo, "commit", "-q", "-m", "gitlink")
    (repo / ".nexus").mkdir(exist_ok=True)
    (repo / PROJECT).write_text(_j(), encoding="utf-8")
    shim.install(monkeypatch)
    s = audience.read_settings(_p(repo), head=True)
    assert s.values["subagent_git"] == "strict" and _probs(s) == [(HEAD, "")] and "not a file" in s.problems[0].what
    assert shim.verbs() == ["cat-file"]


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks")
def test_a_committed_symlink_is_a_blob_of_its_target_text_and_so_is_strict_when_it_does_not_parse(audience, tmp_path, shim, monkeypatch):
    repo = init_repo(tmp_path / "r", {"README": "x\n"})
    (repo / ".nexus").mkdir()
    try:
        os.symlink("elsewhere.json", repo / PROJECT)
    except OSError as exc:
        if sys.platform != "win32":
            raise
        pytest.skip(f"a file symlink needs SeCreateSymbolicLinkPrivilege, which this Windows account lacks ({exc})")
    run_git(repo, "add", PROJECT)
    run_git(repo, "commit", "-q", "-m", "link")
    (repo / PROJECT).unlink()
    (repo / PROJECT).write_text(_j(), encoding="utf-8")
    shim.install(monkeypatch)
    s = audience.read_settings(_p(repo), head=True)
    assert s.values["subagent_git"] == "strict" and _probs(s) == [(HEAD, "")]
    assert shim.verbs() == ["cat-file"]


def test_a_committed_file_over_the_cap_is_strict_with_one_process(audience, tmp_path, shim, monkeypatch):
    repo = init_repo(tmp_path / "r", {PROJECT: _j() + " " * 70000})
    _set_wt(repo, _j())
    shim.install(monkeypatch)
    s = audience.read_settings(_p(repo), head=True)
    assert s.values["subagent_git"] == "strict" and "64 KiB" in s.problems[0].what
    assert shim.verbs() == ["cat-file"]


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="a POSIX owner check: Windows has no uid and the reader makes no ownership test there")
def test_a_repository_owned_by_another_user_is_not_read_and_the_working_tree_decides(audience, tmp_path, shim, monkeypatch):
    repo = init_repo(tmp_path / "r", {PROJECT: _j(subagent_git="strict")})
    _set_wt(repo, _j())
    shim.install(monkeypatch)
    setattr_in(monkeypatch, audience, "os.getuid", lambda: os.getuid() + 1)
    s = audience.read_settings(_p(repo), head=True)
    assert s.values["subagent_git"] == "seatbelt", "the working tree decides"
    assert [(p.file, p.category) for p in s.problems] == [(HEAD, "environment")] and not s.problems[0].forced_strict
    assert shim.calls() == [], "git is not asked about a repository it would refuse"


def test_the_exit_code_decides_never_the_language_of_a_message(audience, tmp_path, shim, monkeypatch):
    repo = init_repo(tmp_path / "r", {PROJECT: _j(subagent_git="strict")})
    _set_wt(repo, _j())
    shim.install(monkeypatch)
    monkeypatch.setenv("LANGUAGE", "de")
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    assert audience.guard_mode(_p(repo), head=True) == "strict"


# ---------------------------------------------------------------------------
# The session root and the payload's checkout
# ---------------------------------------------------------------------------


def _plain(path: pathlib.Path, **project) -> pathlib.Path:
    (path / ".git").mkdir(parents=True)
    if project:
        (path / ".nexus").mkdir()
        (path / PROJECT).write_text(_j(**project), encoding="utf-8")
    return path


def test_claude_project_dir_names_the_session_root_and_the_cwd_checkout_can_only_add(audience, tmp_path, monkeypatch):
    primary = _plain(tmp_path / "primary", preflight="primary.sh")
    other = _plain(tmp_path / "other", subagent_git="strict", preflight="other.sh", advice_block=["pattern_kill"], brief_questions=["no?"])
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(primary))
    s = audience.read_settings(_p(other))
    assert s.values["subagent_git"] == "strict" and s.values["advice_block"] == ["pattern_kill"]
    assert s.values["preflight"] == "primary.sh", "text keys come from the session root only"
    assert s.values["brief_questions"] == [], "the cwd checkout's questions are not read"
    assert s.sources["subagent_git"] == "project (cwd checkout)"
    # the reverse: the session root is strict, the cwd checkout is plain: strict, and a plain copy cannot lower it
    strict_primary = _plain(tmp_path / "primary2", subagent_git="strict")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(strict_primary))
    assert audience.guard_mode(_p(_plain(tmp_path / "plain2", subagent_git="seatbelt"))) == "strict"
    assert audience.guard_mode(_p(_plain(tmp_path / "plain3"))) == "strict"


def test_a_problem_in_the_cwd_checkouts_file_is_reported_for_the_strictness_keys_only(audience, tmp_path, monkeypatch):
    primary = _plain(tmp_path / "primary")
    other = _plain(tmp_path / "other", subagent_git="loose", preflight="a\nb", zzz=1)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(primary))
    s = audience.read_settings(_p(other))
    assert _probs(s) == [(".nexus/project.json (cwd checkout)", "subagent_git")] and s.values["subagent_git"] == "strict"


def test_without_claude_project_dir_the_payloads_checkout_is_the_session_root(audience, tmp_path):
    repo = _plain(tmp_path / "r", subagent_git="strict", preflight="p.sh")
    s = audience.read_settings(_p(repo / "sub"))  # a path inside the repo that does not exist yet
    assert s.values["preflight"] == "p.sh" and s.values["subagent_git"] == "strict"
    assert audience.session_root(_p(repo)) == os.path.realpath(repo)


def test_claude_project_dir_outside_any_repository_falls_back_to_the_payload_checkout(audience, tmp_path, monkeypatch):
    repo = _plain(tmp_path / "r", subagent_git="strict")
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(bare))
    assert audience.session_root(_p(repo)) == os.path.realpath(repo)
    assert audience.guard_mode(_p(repo)) == "strict"
    for unusable in ("$HOME/x", "relative", ""):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", unusable)
        assert audience.session_root(_p(repo)) == os.path.realpath(repo)


def test_a_linked_worktree_on_a_commit_without_the_file_only_adds_to_the_primary(audience, tmp_path, shim, monkeypatch):
    primary = init_repo(tmp_path / "primary", {PROJECT: _j(subagent_git="strict", preflight="p.sh")})
    wt = tmp_path / "wt"
    run_git(primary, "worktree", "add", "-q", "--orphan", "-b", "old", str(wt))
    assert (wt / ".git").is_file() and not (wt / PROJECT).exists()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(primary))
    s = audience.read_settings(_p(wt))
    assert s.values["subagent_git"] == "strict" and s.values["preflight"] == "p.sh"
    # the primary's working copy drops strict, and only its committed copy still says it
    _set_wt(primary, _j())
    shim.install(monkeypatch)
    assert audience.guard_mode(_p(wt)) == "seatbelt"
    assert audience.guard_mode(_p(wt), head=True) == "strict"
    # with no CLAUDE_PROJECT_DIR the worktree is its own session root, and it has no file
    monkeypatch.delenv("CLAUDE_PROJECT_DIR")
    assert audience.guard_mode(_p(wt), head=True) == "seatbelt"


# ---------------------------------------------------------------------------
# The index check
# ---------------------------------------------------------------------------


def _gd(audience, repo: pathlib.Path) -> str:
    gd = audience.git_dir(str(repo))
    assert gd is not None
    return gd


def test_a_version_2_index_that_names_the_file_says_yes_and_one_that_does_not_says_no(audience, tmp_path):
    with_file = init_repo(tmp_path / "a", {PROJECT: _j(), "z.txt": "z\n"})
    without = init_repo(tmp_path / "b", {"z.txt": "z\n"})
    assert audience.index_names_project(_gd(audience, with_file)) == "yes"
    assert audience.index_names_project(_gd(audience, without)) == "no"
    assert audience.index_names_project(str(tmp_path / "no-such-gitdir")) == "no"


def test_a_version_3_index_is_searched_too(audience, tmp_path):
    repo = init_repo(tmp_path / "r", {PROJECT: _j()})
    (repo / "intent.txt").write_text("i\n", encoding="utf-8")
    run_git(repo, "add", "-N", "intent.txt")  # an intent-to-add entry forces index version 3
    raw = (pathlib.Path(_gd(audience, repo)) / "index").read_bytes()
    assert raw[:4] == b"DIRC" and int.from_bytes(raw[4:8], "big") == 3, "the fixture did not produce a version 3 index"
    assert audience.index_names_project(_gd(audience, repo)) == "yes"


def test_a_version_4_index_cannot_be_searched_and_says_unknown(audience, tmp_path):
    repo = init_repo(tmp_path / "r", {"z.txt": "z\n"})
    run_git(repo, "update-index", "--index-version", "4")
    raw = (pathlib.Path(_gd(audience, repo)) / "index").read_bytes()
    assert int.from_bytes(raw[4:8], "big") == 4
    assert audience.index_names_project(_gd(audience, repo)) == "unknown"


def test_a_linked_worktrees_own_index_is_the_one_searched(audience, tmp_path):
    primary = init_repo(tmp_path / "primary", {PROJECT: _j()})
    wt = tmp_path / "wt"
    run_git(primary, "worktree", "add", "-q", "--orphan", "-b", "bare", str(wt))
    assert audience.git_dir(str(wt)) is not None and audience.git_dir(str(wt)).endswith(os.path.join("worktrees", "wt"))
    assert audience.index_names_project(audience.git_dir(str(wt))) == "no"
    run_git(wt, "checkout", "-q", "main", "--", PROJECT)
    assert audience.index_names_project(audience.git_dir(str(wt))) == "yes"
    assert audience.index_names_project(_gd(audience, primary)) == "yes"


def test_a_split_or_sparse_index_says_unknown(audience, tmp_path):
    split = init_repo(tmp_path / "split", {"z.txt": "z\n"})
    run_git(split, "update-index", "--split-index")
    assert any(n.startswith("sharedindex.") for n in os.listdir(_gd(audience, split))), "git made no shared index"
    assert audience.index_names_project(_gd(audience, split)) == "unknown"
    sparse = init_repo(tmp_path / "sparse", {"z.txt": "z\n"})
    info = pathlib.Path(_gd(audience, sparse)) / "info"
    info.mkdir(exist_ok=True)
    (info / "sparse-checkout").write_text("/*\n", encoding="utf-8")
    assert audience.index_names_project(_gd(audience, sparse)) == "unknown"


def test_the_byte_search_is_bounded_and_a_truncated_miss_is_unknown(audience, tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "r", {"a.txt": "a\n", PROJECT: _j()})
    gd = _gd(audience, repo)
    monkeypatch.setattr(audience, "INDEX_SEARCH_MAX_BYTES", 40)
    assert audience.index_names_project(gd) == "unknown", "the name lies past the bound and the file is longer than it"
    monkeypatch.setattr(audience, "INDEX_SEARCH_MAX_BYTES", 1 << 20)
    assert audience.index_names_project(gd) == "yes"
    assert audience.INDEX_SEARCH_MAX_BYTES == 1 << 20
    # (the entry-walk's own bound, a long index with every path before the file, is
    # test_a_large_index_still_finds_the_project_file_and_a_scan_past_the_bound_is_unknown below)


def test_the_search_bound_constant_is_a_named_few_mib():
    import importlib.util  # noqa: PLC0415

    path = pathlib.Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts" / "_audience.py"
    spec = importlib.util.spec_from_file_location("_audience_const2", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert 1 << 20 <= mod.INDEX_SEARCH_MAX_BYTES <= 16 << 20


def test_a_sparse_index_entry_or_extension_says_unknown(audience, tmp_path):
    """A sparse index folds `.nexus/` into a directory entry (the path ends at its NUL) and carries an `sdir` extension."""
    header = b"DIRC" + (2).to_bytes(4, "big") + (1).to_bytes(4, "big")
    gd = tmp_path / "sparse-0"
    gd.mkdir()
    (gd / "index").write_bytes(header + b"\0" * 62 + b".nexus/\0\0\0\0")
    assert audience.index_names_project(str(gd)) == "unknown"
    # A sparse index with the extension but no `.nexus/` entry: entries sort by path, so the scan reaches README, which sorts after
    # the file, without meeting the directory entry that would fold it: the file is not there.
    gd = tmp_path / "sparse-1"
    gd.mkdir()
    (gd / "index").write_bytes(header + b"\0" * 62 + b"README\0\0" + b"sdir" + (0).to_bytes(4, "big"))
    assert audience.index_names_project(str(gd)) == "no"
    plain = tmp_path / "plain-gitdir"
    plain.mkdir()
    (plain / "index").write_bytes(header + b"\0" * 62 + b"README\0\0")
    assert audience.index_names_project(str(plain)) == "no"


def test_a_staged_deletion_is_the_tables_first_row_and_reads_no_head(audience, tmp_path, shim, monkeypatch):
    """Staged, uncommitted removal: the working file is gone and the index no longer names it, though HEAD still has it.
    Decision 1's table says "no working-tree file and the index does not name it: not read", so nothing is read from HEAD."""
    repo = init_repo(tmp_path / "r", {PROJECT: _j(subagent_git="strict"), "z.txt": "z\n"})
    run_git(repo, "rm", "-q", PROJECT)
    shim.install(monkeypatch)
    assert audience.guard_mode(_p(repo), head=True) == "seatbelt" and shim.calls() == []


# ---------------------------------------------------------------------------
# nexus-sby49.14: advice_block takes the union whatever subagent_git says, and a large index is not a reason to run git
# ---------------------------------------------------------------------------

_COMMITTED_ROW = "whole_disk_scan"


def _repo_whose_head_has_a_row_the_working_copy_dropped(tmp_path, *, working: str):
    repo = init_repo(tmp_path / "r", {PROJECT: _j(subagent_git="strict", advice_block=[_COMMITTED_ROW])})
    _set_wt(repo, working)
    return repo


def test_a_committed_advice_row_survives_a_working_tree_that_is_already_strict(audience, tmp_path, shim, monkeypatch):
    repo = _repo_whose_head_has_a_row_the_working_copy_dropped(tmp_path, working=_j(subagent_git="strict"))
    shim.install(monkeypatch)
    assert audience.advice_blocked(_p(repo)) == frozenset() and shim.calls() == [], "the default reads no HEAD"
    assert audience.advice_blocked(_p(repo), head=True) == {_COMMITTED_ROW}
    assert shim.verbs() == ["cat-file"], "one git process, though the working tree is already strict"


def test_a_committed_advice_row_survives_a_user_file_that_is_already_strict(audience, tmp_path, shim, monkeypatch):
    from tests.hooks.audience_harness import strict_home  # noqa: PLC0415

    strict_home(audience.HOME)
    repo = _repo_whose_head_has_a_row_the_working_copy_dropped(tmp_path, working=_j())
    shim.install(monkeypatch)
    assert audience.advice_blocked(_p(repo), head=True) == {_COMMITTED_ROW}
    assert shim.verbs() == ["cat-file"]


def test_the_guard_and_the_clause_still_skip_head_when_the_answer_is_already_strict(audience, tmp_path, shim, monkeypatch):
    """Only the advice answer depends on HEAD once strict: guard_mode and forced_strict_clause keep their no-git path."""
    repo = _repo_whose_head_has_a_row_the_working_copy_dropped(tmp_path, working=_j(subagent_git="strict"))
    shim.install(monkeypatch)
    assert audience.guard_mode(_p(repo), head=True) == "strict"
    assert audience.forced_strict_clause(_p(repo), head=True) is None
    assert shim.calls() == []


@pytest.mark.parametrize("reader", ["guard_mode", "advice_blocked", "forced_strict_clause"])
def test_every_reader_the_hooks_call_takes_head(audience, tmp_path, shim, monkeypatch, reader):
    repo = init_repo(tmp_path / "r", {PROJECT: _j(subagent_git="strict", advice_block=[_COMMITTED_ROW])})
    _set_wt(repo, _j())
    shim.install(monkeypatch)
    answer = getattr(audience, reader)(_p(repo), head=True)
    assert answer in ("strict", frozenset({_COMMITTED_ROW}), None), (reader, answer)
    assert getattr(audience, reader)(_p(repo)) in ("seatbelt", frozenset(), None), "and without head the working tree alone answers"


def _index(entries: "list[bytes]", version: int = 2) -> bytes:
    out = b"DIRC" + version.to_bytes(4, "big") + len(entries).to_bytes(4, "big")
    for path in entries:
        entry = b"\0" * 62 + path + b"\0"
        out += entry + b"\0" * (-len(entry) % 8)
    return out + b"\0" * 20


@pytest.mark.parametrize("paths,answer", [
    ([b"README", b"src/a.py"], "no"),
    ([b".gitignore", b".nexus/other.json", b"README"], "no"),
    ([b".gitignore", b".nexus/project.json", b"README"], "yes"),
    ([b".github/x.yml", b".nexus/", b"README"], "unknown"),
    ([], "no"),
])
def test_the_scan_walks_sorted_entries_and_stops_at_the_first_path_after_the_file(audience, tmp_path, paths, answer):
    gd = tmp_path / "gitdir"
    gd.mkdir()
    (gd / "index").write_bytes(_index(paths))
    assert audience.index_names_project(str(gd)) == answer


def test_a_large_index_with_nothing_for_the_project_is_no_not_unknown(audience, tmp_path, monkeypatch):
    """The old byte search called anything past its bound "unknown" and so ran git for a plain large repository."""
    gd = tmp_path / "gitdir"
    gd.mkdir()
    (gd / "index").write_bytes(_index([f"src/f{i:05d}.py".encode() for i in range(3000)]))
    monkeypatch.setattr(audience, "INDEX_SEARCH_MAX_BYTES", 4096)
    assert (gd / "index").stat().st_size > 4096 * 10
    assert audience.index_names_project(str(gd)) == "no"


def test_a_large_index_still_finds_the_project_file_and_a_scan_past_the_bound_is_unknown(audience, tmp_path, monkeypatch):
    gd = tmp_path / "gitdir"
    gd.mkdir()
    early = [b".nexus/project.json"] + [f"src/f{i:05d}.py".encode() for i in range(3000)]
    (gd / "index").write_bytes(_index(early))
    monkeypatch.setattr(audience, "INDEX_SEARCH_MAX_BYTES", 4096)
    assert audience.index_names_project(str(gd)) == "yes"
    late = tmp_path / "late"
    late.mkdir()
    # Every entry sorts before the file ('.' is the lowest printable path byte), so the bound is reached before the answer.
    (late / "index").write_bytes(_index([f".a{i:05d}".encode() for i in range(3000)]))
    assert audience.index_names_project(str(late)) == "unknown"


def test_a_plain_repository_with_a_large_index_runs_no_git_for_settings(audience, tmp_path, shim, monkeypatch):
    repo = init_repo(tmp_path / "r", {PROJECT: _j(), **{f"src/f{i:04d}.py": "x\n" for i in range(300)}})
    _set_wt(repo, None)  # the working file is gone and the index still names it: git is asked
    shim.install(monkeypatch)
    audience.guard_mode(_p(repo), head=True)
    assert shim.verbs() == ["cat-file"]
    plain = init_repo(tmp_path / "plain", {f"src/f{i:04d}.py": "x\n" for i in range(300)})
    monkeypatch.setattr(audience, "INDEX_SEARCH_MAX_BYTES", 512)
    shim.install(monkeypatch)
    before = len(shim.calls())
    audience.guard_mode(_p(plain), head=True)
    assert len(shim.calls()) == before, "no settings file and an index that does not name one: no git"
