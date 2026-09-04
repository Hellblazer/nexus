"""nexus-cl14i: the engine release is all-or-nothing.

``engine-service-v0.1.95`` (run 33697831082) published a non-draft GitHub
release carrying PG bundles and zero engine binaries after a transient
upstream failure skipped the native matrix. Every consumer keys on the
tag and none inspects the asset set, so the release looked shipped and
was not installable. The fix creates the release as a DRAFT and adds a
final ``promote-release`` job that needs both matrices, requires both to
have succeeded, asserts the full asset set by name, and only then flips
the draft flag. These tests pin that shape structurally: a green run of
the workflow proves nothing here, because the happy path was already
fine; the shape is what has to hold when something upstream fails.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

WORKFLOW = (
    Path(__file__).parent.parent
    / ".github"
    / "workflows"
    / "engine-service-release.yml"
)

_MATRICES = ("build-publish", "build-publish-pg-bundle")


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def _step_text(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


def test_release_is_created_as_a_draft() -> None:
    create = _step_text(_jobs()["create-release"])
    assert re.search(r"gh release create .*--draft", create), (
        "create-release must create a DRAFT so an upstream failure leaves no "
        "consumable release (nexus-cl14i)"
    )


def test_only_promote_release_publishes() -> None:
    jobs = _jobs()
    publishers = [
        name for name, job in jobs.items()
        if "--draft=false" in _step_text(job) or "promote_engine_release.sh" in _step_text(job)
    ]
    assert publishers == ["promote-release"], publishers
    for name, job in jobs.items():
        if name != "create-release":
            assert "gh release create" not in _step_text(job), name


def test_promote_release_needs_both_matrices_and_requires_their_success() -> None:
    promote = _jobs()["promote-release"]
    needs = promote["needs"]
    for matrix in _MATRICES:
        assert matrix in needs, f"promote-release must need {matrix}"
        assert f"needs.{matrix}.result == 'success'" in promote["if"], (
            f"promote-release must require {matrix} to have SUCCEEDED, not "
            "merely not-failed: fail-fast is false, so a skipped or failed leg "
            "still reports through the matrix result"
        )
    assert "startsWith(github.ref, 'refs/tags/engine-service-v')" in promote["if"]


def test_promote_release_runs_the_promote_script_and_nothing_else_publishes() -> None:
    text = _step_text(_jobs()["promote-release"])
    assert "scripts/promote_engine_release.sh" in text
    assert "--draft=false" not in text, "the flip lives in the script, driven by its own test"


def test_promote_script_asserts_every_expected_asset_before_publishing() -> None:
    text = (Path(__file__).parent.parent / "scripts" / "promote_engine_release.sh").read_text()
    for name in (
        "nexus-service-$arch", "$b.sha256", "$b.cosign.bundle", "$b.sigstore.json",
        "nexus-pg-$arch.txz", "$p.sha256", "$p.sigstore.json",
    ):
        assert name in text, f"asset {name} not asserted"
    assert "linux-amd64 linux-arm64 mac-arm64" in text
    # The assertion must come BEFORE the publish, and a miss must exit non-zero.
    assert text.index("exit 1") < text.index("--draft=false")


def test_matrix_uploads_do_not_publish() -> None:
    """Uploads into a draft are fine; nothing in the matrices may flip it."""
    jobs = _jobs()
    for matrix in _MATRICES:
        text = _step_text(jobs[matrix])
        assert "gh release upload" in text
        assert "--draft=false" not in text
        assert "gh release edit" not in text
