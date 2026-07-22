# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-27i0m: Developer-ID codesign + notarization arm in
engine-service-release.yml.

Shape pins (the test_pg_bundle_version_parity precedent): the workflow is
not executable in CI-of-CI, so these greps hold the load-bearing
properties — the steps exist, are mac-arm64-gated, run BEFORE the sha256
stage/cosign steps (Developer-ID signing MODIFIES the Mach-O; signing
after hashing would invalidate every published digest), fail loud on
partial secrets, and never silently skip (warning annotation + step
summary on absence — the gates-scripted non-vacuity rule).
"""
from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = (
    Path(__file__).parent.parent
    / ".github" / "workflows" / "engine-service-release.yml"
)


def _text() -> str:
    return WORKFLOW.read_text()


def test_workflow_parses_as_yaml() -> None:
    yaml.safe_load(_text())


def test_codesign_step_present_and_mac_gated() -> None:
    text = _text()
    assert "Developer ID codesign (mac-arm64" in text
    assert "Notarize (mac-arm64" in text
    # Both steps are gated to the mac-arm64 matrix arm.
    for step in ("Developer ID codesign", "Notarize (mac-arm64"):
        idx = text.index(step)
        window = text[idx:idx + 1800]
        assert "matrix.target.arch == 'mac-arm64'" in window, (
            f"{step} lost its mac-arm64 gate"
        )


def test_signing_runs_before_hashing_and_cosign() -> None:
    """codesign rewrites the binary — every digest (sha256 stage, cosign
    bundles) must be computed AFTER it or published verification breaks."""
    text = _text()
    assert text.index("Developer ID codesign") < text.index("Stage artifact + sha256")
    assert text.index("Developer ID codesign") < text.index("Sign release asset (cosign")
    assert text.index("Notarize (mac-arm64") < text.index("Stage artifact + sha256")


def test_absent_secrets_warn_never_silent() -> None:
    text = _text()
    assert "::warning title=mac-arm64 UNSIGNED" in text
    assert "::warning title=mac-arm64 NOT NOTARIZED" in text
    assert text.count("GITHUB_STEP_SUMMARY") >= 4, (
        "each signing/notarize outcome must land in the step summary"
    )


def test_partial_secrets_fail_loud() -> None:
    text = _text()
    assert "PARTIALLY configured" in text
    assert text.count("PARTIALLY configured") == 2, (
        "both the cert and notary secret sets need the partial-config guard"
    )


def test_notarize_refuses_adhoc_binary() -> None:
    """Submitting an ad-hoc binary is a guaranteed Apple rejection minutes
    later — the workflow must fail immediately with the real reason."""
    assert "cannot notarize" in _text()


def test_hardened_runtime_and_timestamp() -> None:
    """Notarization REQUIRES --options runtime and a secure timestamp."""
    assert "codesign --force --options runtime --timestamp" in _text()


def test_team_identity_non_vacuity_assert() -> None:
    """The sign step must prove a real TeamIdentifier landed — a silent
    ad-hoc survivor is exactly the failure the bead documents (spctl
    rejected on v0.1.6)."""
    text = _text()
    assert "TeamIdentifier=" in text
    assert "ad-hoc signature survived" in text


def test_keychain_cleanup_always_runs() -> None:
    text = _text()
    idx = text.index("Clean up signing keychain")
    window = text[idx:idx + 400]
    assert "always()" in window


def test_secret_names_documented_for_provisioning() -> None:
    """The six secrets Hal must provision are named in the workflow (the
    bead's checklist survives in-repo, not only in bd)."""
    text = _text()
    for name in (
        "APPLE_DEV_ID_CERT_P12",
        "APPLE_DEV_ID_CERT_PASSWORD",
        "APPLE_DEV_ID_IDENTITY",
        "APPLE_NOTARY_KEY_P8",
        "APPLE_NOTARY_KEY_ID",
        "APPLE_NOTARY_ISSUER_ID",
    ):
        assert name in text, f"secret {name} vanished from the workflow"
