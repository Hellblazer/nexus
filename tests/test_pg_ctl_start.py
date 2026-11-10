# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-925vc: fixtures start pg_ctl through ``tests._pg_ctl.pg_ctl_start``.

``pg_ctl start -w`` leaves a postmaster running that inherited pg_ctl's standard
handles. With ``capture_output=True`` those handles are pipes, so the read in
``subprocess.run`` waits for the postmaster to exit, not pg_ctl. On Windows the
fixtures hung there (measured on qwentescence: DEVNULL returned in 0.3 s, capture
timed out at 30 s).

A fake pg_ctl that leaves a background child holding its stdout reproduces the
same wait on POSIX, so the helper is pinned on every POSIX box rather than only on
Windows. The lint keeps captured-pipe ``pg_ctl ... start`` calls out of ``tests/``.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from tests._child_process import kill_if_identity
from tests import _pg_ctl
from tests._pg_ctl import PG_CTL_STDERR, PG_CTL_STDOUT, pg_ctl_start

_TESTS = Path(__file__).resolve().parent

#: How long the fake postmaster holds pg_ctl's handles.
_POSTMASTER_LIFETIME_S = 30


def _fake_pg_ctl(tmp_path: Path, exit_code: int = 0) -> tuple[Path, Path]:
    """A pg_ctl that starts a long-lived child with its own stdout and stderr,
    prints a line to each, and exits. Returns (script, pid file of the child).

    The child is a Python sleeper whose command line ends in this test's pid-file path, so the cleanup can
    tell it from every other ``sleep`` on the box (``sleep 30`` also matches ``sleep 300``)."""
    pid_file = tmp_path / "postmaster.pid.fake"
    script = tmp_path / "pg_ctl"
    script.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        "{sys.executable}" -c "import time; time.sleep({_POSTMASTER_LIFETIME_S})  # {pid_file}" &
        echo $! > "{pid_file}"
        echo "waiting for server to start.... done"
        echo "a warning" >&2
        exit {exit_code}
    """))
    script.chmod(0o755)
    return script, pid_file


def _kill_fake_postmaster(pid_file: Path) -> None:
    """Stop the fake postmaster the fake pg_ctl recorded, when the number still names it: the sleeper whose
    command line ends in THIS test's pid-file path. The pid file outlives the sleeper, so a bare kill could
    reach whatever the number was reused for (nexus-xzorj)."""
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return
    kill_if_identity(pid, str(pid_file))


posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the fake pg_ctl is a POSIX shell script")


@posix_only
def test_pg_ctl_start_returns_while_the_postmaster_still_holds_its_handles(tmp_path) -> None:
    pgdata = tmp_path / "data"
    pgdata.mkdir()
    script, pid_file = _fake_pg_ctl(tmp_path)
    try:
        started = time.monotonic()
        proc = pg_ctl_start(script, pgdata, pgdata / "pg.log", "-p 5999", timeout=_POSTMASTER_LIFETIME_S / 2)
        assert time.monotonic() - started < 10
    finally:
        _kill_fake_postmaster(pid_file)
    assert proc.returncode == 0
    assert "server to start" in proc.stdout
    assert proc.stderr == "a warning\n"
    assert proc.args[-2:] == ["start", "-w"]
    assert proc.args[1:7] == ["-D", str(pgdata), "-l", str(pgdata / "pg.log"), "-o", "-p 5999"]
    # The output stays on disk for a post-mortem.
    assert "server to start" in (pgdata / PG_CTL_STDOUT).read_text()
    assert (pgdata / PG_CTL_STDERR).read_text() == "a warning\n"


@posix_only
def test_a_failed_start_raises_with_what_pg_ctl_said(tmp_path) -> None:
    pgdata = tmp_path / "data"
    pgdata.mkdir()
    (pgdata / PG_CTL_STDERR).write_text("from an earlier start\n")
    script, pid_file = _fake_pg_ctl(tmp_path, exit_code=1)
    try:
        with pytest.raises(subprocess.CalledProcessError) as caught:
            pg_ctl_start(script, pgdata, pgdata / "pg.log", "-p 5999", timeout=_POSTMASTER_LIFETIME_S / 2)
    finally:
        _kill_fake_postmaster(pid_file)
    try:
        unchecked = pg_ctl_start(script, pgdata, pgdata / "pg.log", "-p 5999", check=False)
    finally:
        _kill_fake_postmaster(pid_file)
    assert caught.value.returncode == 1
    # Only this run's output, not the earlier start's.
    assert caught.value.stderr == "a warning\n"
    assert unchecked.returncode == 1 and unchecked.stderr == "a warning\n"


@posix_only
def test_control_capture_output_waits_for_the_postmaster(tmp_path) -> None:
    """The fake reproduces the hang the helper removes. If capture stops waiting,
    the test above proves nothing."""
    pgdata = tmp_path / "data"
    pgdata.mkdir()
    script, pid_file = _fake_pg_ctl(tmp_path)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            subprocess.run(  # noqa: S603
                [str(script), "-D", str(pgdata), "start", "-w"], capture_output=True, timeout=2,
            )
    finally:
        _kill_fake_postmaster(pid_file)


# ── lint: no pg_ctl start with captured pipes in tests/ ──────────────────────

_SPAWNERS = {"run", "Popen", "check_call", "check_output", "call"}

#: This file runs a captured start on purpose (the control above).
_EXEMPT = {Path(__file__).resolve()}


def _captured_pg_ctl_starts(src: str) -> list[int]:
    """Lines of spawn calls whose literal argv names pg_ctl and ``start`` and
    that read pg_ctl's output through a pipe."""
    hits: list[int] = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in _SPAWNERS or not isinstance(node.args[0], (ast.List, ast.Tuple)):
            continue
        argv = node.args[0].elts
        if not argv or "pg_ctl" not in (ast.get_source_segment(src, argv[0]) or "").lower():
            continue
        if not any(isinstance(e, ast.Constant) and e.value == "start" for e in argv):
            continue
        kwargs = {k.arg: ast.get_source_segment(src, k.value) or "" for k in node.keywords}
        piped = (
            name == "check_output"
            or kwargs.get("capture_output") == "True"
            or any("PIPE" in kwargs.get(k, "") for k in ("stdout", "stderr"))
        )
        if piped:
            hits.append(node.lineno)
    return hits


@pytest.mark.lint
def test_no_fixture_starts_pg_ctl_with_captured_pipes() -> None:
    offenders: list[str] = []
    scanned = 0
    for path in sorted(_TESTS.rglob("*.py")):
        if path.resolve() in _EXEMPT:
            continue
        src = path.read_text(encoding="utf-8")
        if "pg_ctl" not in src.lower():
            continue
        scanned += 1
        offenders += [f"{path.relative_to(_TESTS.parent)}:{line}" for line in _captured_pg_ctl_starts(src)]
    # Non-vacuity: dozens of tests/ files drive pg_ctl; a scan that saw none is broken.
    assert scanned >= 20, scanned
    assert offenders == [], (
        "pg_ctl start with captured pipes hangs on Windows (nexus-925vc); "
        "use tests._pg_ctl.pg_ctl_start:\n" + "\n".join(offenders)
    )


@pytest.mark.lint
def test_the_lint_sees_each_captured_form() -> None:
    bad = [
        'subprocess.run([str(_PG_CTL), "-D", d, "start", "-w"], check=True, capture_output=True)',
        'subprocess.run([bin_dir / "pg_ctl", "start"], stdout=subprocess.PIPE)',
        'subprocess.Popen([pg_ctl, "-D", d, "start"], stderr=subprocess.PIPE)',
        'subprocess.check_output([PG_CTL, "-D", d, "start", "-w"])',
    ]
    good = [
        'pg_ctl_start(_PG_CTL, d, log, "-p 1")',
        'subprocess.run([str(_PG_CTL), "-D", d, "stop", "-m", "fast"], capture_output=True)',
        'subprocess.run([str(_PG_CTL), "-D", d, "start", "-w"], stdout=subprocess.DEVNULL)',
    ]
    for sample in bad:
        assert _captured_pg_ctl_starts(sample) == [1], sample
    for sample in good:
        assert _captured_pg_ctl_starts(sample) == [], sample


# ── labrun-pex: the -k socket path is checked before pg_ctl runs ────────────────────────────────
# A postmaster started with ``-k <dir>`` puts its unix socket there; a path past ``sun_path`` makes
# Postgres refuse to start with only ``pg_ctl: could not start server`` to show for it (one hand-run
# shard on chas whose tempdir was 71 bytes reported 883 such errors). pg_ctl_start is where every
# ``-k`` start passes, so the check is there.

_POSIX_SOCKET = pytest.mark.skipif(os.name == "nt", reason="no -k unix socket on Windows")


def _recording_pg_ctl(tmp_path: Path) -> tuple[Path, Path]:
    ran = tmp_path / "pg_ctl-ran"
    exe = tmp_path / "pg_ctl"
    exe.write_text(f"#!/bin/sh\ntouch {ran}\nexit 0\n")
    exe.chmod(0o755)
    return exe, ran


def _socket_pgdata(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@_POSIX_SOCKET
def test_a_socket_dir_past_sun_path_fails_loud_before_pg_ctl_runs(tmp_path, monkeypatch) -> None:
    exe, ran = _recording_pg_ctl(tmp_path)
    pgdata = _socket_pgdata(tmp_path)
    monkeypatch.setattr(_pg_ctl, "SUN_PATH_MAX", len(os.fsencode(str(pgdata))) + 10)
    with pytest.raises(ValueError, match="sun_path") as err:
        _pg_ctl.pg_ctl_start(exe, pgdata, pgdata / "pg.log", f"-p 5555 -k {pgdata}", check=False)
    assert "TMPDIR" in str(err.value) and "NX_TEST_PG_ROOT" in str(err.value)
    assert not ran.exists(), "pg_ctl ran for a socket path Postgres would refuse"


@_POSIX_SOCKET
def test_a_socket_dir_that_fits_starts_as_before(tmp_path, monkeypatch) -> None:
    exe, ran = _recording_pg_ctl(tmp_path)
    pgdata = _socket_pgdata(tmp_path)
    monkeypatch.setattr(_pg_ctl, "SUN_PATH_MAX", len(os.fsencode(str(pgdata))) + len("/.s.PGSQL.65535"))
    proc = _pg_ctl.pg_ctl_start(exe, pgdata, pgdata / "pg.log", f"-k {pgdata}", check=False)
    assert proc.returncode == 0 and ran.exists()


@_POSIX_SOCKET
def test_a_tcp_only_start_without_k_is_not_checked(tmp_path, monkeypatch) -> None:
    """``provision()``-style clusters run with no socket; a long data dir is fine for them."""
    exe, ran = _recording_pg_ctl(tmp_path)
    pgdata = _socket_pgdata(tmp_path)
    monkeypatch.setattr(_pg_ctl, "SUN_PATH_MAX", 1)
    _pg_ctl.pg_ctl_start(exe, pgdata, pgdata / "pg.log", "-p 5555", check=False)
    assert ran.exists()


@_POSIX_SOCKET
@pytest.mark.parametrize("options", ["-p 1 -k /x/y", "-k/x/y -p 1", "-c a=b -k '/x/y'"])
def test_the_socket_dir_is_read_from_every_k_spelling(options) -> None:
    assert _pg_ctl._socket_dir(options) == "/x/y"


@_POSIX_SOCKET
def test_the_limit_counts_bytes_not_characters(monkeypatch) -> None:
    sock_dir = "/" + "é" * 10  # 21 bytes, 11 characters
    monkeypatch.setattr(_pg_ctl, "SUN_PATH_MAX", 11 + len("/.s.PGSQL.65535"))
    with pytest.raises(ValueError, match="sun_path"):
        _pg_ctl.check_socket_dir(f"-k {sock_dir}")


@_POSIX_SOCKET
def test_the_limit_is_the_platforms() -> None:
    assert _pg_ctl.SUN_PATH_MAX == (103 if sys.platform == "darwin" else 107)
