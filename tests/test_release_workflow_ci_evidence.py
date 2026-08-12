# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-jvhsw: release.yml must verify the tagged commit already passed CI
before publishing to PyPI -- via EVIDENCE (a read of GitHub's existing
check-run record), never by re-running the test suite (CI Cost Discipline,
the 2026-07-06 billing incident).

Shape pins (the ``test_pg_bundle_version_parity`` precedent, same as
``test_engine_release_workflow_signing.py``): the workflow is not
executable in CI-of-CI, so these assertions on the parsed YAML + raw text
hold the load-bearing properties -- the evidence step exists, runs BEFORE
the build/publish steps, is never gated `needs:` on a NEW test job, and the
job requests the ``checks: read`` permission the evidence script requires.
"""
from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "release.yml"


def _text() -> str:
    return WORKFLOW.read_text()


def _doc() -> dict:
    return yaml.safe_load(_text())


def test_workflow_parses_as_yaml() -> None:
    _doc()


def test_evidence_script_is_invoked() -> None:
    text = _text()
    assert "scripts/check_release_ci_evidence.py" in text, (
        "release.yml must invoke the nexus-jvhsw evidence gate -- an "
        "unwired script is exactly as skippable as the prose gate it "
        "replaces (nexus-qc4p1 class)"
    )


def test_evidence_step_runs_before_build_and_publish() -> None:
    """A gate that runs AFTER the expensive build/publish work defeats its
    own purpose -- it must fail fast, before any build cost is paid."""
    text = _text()
    evidence_pos = text.index("check_release_ci_evidence.py")
    build_pos = text.index("Build wheel and sdist")
    publish_pos = text.index("Publish to PyPI")
    assert evidence_pos < build_pos < publish_pos


def test_evidence_step_uses_the_checked_out_commit_not_github_sha() -> None:
    """On workflow_dispatch, $GITHUB_SHA is the DEFAULT BRANCH's tip, not
    the named tag's commit -- checkout already resolved the right commit
    into the working tree regardless of trigger type, so the step's actual
    ``--sha`` argument must come from git, never from the ambient env var
    (the surrounding comment is allowed to MENTION $GITHUB_SHA to explain
    why -- only the invocation itself is pinned here)."""
    text = _text()
    idx = text.index("python3 scripts/check_release_ci_evidence.py")
    invocation = text[idx:idx + 200]
    assert '--sha "$(git rev-parse HEAD)"' in invocation
    assert "GITHUB_SHA" not in invocation


def test_publish_job_requests_checks_read_permission() -> None:
    doc = _doc()
    perms = doc["jobs"]["publish"]["permissions"]
    assert perms.get("checks") == "read", (
        "the evidence step needs checks:read to query the Checks API -- "
        "without it the gate would fail closed on every release with a "
        "403, which is safe but not the intent"
    )


def test_no_new_test_job_or_needs_reintroduced() -> None:
    """THE FORBIDDEN FIX: adding test jobs or a `needs:` dependency on
    ci.yml's matrix would RE-RUN CI at tag time, violating the cost
    directive this same fix is required to honor. `needs:` count must stay
    at zero -- release.yml has exactly one job (`publish`), so there is
    nothing for a `needs:` to depend ON without inventing a second job."""
    doc = _doc()
    jobs = doc["jobs"]
    assert set(jobs) == {"publish"}, (
        f"release.yml grew a new job ({set(jobs) - {'publish'}}) -- "
        "verify it is not a re-introduced test job before accepting this"
    )
    assert "needs" not in jobs["publish"], (
        "publish must not depend on a test job -- CI Cost Discipline "
        "forbids re-testing the tagged tree (2026-07-06 billing incident)"
    )


def test_evidence_gate_module_states_the_forbidden_fix() -> None:
    """The module docstring must itself carry the constraint, so a future
    editor who opens the script (not the workflow) also hits the warning
    before reaching for `needs:`."""
    script = Path(__file__).parent.parent / "scripts" / "check_release_ci_evidence.py"
    text = script.read_text()
    assert "CI Cost Discipline" in text
    assert "never test the same tree twice" in text.lower() or "re-run" in text.lower()
