# SPDX-License-Identifier: AGPL-3.0-or-later
"""``pg_ctl start -w`` for test fixtures, with no pipe the postmaster can inherit.

pg_ctl's postmaster inherits pg_ctl's standard handles. On Windows a captured
pipe therefore keeps a writer open after pg_ctl exits, the read never sees EOF,
and ``subprocess.run(..., capture_output=True)`` never returns (measured on
qwentescence, 2026-10-07: DEVNULL returns in 0.3 s, capture timed out at 30 s;
nexus-925vc). ``nexus.db.pg_provision._pg_ctl_start_detached`` avoids it the same
way, by sending pg_ctl's output to a file in the data directory.

:func:`pg_ctl_start` writes pg_ctl's stdout and stderr to ``pg_ctl.out`` and
``pg_ctl.err`` in *pgdata* and reads them back into the returned
``CompletedProcess``, so a caller that reported ``proc.stderr`` keeps doing so.
The server's own log is *pglog* (``-l``). ``tests/test_pg_ctl_start.py`` lints
``tests/`` for a ``pg_ctl ... start`` call that captures pipes.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

#: Names of the files pg_ctl's own stdout and stderr go to, inside *pgdata*.
PG_CTL_STDOUT = "pg_ctl.out"
PG_CTL_STDERR = "pg_ctl.err"


def _read(path: Path, offset: int, text: bool) -> str | bytes:
    """What was appended to *path* after *offset* (a rerun appends, never truncates)."""
    try:
        data = path.read_bytes()[offset:]
    except OSError:
        data = b""
    return data.decode(errors="replace") if text else data


def pg_ctl_start(
    pg_ctl: str | os.PathLike[str],
    pgdata: str | os.PathLike[str],
    pglog: str | os.PathLike[str],
    options: str,
    *,
    check: bool = True,
    text: bool = True,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run ``pg_ctl -D pgdata -l pglog -o options start -w``.

    Returns a ``CompletedProcess`` whose ``stdout``/``stderr`` are what pg_ctl
    wrote. With *check*, a non-zero exit raises ``CalledProcessError`` carrying
    both."""
    data = Path(pgdata)
    args = [str(pg_ctl), "-D", str(pgdata), "-l", str(pglog), "-o", options, "start", "-w"]
    out_path, err_path = data / PG_CTL_STDOUT, data / PG_CTL_STDERR
    with out_path.open("ab") as out, err_path.open("ab") as err:
        out_start, err_start = out.tell(), err.tell()
        proc = subprocess.run(  # noqa: S603 — fixed argv from the test's own PG bundle
            args, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
            timeout=timeout, env=env, check=False,
        )
    stdout = _read(out_path, out_start, text)
    stderr = _read(err_path, err_start, text)
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, args, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(args, proc.returncode, stdout=stdout, stderr=stderr)
