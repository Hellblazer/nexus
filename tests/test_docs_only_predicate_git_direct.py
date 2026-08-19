# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-h18n5: ci.yml's `changes` job (doc-only fast-lane predicate) must be
immune to dorny/paths-filter's PR-files-API empty-list failure mode.

Incident (PR #1464, 2026-08-18): dorny's PR-files API returned an EMPTY
changed-file list seconds after PR creation ("Matching files: none"), and
predicate-quantifier 'every' silently folded that emptiness to
docs_only=false -> code=true, running the full service-jar + 8-shard pytest
matrix on a 3-file docs/rdr diff. The fix (option b) removes dorny from this
one job entirely and computes docs_only directly from `git diff` against the
checkout's own history (fetch-depth:0).

Two test families:

* Structural pins on the parsed YAML -- the `changes` job no longer uses
  dorny at all, its outputs contract (`code`) is untouched, and every
  downstream consumer (service-jar, test, test-lint, test-mode-census,
  pytest-gate) still reads `needs.changes.outputs.code` the same way. The
  OTHER dorny usages (CA-3 x2, engine-service-build, write-seam-gate) must
  still use dorny -- that family is deliberately unchanged.
* Behavioural tests -- extract the `gate` step's literal `run:` block (same
  discipline as test_dorny_guard_shell_logic.py: the ACTUAL text a runner
  would execute, not a hand-copied duplicate that could drift), substitute
  its `${{ ... }}` GitHub Actions expressions with test values, and execute
  it for real via `bash -c` inside a throwaway git repo. Covers: docs-only ->
  true, mixed -> false, an EMPTY changed-file set against two valid commits
  -> exit 1 named error (the PR #1464 bug class, never silently resolved),
  the CHANGELOG*-is-root-only / conexus/CHANGELOG.md-is-exact edge cases,
  and the unusable-base/before-SHA fallback (empty, all-zero, unreachable)
  on both PR and push events.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

JOB_NAME = "changes"
STEP_NAME_PREFIX = "Compute the doc-only predicate directly from git"

ZERO_SHA = "0000000000000000000000000000000000000000"
GARBAGE_SHA = "abc123abc123abc123abc123abc123abc123ab"  # well-formed hex, absent from any repo


def _doc() -> dict:
    return yaml.safe_load(CI_YML.read_text())


def _changes_job() -> dict:
    return _doc()["jobs"][JOB_NAME]


# ── structural pins ────────────────────────────────────────────────────────


def test_workflow_parses_as_yaml() -> None:
    _doc()


def test_changes_job_has_no_dorny_step() -> None:
    """The whole point of option (b): dorny/paths-filter is gone from this
    job, not merely unused."""
    job = _changes_job()
    for step in job["steps"]:
        assert "dorny" not in step.get("uses", ""), (
            "the 'changes' job must not use dorny/paths-filter any more "
            "(nexus-h18n5 option b) -- found a dorny step"
        )
    step_ids = [step.get("id") for step in job["steps"]]
    assert "changes" not in step_ids, (
        "the old dorny step's id ('changes') must not still exist -- it "
        "would be a sign the step was renamed rather than removed"
    )


def test_changes_job_outputs_contract_unchanged() -> None:
    """Downstream consumers only ever read `needs.changes.outputs.code` --
    that job-level output wiring must be byte-identical."""
    job = _changes_job()
    assert job["outputs"] == {"code": "${{ steps.gate.outputs.code }}"}


def test_gate_step_id_and_name_present() -> None:
    job = _changes_job()
    step = _find_step(job, STEP_NAME_PREFIX)
    assert step["id"] == "gate"


def test_checkout_still_fetch_depth_zero() -> None:
    """Merge-base / same-branch resolution needs full history -- the
    shallow-race memory (project_ci_dorny_shallow_fetch_race) still applies
    even with dorny gone."""
    job = _changes_job()
    checkout = next(s for s in job["steps"] if "actions/checkout" in s.get("uses", ""))
    assert checkout["with"]["fetch-depth"] == 0


@pytest.mark.parametrize(
    "job_name",
    ["ca3-pgvector-bundle", "ca3-pgvector-bundle-macos", "engine-service-build", "write-seam-gate"],
)
def test_conservative_family_still_uses_dorny(job_name: str) -> None:
    """The 'always true on a develop push' safety-net family is a DIFFERENT
    risk profile (extra CI cost on emptiness, never a wrongful skip) and was
    never the thing that broke -- it must stay on dorny, not get "unified"
    onto the changes job's git-direct pattern."""
    job = _doc()["jobs"][job_name]
    assert any("dorny/paths-filter" in s.get("uses", "") for s in job["steps"]), (
        f"job {job_name!r} must still use dorny/paths-filter -- it is "
        "deliberately outside nexus-h18n5's scope"
    )


@pytest.mark.parametrize(
    ("consumer_job", "expected_condition"),
    [
        ("service-jar", "success() && needs.changes.outputs.code == 'true'"),
        ("test", "success() && needs.changes.outputs.code == 'true'"),
        ("test-lint", "success() && needs.changes.outputs.code == 'true'"),
        ("test-mode-census", "success() && needs.changes.outputs.code == 'true'"),
    ],
)
def test_downstream_consumers_condition_unchanged(consumer_job: str, expected_condition: str) -> None:
    job = _doc()["jobs"][consumer_job]
    needs = job["needs"]
    needs_list = needs if isinstance(needs, list) else [needs]
    assert "changes" in needs_list
    assert job["if"].strip() == expected_condition


def test_pytest_gate_still_requires_changes_success() -> None:
    job = _doc()["jobs"]["pytest-gate"]
    assert "changes" in job["needs"]
    run_text = "\n".join(step.get("run", "") for step in job["steps"] if "run" in step)
    assert 'needs.changes.result' in run_text
    assert 'needs.changes.outputs.code' in run_text


# ── behavioural: extract + execute the real gate script ───────────────────


def _find_step(job: dict, name_prefix: str) -> dict:
    for step in job["steps"]:
        if step.get("name", "").startswith(name_prefix):
            return step
    raise AssertionError(
        f"no step named like {name_prefix!r} found in job {JOB_NAME!r} -- "
        "the workflow was restructured; update this test's pointer"
    )


_GH_EXPR_RE = re.compile(r"\$\{\{\s*(.+?)\s*\}\}")


def _gate_script() -> str:
    job = _changes_job()
    step = _find_step(job, STEP_NAME_PREFIX)
    return step["run"]


def _render(run_text: str, values: dict[str, str]) -> str:
    """Same discipline as test_dorny_guard_shell_logic.py's identically
    named helper: substitute every `${{ <expr> }}` textually, exactly as
    GitHub Actions does before invoking the shell -- exercises the ACTUAL
    script, not a hand-copied duplicate."""

    def repl(m: re.Match[str]) -> str:
        expr = m.group(1).strip()
        if expr not in values:
            raise KeyError(
                f"script references {expr!r} but the test fixture supplied "
                f"no value for it (have: {sorted(values)}) -- the gate "
                "step's run: block changed shape; update this test"
            )
        return values[expr]

    return _GH_EXPR_RE.sub(repl, run_text)


def _run_in(repo: Path, script: str) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "gh_output"
        env = dict(os.environ)
        env["GITHUB_OUTPUT"] = str(out)
        proc = subprocess.run(
            ["bash", "-c", script], cwd=repo, capture_output=True, text=True, env=env,
        )
        outputs: dict[str, str] = {}
        if out.exists():
            for line in out.read_text().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    outputs[k] = v
        return proc, outputs


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
    )
    assert proc.returncode == 0, (args, proc.stdout, proc.stderr)
    return proc.stdout.strip()


def _checkout(repo: Path, ref: str) -> None:
    _git(repo, "checkout", "-q", ref)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    return repo


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for relpath, content in files.items():
        p = repo / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _values(event_name: str, base_sha: str = "unused", before_sha: str = "unused") -> dict[str, str]:
    return {
        "github.event_name": event_name,
        "github.event.pull_request.base.sha": base_sha,
        "github.event.before": before_sha,
    }


# -- PR events --------------------------------------------------------------


def test_pr_docs_only_diff_resolves_true(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    base = _commit(repo, {"seed.txt": "x"}, "seed")
    _commit(
        repo,
        {
            "docs/rdr/117-thing.md": "hi",
            "README.md": "readme",
            "CHANGELOG.md": "v2",
            "conexus/CHANGELOG.md": "v2",
            "LICENSE": "mit",
        },
        "docs update",
    )
    script = _render(_gate_script(), _values("pull_request", base_sha=base))
    proc, outputs = _run_in(repo, script)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert outputs.get("code") == "false"


def test_pr_mixed_diff_resolves_false(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    base = _commit(repo, {"seed.txt": "x"}, "seed")
    _commit(
        repo,
        {"docs/rdr/117-thing.md": "hi", "src/nexus/foo.py": "code"},
        "mixed update",
    )
    script = _render(_gate_script(), _values("pull_request", base_sha=base))
    proc, outputs = _run_in(repo, script)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert outputs.get("code") == "true"


def test_pr_empty_diff_against_valid_commits_is_malformed_and_fails_loud(tmp_path: Path) -> None:
    """THE ACTUAL PR #1464 BUG CLASS: an empty changed-file set must never
    silently resolve to docs_only true or false -- it must fail the job."""
    repo = _init_repo(tmp_path)
    head = _commit(repo, {"seed.txt": "x"}, "seed")
    script = _render(_gate_script(), _values("pull_request", base_sha=head))
    proc, outputs = _run_in(repo, script)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "::error::" in proc.stdout or "::error::" in proc.stderr
    assert "code" not in outputs


@pytest.mark.parametrize("bad_base", ["", ZERO_SHA])
def test_pr_unusable_base_sha_falls_back_to_true_not_silently(tmp_path: Path, bad_base: str) -> None:
    repo = _init_repo(tmp_path)
    _commit(repo, {"seed.txt": "x"}, "seed")
    script = _render(_gate_script(), _values("pull_request", base_sha=bad_base))
    proc, outputs = _run_in(repo, script)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert outputs.get("code") == "true"
    assert "::warning::" in proc.stdout or "::warning::" in proc.stderr


def test_pr_unreachable_base_sha_falls_back_to_true_not_silently(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit(repo, {"seed.txt": "x"}, "seed")
    script = _render(_gate_script(), _values("pull_request", base_sha=GARBAGE_SHA))
    proc, outputs = _run_in(repo, script)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert outputs.get("code") == "true"
    assert "::warning::" in proc.stdout or "::warning::" in proc.stderr


def test_pr_three_dot_uses_merge_base_not_raw_base_tip(tmp_path: Path) -> None:
    """substantive-critic Significant (nexus-h18n5): every OTHER PR fixture
    in this file has base_sha as a direct linear ancestor of HEAD, where
    `git diff A...B` (merge-base/three-dot) and `git diff A B` (raw/two-dot)
    produce IDENTICAL output -- a silent regression swapping `...` for `..`
    in the script would pass all of them. This fixture diverges the DAG so
    the two forms disagree: the base branch advances past the PR's fork
    point with an UNRELATED code commit, while the PR's head branches off
    the ORIGINAL fork point with only a docs change. Three-dot correctly
    sees just the PR's own docs-only diff (merge-base recovers the fork
    point); two-dot would additionally see base's own code commit (as a
    removal, since it's absent from HEAD) and wrongly resolve mixed."""
    repo = _init_repo(tmp_path)
    fork = _commit(repo, {"seed.txt": "x"}, "seed")
    # Base advances past the fork with a commit the PR never asked for.
    base_advanced = _commit(repo, {"src/nexus/other.py": "code"}, "base advances independently")
    # Head branches off the FORK POINT, not the advanced base.
    _checkout(repo, fork)
    head = _commit(repo, {"docs/rdr/999-thing.md": "hi"}, "docs-only PR change")
    assert head != base_advanced

    script = _render(_gate_script(), _values("pull_request", base_sha=base_advanced))
    proc, outputs = _run_in(repo, script)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert outputs.get("code") == "false", (
        "three-dot (merge-base) diff must see ONLY the PR's own docs-only "
        "change, not base_advanced's unrelated code commit -- code=true "
        "here would mean the script regressed to raw two-dot semantics"
    )


# -- push events -------------------------------------------------------------


def test_push_docs_only_diff_resolves_true(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    before = _commit(repo, {"seed.txt": "x"}, "seed")
    _commit(repo, {"docs/rdr/117-thing.md": "hi", "LICENSE": "mit"}, "docs update")
    script = _render(_gate_script(), _values("push", before_sha=before))
    proc, outputs = _run_in(repo, script)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert outputs.get("code") == "false"


def test_push_mixed_diff_resolves_false(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    before = _commit(repo, {"seed.txt": "x"}, "seed")
    _commit(repo, {"docs/rdr/117-thing.md": "hi", "src/nexus/foo.py": "code"}, "mixed update")
    script = _render(_gate_script(), _values("push", before_sha=before))
    proc, outputs = _run_in(repo, script)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert outputs.get("code") == "true"


def test_push_empty_diff_against_valid_commits_is_malformed_and_fails_loud(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    head = _commit(repo, {"seed.txt": "x"}, "seed")
    script = _render(_gate_script(), _values("push", before_sha=head))
    proc, outputs = _run_in(repo, script)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "::error::" in proc.stdout or "::error::" in proc.stderr
    assert "code" not in outputs


@pytest.mark.parametrize("bad_before", ["", ZERO_SHA])
def test_push_unusable_before_sha_falls_back_to_true_not_silently(tmp_path: Path, bad_before: str) -> None:
    """Covers the first-push-of-a-new-branch edge case named in the bead."""
    repo = _init_repo(tmp_path)
    _commit(repo, {"seed.txt": "x"}, "seed")
    script = _render(_gate_script(), _values("push", before_sha=bad_before))
    proc, outputs = _run_in(repo, script)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert outputs.get("code") == "true"
    assert "::warning::" in proc.stdout or "::warning::" in proc.stderr


def test_push_unreachable_before_sha_falls_back_to_true_not_silently(tmp_path: Path) -> None:
    """Covers the force-push-rewrote-history edge case named in the bead."""
    repo = _init_repo(tmp_path)
    _commit(repo, {"seed.txt": "x"}, "seed")
    script = _render(_gate_script(), _values("push", before_sha=GARBAGE_SHA))
    proc, outputs = _run_in(repo, script)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert outputs.get("code") == "true"
    assert "::warning::" in proc.stdout or "::warning::" in proc.stderr


# -- path-set edge cases ------------------------------------------------------


def test_changelog_star_is_root_level_only(tmp_path: Path) -> None:
    """CHANGELOG* must NOT match a nested CHANGELOG.md outside docs/** or the
    conexus/CHANGELOG.md exact arm -- e.g. vendor/CHANGELOG.md is CODE as far
    as this predicate is concerned. NOTE: this particular fixture never even
    reaches the `CHANGELOG*` arm (the path doesn't start with the literal
    "CHANGELOG" prefix), so it alone cannot prove the arm is anchored --
    see test_changelog_and_license_glob_arms_are_slash_anchored below for
    the adversarial case that actually exercises the vulnerable pattern."""
    repo = _init_repo(tmp_path)
    before = _commit(repo, {"seed.txt": "x"}, "seed")
    _commit(repo, {"vendor/CHANGELOG.md": "nested"}, "nested changelog")
    script = _render(_gate_script(), _values("push", before_sha=before))
    proc, outputs = _run_in(repo, script)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert outputs.get("code") == "true"


@pytest.mark.parametrize(
    ("relpath", "label"),
    [
        ("CHANGELOG.d/malicious_code.py", "CHANGELOG*"),
        ("LICENSE-vendor/malicious_code.py", "LICENSE*"),
        ("x/docs/malicious_code.py", "docs/* (nested, not repo-root)"),
    ],
)
def test_changelog_and_license_glob_arms_are_slash_anchored(
    tmp_path: Path, relpath: str, label: str
) -> None:
    """code-review-expert Critical (nexus-h18n5): `case` pattern matching is
    plain string matching, not filesystem pathname expansion -- an
    unanchored `CHANGELOG*` or `LICENSE*` arm matches ANY path that merely
    STARTS WITH that literal, slash included. Without the `*/*` guard, a
    pushed commit adding `CHANGELOG.d/malicious_code.py` would read as
    docs-only and silently skip the pytest matrix on an actual code change
    -- the catastrophic direction (false negative), not the PR #1464
    incident's mere cost waste. Each of these three adversarial shapes
    (CHANGELOG*-lookalike directory, LICENSE*-lookalike directory, and a
    nested non-root `docs` directory) must resolve to code=true."""
    repo = _init_repo(tmp_path)
    before = _commit(repo, {"seed.txt": "x"}, "seed")
    _commit(repo, {relpath: "payload"}, f"adversarial {label} probe")
    script = _render(_gate_script(), _values("push", before_sha=before))
    proc, outputs = _run_in(repo, script)
    assert proc.returncode == 0, (label, relpath, proc.stdout, proc.stderr)
    assert outputs.get("code") == "true", (
        f"{relpath!r} (probing the {label} arm) must resolve to code=true "
        "-- an unanchored glob arm would wrongly read this as docs-only"
    )


def test_conexus_changelog_exact_match_is_docs_only(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    before = _commit(repo, {"seed.txt": "x"}, "seed")
    _commit(repo, {"conexus/CHANGELOG.md": "v3"}, "conexus changelog")
    script = _render(_gate_script(), _values("push", before_sha=before))
    proc, outputs = _run_in(repo, script)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert outputs.get("code") == "false"


def test_root_changelog_star_matches(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    before = _commit(repo, {"seed.txt": "x"}, "seed")
    _commit(repo, {"CHANGELOG.md": "v4"}, "root changelog")
    script = _render(_gate_script(), _values("push", before_sha=before))
    proc, outputs = _run_in(repo, script)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert outputs.get("code") == "false"


def test_agents_md_is_code_not_docs(tmp_path: Path) -> None:
    """AGENTS.md is a TEST INPUT (per the job's own comment), so it must not
    qualify as doc-only prose."""
    repo = _init_repo(tmp_path)
    before = _commit(repo, {"seed.txt": "x"}, "seed")
    _commit(repo, {"AGENTS.md": "guidance"}, "agents update")
    script = _render(_gate_script(), _values("push", before_sha=before))
    proc, outputs = _run_in(repo, script)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert outputs.get("code") == "true"


def test_conexus_dir_other_than_changelog_is_code(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    before = _commit(repo, {"seed.txt": "x"}, "seed")
    _commit(repo, {"conexus/hooks/scripts/foo.py": "code"}, "conexus non-changelog")
    script = _render(_gate_script(), _values("push", before_sha=before))
    proc, outputs = _run_in(repo, script)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert outputs.get("code") == "true"
