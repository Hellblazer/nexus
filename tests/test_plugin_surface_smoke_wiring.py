# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""`tests/e2e/release-sandbox.sh smoke` must actually run in CI (nexus-98gpl).

docs/rdr/rdr-197-plugin-only-release-channel.md:171-173 names the gap this
closes: the smoke gate is mandatory for plugin changes per AGENTS.md step 6,
but ran in no CI workflow before this bead -- only ever by hand, right before
a release. The workflow it wires (`.github/workflows/plugin-surface-smoke.yml`)
is intentionally NON-GATING (advisory, no branch-protection requirement), so
this test's job is the same shape as `TestTheCIWiringItself` in
tests/test_plugin_release_drift_ledger.py: prove the workflow file, its path
filter, and its non-vacuity assertion still exist and still cover the surface
-- not re-derive whether the underlying script itself works (that is
release-sandbox.sh's own concern, exercised by hand before every release).

Every test here fails if the corresponding wiring is removed or the surface
list drifts out of sync with AGENTS.md's own "Run sandbox smoke" step 6 list.
"""

from __future__ import annotations

import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "plugin-surface-smoke.yml"

#: AGENTS.md step 6 ("Run sandbox smoke") names these inputs as requiring
#: `release-sandbox.sh smoke` before merge. Kept independent of the workflow
#: file's own `paths:` list so a drift in either direction is caught: the
#: workflow's copy is the wire, this is the mandate it must satisfy.
MANDATED_SURFACE = [
    "pyproject.toml",
    "uv.lock",
    "src/nexus/mcp/",
    "conexus/",
    ".claude-plugin/",
    "src/nexus/commands/doctor.py",
    "src/nexus/commands/upgrade.py",
]


def _workflow() -> dict:
    yaml = pytest.importorskip("yaml")
    assert WORKFLOW_PATH.exists(), (
        "the plugin-surface-smoke workflow is gone. Without it "
        "`tests/e2e/release-sandbox.sh smoke` runs only on a developer's "
        "machine, which is exactly the gap nexus-98gpl/RDR-197 named."
    )
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _job_steps() -> list[dict]:
    wf = _workflow()
    jobs = wf["jobs"]
    assert "smoke" in jobs, "the smoke job was renamed or removed"
    return jobs["smoke"]["steps"]


class TestTheWorkflowRunsTheGate:
    """The step that actually invokes the gate must exist and target the
    real script, not a stand-in."""

    def test_the_workflow_invokes_release_sandbox_smoke(self) -> None:
        steps = _job_steps()
        runs = " ".join(s.get("run", "") for s in steps)
        assert "tests/e2e/release-sandbox.sh" in runs and "smoke" in runs, (
            "the workflow no longer runs tests/e2e/release-sandbox.sh smoke -- "
            "this is the entire point of the gate"
        )

    def test_the_workflow_asserts_the_scripts_own_verdict_line(self) -> None:
        """Non-vacuity (bead step 5): a run that checked nothing, or a run
        whose pipeline exits 0 without the script's real PASSED path
        firing, must fail this job -- not just a bare exit-code check."""
        steps = _job_steps()
        runs = " ".join(s.get("run", "") for s in steps)
        assert "SMOKE PASSED: all steps green." in runs, (
            "the workflow no longer asserts on release-sandbox.sh's own "
            "verdict line -- a smoke run that checked nothing (or exited 0 "
            "on a broken pipeline) would now report green"
        )

    def test_removing_the_gate_step_fails_this_test(self) -> None:
        """Falsification control (developer-agent execution reminder): strip
        the gate step out of a copy of the workflow and confirm detection
        actually fires, rather than trusting the positive assertions alone
        to be well-targeted."""
        wf = _workflow()
        steps = wf["jobs"]["smoke"]["steps"]
        stripped = [
            s for s in steps if "release-sandbox.sh" not in s.get("run", "")
        ]
        assert len(stripped) < len(steps), (
            "test setup bug: no step in the real workflow references "
            "release-sandbox.sh, so this control proves nothing"
        )
        runs = " ".join(s.get("run", "") for s in stripped)
        assert "tests/e2e/release-sandbox.sh" not in runs, (
            "stripping the gate step did not remove the reference -- the "
            "positive assertion above would not have caught its removal"
        )


class TestConcurrencyAndTriggerShape:
    """CI cost discipline (CLAUDE.md / AGENTS.md): cancel superseded runs,
    never fire on an unrelated push."""

    def test_the_workflow_has_a_concurrency_group_that_cancels(self) -> None:
        wf = _workflow()
        concurrency = wf.get("concurrency")
        assert concurrency, "no concurrency group -- superseded pushes stack runs"
        assert concurrency.get("cancel-in-progress") is True, (
            "concurrency group exists but does not cancel in-progress runs"
        )

    def test_the_workflow_has_no_push_trigger(self) -> None:
        """Non-gating + expensive (self-provisions a real engine/PG/embedder):
        PR-only keeps this off the merge-push path entirely -- a tree that
        already ran this on its PR must not pay for it again on merge."""
        wf = _workflow()
        on = wf[True] if True in wf else wf["on"]
        assert "push" not in on, (
            "a push trigger appeared on this job -- it re-tests a tree "
            "that already ran on its PR, violating 'never test the same "
            "tree twice'"
        )

    def test_the_workflow_targets_pull_request(self) -> None:
        wf = _workflow()
        on = wf[True] if True in wf else wf["on"]
        assert "pull_request" in on, "the workflow no longer triggers on pull_request"


class TestPathFilterCoversTheMandatedSurface:
    """A surface prefix outside the trigger paths is a surface whose plugin
    changes never trigger the gate -- exactly the undeclared-drift shape
    the RDR-197 gap called out."""

    def test_the_path_filter_covers_every_mandated_prefix(self) -> None:
        wf = _workflow()
        on = wf[True] if True in wf else wf["on"]
        patterns = " ".join(on["pull_request"]["paths"])
        missing = [p for p in MANDATED_SURFACE if p.rstrip("/") not in patterns]
        assert not missing, (
            f"AGENTS.md step-6 surface prefixes not covered by the "
            f"workflow's path filter: {missing}. A plugin change there "
            f"would merge without this gate ever running."
        )

    def test_the_path_filter_includes_itself(self) -> None:
        """The self-referential entry plugin-drift-ledger.yml also carries:
        a change that disables the filter must itself trip the filter."""
        wf = _workflow()
        on = wf[True] if True in wf else wf["on"]
        patterns = " ".join(on["pull_request"]["paths"])
        assert ".github/workflows/plugin-surface-smoke.yml" in patterns, (
            "the workflow no longer watches its own file -- a change that "
            "narrows or removes the path filter would not re-run the gate "
            "to prove the narrowing was safe"
        )


class TestNotWiredAsARequiredCheck:
    """Bead title: 'RDR-197 F1 (non-gating)'. This job must not become a
    silent PR-blocker by way of a path-filtered required check hanging PRs
    that never touch the surface (feedback_required_check_needs_job_level_
    skip.md) -- documented here so a future edit toward 'required' is a
    deliberate decision, not an accident."""

    def test_the_job_has_no_job_level_skip_shim(self) -> None:
        """A job-level always-run-then-skip pattern is the shape a REQUIRED
        path-filtered check needs (skipped == success for branch
        protection). Its presence here would be a signal this job was
        quietly turned into a required check without updating this test or
        the workflow's own non-gating header comment."""
        steps = _job_steps()
        names = " ".join(s.get("name", "") for s in steps).lower()
        assert "required" not in names, (
            "a step name mentions 'required' -- if this job became a "
            "required status check, the workflow-level `paths:` filter "
            "above must be replaced with a job-level skip (a path-filtered "
            "required check otherwise hangs PRs that never touch the "
            "surface); update this test alongside that change"
        )
