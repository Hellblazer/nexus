#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Relocation smoke for the Windows PG bundle (RDR-224 P2.1, nexus-f9bgu.12).

The gate, as on the other platforms: take the bundle (the EXACT archive about
to be signed, or a staged tree), put it in a fresh directory that has no
relationship to the build prefix, and prove it works from there: initdb,
pg_ctl start, CREATE EXTENSION vector (expect 0.8.2) and pg_trgm, an HNSW
index that a query actually uses and that returns rows, pg_ctl stop.

Hazards it is built around:

  * pg_ctl start hands its stdout to postgres. Reading that through a pipe
    waits for a close that never comes, so pg_ctl's output goes to a FILE (a
    file has no reader to wait on). psql is a plain client and is captured.
  * The recorded build prefix (``bundle/.build_prefix``) must no longer exist:
    a smoke that can still reach the build tree proves nothing. Refused unless
    ``--allow-build-prefix-present`` says the run is knowingly weaker.
  * The children get a scrubbed environment: PATH is the bundle's bin plus the
    OS directories only, so Visual Studio, Python and Strawberry Perl cannot
    satisfy a DLL lookup.
  * On Windows the postmaster's loaded modules are listed and every VC++
    runtime module must resolve from bundle/bin (the CI proxy of P0.5b).

This script does NOT prove Visual Studio is absent: the loader may still find
a system-wide runtime. That claim needs the clean Windows 11 guest, a release-
time hand run (P0.5b, nexus-f9bgu.6). Run here it proves relocation and that
the bundle carries its own runtime beside the exes.

Usage::

    python scripts/pg_bundle_windows_smoke.py --archive dist/nexus-pg-windows-x64.txz
    python scripts/pg_bundle_windows_smoke.py --bundle C:/staged/bundle
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

#: Kept equal to build_pg_bundle_windows.VC_RUNTIME_DLLS by a test.
VC_RUNTIME_DLLS: tuple[str, ...] = (
    "vcruntime140.dll",
    "vcruntime140_1.dll",
    "msvcp140.dll",
    "msvcp140_1.dll",
)
EXPECT_VECTOR = "0.8.2"
ARCHIVE_ROOT = "bundle"
_VERSIONED_VC_RUNTIME_RE = re.compile(r"^(?:vcruntime|msvcp)\d+(?:_\d+)?\.dll$")


class SmokeError(RuntimeError):
    """The smoke failed."""


@dataclass(frozen=True)
class Platform:
    """Injected so both branches run under pytest on any OS."""

    windows: bool

    @property
    def exe(self) -> str:
        return ".exe" if self.windows else ""

    @classmethod
    def current(cls) -> Platform:
        return cls(windows=sys.platform == "win32")


class Runner:
    """Runs commands; tests substitute a fake."""

    def to_file(
        self, argv: Sequence[str], *, env: Mapping[str, str], out: Path
    ) -> int:
        """Run with stdout+stderr to a FILE and stdin closed (pg_ctl)."""
        with out.open("ab") as fh:
            return subprocess.call(
                list(argv), env=dict(env), stdin=subprocess.DEVNULL,
                stdout=fh, stderr=subprocess.STDOUT,
            )

    def capture(
        self, argv: Sequence[str], *, env: Mapping[str, str]
    ) -> tuple[int, str]:
        proc = subprocess.run(
            list(argv), env=dict(env), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, errors="replace",
        )
        return proc.returncode, proc.stdout + proc.stderr


def scrubbed_env(bin_dir: Path, platform: Platform, base: Mapping[str, str]) -> dict[str, str]:
    """PATH = bundle bin + OS dirs; nothing of the build host's toolchain."""
    if platform.windows:
        root = base.get("SystemRoot") or base.get("SYSTEMROOT") or r"C:\Windows"
        keep = ("SystemRoot", "SYSTEMDRIVE", "TEMP", "TMP", "USERNAME", "USERPROFILE",
                "COMSPEC", "PATHEXT", "APPDATA", "LOCALAPPDATA")
        env = {k: base[k] for k in keep if k in base}
        env.setdefault("SystemRoot", root)
        env["PATH"] = ";".join([str(bin_dir), rf"{root}\System32", root])
    else:
        keep = ("HOME", "TMPDIR", "LANG", "USER", "LOGNAME")
        env = {k: base[k] for k in keep if k in base}
        env["PATH"] = ":".join([str(bin_dir), "/usr/bin", "/bin"])
    return env


def make_workdir(parent: Path | None, platform: Platform) -> Path:
    """A fresh private directory for one smoke run.

    NOT ``tempfile.mkdtemp`` on Windows: since Python 3.12 that gives the
    directory an owner-only ACL with inheritance cut, so every file extracted
    into it is readable only by SYSTEM, Administrators and the owner. From an
    ELEVATED session initdb re-runs postgres with a restricted token that has
    Administrators deny-only (src/common/restricted_token.c), and that process
    then cannot read libpq.dll: initdb dies 0xC0000135 with no message. Seen
    on qwentescence 2026-10-05. A plain mkdir inherits the parent's ACL (Users
    read, as for any ordinary directory)."""
    base = parent if parent is not None else Path(tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    while True:
        path = base / f"pgsmoke-{secrets.token_hex(4)}"
        try:
            if platform.windows:
                os.mkdir(path)  # default mode: inherit the parent's ACL
            else:
                os.mkdir(path, 0o700)
        except FileExistsError:
            continue
        return path


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def materialise(source: Path, *, archive: bool, dest: Path) -> Path:
    """Extract the archive (or copy the tree) into ``dest``; return the bundle root."""
    dest.mkdir(parents=True, exist_ok=True)
    if archive:
        with tarfile.open(source, "r:xz") as tf:
            tf.extractall(dest, filter="data")
        root = dest / ARCHIVE_ROOT
    else:
        root = dest / ARCHIVE_ROOT
        shutil.copytree(source, root, symlinks=True)
    if not (root / "bin").is_dir():
        raise SmokeError(f"{root}/bin missing after extraction")
    return root


def check_relocated(root: Path, *, allow_prefix_present: bool) -> str:
    marker = root / ".build_prefix"
    if not marker.is_file():
        raise SmokeError(f"{marker} missing: not a bundle this build produced")
    recorded = Path(marker.read_text().strip())
    if os.path.normcase(os.path.realpath(recorded)) == os.path.normcase(os.path.realpath(root)):
        raise SmokeError(f"extracted root equals the build prefix {recorded}: nothing was relocated")
    if recorded.exists():
        if not allow_prefix_present:
            raise SmokeError(
                f"build prefix {recorded} still exists: remove it first, or pass "
                "--allow-build-prefix-present for a knowingly weaker run"
            )
        return f"WEAK: build prefix {recorded} still exists"
    return f"build prefix {recorded} is gone"


def check_runtime_present(root: Path) -> None:
    missing = [d for d in VC_RUNTIME_DLLS if not (root / "bin" / d).is_file()]
    if missing:
        raise SmokeError(f"bin lacks {', '.join(missing)} (0xC0000135 on a clean machine)")
    if not (root / "THIRD-PARTY-NOTICES.txt").is_file():
        raise SmokeError("THIRD-PARTY-NOTICES.txt missing from the bundle")


def _win_norm(path: str) -> str:
    """Windows paths compare case-insensitively with either slash; done by hand
    so the check behaves the same when run under another OS."""
    return path.replace("\\", "/").rstrip("/").lower()


def check_loaded_modules(modules: Sequence[str], bin_dir: Path) -> list[str]:
    """Every VC++ runtime module the process loaded must be bundle/bin's own.
    Non-vacuous: at least one runtime module must be present in the listing."""
    wanted = {d.lower() for d in VC_RUNTIME_DLLS}
    seen: list[str] = []
    for m in modules:
        name = Path(m.replace("\\", "/")).name.lower()
        # The four shipped files, or any versioned VC++ runtime module (msvcp120, vcruntime140_2, ...).
        # NOT a bare 'msvcp' prefix: msvcp_win.dll is a Windows component in System32 (nexus-f9bgu.9).
        if name in wanted or _VERSIONED_VC_RUNTIME_RE.match(name):
            seen.append(m)
            if _win_norm(m) != _win_norm(str(bin_dir / name)):
                raise SmokeError(f"{name} loaded from {m}, not from the bundle's {bin_dir}")
    if not seen:
        raise SmokeError("module listing held no VC++ runtime module: the check saw nothing")
    return seen


def smoke(
    root: Path,
    work: Path,
    *,
    port: int,
    expect_vector: str,
    platform: Platform,
    runner: Runner,
    base_env: Mapping[str, str],
    emit=print,
) -> None:
    bin_dir = root / "bin"
    env = scrubbed_env(bin_dir, platform, base_env)
    user = getpass.getuser()
    data = work / "pgdata"
    pg_log = work / "postgres.log"
    ctl_out = work / "pg_ctl.out"
    exe = platform.exe

    def tool(name: str) -> str:
        return str(bin_dir / f"{name}{exe}")

    def sql(query: str) -> str:
        rc, out = runner.capture(
            [tool("psql"), "-h", "127.0.0.1", "-p", str(port), "-U", user, "-d", "postgres",
             "-v", "ON_ERROR_STOP=1", "-q", "-tA", "-c", query],
            env=env,
        )
        if rc != 0:
            raise SmokeError(f"psql failed ({rc}) on {query!r}: {out.strip()}")
        return out.strip()

    def step(name: str, detail: str) -> None:
        emit(f"SMOKE {name}: {detail}")

    t0 = time.monotonic()
    rc, out = runner.capture(
        [tool("initdb"), "--no-locale", "-E", "UTF8", "-A", "trust", "-U", user, "-D", str(data)],
        env=env,
    )
    if rc != 0:
        raise SmokeError(f"initdb failed ({rc}): {out.strip()}")
    step("initdb", f"ok in {time.monotonic() - t0:.1f}s")

    started = False
    try:
        t0 = time.monotonic()
        rc = runner.to_file(
            [tool("pg_ctl"), "-D", str(data), "-l", str(pg_log), "-w", "-t", "60",
             "-o", f"-p {port} -c listen_addresses=127.0.0.1", "start"],
            env=env, out=ctl_out,
        )
        if rc != 0:
            tail = pg_log.read_text(errors="replace")[-2000:] if pg_log.exists() else ""
            raise SmokeError(f"pg_ctl start failed ({rc}); postgres log tail:\n{tail}")
        started = True
        step("pg_ctl start", f"ok in {time.monotonic() - t0:.1f}s, output in {ctl_out.name}")

        step("server", sql("SELECT version()").splitlines()[0])

        sql("CREATE EXTENSION vector")
        got = sql("SELECT extversion FROM pg_extension WHERE extname='vector'")
        if got != expect_vector:
            raise SmokeError(f"vector extversion {got!r}, expected {expect_vector!r}")
        step("vector", got)

        sql("CREATE EXTENSION pg_trgm")
        trgm = sql("SELECT extversion FROM pg_extension WHERE extname='pg_trgm'")
        if sql("SELECT similarity('night','nacht') > 0") != "t":
            raise SmokeError("pg_trgm similarity() did not return a positive score")
        step("pg_trgm", trgm)

        sql("CREATE TABLE smoke_items (id int PRIMARY KEY, embedding vector(3))")
        sql("INSERT INTO smoke_items SELECT i, ARRAY[i::real, (i%7)::real, (i%13)::real]::vector "
            "FROM generate_series(1,200) AS i")
        sql("CREATE INDEX smoke_hnsw ON smoke_items USING hnsw (embedding vector_l2_ops)")
        plan = sql("SET enable_seqscan=off; EXPLAIN (COSTS OFF) "
                   "SELECT id FROM smoke_items ORDER BY embedding <-> '[1,2,3]' LIMIT 5")
        if "smoke_hnsw" not in plan:
            raise SmokeError(f"query did not use the HNSW index; plan:\n{plan}")
        rows = sql("SET enable_seqscan=off; SELECT id FROM smoke_items "
                   "ORDER BY embedding <-> '[1,2,3]' LIMIT 5")
        ids = [ln for ln in rows.splitlines() if ln.strip().isdigit()]
        if len(ids) != 5:
            raise SmokeError(f"HNSW query returned {len(ids)} rows, expected 5: {rows!r}")
        step("hnsw", f"index used, 5 rows: {','.join(ids)}")

        if platform.windows:
            pid = (data / "postmaster.pid").read_text().splitlines()[0].strip()
            rc, listing = runner.capture(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 f"(Get-Process -Id {pid}).Modules | ForEach-Object {{ $_.FileName }}"],
                env=env,
            )
            if rc != 0:
                raise SmokeError(f"module listing failed ({rc}): {listing.strip()}")
            seen = check_loaded_modules(listing.splitlines(), bin_dir)
            names = ", ".join(sorted({Path(m.replace("\\", "/")).name.lower() for m in seen}))
            step("runtime modules", f"{names} loaded by the postmaster, all from the bundle's bin")
    finally:
        if started:
            rc = runner.to_file(
                [tool("pg_ctl"), "-D", str(data), "-m", "fast", "-w", "-t", "60", "stop"],
                env=env, out=ctl_out,
            )
            if rc != 0:
                emit(f"SMOKE WARNING: pg_ctl stop exited {rc}")
    step("pg_ctl stop", "ok")


def run(
    *,
    archive: Path | None,
    bundle: Path | None,
    workdir: Path | None,
    port: int,
    expect_vector: str,
    allow_prefix_present: bool,
    keep: bool,
    platform: Platform,
    runner: Runner,
    base_env: Mapping[str, str],
    emit=print,
) -> None:
    if (archive is None) == (bundle is None):
        raise SmokeError("pass exactly one of --archive and --bundle")
    if workdir is not None:
        workdir.mkdir(parents=True, exist_ok=True)
    # Always a fresh private subdirectory: --keep off deletes it, never the parent.
    work = make_workdir(workdir, platform)
    try:
        src = archive if archive is not None else bundle
        assert src is not None
        root = materialise(src, archive=archive is not None, dest=work / "relocated")
        emit(f"SMOKE relocated: {root}")
        emit(f"SMOKE build prefix: {check_relocated(root, allow_prefix_present=allow_prefix_present)}")
        if platform.windows:
            check_runtime_present(root)
            emit("SMOKE runtime: four VC++ DLLs and the notice are in the bundle")
        smoke(root, work, port=port or free_port(), expect_vector=expect_vector,
              platform=platform, runner=runner, base_env=base_env, emit=emit)
        emit("SMOKE PASSED")
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--archive", type=Path)
    p.add_argument("--bundle", type=Path)
    p.add_argument("--workdir", type=Path)
    p.add_argument("--port", type=int, default=0, help="0 picks a free port")
    p.add_argument("--expect-vector", default=EXPECT_VECTOR)
    p.add_argument("--allow-build-prefix-present", action="store_true")
    p.add_argument("--keep", action="store_true")
    args = p.parse_args(argv)
    try:
        run(
            archive=args.archive, bundle=args.bundle, workdir=args.workdir, port=args.port,
            expect_vector=args.expect_vector, allow_prefix_present=args.allow_build_prefix_present,
            keep=args.keep, platform=Platform.current(), runner=Runner(), base_env=os.environ,
        )
    except SmokeError as exc:
        print(f"SMOKE FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
