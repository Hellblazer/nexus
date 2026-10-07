# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/package_engine_posix.py: the Linux and macOS engine archive.

The archive is fed to the REAL client placer, so the asset contract is checked
against its consumer (as tests/test_windows_engine_release.py does for Windows).
"""
from __future__ import annotations

import hashlib
import io
import stat
import tarfile
from pathlib import Path

import pytest

import package_engine_posix as pep
from nexus.daemon import binary_install

_BIN = bytes(range(256)) * 400  # ~100 KB, compressible like a binary is not, but enough


def _binary(tmp_path: Path, data: bytes = _BIN) -> Path:
    p = tmp_path / "nexus-service"
    p.write_bytes(data)
    p.chmod(0o755)
    return p


@pytest.mark.parametrize("arch", pep.ARCHES)
def test_asset_name_matches_the_client(arch):
    assert pep.asset_name(arch) == binary_install.archive_asset_name(arch)


def test_unknown_arch_is_refused(tmp_path):
    with pytest.raises(pep.PackageError, match="unknown arch"):
        pep.package(_binary(tmp_path), "windows-x64", tmp_path / "dist", min_bytes=1)


def test_package_writes_archive_and_sidecar_in_the_client_format(tmp_path):
    archive = pep.package(_binary(tmp_path), "linux-amd64", tmp_path / "dist", min_bytes=1)
    assert archive.name == "nexus-service-linux-amd64.txz"
    sidecar = archive.with_name(archive.name + ".sha256")
    # The client's own sidecar parser accepts it.
    assert binary_install.verify_sha256(archive, sidecar) == hashlib.sha256(archive.read_bytes()).hexdigest()
    with tarfile.open(archive, "r:xz") as tf:
        (m,) = tf.getmembers()
    assert (m.name, m.mode, m.mtime, m.uid, m.gid, m.uname, m.gname) == ("nexus-service", 0o755, 0, 0, 0, "", "")
    assert pep.verify(archive) == []


def test_archive_bytes_depend_on_the_binary_alone(tmp_path):
    a = pep.package(_binary(tmp_path), "mac-arm64", tmp_path / "a", min_bytes=1).read_bytes()
    b = pep.package(_binary(tmp_path), "mac-arm64", tmp_path / "b", min_bytes=1).read_bytes()
    assert a == b


def test_the_client_placer_installs_what_package_writes(tmp_path):
    archive = pep.package(_binary(tmp_path), "linux-arm64", tmp_path / "dist", min_bytes=1)
    dest = tmp_path / "cfg" / "service" / "nexus-service"
    receipt = binary_install._place_engine_archive(archive, dest, support_names=())
    assert dest.read_bytes() == _BIN
    assert stat.S_IMODE(dest.stat().st_mode) == 0o755
    assert receipt["installed_sha256"] == hashlib.sha256(_BIN).hexdigest()


def test_small_binary_is_refused(tmp_path):
    with pytest.raises(pep.PackageError, match="suspiciously small"):
        pep.package(_binary(tmp_path, b"x"), "linux-amd64", tmp_path / "dist")
    assert not (tmp_path / "dist" / "nexus-service-linux-amd64.txz").exists()


def _txz(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.mark.parametrize(
    ("members", "needle"),
    [
        ({"nexus-service": b"x", "extra": b"y"}, "unexpected member"),
        ({"bin/nexus-service": b"x"}, "exactly one"),
        ({"nexus-service": b""}, "empty"),
    ],
)
def test_verify_names_each_layout_problem(tmp_path, members, needle):
    p = tmp_path / "a.txz"
    p.write_bytes(_txz(members))
    assert any(needle in line for line in pep.verify(p))


def test_verify_rejects_a_truncated_archive(tmp_path):
    p = tmp_path / "a.txz"
    p.write_bytes(_txz({"nexus-service": _BIN})[:100])
    (line,) = pep.verify(p)
    assert "could not be read" in line


def test_main_exit_codes(tmp_path):
    binary = _binary(tmp_path, b"z" * (pep.MIN_BINARY_BYTES + 1))
    assert pep.main(["package", "--binary", str(binary), "--arch", "linux-amd64",
                     "--out-dir", str(tmp_path / "dist")]) == 0
    archive = tmp_path / "dist" / "nexus-service-linux-amd64.txz"
    assert pep.main(["verify", "--archive", str(archive)]) == 0
    archive.write_bytes(b"not xz")
    assert pep.main(["verify", "--archive", str(archive)]) == 1
