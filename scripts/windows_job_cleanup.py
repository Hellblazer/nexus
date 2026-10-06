#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-of-job cleanup for the Windows release runner (RDR-224, nexus-f9bgu.27/.28).

``win-release`` is one persistent machine that runs every Windows job, and the
workflows cancel a superseded run (``cancel-in-progress``). The runner kills the
step it is running, but the smokes start Postgres and the engine in their own
process groups, and those can outlive a cancelled step: a stray ``postgres.exe``
keeps its data directory open (so the runner cannot empty ``RUNNER_TEMP``) and a
stray engine keeps its port.

This stops them, and only them. A process is killed when ALL of:

  * its image name is one of ``--image`` (default: nexus-service.exe, postgres.exe,
    pg_ctl.exe, initdb.exe), and
  * its executable path lies under one of the ``--under`` roots (the job's
    workspace and ``RUNNER_TEMP``).

Never by bare name: the box runs other Postgres instances (the host's own work),
and a name-only match would kill them. Paths are compared in their long,
resolved form, case-insensitively, on a path boundary (``C:\\w`` is not ``C:\\work``).

Exit 0 when nothing matched or every match was stopped; 1 when a match could
not be stopped or the process listing came back empty (the job then fails visibly
rather than leaving the box dirty or reporting a clean it never checked);
2 on bad arguments or off Windows. Stdlib only. Tests: tests/test_windows_job_cleanup.py.

Usage::

    python scripts/windows_job_cleanup.py --under "$env:RUNNER_TEMP" --under "$env:GITHUB_WORKSPACE"
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

DEFAULT_IMAGES: tuple[str, ...] = ("nexus-service.exe", "postgres.exe", "pg_ctl.exe", "initdb.exe")

_LIST_PS = (
    "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath } | "
    "Select-Object ProcessId, Name, ExecutablePath | ConvertTo-Json -Compress"
)


@dataclass(frozen=True)
class Proc:
    pid: int
    name: str
    path: str


def _norm(path: str) -> str:
    """Windows path comparison form: forward slashes, no trailing slash, lower case."""
    return path.replace("\\", "/").rstrip("/").lower()


def is_under(path: str, root: str) -> bool:
    """*path* is *root* itself or lies below it, on a path boundary."""
    p, r = _norm(path), _norm(root)
    return bool(r) and (p == r or p.startswith(r + "/"))


def select_victims(
    procs: Sequence[Proc],
    roots: Sequence[str],
    images: Sequence[str] = DEFAULT_IMAGES,
    *,
    long_path: Callable[[str], str] = lambda p: p,
    self_pid: int | None = None,
) -> list[Proc]:
    """The processes to stop: image in *images* AND executable path under one of *roots*.

    An empty *roots* selects nothing (a cleanup with no scope must not fall back to the bare name)."""
    wanted = {i.lower() for i in images}
    long_roots = [long_path(r) for r in roots if r]
    out: list[Proc] = []
    for proc in procs:
        if proc.pid == self_pid or proc.name.lower() not in wanted:
            continue
        exe = long_path(proc.path)
        if any(is_under(exe, root) for root in long_roots):
            out.append(proc)
    return out


def parse_process_list(text: str) -> list[Proc]:
    """Processes from the PowerShell JSON (one object, or a list of them, or nothing)."""
    text = text.strip()
    if not text:
        return []
    data = json.loads(text)
    items = [data] if isinstance(data, dict) else data
    return [Proc(int(i["ProcessId"]), str(i["Name"]), str(i["ExecutablePath"])) for i in items if i.get("ExecutablePath")]


def list_processes() -> list[Proc]:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _LIST_PS],
        env={k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"},  # pwsh 7's module path breaks Windows PowerShell's own modules (nexus-f9bgu.14)
        stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"process listing failed ({proc.returncode}): {proc.stderr.strip()[:300]}")
    return parse_process_list(proc.stdout)


def kill(pid: int) -> bool:
    """taskkill /F /PID; true when the process is gone afterwards (already gone counts)."""
    r = subprocess.run(["taskkill", "/F", "/PID", str(pid)], stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace")
    return r.returncode == 0 or "not found" in (r.stdout + r.stderr).lower()


def run(
    roots: Sequence[str],
    images: Sequence[str],
    *,
    lister: Callable[[], list[Proc]] = list_processes,
    killer: Callable[[int], bool] = kill,
    long_path: Callable[[str], str] = lambda p: p,
    emit: Callable[[str], None] = print,
    dry_run: bool = False,
) -> int:
    procs = lister()
    # The denominator. A listing that returned nothing on a running Windows machine (this very process
    # is on it) is a broken listing, and "no stray process" over it would be a vacuous clean.
    emit(f"cleanup: examined {len(procs)} process(es) with an executable path")
    if not procs:
        emit("cleanup: the process listing is empty, so nothing was checked")
        return 1
    victims = select_victims(procs, roots, images, long_path=long_path, self_pid=os.getpid())
    if not victims:
        emit("cleanup: no stray engine or postgres process under the job's directories")
        return 0
    failed = 0
    for v in victims:
        if dry_run:
            emit(f"cleanup: would stop {v.name} pid {v.pid} ({v.path})")
        elif killer(v.pid):
            emit(f"cleanup: stopped {v.name} pid {v.pid} ({v.path})")
        else:
            failed += 1
            emit(f"cleanup: could NOT stop {v.name} pid {v.pid} ({v.path})")
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--under", action="append", default=[], help="a directory the job owns (repeatable); only processes started from beneath one are touched")
    p.add_argument("--image", action="append", help=f"image name to stop (repeatable; default {', '.join(DEFAULT_IMAGES)})")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    if sys.platform != "win32":
        print("windows_job_cleanup: Windows only", file=sys.stderr)
        return 2
    roots = [r for r in args.under if r]
    if not roots:
        print("windows_job_cleanup: pass at least one --under directory (a cleanup with no scope kills nothing)", file=sys.stderr)
        return 2
    import pg_bundle_windows_smoke as sm  # noqa: PLC0415 - the one long-path resolver, shared

    return run(roots, args.image or DEFAULT_IMAGES, long_path=sm.resolve_long_path, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
