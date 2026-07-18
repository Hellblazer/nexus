# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-161 P1 (TDD): verification suite for ``nx daemon service install-binary``.

Tests are written BEFORE the consumer implementation (bead nexus-epz8h precedes
nexus-ywqza). Until ``nexus.daemon.binary_install`` lands they fail at import;
that is the intended TDD-RED state.

The verification contract (RF-161-1) the install path MUST honour:

1. **sha256 gate** — the downloaded asset's sha256 must equal the published
   ``<asset>.sha256`` sidecar. Mismatch / malformed / missing → fail closed.
2. **signature gate** — the published ``<asset>.sigstore.json`` (new protobuf
   bundle, emitted by the publisher half nexus-ltjws) is verified with
   sigstore-python. The OIDC issuer is pinned exactly to GitHub Actions and the
   signing-cert identity is matched against the literal RF-161-1 regexp. Crypto
   failure, identity mismatch, issuer mismatch, missing bundle, missing
   sigstore dependency, or no network all → fail closed. Verification NEVER
   silently skips (feedback_no_silent_fallbacks_for_correctness).

What is REAL here vs seam-injected, and why (settled via the fail-closed
matrix, T2 nexus_rdr/161-research):

* sha256 + identity-regexp + the pinned constants + the missing-dependency
  message are tested with real fixtures and real regex — no mocks.
* the cryptographic verify itself is sigstore-python's responsibility, not
  ours to re-test. Our wiring (forwarding the pinned issuer + identity regexp,
  and wrapping every failure as a fail-closed error) is tested by injecting a
  fake checker through the ``checker`` seam (constructor injection — project
  style). This lets the fail-closed/pass wiring be covered WITHOUT sigstore
  installed and WITHOUT a real release artifact.
* an end-to-end real verify against a genuine published ``.sigstore.json`` is
  deferred to an integration test (skip-with-reason) because no real
  ``engine-service-v*`` release artifact exists yet (RDR-155 P4b freeze
  window). The gap is declared out loud rather than papered over with a mock
  pretending to be a real verify.
"""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

# Direct import (NOT importorskip): the P1 publisher (nexus-ltjws), this TDD
# suite (nexus-epz8h), and the consumer (nexus-ywqza) land together on one
# branch behind one stacked review. Until ``binary_install`` exists this suite
# is TDD-RED by collection error — the intended signal. A module-wide skip
# would instead let CI stay green-by-skip if the consumer never lands, hiding
# the gap (feedback_no_silent_fallbacks_for_correctness).
from nexus.daemon import binary_install as binstall  # noqa: E402

_SIGSTORE_INSTALLED = importlib.util.find_spec("sigstore") is not None


# ── fixtures ──────────────────────────────────────────────────────────────


def _write_asset(tmp_path: Path, content: bytes = b"native-binary-bytes\n") -> Path:
    asset = tmp_path / "nexus-service-linux-amd64"
    asset.write_bytes(content)
    return asset


def _sha256_sidecar(asset: Path) -> Path:
    """A well-formed ``<hex>  <filename>`` sidecar matching *asset*, as the
    publisher's ``sha256sum`` / ``shasum -a 256`` emits it (two-space sep)."""
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    sidecar = asset.with_suffix(asset.suffix + ".sha256")
    sidecar.write_text(f"{digest}  {asset.name}\n")
    return sidecar


# ── pinned constants (RF-161-1 literal pin — lock as ==) ────────────────────


def test_oidc_issuer_pinned_exactly():
    assert binstall.CERT_OIDC_ISSUER == "https://token.actions.githubusercontent.com"


def test_identity_regexp_is_the_rf161_1_pin():
    # The literal pin from RF-161-1. Anchored to Hellblazer/nexus, the exact
    # workflow file, the engine-service-v* tag namespace, numeric version only.
    assert binstall.CERT_IDENTITY_REGEXP == (
        r"https://github\.com/Hellblazer/nexus/\.github/workflows/"
        r"engine-service-release\.yml@refs/tags/engine-service-v[0-9].*"
    )


# ── sha256 gate (fully real) ────────────────────────────────────────────────


def test_compute_sha256_matches_hashlib(tmp_path):
    asset = _write_asset(tmp_path)
    assert binstall.compute_sha256(asset) == hashlib.sha256(asset.read_bytes()).hexdigest()


def test_verify_sha256_accepts_matching_sidecar(tmp_path):
    asset = _write_asset(tmp_path)
    sidecar = _sha256_sidecar(asset)
    # returns the verified hex digest, does not raise
    assert binstall.verify_sha256(asset, sidecar) == hashlib.sha256(
        asset.read_bytes()
    ).hexdigest()


def test_verify_sha256_rejects_tampered_asset(tmp_path):
    asset = _write_asset(tmp_path, b"original\n")
    sidecar = _sha256_sidecar(asset)
    asset.write_bytes(b"tampered\n")  # change bytes after the sidecar was written
    with pytest.raises(binstall.BinaryVerificationError):
        binstall.verify_sha256(asset, sidecar)


def test_verify_sha256_rejects_malformed_sidecar(tmp_path):
    asset = _write_asset(tmp_path)
    sidecar = asset.with_suffix(asset.suffix + ".sha256")
    sidecar.write_text("not-a-valid-sha256-line\n")
    with pytest.raises(binstall.BinaryVerificationError):
        binstall.verify_sha256(asset, sidecar)


def test_verify_sha256_rejects_missing_sidecar(tmp_path):
    asset = _write_asset(tmp_path)
    sidecar = asset.with_suffix(asset.suffix + ".sha256")  # never created
    with pytest.raises(binstall.BinaryVerificationError):
        binstall.verify_sha256(asset, sidecar)


def test_verify_sha256_rejects_missing_asset(tmp_path):
    asset = tmp_path / "nexus-service-linux-amd64"  # never created
    sidecar = tmp_path / "nexus-service-linux-amd64.sha256"
    sidecar.write_text(f"{'0' * 64}  {asset.name}\n")
    with pytest.raises(binstall.BinaryVerificationError):
        binstall.verify_sha256(asset, sidecar)


# ── cert-identity regexp (fully real, the literal RF-161-1 pin) ─────────────

_VALID_TAG_SAN = (
    "https://github.com/Hellblazer/nexus/.github/workflows/"
    "engine-service-release.yml@refs/tags/engine-service-v0.1.3"
)


def test_identity_matches_valid_release_tag():
    assert binstall.identity_matches(_VALID_TAG_SAN) is True


@pytest.mark.parametrize(
    "san",
    [
        # a fork of the repo
        "https://github.com/attacker/nexus/.github/workflows/"
        "engine-service-release.yml@refs/tags/engine-service-v0.1.3",
        # a branch ref instead of a tag ref
        "https://github.com/Hellblazer/nexus/.github/workflows/"
        "engine-service-release.yml@refs/heads/main",
        # a different workflow file in the same repo
        "https://github.com/Hellblazer/nexus/.github/workflows/"
        "release.yml@refs/tags/engine-service-v0.1.3",
        # the PyPI tag namespace, not engine-service-v*
        "https://github.com/Hellblazer/nexus/.github/workflows/"
        "engine-service-release.yml@refs/tags/v0.1.3",
        # non-numeric version segment (the pin requires v[0-9])
        "https://github.com/Hellblazer/nexus/.github/workflows/"
        "engine-service-release.yml@refs/tags/engine-service-vX",
        "",
        # trailing junk after a newline — fullmatch must reject (`.*` does not
        # cross \n), where re.match would have accepted the leading prefix.
        _VALID_TAG_SAN + "\nevil",
    ],
)
def test_identity_rejects_non_release_identities(san):
    assert binstall.identity_matches(san) is False


# ── tag-namespace guard (fully real) ────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_tag",
    ["v0.1.3", "latest", "engine-service-0.1.3", "0.1.3", "release-v0.1.3", ""],
)
def test_validate_tag_rejects_non_namespace(bad_tag, tmp_path):
    # _validate_tag fires before any network access — install_binary must fail
    # closed on a tag outside the engine-service-v* namespace.
    with pytest.raises(binstall.BinaryVerificationError):
        binstall.install_binary(bad_tag, tmp_path)


def test_validate_tag_accepts_namespace_then_reaches_download(tmp_path, monkeypatch):
    # A well-formed tag passes the namespace guard and proceeds to the download
    # step. Stub _download (no network) to fail closed; assert it was reached
    # and that no binary is placed.
    seen = {"called": False}

    def _stub_download(url, dest, *, timeout=0):
        seen["called"] = True
        raise binstall.BinaryVerificationError("simulated download failure")

    monkeypatch.setattr(binstall, "_download", _stub_download)
    with pytest.raises(binstall.BinaryVerificationError):
        binstall.install_binary(
            "engine-service-v0.1.3", tmp_path, download_dir=tmp_path
        )
    assert seen["called"] is True  # namespace guard passed, download attempted
    assert not (tmp_path / "service" / "nexus-service").exists()


# ── signature gate: missing sigstore dependency (real) ──────────────────────


@pytest.mark.skipif(
    _SIGSTORE_INSTALLED,
    reason="sigstore is installed; the missing-dependency path is unreachable here",
)
def test_verify_signature_missing_dependency_is_actionable(tmp_path):
    asset = _write_asset(tmp_path)
    bundle = asset.with_suffix(asset.suffix + ".sigstore.json")
    bundle.write_text("{}")  # content irrelevant; the import fails first
    with pytest.raises(binstall.BinaryVerificationError) as exc:
        binstall.verify_signature(asset, bundle)
    msg = str(exc.value).lower()
    # Actionable: names the package, NOT a bare ModuleNotFoundError, and does
    # not suggest skipping verification.
    assert "sigstore" in msg
    assert "skip" not in msg


# ── signature gate: wiring, via the injected checker seam ───────────────────


class _FakeChecker:
    """Stands in for the sigstore-backed checker. Records what the install
    path forwarded, and either passes or raises on demand."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self._raises = raises
        self.calls: list[dict] = []

    def check(self, *, asset_bytes, bundle_bytes, identity_regexp, issuer) -> None:
        self.calls.append(
            {
                "asset_bytes": asset_bytes,
                "bundle_bytes": bundle_bytes,
                "identity_regexp": identity_regexp,
                "issuer": issuer,
            }
        )
        if self._raises is not None:
            raise self._raises


def test_verify_signature_forwards_pinned_issuer_and_identity(tmp_path):
    asset = _write_asset(tmp_path)
    bundle = asset.with_suffix(asset.suffix + ".sigstore.json")
    bundle.write_text('{"protobuf":"bundle"}')
    checker = _FakeChecker()
    binstall.verify_signature(asset, bundle, checker=checker)
    assert len(checker.calls) == 1
    call = checker.calls[0]
    # The pins are forwarded verbatim — not silently relaxed.
    assert call["identity_regexp"] == binstall.CERT_IDENTITY_REGEXP
    assert call["issuer"] == binstall.CERT_OIDC_ISSUER
    assert call["asset_bytes"] == asset.read_bytes()


def test_verify_signature_fails_closed_when_checker_raises(tmp_path):
    asset = _write_asset(tmp_path)
    bundle = asset.with_suffix(asset.suffix + ".sigstore.json")
    bundle.write_text('{"protobuf":"bundle"}')
    checker = _FakeChecker(raises=ValueError("signature does not verify"))
    with pytest.raises(binstall.BinaryVerificationError):
        binstall.verify_signature(asset, bundle, checker=checker)


def test_verify_signature_fails_closed_on_missing_bundle(tmp_path):
    asset = _write_asset(tmp_path)
    bundle = asset.with_suffix(asset.suffix + ".sigstore.json")  # never created
    checker = _FakeChecker()
    with pytest.raises(binstall.BinaryVerificationError):
        binstall.verify_signature(asset, bundle, checker=checker)
    # Fail-closed BEFORE the checker is consulted: a missing bundle is rejected
    # outright, never treated as "nothing to verify".
    assert checker.calls == []


# ── CLI command: nx daemon service install-binary (isolated) ────────────────


def test_cli_install_binary_happy_path(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from nexus.commands.daemon import service_install_binary_cmd

    def _fake_install(tag, config_dir, *, installed_by=""):
        return config_dir / "service" / "nexus-service", {
            "asset": "nexus-service-linux-amd64",
            "version": "0.1.3",
            "sha256": "a" * 64,
            "source_url": "https://example/nexus-service-linux-amd64",
        }

    monkeypatch.setattr(binstall, "install_binary", _fake_install)
    # --no-pg-bundle: this test is scoped to the binary path (the PG-bundle
    # default is covered in tests/daemon/test_pg_bundle_install.py).
    result = CliRunner().invoke(
        service_install_binary_cmd,
        ["engine-service-v0.1.3", "--no-pg-bundle", "--config-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "verified" in result.output.lower()


def test_cli_install_binary_fails_closed_exit_2(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from nexus.commands.daemon import service_install_binary_cmd

    def _fake_install(tag, config_dir, *, installed_by=""):
        raise binstall.BinaryVerificationError("sha256 mismatch")

    monkeypatch.setattr(binstall, "install_binary", _fake_install)
    result = CliRunner().invoke(
        service_install_binary_cmd,
        ["engine-service-v0.1.3", "--config-dir", str(tmp_path)],
    )
    assert result.exit_code == 2
    assert "sha256 mismatch" in result.output


# ── end-to-end real verify (deferred, declared out loud) ────────────────────

_REAL_FIXTURE = (
    Path(__file__).parent / "fixtures" / "rdr161" / "nexus-service-linux-amd64.sigstore.json"
)


@pytest.mark.integration
@pytest.mark.skipif(
    not (_SIGSTORE_INSTALLED and _REAL_FIXTURE.is_file()),
    reason=(
        "no genuine engine-service-v* .sigstore.json fixture exists yet "
        "(produced by the publisher nexus-ltjws at a real release; blocked by "
        "the RDR-155 P4b freeze window). Capture one from the first signed "
        "release and drop it at tests/daemon/fixtures/rdr161/ to arm this."
    ),
)
def test_verify_signature_real_bundle_end_to_end():
    asset = _REAL_FIXTURE.with_suffix("")  # the binary sits beside the bundle
    binstall.verify_signature(asset, _REAL_FIXTURE)  # real sigstore verify, no mock


# ── nexus-pnwu0 / GH #1390: chash-poison upgrade gate ───────────────────────


def _poison_result():
    from nexus.db.chash_tables import POISON_DETAIL_TOKEN
    from nexus.health import HealthResult
    return HealthResult(
        label="Chunk chash conformance",
        ok=False,
        detail=(
            f"12 chunk row(s) have a {POISON_DETAIL_TOKEN} "
            "(legacy pre-RDR-108 ids)."
        ),
        warn=True,
    )


def _fake_install_ok(tag, config_dir, *, installed_by=""):
    return config_dir / "service" / "nexus-service", {
        "asset": "nexus-service-linux-amd64", "version": "0.1.3",
        "sha256": "a" * 64, "source_url": "https://example/x",
    }


def test_install_binary_refuses_on_chash_poison(tmp_path, monkeypatch):
    from click.testing import CliRunner
    import nexus.health as _health
    from nexus.commands.daemon import service_install_binary_cmd

    monkeypatch.setattr(binstall, "install_binary", _fake_install_ok)
    monkeypatch.setattr(_health, "_check_migration_state", lambda **kw: [_poison_result()])

    result = CliRunner().invoke(
        service_install_binary_cmd,
        ["engine-service-v0.1.3", "--no-pg-bundle", "--config-dir", str(tmp_path)],
    )
    assert result.exit_code == 3, result.output
    assert "Refusing to install" in result.output
    # full clickable URL + a paste-to-Claude prompt are both present
    assert "https://github.com/Hellblazer/nexus/blob/main/docs/migration-runbook.md" in result.output
    assert "paste this to your Claude" in result.output
    assert "Do NOT drop the chash length constraints" in result.output


def test_install_binary_force_overrides_poison_gate(tmp_path, monkeypatch):
    from click.testing import CliRunner
    import nexus.health as _health
    from nexus.commands.daemon import service_install_binary_cmd

    monkeypatch.setattr(binstall, "install_binary", _fake_install_ok)
    monkeypatch.setattr(_health, "_check_migration_state", lambda **kw: [_poison_result()])

    result = CliRunner().invoke(
        service_install_binary_cmd,
        ["engine-service-v0.1.3", "--no-pg-bundle", "--force", "--config-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "--force overrides" in result.output
    assert "verified" in result.output.lower()  # install proceeded


def test_install_binary_gate_skips_on_probe_error(tmp_path, monkeypatch):
    """A probe that raises must never block a legitimate install."""
    from click.testing import CliRunner
    import nexus.health as _health
    from nexus.commands.daemon import service_install_binary_cmd

    def _boom(**kw):
        raise RuntimeError("psql unreachable")

    monkeypatch.setattr(binstall, "install_binary", _fake_install_ok)
    monkeypatch.setattr(_health, "_check_migration_state", _boom)

    result = CliRunner().invoke(
        service_install_binary_cmd,
        ["engine-service-v0.1.3", "--no-pg-bundle", "--config-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "pre-check skipped" in result.output
