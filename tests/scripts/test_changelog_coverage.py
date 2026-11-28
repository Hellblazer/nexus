# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/changelog_coverage.py (RDR-258 Phase 4, nexus-2jomg.5): a commit that changes shipped code and has no changelog
fragment naming its bead is listed (report mode) or fails the run (block mode). Every case runs on a real git repository."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import changelog_coverage as cc
import ledger_mode
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "changelog_coverage.py"


@pytest.fixture(autouse=True)
def _plain_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (ledger_mode.ENV, "GITHUB_ACTIONS"):
        monkeypatch.delenv(name, raising=False)


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=True).stdout.strip()


def commit(repo: Path, subject: str, body: str = "", **names: str | None) -> str:
    """Commit files given as keyword names with ``__`` for ``/`` and ``_X_`` for ``.X`` or ``-``: ``a__b_dot_py``."""
    for key, text in names.items():
        path = repo / key.replace("__", "/").replace("_dot_", ".").replace("_dash_", "-")
        if text is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", subject + (f"\n\n{body}" if body else ""))
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    git(tmp_path, "init", "-q", "-b", "main")
    base = commit(tmp_path, "base", README_dot_md="x\n", changelog__unreleased__README_dot_md="r\n")
    return tmp_path, base


def run(repo: Path, base: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), "--base", base, *args], capture_output=True,
                          text=True, env={**os.environ, **(env or {})}, check=False)


def test_block_mode_fails_a_shipped_commit_with_no_fragment_and_names_the_fragment_to_add(repo: tuple[Path, str]) -> None:
    root, base = repo
    sha = commit(root, "fix(x): a thing (nexus-abc)", src__nexus__a_dot_py="x = 1\n")
    done = run(root, base)
    assert done.returncode == 10 and sha[:10] in done.stderr
    assert "changelog/unreleased/nexus-abc.md" in done.stderr and "src/nexus/a.py" in done.stderr


def test_report_mode_lists_it_and_exits_zero(repo: tuple[Path, str]) -> None:
    root, base = repo
    commit(root, "fix(x): a thing (nexus-abc)", src__nexus__a_dot_py="x = 1\n")
    commit(root, "test(x): its test (nexus-abc)", tests__test_a_dot_py="def test(): pass\n")
    done = run(root, base, env={ledger_mode.ENV: "report", "GITHUB_ACTIONS": "true"})
    assert done.returncode == 0, done.stderr
    out = done.stdout.splitlines()
    assert sum(ln.startswith("LEDGER-FINDING changelog-coverage: ") for ln in out) == 1
    assert any(ln.startswith("::warning title=changelog-coverage::") for ln in out)
    assert "LEDGER-REPORT changelog-coverage: 1 finding(s), 2 item(s) examined (mode=report)" in out


def test_a_fragment_naming_the_bead_clears_it_in_the_same_or_another_commit(repo: tuple[Path, str]) -> None:
    root, base = repo
    commit(root, "fix(x): a thing (nexus-abc)", src__nexus__a_dot_py="x = 1\n", changelog__unreleased__nexus_dash_abc_dot_md="### Fixed\n\n- x\n")
    commit(root, "fix(y): later (nexus-def)", service__src__main__java__B_dot_java="class B {}\n")
    commit(root, "docs: the entry (nexus-def)", changelog__unreleased__nexus_dash_def_dash_cli_dot_md="### Fixed\n\n- y\n")
    done = run(root, base)
    assert done.returncode == 0 and "3 commit(s) examined" in done.stdout, done.stderr


def test_a_fragment_matches_by_bead_stem_and_never_by_a_longer_bead_id(repo: tuple[Path, str]) -> None:
    assert cc.has_fragment("nexus-abc", {"nexus-abc"}) and cc.has_fragment("nexus-abc", {"nexus-abc-cli"})
    assert not cc.has_fragment("nexus-abc", {"nexus-abc.1"}) and not cc.has_fragment("nexus-abc", {"nexus-abcd"})
    root, base = repo
    commit(root, "fix: a (nexus-abc)", src__nexus__a_dot_py="x = 1\n", changelog__unreleased__nexus_dash_abc_dot_1_dot_md="### Fixed\n\n- x\n")
    assert run(root, base).returncode == 10


def test_a_fragment_added_in_the_range_and_deleted_by_the_release_fold_still_counts(repo: tuple[Path, str]) -> None:
    root, base = repo
    commit(root, "fix: a (nexus-abc)", src__nexus__a_dot_py="x = 1\n", changelog__unreleased__nexus_dash_abc_dot_md="### Fixed\n\n- x\n")
    commit(root, "chore: fold the fragments", changelog__unreleased__nexus_dash_abc_dot_md=None)
    assert run(root, base).returncode == 0


def test_only_shipped_code_needs_a_fragment(repo: tuple[Path, str]) -> None:
    root, base = repo
    commit(root, "test: t (nexus-a1)", tests__test_a_dot_py="x\n")
    commit(root, "docs: d (nexus-a2)", docs__a_dot_md="x\n", src__nexus__AGENTS_dot_md="x\n")
    commit(root, "chore: s (nexus-a3)", scripts__a_dot_py="x\n", **{".github__workflows__a.yml": "x\n"})
    commit(root, "feat(plugin): p (nexus-a4)", conexus__hooks__h_dot_py="x\n", sn__hooks__h_dot_py="x\n")
    done = run(root, base)
    assert done.returncode == 0 and "4 commit(s) examined" in done.stdout, done.stderr
    assert all(cc.shipped(p) for p in ("src/nexus/a.py", "service/src/main/resources/db/changelog/x.xml", "mcpb/src/b.py",
                                       "web/install.sh", "web/install.ps1"))
    assert not any(cc.shipped(p) for p in ("web/other.html", "service/src/test/A.java", "src/nexus/README.md", "pyproject.toml"))


def test_a_commit_that_names_no_bead_is_a_finding_unless_its_body_names_one(repo: tuple[Path, str]) -> None:
    root, base = repo
    commit(root, "fix: nameless", src__nexus__a_dot_py="x = 1\n")
    done = run(root, base)
    assert done.returncode == 10 and "names no bead" in done.stderr
    commit(root, "fix: bead in the body", "Part of nexus-zzz.", src__nexus__b_dot_py="x = 1\n", changelog__unreleased__nexus_dash_zzz_dot_md="### Fixed\n\n- z\n")
    out = run(root, base, "--mode", "report")
    assert out.returncode == 0 and "1 finding(s), 2 item(s) examined" in out.stdout, out.stdout


def test_merges_reverts_and_release_prep_are_exempt(repo: tuple[Path, str]) -> None:
    root, base = repo
    git(root, "checkout", "-q", "-b", "feature")
    feature = commit(root, "fix: f (nexus-f1)", src__nexus__f_dot_py="x\n", changelog__unreleased__nexus_dash_f1_dot_md="### Fixed\n\n- f\n")
    git(root, "checkout", "-q", "main")
    commit(root, "chore: main moves", README_dot_md="y\n")
    git(root, "merge", "-q", "--no-ff", "-m", "Merge feature", "feature")
    commit(root, 'Revert "fix: f (nexus-f1)"', src__nexus__f_dot_py=None)
    commit(root, "chore(release): v1.2.3", src__nexus__engine_version_dot_py="V = 1\n")
    done = run(root, base)
    assert done.returncode == 0, done.stderr
    assert feature[:10] not in done.stderr
    assert "2 commit(s) examined" in done.stdout, "the feature commit and the main commit count; the merge, revert and release prep do not"


def test_an_empty_range_is_an_error_in_both_modes(repo: tuple[Path, str]) -> None:
    root, base = repo
    assert run(root, base).returncode == 10
    done = run(root, base, env={ledger_mode.ENV: "report"})
    assert done.returncode == 10 and "LEDGER-VACUOUS changelog-coverage:" in done.stdout


def test_the_default_base_is_the_merge_base_with_the_release_target(repo: tuple[Path, str]) -> None:
    root, base = repo
    git(root, "update-ref", "refs/remotes/origin/main", base)
    commit(root, "fix: a (nexus-abc)", src__nexus__a_dot_py="x = 1\n")
    done = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(root)], capture_output=True, text=True, check=False)
    assert done.returncode == 10 and "nexus-abc" in done.stderr
    git(root, "update-ref", "-d", "refs/remotes/origin/main")
    broke = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(root)], capture_output=True, text=True, check=False)
    assert broke.returncode == 10 and "merge-base" in broke.stderr


def test_a_bad_mode_value_is_an_error(repo: tuple[Path, str]) -> None:
    root, base = repo
    commit(root, "fix: a (nexus-abc)", src__nexus__a_dot_py="x = 1\n")
    done = run(root, base, env={ledger_mode.ENV: "warn"})
    assert done.returncode == 10 and "NX_LEDGER_MODE" in done.stderr
