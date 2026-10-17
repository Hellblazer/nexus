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
  an integration test over the bundles committed in
  ``tests/daemon/fixtures/rdr161/`` (nexus-3lnxr): real sigstore, real
  Fulcio/Rekor material, the pinned issuer and identity, no mock, plus two
  one-byte-change negatives.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

# Direct import (NOT importorskip): the P1 publisher (nexus-ltjws), this TDD
# suite (nexus-epz8h), and the consumer (nexus-ywqza) land together on one
# branch behind one stacked review. Until ``binary_install`` exists this suite
# is TDD-RED by collection error — the intended signal. A module-wide skip
# would instead let CI stay green-by-skip if the consumer never lands, hiding
# the gap (feedback_no_silent_fallbacks_for_correctness).
from nexus.daemon import binary_install as binstall  # noqa: E402
from nexus.daemon.binary_install import well_known_binary_path  # noqa: E402

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
    sidecar.write_text(f"{digest}  {asset.name}\n", encoding="utf-8")
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
    sidecar.write_text("not-a-valid-sha256-line\n", encoding="utf-8")
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
    sidecar.write_text(f"{'0' * 64}  {asset.name}\n", encoding="utf-8")
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
    bundle.write_text("{}", encoding="utf-8")  # content irrelevant; the import fails first
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
    bundle.write_text('{"protobuf":"bundle"}', encoding="utf-8")
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
    bundle.write_text('{"protobuf":"bundle"}', encoding="utf-8")
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


# ── end-to-end real verify against a genuine published bundle ───────────────
#
# nexus-3lnxr. ``tests/daemon/fixtures/rdr161/`` holds the Sigstore bundles
# published beside a real ``engine-service-v*`` release (see provenance.json
# there for the tag and the pinned sha256 of each asset). The asset itself is
# not committed: the test downloads it once into the test-substrate cache,
# checks the pinned sha256, and runs the production ``verify_signature`` with
# the real sigstore checker, no seam. The 5 MB ``nexus-pg-<platform>.txz`` stands in
# for the 160 MB ``nexus-service`` binary; the same workflow signs both under
# the same identity. The release tag is pinned here and does NOT track
# ``REQUIRED_ENGINE_VERSION``, so an engine bump never invalidates the fixture.
#
# These tests share an integration job with unrelated PRs, and they touch two
# networks (the GitHub release download, sigstore's TUF trust-root refresh).
# Both are retried a bounded number of times on a TRANSPORT failure only, the
# way ``tests/db/_service_fixture.py`` retries the PG-bundle install
# (nexus-9rnfr, nexus-v460j). A verdict (bad digest, bad signature, wrong
# identity) is never retried, and each negative asserts the verdict's own
# message and its sigstore ``VerificationError`` cause, so an outage cannot
# satisfy it.

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "rdr161"
_PROVENANCE = json.loads((_FIXTURE_DIR / "provenance.json").read_text(encoding="utf-8"))


def _sleep(seconds: float) -> None:
    """Module-level so the offline retry tests can stub the backoff."""
    time.sleep(seconds)


def _retry_transient(fn, *, transient):
    """Call *fn*, retrying a ``BinaryVerificationError`` that *transient*
    accepts, up to the attempt count and backoff of the PG-bundle retry."""
    from tests.db._service_fixture import _TUF_RETRY_ATTEMPTS, _TUF_RETRY_BACKOFF_S

    for attempt in range(1, _TUF_RETRY_ATTEMPTS + 1):
        try:
            return fn()
        except binstall.BinaryVerificationError as exc:
            if not transient(exc) or attempt == _TUF_RETRY_ATTEMPTS:
                raise
            _sleep(_TUF_RETRY_BACKOFF_S * attempt)


def _download_is_transient(exc: Exception) -> bool:
    """A fetch failure that is not the release saying it lacks the asset."""
    return isinstance(exc, binstall.BinaryDownloadError) and not isinstance(
        exc, binstall.BinaryAssetAbsentError
    )


def _verify_is_transient(exc: Exception) -> bool:
    """A verify failure whose cause is the TUF refresh or the network, never a
    ``VerificationError`` verdict."""
    try:
        import sigstore.errors as sse
    except ImportError:
        return False
    return isinstance(
        exc.__cause__,
        (sse.TUFError, sse.NetworkError, sse.MetadataError, sse.RootError, OSError),
    )


def _verify_with_retry(asset: Path, bundle: Path, **kwargs) -> None:
    """Production ``verify_signature``, real sigstore, retried on a TUF or
    network failure only."""
    _retry_transient(
        lambda: binstall.verify_signature(asset, bundle, **kwargs),
        transient=_verify_is_transient,
    )


def _is_pinned(path: Path, entry: dict) -> bool:
    return (
        path.is_file()
        and path.stat().st_size == entry["size"]
        and binstall.compute_sha256(path) == entry["sha256"]
    )


def _materialize_asset(cache: Path, release_tag: str, entry: dict) -> Path:
    """The release asset in *cache*, sha256-pinned. A cached copy that fails
    its pin is deleted and downloaded once more; a download that still fails
    its pin fails the test."""
    name = entry["asset"]
    asset = cache / name
    if _is_pinned(asset, entry):
        return asset
    asset.unlink(missing_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    # Per-process temp name + atomic replace: xdist workers and parallel
    # jobs sharing one cache never see a half-written asset.
    part = cache / f"{name}.{os.getpid()}.part"
    try:
        _retry_transient(
            lambda: binstall._download(binstall.release_asset_url(release_tag, name), part),
            transient=_download_is_transient,
        )
        os.replace(part, asset)
    except binstall.BinaryVerificationError as exc:
        pytest.fail(f"cannot arm the rdr161 fixture: {exc}")
    finally:
        part.unlink(missing_ok=True)
    assert _is_pinned(asset, entry), f"{asset} does not match the sha256 pinned in provenance.json"
    return asset


def _real_asset_and_bundle() -> tuple[Path, Path]:
    """The host's genuine release asset (downloaded, sha256-pinned) and its
    committed bundle. Fails, never skips, when the fixture cannot be armed:
    a skip here is the silent disarm this test exists to prevent."""
    from nexus.db.pg_bundle import current_platform_tag
    from tests.db._service_fixture import substrate_cache_root

    plat = current_platform_tag()
    entry = _PROVENANCE["assets"].get(plat)
    if entry is None:
        pytest.skip(
            f"no rdr161 fixture for platform {plat!r} (release {_PROVENANCE['release_tag']} "
            f"publishes {sorted(_PROVENANCE['assets'])}); mac-x64 has no engine artifact"
        )
    bundle = _FIXTURE_DIR / f"{entry['asset']}.sigstore.json"
    assert bundle.is_file(), f"committed fixture missing: {bundle}"
    cache = substrate_cache_root() / "rdr161-fixture" / _PROVENANCE["release_tag"]
    return _materialize_asset(cache, _PROVENANCE["release_tag"], entry), bundle


def _assert_verdict(exc_info, message: str) -> None:
    """The rejection is sigstore's verdict (not a transport failure) and says *message*."""
    import sigstore.errors as sse

    assert message in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, sse.VerificationError), (
        f"rejected for a reason other than a verdict: {exc_info.value.__cause__!r}"
    )


@pytest.mark.integration
def test_verify_signature_real_bundle_end_to_end():
    asset, bundle = _real_asset_and_bundle()
    _verify_with_retry(asset, bundle)  # real sigstore verify, no mock


@pytest.mark.integration
def test_verify_signature_real_bundle_rejects_one_changed_asset_byte(tmp_path):
    asset, bundle = _real_asset_and_bundle()
    data = bytearray(asset.read_bytes())
    data[len(data) // 2] ^= 0x01
    tampered = tmp_path / asset.name
    tampered.write_bytes(bytes(data))
    with pytest.raises(binstall.BinaryVerificationError, match="digest mismatch") as ei:
        _verify_with_retry(tampered, bundle)
    _assert_verdict(ei, "digest mismatch")


@pytest.mark.integration
def test_verify_signature_real_bundle_rejects_one_changed_signature_byte(tmp_path):
    asset, bundle = _real_asset_and_bundle()
    doc = json.loads(bundle.read_text(encoding="utf-8"))
    sig = bytearray(base64.b64decode(doc["messageSignature"]["signature"]))
    sig[0] ^= 0x01
    doc["messageSignature"]["signature"] = base64.b64encode(bytes(sig)).decode("ascii")
    bad_bundle = tmp_path / bundle.name
    bad_bundle.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(binstall.BinaryVerificationError, match="Signature is invalid") as ei:
        _verify_with_retry(asset, bad_bundle)
    _assert_verdict(ei, "Signature is invalid")


@pytest.mark.integration
def test_verify_signature_real_bundle_rejects_a_wrong_identity_regexp():
    asset, bundle = _real_asset_and_bundle()
    with pytest.raises(
        binstall.BinaryVerificationError,
        match="does not match the pinned release identity",
    ) as ei:
        _verify_with_retry(asset, bundle, identity_regexp=r"https://github\.com/Other/repo/.*")
    _assert_verdict(ei, "does not match the pinned release identity")


@pytest.mark.integration
def test_verify_signature_real_bundle_rejects_a_wrong_issuer():
    asset, bundle = _real_asset_and_bundle()
    with pytest.raises(
        binstall.BinaryVerificationError, match=r"0 of 2 policies succeeded"
    ) as ei:
        _verify_with_retry(asset, bundle, issuer="https://accounts.google.com")
    _assert_verdict(ei, "0 of 2 policies succeeded")


# ── the fixture's own retry and cache handling (offline) ────────────────────


def _fake_entry(payload: bytes) -> dict:
    return {
        "asset": "nexus-pg-fake.txz",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }


@pytest.fixture
def _no_backoff(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(sys.modules[__name__], "_sleep", sleeps.append)
    return sleeps


def test_fixture_download_recovers_from_one_transient_failure(tmp_path, monkeypatch, _no_backoff):
    payload = b"release-asset-bytes"
    calls: list[str] = []

    def flaky(url, dest, **_):
        calls.append(url)
        if len(calls) == 1:
            raise binstall.BinaryDownloadError("connection reset")
        dest.write_bytes(payload)

    monkeypatch.setattr(binstall, "_download", flaky)
    asset = _materialize_asset(tmp_path, "engine-service-v0.0.0", _fake_entry(payload))
    assert asset.read_bytes() == payload
    assert len(calls) == 2
    assert len(_no_backoff) == 1


def test_fixture_download_does_not_retry_an_absent_asset(tmp_path, monkeypatch, _no_backoff):
    calls: list[str] = []

    def absent(url, dest, **_):
        calls.append(url)
        raise binstall.BinaryAssetAbsentError("HTTP 404")

    monkeypatch.setattr(binstall, "_download", absent)
    with pytest.raises(pytest.fail.Exception, match="cannot arm the rdr161 fixture"):
        _materialize_asset(tmp_path, "engine-service-v0.0.0", _fake_entry(b"x"))
    assert len(calls) == 1
    assert _no_backoff == []


def test_fixture_download_gives_up_after_the_bounded_attempts(tmp_path, monkeypatch, _no_backoff):
    from tests.db._service_fixture import _TUF_RETRY_ATTEMPTS

    calls: list[str] = []

    def down(url, dest, **_):
        calls.append(url)
        raise binstall.BinaryDownloadError("network is unreachable")

    monkeypatch.setattr(binstall, "_download", down)
    with pytest.raises(pytest.fail.Exception, match="cannot arm the rdr161 fixture"):
        _materialize_asset(tmp_path, "engine-service-v0.0.0", _fake_entry(b"x"))
    assert len(calls) == _TUF_RETRY_ATTEMPTS


def test_fixture_replaces_a_cached_asset_that_fails_its_pin(tmp_path, monkeypatch, _no_backoff):
    payload = b"the-real-release-bytes"
    entry = _fake_entry(payload)
    (tmp_path / entry["asset"]).write_bytes(b"x" * len(payload))  # right size, wrong bytes
    calls: list[str] = []

    def good(url, dest, **_):
        calls.append(url)
        dest.write_bytes(payload)

    monkeypatch.setattr(binstall, "_download", good)
    asset = _materialize_asset(tmp_path, "engine-service-v0.0.0", entry)
    assert asset.read_bytes() == payload
    assert len(calls) == 1


def test_fixture_fails_when_a_fresh_download_still_fails_its_pin(tmp_path, monkeypatch, _no_backoff):
    entry = _fake_entry(b"expected")
    calls: list[str] = []

    def wrong(url, dest, **_):
        calls.append(url)
        dest.write_bytes(b"unexpect")  # same size, wrong digest

    monkeypatch.setattr(binstall, "_download", wrong)
    with pytest.raises(AssertionError, match="does not match the sha256 pinned"):
        _materialize_asset(tmp_path, "engine-service-v0.0.0", entry)
    assert len(calls) == 1


@pytest.mark.skipif(not _SIGSTORE_INSTALLED, reason="sigstore not installed")
def test_verify_retry_retries_a_tuf_failure_and_never_a_verdict(monkeypatch, _no_backoff):
    import sigstore.errors as sse

    def wrapped(cause):
        err = binstall.BinaryVerificationError("signature verification failed")
        err.__cause__ = cause
        return err

    outcomes = [wrapped(sse.TUFError("refresh failed")), None]

    def verify(asset, bundle, **_):
        out = outcomes.pop(0)
        if out is not None:
            raise out

    monkeypatch.setattr(binstall, "verify_signature", verify)
    _verify_with_retry(Path("a"), Path("b"))
    assert outcomes == [] and len(_no_backoff) == 1

    verdicts: list[int] = []

    def reject(asset, bundle, **_):
        verdicts.append(1)
        raise wrapped(sse.VerificationError("Signature is invalid for input"))

    monkeypatch.setattr(binstall, "verify_signature", reject)
    with pytest.raises(binstall.BinaryVerificationError):
        _verify_with_retry(Path("a"), Path("b"))
    assert verdicts == [1]


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


def test_install_binary_gate_warns_unverified_on_probe_error(tmp_path, monkeypatch):
    """nexus-pgdcv round-2 HIGH-1: an unverifiable store must never block a
    legitimate install (install-binary IS the recovery tool for the
    will-not-boot class) — but it must never proceed SILENTLY either. The
    old gate's silent fail-open was the same bug class converge_engine's
    gate had."""
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
    assert "UNVERIFIED" in result.output
    assert "psql unreachable" in result.output
    assert "nx doctor" in result.output  # the verify-after step is named


def test_install_binary_gate_warns_unverified_on_fatal_health_result(
    tmp_path, monkeypatch,
):
    """PG unreachable → _check_migration_state early-returns a fatal result
    and the chash leg never runs. The gate must classify that UNKNOWN
    (warn + proceed), never silent-clean — the exact GH #1414 ordering."""
    from click.testing import CliRunner
    import nexus.health as _health
    from nexus.commands.daemon import service_install_binary_cmd
    from nexus.health import HealthResult

    fatal = HealthResult(
        label="Schema migrations",
        ok=False,
        detail="Cannot query databasechangelog (psql exit 2): connection refused",
        fatal=True,
    )
    monkeypatch.setattr(binstall, "install_binary", _fake_install_ok)
    monkeypatch.setattr(_health, "_check_migration_state", lambda **kw: [fatal])

    result = CliRunner().invoke(
        service_install_binary_cmd,
        ["engine-service-v0.1.3", "--no-pg-bundle", "--config-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "UNVERIFIED" in result.output
    assert "Cannot query databasechangelog" in result.output


class TestVerifyInstalledBinary:
    """nexus-8eaeg: "is the installed engine the one its receipt describes?"
    must be answerable from LOCAL bytes only.

    The bug this closes had two halves that share one cause — convergence was
    decided from the provenance sidecar ALONE. Reading the receipt as gospel
    made a box with a missing/corrupt engine look green forever; and because
    the receipt could not be *checked*, the only way the code knew how to
    re-establish trust was to re-download the ~190 MB asset. Verification
    against the receipt's own recorded digest is what makes both go away
    without ever opening a socket.
    """

    def _install(self, tmp_path, payload: bytes, *, sha: str | None = None):
        import json

        svc = tmp_path / "service"
        svc.mkdir(parents=True, exist_ok=True)
        # The platform's own name for the engine (``nexus-service.exe`` on Windows).
        binary = well_known_binary_path(tmp_path)
        binary.write_bytes(payload)
        digest = sha if sha is not None else hashlib.sha256(payload).hexdigest()
        (svc / "nexus-service.meta.json").write_text(
            json.dumps({"version": "0.1.60", "tag": "engine-service-v0.1.60",
                        "sha256": digest}), encoding="utf-8"
        )
        return binary

    def test_matching_digest_is_ok(self, tmp_path):
        binary = self._install(tmp_path, b"engine-bytes")
        verdict = binstall.verify_installed_binary(tmp_path)
        assert verdict.ok is True
        assert verdict.reason is None
        assert verdict.path == binary

    def test_missing_binary_is_not_ok(self, tmp_path):
        binary = self._install(tmp_path, b"engine-bytes")
        binary.unlink()
        verdict = binstall.verify_installed_binary(tmp_path)
        assert verdict.ok is False
        assert "missing" in verdict.reason

    def test_corrupt_binary_is_not_ok(self, tmp_path):
        binary = self._install(tmp_path, b"engine-bytes")
        binary.write_bytes(b"tampered-bytes")
        verdict = binstall.verify_installed_binary(tmp_path)
        assert verdict.ok is False
        assert "does not match its receipt" in verdict.reason

    def test_absent_receipt_is_not_ok(self, tmp_path):
        verdict = binstall.verify_installed_binary(tmp_path)
        assert verdict.ok is False
        assert "no install receipt" in verdict.reason

    def test_receipt_without_sha_is_not_ok_never_assumed_fine(self, tmp_path):
        """FAIL LOUD: a receipt that records no digest cannot vouch for
        anything. It must NOT degrade to 'probably fine'."""
        import json

        svc = tmp_path / "service"
        svc.mkdir(parents=True)
        (svc / "nexus-service").write_bytes(b"engine-bytes")
        (svc / "nexus-service.meta.json").write_text(json.dumps({"version": "0.1.60"}), encoding="utf-8")
        verdict = binstall.verify_installed_binary(tmp_path)
        assert verdict.ok is False
        assert "no usable sha256" in verdict.reason

    def test_verification_opens_no_network(self, tmp_path, monkeypatch):
        """The whole point: re-establishing trust in the installed binary is
        a LOCAL operation. Any download here would be the defect returning."""
        def _boom(*a, **kw):
            raise AssertionError("verify_installed_binary must not download")

        monkeypatch.setattr(binstall, "_download", _boom)
        self._install(tmp_path, b"engine-bytes")
        assert binstall.verify_installed_binary(tmp_path).ok is True
