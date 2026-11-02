# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/windows_hand_test.py: a hand run of pytest on qwent's Windows side.

The ssh and scp calls go to a recording fake; git calls that read the local
repo go to the fake too, except where a test names a real repo.
"""
from __future__ import annotations

import importlib.util
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("windows_hand_test", REPO_ROOT / "scripts" / "windows_hand_test.py")
assert _spec is not None and _spec.loader is not None
wht = importlib.util.module_from_spec(_spec)
sys.modules["windows_hand_test"] = wht
_spec.loader.exec_module(wht)


# --- read from the workflow, never copied ----------------------------------------


def test_the_set_is_the_ci_steps_own_arguments_junit_included() -> None:
    import yaml  # noqa: PLC0415 — test-local

    doc = yaml.safe_load(wht.WORKFLOW.read_text(encoding="utf-8"))
    runs = [s["run"] for j in doc["jobs"].values() for s in j.get("steps") or [] if s.get("name") == wht.SET_STEP]
    assert len(runs) == 1, "exactly one CI step carries the Windows set"
    words = shlex.split(runs[0])
    assert wht.ci_set_args() == words[words.index("pytest") + 1:]
    assert "tests/hooks/" in wht.ci_set_args(), "non-vacuity: the set includes the hooks tests"
    assert any(a.startswith("--junitxml") for a in wht.ci_set_args()), "the floor step reads the junit file"


def test_a_missing_step_fails_loud(tmp_path: Path) -> None:
    wf = tmp_path / "wf.yml"
    wf.write_text("jobs:\n  j:\n    steps:\n      - name: other\n        run: echo\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="expected one"):
        wht.ci_set_args(wf)


def test_a_set_step_that_changed_shape_fails_loud(tmp_path: Path) -> None:
    wf = tmp_path / "wf.yml"
    wf.write_text(f"jobs:\n  j:\n    steps:\n      - name: {wht.SET_STEP}\n        run: echo hi\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="shape changed"):
        wht.ci_set_args(wf)


def test_the_ci_checkout_fixup_runs_verbatim_after_the_checkout() -> None:
    """Git for Windows checks tracked symlinks out as stub files; CI's fixup
    step copies their targets in. Without it the daemon resources are missing
    and 70 tests fail (the first hand run, 2026-11-01)."""
    fixup = wht.ci_step_run(wht.FIXUP_STEP)
    assert "120000" in fixup and "Copy-Item" in fixup, "non-vacuity: the step materialises symlinks"
    script = wht.remote_script("wren", "abc123", ["t.py"], bundle=None, fixup=fixup, floor=None)
    assert fixup.rstrip() in script
    assert script.index("git checkout -q -f --detach abc123") < script.index(fixup.rstrip()) < script.index("pytest")


def test_the_set_runs_cis_floor_step_after_pytest() -> None:
    floor = wht.ci_step_run(wht.FLOOR_STEP)
    assert "check_junit_floor.py" in floor, "non-vacuity: the floor step is the junit floor"
    script = wht.remote_script("wren", "abc", ["t.py"], bundle=None, fixup="", floor=floor)
    assert script.index("pytest") < script.index(floor.rstrip()) < script.index("exit $code")
    assert f"$code = {wht.FLOOR_FAILED}" in script


def test_push_paths_are_the_workflows_filter_and_hooks_are_in_it() -> None:
    paths = wht.push_paths()
    assert "src/nexus/hooks/**" in paths and "conexus/hooks/**" in paths


@pytest.mark.parametrize(
    ("changed", "hit"),
    [
        ("src/nexus/hooks/agent_dispatch_record.py", True),
        ("conexus/hooks/scripts/x.py", True),
        ("src/nexus/daemon/replace_engine.py", True),
        ("src/nexus/daemon/other.py", False),
        ("src/nexus/daemon/replace_a/b.py", False),  # `*` in replace_*.py stops at `/`
        ("src/nexus/hooks/deep/nested/x.py", True),  # `**` crosses it
        ("docs/foo.md", False),
    ],
)
def test_check_matches_the_push_filter(changed: str, hit: bool) -> None:
    assert (wht.files_needing_windows([changed]) == [changed]) is hit


# --- the remote script ---------------------------------------------------------


def test_the_remote_script_order_is_fetch_checkout_fixup_verify_run() -> None:
    script = wht.remote_script("wren", "abc123", ["tests/x.py", "-k", "it's"], bundle=r"C:\b\w.bundle",
                               fixup="# fixup", floor=None)
    order = [script.index(part) for part in (
        "trap {",
        "git fetch -q origin",
        "git fetch -q 'C:\\b\\w.bundle' '+refs/win-hand/wren:refs/win-hand/wren'",
        "Remove-Item -LiteralPath $rel -Recurse -Force",
        "git checkout -q -f --detach abc123",
        "# fixup",
        "if ($head -ne 'abc123')",
        "$env:NX_TEST_T2_SUBSTRATE = 'none'",
        "lab-run-win.cmd' uv run --python 3.13 --frozen pytest 'tests/x.py' '-k' 'it''s' -p no:cacheprovider",
        "exit $code",
    )]
    assert order == sorted(order)


@pytest.mark.parametrize(
    "step",
    [
        "clone", "git fetch origin", "bundle fetch", "git clean", "checkout", "checkout fixup",
        "applying the uncommitted changes", "staging the uncommitted changes", "committing the uncommitted changes",
    ],
)
def test_every_setup_step_stops_with_the_setup_code(step: str) -> None:
    """A native command's nonzero exit does not trip ErrorActionPreference;
    each git step is checked, or pytest would run on the previous tree."""
    script = wht.remote_script("wren", "abc", ["t.py"], bundle=r"C:\b", fixup="", floor=None, patch=r"C:\p")
    assert f"Write-Host 'windows_hand_test: {step} failed'; exit {wht.SETUP_FAILED}" in script


def test_a_wrong_head_stops_with_the_setup_code() -> None:
    script = wht.remote_script("wren", "abc", ["t.py"], bundle=None, fixup="", floor=None)
    assert f"if ($head -ne 'abc') {{" in script and f"exit {wht.SETUP_FAILED}" in script


@pytest.mark.parametrize("quote", ["'", "\u2018", "\u2019", "\u201a", "\u201b"])
def test_every_powershell_single_quote_is_doubled(quote: str) -> None:
    quoted = wht._ps_quote(f"a{quote}b")
    assert quoted == f"'a{quote}{quote}b'"


@pytest.mark.parametrize("arg", ["test[a|b]", "x&y", "a>b", "a<b", "100%", 'say"hi"', "c^d"])
def test_an_argument_cmd_would_reparse_is_refused(arg: str) -> None:
    with pytest.raises(SystemExit, match="cmd.exe"):
        wht.main(["--session", "wren", "--", arg], run=_Recorder(on_origin=True))


def test_a_pushed_commit_needs_no_bundle() -> None:
    assert "bundle" not in wht.remote_script("wren", "abc123", ["t.py"], bundle=None, fixup="", floor=None)


# --- main, with a recording fake -------------------------------------------------


class _Recorder:
    def __init__(self, *, on_origin: bool, dirty: bool = False, ssh_code: int = 0, bundle_raises: bool = False) -> None:
        self.on_origin = on_origin
        self.dirty = dirty
        self.ssh_code = ssh_code
        self.bundle_raises = bundle_raises
        self.patch = b""
        self.calls: list[list[str]] = []
        self.scripts: list[str] = []

    def __call__(self, argv: list[str], **_kw: object) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        if argv[0] == "git":
            verb = argv[3]
            if verb == "rev-parse":
                return subprocess.CompletedProcess(argv, 0, stdout=f"sha-of-{argv[-1]}\n")
            if verb == "merge-base":
                return subprocess.CompletedProcess(argv, 0 if self.on_origin else 1, stdout="")
            if verb == "status":
                return subprocess.CompletedProcess(argv, 0, stdout=" M x.py\n" if self.dirty else "")
            if verb == "diff":
                return subprocess.CompletedProcess(argv, 0, stdout=self.patch if "HEAD" in argv else b"")
            if verb == "ls-files":
                return subprocess.CompletedProcess(argv, 0, stdout=b"")
            if verb == "bundle":
                if self.bundle_raises:
                    raise subprocess.CalledProcessError(128, argv)
                Path(argv[argv.index("-q") + 1]).write_bytes(b"bundle")
        if argv[0] == "scp" and argv[2].endswith(".ps1"):
            self.scripts.append(Path(argv[2]).read_text(encoding="utf-8"))
        if argv[0] == "ssh" and "pwsh" in argv[2]:
            return subprocess.CompletedProcess(argv, self.ssh_code, stdout="")
        return subprocess.CompletedProcess(argv, 0, stdout="")


def test_a_commit_in_origin_develop_needs_no_bundle() -> None:
    rec = _Recorder(on_origin=True)
    assert wht.main(["--session", "wren", "--", "tests/x.py"], run=rec) == 0
    assert not any(c[0] == "scp" and c[2].endswith(".bundle") for c in rec.calls)
    assert "checkout -q -f --detach sha-of-HEAD^{commit}" in rec.scripts[-1]


def test_on_origin_means_in_origin_develop_not_any_remote_branch() -> None:
    """A commit only on chas/<branch> or a stale origin/<feature> tracking ref
    is not something the remote's `git fetch origin` brings; only an ancestor
    of origin/develop is."""
    rec = _Recorder(on_origin=True)
    wht.main(["--session", "wren", "--", "t.py"], run=rec)
    [check] = [c for c in rec.calls if c[0] == "git" and c[3] == "merge-base"]
    assert check[4:] == ["--is-ancestor", "sha-of-HEAD^{commit}", "origin/develop"]


def test_an_unpushed_commit_travels_as_an_incremental_bundle_and_its_ref_is_dropped() -> None:
    rec = _Recorder(on_origin=False)
    assert wht.main(["--session", "wren", "--", "tests/x.py"], run=rec) == 0
    up = next(i for i, c in enumerate(rec.calls) if c[0] == "git" and c[3] == "update-ref" and "-d" not in c)
    bundle = next(i for i, c in enumerate(rec.calls) if c[0] == "git" and c[3] == "bundle")
    drop = next(i for i, c in enumerate(rec.calls) if c[0] == "git" and c[3] == "update-ref" and "-d" in c)
    scp = next(i for i, c in enumerate(rec.calls) if c[0] == "scp" and c[2].endswith(".bundle"))
    assert up < bundle < drop < scp
    assert rec.calls[bundle][-2:] == ["refs/win-hand/wren", "^origin/develop"], "incremental over origin/develop"
    assert "refs/win-hand/wren:refs/win-hand/wren" in rec.scripts[-1]


def test_the_temp_ref_is_dropped_even_when_the_bundle_fails() -> None:
    rec = _Recorder(on_origin=False, bundle_raises=True)
    assert wht.main(["--session", "wren", "--", "t.py"], run=rec) == wht.SETUP_FAILED
    assert any(c[0] == "git" and c[3] == "update-ref" and "-d" in c for c in rec.calls)


def test_a_local_setup_failure_exits_with_the_setup_code_not_a_test_code(capsys: pytest.CaptureFixture[str]) -> None:
    def no_such_ref(argv: list[str], **kw: object) -> subprocess.CompletedProcess:
        if argv[0] == "git" and argv[3] == "rev-parse":
            raise subprocess.CalledProcessError(128, argv)
        return _Recorder(on_origin=True)(argv, **kw)

    assert wht.main(["--session", "wren", "--ref", "nope", "--", "t.py"], run=no_such_ref) == wht.SETUP_FAILED
    assert "setup failed" in capsys.readouterr().err


def test_ref_names_the_commit_tested() -> None:
    rec = _Recorder(on_origin=True)
    wht.main(["--session", "wren", "--ref", "abc1234", "--", "t.py"], run=rec)
    assert "checkout -q -f --detach sha-of-abc1234^{commit}" in rec.scripts[-1]


@pytest.mark.parametrize("code", [1, 2, 4, wht.SETUP_FAILED, wht.FLOOR_FAILED])
def test_the_exit_code_comes_back_unchanged(code: int) -> None:
    assert wht.main(["--session", "wren", "--", "t.py"], run=_Recorder(on_origin=True, ssh_code=code)) == code


def test_the_ssh_line_passes_the_exit_code_through_powershell_5() -> None:
    rec = _Recorder(on_origin=True)
    wht.main(["--session", "wren", "--", "t.py"], run=rec)
    line = rec.calls[-1][2]
    assert "if ($LASTEXITCODE -eq $null) { exit 97 } else { exit $LASTEXITCODE }" in line, (
        "pytest's code passes through, and pwsh that never ran is a setup failure, not exit 0"
    )


def test_a_dirty_tree_is_warned_about(capsys: pytest.CaptureFixture[str]) -> None:
    wht.main(["--session", "wren", "--", "t.py"], run=_Recorder(on_origin=True, dirty=True))
    assert "NOT tested" in capsys.readouterr().err


@pytest.mark.parametrize("session", ["a b", "x;y", "../z", "", "con", "LPT1", "caf\u00e9", "x" * 41])
def test_a_session_name_that_could_escape_the_path_is_refused(session: str) -> None:
    with pytest.raises(SystemExit):
        wht.main(["--session", session, "--", "t.py"], run=_Recorder(on_origin=True))


def test_no_tests_named_is_refused() -> None:
    with pytest.raises(SystemExit, match="give pytest arguments"):
        wht.main(["--session", "wren"], run=_Recorder(on_origin=True))


def test_a_kept_clone_is_not_judged_by_an_unset_exit_code() -> None:
    """$LASTEXITCODE is $null in a fresh pwsh, and $null -ne 0 is true: a
    clone check outside the branch that clones failed every run on a kept
    clone (first live run of the r1 fix, 2026-11-01)."""
    script = wht.remote_script("wren", "abc", ["t.py"], bundle=None, fixup="", floor=None)
    [clone_line] = [ln for ln in script.splitlines() if "git clone" in ln]
    assert clone_line.startswith("if (-not (Test-Path")
    assert "windows_hand_test: clone failed" in clone_line, "the clone's check is inside the clone branch"
    assert script.count("clone failed") == 1


def test_set_in_main_runs_the_ci_set_and_its_floor() -> None:
    rec = _Recorder(on_origin=True)
    assert wht.main(["--session", "wren", "--set"], run=rec) == 0
    script = rec.scripts[-1]
    assert " ".join(wht._ps_quote(a) for a in wht.ci_set_args()) in script
    assert wht.ci_step_run(wht.FLOOR_STEP).rstrip() in script


def test_a_run_without_set_has_no_floor() -> None:
    rec = _Recorder(on_origin=True)
    wht.main(["--session", "wren", "--", "t.py"], run=rec)
    assert "check_junit_floor" not in rec.scripts[-1]


def test_a_half_made_clone_is_removed_and_cloned_again() -> None:
    script = wht.remote_script("wren", "abc", ["t.py"], bundle=None, fixup="", floor=None)
    rm = script.index("Remove-Item -LiteralPath 'C:\\build\\hand\\wren\\nexus' -Recurse -Force")
    assert "\\.git\\HEAD" in script[:rm + 200]
    assert rm < script.index("git clone")


# --- --uncommitted: an agent that cannot commit still tests its work ---------------


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    env_git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (repo / "a.py").write_text("one\n", encoding="utf-8")
    subprocess.run([*env_git, "add", "-A"], check=True)
    subprocess.run([*env_git, "commit", "-q", "-m", "base"], check=True)
    return repo


def test_the_patch_carries_tracked_edits_and_untracked_files_but_not_ignored_ones(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    (repo / "a.py").write_text("one\ntwo\n", encoding="utf-8")
    (repo / "new.py").write_text("brand new\n", encoding="utf-8")
    (repo / "ignored.txt").write_text("secret\n", encoding="utf-8")
    patch = wht.uncommitted_patch(subprocess.run, repo)
    assert b"+two" in patch and b"brand new" in patch
    assert b"secret" not in patch
    # It applies to a clean checkout of the same commit.
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(repo), str(clone)], check=True)
    (tmp_path / "p.patch").write_bytes(patch)
    subprocess.run(["git", "-C", str(clone), "apply", "--binary", str(tmp_path / "p.patch")], check=True)
    assert (clone / "a.py").read_text(encoding="utf-8") == "one\ntwo\n"
    assert (clone / "new.py").read_text(encoding="utf-8") == "brand new\n"


def test_a_clean_tree_has_an_empty_patch(tmp_path: Path) -> None:
    assert wht.uncommitted_patch(subprocess.run, _git_repo(tmp_path)) == b""


def test_uncommitted_is_committed_before_the_fixup_so_its_copies_carry_it() -> None:
    """CI's fixup copies each tracked symlink's target over its stub
    (src/nexus/_resources/daemon <- conexus/daemon). Applied after it, an edit
    to conexus/daemon reached the target but not the copy the tests read, and
    the run still said "plus uncommitted changes" (review r1, shown live). So
    the patch is applied and committed locally first, the fixup's hard reset
    keeps it, and HEAD's parent is the commit asked for."""
    rec = _Recorder(on_origin=True)
    rec.patch = b"diff --git a/x b/x\n"
    assert wht.main(["--session", "wren", "--uncommitted", "--", "t.py"], run=rec) == 0
    assert any(c[0] == "scp" and c[2].endswith(".patch") for c in rec.calls)
    script = rec.scripts[-1]
    fixup = wht.ci_step_run(wht.FIXUP_STEP).rstrip()
    order = [script.index(part) for part in (
        "git clean -fdq",
        "git checkout -q -f --detach",
        "git apply --binary --whitespace=nowarn 'C:\\build\\hand\\wren\\win-hand.patch'",
        "git add -A",
        "commit -q --no-verify",
        fixup,
        "$head = (git rev-parse HEAD~1).Trim()",
        "testing sha-of-HEAD^{commit} plus uncommitted changes",
        "pytest",
    )]
    assert order == sorted(order)

def test_uncommitted_on_a_clean_tree_is_refused() -> None:
    with pytest.raises(SystemExit, match="no uncommitted changes"):
        wht.main(["--session", "wren", "--uncommitted", "--", "t.py"], run=_Recorder(on_origin=True))


def test_uncommitted_with_another_ref_is_refused() -> None:
    with pytest.raises(SystemExit, match="drop --ref"):
        wht.main(["--session", "wren", "--uncommitted", "--ref", "abc", "--", "t.py"], run=_Recorder(on_origin=True))


def test_without_uncommitted_no_patch_is_sent_or_applied() -> None:
    rec = _Recorder(on_origin=True)
    rec.patch = b"diff --git a/x b/x\n"
    wht.main(["--session", "wren", "--", "t.py"], run=rec)
    assert not any(c[0] == "scp" and c[2].endswith(".patch") for c in rec.calls)
    assert "git apply" not in rec.scripts[-1]


def test_the_patch_shape_ignores_the_callers_diff_config(tmp_path: Path) -> None:
    """diff.noprefix drops a/ and b/, and diff.external replaces the patch;
    the builder pins its own options so git apply always gets a patch."""
    repo = _git_repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "config", "diff.noprefix", "true"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "diff.external", "cat"], check=True)
    (repo / "a.py").write_text("one\ntwo\n", encoding="utf-8")
    patch = wht.uncommitted_patch(subprocess.run, repo)
    assert b"--- a/a.py" in patch and b"+++ b/a.py" in patch


def test_an_oversized_patch_is_refused() -> None:
    rec = _Recorder(on_origin=True)
    rec.patch = b"x" * (wht.MAX_PATCH_BYTES + 1)
    with pytest.raises(SystemExit, match="over the"):
        wht.main(["--session", "wren", "--uncommitted", "--", "t.py"], run=rec)
