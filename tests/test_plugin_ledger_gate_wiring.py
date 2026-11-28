# SPDX-License-Identifier: AGPL-3.0-or-later
"""The plugin ledger is a gate on the pull request into main (RDR-258 Phase 4, nexus-2jomg.5; Sam, 2026-11-27).

ci.yml's ``plugin-ledger-gate`` job calls ``plugin-drift-ledger.yml`` (a called workflow is a job ``pytest-gate`` can ``need``),
``pytest-gate`` blocks on its result when the event is a pull request into main and warns otherwise, and the job takes a
runner only when ``scripts/plugin_surface.py`` says the plugin surface changed or the pull request targets main. The
workflow file text is pinned here, the surface predicate runs on real tmp repositories, and the fan-in's shell runs for real.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import plugin_surface
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
BASH = shutil.which("bash")
CI_TEXT = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
CI = yaml.safe_load(CI_TEXT)
LEDGER_WF = yaml.safe_load((WORKFLOWS / "plugin-drift-ledger.yml").read_text(encoding="utf-8"))


REPORT_ON_DEVELOP = ("${{ ((github.event_name == 'push' && github.ref == 'refs/heads/develop') || "
                     "(github.event_name == 'pull_request' && github.base_ref == 'develop')) && 'report' || 'block' }}")


@pytest.mark.parametrize("job", ["test-lint", "test-docs-readers"])
def test_the_ledger_tests_run_in_report_mode_on_develop_and_block_mode_elsewhere(job: str) -> None:
    assert CI["jobs"][job]["env"]["NX_LEDGER_MODE"] == REPORT_ON_DEVELOP


def test_the_lint_leg_collects_xdist_report_lines_in_a_file_and_prints_it() -> None:
    lint = CI["jobs"]["test-lint"]
    assert lint["env"]["NX_LEDGER_REPORT_FILE"] == "${{ github.workspace }}/ledger-report.txt"
    step = next(s for s in lint["steps"] if s.get("name") == "Ledger findings (report mode)")
    assert step["if"] == "always()" and 'cat "${{ github.workspace }}/ledger-report.txt"' in step["run"]


def test_the_release_pr_runs_the_missing_fragment_detector_in_report_mode_first() -> None:
    steps = CI["jobs"]["release-ledger-gate"]["steps"]
    step = next(s for s in steps if "changelog fragment" in s.get("name", ""))
    assert step["env"] == {"NX_LEDGER_MODE": "report"} and "scripts/changelog_coverage.py" in step["run"]
    assert next(s for s in steps if s.get("uses", "").startswith("actions/checkout"))["with"]["fetch-depth"] == 0


def _triggers(wf: dict) -> dict:
    return wf.get("on", wf.get(True))


# ── the workflow files ──────────────────────────────────────────────────────

def test_the_ledger_workflow_is_called_by_ci_and_does_not_trigger_itself() -> None:
    on = _triggers(LEDGER_WF)
    assert "workflow_call" in on and "workflow_dispatch" in on
    assert "push" not in on and "pull_request" not in on, "a second trigger tests the same tree twice"
    assert "concurrency" not in LEDGER_WF, "a called workflow inherits its caller's group; its own collides with it"
    assert on["workflow_call"]["inputs"]["ledger_mode"]["default"] == "block"
    env = next(s["env"] for s in LEDGER_WF["jobs"]["ledger"]["steps"] if "NX_REQUIRE_PLUGIN_DRIFT_CHECK" in s.get("env", {}))
    assert env["NX_LEDGER_MODE"] == "${{ inputs.ledger_mode }}"


def test_ci_has_exactly_one_caller_of_the_ledger_workflow() -> None:
    callers = [(f.name, name) for f in sorted(WORKFLOWS.glob("*.yml"))
               for name, job in (yaml.safe_load(f.read_text(encoding="utf-8")).get("jobs") or {}).items()
               if str(job.get("uses", "")).endswith("plugin-drift-ledger.yml")]
    assert callers == [("ci.yml", "plugin-ledger-gate")], callers


def test_the_gate_job_runs_on_a_changed_surface_or_a_pull_request_into_main_and_reads_nothing_else() -> None:
    job = CI["jobs"]["plugin-ledger-gate"]
    assert job["uses"] == "./.github/workflows/plugin-drift-ledger.yml"
    assert job["needs"] == ["changes"]
    assert job["if"] == ("${{ needs.changes.outputs.plugin == 'true' || "
                         "(github.event_name == 'pull_request' && github.base_ref == 'main') }}")
    assert job["permissions"] == {"contents": "read"}
    assert job["with"] == {"ledger_mode": REPORT_ON_DEVELOP}
    assert "runs-on" not in job and "steps" not in job, "a caller job starts no runner of its own"


def test_the_changes_job_computes_the_surface_predicate_from_the_script_and_refuses_an_unusable_output() -> None:
    changes = CI["jobs"]["changes"]
    assert changes["outputs"]["plugin"] == "${{ steps.plugin.outputs.plugin }}"
    step = next(s for s in changes["steps"] if s.get("id") == "plugin")
    assert "python3 scripts/plugin_surface.py" in step["run"] and '--output "$GITHUB_OUTPUT"' in step["run"]
    verify = next(s for s in changes["steps"] if "plugin predicate is usable" in s.get("name", ""))
    assert "true|false) ;;" in verify["run"] and "exit 1" in verify["run"]


def test_pytest_gate_needs_the_job_and_reads_its_result() -> None:
    gate = CI["jobs"]["pytest-gate"]
    assert "plugin-ledger-gate" in gate["needs"]
    run = next(s["run"] for s in gate["steps"] if "Verify the pytest suite" in s.get("name", ""))
    assert 'plugin_ledger_result="${{ needs.plugin-ledger-gate.result }}"' in run
    assert 'plugin_changed="${{ needs.changes.outputs.plugin }}"' in run


# ── the fan-in's own shell, run for real ────────────────────────────────────

def _fan_in_segment() -> str:
    """The plugin-ledger-gate block of pytest-gate's script, as a shell would run it."""
    run = next(s["run"] for s in CI["jobs"]["pytest-gate"]["steps"] if "Verify the pytest suite" in s.get("name", ""))
    start = run.index("# plugin-ledger-gate (RDR-258 Phase 4")
    end = run.index("# nexus-rpaat: the affected-integration job MUST")
    return run[start:end]


def _run_fan_in(event: str, base: str, result: str, changed: str) -> subprocess.CompletedProcess[str]:
    assert BASH, "bash is on every host that runs this suite (a missing one is a failure, not a skip)"
    script = f'set -euo pipefail\nevent_name={event}\nbase_ref={base}\nplugin_ledger_result={result}\nplugin_changed="{changed}"\n' + _fan_in_segment()
    return subprocess.run([BASH, "-c", script], capture_output=True, text=True, check=False, env={**os.environ})


@pytest.mark.parametrize(("event", "base", "result", "changed", "code", "annotation"), [
    ("pull_request", "main", "success", "true", 0, ""),
    ("pull_request", "main", "success", "false", 0, ""),
    ("pull_request", "main", "failure", "true", 1, "::error::PR targets main but plugin-ledger-gate reported 'failure'"),
    ("pull_request", "main", "skipped", "false", 1, "::error::PR targets main but plugin-ledger-gate reported 'skipped'"),
    ("pull_request", "main", "cancelled", "true", 1, "::error::"),
    ("push", "", "failure", "true", 0, "::warning::plugin-ledger-gate reported 'failure' (advisory off the release PR"),
    ("pull_request", "develop", "failure", "true", 0, "::warning::plugin-ledger-gate reported 'failure'"),
    ("push", "", "skipped", "false", 0, ""),
    ("pull_request", "develop", "success", "false", 0, "::warning::plugin-ledger-gate reported 'success' where the plugin surface did not change"),
    ("push", "", "success", "true", 0, ""),
    ("push", "", "success", "", 1, "::error::the changes job gave no usable plugin-surface output"),
    ("pull_request", "main", "success", "maybe", 1, "::error::the changes job gave no usable plugin-surface output"),
])
def test_the_fan_in_blocks_only_on_a_pull_request_into_main(event: str, base: str, result: str, changed: str, code: int,
                                                             annotation: str) -> None:
    done = _run_fan_in(event, base, result, changed)
    assert done.returncode == code, done.stdout + done.stderr
    if annotation:
        assert annotation in done.stdout, done.stdout
    else:
        assert "::warning::" not in done.stdout and "::error::" not in done.stdout, done.stdout


# ── scripts/plugin_surface.py on real repositories ──────────────────────────

def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=True).stdout.strip()


def _commit(repo: Path, **files: str) -> str:
    for rel, text in files.items():
        path = repo / rel.replace("__", "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    _git(tmp_path, "init", "-q", "-b", "develop")
    return tmp_path, _commit(tmp_path, README__md="base\n")


@pytest.mark.parametrize(("path", "expected"), [
    ("conexus/hooks/x.py", True), ("conexus/skills/a/SKILL.md", True), ("sn/.mcp.json", True), ("conexus/.mcp.json", True),
    (".claude-plugin/marketplace.json", True), (".github/workflows/plugin-drift-ledger.yml", True),
    ("conexus/hooksx/a", False), ("conexus/README.md", False), ("sn/x.md", False), ("src/nexus/cli.py", False),
    ("conexus/.mcp.jsonx", False),
])
def test_the_surface_matches_a_directory_prefix_or_an_exact_path(path: str, expected: bool) -> None:
    assert plugin_surface.matches(path) is expected


def test_a_push_that_touches_the_surface_answers_true_and_one_that_does_not_answers_false(repo: tuple[Path, str]) -> None:
    root, base = repo
    _commit(root, src__a_py="x\n")
    assert plugin_surface.decide(root, "push", base, "")[0] is False
    _commit(root, conexus__hooks__h_py="x\n")
    assert plugin_surface.decide(root, "push", base, "")[0] is True


def test_a_pull_request_diffs_from_its_merge_base(repo: tuple[Path, str]) -> None:
    root, base = repo
    _git(root, "checkout", "-q", "-b", "feature")
    _commit(root, src__a_py="x\n")
    _git(root, "checkout", "-q", "develop")
    _commit(root, conexus__hooks__h_py="develop moved\n")      # not on the PR: three dots leave it out
    _git(root, "checkout", "-q", "feature")
    assert plugin_surface.decide(root, "pull_request", "", _git(root, "rev-parse", "develop"))[0] is False
    _commit(root, sn__hooks__s_sh="x\n")
    assert plugin_surface.decide(root, "pull_request", "", _git(root, "rev-parse", "develop"))[0] is True


@pytest.mark.parametrize("bad", ["", "0" * 40, "f" * 40])
def test_an_unusable_base_answers_true(repo: tuple[Path, str], bad: str) -> None:
    root, _base = repo
    _commit(root, src__a_py="x\n")
    assert plugin_surface.decide(root, "push", bad, "")[0] is True
    assert plugin_surface.decide(root, "pull_request", "", bad)[0] is True


def test_an_empty_diff_between_good_commits_is_a_broken_range_not_a_skip(repo: tuple[Path, str], tmp_path: Path) -> None:
    root, base = repo
    out = tmp_path / "out.txt"
    assert plugin_surface.main(["--repo", str(root), "--event", "push", "--before", base, "--output", str(out)]) == 1
    assert not out.exists(), "no output is written for a broken range, so the job's verify step fails it"


def test_the_script_writes_the_output_line_the_job_reads(repo: tuple[Path, str], tmp_path: Path) -> None:
    root, base = repo
    _commit(root, src__a_py="x\n")
    out = tmp_path / "out.txt"
    assert plugin_surface.main(["--repo", str(root), "--event", "push", "--before", base, "--output", str(out)]) == 0
    assert out.read_text() == "plugin=false\n"
    _commit(root, conexus__commands__c_md="x\n")
    assert plugin_surface.main(["--repo", str(root), "--event", "push", "--before", base, "--output", str(out)]) == 0
    assert out.read_text() == "plugin=false\nplugin=true\n"


def test_the_surface_list_is_distinct_plain_paths() -> None:
    assert len(set(plugin_surface.SURFACE_PATHS)) == len(plugin_surface.SURFACE_PATHS)
    assert all(re.fullmatch(r"[\w.\-/]+(/\*\*)?", p) for p in plugin_surface.SURFACE_PATHS)


# ── the develop listing of commits with no changelog fragment (RDR-258 Phase 4 step 2, Sam 2026-11-28) ────────────────

COVERAGE_STEP = next(s for s in CI["jobs"]["changes"]["steps"] if "changelog fragment" in s.get("name", ""))


def test_a_develop_push_and_a_pull_request_into_develop_list_missing_fragments_and_never_fail_the_job() -> None:
    """The step rides the `changes` job (a full checkout is already there) and is report-only: continue-on-error so a crash of the
    script cannot fail the job every other job needs, a timeout so it cannot hold them, report mode pinned in its env, and a
    condition that leaves the release PR into main to release-ledger-gate."""
    assert CI["jobs"]["changes"]["steps"][0]["with"]["fetch-depth"] == 0
    assert COVERAGE_STEP["env"]["NX_LEDGER_MODE"] == "report"
    assert COVERAGE_STEP["continue-on-error"] is True and COVERAGE_STEP["timeout-minutes"] <= 5
    assert COVERAGE_STEP["if"] == "github.event_name == 'push' || github.base_ref == 'develop'"
    assert "scripts/changelog_coverage.py" in COVERAGE_STEP["run"]


def _run_coverage_step(root: Path, *, event: str, before: str, pr_base: str) -> subprocess.CompletedProcess[str]:
    assert BASH, "bash is on every host that runs this suite (a missing one is a failure, not a skip)"
    env = {**os.environ, "EVENT_NAME": event, "BEFORE_SHA": before, "PR_BASE_SHA": pr_base, "NX_LEDGER_MODE": "report"}
    return subprocess.run([BASH, "-c", COVERAGE_STEP["run"]], cwd=root, capture_output=True, text=True, check=False, env=env)


@pytest.fixture
def coverage_repo(tmp_path: Path) -> tuple[Path, str]:
    """(repo, sha of the first commit). The repository carries the real detector; a commit on top changes shipped code with no
    fragment, naming ``nexus-t1``."""
    root = tmp_path / "cov"
    (root / "scripts").mkdir(parents=True)
    for name in ("changelog_coverage.py", "ledger_mode.py"):
        shutil.copy(ROOT / "scripts" / name, root / "scripts" / name)
    _git(root, "init", "-q")
    first = _commit(root, README_md="x\n")
    _commit(root, src__nexus__x_py="X = 1\n")
    _git(root, "commit", "-q", "--amend", "-m", "feat: add x (nexus-t1)")
    return root, first


def test_the_step_lists_the_pushed_range_and_exits_zero_on_a_finding(coverage_repo: tuple[Path, str]) -> None:
    root, first = coverage_repo
    done = _run_coverage_step(root, event="push", before=first, pr_base="")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "LEDGER-FINDING changelog-coverage:" in done.stdout and "nexus-t1" in done.stdout, done.stdout
    assert "LEDGER-REPORT changelog-coverage: 1 finding(s), 1 item(s) examined" in done.stdout, done.stdout


def test_the_step_lists_a_pull_requests_range_from_its_base(coverage_repo: tuple[Path, str]) -> None:
    root, first = coverage_repo
    done = _run_coverage_step(root, event="pull_request", before="", pr_base=first)
    assert done.returncode == 0 and "LEDGER-FINDING changelog-coverage:" in done.stdout, done.stdout + done.stderr


@pytest.mark.parametrize("bad", ["", "0" * 40, "f" * 40], ids=["empty", "all-zeros", "not-in-checkout"])
def test_the_step_falls_back_to_the_last_commit_when_the_base_is_unusable(coverage_repo: tuple[Path, str], bad: str) -> None:
    """A brand-new branch (all zeros) or a force-push that dropped `before` from the checkout: warn, then list the last commit."""
    root, _first = coverage_repo
    done = _run_coverage_step(root, event="push", before=bad, pr_base="")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "::warning::no usable range base" in done.stdout and "LEDGER-FINDING changelog-coverage:" in done.stdout, done.stdout


def test_the_step_warns_and_continues_on_a_range_that_examined_nothing(coverage_repo: tuple[Path, str]) -> None:
    """A merge-only range (a release back-merge): the script exits 10 with a LEDGER-VACUOUS line; the step warns and exits 0."""
    root, first = coverage_repo
    old = _git(root, "rev-parse", "HEAD")
    merge = _git(root, "commit-tree", f"{old}^{{tree}}", "-p", old, "-p", first, "-m", "Merge back")
    _git(root, "reset", "-q", "--hard", merge)
    done = _run_coverage_step(root, event="push", before=old, pr_base="")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "LEDGER-VACUOUS changelog-coverage:" in done.stdout and "::warning::changelog-coverage examined nothing" in done.stdout


def test_the_step_still_fails_when_the_script_crashes(coverage_repo: tuple[Path, str]) -> None:
    """Only the examined-nothing exit is tolerated: a missing script is a nonzero exit the step reports (continue-on-error keeps the job)."""
    root, first = coverage_repo
    (root / "scripts" / "changelog_coverage.py").unlink()
    done = _run_coverage_step(root, event="push", before=first, pr_base="")
    assert done.returncode != 0, done.stdout + done.stderr
