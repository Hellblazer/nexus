#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The windows-x64 engine release leg's steps (RDR-224 P1.2, bead nexus-f9bgu.9).

Stdlib only, Python 3.12+, with the process runner and the platform injected so
every decision runs under pytest on any OS (tests/test_windows_engine_release.py).
The leg's runner (``win-release``) has no bash; this is what the bash steps of
the other three legs do, written once.

Subcommands::

    vcvars          export the MSVC build environment (vcvars64.bat) to
                    $GITHUB_ENV so GraalVM native-image finds cl and link
    stamp           write release_version into release.properties (tag runs)
    check-deps      dumpbin /dependents on nexus-service.exe AND on every native
                    library the image embeds; fail on any import that is not one
                    of the four shipped VC++ DLLs, a Windows system DLL, or (for
                    an embedded library) a sibling extracted beside it
    package         exe + the four VC++ DLLs + the notice -> nexus-service-windows-x64.txz
                    (+ .sha256), the P0.4 layout
    verify-archive  assert an archive has exactly that layout

Why the dependency check reads the embedded-resources report rather than a
hard-coded list. The image embeds native libraries (ONNX Runtime, DJL's
tokenizers and its three MinGW DLLs, JNA's jnidispatch) and extracts them at run
time; checking only the exe misses msvcp140.dll and msvcp140_1.dll, which only
onnxruntime.dll imports (bead nexus-f9bgu.9, RDR-224 research-4). GraalVM's
``-H:+GenerateEmbeddedResourcesFile`` report names every embedded resource with
the jar it came from, so the set checked is the set shipped, and a new embedded
DLL is checked the day it appears. Each library is pulled from its origin jar:
the same bytes the image embedded.

The Windows-system allowlist is closed and was MEASURED, not recalled: the
union of the imports of the exe and of every embedded DLL in the 2026-10-05
build on native Windows 11 (qwentescence), plus the API-set schema names
(api-ms-win-*, ext-ms-win-*), which the OS resolves. A new import outside it
fails the leg and gets a reviewed one-line addition: the failure is the
prompt to ask whether that DLL really ships with every Windows the client
supports.

Environment: none read implicitly except GITHUB_ENV (vcvars), RUNNER_TEMP (the
default parent of ``check-deps``' temporary extraction directory, which is removed when
the check ends unless --keep-work-dir) and the Visual Studio location (VS_INSTALL_PATH,
as build_pg_bundle_windows.py). ``package`` refuses a runtime DLL that is not validly
signed by Microsoft Corporation (build_pg_bundle_windows.copy_runtime_dlls).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import build_pg_bundle_windows as bw

ARCH = "windows-x64"
ASSET_NAME = f"nexus-service-{ARCH}.txz"
ENGINE_EXE = "nexus-service.exe"
NOTICE_NAME = "THIRD-PARTY-NOTICES.txt"
#: Same four files, same routine as the PG bundle (one source of truth: P0.6).
VC_RUNTIME_DLLS: tuple[str, ...] = bw.VC_RUNTIME_DLLS
#: An engine exe is ~130 MB; a truncated link that still has an MZ header is not shippable.
MIN_EXE_BYTES = 20_000_000
#: What the 2026-10-05 build's exe and embedded libraries import from Windows (lowercase).
SYSTEM_DLLS: frozenset[str] = frozenset(
    {
        "advapi32.dll", "bcrypt.dll", "bcryptprimitives.dll", "crypt32.dll", "iphlpapi.dll",
        "kernel32.dll", "msvcrt.dll", "mswsock.dll", "ncrypt.dll", "ntdll.dll", "ole32.dll",
        "psapi.dll", "secur32.dll", "shell32.dll", "user32.dll", "userenv.dll", "version.dll",
        "winhttp.dll", "ws2_32.dll",
    }
)
API_SET_RE = re.compile(r"^(?:api|ext)-ms-win-[a-z0-9-]+-l\d+-\d+-\d+\.dll$", re.IGNORECASE)
_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*$")
_DEP_SECTION_RE = re.compile(r"Image has the following (?:delay load )?dependencies:", re.IGNORECASE)
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_()]*$")
_RUNNER_OWN_ENV = ("GITHUB_", "RUNNER_", "ACTIONS_", "INPUT_")


class CheckError(RuntimeError):
    """A check failed or a precondition does not hold."""


# --------------------------------------------------------------------------- #
# dumpbin
# --------------------------------------------------------------------------- #


def parse_dependents(text: str) -> list[str]:
    """DLL names from ``dumpbin /dependents`` output, delay-load section included.

    Raises when no dependency section is present: a dumpbin that failed or printed
    only its banner must not read as "imports nothing"."""
    deps: list[str] = []
    in_section = False
    saw_section = False
    for raw in text.splitlines():
        line = raw.strip()
        if _DEP_SECTION_RE.search(line):
            in_section = True
            saw_section = True
            continue
        if line.lower().startswith("summary"):
            in_section = False
            continue
        if in_section and line.lower().endswith(".dll"):
            deps.append(line)
    if not saw_section:
        raise CheckError("dumpbin output has no 'Image has the following dependencies' section")
    return deps


def classify(name: str, *, siblings: frozenset[str]) -> str | None:
    """``vc`` | ``system`` | ``sibling``, or None when the import is not allowed.

    *siblings* are the lowercase basenames extracted into the same directory as
    the importing library; the exe has none."""
    low = name.strip().lower()
    if low in {d.lower() for d in VC_RUNTIME_DLLS}:
        return "vc"
    if low in SYSTEM_DLLS or API_SET_RE.match(low):
        return "system"
    if low in siblings:
        return "sibling"
    return None


def check_binary(
    label: str, deps: Sequence[str], siblings: frozenset[str]
) -> tuple[list[str], list[str]]:
    """``(report_lines, unclassified_imports)`` for one binary."""
    counts = {"vc": 0, "system": 0, "sibling": 0}
    bad: list[str] = []
    for dep in deps:
        kind = classify(dep, siblings=siblings)
        if kind is None:
            bad.append(dep)
        else:
            counts[kind] += 1
    summary = (
        f"{label}: {len(deps)} imports ({counts['vc']} VC++, {counts['system']} system, "
        f"{counts['sibling']} embedded sibling)"
    )
    lines = [("FAIL " if bad else "ok   ") + summary]
    lines += [f"FAIL {label}: imports {b}, which is not shipped and not a Windows system DLL" for b in bad]
    return lines, bad


# --------------------------------------------------------------------------- #
# Embedded native libraries
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Embedded:
    resource: str
    group: str
    path: Path


def is_embedded_dll(name: str) -> bool:
    """A resource the image embeds that Windows would load: ``*.dll``, not a directory entry."""
    return name.lower().endswith(".dll")


def read_report(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CheckError(f"embedded-resources report {path} is unreadable: {exc}") from exc
    if not isinstance(data, list):
        raise CheckError(f"embedded-resources report {path} is not a JSON list")
    return data


def _origin_path(origin: str) -> Path:
    parsed = urllib.parse.urlparse(origin)
    if parsed.scheme != "file":
        raise CheckError(f"origin {origin!r} is not a file: URI; cannot read the embedded library")
    return Path(urllib.request.url2pathname(parsed.path))


def extract_embedded(report: Sequence[Mapping], dest: Path) -> list[Embedded]:
    """Every embedded ``.dll`` of the report, extracted from its origin jar into *dest*.

    JNA's per-platform libraries are included: the Windows one is embedded and
    loaded, so it is checked like the rest. Raises when the report names no DLL
    (a report format change must not read as a clean check)."""
    dest.mkdir(parents=True, exist_ok=True)
    out: list[Embedded] = []
    for item in report:
        name = str(item.get("name", "")).lstrip("/") if isinstance(item, Mapping) else ""
        if not name or not is_embedded_dll(name):
            continue
        for index, entry in enumerate(item.get("entries") or []):
            jar = _origin_path(str(entry.get("origin", "")))
            if not jar.is_file():
                raise CheckError(f"origin jar {jar} of {name} does not exist")
            try:
                with zipfile.ZipFile(jar) as zf:
                    data = zf.read(name)
            except (KeyError, zipfile.BadZipFile) as exc:
                raise CheckError(f"{name} is not readable from {jar}: {exc}") from exc
            target = dest / f"{len(out):02d}-{index}" / Path(name).name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            out.append(Embedded(name, name.rsplit("/", 1)[0] if "/" in name else "", target))
    if not out:
        raise CheckError("the embedded-resources report names no .dll: nothing to check (vacuous)")
    return out


def find_dumpbin(vs_path: Path) -> Path:
    """The newest MSVC toolset's x64-hosted x64 dumpbin.exe under a Visual Studio install."""
    found = list((vs_path / "VC" / "Tools" / "MSVC").glob("*/bin/Hostx64/x64/dumpbin.exe"))
    if not found:
        raise CheckError(f"dumpbin.exe not found under {vs_path / 'VC' / 'Tools' / 'MSVC'}")

    def version(p: Path) -> tuple[int, ...]:
        ver = p.parts[-5]
        return tuple(int(x) for x in ver.split(".")) if re.fullmatch(r"\d+(\.\d+)*", ver) else (0,)

    return max(found, key=version)


Dumper = Callable[[Path], str]


def run_dumpbin(dumpbin: Path) -> Dumper:
    def dump(path: Path) -> str:
        proc = subprocess.run(
            [str(dumpbin), "/nologo", "/dependents", str(path)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
        )
        if proc.returncode != 0:
            raise CheckError(f"dumpbin failed ({proc.returncode}) on {path.name}: {(proc.stdout + proc.stderr).strip()[:300]}")
        return proc.stdout

    return dump


def check_deps(
    exe: Path, report: Sequence[Mapping], dumper: Dumper, workdir: Path
) -> tuple[int, list[str]]:
    """``(exit_code, report_lines)``: dumpbin the exe and every embedded DLL against the allowlist."""
    lines: list[str] = []
    failed = 0
    if not exe.is_file():
        return 1, [f"FAIL {exe} not found: nothing to check"]
    try:
        embedded = extract_embedded(report, workdir)
    except CheckError as exc:
        return 1, [f"FAIL {exc}"]

    def examine(label: str, path: Path, siblings: frozenset[str], must_import: str | None = None) -> None:
        nonlocal failed
        try:
            deps = parse_dependents(dumper(path))
        except CheckError as exc:
            failed += 1
            lines.append(f"FAIL {label}: {exc}")
            return
        if must_import and must_import.lower() not in {d.lower() for d in deps}:
            failed += 1
            lines.append(
                f"FAIL {label}: does not import {must_import}; the parser or the dumpbin output is "
                f"not what this check assumes ({len(deps)} imports read)"
            )
            return
        out, bad = check_binary(label, deps, siblings)
        lines.extend(out)
        failed += len(bad)

    examine(exe.name, exe, frozenset(), must_import="KERNEL32.dll")
    for lib in embedded:
        sibs = frozenset(
            e.path.name.lower() for e in embedded if e.group == lib.group and e.path != lib.path
        )
        examine(lib.path.name, lib.path, sibs)
    total = 1 + len(embedded)
    if failed:
        lines.append(f"FAILED: {failed} problem(s) across {total} binaries")
        return 1, lines
    lines.append(f"PASSED: {total} binaries (the exe and {len(embedded)} embedded libraries) import only shipped or system DLLs")
    return 0, lines


# --------------------------------------------------------------------------- #
# Packaging (P0.4 layout: one flat archive)
# --------------------------------------------------------------------------- #


#: The native libraries the exe embeds and extracts (the embedded-resources report names them
#: and ``check-deps`` dumps every one), with the licence each is published under
#: (nexus-f9bgu.27, code review m6). A test pins the file names against the committed jar listings,
#: so an embedded library added by a dependency bump cannot ship without a line here.
EMBEDDED_NOTICE = """   ONNX Runtime 1.20.0 (onnxruntime.dll, onnxruntime4j_jni.dll)
       MIT License. https://github.com/microsoft/onnxruntime

   DJL Hugging Face tokenizers 0.30.0 (tokenizers.dll)
       Apache License 2.0. https://github.com/deepjavalibrary/djl

   MinGW-w64 runtime libraries that tokenizers.dll links (libgcc_s_seh-1.dll,
   libstdc++-6.dll, libwinpthread-1.dll)
       libgcc_s_seh-1.dll and libstdc++-6.dll: GNU General Public License v3 or
       later with the GCC Runtime Library Exception, version 3.1; source at
       https://gcc.gnu.org. libwinpthread-1.dll: the mingw-w64 licence (MIT
       style), https://www.mingw-w64.org.

   Java Native Access, JNA (its jnidispatch library)
       Apache License 2.0 or LGPL 2.1 or later, at your option.
       https://github.com/java-native-access/jna
"""


def notice_text(redist: bw.Redist, digests: Mapping[str, str]) -> str:
    dll_lines = "\n".join(f"    {n}  sha256 {digests[n]}" for n in VC_RUNTIME_DLLS)
    return f"""THIRD-PARTY NOTICES for the nexus engine service ({ARCH})

1. Microsoft Visual C++ runtime ({', '.join(VC_RUNTIME_DLLS)})

   These four files are Microsoft's. They are NOT covered by the AGPL-3.0 that
   covers nexus. They are copied unmodified from Visual Studio 2022's
   redistributable folder (VC redist {redist.version}) and are distributed with
   this program under the Distributable Code terms of the Microsoft Visual
   Studio 2022 license (https://aka.ms/vs/17/redistribution). Those terms
   permit use only as part of this program: you may not modify these files,
   reverse engineer them, or redistribute them separately from it.

   Files shipped beside {ENGINE_EXE}:
{dll_lines}

2. Native components embedded in {ENGINE_EXE} and extracted beside it at run time
   in a temporary directory. Each keeps the licence it is published under; none
   is modified here.

{EMBEDDED_NOTICE}
3. Everything else linked into {ENGINE_EXE} (the GraalVM runtime and the Java
   libraries) keeps its own licence; see the nexus source distribution for the list.
"""


def package(
    exe: Path,
    redist: bw.Redist,
    out_dir: Path,
    *,
    min_exe_bytes: int = MIN_EXE_BYTES,
    signature_reader: bw.SignatureReader | None = None,
) -> Path:
    """exe + four DLLs + notice -> ``nexus-service-windows-x64.txz`` and its ``.sha256``.

    Flat layout, exactly what ``nexus.daemon.binary_install._place_engine_archive``
    reads. The DLLs go through ``bw.copy_runtime_dlls``, the PG bundle's copy
    routine, into a staging directory first, so the P0.6 unmodified-copy check
    is the same code for both artifacts."""
    if not exe.is_file():
        raise CheckError(f"engine exe {exe} is absent")
    size = exe.stat().st_size
    if size < min_exe_bytes:
        raise CheckError(f"engine exe is suspiciously small ({size} bytes < {min_exe_bytes}): a truncated build")
    missing = [d for d in VC_RUNTIME_DLLS if not (redist.directory / d).is_file()]
    if missing:
        raise CheckError(f"redist {redist.directory} lacks {', '.join(missing)}")
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / ASSET_NAME
    with tempfile.TemporaryDirectory(prefix="engine-stage-") as stage_name:
        stage = Path(stage_name)
        try:
            digests = bw.copy_runtime_dlls(stage, redist, signature_reader=signature_reader)
        except bw.BuildError as exc:
            raise CheckError(str(exc)) from exc
        (stage / NOTICE_NAME).write_text(notice_text(redist, digests), encoding="utf-8", newline="\n")
        members = [(exe, ENGINE_EXE), *((stage / d, d) for d in VC_RUNTIME_DLLS), (stage / NOTICE_NAME, NOTICE_NAME)]
        with lzma.open(archive, "wb", preset=6) as xz, tarfile.open(fileobj=xz, mode="w") as tf:
            for src, name in members:
                info = tarfile.TarInfo(name)
                info.size = src.stat().st_size
                info.mode = 0o644
                info.mtime = 0
                with src.open("rb") as fh:
                    tf.addfile(info, fh)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (out_dir / f"{ASSET_NAME}.sha256").write_text(f"{digest}  {ASSET_NAME}\n", encoding="utf-8", newline="\n")
    return archive


def bare_name_problem(name: str) -> str | None:
    """Why *name* is not a bare file name, or None. Stricter than "no slash": on Windows
    ``C:x`` is drive-relative and ``dest / "C:x"`` leaves *dest*, and ``..`` climbs out of it."""
    if not name or name == "." or ".." in name:
        return f"member name {name!r} is not a file name"
    if "/" in name or "\\" in name:
        return f"nested member {name!r}: the layout is flat"
    if ":" in name:
        return f"member name {name!r} holds ':' (a drive or stream designator on Windows)"
    if Path(name).name != name:
        return f"member name {name!r} is not a bare file name"
    return None


def verify_archive(archive: Path) -> list[str]:
    """Problems with an engine archive (empty list = the P0.4 layout, intact)."""
    problems: list[str] = []
    seen: dict[str, int] = {}
    try:
        with tarfile.open(archive, "r:xz") as tf:
            for member in tf:
                name = member.name
                if member.isdir():
                    problems.append(f"nested directory member {name!r}")
                    continue
                if not member.isreg():
                    problems.append(f"non-regular member {name!r}")
                    continue
                bad_name = bare_name_problem(name)
                if bad_name is not None:
                    problems.append(bad_name)
                    continue
                if name in seen:
                    problems.append(f"duplicate member {name!r}")
                seen[name] = member.size
    except (tarfile.TarError, lzma.LZMAError, EOFError, OSError) as exc:
        return [f"archive {archive} could not be read: {exc}"]
    for required in (ENGINE_EXE, *VC_RUNTIME_DLLS, NOTICE_NAME):
        if required not in seen:
            problems.append(f"missing {required}")
        elif seen[required] == 0:
            problems.append(f"{required} is empty")
    for name in seen:
        low = name.lower()
        if low.endswith(".pdb"):
            problems.append(f"{name}: a .pdb must not ship (pdb)")
        elif low.endswith(".dll") and name not in VC_RUNTIME_DLLS:
            problems.append(f"{name}: a DLL outside the four VC++ runtime files")
        elif low.endswith(".exe") and name != ENGINE_EXE:
            problems.append(f"{name}: an executable other than {ENGINE_EXE}")
    return problems


# --------------------------------------------------------------------------- #
# release_version stamp and MSVC environment
# --------------------------------------------------------------------------- #


def stamp_release_version(text: str, version: str) -> str:
    """*text* with a single ``release_version=<version>`` line (header and other keys kept, LF)."""
    if not _VERSION_RE.fullmatch(version):
        raise CheckError(f"release version {version!r} is not dotted digits")
    lines = [ln for ln in text.replace("\r\n", "\n").split("\n") if not ln.startswith("release_version=")]
    while lines and lines[-1] == "":
        lines.pop()
    lines.append(f"release_version={version}")
    return "\n".join(lines) + "\n"


def github_env_lines(env: Mapping[str, str], base: Mapping[str, str] | None = None) -> list[str]:
    """``NAME=value`` lines for $GITHUB_ENV: what *env* adds to or changes from *base*.

    Single-line values with legal names only, minus the runner's own variables. Names compare
    case-insensitively (Windows), so a PATH that vcvars rewrote is exported and one it only
    re-cased is not."""
    have = {k.upper(): v for k, v in (base or {}).items()}
    out = []
    for key, value in env.items():
        if key.upper().startswith(_RUNNER_OWN_ENV) or not _ENV_NAME_RE.match(key):
            continue
        if "\n" in value or "\r" in value:
            continue
        if have.get(key.upper()) == value:
            continue
        out.append(f"{key}={value}")
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _vs_path(env: Mapping[str, str], explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    try:
        return bw.find_vs_install(env, bw.Runner())
    except bw.BuildError as exc:
        raise CheckError(str(exc)) from exc


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    vc = sub.add_parser("vcvars")
    vc.add_argument("--vs-install", type=Path)
    st = sub.add_parser("stamp")
    st.add_argument("--file", type=Path, required=True)
    st.add_argument("--version", required=True)
    cd = sub.add_parser("check-deps")
    cd.add_argument("--exe", type=Path, required=True)
    cd.add_argument("--report", type=Path, required=True, help="service/target/embedded-resources.json")
    cd.add_argument("--dumpbin", type=Path, help="default: the newest under the Visual Studio install")
    cd.add_argument("--vs-install", type=Path)
    cd.add_argument("--workdir", type=Path, help="extraction directory (default: a temporary one, removed afterwards; yours is never removed)")
    cd.add_argument("--keep-work-dir", action="store_true", help="keep the temporary extraction directory")
    pk = sub.add_parser("package")
    pk.add_argument("--exe", type=Path, required=True)
    pk.add_argument("--out-dir", type=Path, required=True)
    pk.add_argument("--vs-install", type=Path)
    va = sub.add_parser("verify-archive")
    va.add_argument("--archive", type=Path, required=True)
    return p


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    args = _parser().parse_args(argv)
    try:
        if args.cmd == "stamp":
            args.file.write_text(stamp_release_version(args.file.read_text(encoding="utf-8"), args.version), encoding="utf-8", newline="\n")
            print(f"stamped release_version={args.version} into {args.file}")
            return 0
        if args.cmd == "verify-archive":
            problems = verify_archive(args.archive)
            for pr in problems:
                print(f"FAIL: {pr}", file=sys.stderr)
            if not problems:
                print(f"{args.archive.name}: layout ok")
            return 1 if problems else 0
        if args.cmd == "vcvars":
            try:
                vs_env = bw.load_vs_env(_vs_path(env, args.vs_install), env)
            except bw.BuildError as exc:
                raise CheckError(str(exc)) from exc
            target = env.get("GITHUB_ENV")
            lines = github_env_lines(vs_env, env)
            if not target:
                raise CheckError("GITHUB_ENV is not set: this subcommand exports to a workflow")
            with open(target, "a", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(lines) + "\n")
            print(f"exported {len(lines)} MSVC environment variables")
            return 0
        if args.cmd == "package":
            vs = _vs_path(env, args.vs_install)
            try:
                redist = bw.find_redist(vs)
            except bw.BuildError as exc:
                raise CheckError(str(exc)) from exc
            archive = package(args.exe, redist, args.out_dir)
            print(f"{archive} (VC redist {redist.version})")
            return 0
        # check-deps
        report = read_report(args.report) if args.report.is_file() else None
        if report is None:
            raise CheckError(f"embedded-resources report {args.report} not found")
        dumpbin = args.dumpbin or find_dumpbin(_vs_path(env, args.vs_install))
        # Extracted DLLs on a persistent runner: remove them, pass or fail (nexus-f9bgu.27, review S3).
        owned = args.workdir is None
        work = args.workdir or Path(tempfile.mkdtemp(prefix="engine-deps-", dir=env.get("RUNNER_TEMP") or None))
        try:
            rc, lines = check_deps(args.exe, report, run_dumpbin(dumpbin), work)
        finally:
            if owned and not args.keep_work_dir:
                bw.remove_tree(work)
        print("\n".join(lines), file=sys.stderr if rc else sys.stdout)
        return rc
    except CheckError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
