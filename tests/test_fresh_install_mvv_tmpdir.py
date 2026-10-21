# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-0tqr7: ``tests/e2e/fresh-install-mvv.sh`` puts its bulky work tree
under ``${TMPDIR:-/tmp}`` and removes it on exit, success or failure.

Before: ``mktemp -d /tmp/fresh-mvv-XXXXXX``. On hellmini TMPDIR lives on
/Volumes/Bulk, so every run left a ~2.5 GB tree on the internal SSD, and a
failed run kept it on purpose.

The usual worry with moving off /tmp is the macOS 104-byte unix-socket path
limit. The journey creates no unix socket (Postgres is TCP-only), which is
the premise these tests pin, so the work tree can live under any TMPDIR.

The behavioural tests run the script's real setup-and-cleanup block (sliced
out by anchors, so it is the shipped text, not a copy) in a bash subshell.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "tests" / "e2e" / "fresh-install-mvv.sh"
PG_PROVISION = REPO_ROOT / "src" / "nexus" / "db" / "pg_provision.py"

_BEGIN = 'MVV_TMP="${TMPDIR:-/tmp}"'
_END = "trap 'exit 143' TERM"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _block() -> str:
    text = _text()
    start = text.index(_BEGIN)
    end = text.index(_END, start) + len(_END)
    return text[start:end]


def _run_block(tmpdir: str, tail: str, **extra_env: str) -> tuple[subprocess.CompletedProcess, str]:
    """Run the setup/cleanup block, then *tail*; return (result, WORK path)."""
    marker = Path(tmpdir) / "work.path"
    prog = (
        "set -euo pipefail\n"
        "REPO_ROOT=/nonexistent\n"
        + _block()
        + '\nprintf %s "$WORK" > "$MARKER"\n'
        + tail
    )
    env = {"PATH": "/usr/bin:/bin", "TMPDIR": tmpdir, "MARKER": str(marker), **extra_env}
    res = subprocess.run(
        ["bash", "-c", prog], capture_output=True, text=True, env=env, timeout=60
    )
    return res, marker.read_text(encoding="utf-8") if marker.exists() else ""


def _long_tmpdir(tmp_path: Path) -> str:
    d = tmp_path / ("t" * 40) / ("u" * 40)  # well past 60 chars
    d.mkdir(parents=True)
    assert len(str(d)) >= 60
    return str(d)


def test_work_is_created_under_tmpdir_not_hardcoded_tmp(tmp_path: Path) -> None:
    tmpdir = _long_tmpdir(tmp_path)
    res, work = _run_block(tmpdir, "GATE_OK=1\nexit 0\n")
    assert res.returncode == 0, res.stderr
    assert work.startswith(tmpdir + "/fresh-mvv-"), work


def test_trailing_slash_tmpdir_does_not_double_the_slash(tmp_path: Path) -> None:
    tmpdir = _long_tmpdir(tmp_path)
    res, work = _run_block(tmpdir + "/", "GATE_OK=1\nexit 0\n")
    assert res.returncode == 0, res.stderr
    assert "//" not in work, work
    assert work.startswith(tmpdir + "/fresh-mvv-"), work


def test_work_removed_on_success(tmp_path: Path) -> None:
    tmpdir = _long_tmpdir(tmp_path)
    res, work = _run_block(tmpdir, 'echo big > "$WORK/venv-stand-in"\nGATE_OK=1\nexit 0\n')
    assert res.returncode == 0, res.stderr
    assert work and not Path(work).exists()
    assert [p.name for p in Path(tmpdir).iterdir() if p.name != "work.path"] == []


def test_work_removed_on_failure_but_logs_survive(tmp_path: Path) -> None:
    tmpdir = _long_tmpdir(tmp_path)
    tail = (
        'echo bulky > "$WORK/venv-stand-in"\n'
        'echo leg-log > "$LOGS/leg.log"\n'
        # Service-side root causes live under the sandbox HOME (item 2).
        'mkdir -p "$HOME_DIR/.config/nexus/logs" "$HOME_DIR/.config/nexus/postgres"\n'
        'echo engine-root-cause > "$HOME_DIR/.config/nexus/logs/storage_service.log"\n'
        'echo hs-err > "$HOME_DIR/.config/nexus/logs/hs_err_1234.log"\n'
        'echo pg-root-cause > "$HOME_DIR/.config/nexus/postgres/pg.log"\n'
        'echo ctl-out > "$HOME_DIR/.config/nexus/postgres/pg_ctl.out"\n'
        'echo "token: SECRET-CONFIG" > "$HOME_DIR/.config/nexus/config.yml"\n'
        'echo SECRET-CRED > "$HOME_DIR/.config/nexus/postgres/pg_credentials"\n'
        '_fail boom\n'
    )
    res, work = _run_block(tmpdir, tail)
    assert res.returncode == 1
    assert "FRESH-INSTALL MVV FAILED: boom" in res.stderr
    assert work and not Path(work).exists(), "failed run must not strand the work tree"
    evidence = Path(tmpdir) / (Path(work).name + "-logs")
    assert (evidence / "leg.log").read_text() == "leg-log\n"
    assert str(evidence) in res.stderr
    assert not (evidence / "venv-stand-in").exists()
    # Engine and PG logs survive, and the message names what was kept.
    assert (evidence / "engine-logs" / "storage_service.log").read_text() == "engine-root-cause\n"
    assert (evidence / "engine-logs" / "hs_err_1234.log").exists()
    assert (evidence / "pg.log").read_text() == "pg-root-cause\n"
    assert (evidence / "pg_ctl.out").read_text() == "ctl-out\n"
    for kept in ("legs", "engine-logs", "pg.log", "pg_ctl.out"):
        assert kept in res.stderr, res.stderr
    # Config and credentials are never copied, anywhere in the evidence tree.
    leaked = [
        str(f) for f in evidence.rglob("*")
        if f.is_file() and ("SECRET" in f.read_text(errors="replace") or f.name in ("config.yml", "pg_credentials"))
    ]
    assert leaked == []


def test_keep_knob_preserves_the_whole_tree_on_failure(tmp_path: Path) -> None:
    tmpdir = _long_tmpdir(tmp_path)
    res, work = _run_block(tmpdir, 'echo bulky > "$WORK/venv-stand-in"\n_fail boom\n',
                           FRESH_MVV_KEEP_WORK="1")
    assert res.returncode == 1
    assert (Path(work) / "venv-stand-in").exists()
    assert "FAILURE EVIDENCE PRESERVED" in res.stderr


def test_sigterm_still_runs_cleanup(tmp_path: Path) -> None:
    tmpdir = _long_tmpdir(tmp_path)
    res, work = _run_block(tmpdir, 'kill -TERM $$\nsleep 5\n')
    assert res.returncode == 143, (res.returncode, res.stderr)
    assert work and not Path(work).exists()


def test_no_hardcoded_tmp_mktemp_remains() -> None:
    for i, line in enumerate(_text().splitlines(), 1):
        code = line.split("#", 1)[0]
        assert "mktemp" not in code or "/tmp/" not in code, f"line {i}: {line}"


def test_journey_creates_no_unix_socket() -> None:
    """The premise that lets WORK sit under a long TMPDIR (macOS 104-byte
    sun_path limit): Postgres is TCP-only, and neither the script nor the
    cluster start asks for a unix socket."""
    pg = PG_PROVISION.read_text(encoding="utf-8")
    assert "f\"unix_socket_directories = ''\"" in pg, (
        "pg_provision must keep unix_socket_directories empty; if a socket is "
        "ever added, its dir must be short and OUTSIDE $WORK in fresh-install-mvv.sh"
    )
    start = pg[pg.index("# No \"-k <pgdata>\""):]
    start = start[: start.index("timeout=_PG_CTL_WAIT_TIMEOUT_S")]
    assert '"-k"' not in start
    script = _text()
    for needle in ("AF_UNIX", ".s.PGSQL", "unix_socket"):
        for line in script.splitlines():
            assert needle not in line.split("#", 1)[0], f"{needle!r}: {line}"


# ── postmaster reaping before the rm (item 3) ───────────────────────────────

_PERL = "/usr/bin/perl"


def test_cleanup_reaps_a_postmaster_under_work_before_removing_it(tmp_path: Path) -> None:
    """The service stop is `|| true`; a postmaster left over a deleted datadir
    holds a SysV shm segment. A stand-in whose command line reads
    `postgres -D $WORK` must be dead and WORK gone after the exit."""
    assert Path(_PERL).exists(), "non-vacuity: the stand-in process needs perl"
    tmpdir = _long_tmpdir(tmp_path)
    pidfile = Path(tmpdir) / "stand-in.pid"
    tail = (
        f'{_PERL} -e \'sleep 60\' "postgres -D $WORK/home/.config/nexus/postgres" &\n'
        f'echo $! > "{pidfile}"\n'
        "sleep 0.5\n"
        "GATE_OK=1\nexit 0\n"
    )
    res, work = _run_block(tmpdir, tail)
    try:
        assert res.returncode == 0, res.stderr
        pid = int(pidfile.read_text())
        alive = subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0
        assert not alive, "the stand-in postmaster survived cleanup"
        assert work and not Path(work).exists()
    finally:
        if pidfile.exists():
            subprocess.run(["kill", "-KILL", pidfile.read_text().strip()], capture_output=True)


def test_cleanup_keeps_work_and_says_why_when_a_postmaster_survives(tmp_path: Path) -> None:
    """A stub `pgrep` that always reports a pid models a postmaster that
    outlives INT and KILL: removing the tree would orphan it, so WORK stays."""
    tmpdir = _long_tmpdir(tmp_path)
    stub = tmp_path / "stub-bin"
    stub.mkdir()
    pgrep = stub / "pgrep"
    pgrep.write_text("#!/bin/sh\necho 2147483646\n")
    pgrep.chmod(0o755)
    res, work = _run_block(
        tmpdir, "GATE_OK=1\nexit 0\n", PATH=f"{stub}:/usr/bin:/bin",
    )
    assert res.returncode == 0, res.stderr
    assert "WORK KEPT" in res.stderr and "postmaster" in res.stderr
    assert work and Path(work).exists()


# ── the no-socket census (item 4) ───────────────────────────────────────────


def _census() -> str:
    text = _text()
    start = text.index("NO_SOCKET_CENSUS=")
    end = text.index("\nGATE_OK=1\n", start)
    assert end > start
    return text[start:end]


def test_census_sits_immediately_before_the_gate_is_marked_ok() -> None:
    text = _text()
    assert text.count("find \"$WORK\" -type s") == 1
    assert text.index("find \"$WORK\" -type s") < text.index("\nGATE_OK=1\n", text.index("NO_SOCKET_CENSUS="))


def _run_census(work: Path) -> subprocess.CompletedProcess:
    prog = (
        "set -euo pipefail\n"
        '_fail() { echo "FRESH-INSTALL MVV FAILED: $*" >&2; exit 1; }\n'
        f'WORK="{work}"\n' + _census() + "\necho CENSUS-PASSED\n"
    )
    return subprocess.run(
        ["bash", "-c", prog], capture_output=True, text=True, timeout=30,
        env={"PATH": "/usr/bin:/bin"},
    )


def test_census_passes_on_a_socketless_tree(tmp_path: Path) -> None:
    (tmp_path / "w" / "home").mkdir(parents=True)
    (tmp_path / "w" / "home" / "f.txt").write_text("x")
    res = _run_census(tmp_path / "w")
    assert res.returncode == 0 and "CENSUS-PASSED" in res.stdout, res.stderr


def test_census_fails_naming_a_socket_under_work(tmp_path: Path) -> None:
    import os
    import socket

    work = tmp_path / "w"
    (work / "home").mkdir(parents=True)
    cwd = os.getcwd()
    os.chdir(work / "home")  # relative bind keeps sun_path under the limit
    try:
        sock = socket.socket(socket.AF_UNIX)
        sock.bind("s.sock")
    finally:
        os.chdir(cwd)
    try:
        res = _run_census(work)
    finally:
        sock.close()
    assert res.returncode == 1, res.stdout
    assert "FRESH-INSTALL MVV FAILED" in res.stderr and "s.sock" in res.stderr
