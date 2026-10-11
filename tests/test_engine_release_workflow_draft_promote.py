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


def test_release_is_a_draft_until_only_promote_release_publishes() -> None:
    """Created as a DRAFT so an upstream failure leaves no consumable release; nothing but
    promote-release flips it, and matrix uploads into the draft never do."""
    jobs = _jobs()
    assert re.search(r"gh release create .*--draft", _step_text(jobs["create-release"]))
    publishers = [
        name for name, job in jobs.items()
        if "--draft=false" in _step_text(job) or "promote_engine_release.sh" in _step_text(job)
    ]
    assert publishers == ["promote-release"], publishers
    for name, job in jobs.items():
        if name != "create-release":
            assert "gh release create" not in _step_text(job), name
    for matrix in _MATRICES:
        text = _step_text(jobs[matrix])
        assert "gh release upload" in text
        assert "--draft=false" not in text and "gh release edit" not in text


def test_promote_release_needs_both_matrices_to_have_succeeded() -> None:
    """fail-fast is false, so a skipped or failed leg still reports through the matrix result:
    require success, not merely not-failed."""
    promote = _jobs()["promote-release"]
    for matrix in _MATRICES:
        assert matrix in promote["needs"], f"promote-release must need {matrix}"
        assert f"needs.{matrix}.result == 'success'" in promote["if"]
    assert "startsWith(github.ref, 'refs/tags/engine-service-v')" in promote["if"]
    text = _step_text(promote)
    assert "scripts/promote_engine_release.sh" in text
    assert "--draft=false" not in text, "the flip lives in the script, driven by its own test"


def test_promote_script_asserts_every_asset_and_the_size_ceilings_before_publishing() -> None:
    """Both checks come BEFORE the publish, and a miss exits non-zero (nexus-cl14i, nexus-ujbz8)."""
    text = (Path(__file__).parent.parent / "scripts" / "promote_engine_release.sh").read_text()
    for name in (
        "nexus-service-$arch", "$b.sha256", "$b.cosign.bundle", "$b.sigstore.json",
        "$b.txz", "$b.txz.sha256", "$b.txz.sigstore.json",
        "nexus-pg-$arch.txz", "$p.sha256", "$p.sigstore.json",
    ):
        assert name in text, f"asset {name} not asserted"
    assert "linux-amd64 linux-arm64 mac-arm64" in text
    assert text.index("exit 1") < text.index("--draft=false")
    assert "check_engine_cut_riders.py" in text and " sizes " in text
    assert text.index("check_engine_cut_riders.py") < text.index("--draft=false")
    assert "scripts/check_engine_cut_riders.py" in WORKFLOW.read_text().split("promote-release:")[1]


def _mac_leg(job: str) -> dict:
    return next(t for t in _jobs()[job]["strategy"]["matrix"]["target"] if t["arch"] == "mac-arm64")


def test_the_mac_legs_build_on_the_pool_and_a_hellmini_job_smokes_what_they_built() -> None:
    """nexus-9q6ox: the lab pool's mac class has no Docker, which native-smoke.sh needs. The mac
    build legs default to lab-mac-m; the smoke runs on hellmini against the uploaded artifact
    (on a tag, the signed bytes), and promotion requires it to have succeeded."""
    for job in _MATRICES:
        assert _mac_leg(job)["runner"] == "${{ inputs.mac_runner || 'lab-mac-m' }}", job
    # The build leg smokes inline only when a dispatch names hellmini itself.
    assert _mac_leg("build-publish")["smoke"] == "${{ (inputs.mac_runner || 'lab-mac-m') == 'hellmini' }}"
    spec = yaml.safe_load(WORKFLOW.read_text())
    mac_runner = spec.get("on", spec.get(True))["workflow_dispatch"]["inputs"]["mac_runner"]
    assert mac_runner["default"] == "lab-mac-m"
    assert mac_runner["options"] == ["lab-mac-m", "hellmini", "macos-14"]

    smoke = _jobs()["smoke-mac-arm64"]
    assert smoke["runs-on"] == "hellmini"
    assert smoke["needs"] == "build-publish"
    assert "needs.build-publish.result == 'success'" in smoke["if"]
    assert "(inputs.mac_runner || 'lab-mac-m') == 'lab-mac-m'" in smoke["if"]
    assert smoke["permissions"] == {"contents": "read"}, "the smoke publishes nothing"
    download = next(s for s in smoke["steps"] if "download-artifact" in s.get("uses", ""))
    assert download["with"]["name"] == "nexus-service-mac-arm64"
    text = _step_text(smoke)
    assert "shasum -a 256 -c nexus-service-mac-arm64.sha256" in text, "the smoked bytes are the uploaded bytes"
    assert "./native-smoke.sh" in text
    assert not any(v in text for v in ("gh release", "cosign", "mac-sign.sh")), "the smoke job ships nothing"

    promote = _jobs()["promote-release"]
    assert "smoke-mac-arm64" in promote["needs"]
    assert "needs.smoke-mac-arm64.result == 'success'" in promote["if"]
