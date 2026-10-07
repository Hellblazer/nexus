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

import contextlib
import hashlib
import json
import lzma
import os
import re
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import structlog

from nexus._winsec import grant_user_tree_access
from nexus.daemon.binary_lifecycle import (
    WINDOWS_ENGINE_EXE,  # noqa: F401 - part of this module's asset contract, read by the Windows release leg's tests
    WINDOWS_RUNTIME_DLLS,
    well_known_binary_path,
)
from nexus.daemon.replace_guard import place_set_with_rollback
from nexus.db.pg_bundle import current_platform_tag
from nexus.engine_version import REQUIRED_ENGINE_VERSION

_log = structlog.get_logger(__name__)

__all__ = [
    "BinaryAssetAbsentError",
    "BinaryDownloadError",
    "BinaryVerificationError",
    "CERT_IDENTITY_REGEXP",
    "CERT_OIDC_ISSUER",
    "TAG_NAMESPACE_PREFIX",
    "archive_asset_name",
    "asset_name",
    "binary_sidecar_path",
    "compute_sha256",
    "identity_matches",
    "install_binary",
    "install_pg_bundle",
    "InstalledBinaryVerdict",
    "verify_installed_binary",
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

#: The ``engine-service-v*`` tag this conexus build is compatible with.
#: DERIVED from :data:`nexus.engine_version.REQUIRED_ENGINE_VERSION` — never
#: an independent literal. Prior to this, the two were separately hand-typed
#: constants that could (and did: pinned at v0.1.36 while the floor had
#: already moved to a verified, cloud-deployed v0.1.39, 2026-07-12) silently
#: drift apart. There is no topology reason for two numbers here: a fresh
#: local install has no existing state to preserve, so it should always get
#: EXACTLY the engine this release verified against — which is precisely
#: what the floor means the moment it's raised (the floor is only ever
#: bumped once a real, deployed, live-verified engine tag satisfies it; see
#: engine_version.py's docstring and the `release` skill's engine-freshness
#: gate). Bump ONE constant (``REQUIRED_ENGINE_VERSION`` in
#: ``engine_version.py``) to raise both the compatibility floor AND the
#: fresh-install pin together — there is no second knob to remember.
#: NOT a "latest" lookup — RF-161-2 forbids resolving "latest" at install
#: time (a supply-chain risk; installs must be reproducible/reviewable).
PINNED_SERVICE_TAG: str | None = (
    TAG_NAMESPACE_PREFIX + ".".join(str(p) for p in REQUIRED_ENGINE_VERSION)
)

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


class BinaryDownloadError(BinaryVerificationError):
    """An artifact could not be FETCHED — transport or availability, never a
    tamper signal (nexus-v460j).

    Subclass, deliberately: every product caller keeps catching
    :class:`BinaryVerificationError` and stays fail-closed, unchanged. The
    distinction exists for the test substrate's provisioning classifier,
    which must degrade a connectivity-class miss to the documented
    skip-sentinel while still re-raising genuine verification failures — a
    connection reset during the sigstore-attestation download used to
    name-match "Verification" and abort pytest collection with zero tests
    run (PR #1474, 2026-08-23)."""


class BinaryAssetAbsentError(BinaryDownloadError):
    """The release answered HTTP 404 for an asset: it does not carry it.

    The one download failure :func:`install_binary` acts on rather than
    propagates, and only for the first request of a POSIX install (the
    archive's ``.sha256``), where a 404 means the pinned release predates the
    POSIX ``.txz`` assets and the single-file asset is fetched instead. Every
    other caller sees a :class:`BinaryDownloadError` as before.
    """


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
            from sigstore.models import Bundle  # noqa: PLC0415 — heavy/optional dep deferred
            from sigstore.verify import Verifier  # noqa: PLC0415 — heavy/optional dep deferred
            from sigstore.verify.policy import AllOf, AnyOf, OIDCIssuer, OIDCIssuerV2  # noqa: PLC0415 — heavy/optional dep deferred
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
        from cryptography import x509  # noqa: PLC0415 — heavy/optional dep deferred
        from sigstore.errors import VerificationError  # noqa: PLC0415 — heavy/optional dep deferred

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


def _is_windows_tag(platform_tag: str) -> bool:
    return platform_tag.startswith("windows-")


def asset_name(platform_tag: str | None = None) -> str:
    """Native-binary asset name for this host.

    Unix: ``nexus-service-<platform>``, one file. Windows (RDR-224, P0.4): one
    archive, ``nexus-service-windows-x64.txz``, holding ``nexus-service.exe``
    and the four app-local VC++ runtime DLLs.

    Same ``<target>`` tokens as the PG bundle — reuses
    :func:`nexus.db.pg_bundle.current_platform_tag`. *platform_tag* is
    injectable so every branch runs on every host.
    """
    tag = platform_tag if platform_tag is not None else current_platform_tag()
    suffix = ".txz" if _is_windows_tag(tag) else ""
    return f"nexus-service-{tag}{suffix}"


def archive_asset_name(platform_tag: str | None = None) -> str:
    """The compressed engine asset: ``nexus-service-<platform>.txz``.

    The only engine asset on Windows. On Linux and macOS it holds the one file
    ``nexus-service`` (mode 0755), and engine releases carry it beside the
    single-file :func:`asset_name` asset from the first tag that publishes it;
    older releases carry only the single file. :func:`install_binary` takes the
    archive when the release has it (about 41 MB against about 160 MB) and the
    single file otherwise, so one client works against both kinds of release.
    """
    tag = platform_tag if platform_tag is not None else current_platform_tag()
    return f"nexus-service-{tag}.txz"


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


# ── installed-binary integrity (nexus-8eaeg) ────────────────────────────────


@dataclass(frozen=True)
class InstalledBinaryVerdict:
    """Does the engine binary ON DISK match the receipt that describes it?

    nexus-8eaeg: convergence used to be answered from the provenance sidecar
    ALONE — a receipt claiming v0.1.60 made the box "converged" whether or not
    the bytes it describes were still there, or still those bytes. That is
    fail-SILENT in the one direction that matters (a missing or corrupt engine
    is reported green and never re-acquired), and it is also what makes
    "should we re-acquire?" answerable WITHOUT the network: the installed
    file's digest is compared against the RECEIPT, never against a fresh
    download.

    ``ok`` is True only when the receipt records a sha256 AND the file at the
    well-known location hashes to it. Everything else — no receipt, a receipt
    with no/short digest, an absent file, an unreadable file, a digest
    disagreement — is ``ok=False`` with an operator-facing ``reason``. There
    is deliberately no "probably fine" state: an unverifiable receipt is a
    re-acquisition trigger on a wet run and a PLANNED action on a dry run.
    """

    ok: bool
    reason: str | None = None
    path: Path | None = None
    sha256: str | None = None


def verify_installed_binary(
    config_dir: Path, *, provenance: dict | None = None,
    platform_tag: str | None = None,
) -> InstalledBinaryVerdict:
    """Verify the installed engine binary against its own install receipt.

    Costs one stat + one streaming sha256 of the on-disk binary (~0.1 s for
    the ~190 MB native image on an M-series box) and NO network at all. Never
    raises: an unreadable file is a not-ok verdict, not a traceback.

    ``provenance`` is an injection seam — callers that already read the
    sidecar pass it rather than reading it twice.

    An archive-layout receipt (Windows) records the verified ARCHIVE digest in
    ``sha256`` and the digest of the placed exe in ``installed_sha256``; the
    exe is checked against the latter, and every file in ``support_files``
    (the app-local DLLs, without which the engine cannot start) is checked
    beside it. A receipt without ``installed_sha256`` is the single-file layout
    and compares ``sha256`` directly.
    """
    dest = well_known_binary_path(config_dir, platform_tag=platform_tag)
    if provenance is None:
        from nexus.daemon.binary_lifecycle import read_installed_provenance  # noqa: PLC0415 — deferred to avoid import cycle

        provenance = read_installed_provenance(config_dir)

    if not provenance:
        return InstalledBinaryVerdict(
            ok=False,
            reason=f"no install receipt at {binary_sidecar_path(config_dir)}",
            path=dest,
        )

    recorded = provenance.get("installed_sha256") or provenance.get("sha256")
    _sha_match = (
        _SHA256_LINE_RE.match(recorded.strip())
        if isinstance(recorded, str) else None
    )
    if _sha_match is None:
        return InstalledBinaryVerdict(
            ok=False,
            reason=(
                "install receipt records no usable sha256, so the installed "
                "engine binary cannot be verified against it"
            ),
            path=dest,
        )
    # The regex's captured group, not the whole trimmed string — a
    # sha256sum-style decorated value ("<hex> *file") must compare by its
    # hex alone, same as verify_sha256 (review 2026-08-02 Low).
    expected = _sha_match.group(1).lower()

    if not dest.is_file():
        return InstalledBinaryVerdict(
            ok=False,
            reason=f"installed engine binary is missing at {dest}",
            path=dest,
            sha256=expected,
        )
    try:
        actual = compute_sha256(dest)
    except OSError as exc:
        return InstalledBinaryVerdict(
            ok=False,
            reason=f"installed engine binary at {dest} is unreadable: {exc}",
            path=dest,
            sha256=expected,
        )
    if actual != expected:
        return InstalledBinaryVerdict(
            ok=False,
            reason=(
                f"installed engine binary at {dest} does not match its "
                f"receipt (receipt {expected[:12]}, on disk {actual[:12]})"
            ),
            path=dest,
            sha256=actual,
        )
    support = provenance.get("support_files") or {}
    if not isinstance(support, dict):
        return InstalledBinaryVerdict(
            ok=False,
            reason="install receipt's support_files is not a name-to-digest map",
            path=dest,
            sha256=actual,
        )
    for name, want in support.items():
        if not isinstance(name, str) or Path(name).name != name or not isinstance(want, str):
            return InstalledBinaryVerdict(
                ok=False,
                reason=f"install receipt names an unusable support file {name!r}",
                path=dest,
                sha256=actual,
            )
        path = dest.parent / name
        try:
            got = compute_sha256(path)
        except OSError as exc:
            return InstalledBinaryVerdict(
                ok=False,
                reason=f"installed support file {name} at {path} is missing or unreadable: {exc}",
                path=dest,
                sha256=actual,
            )
        if got != want.strip().lower():
            return InstalledBinaryVerdict(
                ok=False,
                reason=(
                    f"installed support file {name} at {path} does not match "
                    f"its receipt (receipt {want[:12]}, on disk {got[:12]})"
                ),
                path=dest,
                sha256=actual,
            )
    return InstalledBinaryVerdict(ok=True, path=dest, sha256=actual)


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
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise BinaryAssetAbsentError(
                f"failed to download {url}: HTTP 404, the release does not carry "
                "this asset. Check the tag exists and the asset was published for "
                "this platform."
            ) from exc
        raise BinaryDownloadError(
            f"failed to download {url}: {exc}. Check the tag exists, the "
            "asset was published for this platform, and the network is up."
        ) from exc
    except Exception as exc:  # network, timeout
        raise BinaryDownloadError(
            f"failed to download {url}: {exc}. Check the tag exists, the "
            "asset was published for this platform, and the network is up."
        ) from exc


def install_binary(
    tag: str,
    config_dir: Path,
    *,
    installed_by: str = "",
    checker: _SignatureChecker | None = None,
    download_dir: Path | None = None,
    platform_tag: str | None = None,
    quiesce: AbstractContextManager[object] | None = None,
    restart_after: bool = True,
    host_platform: str | None = None,
) -> tuple[Path, dict]:
    """Download, verify, and atomically place the native binary for *tag*.

    Returns ``(installed_path, provenance)``. Raises
    :class:`BinaryVerificationError` (fail closed) on any download or
    verification failure — the well-known location is only ever updated with a
    binary that passed BOTH gates.

    On Linux and macOS the asset is ``nexus-service-<platform>.txz`` when the
    release carries it, else the single file ``nexus-service-<platform>`` (every
    release before the POSIX archives, so a client pinned to one keeps working).
    The choice is made by what the release serves: a 404 on the archive's
    ``.sha256``, and nothing else, selects the single file. Either way both gates
    run on the downloaded bytes; an archive's one member ``nexus-service`` is then
    staged, given mode 0755 and moved into place by one rename, and the receipt
    records the archive digest in ``sha256`` and the binary's in ``installed_sha256``.

    On Windows (*platform_tag* ``windows-x64``) the asset is one archive; both
    gates run on the ARCHIVE bytes, then ``nexus-service.exe`` and the four
    runtime DLLs are placed side by side (RDR-224 P0.4). Windows refuses to
    overwrite a running executable, so on a Windows HOST the placement runs
    inside :func:`nexus.daemon.replace_quiesce.quiesced`: the storage service
    is stopped, the set is placed as one unit (a failure puts every file back,
    see :func:`~nexus.daemon.replace_guard.place_set_with_rollback`), and the
    service is started again, or not when *restart_after* is False because the
    caller restarts and verifies it itself. *quiesce* replaces that context
    manager and *host_platform* (default ``sys.platform``) is the platform the
    stop and the retry are decided on; both are test seams. On POSIX nothing
    is stopped and the placement is unchanged.
    """
    _validate_tag(tag)
    ptag = platform_tag if platform_tag is not None else current_platform_tag()
    windows = _is_windows_tag(ptag)
    extra: dict = {}

    with tempfile.TemporaryDirectory(
        dir=str(download_dir) if download_dir else None, prefix="nx_install_binary_"
    ) as td:
        tmp = Path(td)
        if windows:
            name = asset_name(ptag)
            archive_layout = True
            _download(release_asset_url(tag, name), tmp / name)
            _download(f"{release_asset_url(tag, name)}.sha256", tmp / f"{name}.sha256")
        else:
            # Linux and macOS: the .txz when the release carries it, else the single
            # file. The archive's .sha256 is the probe (a few bytes): a 404 on it,
            # and only on it, means a release from before the POSIX archives. Any
            # other failure, and a 404 on a later request, fails closed as before.
            name = archive_asset_name(ptag)
            archive_layout = True
            try:
                _download(f"{release_asset_url(tag, name)}.sha256", tmp / f"{name}.sha256")
            except BinaryAssetAbsentError:
                name = asset_name(ptag)
                archive_layout = False
                _download(f"{release_asset_url(tag, name)}.sha256", tmp / f"{name}.sha256")
            _download(release_asset_url(tag, name), tmp / name)
        asset_url = release_asset_url(tag, name)
        asset = tmp / name
        sha_sidecar = tmp / f"{name}.sha256"
        bundle = tmp / f"{name}.sigstore.json"
        _download(f"{asset_url}.sigstore.json", bundle)

        # Gate 1: cheap integrity check first — a corrupt download fails here
        # before the (heavier) crypto verify. Both gates run on the bytes that were
        # downloaded, the archive itself when the asset is one.
        digest = verify_sha256(asset, sha_sidecar)
        # Gate 2: provenance.
        verify_signature(asset, bundle, checker=checker)

        dest = well_known_binary_path(config_dir, platform_tag=ptag)
        if windows:
            guard = quiesce if quiesce is not None else _engine_quiesce(
                config_dir, restart_after=restart_after, host_platform=host_platform,
            )
            # The archive is verified and extracted with the service still up; only
            # the swap runs inside the stop (nexus-f9bgu.49, as the PG bundle does).
            extra = _place_engine_archive(asset, dest, platform=host_platform, guard=guard)
        elif archive_layout:
            # POSIX replaces a running executable by rename, so nothing is stopped,
            # exactly as for the single file below.
            extra = _place_engine_archive(
                asset, dest, support_names=(), platform=host_platform, guard=quiesce,
            )
        else:
            _atomic_copy(asset, dest, executable=True)

    provenance = _provenance(tag, name, digest, asset_url, installed_by)
    provenance.update(extra)
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


def _flat_member_name(raw: str) -> str | None:
    """The single file name an archive member denotes, or ``None`` for the
    archive-root entry (``.`` / ``./``). Raises
    :class:`BinaryVerificationError` for anything that is not a plain file name
    directly in the archive root: absolute paths, ``..``, nested paths,
    backslashes, drive letters and NTFS stream suffixes (``:``), empty names.
    """
    if raw.startswith("/") or "\\" in raw or ":" in raw or "\x00" in raw:
        raise BinaryVerificationError(f"unsafe archive member name {raw!r}")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts:
        if raw in (".", "./"):
            return None
        raise BinaryVerificationError(f"unsafe archive member name {raw!r}")
    if len(parts) != 1 or parts[0] == "..":
        raise BinaryVerificationError(f"unsafe archive member name {raw!r}")
    return parts[0]


def _engine_quiesce(
    config_dir: Path, *, restart_after: bool, host_platform: str | None,
) -> AbstractContextManager[object]:
    """The default stop-replace-restart guard for the engine files."""
    from nexus.daemon.replace_quiesce import quiesced  # noqa: PLC0415 - deferred, keeps CLI startup light

    return quiesced(
        config_dir, replacing="engine", platform=host_platform, restart_after=restart_after,
    )


def _grant_user_ace(path: Path, platform: str | None) -> None:
    """Give the user's own SID an inheritable full-control ACE on *path*: a normal
    token must be able to read and run what an ELEVATED install places here
    (nexus-f9bgu.33, critique S5; see :func:`nexus._winsec.make_user_dir` for the
    measurement). A no-op off Windows; a failure is logged, never the install's."""
    try:
        grant_user_tree_access(path, platform=platform)
    except OSError as exc:
        _log.warning("service_binary_dir_acl_grant_failed", dir=str(path), error=str(exc))


def _place_engine_archive(
    archive: Path,
    exe_dest: Path,
    *,
    support_names: tuple[str, ...] = WINDOWS_RUNTIME_DLLS,
    platform: str | None = None,
    guard: AbstractContextManager[object] | None = None,
) -> dict:
    """Extract an engine archive and place its files beside *exe_dest*.

    The archive is already sha256- and signature-verified; this is defence in
    depth. The layout is FLAT: the engine (named ``exe_dest.name``:
    ``nexus-service.exe`` on Windows, ``nexus-service`` on Linux and macOS) plus
    *support_names* (the four runtime DLLs on Windows, none elsewhere) in the
    archive root. Members stream out of the tar (no ``extractall``), so no
    member name ever reaches the filesystem unless it is a required name. Any
    other regular file (a third-party notice, P0.6) is tolerated and ignored;
    any unsafe member, link, device or nested path fails the whole archive
    before anything is placed. The staged engine gets mode 0755 before it is
    moved, so the executable bit never depends on the archive's own mode.

    Staged beside the destination, then moved into place as one set with the
    DLLs first and the exe last (:func:`~nexus.daemon.replace_guard.
    place_set_with_rollback`): each file by one atomic replace, retried on
    Windows when a scan or a handle holds it, and a failure part way restores
    every file, so the set is never half old and half new. *guard* (the
    service stop on a Windows host) is entered only for that move: reading,
    hashing and extracting the ~190 MB archive happen before it, with the
    service still running (nexus-f9bgu.49). Returns the receipt fields
    ``installed_sha256`` (exe), ``support_files`` (DLL digests) and ``layout``.
    """
    exe_name = exe_dest.name
    required = (exe_name, *support_names)
    exe_dest.parent.mkdir(parents=True, exist_ok=True)
    _grant_user_ace(exe_dest.parent, platform)
    stage = Path(tempfile.mkdtemp(dir=exe_dest.parent, prefix=".nx_stage_"))
    # ``mkdtemp`` makes the stage directory owner-only (SYSTEM, Administrators, OWNER
    # RIGHTS), and ``os.replace`` keeps the SOURCE's security descriptor: without this
    # grant, every placed file carries that ACL and a normal token cannot read or run the
    # engine an elevated install placed (measured, nexus-f9bgu.33). Granted BEFORE the first
    # staged byte so the files inherit it.
    _grant_user_ace(stage, platform)
    digests: dict[str, str] = {}
    seen: set[str] = set()
    try:
        try:
            with tarfile.open(archive, "r:xz") as tf:
                for member in tf:
                    name = _flat_member_name(member.name)
                    if member.isdir():
                        if name is not None:
                            raise BinaryVerificationError(
                                f"unsafe archive member {member.name!r}: nested directory"
                            )
                        continue
                    if not member.isreg() or name is None:
                        raise BinaryVerificationError(
                            f"unsafe archive member {member.name!r}: not a regular file"
                        )
                    if name in seen:
                        raise BinaryVerificationError(
                            f"unsafe archive: duplicate member {name!r}"
                        )
                    seen.add(name)
                    if name not in required:
                        _log.debug("service_binary_archive_extra_member_ignored", member=name)
                        continue
                    src = tf.extractfile(member)
                    if src is None:  # defensive: isreg() members always have data
                        raise BinaryVerificationError(
                            f"unsafe archive member {member.name!r}: no data"
                        )
                    sha = hashlib.sha256()
                    with src, (stage / name).open("wb") as out:
                        for block in iter(lambda: src.read(_HASH_BLOCK), b""):
                            sha.update(block)
                            out.write(block)
                    digests[name] = sha.hexdigest()
        except (tarfile.TarError, lzma.LZMAError, EOFError) as exc:
            raise BinaryVerificationError(
                f"engine archive {archive.name} could not be read: {exc}"
            ) from exc

        missing = [n for n in required if n not in digests]
        if missing:
            raise BinaryVerificationError(
                f"engine archive {archive.name} is missing required file(s): "
                f"{', '.join(missing)}; not installing."
            )

        os.chmod(stage / exe_name, 0o755)
        with guard if guard is not None else contextlib.nullcontext():
            place_set_with_rollback(
                stage, exe_dest.parent, (*support_names, exe_name),
                platform=platform,
            )
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    return {
        "layout": "archive",
        "installed_sha256": digests[exe_name],
        "support_files": {name: digests[name] for name in support_names},
    }


def _provenance(
    tag: str, asset: str, digest: str, source_url: str, installed_by: str
) -> dict:
    """Provenance sidecar payload for an installed native binary."""
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


def pg_bundle_asset_name(platform_tag: str | None = None) -> str:
    """PG-bundle asset name for this host (``nexus-pg-<platform>.txz``).

    Same ``<target>`` tokens as the binary, and the SAME name
    :func:`nexus.db.pg_bundle.locate_bundle_archive` /
    ``_select_bundled_pg`` look for under ``<config_dir>/service/``.
    """
    tag = platform_tag if platform_tag is not None else current_platform_tag()
    return f"nexus-pg-{tag}.txz"


def pg_bundle_dest(config_dir: Path, *, platform_tag: str | None = None) -> Path:
    """Where the acquired PG bundle is placed — next to the binary, where the
    (RF-161-3-fixed) ``_select_bundled_pg`` default search dir looks."""
    return config_dir / "service" / pg_bundle_asset_name(platform_tag)


def install_pg_bundle(
    tag: str,
    config_dir: Path,
    *,
    installed_by: str = "",
    checker: _SignatureChecker | None = None,
    download_dir: Path | None = None,
    platform_tag: str | None = None,
) -> tuple[Path, dict]:
    """Download, verify, and atomically place the PG bundle for *tag*.

    Same two fail-closed gates and sigstore pin as :func:`install_binary`
    (one verified seam, RDR-161 Open Question 2). Places
    ``nexus-pg-<platform>.txz`` at ``<config_dir>/service/`` with a provenance
    sidecar. Returns ``(installed_path, provenance)``.
    """
    _validate_tag(tag)
    name = pg_bundle_asset_name(platform_tag)
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

        dest = pg_bundle_dest(config_dir, platform_tag=platform_tag)
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
