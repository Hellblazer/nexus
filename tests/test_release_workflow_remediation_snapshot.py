# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-fehi3: release.yml must replay the remediation-commit-ride-release
gate against a committed pre-tag snapshot, since this repo's `bd` backend
(Dolt) has no credentials on the CI runner and cannot run `bd export` live
(nexus-fix9t's rejection of wiring the gate against the stale tracked
.beads/issues.jsonl -- see that bead's description for the full verification).

Shape pins (the ``test_release_workflow_ci_evidence.py`` precedent, same as
``test_pg_bundle_version_parity``): the workflow is not executable in
CI-of-CI, so these assertions on the parsed YAML + raw text hold the
load-bearing properties -- the replay step exists, runs BEFORE
build/publish, uses --verify-snapshot (not a bare load), and the workflow
grows no new job/needs to get there (CI Cost Discipline).
"""
from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "release.yml"


def _text() -> str:
    return WORKFLOW.read_text()


def _doc() -> dict:
    return yaml.safe_load(_text())


def test_remediation_snapshot_gate_is_invoked() -> None:
    text = _text()
    assert "scripts/check_remediation_commits_ride_release.py" in text, (
        "release.yml must invoke the nexus-fehi3 remediation-snapshot replay -- an "
        "unwired script is exactly as skippable as the honor-system step it replaces"
    )


def test_remediation_snapshot_gate_uses_verify_snapshot_flag() -> None:
    """A bare --bd-export-json load (no --verify-snapshot) would skip both
    the committed-on-this-ref check and the staleness check -- CI must use
    the full verification mode, not just parse the file."""
    text = _text()
    idx = text.index("python3 scripts/check_remediation_commits_ride_release.py")
    invocation = text[idx : idx + 300]
    assert "--verify-snapshot" in invocation
    assert "--bd-export-json" in invocation
    assert ".release-gates/remediation-snapshot.json" in invocation


def test_remediation_snapshot_gate_uses_resolved_release_tag_not_github_ref_name() -> None:
    """Same v7.7.0-retry-incident class the evidence step already guards
    against: on workflow_dispatch, $GITHUB_REF_NAME is the triggering
    BRANCH, not the tag. This step must use the already-resolved
    $RELEASE_TAG env var, set by the 'Resolve release tag' step."""
    text = _text()
    idx = text.index("python3 scripts/check_remediation_commits_ride_release.py")
    invocation = text[idx : idx + 300]
    assert '--release-ref "$RELEASE_TAG"' in invocation


def test_remediation_snapshot_step_runs_before_build_and_publish() -> None:
    text = _text()
    gate_pos = text.index("check_remediation_commits_ride_release.py")
    build_pos = text.index("Build wheel and sdist")
    publish_pos = text.index("Publish to PyPI")
    assert gate_pos < build_pos < publish_pos


def test_remediation_snapshot_step_runs_after_ci_evidence_step() -> None:
    """Both are cheap stdlib-only fail-fast checks; ordering itself is not
    load-bearing beyond 'before build/publish', but pins the intended
    placement (immediately after the jvhsw evidence step) so a future edit
    that separates them is a deliberate choice, not drift."""
    text = _text()
    evidence_pos = text.index("check_release_ci_evidence.py")
    gate_pos = text.index("check_remediation_commits_ride_release.py")
    assert evidence_pos < gate_pos


def test_workflow_parses_as_yaml() -> None:
    _doc()


def test_no_new_test_job_or_needs_reintroduced() -> None:
    """Same forbidden-fix guard as the evidence gate's test -- this is one
    more step inside the existing `publish` job, never a new job or a
    `needs:` dependency (CI Cost Discipline: never re-test the same tree)."""
    doc = _doc()
    jobs = doc["jobs"]
    assert set(jobs) == {"publish"}, (
        f"release.yml grew a new job ({set(jobs) - {'publish'}}) -- "
        "verify it is not a re-introduced test job before accepting this"
    )
    assert "needs" not in jobs["publish"]
