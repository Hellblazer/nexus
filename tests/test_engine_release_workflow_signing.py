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

import re

import yaml

REPO = Path(__file__).parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "engine-service-release.yml"
REHEARSAL = REPO / ".github" / "workflows" / "mac-signing-rehearsal.yml"
SIGN_SCRIPT = REPO / "service" / "deploy" / "mac-sign.sh"
G2_CER = REPO / "service" / "deploy" / "apple" / "DeveloperIDG2CA.cer"


def _text() -> str:
    return WORKFLOW.read_text()


def _code(text: str) -> str:
    """Executable lines only. The signing mechanics are also NARRATED in
    comments, and a pin that a comment can satisfy is not a pin."""
    return "\n".join(
        ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
    )


def _script() -> str:
    return _code(SIGN_SCRIPT.read_text())


def _step_run(spec: dict, job: str, name_prefix: str) -> str:
    for step in spec["jobs"][job]["steps"]:
        if str(step.get("name", "")).startswith(name_prefix):
            return str(step.get("run", ""))
    raise AssertionError(f"{job}: no step named {name_prefix!r}")


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


def test_entitlements_disable_library_validation() -> None:
    """Critique 96677bf7 Critical / nexus-2oh5q: Hardened Runtime implies
    Library Validation, which refuses the bundled onnxruntime/DJL dylibs
    local-mode embedding System.load()s — signing WITHOUT the entitlement
    ships a binary that crashes where the ad-hoc one worked."""
    script = _script()
    assert 'ENTITLEMENTS="$HERE/mac-entitlements.plist"' in script
    assert '--entitlements "$ENTITLEMENTS"' in script
    plist = REPO / "service" / "deploy" / "mac-entitlements.plist"
    assert plist.exists(), "mac-entitlements.plist vanished"
    assert "com.apple.security.cs.disable-library-validation" in plist.read_text()


def test_post_activation_regression_guard() -> None:
    """Critique 96677bf7 Significant 1: once signing is activated, vanished
    secrets must hard-fail (vars.APPLE_SIGNING_REQUIRED), never regress to
    a warn-in-a-run-nobody-reads."""
    text = _text()
    assert text.count("APPLE_SIGNING_REQUIRED: ${{ vars.APPLE_SIGNING_REQUIRED }}") == 2, (
        "both signing steps must read the activation variable"
    )
    assert text.count('if [ "${APPLE_SIGNING_REQUIRED:-}" = "true" ]') == 2, (
        "both absent-secrets branches need the activation hard-fail guard"
    )


def test_signing_is_tag_gated_like_cosign() -> None:
    """Review 96677bf7 Critical: without the tag gate, every
    workflow_dispatch dev/smoke run would burn a Developer-ID sign + a
    20-minute notarytool --wait on a 10x-billed macOS runner for a 14-day
    Actions artifact — the CI Cost Discipline class. Both steps must gate
    on the release tag exactly like the sibling cosign steps."""
    text = _text()
    for step in ("Developer ID codesign", "Notarize (mac-arm64"):
        idx = text.index(step)
        window = text[idx:idx + 1800]
        assert "startsWith(github.ref, 'refs/tags/engine-service-v')" in window, (
            f"{step} lost its release-tag gate"
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
    assert "cannot notarize" in _script()


def test_hardened_runtime_and_timestamp() -> None:
    """Notarization REQUIRES --options runtime and a secure timestamp."""
    assert "codesign --force --options runtime --timestamp" in _script()


def test_team_identity_non_vacuity_assert() -> None:
    """The sign step must prove a real TeamIdentifier landed — a silent
    ad-hoc survivor is exactly the failure the bead documents (spctl
    rejected on v0.1.6)."""
    script = _script()
    assert "TeamIdentifier=" in script
    assert "ad-hoc signature survived" in script


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


def test_signing_job_is_environment_scoped() -> None:
    """The APPLE_* secrets must be reachable only from an environment-gated job.

    Repo-level secrets are readable by ANY job in ANY workflow that names them,
    including one added later. The Developer ID private key is the credential
    here whose compromise is not cleanly recoverable — Apple's remedy is
    revoking the cert, and Gatekeeper checks revocation ONLINE, so binaries
    already published can start failing. Scoping the secrets to the
    `apple-signing` environment (required reviewer + deployment policy limited
    to the release tag) is what makes "only an approved release run can read the
    signing key" true.

    Deleting the one `environment:` line silently reverts that to repo-wide
    readability with no other visible symptom, which is exactly why it is
    pinned here rather than left to review.
    """
    spec = yaml.safe_load(_text())
    job = spec["jobs"]["build-publish"]

    assert job.get("environment") == "apple-signing", (
        "build-publish must declare environment: apple-signing — without it the "
        "APPLE_* secrets fall back to repo scope, readable by every workflow"
    )

    # The secrets are consumed by THIS job, so this job is the one that has to
    # carry the environment. If the signing steps ever move, this assertion
    # moves with them rather than being quietly satisfied by the wrong job.
    step_env_blobs = " ".join(
        str(step.get("env", "")) for step in job["steps"]
    )
    assert "APPLE_DEV_ID_CERT_P12" in step_env_blobs, (
        "the codesign secrets are no longer read by build-publish — move this "
        "environment assertion to whichever job now reads them"
    )


def test_release_steps_run_the_shared_sign_script() -> None:
    """nexus-aq9y8: the release and the rehearsal must execute the SAME
    signing bytes, or a green rehearsal proves nothing about the release."""
    spec = yaml.safe_load(_text())
    for step, verb in (
        ("Developer ID codesign", "sign"),
        ("Notarize (mac-arm64", "notarize"),
        ("Clean up signing keychain", "cleanup"),
    ):
        run = _code(_step_run(spec, "build-publish", step))
        assert f"service/deploy/mac-sign.sh {verb}" in run, (
            f"{step} no longer calls mac-sign.sh {verb}"
        )
    # The inline mechanics must not creep back in beside the script call.
    codesign_run = _code(_step_run(spec, "build-publish", "Developer ID codesign"))
    assert "security import" not in codesign_run
    assert "codesign --force" not in codesign_run


def test_sign_script_is_executable_and_parses() -> None:
    import os
    import subprocess

    assert os.access(SIGN_SCRIPT, os.X_OK), "mac-sign.sh lost its exec bit"
    subprocess.run(["bash", "-n", str(SIGN_SCRIPT)], check=True)


def test_vendored_g2_intermediate_matches_the_pinned_hash() -> None:
    """The script refuses a G2 intermediate whose SHA-256 differs from its
    pin; check the vendored file and the pin agree, and that the pin is
    the published Apple fingerprint (CN=Developer ID Certification
    Authority, OU=G2)."""
    import hashlib

    digest = hashlib.sha256(G2_CER.read_bytes()).hexdigest()
    assert digest == (
        "f16cd3c54c7f83cea4bf1a3e6a0819c8aaa8e4a1528fd144715f350643d2df3a"
    )
    assert f'G2_SHA256="{digest}"' in _script()


def test_sign_script_keychain_shape() -> None:
    """The v0.1.142 failure (identity imported, then "no identity found")
    is what these pin against: intermediate imported into the temp
    keychain, codesign told which keychain to use, a find-identity
    preflight BEFORE codesign, and a cleanup that restores the saved
    search list instead of forcing login.keychain-db."""
    script = _script()
    assert 'security import "$G2_CER" -k "$KEYCHAIN"' in script
    assert '--keychain "$KEYCHAIN"' in script
    g2 = script.index('security import "$G2_CER"')
    preflight = script.index("security find-identity -v -p codesigning")
    sign = script.index("codesign --force")
    assert g2 < preflight < sign, (
        "the G2 intermediate must be in the keychain before the identity "
        "preflight, and the preflight must pass before codesign runs"
    )
    assert "login.keychain-db" not in script
    assert '"$SAVED_LIST"' in script


def test_notarize_success_is_the_status_line_not_the_exit_code() -> None:
    assert "status:\\ Accepted" in _script()
    assert "notarytool log" in _script()


def test_rehearsal_workflow_shape() -> None:
    """Push to develop limited to the signing inputs, plus dispatch; never
    pull_request (public repo, self-hosted runner). Owner-only on hellmini,
    environment-scoped secrets, the shared script, loud failure on missing
    secrets, notarization on push runs, and an always() cleanup."""
    text = REHEARSAL.read_text()
    spec = yaml.safe_load(text)
    triggers = spec.get("on", spec.get(True))
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"]["branches"] == ["develop"]
    paths = triggers["push"]["paths"]
    assert "service/deploy/mac-sign.sh" in paths
    assert all(
        p.startswith("service/deploy/") or p == ".github/workflows/mac-signing-rehearsal.yml"
        for p in paths
    ), "the push trigger must stay limited to the signing inputs"
    for name in ("Notarize", "Gatekeeper verdict (quarantined copy)"):
        step = next(s for s in spec["jobs"]["rehearse"]["steps"] if s.get("name") == name)
        assert "github.event_name == 'push'" in step["if"], (
            f"{name} must run on push, where inputs.notarize is empty"
        )
    job = spec["jobs"]["rehearse"]
    assert job["runs-on"] == "hellmini"
    assert job["environment"] == "apple-signing"
    assert "github.actor == 'Hellblazer'" in job["if"]
    code = _code(text)
    for verb in ("sign", "notarize", "cleanup"):
        assert f"service/deploy/mac-sign.sh {verb}" in code
    assert "::warning" not in code, "a rehearsal must fail, never warn-and-skip"
    assert "source=Notarized Developer ID" in code
    cleanup = next(s for s in job["steps"] if s.get("name") == "Clean up signing keychain")
    assert cleanup.get("if") == "always()"


def test_mac_abi_floor_arm_is_enforced_not_informational() -> None:
    """nexus-0n3vt: the mac-* arm of "Assert binary ABI floor" was
    `otool ... || true` — purely informational while every linux arm
    exits 1, on the one platform whose binary also ships unsmoked
    (nexus-4xf5m). Pin the enforced shape: the mac arm extracts minos,
    FAILS on an empty probe (a probe that produced nothing must never
    read as a pass), and FAILS above the dated ceiling."""
    text = _text()
    start = text.index("mac-*)")
    end = text.index(";;", start)
    # Comment lines are allowed to NARRATE the old `|| true` bug; only
    # executable lines are pinned.
    arm = "\n".join(
        ln for ln in text[start:end].splitlines()
        if not ln.lstrip().startswith("#")
    )
    assert "|| true" not in arm, (
        "the mac ABI arm regressed to informational-only (`|| true`)"
    )
    assert arm.count("exit 1") >= 2, (
        "the mac ABI arm must fail BOTH on an unparseable minos probe and "
        "on a floor violation"
    )
    assert "could not read LC_BUILD_VERSION" in arm
    assert re.search(r'ceiling="\d+\.\d+"', arm), (
        "the mac ABI arm must carry an explicit numeric macOS ceiling"
    )
