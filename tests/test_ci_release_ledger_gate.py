# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-55r6o: ci.yml must catch an unacknowledged wire-contract-ledger
entry on any PR targeting main, before any tag exists, not just at publish
time.

Root cause this closes: release.yml's `--paired-deploy-auto` invocation of
`check_client_lag_ledger` has no CI-side bypass by design (no human present
to type `--ack-client-lag`) -- an unacknowledged
`docs/wire-contract-pending.md` `## Unshipped` entry fails that step CLOSED
on the tagged commit's FROZEN tree. A `workflow_dispatch` retry of the SAME
tag cannot pick up a ledger fix (`actions/checkout` pins `ref: inputs.tag`,
an immutable tag), so the only real remedies are (a) cut a fresh tag, or (b)
this job: run the identical ledger-only check on the PR that promotes to
main, while the tree is still mutable. Fires on ANY PR targeting main
(matching ci.yml's existing release-boundary precedent at the `test`
matrix's python-version expression), not just `release/*`-named branches --
a hand-named branch releasing to main is not exempt from either the full
pytest matrix or this gate. See `bd show nexus-55r6o`.

Shape pins, same discipline as `test_release_workflow_ci_evidence.py`: the
workflow is not executable in CI-of-CI, so these assertions on the parsed
YAML + raw text hold the load-bearing properties.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml"
JOB_NAME = "release-ledger-gate"
FANIN_JOB_NAME = "pytest-gate"


def _text() -> str:
    return WORKFLOW.read_text()


def _doc() -> dict:
    return yaml.safe_load(_text())


def test_workflow_parses_as_yaml() -> None:
    _doc()


def test_job_exists() -> None:
    doc = _doc()
    assert JOB_NAME in doc["jobs"], (
        f"ci.yml must carry a '{JOB_NAME}' job -- the pre-tag ledger check "
        "moved into PR-gated CI targeting main (nexus-55r6o)"
    )


#: The exact composed condition, matching ci.yml:389's existing
#: release-boundary precedent (`github.event_name == 'pull_request' &&
#: github.base_ref == 'main'`) verbatim -- deliberately NOT narrowed to
#: release/*-named head branches (a hand-named branch releasing to main
#: would otherwise silently skip the one gate built to catch it pre-tag).
_EXPECTED_CONDITION = "github.event_name == 'pull_request' && github.base_ref == 'main'"


def test_job_fires_on_every_pr_to_main() -> None:
    """The job-level `if:` must be the exact composed condition -- not just
    both clauses present under any connective. A substring-presence check on
    each clause alone would pass if `&&` were swapped for `||` (verified:
    mutating the connective to `||` and re-running this test goes red only
    with an exact-string / connective-aware assertion, never with a
    substring-only one), which would fire this job on EVERY pull_request
    event (any target branch) rather than scoping it to main -- a much
    bigger cost regression than the release/*-narrowing this test replaces."""
    job = _doc()["jobs"][JOB_NAME]
    condition = job.get("if", "").strip()
    assert condition == _EXPECTED_CONDITION
    # Belt-and-suspenders on the connective specifically, independent of
    # the exact-string check above: exactly one `&&`, zero `||`.
    assert condition.count("&&") == 1
    assert "||" not in condition
    # And the release/*-narrowing this replaces must be gone, not just
    # additionally-present alongside the broader condition.
    assert "release/" not in condition
    assert "head_ref" not in condition


def test_job_invokes_ledger_only_flag() -> None:
    """Must run the tree-static ledger check, not the network-probing
    default path or the paired-deploy modes."""
    job = _doc()["jobs"][JOB_NAME]
    run_lines = "\n".join(
        step.get("run", "") for step in job["steps"] if "run" in step
    )
    assert "check_engine_release_floor.py --ledger-only" in run_lines


def test_job_never_invokes_the_network_probing_paired_deploy_flags() -> None:
    """The whole design point (per AGENTS.md / the bead): cloud state at PR
    time is not publish-time state -- this job must never reach for
    --paired-deploy or --paired-deploy-auto, which probe the live managed
    service."""
    job = _doc()["jobs"][JOB_NAME]
    run_lines = "\n".join(
        step.get("run", "") for step in job["steps"] if "run" in step
    )
    assert "--paired-deploy" not in run_lines


def test_job_has_no_needs_dependency() -> None:
    """Standalone, cheap job -- no fan-in wiring, and critically no
    dependency on the doc-only `changes` predicate (a release PR always
    touches version-surface files, so gating on `changes` would be a no-op
    at best and a footgun at worst if that predicate's shape ever changes)."""
    job = _doc()["jobs"][JOB_NAME]
    assert "needs" not in job


def test_job_carries_a_timeout() -> None:
    job = _doc()["jobs"][JOB_NAME]
    assert job.get("timeout-minutes"), (
        "every job needs an explicit timeout -- an unbounded job can hang "
        "a release PR indefinitely"
    )


def test_job_makes_no_network_calls() -> None:
    """No curl/wget/gh-api-style egress in this job's steps -- the ledger
    check is git/filesystem-only by design (see check_client_lag_ledger)."""
    job = _doc()["jobs"][JOB_NAME]
    run_lines = "\n".join(
        step.get("run", "") for step in job["steps"] if "run" in step
    ).lower()
    for forbidden in ("curl ", "wget ", "gh release", "gh api"):
        assert forbidden not in run_lines, (
            f"unexpected network call ({forbidden!r}) in the pre-tag ledger "
            "gate -- this check must stay tree-static"
        )


# ── nexus-55r6o critic round: merge-blocking wiring, not advisory ─────────
#
# code-review found the job standalone (no branch-protection entry, no
# pytest-gate `needs:`) -- a red release-ledger-gate could not actually
# block a merge. THE FIX: wire it into pytest-gate's `needs:`, since
# pytest-gate IS main's required branch-protection check
# (scripts/check_release_ci_evidence.py's REQUIRED_CHECK_CONTEXTS ==
# ("pytest-gate",)). These tests pin BOTH halves of that wiring: the needs
# edge exists, AND the fan-in's shell logic actually enforces (not just
# references) the release-ledger-gate result -- requiring success on a
# main-targeting PR, tolerating skipped everywhere else (the same
# "skipped == success for branch protection" idiom the code==false branch
# above already uses for the other three legs).


def test_release_ledger_gate_is_in_pytest_gate_needs() -> None:
    """The load-bearing edge: without this, a red release-ledger-gate is
    invisible to the only check branch protection actually requires."""
    fanin = _doc()["jobs"][FANIN_JOB_NAME]
    assert JOB_NAME in fanin["needs"], (
        f"'{JOB_NAME}' must be a needs: dependency of '{FANIN_JOB_NAME}' -- "
        "without this edge, release-ledger-gate is advisory-only: a release "
        "PR can merge with it red (nexus-55r6o substantive-critic Critical "
        "finding)"
    )


_GH_EXPR_RE = re.compile(r"\$\{\{\s*(.+?)\s*\}\}")

#: A clean, code-changed, non-main baseline -- every leg succeeds, and
#: release-ledger-gate legitimately skipped (its own job-level `if:` is
#: false off a main-targeting PR). Individual tests override just the keys
#: they're exercising.
_BASE_VALUES = {
    "needs.changes.result": "success",
    "needs.changes.outputs.code": "true",
    "needs.test.result": "success",
    "needs.test-lint.result": "success",
    "needs.test-mode-census.result": "success",
    "needs.release-ledger-gate.result": "skipped",
    "github.event_name": "push",
    "github.base_ref": "",
}


def _find_step(job: dict, name_prefix: str) -> dict:
    for step in job["steps"]:
        if step.get("name", "").startswith(name_prefix):
            return step
    raise AssertionError(
        f"no step named like {name_prefix!r} found in job {FANIN_JOB_NAME!r} "
        "-- the workflow was restructured; update this test's pointer"
    )


def _render(run_text: str, values: dict[str, str]) -> str:
    """Substitute every ``${{ <expr> }}`` the same way GitHub Actions
    inlines them before invoking the shell -- same discipline as
    test_dorny_guard_shell_logic.py's identically-named helper: this
    exercises the ACTUAL run: text, not a hand-copied duplicate that could
    drift from it."""
    def repl(m: re.Match[str]) -> str:
        expr = m.group(1).strip()
        if expr not in values:
            raise KeyError(
                f"script references {expr!r} but the test fixture supplied "
                f"no value for it (have: {sorted(values)}) -- the fan-in's "
                "run: block changed shape; update _BASE_VALUES"
            )
        return values[expr]

    return _GH_EXPR_RE.sub(repl, run_text)


def _pytest_gate_script(**overrides: str) -> str:
    job = _doc()["jobs"][FANIN_JOB_NAME]
    step = _find_step(job, "Verify the sharded pytest matrix")
    values = {**_BASE_VALUES, **overrides}
    return _render(step["run"], values)


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True,
    )


def test_fanin_requires_release_ledger_success_on_main_pr() -> None:
    """A PR targeting main with release-ledger-gate reporting anything but
    success must fail the fan-in -- this is the actual merge-blocking
    mechanism, exercised as real bash, not just present in the YAML text."""
    for bad_result in ("skipped", "failure", "cancelled"):
        proc = _run(_pytest_gate_script(**{
            "github.event_name": "pull_request",
            "github.base_ref": "main",
            "needs.release-ledger-gate.result": bad_result,
        }))
        assert proc.returncode == 1, (bad_result, proc.stdout, proc.stderr)
        assert "release-ledger-gate" in proc.stdout + proc.stderr


def test_fanin_passes_on_main_pr_when_release_ledger_succeeds() -> None:
    proc = _run(_pytest_gate_script(**{
        "github.event_name": "pull_request",
        "github.base_ref": "main",
        "needs.release-ledger-gate.result": "success",
    }))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "pytest-gate: PASS" in proc.stdout


def test_fanin_tolerates_release_ledger_skip_off_main_pr() -> None:
    """The idiom under test: `skipped` is CORRECT (not a failure) whenever
    release-ledger-gate's own `if:` legitimately did not fire -- develop
    push, or a PR targeting develop."""
    for event_name, base_ref in (("push", ""), ("pull_request", "develop")):
        proc = _run(_pytest_gate_script(**{
            "github.event_name": event_name,
            "github.base_ref": base_ref,
            "needs.release-ledger-gate.result": "skipped",
        }))
        assert proc.returncode == 0, (event_name, base_ref, proc.stdout, proc.stderr)


def test_fanin_still_fails_on_genuine_release_ledger_failure_off_main_pr() -> None:
    """Tolerating `skipped` off a main PR must not widen into tolerating a
    genuine failure there too -- a red release-ledger-gate on, say, a
    develop-bound PR (if it ever ran there) must still fail the fan-in."""
    proc = _run(_pytest_gate_script(**{
        "github.event_name": "pull_request",
        "github.base_ref": "develop",
        "needs.release-ledger-gate.result": "failure",
    }))
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "release-ledger-gate" in proc.stdout + proc.stderr
