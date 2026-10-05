# SPDX-License-Identifier: AGPL-3.0-or-later
"""Windows PostgreSQL start path (RDR-224 P3.2c, bead nexus-f9bgu.18).

Every Windows branch here takes ``platform="win32"`` and a ``popen`` seam, so
the branch runs on macOS and Linux as well as on Windows. A test that only
asserts the absence of an error would pass with the Windows branch deleted, so
each Windows test names something the POSIX branch cannot produce (a ``.exe``
name, a creation flag, a file where POSIX has a pipe).

The real-Windows check is the qwentescence run recorded in the bead; these tests
pin the decisions that run exercised.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from nexus import _winsec
from nexus.db import pg_bundle, pg_provision as pp
from nexus.db.pg_provision import (
    PgBinaries,
    PgRootUserError,
    PgStartError,
    _bundle_lib_env,
    _start_cluster,
    bootstrap_superuser,
    windows_superuser_name,
)

CREATE_NEW_PROCESS_GROUP = 0x00000200
SID_A = "S-1-5-21-3623811015-3361044348-30300820-1013"
SID_B = "S-1-5-21-3623811015-3361044348-30300820-1014"


# ── step 1: executable names ─────────────────────────────────────────────────────


def _touch_all(d: Path, suffix: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    for name in ("initdb", "pg_ctl", "psql", "createdb"):
        (d / f"{name}{suffix}").write_text("")


def test_from_dir_windows_names_carry_exe(tmp_path: Path) -> None:
    bins = PgBinaries.from_dir(tmp_path, platform="win32")
    assert [p.name for p in (bins.initdb, bins.pg_ctl, bins.psql, bins.createdb)] == [
        "initdb.exe", "pg_ctl.exe", "psql.exe", "createdb.exe",
    ]


def test_from_dir_posix_names_are_bare(tmp_path: Path) -> None:
    bins = PgBinaries.from_dir(tmp_path, platform="linux")
    assert [p.name for p in (bins.initdb, bins.pg_ctl, bins.psql, bins.createdb)] == [
        "initdb", "pg_ctl", "psql", "createdb",
    ]


def test_an_extracted_windows_bundle_reads_complete_only_under_the_windows_names(tmp_path: Path) -> None:
    _touch_all(tmp_path, ".exe")
    # Non-vacuity: the same directory is incomplete when read as POSIX, so
    # all_present() is really sensitive to the names this change derives.
    assert PgBinaries.from_dir(tmp_path, platform="win32").all_present()
    posix = PgBinaries.from_dir(tmp_path, platform="linux")
    assert not posix.all_present()
    assert posix.missing_names() == ["initdb", "pg_ctl", "psql", "createdb"]


def test_missing_names_stay_logical_on_windows(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "initdb.exe").write_text("")
    assert PgBinaries.from_dir(tmp_path, platform="win32").missing_names() == [
        "pg_ctl", "psql", "createdb",
    ]


# ── step 2: no loader variable on Windows ────────────────────────────────────────


def test_bundle_lib_env_sets_nothing_on_windows_but_does_on_posix(tmp_path: Path) -> None:
    (tmp_path / "bundle" / "bin").mkdir(parents=True)
    (tmp_path / "bundle" / "lib").mkdir()
    exe = tmp_path / "bundle" / "bin" / "initdb.exe"
    exe.write_text("")
    win = _bundle_lib_env([str(exe)], {"FOO": "bar"}, platform="win32")
    posix = _bundle_lib_env([str(exe)], {"FOO": "bar"}, platform="linux")
    assert "LD_LIBRARY_PATH" not in win and win == {"FOO": "bar"}
    assert posix["LD_LIBRARY_PATH"] == str(tmp_path / "bundle" / "lib")  # the sibling lib/ exists, so the POSIX branch does inject


# ── step 5: the cluster superuser ────────────────────────────────────────────────

_LEGAL_UNQUOTED_ROLE = re.compile(r"[a-z_][a-z0-9_]*")


@pytest.mark.parametrize("sid", [SID_A, SID_B, "S-1-5-21-" + "-".join(["4294967295"] * 15)])
def test_windows_superuser_is_a_legal_unquoted_role_name(sid: str) -> None:
    name = windows_superuser_name(sid)
    assert _LEGAL_UNQUOTED_ROLE.fullmatch(name)
    assert len(name.encode()) <= 63
    assert not name.startswith("pg_")  # reserved prefix


def test_windows_superuser_is_stable_and_per_identity() -> None:
    assert windows_superuser_name(SID_A) == windows_superuser_name(SID_A)
    assert windows_superuser_name(SID_A) != windows_superuser_name(SID_B)


def test_bootstrap_superuser_on_windows_ignores_user_and_logname(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USER", "Sam Smith")
    monkeypatch.setenv("LOGNAME", "also bad")
    name = bootstrap_superuser(platform="win32", identity=lambda: SID_A)
    assert name == windows_superuser_name(SID_A)
    assert "sam" not in name.lower()


def test_bootstrap_superuser_on_posix_is_the_login_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USER", "hal")
    assert bootstrap_superuser(platform="linux", identity=lambda: pytest.fail("not read on POSIX")) == "hal"
    monkeypatch.delenv("USER")
    monkeypatch.setenv("LOGNAME", "ops")
    assert bootstrap_superuser(platform="linux") == "ops"


def test_bootstrap_superuser_on_windows_reads_the_service_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no identity seam it asks service_identity, the one source of the SID."""
    from nexus.daemon import service_registry as sr

    seen: list[Any] = []

    def fake_identity(**kw: Any) -> str:
        seen.append(kw)
        return SID_B

    monkeypatch.setattr(sr, "service_identity", fake_identity)
    assert bootstrap_superuser(platform="win32") == windows_superuser_name(SID_B)
    assert seen == [{"platform": "win32"}]


# ── steps 3, 4, 6: the start ─────────────────────────────────────────────────────


class _Proc:
    """A fake pg_ctl process: records its kwargs, writes to the stdout it was given."""

    def __init__(self, returncode: int = 0, wait_raises: Exception | None = None) -> None:
        self.returncode = returncode
        self.wait_raises = wait_raises
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        if self.wait_raises is not None and not self.killed:
            raise self.wait_raises
        return self.returncode

    def kill(self) -> None:
        self.killed = True


class _Spawner:
    def __init__(self, proc: _Proc, *, ctl_output: str = "", pg_log: str = "", pgdata: Path | None = None) -> None:
        self.proc = proc
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self._ctl_output = ctl_output
        self._pg_log = pg_log
        self._pgdata = pgdata

    def __call__(self, cmd: list[str], **kw: Any) -> _Proc:
        self.calls.append((cmd, kw))
        kw["stdout"].write(self._ctl_output.encode())
        kw["stdout"].flush()
        if self._pg_log and self._pgdata is not None:
            (self._pgdata / "pg.log").write_text(self._pg_log)
        return self.proc


@pytest.fixture
def start_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A Windows-shaped bundle dir and pgdata, `pg_ctl status` reporting stopped,
    run_bounded and contain recorded, root refusal passing."""
    bin_dir = tmp_path / "bundle" / "bin"
    _touch_all(bin_dir, ".exe")
    pgdata = tmp_path / "data"
    pgdata.mkdir()
    bins = PgBinaries.from_dir(bin_dir, platform="win32")
    bounded: list[list[str]] = []

    def fake_run_bounded(argv, **kw):
        bounded.append(list(argv))
        return subprocess.CompletedProcess(argv, 3, "", "")  # status: not running

    contained: list[Any] = []
    monkeypatch.setattr(pp, "run_bounded", fake_run_bounded)
    monkeypatch.setattr("nexus.util.process_group.contain", lambda p: contained.append(p) or None)
    monkeypatch.setattr(pp, "refuse_root", lambda: None)
    monkeypatch.setattr(pp, "_port_accepting", lambda host, port: True)
    return bins, pgdata, bounded, contained


def test_windows_start_spawns_pg_ctl_with_a_new_process_group_and_a_file_not_a_pipe(start_env) -> None:
    bins, pgdata, bounded, contained = start_env
    spawn = _Spawner(_Proc(0), ctl_output="waiting for server to start... done\n")
    _start_cluster(bins, pgdata, 5433, platform="win32", popen=spawn)

    (cmd, kw), = spawn.calls  # exactly one detached spawn: the start
    assert kw["creationflags"] & CREATE_NEW_PROCESS_GROUP
    assert kw["stdout"] is not subprocess.PIPE and kw["stderr"] is subprocess.STDOUT
    assert kw["stdin"] is subprocess.DEVNULL
    assert hasattr(kw["stdout"], "write")  # a real file object, the one pg_ctl.out is written through
    assert (pgdata / "pg_ctl.out").read_text() == "waiting for server to start... done\n"
    assert cmd[0] == str(bins.pg_ctl) and os.path.isabs(cmd[0]) and cmd[0].endswith("pg_ctl.exe")
    assert cmd[1:] == ["-D", str(pgdata), "-l", str(pgdata / "pg.log"), "-o", "-p 5433", "start", "-w"]


def test_windows_start_stays_out_of_the_per_call_job_object(start_env) -> None:
    bins, pgdata, bounded, contained = start_env
    _start_cluster(bins, pgdata, 5433, platform="win32", popen=_Spawner(_Proc(0)))
    # Only `pg_ctl status` went through run_bounded (which owns the per-call
    # job); the start did not, and no job was ever assigned to its process.
    assert len(bounded) == 1 and bounded[0][-1] == "status"
    assert contained == []


def test_posix_start_is_unchanged_and_never_spawns_detached(start_env, tmp_path: Path) -> None:
    bins, pgdata, bounded, contained = start_env
    posix = PgBinaries.from_dir(tmp_path / "pbin", platform="linux")
    spawn = _Spawner(_Proc(0))
    _start_cluster(posix, pgdata, 5433, platform="linux", popen=spawn)
    assert spawn.calls == []
    assert bounded == [
        [str(posix.pg_ctl), "-D", str(pgdata), "status"],
        [str(posix.pg_ctl), "-D", str(pgdata), "-l", str(pgdata / "pg.log"), "-o", "-p 5433", "start", "-w"],
    ]


def test_windows_start_keeps_the_root_refusal(start_env, monkeypatch: pytest.MonkeyPatch) -> None:
    bins, pgdata, *_ = start_env
    monkeypatch.undo()  # drop the fixture's refuse_root stub; use the real one
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    spawn = _Spawner(_Proc(0))
    with pytest.raises(PgRootUserError):
        pp._pg_ctl_start_detached(bins, pgdata, 5433, platform="win32", popen=spawn)
    assert spawn.calls == []  # refused before anything was spawned


def test_windows_start_failure_names_the_log_files_and_shows_their_tails(start_env) -> None:
    bins, pgdata, *_ = start_env
    spawn = _Spawner(
        _Proc(1),
        ctl_output="pg_ctl: could not start server\nExamine the log output.\n",
        pg_log="2026-10-05 LOG:  starting\nFATAL:  could not open shared memory segment\n",
        pgdata=pgdata,
    )
    with pytest.raises(PgStartError) as ei:
        _start_cluster(bins, pgdata, 5433, platform="win32", popen=spawn)
    text = str(ei.value)
    assert str(pgdata / "pg.log") in text and str(pgdata / "pg_ctl.out") in text
    assert "FATAL:  could not open shared memory segment" in text
    assert "pg_ctl: could not start server" in text
    assert "exited 1" in text


def test_start_failure_tail_is_credential_scrubbed(start_env) -> None:
    bins, pgdata, *_ = start_env
    spawn = _Spawner(
        _Proc(1),
        pg_log="STATEMENT:  CREATE ROLE nexus_svc LOGIN PASSWORD 'hunter2-live-secret'\n",
        pgdata=pgdata,
    )
    with pytest.raises(PgStartError) as ei:
        _start_cluster(bins, pgdata, 5433, platform="win32", popen=spawn)
    assert "hunter2-live-secret" not in str(ei.value)


def test_port_never_accepting_raises_with_the_log_tail(start_env, monkeypatch: pytest.MonkeyPatch) -> None:
    bins, pgdata, *_ = start_env
    monkeypatch.setattr(pp, "_port_accepting", lambda host, port: False)
    monkeypatch.setattr(pp, "_PG_ACCEPT_TIMEOUT_S", 0.3)
    spawn = _Spawner(_Proc(0), pg_log="LOG:  database system is starting up\n", pgdata=pgdata)
    with pytest.raises(PgStartError, match="did not accept connections") as ei:
        _start_cluster(bins, pgdata, 5433, platform="win32", popen=spawn)
    assert "database system is starting up" in str(ei.value)
    assert str(pgdata / "pg.log") in str(ei.value)


def test_a_pg_ctl_that_never_returns_is_killed_and_times_out(start_env, monkeypatch: pytest.MonkeyPatch) -> None:
    bins, pgdata, *_ = start_env
    proc = _Proc(0, wait_raises=subprocess.TimeoutExpired("pg_ctl", 1))
    with pytest.raises(subprocess.TimeoutExpired):
        _start_cluster(bins, pgdata, 5433, platform="win32", popen=_Spawner(proc))
    assert proc.killed


def test_log_tail_of_a_missing_file_is_a_marker_not_an_exception(tmp_path: Path) -> None:
    assert "cannot read" in pp._log_tail(tmp_path / "absent.log")


# ── the pipe hang, with a real process ───────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="runs a shebang script as pg_ctl; the Windows host evidence is the qwentescence run")
def test_start_returns_while_a_descendant_still_holds_pg_ctls_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The postmaster keeps pg_ctl's standard handles. With a pipe the caller
    would wait for it; with a file the start returns as soon as pg_ctl exits."""
    pidfile = tmp_path / "grandchild.pid"
    script = tmp_path / "bin" / "pg_ctl"
    script.parent.mkdir()
    script.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(g.pid))\n"
        "print('waiting for server to start... done')\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    pgdata = tmp_path / "data"
    pgdata.mkdir()
    bins = PgBinaries.from_dir(script.parent, platform="linux")
    monkeypatch.setattr(pp, "refuse_root", lambda: None)
    monkeypatch.setattr(pp, "_port_accepting", lambda host, port: True)
    monkeypatch.setattr(pp, "_run", lambda *a, **k: subprocess.CompletedProcess(a, 3))  # status: stopped

    real_popen = subprocess.Popen

    def popen_without_windows_flags(cmd, **kw):
        # creationflags is Windows-only; the real flag is pinned by the fake-popen tests above.
        assert kw.pop("creationflags") & CREATE_NEW_PROCESS_GROUP
        return real_popen(cmd, **kw)

    started = time.monotonic()
    try:
        _start_cluster(bins, pgdata, 5433, platform="win32", popen=popen_without_windows_flags)
        elapsed = time.monotonic() - started
        grandchild = int(pidfile.read_text())
        os.kill(grandchild, 0)  # still alive: the start did not wait for it, and did not kill it
        assert elapsed < 20
        assert "done" in (pgdata / "pg_ctl.out").read_text()
    finally:
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), 9)
            except OSError:
                pass


# ── the ACL guard (initdb 0xC0000135 under an elevated session) ──────────────────


def test_grant_user_tree_access_applies_the_users_sid_on_windows_only(tmp_path: Path) -> None:
    applied: list[tuple[str, str]] = []
    _winsec.grant_user_tree_access(tmp_path, platform="win32", sid_lookup=lambda: SID_A, acl_apply=lambda p, s: applied.append((p, s)))
    assert applied == [(str(tmp_path), SID_A)]
    _winsec.grant_user_tree_access(
        tmp_path, platform="linux",
        sid_lookup=lambda: pytest.fail("no SID lookup on POSIX"), acl_apply=lambda p, s: pytest.fail("no ACL on POSIX"),
    )


def test_user_tree_sddl_is_inheritable_and_unprotected() -> None:
    sddl = _winsec._USER_TREE_SDDL.format(sid=SID_A)
    assert sddl == f"D:(A;OICI;FA;;;{SID_A})"  # object+container inherit, full control, no P flag


def test_init_cluster_grants_the_data_dir_before_initdb_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(pp, "grant_user_tree_access", lambda p, **kw: order.append("grant"))
    monkeypatch.setattr(pp, "_run", lambda cmd, **kw: order.append("initdb"))
    pgdata = tmp_path / "pg"
    bins = PgBinaries.from_dir(tmp_path / "bin", platform="win32")
    assert pp._init_cluster(bins, pgdata, "nx_x", platform="win32") is True
    assert order == ["grant", "initdb"]


def test_bundle_extraction_grants_the_tree_before_writing_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_pg_bundle_txz) -> None:
    seen: list[tuple[Path, bool]] = []

    def record(path: Path, **kw: Any) -> None:
        seen.append((Path(path), not any(Path(path).iterdir())))  # (dest, empty at grant time)

    monkeypatch.setattr(pg_bundle, "grant_user_tree_access", record)
    archive = make_pg_bundle_txz(tmp_path)
    pg_bundle.extract_bundle(archive, tmp_path / "cache")
    assert seen == [(tmp_path / "cache", True)]
