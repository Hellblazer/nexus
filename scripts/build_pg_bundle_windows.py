#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build the Windows x64 PostgreSQL + pgvector + pg_trgm bundle (RDR-224 P2.1,
bead nexus-f9bgu.12). The Windows counterpart of ``scripts/build_pg_bundle.sh``.

Why a separate script, and why Python. The autoconf / make / patchelf /
install_name_tool toolchain of the shell script shares nothing with meson +
MSVC + nmake, so the shell script is not extended. The language is Python
(stdlib only) rather than PowerShell because every decision that burned the
2026-09-29 spike is a pure function here (the cache key, the generated-target
list, the pgvector overrides, the runtime-DLL selection) and so runs under
pytest on any OS, with the platform and the process runner injected; a .ps1
could only be regex-parsed on a laptop. The release and rehearsal hosts both
carry Python (uv).

Output is the same shape as the other bundles: a prefix holding
``bin include lib share`` plus ``.build_prefix``, packaged by the ``package``
subcommand as ``nexus-pg-windows-x64.txz`` (+ ``.sha256``) whose single
top-level directory is ``bundle/``. The cosign ``.sigstore.json`` is produced
by the release workflow, as for the other platforms.

Subcommands::

    build            compile PG + pgvector into --prefix, add the VC++ runtime
    refresh-runtime  re-copy the four VC++ DLLs + notice into an existing
                     (e.g. cache-restored) prefix from the CURRENT redist
    verify           assert the prefix holds the complete bundle layout
    package          prefix -> nexus-pg-windows-x64.txz (+ .sha256)
    cache-key        print the exact-input cache key

Cache key = version pins + the sha256 of this file, same shape as the other
bundles' ``pg-bundle-<arch>-<runner>-pg<V>-pgvector<V>-img-<image|native>-<hash>``
(no ``macfloor`` segment: nothing here reads MACOSX_DEPLOYMENT_TARGET). The
VC++ runtime DLLs are NOT a key input: they come from the host's Visual Studio
at build time, and the P0.6 terms (T2 nexus_rdr/224-vcruntime-terms, condition
8) require them refreshed from the current redist on every release. A
cache-restored prefix therefore goes through ``refresh-runtime`` before it is
packaged.

Hazards found by the spike, each handled below:
  * winget's ``Links`` shim for win_bison cannot find bison's data directory:
    the package directory (the one holding ``data/``) goes on PATH instead.
  * win_bison races on shared temp files under parallel ninja: the 20
    ``*gram.c`` / ``*scan.c`` targets are generated one at a time first.
  * meson installs under ``include/postgresql`` and ``lib/postgresql`` when
    the prefix lacks "postgres", and pgvector's ``Makefile.win`` assumes a flat
    layout: INCLUDEDIR, INCLUDEDIR_SERVER, PKGLIBDIR, SHAREDIR and LIBDIR come
    from ``pg_config``.
  * postgres.exe and initdb.exe exit 0xC0000135 on a machine without the VC++
    runtime: the four DLLs ship in ``bin``, unmodified, from the released
    toolset's redist folder.

Environment (flags win): BUNDLE_PREFIX, PG_VERSION, PGVECTOR_VERSION,
WORK_DIR, PG_BUNDLE_RUNNER, VS_INSTALL_PATH, WIN_FLEX_BISON_DIR, RUNNER_TEMP.

The build's work directory (PG tarball, source tree, meson build tree: gigabytes) is a
temporary one under RUNNER_TEMP unless WORK_DIR / --work-dir names one, and is removed
when the build ends, passed or failed, unless --keep-work-dir; a directory the caller
named is the caller's and is never removed (nexus-f9bgu.27). Every shipped VC++ DLL must
carry a Valid Authenticode signature from Microsoft Corporation, the PG tarball must match
the sha256 pinned in this file and pgvector the pinned commit (PINNED_PG_SHA256,
PINNED_PGVECTOR_COMMITS).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: Defaults mirror scripts/build_pg_bundle.sh and the workflows' env pins;
#: tests/test_pg_bundle_windows.py pins all three together.
DEFAULT_PG_VERSION = "17.5"
DEFAULT_PGVECTOR_VERSION = "v0.8.2"

#: Literal pins (nexus-f9bgu.27, code review m5). ftp.postgresql.org publishes the
#: tarball and its .sha256 side by side, so the published file proves integrity but
#: not authenticity; the literal below was fetched once from the host on 2026-10-05
#: and recomputed locally over the downloaded tarball, and the build refuses a
#: tarball that matches only the host's own file. A git tag moves; the commit does
#: not (``git ls-remote https://github.com/pgvector/pgvector refs/tags/v0.8.2`` on
#: 2026-10-05). A version not listed here is refused: moving a pin is one reviewed
#: line, never an environment variable.
PINNED_PG_SHA256: dict[str, str] = {
    "17.5": "fcb7ab38e23b264d1902cb25e6adafb4525a6ebcbd015434aeef9eda80f528d8",
}
PINNED_PGVECTOR_COMMITS: dict[str, str] = {
    "v0.8.2": "cab9da72c04353f143bb06b42ab70a403daac64a",
}

ARCH = "windows-x64"
ASSET_NAME = f"nexus-pg-{ARCH}.txz"
#: The one top-level directory of the archive (client: ``<root>/bundle/bin``).
ARCHIVE_ROOT = "bundle"

#: The four app-local runtime DLLs (RDR-224 Key Discoveries, T2 224-research-13).
VC_RUNTIME_DLLS: tuple[str, ...] = (
    "vcruntime140.dll",
    "vcruntime140_1.dll",
    "msvcp140.dll",
    "msvcp140_1.dll",
)
NOTICE_NAME = "THIRD-PARTY-NOTICES.txt"
LICENSES_DIR = "licenses"

#: PG 17.5's generated grammar/scanner targets (bead step 3). A different count
#: means the filter drifted from what meson generates: fail, do not build on.
EXPECTED_GENERATED_TARGETS = 20
GENERATED_TARGET_RE = re.compile(
    r"^(\S+(?:gram|scan|scanner|parse)\.c):\s+CUSTOM_COMMAND\s*$", re.MULTILINE
)

#: Lean flags, the Linux bundle's: no ICU, zlib, readline or OpenSSL.
MESON_OPTIONS: tuple[str, ...] = (
    "-Dbuildtype=release",
    "-Dauto_features=disabled",
    "-Dssl=none",
    "-Dicu=disabled",
    "-Dzlib=disabled",
    "-Dreadline=disabled",
)

#: Tools whose absence is reported together, before anything is downloaded.
REQUIRED_TOOLS: tuple[str, ...] = (
    "meson", "ninja", "perl", "win_bison", "win_flex", "cl", "nmake", "git",
)

#: Binaries every bundle must hold (mirrors verify_and_mark in the shell script).
REQUIRED_BINARIES: tuple[str, ...] = (
    "initdb", "pg_ctl", "postgres", "psql", "createdb", "pg_config",
)

PG_SOURCE_URL = "https://ftp.postgresql.org/pub/source/v{v}/postgresql-{v}.tar.bz2"
PGVECTOR_REPO = "https://github.com/pgvector/pgvector.git"


class BuildError(RuntimeError):
    """A build step failed or a precondition does not hold."""


# --------------------------------------------------------------------------- #
# Pins and the cache key
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Pins:
    pg_version: str
    pgvector_version: str


def pins_from_env(env: Mapping[str, str]) -> Pins:
    return Pins(
        pg_version=env.get("PG_VERSION") or DEFAULT_PG_VERSION,
        pgvector_version=env.get("PGVECTOR_VERSION") or DEFAULT_PGVECTOR_VERSION,
    )


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cache_key(pins: Pins, runner: str, script_sha256: str) -> str:
    """Exact-input cache key: version pins + this script's hash.

    The runner is the resolved runner LABEL, as in the other bundles' keys.
    Nothing outside these four values may change the bundle without changing
    the key, except the VC++ runtime DLLs, which ``refresh-runtime`` re-copies
    after every restore.
    """
    if not runner or not re.fullmatch(r"[A-Za-z0-9._-]+", runner):
        raise BuildError(f"runner label {runner!r} is empty or not [A-Za-z0-9._-]")
    return (
        f"pg-bundle-{ARCH}-{runner}-pg{pins.pg_version}"
        f"-pgvector{pins.pgvector_version}-img-native-{script_sha256}"
    )


# --------------------------------------------------------------------------- #
# Process running (output goes to files, never a pipe held open for a build)
# --------------------------------------------------------------------------- #


def resolve_exe(argv: Sequence[str], env: Mapping[str, str]) -> list[str]:
    """argv with argv[0] resolved against the CHILD's PATH. Windows'
    CreateProcess searches the PARENT's PATH, so ``nmake`` and ``cl`` (only on
    the post-vcvars PATH) are not found by a bare name: found on the 2026-10-05
    qwentescence run, after meson and ninja (on the parent's PATH) had worked."""
    exe = shutil.which(argv[0], path=env.get("PATH"))
    if exe is None:
        raise BuildError(f"{argv[0]}: not found on the build PATH")
    return [exe, *argv[1:]]


class Runner:
    """Runs and captures external commands; tests substitute a fake."""

    def run(
        self, argv: Sequence[str], *, cwd: Path | None, env: Mapping[str, str], log: Path
    ) -> None:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as out:
            out.write(("\n$ " + " ".join(argv) + "\n").encode())
            out.flush()
            rc = subprocess.call(
                resolve_exe(argv, env), cwd=cwd, env=dict(env),
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            )
        if rc != 0:
            tail = "\n".join(log.read_text(errors="replace").splitlines()[-40:])
            raise BuildError(f"{argv[0]} exited {rc} (log {log}):\n{tail}")

    def capture(
        self, argv: Sequence[str], *, cwd: Path | None, env: Mapping[str, str]
    ) -> str:
        proc = subprocess.run(
            resolve_exe(argv, env), cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, errors="replace",
        )
        if proc.returncode != 0:
            raise BuildError(f"{argv[0]} exited {proc.returncode}: {proc.stderr.strip()}")
        return proc.stdout


# --------------------------------------------------------------------------- #
# Toolchain discovery (Windows)
# --------------------------------------------------------------------------- #


def find_flex_bison_dir(
    env: Mapping[str, str], *, search_roots: Sequence[Path] | None = None
) -> Path:
    """The REAL win_flex_bison package directory: the one holding win_bison.exe
    AND bison's ``data/`` directory. winget's ``Links`` shim holds only a stub
    that cannot find ``data/``.
    """
    candidates: list[Path] = []
    explicit = env.get("WIN_FLEX_BISON_DIR")
    if explicit:
        candidates.append(Path(explicit))
    roots = list(search_roots) if search_roots is not None else []
    if search_roots is None:
        local = env.get("LOCALAPPDATA")
        if local:
            roots.append(Path(local) / "Microsoft" / "WinGet" / "Packages")
        choco = env.get("ChocolateyInstall") or env.get("CHOCOLATEYINSTALL")
        if choco:
            candidates.append(Path(choco) / "lib" / "winflexbison3" / "tools")
    for root in roots:
        if root.is_dir():
            candidates.extend(sorted(root.glob("WinFlexBison.win_flex_bison_*")))
    for cand in candidates:
        if (cand / "win_bison.exe").is_file() and (cand / "data").is_dir():
            return cand
    raise BuildError(
        "win_flex_bison package directory not found (needs win_bison.exe and data/ "
        f"side by side; tried {[str(c) for c in candidates] or 'nothing'}); install it "
        "(winget install WinFlexBison.win_flex_bison) or set WIN_FLEX_BISON_DIR. "
        "The winget Links shim does not work."
    )


def find_vs_install(
    env: Mapping[str, str], runner: Runner, *, vswhere: Path | None = None
) -> Path:
    """Visual Studio install path; refuses a Preview / pre-release product
    (P0.6 condition 5: only the released toolset's runtime is redistributed)."""
    explicit = env.get("VS_INSTALL_PATH")
    if explicit:
        return Path(explicit)
    if vswhere is None:
        pf86 = env.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
        vswhere = Path(pf86) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.is_file():
        raise BuildError(f"vswhere.exe not found at {vswhere}; set VS_INSTALL_PATH")
    out = runner.capture(
        [str(vswhere), "-latest", "-products", "*", "-requires",
         "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-format", "json"],
        cwd=None, env=env,
    )
    found = json.loads(out or "[]")
    if not found:
        raise BuildError("vswhere found no Visual Studio with the x64 C++ tools")
    first = found[0]
    if first.get("isPrerelease"):
        raise BuildError(
            "the newest Visual Studio is a Preview/pre-release product; its runtime "
            "must not be redistributed (P0.6). Set VS_INSTALL_PATH to a released one."
        )
    return Path(first["installationPath"])


def load_vs_env(vs_path: Path, base: Mapping[str, str]) -> dict[str, str]:
    """The environment after vcvars64.bat (cl, nmake, INCLUDE, LIB on PATH)."""
    vcvars = vs_path / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
    if not vcvars.is_file():
        raise BuildError(f"{vcvars} not found")
    # cmd /s strips the outer quotes: the standard way to run a quoted .bat.
    cmdline = f'cmd /d /s /c ""{vcvars}" >nul && set"'
    proc = subprocess.run(
        cmdline, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        errors="replace", env=dict(base),
    )
    if proc.returncode != 0:
        raise BuildError(f"vcvars64.bat failed ({proc.returncode}): {proc.stderr.strip()}")
    env: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep and key and not key.startswith("="):
            env[key.upper()] = value
    if "PATH" not in env:
        raise BuildError("vcvars64.bat produced no PATH")
    return env


def prepend_path(env: Mapping[str, str], directory: Path) -> dict[str, str]:
    out = dict(env)
    out["PATH"] = f"{directory}{os.pathsep}{out.get('PATH', '')}"
    return out


def missing_tools(env: Mapping[str, str]) -> list[str]:
    path = env.get("PATH", "")
    return [t for t in REQUIRED_TOOLS if shutil.which(t, path=path) is None]


# --------------------------------------------------------------------------- #
# VC++ runtime (P0.6 conditions) and notices
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Redist:
    directory: Path
    version: str


def find_redist(vs_path: Path) -> Redist:
    """The x64 CRT redist folder of the Visual Studio install, never
    debug_nonredist, never System32 (P0.6 condition 1)."""
    base = vs_path / "VC" / "Redist" / "MSVC"
    if not base.is_dir():
        raise BuildError(f"{base} not found: the Visual Studio C++ redist is not installed")
    version = ""
    marker = vs_path / "VC" / "Auxiliary" / "Build" / "Microsoft.VCRedistVersion.default.txt"
    if marker.is_file():
        version = marker.read_text().strip()
    if not version or not (base / version).is_dir():
        numbered = sorted(
            (d for d in base.iterdir() if d.is_dir() and re.fullmatch(r"\d+(\.\d+)+", d.name)),
            key=lambda d: tuple(int(p) for p in d.name.split(".")),
        )
        if not numbered:
            raise BuildError(f"no versioned redist folder under {base}")
        version = numbered[-1].name
    # debug_nonredist is a sibling of x64 (<version>/debug_nonredist/x64/...DebugCRT), so scanning only
    # <version>/x64 for the release CRT folders never reaches it; no guard is needed here.
    for crt in sorted((base / version / "x64").glob("Microsoft.VC*.CRT")):
        if all((crt / dll).is_file() for dll in VC_RUNTIME_DLLS):
            return Redist(crt, version)
    raise BuildError(
        f"no Microsoft.VC*.CRT folder under {base / version / 'x64'} holds all of "
        f"{', '.join(VC_RUNTIME_DLLS)}"
    )


def notice_text(redist: Redist, digests: Mapping[str, str], pins: Pins) -> str:
    dll_lines = "\n".join(f"    {n}  sha256 {digests[n]}" for n in VC_RUNTIME_DLLS)
    return f"""THIRD-PARTY NOTICES for the nexus PostgreSQL bundle ({ARCH})

1. Microsoft Visual C++ runtime (bin/{', bin/'.join(VC_RUNTIME_DLLS)})

   These four files are Microsoft's. They are NOT covered by the AGPL-3.0 that
   covers nexus. They are copied unmodified from Visual Studio 2022's
   redistributable folder (VC redist {redist.version}) and are distributed with
   this program under the Distributable Code terms of the Microsoft Visual
   Studio 2022 license (https://aka.ms/vs/17/redistribution). Those terms
   permit use only as part of this program: you may not modify these files,
   reverse engineer them, or redistribute them separately from it.

   Files shipped:
{dll_lines}

2. PostgreSQL {pins.pg_version}: PostgreSQL License, see licenses/postgresql-COPYRIGHT.txt.
3. pgvector {pins.pgvector_version}: PostgreSQL License, see licenses/pgvector-LICENSE.txt.
"""


#: The organisation the four runtime files must be signed by (nexus-f9bgu.27, code review S4). Measured
#: on the real VS 2022 redist (14.44.35112, qwentescence, 2026-10-05): Status Valid, subject
#: ``CN=Microsoft Windows, O=Microsoft Corporation, L=Redmond, S=Washington, C=US``. The organisation is
#: the signer; the CN is the certificate's own name (here the Windows-component one), so it is held only
#: to start with "Microsoft", never to equal the organisation.
MICROSOFT_SIGNER = "Microsoft Corporation"

_AUTHENTICODE_PS = (
    "$s = Get-AuthenticodeSignature -LiteralPath $env:NX_SIGCHECK_PATH; "
    "$subject = ''; if ($s.SignerCertificate) { $subject = $s.SignerCertificate.Subject }; "
    "ConvertTo-Json -Compress -InputObject @{ Status = [string]$s.Status; Subject = $subject }"
)


@dataclass(frozen=True)
class Signature:
    status: str
    subject: str


SignatureReader = Callable[[Path], Signature]


def read_authenticode(path: Path) -> Signature:
    """The Authenticode status and signer subject of *path* (Windows PowerShell's
    Get-AuthenticodeSignature; the path travels in the environment, never quoted into
    the script). Windows only: off Windows there is nothing to ask, and a check that
    cannot run must fail, not pass."""
    if sys.platform != "win32":
        raise BuildError(
            f"cannot read the Authenticode signature of {path.name} on {sys.platform}: "
            "the runtime DLLs are packaged on Windows only"
        )
    # PSModulePath goes: launched from a pwsh 7 step, Windows PowerShell 5.1
    # inherits pwsh 7's module path, fails to load Microsoft.PowerShell.Security,
    # and still exits 0 with an empty Status (rehearsal run 37410530509). Windows
    # keys are case-insensitive, so match the name in any case.
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    env["NX_SIGCHECK_PATH"] = str(path)
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _AUTHENTICODE_PS],
        env=env,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
    )
    if proc.returncode != 0:
        raise BuildError(f"Get-AuthenticodeSignature failed ({proc.returncode}) on {path.name}: {proc.stderr.strip()[:300]}")
    try:
        body = json.loads(proc.stdout)
        sig = Signature(str(body["Status"]), str(body["Subject"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise BuildError(f"unreadable Get-AuthenticodeSignature output for {path.name}: {proc.stdout[:200]!r}") from exc
    if not sig.status:
        # A signature always has a status; an empty one means the cmdlet never ran.
        raise BuildError(f"Get-AuthenticodeSignature returned no status for {path.name}: {proc.stderr.strip()[:300]}")
    return sig


def subject_attribute(subject: str, key: str) -> str | None:
    """The value of attribute *key* (``CN``, ``O``, ...) in a certificate subject such as
    ``CN=Microsoft Windows, O=Microsoft Corporation, L=Redmond``, quoted values included; None when absent."""
    match = re.search(rf'(?:^|,)\s*{re.escape(key)}=(?:"((?:[^"]|"")*)"|([^,]*))', subject)
    if not match:
        return None
    value = match.group(1).replace('""', '"') if match.group(1) is not None else match.group(2)
    return value.strip()


def check_microsoft_signature(path: Path, reader: SignatureReader) -> str:
    """Return the signer subject of *path*; raise unless it is Valid and signed by an organisation of
    ``Microsoft Corporation`` under a ``Microsoft ...`` certificate name.

    Distribution rights come from the Distributable Code terms for Microsoft's files
    (T2 224-vcruntime-terms). A copy that is unsigned, tampered with, or signed by anyone
    else is not that file, whatever its hash says about the copy."""
    sig = reader(path)
    if sig.status != "Valid":
        raise BuildError(f"{path.name}: Authenticode status {sig.status!r}, expected 'Valid'; refusing to ship it")
    cn = subject_attribute(sig.subject, "CN") or ""
    if subject_attribute(sig.subject, "O") != MICROSOFT_SIGNER or not cn.startswith("Microsoft"):
        raise BuildError(
            f"{path.name}: signed by {sig.subject!r}, not by {MICROSOFT_SIGNER!r}; refusing to ship it"
        )
    return sig.subject


def copy_runtime_dlls(
    dest_dir: Path,
    redist: Redist,
    *,
    signature_reader: SignatureReader | None = None,
    emit: Callable[[str], None] = print,
) -> dict[str, str]:
    """Copy the four VC++ runtime DLLs unmodified into *dest_dir*; return name -> sha256.

    The one copy routine for every Windows artifact that ships the runtime (the PG
    bundle's bin, the engine archive's staging directory, nexus-f9bgu.9), so the
    P0.6 "ship unmodified" condition is enforced in one place. Each shipped copy must
    carry a Valid Authenticode signature from Microsoft Corporation, and the signer is
    printed so the build log records what shipped. Idempotent."""
    reader = signature_reader if signature_reader is not None else read_authenticode
    dest_dir.mkdir(parents=True, exist_ok=True)
    digests: dict[str, str] = {}
    for dll in VC_RUNTIME_DLLS:
        src = redist.directory / dll
        dst = dest_dir / dll
        shutil.copyfile(src, dst)
        want, got = file_sha256(src), file_sha256(dst)
        if want != got:
            raise BuildError(f"{dll} changed in transit ({want} != {got}); must ship unmodified")
        subject = check_microsoft_signature(dst, reader)
        emit(f"signature {dll}: Valid, {subject}")
        digests[dll] = got
    return digests


def copy_runtime(
    bundle: Path,
    redist: Redist,
    pins: Pins,
    *,
    extra_notice: str = "",
    signature_reader: SignatureReader | None = None,
) -> dict[str, str]:
    """Copy the four DLLs unmodified into bundle/bin and write the notice.
    Idempotent: re-running replaces them from the redist it is given."""
    digests = copy_runtime_dlls(bundle / "bin", redist, signature_reader=signature_reader)
    (bundle / NOTICE_NAME).write_text(notice_text(redist, digests, pins) + extra_notice)
    return digests


# --------------------------------------------------------------------------- #
# Command planners (pure: the hazards live here)
# --------------------------------------------------------------------------- #


def meson_setup_cmd(src: Path, build_dir: Path, prefix: Path) -> list[str]:
    return ["meson", "setup", str(build_dir), str(src), f"--prefix={prefix}", *MESON_OPTIONS]


def generated_targets(ninja_targets_output: str) -> list[str]:
    found = GENERATED_TARGET_RE.findall(ninja_targets_output)
    if len(found) != EXPECTED_GENERATED_TARGETS:
        raise BuildError(
            f"expected {EXPECTED_GENERATED_TARGETS} generated grammar/scanner targets, "
            f"found {len(found)}: the filter has drifted from what meson generates"
        )
    return found


def ninja_generate_cmds(build_dir: Path, targets: Sequence[str]) -> list[list[str]]:
    """One ninja per target, -j1: win_bison races on shared temp files."""
    return [["ninja", "-j1", "-C", str(build_dir), t] for t in targets]


def pgvector_make_cmds(bundle: Path, dirs: Mapping[str, str]) -> list[list[str]]:
    """nmake over Makefile.win with pg_config's directories (a flat layout is
    assumed otherwise). Makefile.win's OPTFLAGS defaults to empty (no /arch:
    flag), so the DLL carries no builder-CPU ISA, unlike the stock Unix
    Makefile's -march=native that build_pg_bundle.sh has to blank."""
    defs = [
        f"PGROOT={bundle}",
        f"INCLUDEDIR={dirs['includedir']}",
        f"INCLUDEDIR_SERVER={dirs['includedir-server']}",
        f"PKGLIBDIR={dirs['pkglibdir']}",
        f"SHAREDIR={dirs['sharedir']}",
        f"LIBDIR={dirs['libdir']}",
    ]
    base = ["nmake", "/NOLOGO", "/F", "Makefile.win", *defs]
    return [base, [*base, "install"]]


PG_CONFIG_KEYS = ("includedir", "includedir-server", "pkglibdir", "sharedir", "libdir")


# --------------------------------------------------------------------------- #
# Layout verification and packaging
# --------------------------------------------------------------------------- #


def verify_layout(bundle: Path, *, runtime: bool = True) -> list[str]:
    """Problems with the bundle tree (empty list = complete). Filesystem only,
    so it runs on any OS against a staged tree."""
    problems: list[str] = []
    for d in ("bin", "include", "lib", "share"):
        if not (bundle / d).is_dir():
            problems.append(f"missing directory {d}/")
    for b in REQUIRED_BINARIES:
        if not (bundle / "bin" / f"{b}.exe").is_file():
            problems.append(f"missing bin/{b}.exe")
    if not (bundle / "bin" / "libpq.dll").is_file():
        problems.append("missing bin/libpq.dll")
    if not any((bundle / "lib").rglob("vector.dll")):
        problems.append("missing vector.dll under lib/")
    for ctl in ("vector", "pg_trgm"):
        if not any((bundle / "share").rglob(f"extension/{ctl}.control")):
            problems.append(f"missing {ctl}.control under share/**/extension")
    if not (bundle / ".build_prefix").is_file():
        problems.append("missing .build_prefix")
    if runtime:
        for dll in VC_RUNTIME_DLLS:
            if not (bundle / "bin" / dll).is_file():
                problems.append(f"missing bin/{dll} (0xC0000135 on a clean machine)")
        if not (bundle / NOTICE_NAME).is_file():
            problems.append(f"missing {NOTICE_NAME} (P0.6 condition 4)")
    return problems


def package(bundle: Path, out_dir: Path, *, arcroot: str = ARCHIVE_ROOT) -> Path:
    """prefix -> nexus-pg-windows-x64.txz (+ .sha256, sha256sum format).

    Every member is under ``<arcroot>/`` so extraction yields ``bundle/bin``
    (client: nexus.db.pg_bundle.bundle_bin_dir). Ownership is normalised."""
    problems = verify_layout(bundle)
    if problems:
        raise BuildError("refusing to package an incomplete bundle: " + "; ".join(problems))
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / ASSET_NAME

    def norm(ti: tarfile.TarInfo) -> tarfile.TarInfo:
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        return ti

    with lzma.open(archive, "wb", preset=6) as xz, tarfile.open(fileobj=xz, mode="w") as tf:
        tf.add(bundle, arcname=arcroot, filter=norm)
    digest = file_sha256(archive)
    (out_dir / f"{ASSET_NAME}.sha256").write_text(f"{digest}  {ASSET_NAME}\n")
    return archive


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #


def _log(msg: str) -> None:
    print(f"=== {msg} ===", flush=True)


def fetch_to(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url, timeout=120) as resp, dest.open("wb") as out:  # noqa: S310
        shutil.copyfileobj(resp, out)


def unpack_bz2(archive: Path, dest: Path) -> None:
    with tarfile.open(archive, "r:bz2") as tf:
        tf.extractall(dest, filter="data")


@dataclass
class Host:
    """Everything the build touches outside the filesystem; tests inject fakes."""

    runner: Runner
    fetch: Callable[[str, Path], None] = fetch_to
    unpack: Callable[[Path, Path], None] = unpack_bz2
    signature_reader: SignatureReader | None = None
    pg_sha256_pins: Mapping[str, str] = field(default_factory=lambda: dict(PINNED_PG_SHA256))
    pgvector_commit_pins: Mapping[str, str] = field(default_factory=lambda: dict(PINNED_PGVECTOR_COMMITS))


def remove_tree(path: Path) -> None:
    """Delete a build directory, never raising: a failure to clean up must not turn a
    finished build red. Clears the read-only bit first (git's object files carry it, and
    Windows refuses to delete them otherwise)."""

    def clear_and_retry(func: Callable[[str], object], name: str, _exc: BaseException) -> None:
        try:
            os.chmod(name, stat.S_IWRITE)
            func(name)
        except OSError:
            pass

    shutil.rmtree(path, onexc=clear_and_retry)
    if path.exists():
        print(f"WARNING: could not fully remove the work directory {path}", file=sys.stderr, flush=True)


def build(
    *,
    prefix: Path,
    work: Path,
    pins: Pins,
    jobs: int,
    env: Mapping[str, str],
    vs_env: Mapping[str, str],
    flex_bison_dir: Path,
    redist: Redist,
    host: Host,
) -> None:
    """The full build. ``vs_env`` is the post-vcvars environment; the order of
    the steps below is the contract the tests pin."""
    for p in (prefix, work):
        if " " in str(p):
            raise BuildError(f"{p} contains a space; nmake cannot take it as a macro value")
    logs = work / "logs"
    run_env = prepend_path(vs_env, flex_bison_dir)  # step 2: before meson sees PATH
    run_env["CC"] = "cl"
    gone = missing_tools(run_env)
    if gone:
        raise BuildError(f"required tools not on PATH: {', '.join(gone)}")
    prefix.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    r = host.runner

    _log(f"fetch + verify PostgreSQL {pins.pg_version}")
    url = PG_SOURCE_URL.format(v=pins.pg_version)
    tarball = work / "pg.tar.bz2"
    host.fetch(url, tarball)
    sums = work / "pg.tar.bz2.sha256"
    host.fetch(url + ".sha256", sums)
    want = sums.read_text().split()[0].lower()
    got = file_sha256(tarball)
    if want != got:
        raise BuildError(f"PostgreSQL tarball sha256 {got} != published {want}")
    pinned = host.pg_sha256_pins.get(pins.pg_version)
    if pinned is None:
        raise BuildError(
            f"no pinned sha256 for PostgreSQL {pins.pg_version}: add it to PINNED_PG_SHA256 "
            "(the host's own .sha256 proves integrity, not authenticity)"
        )
    if got != pinned:
        raise BuildError(
            f"PostgreSQL tarball sha256 {got} != the pin in this script {pinned}: the host's "
            "published file agrees with the download, but neither is what was reviewed"
        )
    host.unpack(tarball, work)
    pg_src = work / f"postgresql-{pins.pg_version}"

    _log("meson setup (lean: no ICU/zlib/readline/OpenSSL)")
    build_dir = work / "pgbuild"
    r.run(meson_setup_cmd(pg_src, build_dir, prefix), cwd=None, env=run_env, log=logs / "meson-setup.log")

    _log("generate grammar/scanner targets serially (win_bison races under -j)")
    listing = r.capture(["ninja", "-C", str(build_dir), "-t", "targets", "all"], cwd=None, env=run_env)
    for cmd in ninja_generate_cmds(build_dir, generated_targets(listing)):
        r.run(cmd, cwd=None, env=run_env, log=logs / "gen-targets.log")

    _log(f"ninja build -j{jobs}")
    r.run(["ninja", f"-j{jobs}", "-C", str(build_dir)], cwd=None, env=run_env, log=logs / "build.log")
    r.run(["ninja", "-C", str(build_dir), "install"], cwd=None, env=run_env, log=logs / "install.log")

    _log(f"pgvector {pins.pgvector_version} against pg_config")
    pgv_src = work / "pgvector"
    pgv_commit = host.pgvector_commit_pins.get(pins.pgvector_version)
    if pgv_commit is None:
        raise BuildError(
            f"no pinned commit for pgvector {pins.pgvector_version}: add it to PINNED_PGVECTOR_COMMITS "
            "(a tag can be moved; a commit cannot)"
        )
    r.run(
        ["git", "clone", "--depth", "1", "--branch", pins.pgvector_version, PGVECTOR_REPO, str(pgv_src)],
        cwd=work, env=run_env, log=logs / "pgvector-clone.log",
    )
    head = r.capture(["git", "-C", str(pgv_src), "rev-parse", "HEAD"], cwd=None, env=run_env).strip().lower()
    if head != pgv_commit:
        raise BuildError(
            f"pgvector {pins.pgvector_version} resolved to commit {head}, the pin is {pgv_commit}: "
            "the tag moved, or the clone is not what was reviewed"
        )
    pg_config = prefix / "bin" / "pg_config.exe"
    dirs = {
        k: r.capture([str(pg_config), f"--{k}"], cwd=None, env=run_env).strip()
        for k in PG_CONFIG_KEYS
    }
    for cmd in pgvector_make_cmds(prefix, dirs):
        r.run(cmd, cwd=pgv_src, env=run_env, log=logs / "pgvector.log")

    _log("licenses, VC++ runtime, notice, .build_prefix")
    lic = prefix / LICENSES_DIR
    lic.mkdir(exist_ok=True)
    shutil.copyfile(pg_src / "COPYRIGHT", lic / "postgresql-COPYRIGHT.txt")
    for name in ("LICENSE", "LICENSE.txt"):
        if (pgv_src / name).is_file():
            shutil.copyfile(pgv_src / name, lic / "pgvector-LICENSE.txt")
            break
    copy_runtime(prefix, redist, pins, signature_reader=host.signature_reader)
    (prefix / ".build_prefix").write_text(os.path.realpath(prefix) + "\n")

    problems = verify_layout(prefix)
    if problems:
        raise BuildError("bundle incomplete: " + "; ".join(problems))
    _log(f"bundle complete: {prefix}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _require_windows(platform: str) -> None:
    if platform != "win32":
        raise BuildError("this subcommand builds with MSVC and runs on Windows only")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--pg-version")
        sp.add_argument("--pgvector-version")

    b = sub.add_parser("build")
    common(b)
    b.add_argument("--prefix", type=Path)
    b.add_argument("--work-dir", type=Path)
    b.add_argument("--jobs", type=int, default=min(os.cpu_count() or 4, 8))
    b.add_argument(
        "--keep-work-dir", action="store_true",
        help="keep the temporary work directory (PG source, build tree, logs) after the build; "
        "a --work-dir / WORK_DIR you named is yours and is never removed",
    )
    rr = sub.add_parser("refresh-runtime")
    common(rr)
    rr.add_argument("--prefix", type=Path)
    v = sub.add_parser("verify")
    v.add_argument("--prefix", type=Path)
    pk = sub.add_parser("package")
    pk.add_argument("--prefix", type=Path)
    pk.add_argument("--out-dir", type=Path, required=True)
    ck = sub.add_parser("cache-key")
    common(ck)
    ck.add_argument("--runner")
    return p


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    platform: str = sys.platform,
) -> int:
    env = os.environ if env is None else env
    args = _parser().parse_args(argv)
    pins = Pins(
        getattr(args, "pg_version", None) or pins_from_env(env).pg_version,
        getattr(args, "pgvector_version", None) or pins_from_env(env).pgvector_version,
    )
    prefix_arg = getattr(args, "prefix", None) or (
        Path(env["BUNDLE_PREFIX"]) if env.get("BUNDLE_PREFIX") else None
    )
    try:
        if args.cmd == "cache-key":
            runner = args.runner or env.get("PG_BUNDLE_RUNNER", "")
            print(cache_key(pins, runner, file_sha256(Path(__file__))))
            return 0
        if prefix_arg is None:
            raise BuildError("--prefix (or BUNDLE_PREFIX) is required")
        prefix = prefix_arg.resolve()
        if args.cmd == "verify":
            problems = verify_layout(prefix)
            for pr in problems:
                print(f"FAIL: {pr}", file=sys.stderr)
            return 1 if problems else 0
        if args.cmd == "package":
            print(package(prefix, args.out_dir))
            return 0
        _require_windows(platform)
        if args.cmd == "refresh-runtime" and not (prefix / "bin" / "initdb.exe").is_file():
            raise BuildError(f"{prefix} holds no built bundle (bin/initdb.exe): nothing to refresh")
        runner = Runner()
        vs_path = find_vs_install(env, runner)
        redist = find_redist(vs_path)
        if args.cmd == "refresh-runtime":
            digests = copy_runtime(prefix, redist, pins)
            _log(f"VC++ runtime refreshed from redist {redist.version}: {sorted(digests)}")
            return 0
        work_env = args.work_dir or (Path(env["WORK_DIR"]) if env.get("WORK_DIR") else None)
        # An auto-made directory holds the PG tarball, source tree and build tree (gigabytes) and the
        # runner is persistent: remove it, win or lose (nexus-f9bgu.27, code review S3).
        owned = work_env is None
        # Under RUNNER_TEMP when the runner sets it: the runner empties that directory between jobs, so a
        # job killed mid-build (cancel-in-progress) leaves nothing behind either.
        work = work_env or Path(tempfile.mkdtemp(prefix="pgbundle-", dir=env.get("RUNNER_TEMP") or None))
        try:
            build(
                prefix=prefix, work=work.resolve(), pins=pins, jobs=args.jobs, env=env,
                vs_env=load_vs_env(vs_path, env), flex_bison_dir=find_flex_bison_dir(env),
                redist=redist, host=Host(runner),
            )
        finally:
            if owned and not args.keep_work_dir:
                remove_tree(work)
        return 0
    except BuildError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
