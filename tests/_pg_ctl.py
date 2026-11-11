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
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

#: Names of the files pg_ctl's own stdout and stderr go to, inside *pgdata*.
PG_CTL_STDOUT = "pg_ctl.out"
PG_CTL_STDERR = "pg_ctl.err"

#: Bytes a unix socket path may use: ``sun_path`` holds 108 on Linux and 104 on macOS, each
#: including the terminating NUL.
SUN_PATH_MAX = 103 if sys.platform == "darwin" else 107
#: The socket file Postgres puts in its ``-k`` directory, at the widest port.
_SOCKET_FILE = "/.s.PGSQL.65535"


def _socket_dir(options: str) -> str | None:
    """The directory a ``-k`` in the postmaster *options* names, or None."""
    try:
        parts = shlex.split(options)
    except ValueError:
        return None
    for i, part in enumerate(parts):
        if part == "-k" and i + 1 < len(parts):
            return parts[i + 1]
        if part.startswith("-k") and len(part) > 2:
            return part[2:]
    return None


@pytest.fixture
def sun_path_limit_lifted(monkeypatch: pytest.MonkeyPatch) -> None:
    """For tests that run the real :func:`pg_ctl_start` (with a faked ``subprocess.run`` or
    ``pg_ctl``) on a cluster under ``tmp_path``, which is past ``sun_path`` on macOS and under a
    CI tmpfs basetemp; no postmaster starts there, so the length is not what they test. Import it
    by name into the test module."""
    monkeypatch.setattr(sys.modules[__name__], "SUN_PATH_MAX", 10_000)


def check_socket_dir(options: str) -> None:
    """Raise ``ValueError`` when the ``-k`` directory in *options* would put the postmaster's unix
    socket past ``sun_path``. Postgres then refuses to start and pg_ctl says only ``could not start
    server``: one hand-run shard whose tempdir was 71 bytes reported 883 such errors (labrun-pex).
    Every cluster started with ``-k`` passes here, whatever root it was made under; one started
    without ``-k`` (TCP only) is not checked. Windows has no ``-k`` socket and is not checked."""
    if os.name == "nt":
        return
    sock_dir = _socket_dir(options)
    if sock_dir is None:
        return
    length = len(os.fsencode(sock_dir)) + len(_SOCKET_FILE)
    if length > SUN_PATH_MAX:
        raise ValueError(
            f"the unix socket under {sock_dir} would be {length} bytes, past the {SUN_PATH_MAX}-byte "
            f"sun_path limit, so Postgres could not start there; run with a shorter TMPDIR or set "
            f"NX_TEST_PG_ROOT to a short directory"
        )


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
    both. A ``-k`` socket directory too long for ``sun_path`` raises ``ValueError`` before pg_ctl
    runs (:func:`check_socket_dir`)."""
    check_socket_dir(options)
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
