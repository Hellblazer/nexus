# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``scripts/check_ci_commit_coverage.py`` (nexus-of2x8).

nexus-of2x8: a code commit whose CI run is cancelled by a later push, itself
followed by a docs-only push, is silently never exercised by pytest -- develop
goes green while carrying untested code. This is remedy (c) from the bead:
DETECT rather than prevent, re-deriving the coverage floor from the GitHub
Actions API and local git history on every invocation rather than requiring a
durable "last exercised sha" record.

``scripts/`` is on ``pythonpath`` via ``[tool.pytest.ini_options]`` in
``pyproject.toml``, so ``check_ci_commit_coverage`` imports directly.

THE MANDATORY FALSIFICATION CHECK lives at the bottom of this file
(``test_falsification_reconstructed_bead_sequence_goes_red_then_green``): it
builds a REAL git repo reproducing the bead's exact measured sequence (a code
commit, cancelled; a docs-only push on top; nothing since) and proves
``check()`` reports it BLOCKED, then proves a subsequent code-touching push
that actually runs pytest clears it. A gate that has never failed proves
nothing.
"""
from __future__ import annotations

import subprocess
import urllib.error
from pathlib import Path

import pytest

import check_ci_commit_coverage as gate

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── is_docs_only_path(): ported byte-for-byte from ci.yml's `changes` job ──


@pytest.mark.parametrize(
    "path,expected",
    [
        ("docs/architecture.md", True),
        ("docs/rdr/rdr-200-foo.md", True),
        ("web/index.html", True),
        ("conexus/CHANGELOG.md", True),
        ("README.md", True),
        ("CHANGELOG.md", True),
        ("CHANGELOG", True),
        ("LICENSE", True),
        # LICENSING.md is NOT covered despite ci.yml's own comment CLAIMING
        # otherwise ("LICENSE* covers LICENSE and LICENSING.md") -- verified
        # against real bash case semantics: `case "LICENSING.md" in
        # LICENSE*)` does NOT match, because "LICENSING"[6] is 'I', not the
        # 'E' the "LICENSE" prefix requires. This is a genuine (harmless --
        # it only means a LICENSING.md-only push over-runs the full matrix
        # rather than under-running it) discrepancy between ci.yml's comment
        # and its actual behavior, found while pinning this predicate
        # against the real workflow rather than the bead's or a comment's
        # prose (nexus-of2x8 report).
        ("LICENSING.md", False),
        # code
        ("src/nexus/pdf_extractor.py", False),
        ("conexus/skills/foo/SKILL.md", False),  # conexus/** is code, not docs
        ("tests/test_foo.py", False),
        ("AGENTS.md", False),  # root *.md other than README is a test input
        # the anchoring fix (code-review-expert Critical, nexus-h18n5): a
        # nested path must not match the unanchored CHANGELOG*/LICENSE* arms
        ("CHANGELOG.d/malicious_code.py", False),
        ("LICENSE-vendor/code.py", False),
    ],
)
def test_is_docs_only_path_matches_ci_yml_predicate(path: str, expected: bool) -> None:
    assert gate.is_docs_only_path(path) is expected


#: EXACT literal text of ci.yml's `case "$f" in ... esac` block (captured
#: 2026-09-22). A strong pin, not just an ordering check: ANY edit to the
#: predicate's patterns, arm order, or whitespace reds this test and forces
#: a deliberate, reviewed update to `is_docs_only_path` alongside it, rather
#: than letting the Python port silently drift from the workflow it claims
#: to reflect (the bead's own instruction: "the bead is a report, the
#: workflow is the source of truth"). DESIGN NOTE: this repo also has
#: precedent (tests/test_docs_only_predicate_git_direct.py,
#: tests/test_dorny_guard_shell_logic.py) for extracting a workflow's `run:`
#: block and executing it live via `bash -c` for maximum fidelity -- that
#: was considered here and rejected as disproportionate: it would require
#: checking out each historical commit into a scratch worktree (the real
#: script hardcodes `git diff ... HEAD`, so "HEAD" must genuinely BE the
#: commit under test) purely to re-derive a predicate that is eight `case`
#: arms of plain string matching. The literal-text pin below gives the same
#: "any drift is loud" guarantee at a fraction of the moving parts.
_CI_YML_CASE_BLOCK = (
    'case "$f" in\n'
    "              docs/*) ;;\n"
    "              web/*) ;;\n"
    "              conexus/CHANGELOG.md) ;;\n"
    "              */*) docs_only=false ;;\n"
    "              README.md) ;;\n"
    "              CHANGELOG*) ;;\n"
    "              LICENSE*) ;;\n"
    "              *) docs_only=false ;;\n"
    "            esac"
)


def test_predicate_order_matches_ci_yml_case_block() -> None:
    """Pin against a LIVE parse of ci.yml's case block, not a copy of its
    text -- an edit to one without the other reds this test instead of the
    two silently drifting apart."""
    ci_yml = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    start = ci_yml.index('case "$f" in')
    end = ci_yml.index("esac", start) + len("esac")
    case_block = ci_yml[start:end]

    assert case_block == _CI_YML_CASE_BLOCK, (
        "ci.yml's doc-only case block changed -- is_docs_only_path in "
        "check_ci_commit_coverage.py must be updated to match, in the SAME "
        "change, or this checker will silently classify commits by a stale "
        "predicate"
    )

    # The arms that resolve docs_only=true, in the order the real case
    # block tries them (a bash `case` takes the FIRST match) -- redundant
    # with the exact-text pin above, kept because it documents the
    # SEMANTIC property (first-match-wins order) the exact text encodes.
    assert case_block.index("docs/*)") < case_block.index("web/*)")
    assert case_block.index("web/*)") < case_block.index("conexus/CHANGELOG.md)")
    assert case_block.index("conexus/CHANGELOG.md)") < case_block.index("*/*)")
    assert case_block.index("*/*)") < case_block.index("README.md)")
    assert case_block.index("README.md)") < case_block.index("CHANGELOG*)")
    assert case_block.index("CHANGELOG*)") < case_block.index("LICENSE*)")
    # every arm before `*/*` marks its file docs-only (`;;` with no assignment);
    # `*/*` and the trailing default `*)` are the only two that assign false.
    assert case_block.count("docs_only=false") == 2


# ── is_docs_only_commit() ───────────────────────────────────────────────────


def test_docs_only_commit_true_when_every_file_is_docs() -> None:
    assert gate.is_docs_only_commit(("docs/a.md", "README.md")) is True


def test_docs_only_commit_false_when_any_file_is_code() -> None:
    assert gate.is_docs_only_commit(("docs/a.md", "src/nexus/foo.py")) is False


def test_docs_only_commit_false_on_empty_changed_files() -> None:
    """Conservative fail-toward-code direction: an empty diff for a single
    commit means the caller could not resolve it, not that nothing changed."""
    assert gate.is_docs_only_commit(()) is False


# ── run_is_code_exercised() / find_last_code_exercised_run() ───────────────


def test_run_is_code_exercised_true_on_a_successful_shard() -> None:
    jobs = {"pytest (Python 3.12, shard 1/4)": "success", "ruff lint": "success"}
    assert gate.run_is_code_exercised(jobs) is True


def test_run_is_code_exercised_false_when_all_pytest_jobs_skipped() -> None:
    """The doc-only fast lane shape: every pytest leg SKIPPED, not absent."""
    jobs = {
        "pytest (Python 3.12, shard 1/4)": "skipped",
        "pytest (Python 3.12, shard 2/4)": "skipped",
        "pytest (lint markers)": "skipped",
        "pytest (mode-declarations census)": "skipped",
        "doc-only fast lane predicate": "success",
    }
    assert gate.run_is_code_exercised(jobs) is False


def test_run_is_code_exercised_false_when_cancelled() -> None:
    """The exact shape of the bead's own cancelled run: concurrency
    cancellation, not a doc-only skip."""
    jobs = {
        "pytest (Python 3.12, shard 1/4)": "cancelled",
        "pytest (Python 3.12, shard 2/4)": "cancelled",
        "service jar (pytest substrate)": "cancelled",
    }
    assert gate.run_is_code_exercised(jobs) is False


def test_find_last_code_exercised_run_returns_the_newest_match() -> None:
    runs = [
        gate.RunRecord("newest", {"pytest (Python 3.12, shard 1/4)": "skipped"}),
        gate.RunRecord("middle", {"pytest (Python 3.12, shard 1/4)": "success"}),
        gate.RunRecord("oldest", {"pytest (Python 3.12, shard 1/4)": "success"}),
    ]
    found = gate.find_last_code_exercised_run(runs)
    assert found is not None
    assert found.head_sha == "middle"


def test_find_last_code_exercised_run_none_when_nothing_qualifies() -> None:
    runs = [gate.RunRecord("a", {"pytest (Python 3.12, shard 1/4)": "cancelled"})]
    assert gate.find_last_code_exercised_run(runs) is None


# ── find_uncovered_commits() ────────────────────────────────────────────────


def test_find_uncovered_commits_flags_only_code_touching_commits() -> None:
    commits = [
        gate.CommitInfo("docs1", ("docs/a.md",)),
        gate.CommitInfo("code1", ("src/nexus/foo.py",)),
        gate.CommitInfo("docs2", ("README.md",)),
    ]
    uncovered = gate.find_uncovered_commits(commits)
    assert [c.sha for c in uncovered] == ["code1"]


def test_find_uncovered_commits_empty_when_all_docs_only() -> None:
    commits = [gate.CommitInfo("d1", ("docs/a.md",)), gate.CommitInfo("d2", ("web/x.html",))]
    assert gate.find_uncovered_commits(commits) == []


# ── fetch_push_runs() / fetch_run_jobs(): thin API wrappers ────────────────


def test_fetch_push_runs_requests_the_right_url() -> None:
    seen_urls = []

    def fake_api(url: str) -> dict:
        seen_urls.append(url)
        return {"workflow_runs": [{"id": 1, "head_sha": "aaa"}]}

    runs = gate.fetch_push_runs("o/r", "tok", "develop", "ci.yml", max_runs=100, api=fake_api)
    assert len(runs) == 1
    assert len(seen_urls) == 1
    assert "branch=develop" in seen_urls[0]
    assert "event=push" in seen_urls[0]
    assert "workflows/ci.yml/runs" in seen_urls[0]


def test_fetch_push_runs_paginates_until_max_runs_or_empty_page() -> None:
    pages = [
        {"workflow_runs": [{"id": i, "head_sha": str(i)} for i in range(100)]},
        {"workflow_runs": [{"id": 100, "head_sha": "100"}]},
        {"workflow_runs": []},
    ]
    calls = {"n": 0}

    def fake_api(url: str) -> dict:
        page = pages[calls["n"]]
        calls["n"] += 1
        return page

    runs = gate.fetch_push_runs("o/r", "tok", "develop", "ci.yml", max_runs=150, api=fake_api)
    assert len(runs) == 101
    # page 1 (100 items, a FULL page -- keep going) + page 2 (1 item, SHORT
    # of the 50 requested -- GitHub's own "no more pages" signal, stop
    # without spending a third call to learn what the short page already
    # told us).
    assert calls["n"] == 2


def test_fetch_run_jobs_returns_name_to_conclusion() -> None:
    def fake_api(url: str) -> dict:
        assert "actions/runs/42/jobs" in url
        return {"jobs": [{"name": "ruff lint", "conclusion": "success"}]}

    jobs = gate.fetch_run_jobs("o/r", 42, "tok", api=fake_api)
    assert jobs == {"ruff lint": "success"}


# ── check(): orchestration, with a real temp git repo + a fake API router ──


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return result.stdout


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    return repo


def _commit(repo: Path, rel_path: str, content: str, message: str) -> str:
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(repo, "add", rel_path)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


class _RunRouter:
    """Fake GitHub API: routes the runs-list URL to a fixed page, and each
    per-run jobs URL to that run's own job dict, keyed by run id."""

    def __init__(self, runs_page: list[dict], jobs_by_run_id: dict[int, dict[str, str]]) -> None:
        self.runs_page = runs_page
        self.jobs_by_run_id = jobs_by_run_id

    def __call__(self, url: str) -> dict:
        if "/runs?" in url or url.endswith("/runs"):
            return {"workflow_runs": self.runs_page}
        for run_id, jobs in self.jobs_by_run_id.items():
            if f"/runs/{run_id}/jobs" in url:
                return {"jobs": [{"name": n, "conclusion": c} for n, c in jobs.items()]}
        raise AssertionError(f"unexpected URL in test router: {url}")


_SUCCESS_JOBS = {
    "doc-only fast lane predicate": "success",
    "pytest (Python 3.12, shard 1/4)": "success",
    "pytest (Python 3.12, shard 2/4)": "success",
    "pytest (Python 3.12, shard 3/4)": "success",
    "pytest (Python 3.12, shard 4/4)": "success",
    "pytest (lint markers)": "success",
    "pytest (mode-declarations census)": "success",
    "pytest-gate": "success",
}
_SKIPPED_JOBS = {
    "doc-only fast lane predicate": "success",
    "pytest (Python 3.12, shard 1/4)": "skipped",
    "pytest (Python 3.12, shard 2/4)": "skipped",
    "pytest (Python 3.12, shard 3/4)": "skipped",
    "pytest (Python 3.12, shard 4/4)": "skipped",
    "pytest (lint markers)": "skipped",
    "pytest (mode-declarations census)": "skipped",
    "pytest-gate": "success",  # skipped == success for branch protection
}
_CANCELLED_JOBS = {
    "doc-only fast lane predicate": "success",
    "pytest (Python 3.12, shard 1/4)": "cancelled",
    "pytest (Python 3.12, shard 2/4)": "cancelled",
    "service jar (pytest substrate)": "cancelled",
    "pytest-gate": "cancelled",
}


def test_check_cannot_verify_without_repo_or_token() -> None:
    assert gate.check("", "", "develop", "ci.yml", "deadbeef", ".", 100) == 2
    assert gate.check("o/r", "", "develop", "ci.yml", "deadbeef", ".", 100) == 2


def test_check_cannot_verify_when_no_runs_found(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit(repo, "README.md", "hi", "init")
    rc = gate.check(
        "o/r", "tok", "develop", "ci.yml", "HEAD", str(repo), 100, api=lambda u: {"workflow_runs": []}
    )
    assert rc == 2


def test_check_cannot_verify_when_no_run_ever_exercised_code(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    sha = _commit(repo, "README.md", "hi", "init")
    router = _RunRouter(
        runs_page=[{"id": 1, "head_sha": sha, "status": "completed"}],
        jobs_by_run_id={1: _SKIPPED_JOBS},
    )
    rc = gate.check("o/r", "tok", "develop", "ci.yml", sha, str(repo), 100, api=router)
    assert rc == 2


def test_check_passes_when_head_itself_is_the_code_exercised_run(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    sha = _commit(repo, "src/nexus/foo.py", "code", "add code")
    router = _RunRouter(
        runs_page=[{"id": 1, "head_sha": sha, "status": "completed"}],
        jobs_by_run_id={1: _SUCCESS_JOBS},
    )
    rc = gate.check("o/r", "tok", "develop", "ci.yml", sha, str(repo), 100, api=router)
    assert rc == 0


def test_check_passes_when_trailing_commits_since_h_are_all_docs_only(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    code_sha = _commit(repo, "src/nexus/foo.py", "code", "add code")
    _commit(repo, "docs/a.md", "doc", "docs only")
    tip_sha = _commit(repo, "README.md", "readme", "more docs")
    router = _RunRouter(
        runs_page=[
            {"id": 2, "head_sha": tip_sha, "status": "completed"},
            {"id": 1, "head_sha": code_sha, "status": "completed"},
        ],
        jobs_by_run_id={2: _SKIPPED_JOBS, 1: _SUCCESS_JOBS},
    )
    rc = gate.check("o/r", "tok", "develop", "ci.yml", tip_sha, str(repo), 100, api=router)
    assert rc == 0


def test_check_blocked_when_a_code_commit_since_h_was_never_exercised(tmp_path: Path) -> None:
    """The class this script exists to catch, in miniature: a code commit
    (cancelled run) followed only by docs-only pushes, with NO in-flight run
    anywhere to explain the gap -- genuinely, permanently uncovered."""
    repo = _init_repo(tmp_path)
    floor_sha = _commit(repo, "src/nexus/base.py", "base", "known-good floor")
    uncovered_sha = _commit(repo, "src/nexus/pdf_extractor.py", "new code", "touches code, run cancelled")
    tip_sha = _commit(repo, "docs/a.md", "doc", "docs only, supersedes the cancelled run")
    router = _RunRouter(
        runs_page=[
            {"id": 3, "head_sha": tip_sha, "status": "completed"},
            {"id": 2, "head_sha": uncovered_sha, "status": "completed"},  # CANCELLED, but CONCLUDED
            {"id": 1, "head_sha": floor_sha, "status": "completed"},
        ],
        jobs_by_run_id={3: _SKIPPED_JOBS, 2: _CANCELLED_JOBS, 1: _SUCCESS_JOBS},
    )
    rc = gate.check("o/r", "tok", "develop", "ci.yml", tip_sha, str(repo), 100, api=router)
    assert rc == 1


# ── run_is_in_flight() / classify_pending_commits(): the in-flight fix ─────
#
# nexus-of2x8 round 2 (2026-09-22): this audit's own FIRST live run went red
# on a false positive -- it is triggered BY the same push that starts
# ci.yml, so it found "the most recent COMPLETED code-exercised run" was the
# PREVIOUS push while the covering run for the current push was still
# `in_progress`, and reported four just-pushed, currently-being-tested
# commits BLOCKED.
#
# Round 2 made an in-flight covering run CANNOT VERIFY (exit 2). Round 3
# corrected that: `workflow_run`/`schedule` (the trigger fix that would have
# made "in flight" the rare case) only fire from the DEFAULT branch
# (verified against GitHub's own docs -- this repo's default branch is
# `main`, which only advances at a release), so a workflow_run-triggered
# copy of this audit added on `develop` would have been DORMANT until the
# next release and then fired using `main`'s stale copy -- a silent
# vacuous-gate absence, worse than the noisy false positive it replaced.
# The trigger stays `on: push`, which means an in-flight covering run is the
# COMMON case, not a rare race -- so exit 2 (a failing exit code) on every
# single code push would be exactly the "an honest check that is useless"
# outcome that gets muted. classify_pending_commits's OUTPUT is unchanged
# (still `(blocked, pending)`); what changed is what `check()` DOES with
# `pending` -- it narrows the CLAIM to exclude those commits (prints them,
# does not fail on them) rather than failing the whole invocation. These are
# the first-class scenarios required as evidence: queued, in_progress,
# in_progress-then-cancelled, and completed-but-skipped.


def test_run_is_in_flight_true_for_queued() -> None:
    assert gate.run_is_in_flight({"status": "queued"}) is True


def test_run_is_in_flight_true_for_in_progress() -> None:
    assert gate.run_is_in_flight({"status": "in_progress"}) is True


def test_run_is_in_flight_false_for_completed() -> None:
    assert gate.run_is_in_flight({"status": "completed", "conclusion": "success"}) is False


def test_classify_pending_commits_pending_when_covered_by_in_flight_head() -> None:
    """A code-touching commit that is an ancestor of an in-flight run's head
    is PENDING, not BLOCKED -- its verdict does not exist yet."""
    commits = [gate.CommitInfo("code1", ("src/nexus/foo.py",))]
    blocked, pending = gate.classify_pending_commits(
        commits, in_flight_head_shas=["still-running-head"],
        is_ancestor_or_equal=lambda c, r: (c, r) == ("code1", "still-running-head"),
    )
    assert blocked == []
    assert [c.sha for c in pending] == ["code1"]


def test_classify_pending_commits_blocked_when_no_in_flight_run_covers_it() -> None:
    """No in-flight run at all (or none whose tree reaches this commit):
    genuinely, currently uncovered."""
    commits = [gate.CommitInfo("code1", ("src/nexus/foo.py",))]
    blocked, pending = gate.classify_pending_commits(
        commits, in_flight_head_shas=[], is_ancestor_or_equal=lambda c, r: False
    )
    assert [c.sha for c in blocked] == ["code1"]
    assert pending == []


def test_classify_pending_commits_docs_only_commits_are_in_neither_list() -> None:
    commits = [gate.CommitInfo("docs1", ("docs/a.md",))]
    blocked, pending = gate.classify_pending_commits(
        commits, in_flight_head_shas=["anything"], is_ancestor_or_equal=lambda c, r: True
    )
    assert blocked == []
    assert pending == []


# ── check()-level in-flight scenarios (the required evidence) ──────────────


def test_check_out_of_scope_not_failing_when_covering_run_is_queued(tmp_path: Path) -> None:
    """SCENARIO 1: queued. The exact race from the audit's own first live
    run -- ci.yml's run for THIS push has not even started executing jobs
    yet. Round 3: this is now a NON-FAILING out-of-scope note, since the
    push trigger makes it the routine case, not a rare race."""
    repo = _init_repo(tmp_path)
    floor_sha = _commit(repo, "src/nexus/base.py", "base", "known-good floor")
    tip_sha = _commit(repo, "src/nexus/pdf_extractor.py", "new code", "just pushed")
    router = _RunRouter(
        runs_page=[
            {"id": 2, "head_sha": tip_sha, "status": "queued"},
            {"id": 1, "head_sha": floor_sha, "status": "completed"},
        ],
        jobs_by_run_id={1: _SUCCESS_JOBS},  # run 2's jobs are never fetched: still queued
    )
    rc = gate.check("o/r", "tok", "develop", "ci.yml", tip_sha, str(repo), 100, api=router)
    assert rc == 0, "a queued covering run must never fail the invocation -- it is out of scope, not a verdict"


def test_check_out_of_scope_not_failing_when_covering_run_is_in_progress(tmp_path: Path) -> None:
    """SCENARIO 2: in_progress. The literal shape of run 35770759430 at the
    moment the audit's own first live run (35770759535) queried it."""
    repo = _init_repo(tmp_path)
    floor_sha = _commit(repo, "src/nexus/base.py", "base", "known-good floor")
    tip_sha = _commit(repo, "src/nexus/pdf_extractor.py", "new code", "just pushed")
    router = _RunRouter(
        runs_page=[
            {"id": 2, "head_sha": tip_sha, "status": "in_progress"},
            {"id": 1, "head_sha": floor_sha, "status": "completed"},
        ],
        jobs_by_run_id={1: _SUCCESS_JOBS},
    )
    rc = gate.check("o/r", "tok", "develop", "ci.yml", tip_sha, str(repo), 100, api=router)
    assert rc == 0, "an in_progress covering run must never fail the invocation"


def test_check_blocked_after_in_progress_run_concludes_cancelled(tmp_path: Path) -> None:
    """SCENARIO 3: in_progress, THEN cancelled. Re-running the SAME audit
    after the in-flight run concludes (as the NEXT push's own audit would,
    since the trigger stays `on: push`) must resolve the out-of-scope note
    to a real BLOCKED -- this is the bead's actual defect, caught the
    moment a fresh push re-derives coverage rather than staying silent
    forever."""
    repo = _init_repo(tmp_path)
    floor_sha = _commit(repo, "src/nexus/base.py", "base", "known-good floor")
    tip_sha = _commit(repo, "src/nexus/pdf_extractor.py", "new code", "just pushed")
    router = _RunRouter(
        runs_page=[
            {"id": 2, "head_sha": tip_sha, "status": "in_progress"},
            {"id": 1, "head_sha": floor_sha, "status": "completed"},
        ],
        jobs_by_run_id={1: _SUCCESS_JOBS},
    )
    rc_while_running = gate.check("o/r", "tok", "develop", "ci.yml", tip_sha, str(repo), 100, api=router)
    assert rc_while_running == 0, "still in flight -- out of scope, not a verdict yet"

    # The run concludes -- cancelled (superseded by a later push, in the
    # real scenario). Re-audit the SAME head with no other change (this
    # models the NEXT push's own from-scratch audit, since nothing here is
    # workflow_run-triggered by the conclusion itself).
    router.runs_page = [
        {"id": 2, "head_sha": tip_sha, "status": "completed"},
        {"id": 1, "head_sha": floor_sha, "status": "completed"},
    ]
    router.jobs_by_run_id[2] = _CANCELLED_JOBS
    rc_after_cancel = gate.check("o/r", "tok", "develop", "ci.yml", tip_sha, str(repo), 100, api=router)
    assert rc_after_cancel == 1, "a concluded-cancelled covering run must resolve to BLOCKED, never stay silent"


def test_check_prints_the_out_of_scope_tail_to_stdout_not_stderr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The out-of-scope note must be VISIBLE (round 3's whole point -- "print
    it, don't stay silent"), and on stdout specifically: a caller scanning
    stderr for trouble must not see it, since exit 0 already says nothing
    failed."""
    repo = _init_repo(tmp_path)
    floor_sha = _commit(repo, "src/nexus/base.py", "base", "known-good floor")
    tip_sha = _commit(repo, "src/nexus/pdf_extractor.py", "new code", "just pushed")
    router = _RunRouter(
        runs_page=[
            {"id": 2, "head_sha": tip_sha, "status": "in_progress"},
            {"id": 1, "head_sha": floor_sha, "status": "completed"},
        ],
        jobs_by_run_id={1: _SUCCESS_JOBS},
    )
    rc = gate.check("o/r", "tok", "develop", "ci.yml", tip_sha, str(repo), 100, api=router)
    assert rc == 0
    out, err = capsys.readouterr()
    assert "OUT OF SCOPE" in out
    assert tip_sha in out
    assert "OUT OF SCOPE" not in err


def test_check_stays_blocked_for_a_completed_run_whose_pytest_jobs_skipped(tmp_path: Path) -> None:
    """SCENARIO 4: completed, but SKIPPED (the doc-only fast lane's own
    shape). The second-order trap named explicitly: this must NOT be read
    as in-flight (it is not -- status is completed) and must NOT become H
    (its pytest jobs never ran) -- a code-touching commit here stays
    BLOCKED, unconditionally."""
    repo = _init_repo(tmp_path)
    floor_sha = _commit(repo, "src/nexus/base.py", "base", "known-good floor")
    uncovered_sha = _commit(repo, "src/nexus/pdf_extractor.py", "new code", "touches code")
    tip_sha = _commit(repo, "docs/a.md", "doc", "docs-only push on top")
    router = _RunRouter(
        runs_page=[
            {"id": 2, "head_sha": tip_sha, "status": "completed"},  # SKIPPED, not in-flight
            {"id": 1, "head_sha": floor_sha, "status": "completed"},
        ],
        jobs_by_run_id={2: _SKIPPED_JOBS, 1: _SUCCESS_JOBS},
    )
    rc = gate.check("o/r", "tok", "develop", "ci.yml", tip_sha, str(repo), 100, api=router)
    assert rc == 1, "a completed-but-skipped run must never excuse a code commit -- BLOCKED, not PENDING"


# ── THE MANDATORY FALSIFICATION CHECK ───────────────────────────────────────
#
# Reconstructs the bead's exact measured sequence end to end through check()
# with a real git repo, and proves the gate goes RED on it, then GREEN once a
# later code-touching push actually runs pytest -- exactly nexus-00's real
# resolution ("nexus-00 pushed bf84cb512 ... so the fast lane does not fire
# and the real matrix runs, covering dbf255efe and bf84cb512 together").


def test_falsification_reconstructed_bead_sequence_goes_red_then_green(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    # A prior known-good floor: some earlier commit whose run genuinely
    # exercised code and succeeded.
    floor_sha = _commit(repo, "src/nexus/base.py", "base", "prior code, fully tested")

    # dbf255efe-equivalent: touches src/nexus/pdf_extractor.py. Its own run
    # started, then was CANCELLED by the next push's concurrency group.
    cancelled_code_sha = _commit(
        repo, "src/nexus/pdf_extractor.py", "extractor v1", "touches pdf_extractor.py"
    )

    # df3a688fd-equivalent: three docs-only commits rebased on top. The tip.
    _commit(repo, "docs/one.md", "one", "docs commit 1")
    _commit(repo, "docs/two.md", "two", "docs commit 2")
    docs_tip_sha = _commit(repo, "docs/three.md", "three", "docs commit 3 (tip)")

    router = _RunRouter(
        runs_page=[
            # The replacement run: doc-only predicate correctly said
            # docs-only against floor_sha..docs_tip_sha's OWN diff would in
            # reality have been cancelled_code_sha..docs_tip_sha, but the
            # predicate itself is not under test here -- what matters is
            # that its jobs report skip, not success. CONCLUDED (status
            # completed): by the time THIS audit run fires, the docs-only
            # push's own ci.yml run finished fast (~55s per the bead).
            {"id": 3, "head_sha": docs_tip_sha, "status": "completed"},
            # The cancelled run: CONCLUDED with conclusion cancelled --
            # cancellation IS a completion (status becomes "completed",
            # conclusion "cancelled"), and by the time the docs-only push's
            # own run exists at all, this one is long since finished being
            # cancelled -- no in-flight run anywhere in this fixture is
            # what makes this scenario genuinely BLOCKED rather than merely
            # out-of-scope.
            {"id": 2, "head_sha": cancelled_code_sha, "status": "completed"},
            # The last run that actually exercised code, further back.
            {"id": 1, "head_sha": floor_sha, "status": "completed"},
        ],
        jobs_by_run_id={3: _SKIPPED_JOBS, 2: _CANCELLED_JOBS, 1: _SUCCESS_JOBS},
    )

    rc_before = gate.check(
        "Hellblazer/nexus", "tok", "develop", "ci.yml", docs_tip_sha, str(repo), 100, api=router
    )
    assert rc_before == 1, "the reconstructed bead sequence must be reported BLOCKED"

    # RESOLUTION: nexus-00's real fix -- a later push that touches code, so
    # the fast lane does not fire and the real matrix runs, covering the
    # cancelled commit and this one together.
    resolved_tip_sha = _commit(
        repo, "src/nexus/pdf_chunker.py", "chunker v1", "touches code again; real matrix runs"
    )
    router.runs_page = [
        {"id": 4, "head_sha": resolved_tip_sha, "status": "completed"},
        *router.runs_page,
    ]
    router.jobs_by_run_id[4] = _SUCCESS_JOBS

    rc_after = gate.check(
        "Hellblazer/nexus", "tok", "develop", "ci.yml", resolved_tip_sha, str(repo), 100, api=router
    )
    assert rc_after == 0, "a later code-touching, fully-tested push must clear the hole"
