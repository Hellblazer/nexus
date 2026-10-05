# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-224 P3.1 (nexus-f9bgu.15): the Windows engine asset through the shared
verified install seam.

Layout decided in P0.4 (nexus-f9bgu.4): ONE archive holding
``nexus-service.exe`` plus the four app-local VC++ runtime DLLs, verified by one
``.sha256`` and one sigstore bundle. The archive is a ``.txz`` like the PG
bundle, named ``nexus-service-windows-x64.txz``.

The platform is injected (``platform_tag="windows-x64"``) so every test here
runs on macOS and Linux as well as Windows; nothing skips.
"""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.commands import init as init_mod
from nexus.commands.daemon import service_install_binary_cmd
from nexus.daemon import binary_install as b
from nexus.daemon.binary_lifecycle import (
    WINDOWS_ENGINE_EXE,
    WINDOWS_RUNTIME_DLLS,
    well_known_binary_path,
)

_TAG = "engine-service-v0.1.200"
_WIN = "windows-x64"
_ASSET = "nexus-service-windows-x64.txz"
_PAYLOADS = {
    WINDOWS_ENGINE_EXE: b"MZ-engine-exe\n",
    **{dll: f"MZ-{dll}\n".encode() for dll in WINDOWS_RUNTIME_DLLS},
}


class _OkChecker:
    def __init__(self) -> None:
        self.asset_bytes: list[bytes] = []

    def check(self, *, asset_bytes: bytes, **_kw) -> None:
        self.asset_bytes.append(asset_bytes)


class _FailChecker:
    def check(self, **_kw) -> None:
        raise b.BinaryVerificationError("identity mismatch")


def _txz(members: dict[str, bytes], *, extra=()) -> bytes:
    """Build a .txz in memory. *extra* is an iterable of prebuilt TarInfo
    (with optional payload) for members a dict cannot express (links, dirs)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        for info, data in extra:
            tf.addfile(info, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


def _serve(monkeypatch, archive: bytes, *, urls: list[str] | None = None,
           sha_override: str | None = None) -> None:
    def _dl(url, dest, *, timeout=0):
        if urls is not None:
            urls.append(url)
        if url.endswith(".sha256"):
            digest = sha_override or hashlib.sha256(archive).hexdigest()
            dest.write_text(f"{digest}  {_ASSET}\n")
        elif url.endswith(".sigstore.json"):
            dest.write_text('{"protobuf":"bundle"}')
        else:
            dest.write_bytes(archive)

    monkeypatch.setattr(b, "_download", _dl)


def _install(tmp_path, monkeypatch, archive: bytes, **kw):
    _serve(monkeypatch, archive)
    return b.install_binary(
        _TAG, tmp_path, checker=kw.pop("checker", _OkChecker()),
        download_dir=tmp_path, platform_tag=_WIN, **kw,
    )


def _service_files(tmp_path: Path) -> set[str]:
    svc = tmp_path / "service"
    return {p.name for p in svc.iterdir()} if svc.is_dir() else set()


# ── names ───────────────────────────────────────────────────────────────────


def test_windows_asset_names():
    assert b.asset_name(_WIN) == "nexus-service-windows-x64.txz"
    assert b.pg_bundle_asset_name(_WIN) == "nexus-pg-windows-x64.txz"
    assert b.pg_bundle_dest(Path("/c"), platform_tag=_WIN) == Path(
        "/c/service/nexus-pg-windows-x64.txz")


def test_unix_asset_names_unchanged():
    assert b.asset_name("linux-amd64") == "nexus-service-linux-amd64"
    assert b.asset_name("mac-arm64") == "nexus-service-mac-arm64"
    assert b.pg_bundle_asset_name("linux-arm64") == "nexus-pg-linux-arm64.txz"


def test_well_known_binary_is_exe_on_windows_only():
    cfg = Path("/c")
    assert well_known_binary_path(cfg, platform_tag=_WIN) == cfg / "service" / "nexus-service.exe"
    assert well_known_binary_path(cfg, platform_tag="linux-amd64") == cfg / "service" / "nexus-service"
    assert well_known_binary_path(cfg, platform_tag="mac-arm64") == cfg / "service" / "nexus-service"


# ── install_binary, one-archive layout ──────────────────────────────────────


def test_install_places_exe_and_four_dlls_side_by_side(tmp_path, monkeypatch):
    archive = _txz(_PAYLOADS)
    dest, prov = _install(tmp_path, monkeypatch, archive)

    assert dest == tmp_path / "service" / "nexus-service.exe"
    assert dest.suffix == ".exe"  # non-vacuity: the Windows name, not the Unix one
    expected = {WINDOWS_ENGINE_EXE, *WINDOWS_RUNTIME_DLLS}
    assert len(expected) == 5
    for name in expected:
        assert (tmp_path / "service" / name).read_bytes() == _PAYLOADS[name]
    assert "nexus-service" not in _service_files(tmp_path)
    # no staging debris left beside the install
    assert not [n for n in _service_files(tmp_path) if n.startswith(".nx_")]
    assert prov["asset"] == _ASSET


def test_fetches_archive_sha256_and_bundle_by_asset_name(tmp_path, monkeypatch):
    archive = _txz(_PAYLOADS)
    urls: list[str] = []
    _serve(monkeypatch, archive, urls=urls)
    b.install_binary(_TAG, tmp_path, checker=_OkChecker(), download_dir=tmp_path,
                     platform_tag=_WIN)
    base = f"https://github.com/Hellblazer/nexus/releases/download/{_TAG}/{_ASSET}"
    assert urls == [base, f"{base}.sha256", f"{base}.sigstore.json"]


def test_cosign_verification_covers_the_archive_bytes(tmp_path, monkeypatch):
    archive = _txz(_PAYLOADS)
    chk = _OkChecker()
    _install(tmp_path, monkeypatch, archive, checker=chk)
    assert chk.asset_bytes == [archive]


def test_receipt_records_archive_digest_and_installed_file_digests(tmp_path, monkeypatch):
    archive = _txz(_PAYLOADS)
    _, prov = _install(tmp_path, monkeypatch, archive)
    assert prov["sha256"] == hashlib.sha256(archive).hexdigest()
    assert prov["installed_sha256"] == hashlib.sha256(_PAYLOADS[WINDOWS_ENGINE_EXE]).hexdigest()
    assert prov["support_files"] == {
        dll: hashlib.sha256(_PAYLOADS[dll]).hexdigest() for dll in WINDOWS_RUNTIME_DLLS
    }
    on_disk = json.loads((tmp_path / "service" / "nexus-service.meta.json").read_text())
    assert on_disk["installed_sha256"] == prov["installed_sha256"]


def test_sha256_mismatch_installs_nothing(tmp_path, monkeypatch):
    archive = _txz(_PAYLOADS)
    _serve(monkeypatch, archive, sha_override="0" * 64)
    with pytest.raises(b.BinaryVerificationError, match="sha256 mismatch"):
        b.install_binary(_TAG, tmp_path, checker=_OkChecker(), download_dir=tmp_path,
                         platform_tag=_WIN)
    assert _service_files(tmp_path) == set()


def test_signature_failure_installs_nothing(tmp_path, monkeypatch):
    _serve(monkeypatch, _txz(_PAYLOADS))
    with pytest.raises(b.BinaryVerificationError, match="identity mismatch"):
        b.install_binary(_TAG, tmp_path, checker=_FailChecker(), download_dir=tmp_path,
                         platform_tag=_WIN)
    assert _service_files(tmp_path) == set()


@pytest.mark.parametrize("missing", [WINDOWS_ENGINE_EXE, *WINDOWS_RUNTIME_DLLS])
def test_archive_missing_a_required_file_installs_nothing(tmp_path, monkeypatch, missing):
    members = {k: v for k, v in _PAYLOADS.items() if k != missing}
    with pytest.raises(b.BinaryVerificationError, match=missing.replace(".", r"\.")):
        _install(tmp_path, monkeypatch, _txz(members))
    assert _service_files(tmp_path) == set()


def test_dot_slash_member_names_are_accepted(tmp_path, monkeypatch):
    """bsdtar ``-C dir .`` writes ``./name`` members and a ``./`` directory."""
    archive = _txz({f"./{k}": v for k, v in _PAYLOADS.items()},
                   extra=[(_dir_info("./"), None)])
    dest, _ = _install(tmp_path, monkeypatch, archive)
    assert dest.read_bytes() == _PAYLOADS[WINDOWS_ENGINE_EXE]


def test_extra_flat_files_are_tolerated_but_not_installed(tmp_path, monkeypatch):
    """P0.6 requires a third-party notice in the shipped archive; it must not
    break the install, and it is not placed beside the engine."""
    archive = _txz({**_PAYLOADS, "THIRD-PARTY-NOTICES.txt": b"notice"})
    _install(tmp_path, monkeypatch, archive)
    assert "THIRD-PARTY-NOTICES.txt" not in _service_files(tmp_path)


def _dir_info(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    return info


def _link_info(name: str, target: str, *, symlink: bool) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE if symlink else tarfile.LNKTYPE
    info.linkname = target
    return info


_HOSTILE = [
    "../evil.dll",
    "../../evil.dll",
    "/abs/evil.dll",
    "sub/vcruntime140.dll",
    "..\\evil.dll",
    "C:\\Windows\\evil.dll",
    "C:evil.dll",
    "vcruntime140.dll:stream",
    "",
]


@pytest.mark.parametrize("name", [n for n in _HOSTILE if n])
def test_traversal_and_absolute_members_are_rejected(tmp_path, monkeypatch, name):
    install_root = tmp_path / "cfg"
    install_root.mkdir()
    sentinel_dir = tmp_path
    archive = _txz({**_PAYLOADS, name: b"owned"})
    _serve(monkeypatch, archive)
    with pytest.raises(b.BinaryVerificationError, match="unsafe"):
        b.install_binary(_TAG, install_root, checker=_OkChecker(),
                         download_dir=tmp_path, platform_tag=_WIN)
    assert _service_files(install_root) == set()
    assert not (sentinel_dir / "evil.dll").exists()
    assert not (install_root.parent / "evil.dll").exists()


@pytest.mark.parametrize("symlink", [True, False])
def test_link_members_are_rejected(tmp_path, monkeypatch, symlink):
    archive = _txz(_PAYLOADS, extra=[(_link_info("evil.dll", "/etc/passwd", symlink=symlink), None)])
    with pytest.raises(b.BinaryVerificationError, match="unsafe"):
        _install(tmp_path, monkeypatch, archive)
    assert _service_files(tmp_path) == set()


def test_nested_directory_member_is_rejected(tmp_path, monkeypatch):
    archive = _txz(_PAYLOADS, extra=[(_dir_info("sub"), None)])
    with pytest.raises(b.BinaryVerificationError, match="unsafe"):
        _install(tmp_path, monkeypatch, archive)
    assert _service_files(tmp_path) == set()


def test_corrupt_archive_installs_nothing(tmp_path, monkeypatch):
    with pytest.raises(b.BinaryVerificationError):
        _install(tmp_path, monkeypatch, b"not-an-xz-stream")
    assert _service_files(tmp_path) == set()


def test_reinstall_replaces_files_in_place(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, _txz(_PAYLOADS))
    newer = {k: v + b"-v2" for k, v in _PAYLOADS.items()}
    dest, _ = _install(tmp_path, monkeypatch, _txz(newer))
    assert dest.read_bytes() == newer[WINDOWS_ENGINE_EXE]
    assert (tmp_path / "service" / "msvcp140.dll").read_bytes() == newer["msvcp140.dll"]


# ── verify_installed_binary against an archive-layout receipt ───────────────


def test_verify_installed_binary_ok_for_windows_layout(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, _txz(_PAYLOADS))
    verdict = b.verify_installed_binary(tmp_path, platform_tag=_WIN)
    assert verdict.ok is True, verdict.reason
    assert verdict.path == tmp_path / "service" / "nexus-service.exe"


def test_verify_installed_binary_flags_tampered_exe(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, _txz(_PAYLOADS))
    (tmp_path / "service" / "nexus-service.exe").write_bytes(b"tampered")
    verdict = b.verify_installed_binary(tmp_path, platform_tag=_WIN)
    assert verdict.ok is False
    assert "does not match its receipt" in verdict.reason


@pytest.mark.parametrize("dll", WINDOWS_RUNTIME_DLLS)
def test_verify_installed_binary_flags_missing_or_tampered_dll(tmp_path, monkeypatch, dll):
    _install(tmp_path, monkeypatch, _txz(_PAYLOADS))
    path = tmp_path / "service" / dll
    path.write_bytes(b"tampered")
    tampered = b.verify_installed_binary(tmp_path, platform_tag=_WIN)
    assert tampered.ok is False and dll in tampered.reason
    path.unlink()
    gone = b.verify_installed_binary(tmp_path, platform_tag=_WIN)
    assert gone.ok is False and dll in gone.reason


# ── init and install-binary CLI ─────────────────────────────────────────────


def test_init_step_fetches_the_windows_asset_names(tmp_path, monkeypatch):

    monkeypatch.setenv("NEXUS_SERVICE_TAG", _TAG)
    monkeypatch.delenv("NEXUS_SERVICE_BIN", raising=False)
    monkeypatch.delenv("NEXUS_SERVICE_JAR", raising=False)
    monkeypatch.setattr(b, "current_platform_tag", lambda **_kw: _WIN)
    urls: list[str] = []
    _serve(monkeypatch, _txz(_PAYLOADS), urls=urls)
    real_install = b.install_binary
    monkeypatch.setattr(
        b, "install_binary",
        lambda *a, **kw: real_install(*a, checker=_OkChecker(), download_dir=tmp_path, **kw),
    )

    assert init_mod._ensure_service_binary_step(tmp_path) is True
    assert urls and all(f"/{_ASSET}" in u for u in urls)
    assert (tmp_path / "service" / "nexus-service.exe").is_file()
    assert (tmp_path / "service" / "vcruntime140.dll").is_file()


def _stub_cli(monkeypatch, calls: list[str], *, windows: bool = False) -> None:
    def _fake_binary(tag, config_dir, *, installed_by="", **_kw):
        calls.append("binary")
        return config_dir / "service" / "nexus-service", {
            "asset": b.asset_name(), "version": "0.1.200",
            "sha256": "a" * 64, "source_url": "https://example/x",
        }

    def _fake_pg(tag, config_dir, *, installed_by="", **_kw):
        calls.append("pg")
        return config_dir / "service" / b.pg_bundle_asset_name(), {
            "asset": b.pg_bundle_asset_name(), "sha256": "b" * 64,
        }

    monkeypatch.setattr(b, "install_binary", _fake_binary)
    monkeypatch.setattr(b, "install_pg_bundle", _fake_pg)
    if windows:
        monkeypatch.setattr(b, "current_platform_tag", lambda **_kw: _WIN)


def test_install_binary_installs_the_pg_bundle_by_default(tmp_path, monkeypatch):

    calls: list[str] = []
    _stub_cli(monkeypatch, calls)
    result = CliRunner().invoke(
        service_install_binary_cmd, [_TAG, "--config-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert calls == ["binary", "pg"]


def test_no_pg_bundle_flag_installs_only_the_engine(tmp_path, monkeypatch):

    calls: list[str] = []
    _stub_cli(monkeypatch, calls)
    result = CliRunner().invoke(
        service_install_binary_cmd, [_TAG, "--no-pg-bundle", "--config-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert calls == ["binary"]


def test_install_binary_names_the_windows_assets(tmp_path, monkeypatch):

    calls: list[str] = []
    _stub_cli(monkeypatch, calls, windows=True)
    result = CliRunner().invoke(
        service_install_binary_cmd, [_TAG, "--config-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Resolving nexus-service-windows-x64.txz" in result.output
    assert "Resolving nexus-pg-windows-x64.txz" in result.output
    assert calls == ["binary", "pg"]
