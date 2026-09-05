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
#: file's own `paths:` list, spelled as directory prefixes (trailing "/")
#: or exact filenames -- `_expected_glob` below converts a prefix to the
#: `**`-suffixed glob form the workflow actually uses, so the two direction
#: checks below (missing / undocumented-extra) compare like with like
#: instead of doing substring containment.
MANDATED_SURFACE = [
    "pyproject.toml",
    "uv.lock",
    "src/nexus/mcp/",
    "conexus/",
    ".claude-plugin/",
    "src/nexus/commands/doctor.py",
    "src/nexus/commands/upgrade.py",
]

#: Entries the workflow's `paths:` list carries beyond MANDATED_SURFACE,
#: each with its own reason -- self-reference so a change that narrows or
#: removes the filter still re-runs the gate to prove the narrowing was
#: safe. A path here that is NOT one of these three is undocumented drift
#: (nexus-98gpl review Important #1/#2: `sn/**` was exactly this, added
#: with no causal link to anything release-sandbox.sh smoke touches, and
#: nothing tested the workflow for extras beyond the mandated list).
KNOWN_EXTRAS = {
    "tests/e2e/release-sandbox.sh",
    "tests/test_plugin_surface_smoke_wiring.py",
    ".github/workflows/plugin-surface-smoke.yml",
}


def _expected_glob(prefix: str) -> str:
    """Directory prefixes are spelled with a trailing `**` glob in the
    workflow; exact filenames pass through unchanged."""
    return f"{prefix}**" if prefix.endswith("/") else prefix


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
    the RDR-197 gap called out. Both directions are tested: a MANDATED
    entry missing from the workflow (silent coverage loss) and a workflow
    entry not accounted for by either MANDATED_SURFACE or KNOWN_EXTRAS
    (silent scope creep -- the `sn/**` finding from the nexus-98gpl
    review, which nothing here used to catch)."""

    @staticmethod
    def _workflow_paths() -> list[str]:
        wf = _workflow()
        on = wf[True] if True in wf else wf["on"]
        return on["pull_request"]["paths"]

    def test_the_path_filter_covers_every_mandated_prefix(self) -> None:
        patterns = self._workflow_paths()
        missing = [p for p in MANDATED_SURFACE if _expected_glob(p) not in patterns]
        assert not missing, (
            f"AGENTS.md step-6 surface prefixes not covered by the "
            f"workflow's path filter: {missing}. A plugin change there "
            f"would merge without this gate ever running."
        )

    def test_the_path_filter_has_no_undocumented_extras(self) -> None:
        """The converse of the test above: every workflow path entry must
        be either a mandated prefix or a named-and-justified KNOWN_EXTRAS
        entry. An addition that is neither is undeclared scope creep --
        it makes the gate fire on inputs release-sandbox.sh smoke never
        touches, which is a cost-discipline violation in the causal sense
        even though it does not break correctness."""
        mandated_globs = {_expected_glob(p) for p in MANDATED_SURFACE}
        extras = set(self._workflow_paths()) - mandated_globs
        undocumented = extras - KNOWN_EXTRAS
        assert not undocumented, (
            f"workflow path filter entries not in MANDATED_SURFACE or "
            f"KNOWN_EXTRAS: {undocumented}. Either it belongs in "
            f"MANDATED_SURFACE (name the AGENTS.md step-6 line that "
            f"requires it) or KNOWN_EXTRAS (name what release-sandbox.sh "
            f"smoke actually touches there), or it should not be in the "
            f"filter at all."
        )
        stale = KNOWN_EXTRAS - extras
        assert not stale, (
            f"KNOWN_EXTRAS names entries the workflow's path filter no "
            f"longer carries: {stale}. Remove them from KNOWN_EXTRAS so "
            f"this test keeps proving something."
        )

    def test_the_path_filter_includes_itself(self) -> None:
        """The self-referential entry plugin-drift-ledger.yml also carries:
        a change that disables the filter must itself trip the filter."""
        patterns = self._workflow_paths()
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

    def test_the_job_has_no_job_level_conditional(self) -> None:
        """Structural check (nexus-98gpl review Suggestion): the shape a
        REQUIRED path-filtered check needs is a job-level `if:` that lets
        the job no-op under some condition while the JOB itself still
        reports a status (skipped == success for branch protection). This
        workflow relies on the workflow-level `paths:` filter instead --
        the job carries no `if:` of its own. A job-level `if:` appearing
        here is exactly the shape a quiet move to 'required' would need."""
        wf = _workflow()
        assert "if" not in wf["jobs"]["smoke"], (
            "the smoke job gained a job-level `if:` condition -- this is "
            "the structural shape a required-check skip shim needs "
            "(skipped == success for branch protection); if this job is "
            "becoming a required check, replace the workflow-level "
            "`paths:` filter with this job-level skip deliberately and "
            "update this test alongside that change, rather than letting "
            "both exist as an accident"
        )

    def test_no_step_swallows_its_own_failure(self) -> None:
        """A `continue-on-error: true` step lets a real failure pass
        silently while the job still reports success -- the other half of
        the required-check-skip-shim shape, applied per-step instead of
        per-job."""
        steps = _job_steps()
        offenders = [s.get("name", "<unnamed>") for s in steps if s.get("continue-on-error")]
        assert not offenders, (
            f"step(s) {offenders} set continue-on-error: true -- a real "
            f"failure there would be swallowed and the job would still "
            f"report green, defeating the entire point of an advisory "
            f"gate (which exists to be looked at when red, not to hide "
            f"reds)"
        )

    def test_the_job_has_no_job_level_skip_shim(self) -> None:
        """Weaker naming-convention tripwire, kept alongside the two
        structural checks above (nexus-98gpl review: acceptable as a
        tripwire, do not over-trust alone). A step literally named with
        'required' would be an odd, easy-to-notice signal on its own."""
        steps = _job_steps()
        names = " ".join(s.get("name", "") for s in steps).lower()
        assert "required" not in names, (
            "a step name mentions 'required' -- if this job became a "
            "required status check, the workflow-level `paths:` filter "
            "above must be replaced with a job-level skip (a path-filtered "
            "required check otherwise hangs PRs that never touch the "
            "surface); update this test alongside that change"
        )
