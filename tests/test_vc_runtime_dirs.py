# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-lqjll: on Windows without the VC++ redistributable, the client's own
extension modules (onnxruntime, pymupdf, torch, fasttext) find msvcp140*.dll in
the engine directory or the PG bundle's bin, which ship them app-local.

Measured on the RDR-224 clean guest: all four failed to import, and all four
imported once a directory holding msvcp140.dll and msvcp140_1.dll was added with
os.add_dll_directory. These tests run the Windows branch on every host.
"""

from __future__ import annotations

from pathlib import Path

import nexus


def _dlls(d: Path, names: tuple[str, ...] = nexus._VC_RUNTIME_DLLS) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_bytes(b"MZ")
    return d


def _run(tmp_path: Path, **kw):  # noqa: ANN202
    added: list[str] = []
    out = nexus._add_vc_runtime_dirs(
        platform=kw.pop("platform", "win32"),
        config_dir=str(tmp_path / "cfg"),
        system_dir=str(kw.pop("system", tmp_path / "System32")),
        add=lambda d: added.append(d) or object(),
    )
    return out, added


def test_posix_is_untouched(tmp_path: Path) -> None:
    _dlls(tmp_path / "cfg" / "service")
    assert _run(tmp_path, platform="darwin") == ([], [])


def test_a_system_runtime_means_nothing_is_added(tmp_path: Path) -> None:
    _dlls(tmp_path / "cfg" / "service")
    sysdir = _dlls(tmp_path / "System32")
    assert _run(tmp_path, system=sysdir) == ([], [])


def test_engine_dir_and_bundle_bin_are_added_when_the_system_lacks_the_runtime(tmp_path: Path) -> None:
    svc = _dlls(tmp_path / "cfg" / "service")
    pgbin = _dlls(tmp_path / "cfg" / "pg-bundle" / "bundle" / "bin")
    out, added = _run(tmp_path)
    assert out == [str(svc), str(pgbin)] and added == out


def test_a_directory_missing_one_dll_is_skipped(tmp_path: Path) -> None:
    _dlls(tmp_path / "cfg" / "service", ("msvcp140.dll",))
    assert _run(tmp_path) == ([], [])


def test_nothing_installed_yet_adds_nothing(tmp_path: Path) -> None:
    assert _run(tmp_path) == ([], [])


def test_a_refused_directory_does_not_break_import(tmp_path: Path) -> None:
    _dlls(tmp_path / "cfg" / "service")

    def refuse(d: str) -> object:
        raise OSError("refused")

    out = nexus._add_vc_runtime_dirs(platform="win32", config_dir=str(tmp_path / "cfg"),
                                     system_dir=str(tmp_path / "System32"), add=refuse)
    assert out == []


def test_the_needed_set_is_the_measured_one() -> None:
    # msvcp140 alone left onnxruntime failing on the guest; both are required.
    assert nexus._VC_RUNTIME_DLLS == ("msvcp140.dll", "msvcp140_1.dll")
