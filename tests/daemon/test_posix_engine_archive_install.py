# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Linux and macOS engine as a ``.txz``, through the shared verified seam.

Engine releases from the first POSIX-archive tag carry
``nexus-service-<platform>.txz`` (one member, ``nexus-service``) beside the
single-file ``nexus-service-<platform>``; older releases carry only the single
file. The client pins ONE engine tag, so it must install from either kind of
release: it asks for the archive's ``.sha256`` first and takes the single file
only when that request is a 404. Platforms are injected, so every test runs on
every host.
"""
from __future__ import annotations

import hashlib
import io
import json
import stat
import sys
import tarfile
import urllib.error
from pathlib import Path

import pytest

from nexus.daemon import binary_install as b
from tests._module_seam import setattr_in

_TAG = "engine-service-v0.1.300"
_BASE = f"https://github.com/Hellblazer/nexus/releases/download/{_TAG}"
_BIN = b"\x7fELF-native-engine\n" * 64


class _OkChecker:
    def __init__(self) -> None:
        self.asset_bytes: list[bytes] = []

    def check(self, *, asset_bytes: bytes, **_kw) -> None:
        self.asset_bytes.append(asset_bytes)


class _FailChecker:
    def check(self, **_kw) -> None:
        raise b.BinaryVerificationError("identity mismatch")


def _txz(members: dict[str, bytes], *, mode: int = 0o644, extra=()) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            tf.addfile(info, io.BytesIO(data))
        for info in extra:
            tf.addfile(info)
    return buf.getvalue()


def _release(monkeypatch, files: dict[str, bytes], *, urls: list[str],
             sha_override: dict[str, str] | None = None, fail: dict[str, Exception] | None = None):
    """Serve *files* (name -> bytes) as a release: each gets a .sha256 and a
    .sigstore.json; anything else is a 404 (``BinaryAssetAbsentError``)."""
    sha_override = sha_override or {}
    fail = fail or {}

    def _dl(url, dest, *, timeout=0):
        urls.append(url)
        name = url.rsplit("/", 1)[1]
        if name in fail:
            raise fail[name]
        for asset, data in files.items():
            if name == asset:
                dest.write_bytes(data)
                return
            if name == f"{asset}.sha256":
                digest = sha_override.get(asset) or hashlib.sha256(data).hexdigest()
                dest.write_text(f"{digest}  {asset}\n")
                return
            if name == f"{asset}.sigstore.json":
                dest.write_text('{"protobuf":"bundle"}')
                return
        raise b.BinaryAssetAbsentError(f"404 {url}")

    monkeypatch.setattr(b, "_download", _dl)


def _install(tmp_path, ptag="linux-amd64", checker=None):
    return b.install_binary(
        _TAG, tmp_path, checker=checker or _OkChecker(), download_dir=tmp_path,
        platform_tag=ptag,
    )


def _service_files(tmp_path: Path) -> set[str]:
    svc = tmp_path / "service"
    return {p.name for p in svc.iterdir()} if svc.is_dir() else set()


def test_archive_asset_names_cover_every_platform():
    assert b.archive_asset_name("linux-amd64") == "nexus-service-linux-amd64.txz"
    assert b.archive_asset_name("linux-arm64") == "nexus-service-linux-arm64.txz"
    assert b.archive_asset_name("mac-arm64") == "nexus-service-mac-arm64.txz"
    assert b.archive_asset_name("windows-x64") == b.asset_name("windows-x64")


# ── a release that carries the archive ──────────────────────────────────────


@pytest.mark.parametrize("ptag", ["linux-amd64", "linux-arm64", "mac-arm64"])
def test_archive_release_installs_the_binary_from_the_txz(tmp_path, monkeypatch, ptag):
    archive = _txz({"nexus-service": _BIN})
    raw = f"nexus-service-{ptag}"
    urls: list[str] = []
    _release(monkeypatch, {f"{raw}.txz": archive, raw: b"RAW-MUST-NOT-BE-FETCHED"}, urls=urls)
    chk = _OkChecker()
    dest, prov = _install(tmp_path, ptag, checker=chk)

    assert dest == tmp_path / "service" / "nexus-service"
    assert dest.read_bytes() == _BIN
    assert urls == [f"{_BASE}/{raw}.txz.sha256", f"{_BASE}/{raw}.txz", f"{_BASE}/{raw}.txz.sigstore.json"]
    # The signature gate saw the ARCHIVE, the bytes that were downloaded.
    assert chk.asset_bytes == [archive]
    assert prov["asset"] == f"{raw}.txz"
    assert prov["layout"] == "archive"
    assert prov["sha256"] == hashlib.sha256(archive).hexdigest()
    assert prov["installed_sha256"] == hashlib.sha256(_BIN).hexdigest()
    assert prov["support_files"] == {}
    assert prov["source_url"] == f"{_BASE}/{raw}.txz"
    on_disk = json.loads((tmp_path / "service" / "nexus-service.meta.json").read_text())
    assert on_disk["installed_sha256"] == prov["installed_sha256"]
    assert b.verify_installed_binary(tmp_path, platform_tag=ptag).ok is True
    assert not [n for n in _service_files(tmp_path) if n.startswith(".nx_")]


def test_archive_member_mode_does_not_decide_the_executable_bit(tmp_path, monkeypatch):
    _release(monkeypatch, {"nexus-service-linux-amd64.txz": _txz({"nexus-service": _BIN}, mode=0o600)},
             urls=[])
    dest, _ = _install(tmp_path)
    assert dest.read_bytes() == _BIN
    if sys.platform != "win32":  # Windows has no POSIX execute bit: st_mode reads 0o100666 there
        mode = stat.S_IMODE(dest.stat().st_mode)
        assert mode & stat.S_IXUSR and mode & stat.S_IRGRP and mode & stat.S_IXOTH


def test_archive_install_replaces_an_existing_binary(tmp_path, monkeypatch):
    dest = tmp_path / "service" / "nexus-service"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"old engine")
    _release(monkeypatch, {"nexus-service-linux-amd64.txz": _txz({"nexus-service": _BIN})}, urls=[])
    _install(tmp_path)
    assert dest.read_bytes() == _BIN


def test_extra_regular_member_is_ignored(tmp_path, monkeypatch):
    archive = _txz({"nexus-service": _BIN, "THIRD-PARTY-NOTICES.txt": b"n"})
    _release(monkeypatch, {"nexus-service-linux-amd64.txz": archive}, urls=[])
    _install(tmp_path)
    assert "THIRD-PARTY-NOTICES.txt" not in _service_files(tmp_path)


# ── a release from before the archives (the currently pinned shape) ────────


@pytest.mark.parametrize("ptag", ["linux-amd64", "mac-arm64"])
def test_single_file_release_falls_back_on_a_404_for_the_archive(tmp_path, monkeypatch, ptag):
    raw = f"nexus-service-{ptag}"
    urls: list[str] = []
    _release(monkeypatch, {raw: _BIN}, urls=urls)
    chk = _OkChecker()
    dest, prov = _install(tmp_path, ptag, checker=chk)

    assert dest.read_bytes() == _BIN
    if sys.platform != "win32":  # no POSIX execute bit to observe on Windows
        assert stat.S_IMODE(dest.stat().st_mode) & stat.S_IXUSR
    assert urls == [
        f"{_BASE}/{raw}.txz.sha256",
        f"{_BASE}/{raw}.sha256",
        f"{_BASE}/{raw}",
        f"{_BASE}/{raw}.sigstore.json",
    ]
    assert chk.asset_bytes == [_BIN]
    assert prov["asset"] == raw
    assert prov["sha256"] == hashlib.sha256(_BIN).hexdigest()
    assert "installed_sha256" not in prov and "layout" not in prov
    assert b.verify_installed_binary(tmp_path, platform_tag=ptag).ok is True


# ── fail closed ─────────────────────────────────────────────────────────────


def test_a_transport_failure_on_the_probe_is_not_a_fallback(tmp_path, monkeypatch):
    urls: list[str] = []
    _release(monkeypatch, {"nexus-service-linux-amd64": _BIN}, urls=urls,
             fail={"nexus-service-linux-amd64.txz.sha256": b.BinaryDownloadError("reset")})
    with pytest.raises(b.BinaryDownloadError, match="reset"):
        _install(tmp_path)
    assert urls == [f"{_BASE}/nexus-service-linux-amd64.txz.sha256"]
    assert _service_files(tmp_path) == set()


def test_a_404_on_the_archive_after_its_sha256_fails_closed(tmp_path, monkeypatch):
    """A release with the archive's .sha256 but not the archive is broken, not old."""
    urls: list[str] = []
    _release(monkeypatch, {"nexus-service-linux-amd64": _BIN}, urls=urls,
             fail={"nexus-service-linux-amd64.txz": b.BinaryAssetAbsentError("404 txz")})
    # The probe must succeed for this case: serve a .sha256 for the archive.
    orig = b._download

    def _dl(url, dest, *, timeout=0):
        if url.endswith(".txz.sha256"):
            urls.append(url)
            dest.write_text(f"{'a' * 64}  nexus-service-linux-amd64.txz\n")
            return
        orig(url, dest, timeout=timeout)

    monkeypatch.setattr(b, "_download", _dl)
    with pytest.raises(b.BinaryAssetAbsentError):
        _install(tmp_path)
    assert f"{_BASE}/nexus-service-linux-amd64" not in urls
    assert _service_files(tmp_path) == set()


def test_sha256_mismatch_on_the_archive_installs_nothing(tmp_path, monkeypatch):
    _release(monkeypatch, {"nexus-service-linux-amd64.txz": _txz({"nexus-service": _BIN})}, urls=[],
             sha_override={"nexus-service-linux-amd64.txz": "0" * 64})
    with pytest.raises(b.BinaryVerificationError, match="sha256 mismatch"):
        _install(tmp_path)
    assert _service_files(tmp_path) == set()


def test_signature_failure_on_the_archive_installs_nothing(tmp_path, monkeypatch):
    _release(monkeypatch, {"nexus-service-linux-amd64.txz": _txz({"nexus-service": _BIN})}, urls=[])
    with pytest.raises(b.BinaryVerificationError, match="identity mismatch"):
        _install(tmp_path, checker=_FailChecker())
    assert _service_files(tmp_path) == set()


def test_corrupt_archive_leaves_the_installed_binary_untouched(tmp_path, monkeypatch):
    """Digest and signature match the published bytes, which are not an xz tar."""
    dest = tmp_path / "service" / "nexus-service"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"old engine")
    good = _txz({"nexus-service": _BIN})
    corrupt = good[: len(good) // 2]
    _release(monkeypatch, {"nexus-service-linux-amd64.txz": corrupt}, urls=[])
    with pytest.raises(b.BinaryVerificationError, match="could not be read"):
        _install(tmp_path)
    assert dest.read_bytes() == b"old engine"
    assert not [n for n in _service_files(tmp_path) if n.startswith(".nx_")]
    assert "nexus-service.meta.json" not in _service_files(tmp_path)


def test_archive_without_the_binary_installs_nothing(tmp_path, monkeypatch):
    _release(monkeypatch, {"nexus-service-linux-amd64.txz": _txz({"nexus-service.exe": _BIN})}, urls=[])
    with pytest.raises(b.BinaryVerificationError, match="missing required file"):
        _install(tmp_path)
    assert not (tmp_path / "service" / "nexus-service").exists()


@pytest.mark.parametrize("name", ["../nexus-service", "/abs/nexus-service", "bin/nexus-service"])
def test_unsafe_member_names_are_rejected(tmp_path, monkeypatch, name):
    _release(monkeypatch, {"nexus-service-linux-amd64.txz": _txz({name: _BIN})}, urls=[])
    with pytest.raises(b.BinaryVerificationError, match="unsafe archive member"):
        _install(tmp_path)
    assert not (tmp_path / "nexus-service").exists()
    assert not (tmp_path / "service" / "nexus-service").exists()


def test_symlink_member_is_rejected(tmp_path, monkeypatch):
    link = tarfile.TarInfo("nexus-service")
    link.type = tarfile.SYMTYPE
    link.linkname = "/bin/sh"
    _release(monkeypatch, {"nexus-service-linux-amd64.txz": _txz({}, extra=[link])}, urls=[])
    with pytest.raises(b.BinaryVerificationError, match="not a regular file"):
        _install(tmp_path)
    assert not (tmp_path / "service" / "nexus-service").exists()


# ── _download's 404 classification ──────────────────────────────────────────


def _raise_http(code: int):
    def _urlopen(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)
    return _urlopen


def test_download_maps_404_to_asset_absent(tmp_path, monkeypatch):
    setattr_in(monkeypatch, b, "urllib.request.urlopen", _raise_http(404))
    with pytest.raises(b.BinaryAssetAbsentError, match="HTTP 404"):
        b._download("https://example/x", tmp_path / "x")


@pytest.mark.parametrize("code", [403, 500, 503])
def test_download_other_http_errors_are_not_asset_absent(tmp_path, monkeypatch, code):
    setattr_in(monkeypatch, b, "urllib.request.urlopen", _raise_http(code))
    with pytest.raises(b.BinaryDownloadError) as exc_info:
        b._download("https://example/x", tmp_path / "x")
    assert not isinstance(exc_info.value, b.BinaryAssetAbsentError)
