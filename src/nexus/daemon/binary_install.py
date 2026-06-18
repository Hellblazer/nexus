# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-161 P1: acquire + verify the signed native ``nexus-service`` binary.

``nx daemon service install-binary TAG`` downloads the per-platform native
binary published by the ``engine-service-release.yml`` workflow, verifies it,
and places it at the well-known location the supervisor execs.

Two independent gates, both fail-closed (RF-161-1, fail-closed matrix in T2
``nexus_rdr/161-research``):

1. **sha256** — the asset's digest must equal the published ``<asset>.sha256``
   sidecar. Catches a corrupt or truncated download before anything else.
2. **signature** — the published ``<asset>.sigstore.json`` (new protobuf
   bundle, emitted by the publisher half nexus-ltjws) is verified with
   sigstore-python, with the OIDC issuer pinned exactly to GitHub Actions and
   the signing-certificate identity matched against the RF-161-1 regexp.

Verification NEVER silently skips. A missing bundle, a bad signature, an
identity that does not match the pin, an unreachable transparency log, or an
absent ``sigstore`` package all raise :class:`BinaryVerificationError` and the
binary is not installed (feedback_no_silent_fallbacks_for_correctness).

This module deliberately consumes NO cosign binary (~130MB/platform); the new
protobuf bundle is verifiable offline by the pure-Python ``sigstore`` package.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import structlog

from nexus.daemon.binary_lifecycle import well_known_binary_path
from nexus.db.pg_bundle import current_platform_tag

_log = structlog.get_logger(__name__)

__all__ = [
    "BinaryVerificationError",
    "CERT_IDENTITY_REGEXP",
    "CERT_OIDC_ISSUER",
    "TAG_NAMESPACE_PREFIX",
    "asset_name",
    "binary_sidecar_path",
    "compute_sha256",
    "identity_matches",
    "install_binary",
    "install_pg_bundle",
    "pg_bundle_asset_name",
    "pg_bundle_dest",
    "release_asset_url",
    "resolve_service_tag",
    "verify_sha256",
    "verify_signature",
    "PINNED_SERVICE_TAG",
    "SERVICE_TAG_ENV",
]

#: Exact OIDC issuer for GitHub Actions keyless signing (RF-161-1 pin).
CERT_OIDC_ISSUER = "https://token.actions.githubusercontent.com"

#: Signing-certificate identity pin (RF-161-1). Anchored to this repo, the exact
#: release workflow file, and the ``engine-service-v<numeric>`` tag namespace.
#: ``Identity`` in sigstore-python is exact-match only, so identity is matched
#: against this regexp by :class:`_RegexpIdentityPolicy` instead.
CERT_IDENTITY_REGEXP = (
    r"https://github\.com/Hellblazer/nexus/\.github/workflows/"
    r"engine-service-release\.yml@refs/tags/engine-service-v[0-9].*"
)

#: The native-binary release tag namespace. Phase 1 requires an EXPLICIT tag in
#: this namespace — no "latest" resolution (RF-161-2). Kept as a constant so the
#: future "latest" helper filters on the same prefix.
TAG_NAMESPACE_PREFIX = "engine-service-v"

#: The ``engine-service-v*`` tag this conexus build is compatible with. A
#: BUILD-TIME PIN (bumped per release as the engine-service version advances),
#: NOT a "latest" lookup — it avoids silent schema drift against the running
#: service (RF-161-2). ``None`` until the first real ``engine-service-v*``
#: release exists; while it is ``None``, ``nx init --service`` cannot
#: auto-install and instructs the user to pass an explicit tag.
PINNED_SERVICE_TAG: str | None = None

#: Env override for the service tag (operator / CI). Takes precedence over the
#: build-time pin. Still an explicit tag — no "latest" semantics.
SERVICE_TAG_ENV = "NEXUS_SERVICE_TAG"

_REPO = "Hellblazer/nexus"
_RELEASE_DOWNLOAD_BASE = f"https://github.com/{_REPO}/releases/download"
_BINARY_SIDECAR_NAME = "nexus-service.meta.json"
_PG_SIDECAR_NAME = "nexus-pg.meta.json"
_DOWNLOAD_TIMEOUT_S = 120.0
_HASH_BLOCK = 1 << 20


class BinaryVerificationError(Exception):
    """A verification gate failed. The binary must not be installed."""


# ── sha256 gate ─────────────────────────────────────────────────────────────


def compute_sha256(path: Path) -> str:
    """Streaming sha256 hex digest of *path*."""
    sha = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(_HASH_BLOCK), b""):
            sha.update(block)
    return sha.hexdigest()


# A ``sha256sum`` / ``shasum -a 256`` line: 64 hex chars, separator, filename.
_SHA256_LINE_RE = re.compile(r"^([0-9a-fA-F]{64})\b")


def verify_sha256(asset_path: Path, sha256_sidecar: Path) -> str:
    """Verify *asset_path* against the published ``<asset>.sha256`` sidecar.

    Returns the verified lowercase hex digest. Raises
    :class:`BinaryVerificationError` (fail closed) when the asset is missing,
    the sidecar is missing or malformed, or the digests disagree.
    """
    if not asset_path.is_file():
        raise BinaryVerificationError(
            f"asset {asset_path} is missing; cannot verify its sha256"
        )
    if not sha256_sidecar.is_file():
        raise BinaryVerificationError(
            f"sha256 sidecar {sha256_sidecar} is missing; refusing to install "
            "an unverified binary"
        )
    raw = sha256_sidecar.read_text(errors="replace").strip()
    m = _SHA256_LINE_RE.match(raw)
    if not m:
        raise BinaryVerificationError(
            f"sha256 sidecar {sha256_sidecar} is malformed "
            f"(expected '<64-hex>  <filename>', got {raw[:80]!r})"
        )
    expected = m.group(1).lower()
    actual = compute_sha256(asset_path)
    if actual != expected:
        raise BinaryVerificationError(
            f"sha256 mismatch for {asset_path.name}: published {expected}, "
            f"computed {actual}. The download is corrupt or tampered; not "
            "installing."
        )
    return actual


# ── certificate-identity regexp gate ────────────────────────────────────────

_IDENTITY_RE = re.compile(CERT_IDENTITY_REGEXP)


def identity_matches(san: str, *, pattern: str = CERT_IDENTITY_REGEXP) -> bool:
    """True when a signing-cert SAN matches the pinned release identity.

    Whole-string match (``re.fullmatch``): the pin begins at ``https://github.com``
    so a forked repo, a branch ref, a different workflow file, the PyPI ``v*``
    tag namespace, or a non-numeric version segment all fail. ``fullmatch`` (vs
    ``match``) also rejects a SAN with trailing junk after a newline — ``.*``
    does not cross ``\\n`` — closing a defense-in-depth gap even though the SAN
    is GitHub-OIDC-controlled, not attacker-controlled.
    """
    compiled = _IDENTITY_RE if pattern == CERT_IDENTITY_REGEXP else re.compile(pattern)
    return compiled.fullmatch(san) is not None


# ── signature gate ──────────────────────────────────────────────────────────


class _SignatureChecker(Protocol):
    """Seam for the cryptographic verify (constructor-injected in tests)."""

    def check(
        self, *, asset_bytes: bytes, bundle_bytes: bytes, identity_regexp: str, issuer: str
    ) -> None:
        """Raise on any verification failure; return ``None`` on success."""


class _SigstoreChecker:
    """Default checker: verifies the protobuf bundle with sigstore-python,
    offline, pinning the issuer (exact) and the identity (regexp)."""

    def check(
        self, *, asset_bytes: bytes, bundle_bytes: bytes, identity_regexp: str, issuer: str
    ) -> None:
        try:
            from sigstore.models import Bundle
            from sigstore.verify import Verifier
            from sigstore.verify.policy import AllOf, AnyOf, OIDCIssuer, OIDCIssuerV2
        except ImportError as exc:  # actionable, never a bare ModuleNotFoundError
            raise BinaryVerificationError(
                "signature verification requires the 'sigstore' package "
                "(pip install sigstore). Refusing to install an unverified "
                "binary; do not bypass verification."
            ) from exc

        bundle = Bundle.from_json(bundle_bytes)
        # offline=False: install-binary is inherently online (it just downloaded
        # the asset), so let sigstore fetch/refresh its TUF trust root if the
        # local cache is cold — a fresh `pip install` has no ~/.cache/sigstore.
        # The protobuf bundle still carries the Rekor inclusion PROOF, so
        # verify_artifact needs no Rekor round-trip regardless of this flag.
        verifier = Verifier.production(offline=False)
        # Issuer extension exists as both the v1 OID (1.3.6.1.4.1.57264.1.1) and
        # the DER-encoded v2 (…1.8) in current GitHub Fulcio certs; accept either
        # so a CA rotation that drops v1 does not break every install (CRE H1).
        policy = AllOf(
            [
                AnyOf([OIDCIssuer(issuer), OIDCIssuerV2(issuer)]),
                _RegexpIdentityPolicy(identity_regexp),
            ]
        )
        # Raises sigstore VerificationError on any failure (bad sig, identity
        # mismatch, issuer mismatch, tlog/inclusion-proof failure).
        verifier.verify_artifact(asset_bytes, bundle, policy)


class _RegexpIdentityPolicy:
    """sigstore VerificationPolicy matching the cert SAN against a regexp.

    sigstore-python's stock ``Identity`` policy is exact-match only; the
    RDR-161 contract pins a cert-identity *regexp*, so this custom policy
    extracts the URI SAN and applies :func:`identity_matches`.
    """

    def __init__(self, pattern: str) -> None:
        self._pattern = pattern

    def verify(self, cert) -> None:  # noqa: ANN001 — cryptography x509 cert
        from cryptography import x509
        from sigstore.errors import VerificationError

        try:
            san = cert.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value
            uris = san.get_values_for_type(x509.UniformResourceIdentifier)
        except x509.ExtensionNotFound as exc:
            raise VerificationError(
                "signing certificate has no SubjectAlternativeName"
            ) from exc
        if not any(identity_matches(u, pattern=self._pattern) for u in uris):
            raise VerificationError(
                f"signing-cert identity {uris!r} does not match the pinned "
                f"release identity {self._pattern!r}"
            )


def verify_signature(
    asset_path: Path,
    bundle_path: Path,
    *,
    identity_regexp: str = CERT_IDENTITY_REGEXP,
    issuer: str = CERT_OIDC_ISSUER,
    checker: _SignatureChecker | None = None,
) -> None:
    """Verify *asset_path*'s Sigstore signature from *bundle_path*.

    Fail-closed: a missing bundle/asset is rejected before the checker is even
    consulted, and any exception the checker raises becomes a
    :class:`BinaryVerificationError`.
    """
    if not bundle_path.is_file():
        raise BinaryVerificationError(
            f"signature bundle {bundle_path} is missing; refusing to install "
            "an unverified binary"
        )
    if not asset_path.is_file():
        raise BinaryVerificationError(
            f"asset {asset_path} is missing; cannot verify its signature"
        )
    chk: _SignatureChecker = checker if checker is not None else _SigstoreChecker()
    try:
        chk.check(
            asset_bytes=asset_path.read_bytes(),
            bundle_bytes=bundle_path.read_bytes(),
            identity_regexp=identity_regexp,
            issuer=issuer,
        )
    except BinaryVerificationError:
        raise  # already actionable
    except Exception as exc:  # fail closed on anything else the checker throws
        raise BinaryVerificationError(
            f"signature verification failed for {asset_path.name}: {exc}"
        ) from exc


# ── download + atomic place ─────────────────────────────────────────────────


def asset_name() -> str:
    """Native-binary asset name for this host (``nexus-service-<platform>``).

    Same ``<target>`` tokens as the PG bundle — reuses
    :func:`nexus.db.pg_bundle.current_platform_tag`.
    """
    return f"nexus-service-{current_platform_tag()}"


def release_asset_url(tag: str, name: str) -> str:
    """GitHub release download URL for *name* at *tag*."""
    return f"{_RELEASE_DOWNLOAD_BASE}/{tag}/{name}"


def resolve_service_tag() -> str | None:
    """The explicit ``engine-service-v*`` tag to install, or ``None``.

    Precedence: ``NEXUS_SERVICE_TAG`` env override, then the build-time
    :data:`PINNED_SERVICE_TAG`. Never resolves "latest" (RF-161-2). ``None``
    means no tag is configured and the caller must ask the user for one.
    """
    env = os.environ.get(SERVICE_TAG_ENV, "").strip()
    return env or PINNED_SERVICE_TAG


def binary_sidecar_path(config_dir: Path) -> Path:
    """Provenance sidecar next to the well-known native binary."""
    return config_dir / "service" / _BINARY_SIDECAR_NAME


def _validate_tag(tag: str) -> None:
    if not tag.startswith(TAG_NAMESPACE_PREFIX):
        raise BinaryVerificationError(
            f"refusing tag {tag!r}: native-binary releases live in the "
            f"{TAG_NAMESPACE_PREFIX!r} namespace. Pass an explicit tag, "
            f"e.g. {TAG_NAMESPACE_PREFIX}0.1.3 (no 'latest' resolution in "
            "this release)."
        )


def _download(url: str, dest: Path, *, timeout: float = _DOWNLOAD_TIMEOUT_S) -> None:
    """Download *url* to *dest* (stdlib urllib — the repo has no ``requests``)."""
    req = urllib.request.Request(url, headers={"User-Agent": "conexus-install-binary"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, dest.open("wb") as out:
            for block in iter(lambda: resp.read(_HASH_BLOCK), b""):
                out.write(block)
    except Exception as exc:  # network, 404 for a bad tag/platform, timeout
        raise BinaryVerificationError(
            f"failed to download {url}: {exc}. Check the tag exists and the "
            "asset was published for this platform."
        ) from exc


def install_binary(
    tag: str,
    config_dir: Path,
    *,
    installed_by: str = "",
    checker: _SignatureChecker | None = None,
    download_dir: Path | None = None,
) -> tuple[Path, dict]:
    """Download, verify, and atomically place the native binary for *tag*.

    Returns ``(installed_path, provenance)``. Raises
    :class:`BinaryVerificationError` (fail closed) on any download or
    verification failure — the well-known location is only ever updated with a
    binary that passed BOTH gates.
    """
    _validate_tag(tag)
    name = asset_name()
    asset_url = release_asset_url(tag, name)

    with tempfile.TemporaryDirectory(
        dir=str(download_dir) if download_dir else None, prefix="nx_install_binary_"
    ) as td:
        tmp = Path(td)
        asset = tmp / name
        sha_sidecar = tmp / f"{name}.sha256"
        bundle = tmp / f"{name}.sigstore.json"

        _download(asset_url, asset)
        _download(f"{asset_url}.sha256", sha_sidecar)
        _download(f"{asset_url}.sigstore.json", bundle)

        # Gate 1: cheap integrity check first — a corrupt download fails here
        # before the (heavier) crypto verify.
        digest = verify_sha256(asset, sha_sidecar)
        # Gate 2: provenance.
        verify_signature(asset, bundle, checker=checker)

        dest = well_known_binary_path(config_dir)
        _atomic_copy(asset, dest, executable=True)

    provenance = _provenance(tag, name, digest, asset_url, installed_by)
    try:
        _atomic_write_json(binary_sidecar_path(config_dir), provenance)
    except OSError as exc:
        # The binary is already verified AND atomically in place; the sidecar is
        # informational provenance, not a gate. Don't turn a disk-full/perms
        # error into a traceback over a successful install (CRE M1).
        _log.warning("service_binary_sidecar_write_failed", error=str(exc))

    _log.info(
        "service_binary_installed",
        dest=str(dest),
        tag=tag,
        asset=name,
        sha256=digest[:12],
    )
    return dest, provenance


def _provenance(
    tag: str, asset: str, digest: str, source_url: str, installed_by: str
) -> dict:
    """Provenance sidecar payload (mirrors install_jar's fields)."""
    return {
        # _validate_tag guarantees the prefix; strip it to the bare version
        # (engine-service-v0.1.3 -> 0.1.3).
        "version": tag[len(TAG_NAMESPACE_PREFIX) :],
        "tag": tag,
        "asset": asset,
        "sha256": digest,
        "source_url": source_url,
        "installed_at": datetime.now(UTC).isoformat(),
        "installed_by": installed_by,
    }


def _atomic_copy(src: Path, dest: Path, *, executable: bool) -> None:
    """Copy *src* to *dest* atomically (tmp + os.replace), so a crash never
    leaves a half-written file where a consumer would find it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".nx_place_")
    try:
        with os.fdopen(tmp_fd, "wb") as out, src.open("rb") as fh:
            for block in iter(lambda: fh.read(_HASH_BLOCK), b""):
                out.write(block)
        if executable:
            os.chmod(tmp_name, 0o755)
        os.replace(tmp_name, dest)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _atomic_write_json(dest: Path, data: dict) -> None:
    """Write *data* as pretty JSON to *dest* atomically."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".nx_meta_")
    try:
        with os.fdopen(tmp_fd, "w") as out:
            json.dump(data, out, indent=2)
        os.replace(tmp_name, dest)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ── PG bundle acquisition (RDR-161 P2, same verified seam) ──────────────────


def pg_bundle_asset_name() -> str:
    """PG-bundle asset name for this host (``nexus-pg-<platform>.txz``).

    Same ``<target>`` tokens as the binary, and the SAME name
    :func:`nexus.db.pg_bundle.locate_bundle_archive` /
    ``_select_bundled_pg`` look for under ``<config_dir>/service/``.
    """
    return f"nexus-pg-{current_platform_tag()}.txz"


def pg_bundle_dest(config_dir: Path) -> Path:
    """Where the acquired PG bundle is placed — next to the binary, where the
    (RF-161-3-fixed) ``_select_bundled_pg`` default search dir looks."""
    return config_dir / "service" / pg_bundle_asset_name()


def install_pg_bundle(
    tag: str,
    config_dir: Path,
    *,
    installed_by: str = "",
    checker: _SignatureChecker | None = None,
    download_dir: Path | None = None,
) -> tuple[Path, dict]:
    """Download, verify, and atomically place the PG bundle for *tag*.

    Same two fail-closed gates and sigstore pin as :func:`install_binary`
    (one verified seam, RDR-161 Open Question 2). Places
    ``nexus-pg-<platform>.txz`` at ``<config_dir>/service/`` with a provenance
    sidecar. Returns ``(installed_path, provenance)``.
    """
    _validate_tag(tag)
    name = pg_bundle_asset_name()
    asset_url = release_asset_url(tag, name)

    with tempfile.TemporaryDirectory(
        dir=str(download_dir) if download_dir else None, prefix="nx_install_pgbundle_"
    ) as td:
        tmp = Path(td)
        asset = tmp / name
        sha_sidecar = tmp / f"{name}.sha256"
        bundle = tmp / f"{name}.sigstore.json"

        _download(asset_url, asset)
        _download(f"{asset_url}.sha256", sha_sidecar)
        _download(f"{asset_url}.sigstore.json", bundle)

        digest = verify_sha256(asset, sha_sidecar)
        verify_signature(asset, bundle, checker=checker)

        dest = pg_bundle_dest(config_dir)
        _atomic_copy(asset, dest, executable=False)  # a tarball, not an executable

    provenance = _provenance(tag, name, digest, asset_url, installed_by)
    try:
        _atomic_write_json(config_dir / "service" / _PG_SIDECAR_NAME, provenance)
    except OSError as exc:
        # The bundle is verified + atomically placed; the sidecar is informational.
        _log.warning("service_pg_bundle_sidecar_write_failed", error=str(exc))

    _log.info(
        "service_pg_bundle_installed",
        dest=str(dest),
        tag=tag,
        asset=name,
        sha256=digest[:12],
    )
    return dest, provenance
