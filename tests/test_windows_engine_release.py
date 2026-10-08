# SPDX-License-Identifier: AGPL-3.0-or-later
"""The windows-x64 engine release leg's scripts (RDR-224 P1.2, nexus-f9bgu.9).

``scripts/windows_engine_release.py`` is Python with the process runner and the
platform injected, so every branch runs on every OS and nothing skip-passes on a
laptop. The fixtures under ``tests/fixtures/windows_dumpbin/`` are real
``dumpbin /dependents`` output captured on qwentescence (native Windows 11, MSVC
14.44) from the engine exe a ``-Pnative`` build produced and from the native
libraries it embeds; the archive tests feed the packaged archive to the REAL
client placer (``nexus.daemon.binary_install._place_engine_archive``), so the
asset contract is checked against its consumer, not against a copy of it.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import lzma
import tarfile
import zipfile
from pathlib import Path

import pytest

import build_pg_bundle_windows as bw
import check_native_embedded_resources as chk
import windows_engine_release as wer
from nexus.daemon import binary_install
from tests._module_seam import setattr_in

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "windows_dumpbin"
MS_SUBJECT = "CN=Microsoft Windows, O=Microsoft Corporation, L=Redmond, S=Washington, C=US"


@pytest.fixture(autouse=True)
def _runtime_dlls_are_signed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The packaging tests stage fake DLLs on any OS; Authenticode is asked of a real file on Windows only.
    The signature check has its own tests (tests/test_pg_bundle_windows.py and below), which inject readers."""
    monkeypatch.setattr(bw, "read_authenticode", lambda path: bw.Signature("Valid", MS_SUBJECT))


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _dump(*deps: str, delay: tuple[str, ...] = ()) -> str:
    """dumpbin /dependents output for a binary importing *deps* (and delay-loading *delay*)."""
    out = ["Microsoft (R) COFF/PE Dumper Version 14.44.35229.0", "", "", "Dump of file x.dll", "",
           "File Type: DLL", "", "  Image has the following dependencies:", ""]
    out += [f"    {d}" for d in deps]
    if delay:
        out += ["", "  Image has the following delay load dependencies:", ""]
        out += [f"    {d}" for d in delay]
    out += ["", "  Summary", "", "        E000 .data"]
    return "\r\n".join(out) + "\r\n"


# --------------------------------------------------------------------------- #
# Constants shared with the client and the PG bundle script
# --------------------------------------------------------------------------- #


def test_the_asset_contract_equals_what_the_client_expects() -> None:
    assert wer.ASSET_NAME == binary_install.asset_name("windows-x64")
    assert wer.ENGINE_EXE == binary_install.WINDOWS_ENGINE_EXE
    assert tuple(wer.VC_RUNTIME_DLLS) == tuple(binary_install.WINDOWS_RUNTIME_DLLS)
    assert tuple(wer.VC_RUNTIME_DLLS) == tuple(bw.VC_RUNTIME_DLLS)


def test_the_platform_key_is_a_checker_platform() -> None:
    assert wer.ARCH in chk.PLATFORMS


# --------------------------------------------------------------------------- #
# dumpbin parsing
# --------------------------------------------------------------------------- #


def test_parse_the_real_exe_output() -> None:
    deps = wer.parse_dependents(_fixture("nexus-service.exe.txt"))
    assert len(deps) == 24
    assert deps[:3] == ["VERSION.dll", "ADVAPI32.dll", "WS2_32.dll"]
    assert "VCRUNTIME140_1.dll" in deps and "MSWSOCK.dll" in deps
    # dumpbin's own header and the section-size summary are not dependencies
    assert all(d.lower().endswith(".dll") for d in deps)
    assert not any("Dump" in d or "Summary" in d for d in deps)


def test_parse_handles_crlf_and_every_real_fixture() -> None:
    for name in ("nexus-service.exe.txt", "tokenizers.dll.txt", "onnxruntime.dll.txt", "onnxruntime4j_jni.dll.txt"):
        assert wer.parse_dependents(_fixture(name)), name
    assert "MSVCP140_1.dll" in wer.parse_dependents(_fixture("onnxruntime.dll.txt"))


def test_parse_includes_delay_loaded_dependencies() -> None:
    deps = wer.parse_dependents(_dump("KERNEL32.dll", delay=("dbghelp.dll",)))
    assert deps == ["KERNEL32.dll", "dbghelp.dll"]


@pytest.mark.parametrize(
    "text",
    ["", "Microsoft (R) COFF/PE Dumper\r\n\r\nDump of file x\r\n\r\nFile Type: DLL\r\n\r\n  Summary\r\n",
     "LINK : fatal error LNK1181: cannot open input file 'x.dll'"],
    ids=["empty", "no-section", "error-text"],
)
def test_parse_refuses_output_with_no_dependency_section(text: str) -> None:
    """A dumpbin that printed nothing useful must not read as 'depends on nothing'."""
    with pytest.raises(wer.CheckError):
        wer.parse_dependents(text)


# --------------------------------------------------------------------------- #
# The allowlist
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", [*bw.VC_RUNTIME_DLLS, "VCRUNTIME140.dll", "MSVCP140_1.DLL"])
def test_the_four_vc_runtime_dlls_classify_as_vc_case_insensitively(name: str) -> None:
    assert wer.classify(name, siblings=frozenset()) == "vc"


@pytest.mark.parametrize(
    "name",
    ["KERNEL32.dll", "kernel32.dll", "ADVAPI32.dll", "PSAPI.DLL", "msvcrt.dll", "ntdll.dll",
     "api-ms-win-crt-runtime-l1-1-0.dll", "api-ms-win-core-synch-l1-2-0.dll", "ext-ms-win-ntuser-window-l1-1-0.dll"],
)
def test_windows_system_dlls_classify_as_system(name: str) -> None:
    assert wer.classify(name, siblings=frozenset()) == "system"


@pytest.mark.parametrize(
    "name",
    ["VCRUNTIME140_THREADS.dll", "concrt140.dll", "vcomp140.dll", "msvcp140_2.dll", "msvcp140_atomic_wait.dll",
     "vcruntime140d.dll", "ucrtbased.dll", "libcrypto-3-x64.dll", "zlib1.dll", "dbghelp.dll", "api-ms-win-crt.exe"],
    ids=lambda n: n,
)
def test_anything_else_is_unclassified(name: str) -> None:
    """Includes the VC++ DLLs we do NOT ship: importing one is a missing-DLL crash on a clean machine."""
    assert wer.classify(name, siblings=frozenset()) is None


def test_a_sibling_is_allowed_only_when_named_in_the_sibling_set() -> None:
    assert wer.classify("libwinpthread-1.dll", siblings=frozenset({"libwinpthread-1.dll"})) == "sibling"
    assert wer.classify("LibWinPthread-1.dll", siblings=frozenset({"libwinpthread-1.dll"})) == "sibling"
    assert wer.classify("libwinpthread-1.dll", siblings=frozenset()) is None


def test_check_binary_passes_the_real_exe_and_names_nothing_foreign() -> None:
    lines, bad = wer.check_binary("nexus-service.exe", wer.parse_dependents(_fixture("nexus-service.exe.txt")), frozenset())
    assert bad == []
    assert "nexus-service.exe" in lines[0]


def test_check_binary_reports_every_unclassified_import() -> None:
    deps = ["KERNEL32.dll", "vcruntime140_threads.dll", "libcrypto-3-x64.dll"]
    _, bad = wer.check_binary("tokenizers.dll", deps, frozenset())
    assert [b.lower() for b in bad] == ["vcruntime140_threads.dll", "libcrypto-3-x64.dll"]


# --------------------------------------------------------------------------- #
# Embedded native libraries: extraction from the report's origin jars
# --------------------------------------------------------------------------- #


def _jar(path: Path, members: dict[str, bytes]) -> str:
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path.as_uri()


def _report(*items: tuple[str, list[str]]) -> list[dict]:
    return [{"name": n, "entries": [{"origin": o, "registration_origin": "command line", "size": 1} for o in origins]}
            for n, origins in items]


def test_extract_embedded_pulls_every_dll_and_ignores_other_platforms_and_non_dlls(tmp_path: Path) -> None:
    ort = _jar(tmp_path / "ort.jar", {"ai/onnxruntime/native/win-x64/onnxruntime.dll": b"MZ-ort"})
    djl = _jar(tmp_path / "djl.jar", {
        "native/lib/win-x86_64/cpu/tokenizers.dll": b"MZ-tok",
        "native/lib/win-x86_64/cpu/libwinpthread-1.dll": b"MZ-pth",
    })
    jna = _jar(tmp_path / "jna.jar", {"com/sun/jna/win32-x86-64/jnidispatch.dll": b"MZ-jna",
                                       "com/sun/jna/linux-x86-64/libjnidispatch.so": b"ELF"})
    report = _report(
        ("ai/onnxruntime/native/win-x64/onnxruntime.dll", [ort]),
        ("native/lib/win-x86_64/cpu/tokenizers.dll", [djl]),
        ("native/lib/win-x86_64/cpu/libwinpthread-1.dll", [djl]),
        ("com/sun/jna/win32-x86-64/jnidispatch.dll", [jna]),
        ("com/sun/jna/linux-x86-64/libjnidispatch.so", [jna]),
        ("native/lib/tokenizers.properties", [djl]),
        ("native/lib/win-x86_64/cpu", [djl]),
    )
    out = wer.extract_embedded(report, tmp_path / "x")
    assert sorted(e.resource for e in out) == sorted([
        "ai/onnxruntime/native/win-x64/onnxruntime.dll",
        "native/lib/win-x86_64/cpu/tokenizers.dll",
        "native/lib/win-x86_64/cpu/libwinpthread-1.dll",
        "com/sun/jna/win32-x86-64/jnidispatch.dll",
    ])
    by = {e.resource: e for e in out}
    assert by["native/lib/win-x86_64/cpu/tokenizers.dll"].path.read_bytes() == b"MZ-tok"
    assert by["native/lib/win-x86_64/cpu/tokenizers.dll"].group == "native/lib/win-x86_64/cpu"
    assert by["native/lib/win-x86_64/cpu/libwinpthread-1.dll"].group == by["native/lib/win-x86_64/cpu/tokenizers.dll"].group


def test_extract_embedded_refuses_a_report_with_no_dll(tmp_path: Path) -> None:
    jar = _jar(tmp_path / "a.jar", {"x/y.so": b"ELF"})
    with pytest.raises(wer.CheckError, match="no .dll"):
        wer.extract_embedded(_report(("x/y.so", [jar])), tmp_path / "x")


def test_extract_embedded_refuses_a_missing_origin_jar_and_a_missing_member(tmp_path: Path) -> None:
    gone = (tmp_path / "gone.jar").as_uri()
    with pytest.raises(wer.CheckError, match="gone.jar"):
        wer.extract_embedded(_report(("a/b.dll", [gone])), tmp_path / "x")
    jar = _jar(tmp_path / "a.jar", {"other.dll": b"MZ"})
    with pytest.raises(wer.CheckError, match="a/b.dll"):
        wer.extract_embedded(_report(("a/b.dll", [jar])), tmp_path / "y")


def test_extract_embedded_keeps_two_origins_of_one_resource_apart(tmp_path: Path) -> None:
    j1 = _jar(tmp_path / "1.jar", {"a/b.dll": b"MZ1"})
    j2 = _jar(tmp_path / "2.jar", {"a/b.dll": b"MZ2"})
    out = wer.extract_embedded(_report(("a/b.dll", [j1, j2])), tmp_path / "x")
    assert sorted(e.path.read_bytes() for e in out) == [b"MZ1", b"MZ2"]
    assert len({e.path for e in out}) == 2


WINDOWS_FIXTURES = REPO / "tests" / "fixtures" / "windows_engine"


def test_the_windows_shaped_embedded_resources_fragment_passes_the_checker_and_names_its_libraries() -> None:
    """tests/fixtures/windows_engine/embedded-resources-fragment.json (see PROVENANCE.md: derived, with real
    member sizes and the file:///C:/ origin form GraalVM printed on Windows) against both consumers."""
    report = wer.read_report(WINDOWS_FIXTURES / "embedded-resources-fragment.json")
    assert chk.check_report(report, chk.PLATFORMS["windows-x64"]) == []
    names = {str(i["name"]).lstrip("/") for i in report}
    assert set(chk.PLATFORMS["windows-x64"].required) <= names
    assert all(str(e["origin"]).startswith("file:///C:/") for i in report for e in i["entries"])


def test_extract_embedded_reads_windows_file_uris_through_the_windows_url_to_path_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows urllib maps /C:/Users/... to C:\\Users\\...; nturl2path is that rule on every OS. The jars are
    staged where the mapped path lands (the C:\\Users\\Sam\\.m2 prefix swapped for a temp directory)."""
    import nturl2path

    report = wer.read_report(WINDOWS_FIXTURES / "embedded-resources-fragment.json")
    seen: list[str] = []

    def windows_url2pathname(path: str) -> str:
        win = nturl2path.url2pathname(path)
        seen.append(win)
        assert win.startswith("C:\\"), win
        return win.replace("C:\\Users\\Sam\\.m2", str(tmp_path / "m2")).replace("\\", "/")

    setattr_in(monkeypatch, wer, "urllib.request.url2pathname", windows_url2pathname)
    members: dict[str, dict[str, bytes]] = {}
    for item in report:
        name = str(item["name"]).lstrip("/")
        if not name.endswith(".dll"):
            continue
        origin = windows_url2pathname(wer.urllib.parse.urlparse(item["entries"][0]["origin"]).path)
        members.setdefault(origin, {})[name] = b"MZ-" + name.encode()
    for origin, entries in members.items():
        jar = Path(origin)
        jar.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(jar, "w") as z:
            for n, data in entries.items():
                z.writestr(n, data)
    out = wer.extract_embedded(report, tmp_path / "x")
    assert sorted(e.resource for e in out) == sorted(chk.PLATFORMS["windows-x64"].required)
    assert any(s.startswith("C:\\Users\\Sam\\.m2\\repository\\com\\microsoft\\onnxruntime") for s in seen)


# --------------------------------------------------------------------------- #
# check_deps end to end, with the real captured dumpbin output
# --------------------------------------------------------------------------- #

REAL = {
    "nexus-service.exe": _fixture("nexus-service.exe.txt"),
    "onnxruntime.dll": _fixture("onnxruntime.dll.txt"),
    "onnxruntime4j_jni.dll": _fixture("onnxruntime4j_jni.dll.txt"),
    "tokenizers.dll": _fixture("tokenizers.dll.txt"),
    "libgcc_s_seh-1.dll": _dump("KERNEL32.dll", "msvcrt.dll", "libwinpthread-1.dll"),
    "libstdc++-6.dll": _dump("libgcc_s_seh-1.dll", "KERNEL32.dll", "msvcrt.dll", "libwinpthread-1.dll"),
    "libwinpthread-1.dll": _dump("KERNEL32.dll", "msvcrt.dll"),
    "jnidispatch.dll": _dump("PSAPI.DLL", "KERNEL32.dll"),
}


def _real_layout(tmp_path: Path) -> tuple[Path, list[dict]]:
    exe = tmp_path / "nexus-service.exe"
    exe.write_bytes(b"MZ-exe")
    ort = _jar(tmp_path / "ort.jar", {f"ai/onnxruntime/native/win-x64/{n}": b"MZ" for n in ("onnxruntime.dll", "onnxruntime4j_jni.dll")})
    djl = _jar(tmp_path / "djl.jar", {f"native/lib/win-x86_64/cpu/{n}": b"MZ" for n in
                                       ("tokenizers.dll", "libwinpthread-1.dll", "libstdc++-6.dll", "libgcc_s_seh-1.dll")})
    jna = _jar(tmp_path / "jna.jar", {"com/sun/jna/win32-x86-64/jnidispatch.dll": b"MZ"})
    report = _report(
        *((f"ai/onnxruntime/native/win-x64/{n}", [ort]) for n in ("onnxruntime.dll", "onnxruntime4j_jni.dll")),
        *((f"native/lib/win-x86_64/cpu/{n}", [djl]) for n in ("tokenizers.dll", "libwinpthread-1.dll", "libstdc++-6.dll", "libgcc_s_seh-1.dll")),
        ("com/sun/jna/win32-x86-64/jnidispatch.dll", [jna]),
    )
    return exe, report


def _dumper(table: dict[str, str]):
    def run(path: Path) -> str:
        return table[path.name]
    return run


def test_check_deps_passes_the_real_engine_and_its_embedded_libraries(tmp_path: Path) -> None:
    exe, report = _real_layout(tmp_path)
    rc, lines = wer.check_deps(exe, report, _dumper(REAL), tmp_path / "w")
    assert rc == 0, "\n".join(lines)
    text = "\n".join(lines)
    assert "PASSED" in text
    # the exe and all seven embedded libraries were examined: not a vacuous pass
    assert "8 binaries" in text


def test_check_deps_fails_when_an_embedded_library_imports_a_stray_dll(tmp_path: Path) -> None:
    exe, report = _real_layout(tmp_path)
    table = dict(REAL, **{"tokenizers.dll": _dump("KERNEL32.dll", "VCRUNTIME140.dll", "vcruntime140_threads.dll")})
    rc, lines = wer.check_deps(exe, report, _dumper(table), tmp_path / "w")
    assert rc == 1
    assert any("vcruntime140_threads.dll" in ln and "tokenizers.dll" in ln for ln in lines)


def test_the_exe_gets_no_sibling_allowance(tmp_path: Path) -> None:
    """libwinpthread-1.dll is embedded and extracted beside tokenizers.dll only; the exe importing
    it would find nothing next to nexus-service.exe."""
    exe, report = _real_layout(tmp_path)
    table = dict(REAL, **{"nexus-service.exe": _dump("KERNEL32.dll", "libwinpthread-1.dll")})
    rc, lines = wer.check_deps(exe, report, _dumper(table), tmp_path / "w")
    assert rc == 1 and any("libwinpthread-1.dll" in ln for ln in lines)


def test_a_sibling_in_another_directory_is_not_a_sibling(tmp_path: Path) -> None:
    exe, report = _real_layout(tmp_path)
    table = dict(REAL, **{"onnxruntime4j_jni.dll": _dump("onnxruntime.dll", "libwinpthread-1.dll", "KERNEL32.dll")})
    rc, lines = wer.check_deps(exe, report, _dumper(table), tmp_path / "w")
    assert rc == 1 and any("libwinpthread-1.dll" in ln for ln in lines)


def test_check_deps_fails_when_dumpbin_output_is_unusable(tmp_path: Path) -> None:
    exe, report = _real_layout(tmp_path)
    table = dict(REAL, **{"onnxruntime.dll": "LINK : fatal error LNK1107: invalid or corrupt file"})
    rc, lines = wer.check_deps(exe, report, _dumper(table), tmp_path / "w")
    assert rc == 1 and any("onnxruntime.dll" in ln for ln in lines)


def test_check_deps_fails_on_a_missing_exe(tmp_path: Path) -> None:
    _, report = _real_layout(tmp_path)
    rc, lines = wer.check_deps(tmp_path / "absent.exe", report, _dumper(REAL), tmp_path / "w")
    assert rc == 1 and any("absent.exe" in ln for ln in lines)


def test_check_deps_requires_the_exe_to_import_kernel32_as_a_parser_sanity_check(tmp_path: Path) -> None:
    exe, report = _real_layout(tmp_path)
    table = dict(REAL, **{"nexus-service.exe": _dump("VCRUNTIME140.dll")})
    rc, lines = wer.check_deps(exe, report, _dumper(table), tmp_path / "w")
    assert rc == 1 and any("KERNEL32" in ln.upper() for ln in lines)


def test_the_embedded_resources_checker_requires_the_same_dlls_this_check_examines() -> None:
    """Both read the report: every library the zz2w7 checker requires for windows-x64 is a .dll
    this check would extract, so the two gates cannot disagree about what is embedded."""
    required = chk.PLATFORMS["windows-x64"].required
    assert required and all(r.lower().endswith(".dll") for r in required)
    assert all(wer.is_embedded_dll(r) for r in required)
    assert not wer.is_embedded_dll("com/sun/jna/linux-x86-64/libjnidispatch.so")
    assert not wer.is_embedded_dll("native/lib/win-x86_64/cpu")


# --------------------------------------------------------------------------- #
# find_dumpbin
# --------------------------------------------------------------------------- #


def test_find_dumpbin_prefers_the_newest_msvc_toolset(tmp_path: Path) -> None:
    for ver in ("14.38.33130", "14.44.35207", "14.9.1"):
        d = tmp_path / "VC" / "Tools" / "MSVC" / ver / "bin" / "Hostx64" / "x64"
        d.mkdir(parents=True)
        (d / "dumpbin.exe").write_bytes(b"")
    assert wer.find_dumpbin(tmp_path).parts[-5] == "14.44.35207"


def test_find_dumpbin_names_the_missing_component(tmp_path: Path) -> None:
    with pytest.raises(wer.CheckError, match="dumpbin"):
        wer.find_dumpbin(tmp_path)


# --------------------------------------------------------------------------- #
# Packaging
# --------------------------------------------------------------------------- #


def _redist(tmp_path: Path) -> bw.Redist:
    d = tmp_path / "redist" / "Microsoft.VC143.CRT"
    d.mkdir(parents=True)
    for dll in bw.VC_RUNTIME_DLLS:
        (d / dll).write_bytes(b"MZ-" + dll.encode())
    return bw.Redist(d, "14.44.35112")


def _packaged(tmp_path: Path) -> tuple[Path, Path]:
    exe = tmp_path / "nexus-service.exe"
    exe.write_bytes(b"MZ-engine" * 1000)
    return exe, wer.package(exe, _redist(tmp_path), tmp_path / "dist", min_exe_bytes=1)


def test_package_writes_a_flat_archive_with_the_exe_the_four_dlls_and_the_notice(tmp_path: Path) -> None:
    exe, archive = _packaged(tmp_path)
    assert archive.name == "nexus-service-windows-x64.txz"
    with tarfile.open(archive, "r:xz") as tf:
        names = sorted(m.name for m in tf)
        assert all(m.isreg() for m in tf.getmembers())
    assert names == sorted(["nexus-service.exe", *bw.VC_RUNTIME_DLLS, "THIRD-PARTY-NOTICES.txt"])
    assert not any("/" in n or "\\" in n for n in names)


def test_package_copies_every_file_unmodified(tmp_path: Path) -> None:
    exe, archive = _packaged(tmp_path)
    with tarfile.open(archive, "r:xz") as tf:
        assert tf.extractfile("nexus-service.exe").read() == exe.read_bytes()  # type: ignore[union-attr]
        for dll in bw.VC_RUNTIME_DLLS:
            assert tf.extractfile(dll).read() == b"MZ-" + dll.encode()  # type: ignore[union-attr]


def test_package_sha256_first_token_is_the_archives_digest(tmp_path: Path) -> None:
    _, archive = _packaged(tmp_path)
    sidecar = archive.with_name(archive.name + ".sha256").read_text(encoding="utf-8")
    assert sidecar == f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n"


def test_the_notice_carries_the_p06_conditions(tmp_path: Path) -> None:
    _, archive = _packaged(tmp_path)
    with tarfile.open(archive, "r:xz") as tf:
        notice = tf.extractfile("THIRD-PARTY-NOTICES.txt").read().decode()  # type: ignore[union-attr]
    for dll in bw.VC_RUNTIME_DLLS:
        assert dll in notice
        assert hashlib.sha256(b"MZ-" + dll.encode()).hexdigest() in notice
    assert "14.44.35112" in notice
    assert "unmodified" in notice and "https://aka.ms/vs/17/redistribution" in notice
    assert "AGPL" in notice


def test_the_real_client_places_the_packaged_archive(tmp_path: Path) -> None:
    """The consumer, not a copy of its rules: nexus.daemon.binary_install places the exe LAST and
    the four DLLs beside it, tolerates the notice, and reports the digests we wrote."""
    exe, archive = _packaged(tmp_path)
    dest = tmp_path / "service" / "nexus-service.exe"
    receipt = binary_install._place_engine_archive(archive, dest)
    assert dest.read_bytes() == exe.read_bytes()
    for dll in bw.VC_RUNTIME_DLLS:
        assert (dest.parent / dll).read_bytes() == b"MZ-" + dll.encode()
    assert receipt["layout"] == "archive"
    assert receipt["installed_sha256"] == hashlib.sha256(exe.read_bytes()).hexdigest()
    assert not (dest.parent / "THIRD-PARTY-NOTICES.txt").exists()


def test_package_refuses_an_absent_or_tiny_exe(tmp_path: Path) -> None:
    redist = _redist(tmp_path)
    with pytest.raises(wer.CheckError, match="absent"):
        wer.package(tmp_path / "absent.exe", redist, tmp_path / "d")
    tiny = tmp_path / "t.exe"
    tiny.write_bytes(b"MZ")
    with pytest.raises(wer.CheckError, match="small"):
        wer.package(tiny, redist, tmp_path / "d", min_exe_bytes=1000)


def test_package_refuses_a_redist_missing_a_dll(tmp_path: Path) -> None:
    redist = _redist(tmp_path)
    (redist.directory / "msvcp140_1.dll").unlink()
    exe = tmp_path / "nexus-service.exe"
    exe.write_bytes(b"MZ" * 100)
    with pytest.raises(wer.CheckError, match="msvcp140_1.dll"):
        wer.package(exe, redist, tmp_path / "d", min_exe_bytes=1)


def test_verify_archive_accepts_the_packaged_archive(tmp_path: Path) -> None:
    _, archive = _packaged(tmp_path)
    assert wer.verify_archive(archive) == []


def _write_tar(path: Path, members: dict[str, bytes]) -> Path:
    with lzma.open(path, "wb") as xz, tarfile.open(fileobj=xz, mode="w") as tf:
        for name, data in members.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return path


def _write_members(path: Path, members: list[tuple[str, str, bytes]]) -> Path:
    """Like :func:`_write_tar` but ordered, repeatable and typed: ``(name, kind, data)`` with kind one of
    ``file``, ``dir``, ``symlink`` (data is the link target)."""
    with lzma.open(path, "wb") as xz, tarfile.open(fileobj=xz, mode="w") as tf:
        for name, kind, data in members:
            ti = tarfile.TarInfo(name)
            if kind == "dir":
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            elif kind == "symlink":
                ti.type = tarfile.SYMTYPE
                ti.linkname = data.decode()
                tf.addfile(ti)
            else:
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
    return path


GOOD = {"nexus-service.exe": b"MZ", **{d: b"MZ" for d in bw.VC_RUNTIME_DLLS}, "THIRD-PARTY-NOTICES.txt": b"n"}


@pytest.mark.parametrize(
    "mutate, expect",
    [
        (lambda m: {k: v for k, v in m.items() if k != "msvcp140.dll"}, "msvcp140.dll"),
        (lambda m: {k: v for k, v in m.items() if k != "nexus-service.exe"}, "nexus-service.exe"),
        (lambda m: {k: v for k, v in m.items() if k != "THIRD-PARTY-NOTICES.txt"}, "THIRD-PARTY-NOTICES.txt"),
        (lambda m: {**m, "bundle/extra.dll": b"x"}, "nested"),
        (lambda m: {**m, "nexus-service.pdb": b"x"}, "pdb"),
        (lambda m: {**m, "vcruntime140d.dll": b"x"}, "vcruntime140d.dll"),
        (lambda m: {**m, "msvcp140.dll": b""}, "empty"),
    ],
    ids=["no-dll", "no-exe", "no-notice", "nested", "pdb", "debug-runtime", "empty-member"],
)
def test_verify_archive_names_each_defect(tmp_path: Path, mutate, expect: str) -> None:
    problems = wer.verify_archive(_write_tar(tmp_path / "a.txz", mutate(GOOD)))
    assert problems and any(expect in p for p in problems), problems


_GOOD_LIST = [(k, "file", v) for k, v in {"nexus-service.exe": b"MZ", **{d: b"MZ" for d in bw.VC_RUNTIME_DLLS},
                                           "THIRD-PARTY-NOTICES.txt": b"n"}.items()]


@pytest.mark.parametrize(
    "extra, expect",
    [
        ([("bundle", "dir", b"")], "nested directory member 'bundle'"),
        ([("msvcp140.dll", "file", b"MZ")], "duplicate member 'msvcp140.dll'"),
        ([("link.dll", "symlink", b"msvcp140.dll")], "non-regular member 'link.dll'"),
        ([("other.exe", "file", b"MZ")], "other.exe: an executable other than nexus-service.exe"),
        ([("OTHER.EXE", "file", b"MZ")], "OTHER.EXE: an executable other than nexus-service.exe"),
    ],
    ids=["nested-dir", "duplicate", "symlink", "extra-exe", "extra-exe-upper-case"],
)
def test_verify_archive_names_each_structural_defect(tmp_path: Path, extra, expect: str) -> None:
    """The member-by-member checks: a directory, a repeated name, a non-regular member and a second
    executable each get their own message, and the good archive without them has none."""
    assert wer.verify_archive(_write_members(tmp_path / "good.txz", _GOOD_LIST)) == []
    problems = wer.verify_archive(_write_members(tmp_path / "bad.txz", _GOOD_LIST + extra))
    assert expect in problems, problems


def test_verify_archive_reports_an_unreadable_archive(tmp_path: Path) -> None:
    bad = tmp_path / "a.txz"
    bad.write_bytes(b"not xz")
    assert wer.verify_archive(bad)


# --------------------------------------------------------------------------- #
# release_version stamping (the bash step's job, for a runner with no bash)
# --------------------------------------------------------------------------- #


def test_stamp_replaces_the_release_version_line_and_keeps_the_header() -> None:
    src = "# header\n# more\nrelease_version=\nother=1\n"
    assert wer.stamp_release_version(src, "0.1.150") == "# header\n# more\nother=1\nrelease_version=0.1.150\n"


def test_stamp_appends_when_the_line_is_absent_and_is_idempotent() -> None:
    once = wer.stamp_release_version("# h\n", "1.2.3")
    assert once == "# h\nrelease_version=1.2.3\n"
    assert wer.stamp_release_version(once, "1.2.3") == once


def test_stamp_normalises_crlf_input() -> None:
    assert wer.stamp_release_version("# h\r\nrelease_version=\r\n", "1.2.3") == "# h\nrelease_version=1.2.3\n"


@pytest.mark.parametrize("bad", ["", "a b", "1.2.3\nevil=1", "1.2.3;rm", "v1.2.3"])
def test_stamp_refuses_a_version_that_is_not_dotted_digits(bad: str) -> None:
    with pytest.raises(wer.CheckError):
        wer.stamp_release_version("x\n", bad)


def test_the_stamp_matches_the_real_properties_file_shape() -> None:
    props = (REPO / "service/src/main/resources/META-INF/nexus/release.properties").read_text(encoding="utf-8")
    out = wer.stamp_release_version(props, "9.9.9")
    stamped = [ln for ln in out.splitlines() if ln.startswith("release_version=")]
    assert stamped == ["release_version=9.9.9"] and out.endswith("release_version=9.9.9\n")
    # every other line, comments and build_ref included, is kept in order
    assert [ln for ln in out.splitlines() if not ln.startswith("release_version=")] == [
        ln for ln in props.splitlines() if not ln.startswith("release_version=")
    ]


# --------------------------------------------------------------------------- #
# MSVC environment export
# --------------------------------------------------------------------------- #


def test_github_env_lines_carry_the_build_environment_and_skip_the_runner_own_vars() -> None:
    env = {"PATH": r"C:\vs\bin;C:\Windows", "INCLUDE": r"C:\inc", "LIB": r"C:\lib", "GITHUB_ENV": "x", "GITHUB_PATH": "y",
           "RUNNER_TEMP": "t", "BAD NAME": "v", "MULTI": "a\nb"}
    lines = wer.github_env_lines(env)
    assert "PATH=C:\\vs\\bin;C:\\Windows" in lines
    assert "INCLUDE=C:\\inc" in lines and "LIB=C:\\lib" in lines
    assert not any(ln.startswith(("GITHUB_", "RUNNER_", "BAD NAME", "MULTI")) for ln in lines)


def test_github_env_lines_export_only_what_vcvars_changed_comparing_names_case_insensitively() -> None:
    base = {"Path": r"C:\Windows", "TEMP": r"C:\t", "JAVA_HOME": r"C:\jdk"}
    after = {"PATH": r"C:\vs\bin;C:\Windows", "TEMP": r"C:\t", "JAVA_HOME": r"C:\jdk", "INCLUDE": r"C:\inc"}
    assert wer.github_env_lines(after, base) == [f"PATH=C:\\vs\\bin;C:\\Windows", "INCLUDE=C:\\inc"]
    assert wer.github_env_lines(after, after) == []


# --------------------------------------------------------------------------- #
# CLI plumbing
# --------------------------------------------------------------------------- #


def test_main_check_deps_exits_nonzero_with_a_message_when_the_report_is_missing(tmp_path: Path, capsys) -> None:
    rc = wer.main(["check-deps", "--exe", str(tmp_path / "e.exe"), "--report", str(tmp_path / "none.json"),
                   "--dumpbin", str(tmp_path / "d.exe")])
    assert rc == 1
    assert "report" in capsys.readouterr().err


def test_main_verify_archive_exit_codes(tmp_path: Path) -> None:
    good = _write_tar(tmp_path / "good.txz", GOOD)
    assert wer.main(["verify-archive", "--archive", str(good)]) == 0
    bad = _write_tar(tmp_path / "bad.txz", {"nexus-service.exe": b"MZ"})
    assert wer.main(["verify-archive", "--archive", str(bad)]) == 1


def test_main_stamp_rewrites_the_file(tmp_path: Path) -> None:
    p = tmp_path / "release.properties"
    p.write_text("# h\nrelease_version=\n")
    assert wer.main(["stamp", "--file", str(p), "--version", "0.1.7"]) == 0
    assert p.read_text(encoding="utf-8") == "# h\nrelease_version=0.1.7\n"


def test_report_json_round_trips_through_the_cli_reader(tmp_path: Path) -> None:
    p = tmp_path / "r.json"
    p.write_text(json.dumps([{"name": "a.dll", "entries": []}]))
    assert wer.read_report(p) == [{"name": "a.dll", "entries": []}]
    p.write_text("{")
    with pytest.raises(wer.CheckError):
        wer.read_report(p)


# --------------------------------------------------------------------------- #
# Windows reads text in the ANSI code page unless told otherwise
# --------------------------------------------------------------------------- #


def _unencoded_text_io(source: str) -> list[int]:
    """Line numbers of read_text / write_text / text-mode open calls that do not pass ``encoding=``."""
    bad: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        first = node.args[0] if node.args else None
        mode = first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else ""
        text_open = node.func.attr == "open" and mode in {"r", "w", "a", "rt", "wt", "at"}
        if (node.func.attr in {"read_text", "write_text"} or text_open) and "encoding" not in {
            k.arg for k in node.keywords
        }:
            bad.append(node.lineno)
    return bad


@pytest.mark.parametrize("script", ["windows_engine_release.py", "engine_windows_smoke.py"])
def test_every_text_read_and_write_names_its_encoding(script: str) -> None:
    """The first real Windows run of the smoke died with UnicodeDecodeError: cp1252 cannot decode
    a UTF-8 byte in a changelog file, a defect no run on a UTF-8 laptop can show. Pinned by reading
    the source: every read_text / write_text / text-mode open must say ``encoding=``."""
    bad = _unencoded_text_io((REPO / "scripts" / script).read_text(encoding="utf-8"))
    assert not bad, f"{script}: text I/O without encoding= at lines {bad}"


def test_the_encoding_pin_can_fail() -> None:
    """Non-vacuity: the walk flags each bare form and passes the encoded and the binary ones."""
    assert _unencoded_text_io("p.read_text()\nq.write_text('x')\nr.open('r')") == [1, 2, 3]
    assert _unencoded_text_io("p.read_text(encoding='utf-8')\nq.open('rb')\nlzma.open(a, 'wb')") == []


# --------------------------------------------------------------------------- #
# nexus-f9bgu.27: review findings
# --------------------------------------------------------------------------- #


def test_package_refuses_a_runtime_that_is_not_validly_signed_by_microsoft(tmp_path: Path) -> None:
    exe = tmp_path / "nexus-service.exe"
    exe.write_bytes(b"MZ-engine" * 1000)
    redist = _redist(tmp_path)
    for reader, needle in (
        (lambda p: bw.Signature("NotSigned", ""), "NotSigned"),
        (lambda p: bw.Signature("Valid", "CN=Contoso Ltd, C=US"), "Contoso"),
    ):
        dist = tmp_path / f"dist-{needle}"
        with pytest.raises(wer.CheckError, match=needle):
            wer.package(exe, redist, dist, min_exe_bytes=1, signature_reader=reader)
        assert not (dist / wer.ASSET_NAME).exists(), "no archive is left behind for a refused runtime"


@pytest.mark.parametrize(
    "name",
    ["C:evil.exe", "c:", "..", "a..b.dll", "a:stream", "dir/file.dll", "dir\\file.dll", "."],
)
def test_a_member_name_that_is_not_a_bare_file_name_is_refused(tmp_path: Path, name: str) -> None:
    assert wer.bare_name_problem(name) is not None
    assert wer.bare_name_problem("") is not None
    problems = wer.verify_archive(_write_tar(tmp_path / "a.txz", {**GOOD, name: b"x"}))
    assert problems, name


@pytest.mark.parametrize("name", ["nexus-service.exe", "vcruntime140.dll", "THIRD-PARTY-NOTICES.txt", "a-b_c.1.txt"])
def test_a_bare_file_name_passes(name: str) -> None:
    assert wer.bare_name_problem(name) is None


def test_the_engine_notice_names_every_embedded_windows_library_with_its_licence(tmp_path: Path) -> None:
    """Every Windows DLL the jars the image embeds ship (the committed listing) has a line in the notice,
    so a dependency bump that adds one cannot ship without a licence entry."""
    listing = (REPO / "tests" / "fixtures" / "native_jar_listings.txt").read_text(encoding="utf-8")
    dlls = {
        Path(ln).name for ln in listing.splitlines()
        if ln and not ln.startswith("#") and ("/win-" in ln) and ln.lower().endswith(".dll")
    }
    assert len(dlls) >= 6, f"non-vacuity: the listing names the windows libraries ({sorted(dlls)})"
    notice = wer.notice_text(_redist(tmp_path), {d: "0" * 64 for d in bw.VC_RUNTIME_DLLS})
    for dll in sorted(dlls):
        assert dll in notice, f"{dll} is embedded in the engine but has no THIRD-PARTY-NOTICES line"
    for needle in ("ONNX Runtime", "MIT", "DJL", "Apache License 2.0", "GCC Runtime Library Exception", "JNA"):
        assert needle in notice, needle
    assert "source distribution" in notice


def test_check_deps_removes_its_extraction_directory_pass_or_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tempfile

    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_root))
    report = tmp_path / "r.json"
    report.write_text("[]")
    seen: list[Path] = []
    outcome = {"rc": 0}

    def fake_check(exe, rep, dumper, work):  # noqa: ANN001
        seen.append(work)
        (work / "x").mkdir(parents=True)
        (work / "x" / "dep.dll").write_bytes(b"MZ")
        return outcome["rc"], ["line"]

    monkeypatch.setattr(wer, "check_deps", fake_check)
    argv = ["check-deps", "--exe", "e.exe", "--report", str(report), "--dumpbin", "d.exe"]
    assert wer.main(argv) == 0
    outcome["rc"] = 1
    assert wer.main(argv) == 1
    assert len(seen) == 2 and all(w.name.startswith("engine-deps-") and not w.exists() for w in seen)
    outcome["rc"] = 0
    assert wer.main([*argv, "--keep-work-dir"]) == 0
    assert seen[-1].is_dir(), "--keep-work-dir keeps it"
    named = tmp_path / "mine"
    assert wer.main([*argv, "--workdir", str(named)]) == 0
    assert named.is_dir(), "a directory the caller named is the caller's"


def test_check_deps_extracts_under_runner_temp_when_the_runner_sets_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report = tmp_path / "r.json"
    report.write_text("[]")
    rt = tmp_path / "runner-temp"
    rt.mkdir()
    seen: list[Path] = []
    monkeypatch.setattr(wer, "check_deps", lambda exe, rep, dumper, work: (seen.append(work), (0, ["ok"]))[1])
    assert wer.main(["check-deps", "--exe", "e.exe", "--report", str(report), "--dumpbin", "d.exe", "--keep-work-dir"],
                    env={"RUNNER_TEMP": str(rt)}) == 0
    assert seen[0].parent == rt


def test_check_deps_removes_its_directory_when_the_check_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tempfile

    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_root))
    report = tmp_path / "r.json"
    report.write_text("[]")

    def boom(exe, rep, dumper, work):  # noqa: ANN001
        (work / "x").mkdir(parents=True)
        raise wer.CheckError("dumpbin blew up")

    monkeypatch.setattr(wer, "check_deps", boom)
    assert wer.main(["check-deps", "--exe", "e.exe", "--report", str(report), "--dumpbin", "d.exe"]) == 1
    assert list(tmp_root.iterdir()) == []
