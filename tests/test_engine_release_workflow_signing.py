# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-27i0m: Developer-ID codesign + notarization arm in
engine-service-release.yml.

Shape pins (the test_pg_bundle_version_parity precedent): the workflow is
not executable in CI-of-CI, so these greps hold the load-bearing
properties, one test per invariant. The signing mechanics are also NARRATED
in comments, and a pin that a comment can satisfy is not a pin, so the
script and step bodies are read with comment lines stripped.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "engine-service-release.yml"
REHEARSAL = REPO / ".github" / "workflows" / "mac-signing-rehearsal.yml"
SIGN_SCRIPT = REPO / "service" / "deploy" / "mac-sign.sh"
G2_CER = REPO / "service" / "deploy" / "apple" / "DeveloperIDG2CA.cer"


def _code(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def _step_run(spec: dict, job: str, name_prefix: str) -> str:
    for step in spec["jobs"][job]["steps"]:
        if str(step.get("name", "")).startswith(name_prefix):
            return str(step.get("run", ""))
    raise AssertionError(f"{job}: no step named {name_prefix!r}")


def test_signing_steps_are_mac_and_tag_gated_and_run_before_any_digest() -> None:
    """codesign rewrites the Mach-O, so every digest (sha256 stage, cosign bundles) must be
    computed AFTER it. Both steps gate on mac-arm64 and on the release tag like the sibling
    cosign steps (a dev run must not burn a 20-minute notarytool --wait on a 10x-billed
    runner), and run the SAME script the rehearsal runs, with no inline mechanics beside it."""
    text = WORKFLOW.read_text()
    spec = yaml.safe_load(text)
    for step in ("Developer ID codesign (mac-arm64", "Notarize (mac-arm64"):
        window = text[text.index(step):][:1800]
        assert "matrix.target.arch == 'mac-arm64'" in window, f"{step} lost its mac-arm64 gate"
        assert "startsWith(github.ref, 'refs/tags/engine-service-v')" in window, f"{step} lost its release-tag gate"
    assert text.index("Developer ID codesign") < text.index("Stage artifact + sha256")
    assert text.index("Developer ID codesign") < text.index("Sign release asset (cosign")
    assert text.index("Notarize (mac-arm64") < text.index("Stage artifact + sha256")
    for step, verb in (
        ("Developer ID codesign", "sign"),
        ("Notarize (mac-arm64", "notarize"),
        ("Clean up signing keychain", "cleanup"),
    ):
        assert f"service/deploy/mac-sign.sh {verb}" in _code(_step_run(spec, "build-publish", step)), step
    codesign_run = _code(_step_run(spec, "build-publish", "Developer ID codesign"))
    assert "security import" not in codesign_run and "codesign --force" not in codesign_run


def test_signing_secrets_are_environment_scoped_and_the_skip_is_reported() -> None:
    """Repo-level secrets are readable by ANY job in ANY workflow that names them; the Developer
    ID key is the credential whose compromise is not cleanly recoverable, so build-publish
    carries `environment: apple-signing` and the secrets it reads are the six provisioned ones.
    An ad-hoc release says so in the step summary, and the keychain is always cleaned up."""
    text = WORKFLOW.read_text()
    job = yaml.safe_load(text)["jobs"]["build-publish"]
    assert job.get("environment") == "apple-signing"
    step_env = " ".join(str(step.get("env", "")) for step in job["steps"])
    for name in ("APPLE_DEV_ID_CERT_P12", "APPLE_DEV_ID_CERT_PASSWORD", "APPLE_DEV_ID_IDENTITY",
                 "APPLE_NOTARY_KEY_P8", "APPLE_NOTARY_KEY_ID", "APPLE_NOTARY_ISSUER_ID"):
        assert name in text, f"secret {name} vanished from the workflow"
    assert "APPLE_DEV_ID_CERT_P12" in step_env, "the codesign secrets are no longer read by build-publish"
    assert text.count("APPLE_SIGNING_REQUIRED: ${{ vars.APPLE_SIGNING_REQUIRED }}") == 2
    assert "always()" in text[text.index("Clean up signing keychain"):][:400]


_VERB = {"Developer ID codesign": "sign", "Notarize (mac-arm64": "notarize"}
_SECRETS = {
    "Developer ID codesign": ("APPLE_DEV_ID_CERT_P12", "APPLE_DEV_ID_CERT_PASSWORD", "APPLE_DEV_ID_IDENTITY"),
    "Notarize (mac-arm64": ("APPLE_NOTARY_KEY_P8", "APPLE_NOTARY_KEY_ID", "APPLE_NOTARY_ISSUER_ID"),
}


def _run_step(tmp_path: Path, step: str, required: str | None, present: tuple[str, ...]) -> tuple[int, str, str]:
    """Execute the step's real run body under bash in a FRESH scratch tree (never shared
    between steps, so one step's marker or summary cannot satisfy another's assert) whose
    mac-sign.sh is a stub recording its verb. Returns (exit code, verb called or "", summary)."""
    tmp_path = Path(tempfile.mkdtemp(dir=tmp_path))
    run = _step_run(yaml.safe_load(WORKFLOW.read_text()), "build-publish", step)
    stub = tmp_path / "service" / "deploy" / "mac-sign.sh"
    stub.parent.mkdir(parents=True, exist_ok=True)
    marker = tmp_path / "called"
    stub.write_text(f'#!/usr/bin/env bash\nprintf %s "$1" > "{marker}"\necho "PASSED ($1)"\n')
    stub.chmod(0o755)
    summary = tmp_path / "summary.md"
    env = {"PATH": os.environ["PATH"], "RUNNER_TEMP": str(tmp_path), "GITHUB_STEP_SUMMARY": str(summary)}
    if required is not None:
        env["APPLE_SIGNING_REQUIRED"] = required
    env.update({name: "x" for name in present})
    proc = subprocess.run(["bash", "-c", run], cwd=tmp_path, env=env, capture_output=True, text=True)
    return (
        proc.returncode,
        marker.read_text() if marker.exists() else "",
        summary.read_text() if summary.exists() else "",
    )


def test_signing_is_opt_in_by_variable_not_by_secret_presence(tmp_path: Path) -> None:
    """nexus-e8iml: the secrets live in apple-signing so the off-release rehearsal can run, and
    restoring them for that must not arm the next engine tag. A release signs and notarizes only
    when the repo variable APPLE_SIGNING_REQUIRED is exactly "true"; otherwise it ships the
    ad-hoc binary whatever secrets exist, and a signing defect or a slow notary cannot block it."""
    for step, secrets in _SECRETS.items():
        for required in (None, "", "false", "TRUE", "yes"):
            for present in ((), secrets, secrets[:1]):
                rc, called, summary = _run_step(tmp_path, step, required, present)
                assert (rc, called) == (0, ""), f"{step}: switch={required!r} secrets={present} must skip"
                assert "SKIPPED" in summary and "APPLE_SIGNING_REQUIRED" in summary, step


def test_switched_on_signing_fails_loud_on_missing_secrets_and_runs_when_complete(tmp_path: Path) -> None:
    """Switched on, a release never downgrades silently: no secrets or a partial set fails the
    step before the script runs; the full set runs the shared script."""
    for step, secrets in _SECRETS.items():
        for present in ((), secrets[:1], secrets[:2]):
            rc, called, _ = _run_step(tmp_path, step, "true", present)
            assert rc != 0 and not called, f"{step}: switched on with secrets={present} must fail before signing"
        rc, called, summary = _run_step(tmp_path, step, "true", secrets)
        assert (rc, called) == (0, _VERB[step]), step
        assert "PASSED" in summary, step


def test_the_shared_sign_script_enforces_the_signing_invariants() -> None:
    """The release and the rehearsal execute these bytes (nexus-aq9y8)."""
    assert os.access(SIGN_SCRIPT, os.X_OK), "mac-sign.sh lost its exec bit"
    subprocess.run(["bash", "-n", str(SIGN_SCRIPT)], check=True)
    script = _code(SIGN_SCRIPT.read_text())
    # Hardened Runtime implies Library Validation, which refuses the bundled onnxruntime/DJL
    # dylibs: signing without the entitlement ships a binary that crashes (nexus-2oh5q).
    assert 'ENTITLEMENTS="$HERE/mac-entitlements.plist"' in script
    assert '--entitlements "$ENTITLEMENTS"' in script
    plist = REPO / "service" / "deploy" / "mac-entitlements.plist"
    assert "com.apple.security.cs.disable-library-validation" in plist.read_text()
    assert "codesign --force --options runtime --timestamp" in script  # notarization requires both
    assert "TeamIdentifier=" in script and "ad-hoc signature survived" in script  # non-vacuity
    assert "cannot notarize" in script  # an ad-hoc submission is a guaranteed Apple rejection
    assert "status:\\ Accepted" in script and "notarytool log" in script  # the status line, not the exit code
    digest = hashlib.sha256(G2_CER.read_bytes()).hexdigest()
    assert digest == "f16cd3c54c7f83cea4bf1a3e6a0819c8aaa8e4a1528fd144715f350643d2df3a"
    assert f'G2_SHA256="{digest}"' in script
    # The v0.1.142 failure (identity imported, then "no identity found"): the intermediate is in
    # the temp keychain before the preflight, the preflight passes before codesign, and cleanup
    # restores the saved search list rather than forcing login.keychain-db.
    assert 'security import "$G2_CER" -k "$KEYCHAIN"' in script and '--keychain "$KEYCHAIN"' in script
    assert (
        script.index('security import "$G2_CER"')
        < script.index("security find-identity -v -p codesigning")
        < script.index("codesign --force")
    )
    assert "login.keychain-db" not in script and '"$SAVED_LIST"' in script


def test_rehearsal_workflow_shape() -> None:
    """Push to develop limited to the signing inputs, plus dispatch; never pull_request (public
    repo, self-hosted runner). Owner-only on hellmini, environment-scoped secrets, the shared
    script, loud failure on missing secrets, notarization on push runs, an always() cleanup."""
    text = REHEARSAL.read_text()
    spec = yaml.safe_load(text)
    triggers = spec.get("on", spec.get(True))
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"]["branches"] == ["develop"]
    paths = triggers["push"]["paths"]
    assert "service/deploy/mac-sign.sh" in paths
    assert all(
        p.startswith("service/deploy/") or p == ".github/workflows/mac-signing-rehearsal.yml" for p in paths
    ), "the push trigger must stay limited to the signing inputs"
    for name in ("Notarize", "Gatekeeper verdict (quarantined copy)"):
        step = next(s for s in spec["jobs"]["rehearse"]["steps"] if s.get("name") == name)
        assert "github.event_name == 'push'" in step["if"], f"{name} must run on push, where inputs.notarize is empty"
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
    """nexus-0n3vt: the mac-* arm of "Assert binary ABI floor" was `otool ... || true`, purely
    informational while every linux arm exits 1. Pin the enforced shape: it extracts minos,
    FAILS on an empty probe, and FAILS above the dated ceiling."""
    text = WORKFLOW.read_text()
    start = text.index("mac-*)")
    arm = _code(text[start:text.index(";;", start)])
    assert "|| true" not in arm, "the mac ABI arm regressed to informational-only"
    assert arm.count("exit 1") >= 2, "it must fail on an unparseable probe AND on a floor violation"
    assert "could not read LC_BUILD_VERSION" in arm
    assert re.search(r'ceiling="\d+\.\d+"', arm), "the mac ABI arm must carry an explicit numeric macOS ceiling"
