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
    for name in (ledger_mode.ENV, ledger_mode.REPORT_FILE_ENV, "GITHUB_ACTIONS"):
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


# ── covered by a citation, not only by a file name ───────────────────────────

def test_a_bead_cited_in_a_fragment_body_or_in_the_changelog_is_covered(repo: tuple[Path, str]) -> None:
    root, base = repo
    commit(root, "fix: a (nexus-abc.2)", src__nexus__a_dot_py="x = 1\n",
           changelog__unreleased__nexus_dash_abc_dot_1_dot_md="### Fixed\n\n- **x.** Also the follow-up. (nexus-abc.1, nexus-abc.2)\n")
    commit(root, "fix: b (nexus-old)", src__nexus__b_dot_py="x = 1\n", CHANGELOG_dot_md="## [1.0.0]\n\n- **b.** (CodeQL, nexus-old)\n")
    done = run(root, base)
    assert done.returncode == 0, done.stderr


def test_a_bead_named_only_outside_parentheses_is_not_a_citation(repo: tuple[Path, str]) -> None:
    root, base = repo
    commit(root, "fix: a (nexus-abc)", src__nexus__a_dot_py="x = 1\n",
           changelog__unreleased__nexus_dash_zzz_dot_md="### Fixed\n\n- **z.** Builds on nexus-abc without citing it.\n")
    assert run(root, base).returncode == 10


def test_a_sibling_bead_cited_by_an_entry_in_the_range_covers_its_epic_but_an_older_release_does_not(repo: tuple[Path, str]) -> None:
    root, base = repo
    commit(root, "feat: part (nexus-ep.8)", src__nexus__a_dot_py="x = 1\n",
           changelog__unreleased__nexus_dash_ep_dot_3_dot_md="### Added\n\n- **The epic.** (nexus-ep.3)\n")
    assert run(root, base).returncode == 0
    history = commit(root, "docs: an earlier release (nexus-x)", CHANGELOG_dot_md="## [0.9.0]\n\n- **old.** (nexus-old2.5)\n")
    commit(root, "feat: another epic's part (nexus-old2.1)", src__nexus__b_dot_py="x = 1\n")
    done = run(root, history)
    assert done.returncode == 10 and "nexus-old2.1" in done.stderr, "a sibling cited only before the range does not cover it"
    assert run(root, base).returncode == 0, "the same lines ADDED in the range do cover it (what a release fold writes)"


# ── a commit that changes no behaviour ───────────────────────────────────────

def test_comment_and_docstring_only_changes_to_code_are_exempt_and_a_string_literal_is_not(repo: tuple[Path, str]) -> None:
    root, base = repo
    py = 'def f():\n    """old doc"""\n    return "a"  # note\n'
    java = 'class A {\n  // old\n  String s() { return "a // not a comment"; }\n}\n'
    commit(root, "chore: seed (nexus-s)", src__nexus__f_dot_py=py, service__src__main__java__A_dot_java=java,
           changelog__unreleased__nexus_dash_s_dot_md="### Added\n\n- s\n")
    seed = git(root, "rev-parse", "HEAD")
    commit(root, "docs: comments (nexus-c1)", src__nexus__f_dot_py=py.replace("old doc", "new doc").replace("# note", "# other note"),
           service__src__main__java__A_dot_java=java.replace("// old", "/* new\n  * block */"))
    done = run(root, seed)
    assert done.returncode == 0, done.stderr
    commit(root, "fix: literal (nexus-c2)", src__nexus__f_dot_py=py.replace('"a"', '"b"'))
    commit(root, "fix: java literal (nexus-c3)", service__src__main__java__A_dot_java=java.replace('"a //', '"b //'))
    commit(root, "fix: signature (nexus-c4)", src__nexus__f_dot_py="def f(x):\n    return 'b'\n")
    done = run(root, seed)
    assert done.returncode == 10
    assert all(b in done.stderr for b in ("nexus-c2", "nexus-c3", "nexus-c4")) and "nexus-c1" not in done.stderr


def test_a_new_or_deleted_shipped_file_is_never_a_comment_change(repo: tuple[Path, str]) -> None:
    root, base = repo
    commit(root, "feat: new file (nexus-n1)", src__nexus__new_dot_py="# only a comment\n")
    commit(root, "refactor: remove (nexus-n2)", src__nexus__new_dot_py=None)
    done = run(root, base)
    assert done.returncode == 10 and "nexus-n1" in done.stderr and "nexus-n2" in done.stderr


def test_a_changelog_none_trailer_exempts_and_says_why_in_the_message(repo: tuple[Path, str]) -> None:
    root, base = repo
    commit(root, "refactor: a (nexus-t1)", "Changelog: none (internal rename)", src__nexus__a_dot_py="y = 1\n")
    assert run(root, base).returncode == 0
    commit(root, "refactor: b (nexus-t2)", "The Changelog: none idea is in prose, not a trailer.", src__nexus__b_dot_py="y = 1\n")
    assert run(root, base).returncode == 10


def test_a_bead_cited_only_in_an_older_changelog_section_is_not_covered(repo: tuple[Path, str]) -> None:
    root, _base = repo
    history = commit(root, "docs: an earlier release (nexus-x)", CHANGELOG_dot_md="## [0.9.0]\n\n- **old.** (nexus-old3)\n")
    commit(root, "fix: a follow-up to a released bead (nexus-old3)", src__nexus__a_dot_py="x = 1\n")
    done = run(root, history)
    assert done.returncode == 10 and "nexus-old3" in done.stderr, "a later change to a released bead wants its own entry"


def test_docstrings_a_user_reads_are_behaviour_but_other_docstrings_are_not(repo: tuple[Path, str]) -> None:
    root, _base = repo
    src = 'def f():\n    """old"""\n    return 1\n'
    seed = commit(root, "chore: seed (nexus-s)", src__nexus__mcp__tool_dot_py=src, src__nexus__commands__cmd_dot_py=src,
                  src__nexus__other_dot_py=src, changelog__unreleased__nexus_dash_s_dot_md="### Added\n\n- s\n")
    commit(root, "docs: other docstring (nexus-d1)", src__nexus__other_dot_py=src.replace("old", "new"))
    assert run(root, seed).returncode == 0
    commit(root, "docs: tool description (nexus-d2)", src__nexus__mcp__tool_dot_py=src.replace("old", "new"))
    commit(root, "docs: cli help (nexus-d3)", src__nexus__commands__cmd_dot_py=src.replace("old", "new"))
    commit(root, "docs: a comment in a tool (nexus-d4)", src__nexus__mcp__tool_dot_py=src.replace("old", "new") + "# note\n")
    done = run(root, seed)
    assert done.returncode == 10 and "nexus-d2" in done.stderr and "nexus-d3" in done.stderr
    assert "nexus-d1" not in done.stderr and "nexus-d4" not in done.stderr, "comments stay exempt there too"


def test_a_github_issue_is_an_item_whose_fragment_is_gh_n(repo: tuple[Path, str]) -> None:
    assert cc.item_ids("fix(preflight): doctor (GH #1689)") == ["gh-1689"]
    assert cc.item_ids("fix: a (nexus-abc.1) Closes #7") == ["nexus-abc.1", "gh-7"]
    root, base = repo
    commit(root, "fix(preflight): doctor (GH #1689)", src__nexus__a_dot_py="x = 1\n")
    assert run(root, base).returncode == 10
    commit(root, "docs: entry (GH #1689)", changelog__unreleased__gh_dash_1689_dot_md="### Fixed\n\n- doctor\n")
    assert run(root, base).returncode == 0


def test_a_range_of_only_release_preparation_warns_and_passes_in_report_mode_only(repo: tuple[Path, str]) -> None:
    root, base = repo
    commit(root, "chore(release): v1.2.3", src__nexus__engine_version_py="V = 1\n")
    commit(root, 'Revert "fix: a"', src__nexus__a_dot_py="x = 1\n")
    done = run(root, base, "--mode", "report", env={"GITHUB_ACTIONS": "true"})
    assert done.returncode == 0, done.stderr
    assert "LEDGER-REPORT changelog-coverage: 0 finding(s), 0 item(s) examined (mode=report)" in done.stdout
    assert "only 2 release-preparation or revert commit(s)" in done.stdout and "::warning title=changelog-coverage::" in done.stdout
    assert run(root, base).returncode == 10, "block mode still refuses a range with nothing to check"
    empty = run(root, git(root, "rev-parse", "HEAD"), "--mode", "report")
    assert empty.returncode == 10 and "LEDGER-VACUOUS" in empty.stdout, "no commit at all is still an error"


@pytest.mark.parametrize(("subject", "exempt"), [
    ("chore(release): v1.2.3", True), ("release: v1.2.3", True), ("release(8.5.9): fold", True), ('Revert "fix: a"', True),
    ("release.yml: pin the runner (nexus-r1)", False), ("release-notes: tidy (nexus-r2)", False), ("releases: x (nexus-r3)", False),
])
def test_release_preparation_subjects_are_exempt_and_lookalikes_are_not(subject: str, exempt: bool) -> None:
    assert bool(cc.EXEMPT_SUBJECT.match(subject)) is exempt
