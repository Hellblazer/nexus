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


# ── the PDF stack's DLL set (nexus-vyy65) ────────────────────────────────────

from nexus import _vcrt  # noqa: E402


def test_the_pdf_stack_set_is_the_base_pair_plus_atomic_wait() -> None:
    # torch 2.13 imports MSVCP140_ATOMIC_WAIT.dll (WinError 126 without it).
    assert _vcrt.PDF_STACK_DLLS == (*_vcrt.VC_RUNTIME_DLLS, "msvcp140_atomic_wait.dll")


def test_has_runtime_defaults_to_the_base_pair_and_takes_a_set(tmp_path: Path) -> None:
    d = _dlls(tmp_path / "d")
    assert _vcrt.has_runtime(str(d))
    assert not _vcrt.has_runtime(str(d), _vcrt.PDF_STACK_DLLS)
    _dlls(d, ("msvcp140_atomic_wait.dll",))
    assert _vcrt.has_runtime(str(d), _vcrt.PDF_STACK_DLLS)


def test_runtime_source_checks_the_set_it_is_given(tmp_path: Path) -> None:
    cfg, sysdir = tmp_path / "cfg", tmp_path / "System32"
    _dlls(cfg / "service")
    base = _vcrt.runtime_source("win32", str(cfg), str(sysdir))
    assert base == (_vcrt.APP_LOCAL, str(cfg / "service"))
    pdf = _vcrt.runtime_source("win32", str(cfg), str(sysdir), _vcrt.PDF_STACK_DLLS)
    assert pdf == (_vcrt.MISSING, None)
    _dlls(sysdir, _vcrt.PDF_STACK_DLLS)
    assert _vcrt.runtime_source("win32", str(cfg), str(sysdir), _vcrt.PDF_STACK_DLLS) == (
        _vcrt.SYSTEM, str(sysdir))
