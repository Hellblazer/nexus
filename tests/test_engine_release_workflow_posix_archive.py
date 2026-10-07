# SPDX-License-Identifier: AGPL-3.0-or-later
"""engine-service-release.yml ships the Linux and macOS engine as a .txz too.

The client (nexus.daemon.binary_install.install_binary) downloads
``nexus-service-<arch>.txz`` with its ``.sha256`` and ``.sigstore.json`` when a
release carries them. These pins hold the publishing half to that contract: the
archive is packaged from the signed and notarized binary, signed with the
protobuf bundle the client verifies, self-verified, and uploaded; the
single-file asset the cloud deploy consumes stays as it was.
"""
from __future__ import annotations

from pathlib import Path

import yaml

import package_engine_posix as pep
from nexus.daemon import binary_install

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "engine-service-release.yml"


def _steps() -> list[dict]:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["build-publish"]["steps"]


def _index(name_fragment: str) -> int:
    (i,) = [i for i, s in enumerate(_steps()) if name_fragment in s.get("name", "")]
    return i


def _run(name_fragment: str) -> str:
    return _steps()[_index(name_fragment)]["run"]


def test_the_archive_is_packaged_after_signing_and_before_cosign() -> None:
    stage = _run("Stage artifact + sha256")
    assert 'python3 scripts/package_engine_posix.py package --binary "$src" --arch "$ARCH" --out-dir dist' in stage
    assert _steps()[_index("Stage artifact + sha256")]["env"]["ARCH"] == "${{ matrix.target.arch }}"
    assert _index("Developer ID codesign") < _index("Stage artifact + sha256")
    assert _index("Notarize") < _index("Stage artifact + sha256")
    assert _index("Stage artifact + sha256") < _index("Sign release asset")


def test_the_archive_is_signed_with_the_bundle_the_client_verifies_and_self_verified() -> None:
    sign = _run("Sign release asset")
    assert 'cosign sign-blob "dist/$ASSET.txz" --new-bundle-format --bundle "dist/$ASSET.txz.sigstore.json"' in sign
    assert 'cosign verify-blob "dist/$ASSET.txz"' in sign
    assert '--bundle "dist/$ASSET.txz.sigstore.json"' in sign
    # The single-file asset keeps both bundle formats (the cloud deploy reads .cosign.bundle).
    assert '--bundle "dist/$ASSET.cosign.bundle"' in sign


def test_all_three_archive_files_are_uploaded_beside_the_single_file() -> None:
    upload = _run("Upload native binary assets to Release")
    for name in ("$ASSET", "$ASSET.sha256", "$ASSET.cosign.bundle", "$ASSET.sigstore.json",
                 "$ASSET.txz", "$ASSET.txz.sha256", "$ASSET.txz.sigstore.json"):
        assert f'"dist/{name}"' in upload, name


def test_the_packaged_name_is_the_one_the_client_requests() -> None:
    env_asset = yaml.safe_load(WORKFLOW.read_text())["jobs"]["build-publish"]["env"]["ASSET"]
    assert env_asset == "nexus-service-${{ matrix.target.arch }}"
    for arch in pep.ARCHES:
        assert f"nexus-service-{arch}.txz" == pep.asset_name(arch) == binary_install.archive_asset_name(arch)
