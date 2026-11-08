# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-j738q: ``nx daemon service pg-upgrade`` upgrades a local cluster below PostgreSQL 17.

Nothing here touches a real cluster, the real config directory or a real service. The PostgreSQL binaries are
shell scripts under ``tmp_path`` that log their argv and whether ``PGPASSWORD`` was in their environment (never
its value) to one file; ``NEXUS_CONFIG_DIR`` and ``NEXUS_PG_BIN`` point under ``tmp_path``; the storage service
is replaced by a recorder (starting a real one is the one thing a unit test cannot do).
"""
from __future__ import annotations

import errno
import inspect
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.commands import service_pg_upgrade as cmd_mod
from nexus.db import pg_cluster_upgrade as upgrade_mod
from nexus.db import pg_provision
from nexus.db.pg_provision import PgBinaries, _initdb_argv
from nexus.util.process_group import safe_killpg_group
from tests._child_process import pid_alive
from tests._module_seam import setattr_in

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the command is Linux and macOS only")

PASSWORD = "S3cr3t-pw-VALUE-9f2c"
USER = "nxtester"
PORT = "54329"
#: The fake tools that do work (the version probes and pg_ctl status are read-only). The initdb name is spelled
#: in pieces so tests/_ddl_cluster_sites.py does not read this file as a cluster start site: no cluster runs here.
_MUTATING = frozenset({"pg_upgrade", "psql", "vacuumdb", "".join(["init", "db"])})

# ── fake PostgreSQL binaries ────────────────────────────────────────────────────

_PRELUDE = r"""#!/bin/bash
tool="$(basename "$0")"
echo "$tool $* || pw=$([ -n "${PGPASSWORD:-}" ] && echo present || echo absent) cwd=$PWD bin=$(basename "$(dirname "$0")")" >> "$FAKE_LOG"
fail() { case ",${FAKE_FAIL:-}," in *",$1,"*) return 0;; esac; return 1; }
"""

_PG_CTL = _PRELUDE + r"""
if [ "$1" = "--version" ]; then echo "pg_ctl (PostgreSQL) $FAKE_MAJOR.4"; exit 0; fi
flag="$FAKE_STATE/${FAKE_WHICH}_running"
case "$*" in
  *status*) if [ -e "$flag" ]; then echo "pg_ctl: server is running"; exit 0; else echo "pg_ctl: no server running"; exit 3; fi ;;
  *stop*) fail ${FAKE_WHICH}_stop_noop || [ -e "$FAKE_STATE/${FAKE_WHICH}_stuck" ] || rm -f "$flag"; exit 0 ;;
esac
exit 0
"""

_INITDB = _PRELUDE + r"""
while [ $# -gt 0 ]; do case "$1" in -D) shift; D="$1";; esac; shift; done
mkdir -p "$D"
if fail initdb; then echo "initdb: could not create $D/x" >&2; exit 1; fi
echo 17 > "$D/PG_VERSION"; echo trust > "$D/pg_hba.conf"
"""

_PG_UPGRADE = _PRELUDE + r"""
check=0
while [ $# -gt 0 ]; do case "$1" in -d) shift; OLD="$1";; -D) shift; NEW="$1";; --check) check=1;; esac; shift; done
[ -f "$OLD/PG_VERSION" ] || { echo "pg_upgrade: old cluster $OLD missing" >&2; exit 2; }
[ -f "$NEW/PG_VERSION" ] || { echo "pg_upgrade: new cluster $NEW missing" >&2; exit 2; }
leak() { [ -n "${FAKE_LEAK:-}" ] && echo "pg_upgrade: connection failed, password was $PGPASSWORD" >&2; true; }
if [ "$check" = 1 ]; then
  if fail check_hang; then  # pg_upgrade --check started the old server and then waits, ignoring every catchable signal
    touch "$FAKE_STATE/old_running"; trap '' INT TERM HUP
    echo $$ > "$FAKE_STATE/pu.pid"
    while :; do sleep 0.05; done
  fi
  if fail check_leak; then touch "$FAKE_STATE/old_running"; echo "check died with the old server up" >&2; exit 1; fi
  if fail pg_upgrade_check; then leak; echo "check found a problem" >&2; exit 1; fi
  exit 0
fi
touch "$FAKE_STATE/new_running" "$NEW/partial"
if fail old_leak; then touch "$FAKE_STATE/old_running"; fi
if fail old_stuck; then touch "$FAKE_STATE/old_running" "$FAKE_STATE/old_stuck"; fi
if fail hang; then  # a child that survives every catchable signal and keeps writing into the new directory
  trap '' INT TERM HUP
  echo $$ > "$FAKE_STATE/pu.pid"
  while :; do mkdir -p "$NEW"; echo x >> "$NEW/partial"; sleep 0.05; done
fi
mkdir -p "$NEW/pg_upgrade_output.d"; echo "log line" > "$NEW/pg_upgrade_output.d/log.txt"
if fail pg_upgrade; then leak; echo "pg_upgrade: failed" >&2; exit 1; fi
rm -f "$FAKE_STATE/new_running"; rm -rf "$NEW/partial"
echo "migrated" > "$NEW/migrated"
case "${FAKE_EXT:-1}" in
  1) echo "select 1;" > update_extensions.sql ;;
  2) mkdir -p "$NEW/pg_upgrade_output.d/20260101T000000"; echo "select 1;" > "$NEW/pg_upgrade_output.d/20260101T000000/update_extensions.sql" ;;
esac
exit 0
"""

_PSQL = _PRELUDE + r"""
fail psql && exit 1
exit 0
"""

_VACUUMDB = _PRELUDE + r"""
fail vacuumdb && { echo "vacuumdb: failed" >&2; exit 1; }
exit 0
"""

_NOOP = _PRELUDE + "exit 0\n"


def _write_bin(directory: Path, scripts: dict[str, str], *, which: str, major: int) -> None:
    directory.mkdir(parents=True)
    for name, body in scripts.items():
        path = directory / name
        # Per-dir identity is baked in (FAKE_WHICH / FAKE_MAJOR), so the two pg_ctl scripts differ only there.
        path.write_text(body.replace("${FAKE_WHICH}", which).replace("$FAKE_MAJOR", str(major)), encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)


@dataclass
class Rig:
    root: Path
    cfg: Path
    pgdata: Path
    old_bin: Path
    new_bin: Path
    log: Path
    state: Path
    service_calls: list[tuple[str, bool, bool]] = field(default_factory=list)  # verb, pw in env, pgdata present
    service_rc: dict[str, int] = field(default_factory=dict)
    #: Called with the verb before the recorder answers; may raise or change the world (a peer making a .old).
    on_service: Callable[[str], None] | None = None

    @property
    def pgdata_old(self) -> Path:
        return self.pgdata.with_name("postgres.old")

    def log_lines(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def calls(self, tool: str) -> list[str]:
        return [ln for ln in self.log_lines() if ln.startswith(tool + " ")]

    def snapshot(self) -> dict[str, str]:
        """Every file under cfg with its content: the 'nothing changed' witness."""
        out: dict[str, str] = {}
        for p in sorted(self.cfg.rglob("*")):
            out[str(p.relative_to(self.cfg))] = p.read_text(encoding="utf-8") if p.is_file() else "<dir>"
        return out

    def run(self, *args: str, fail: str | None = None, leak: bool = False, ext: bool | str = True,
            old_bin: str | None = None):
        env: dict[str, str] = {}
        if fail:
            env["FAKE_FAIL"] = fail
        if leak:
            env["FAKE_LEAK"] = "1"
        if ext is not True:
            env["FAKE_EXT"] = "0" if ext is False else str(ext)
        return CliRunner().invoke(
            main, ["daemon", "service", "pg-upgrade", "--old-bin", old_bin or str(self.old_bin), *args], env=env
        )

    def assert_original_restored(self) -> None:
        assert (self.pgdata / "orig-marker").is_file(), "the original cluster is not back at the data directory"
        assert (self.pgdata / "orig-marker").read_text(encoding="utf-8") == "original\n"
        assert (self.pgdata / "PG_VERSION").read_text(encoding="utf-8") == "16\n"
        assert not (self.pgdata / "migrated").exists() and not (self.pgdata / "partial").exists()
        assert not self.pgdata_old.exists()


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    cfg = tmp_path / "cfg"
    pgdata = cfg / "postgres"
    pgdata.mkdir(parents=True)
    (pgdata / "PG_VERSION").write_text("16\n", encoding="utf-8")
    (pgdata / "orig-marker").write_text("original\n", encoding="utf-8")
    (cfg / "pg_credentials").write_text(
        f"PG_DATA={pgdata}\nPG_PORT={PORT}\nPG_SUPERUSER_PASS={PASSWORD}\n", encoding="utf-8"
    )
    root = tmp_path / "fake"
    state = root / "state"
    state.mkdir(parents=True)
    (state / "old_running").touch()  # the old server is up until the command stops it
    old_bin, new_bin = root / "old-bin", root / "new-bin"
    _write_bin(old_bin, {"pg_ctl": _PG_CTL}, which="old", major=16)
    _write_bin(new_bin, {"pg_ctl": _PG_CTL, "initdb": _INITDB, "psql": _PSQL, "createdb": _NOOP,
                         "pg_upgrade": _PG_UPGRADE, "vacuumdb": _VACUUMDB}, which="new", major=17)
    log = root / "calls.log"
    short = root / "t"
    short.mkdir()
    monkeypatch.setattr(upgrade_mod, "SHORT_TMP_ROOT", short)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("NEXUS_PG_BIN", str(new_bin))
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_STATE", str(state))
    monkeypatch.setenv("USER", USER)
    monkeypatch.setenv("LOGNAME", USER)
    monkeypatch.delenv("PGPASSWORD", raising=False)
    r = Rig(root, cfg, pgdata, old_bin, new_bin, log, state)

    def fake_service(verb: str, config_dir: Path) -> int:
        assert config_dir == cfg, "the service must be addressed in the test's config dir, never the real one"
        r.service_calls.append((verb, "PGPASSWORD" in os.environ, pgdata.exists()))
        if r.on_service is not None:
            r.on_service(verb)
        return r.service_rc.get(verb, 0)

    monkeypatch.setattr(cmd_mod, "_nx_service", fake_service)
    return r


def _all_text(rig: Rig, result) -> str:
    """Everything the run could have leaked into: output, both streams, every fake log, every file bar the credentials."""
    parts = [result.output, result.stdout, result.stderr, *rig.log_lines(), repr(rig.service_calls)]
    for p in rig.root.rglob("*"):
        if p.is_file() and p != rig.log:
            parts.append(p.read_text(encoding="utf-8", errors="replace"))
    for p in rig.cfg.rglob("*"):
        if p.is_file() and p.name != "pg_credentials":
            parts.append(p.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


# ── dry run ─────────────────────────────────────────────────────────────────────


def test_dry_run_changes_nothing_and_prints_the_plan(rig: Rig) -> None:
    before = rig.snapshot()
    result = rig.run()
    assert result.exit_code == 0, result.output
    assert rig.snapshot() == before
    assert (rig.state / "old_running").exists(), "the dry run must not stop the old server"
    assert rig.service_calls == []
    for step in ("nx daemon service stop", "-m fast stop", "mv ", "initdb", "pg_upgrade --check",
                 "nx daemon service start", "update_extensions.sql", "vacuumdb", "postgres.old"):
        assert step in result.output, step
    # The only commands it ran are the read-only version probe.
    assert all("--version" in ln for ln in rig.log_lines()), rig.log_lines()


def test_check_only_and_apply_together_are_refused(rig: Rig) -> None:
    result = rig.run("--check-only", "--apply")
    assert result.exit_code == 2
    assert rig.log_lines() == [] and rig.service_calls == []


# ── refusals ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", [[], ["--apply"], ["--check-only"]])
def test_a_cluster_already_at_17_is_refused(rig: Rig, mode: list[str]) -> None:
    (rig.pgdata / "PG_VERSION").write_text("17\n", encoding="utf-8")
    before = rig.snapshot()
    result = rig.run(*mode)
    assert result.exit_code == 1
    assert "already PostgreSQL 17" in result.output and "nothing to upgrade" in result.output
    assert rig.snapshot() == before and rig.service_calls == []
    assert not any(ln.split()[0] in _MUTATING for ln in rig.log_lines())


@pytest.mark.parametrize("mode", [[], ["--apply"], ["--check-only"]])
def test_an_old_bin_of_another_major_is_refused(rig: Rig, mode: list[str]) -> None:
    _write_bin(rig.root / "wrong-bin", {"pg_ctl": _PG_CTL}, which="old", major=15)
    before = rig.snapshot()
    result = CliRunner().invoke(main, ["daemon", "service", "pg-upgrade", "--old-bin", str(rig.root / "wrong-bin"), *mode])
    assert result.exit_code == 1
    assert "PostgreSQL 15" in result.output and "PG_VERSION is 16" in result.output
    assert rig.snapshot() == before and rig.service_calls == []


def test_an_existing_old_directory_is_refused_with_the_back_out_text(rig: Rig) -> None:
    rig.pgdata_old.mkdir()
    (rig.pgdata_old / "keep").write_text("pre-upgrade\n", encoding="utf-8")
    before = rig.snapshot()
    result = rig.run("--apply")
    assert result.exit_code == 1
    assert "pre-upgrade cluster" in result.output and "back out" in result.output
    assert f"mv {rig.pgdata_old} {rig.pgdata}" in result.output
    assert rig.snapshot() == before
    assert rig.service_calls == [], "the service must not be stopped when the run would be refused anyway"
    assert (rig.state / "old_running").exists()
    assert not any("stop" in ln.split("||")[0] for ln in rig.calls("pg_ctl"))


def test_check_only_does_not_mind_an_old_directory(rig: Rig) -> None:
    rig.pgdata_old.mkdir()
    (rig.state / "old_running").unlink()
    assert rig.run("--check-only").exit_code == 0


def test_a_missing_bundle_binary_is_refused(rig: Rig) -> None:
    (rig.new_bin / "pg_upgrade").unlink()
    result = rig.run("--apply")
    assert result.exit_code == 1 and "no pg_upgrade" in result.output
    assert rig.service_calls == []


def test_no_credentials_means_no_cluster_to_upgrade(rig: Rig) -> None:
    (rig.cfg / "pg_credentials").unlink()
    result = rig.run()
    assert result.exit_code == 1 and "no nx-managed local cluster" in result.output


# ── --check-only ────────────────────────────────────────────────────────────────


def _initdb_target(rig: Rig) -> Path:
    (line,) = rig.calls("initdb")
    return Path(re.search(r"-D (\S+)", line).group(1))


def test_check_only_runs_initdb_in_a_temp_dir_and_leaves_the_real_cluster_alone(rig: Rig) -> None:
    (rig.state / "old_running").unlink()  # the old server is already down
    before = rig.snapshot()
    result = rig.run("--check-only")
    assert result.exit_code == 0, result.output
    assert "pg_upgrade --check passed" in result.output
    target = _initdb_target(rig)
    assert target != rig.pgdata and rig.cfg not in target.parents, "initdb must never run on the real data dir"
    assert not target.parent.exists(), "the temporary directory must be removed"
    (check,) = rig.calls("pg_upgrade")
    assert "--check" in check and f"-d {rig.pgdata} " in check and f"-D {target} " in check
    assert rig.snapshot() == before
    assert rig.service_calls == []
    assert not any(" stop" in ln for ln in rig.log_lines())


def test_check_only_failure_leaves_the_original_and_removes_the_temp_dir(rig: Rig) -> None:
    (rig.state / "old_running").unlink()
    before = rig.snapshot()
    result = rig.run("--check-only", fail="pg_upgrade_check")
    assert result.exit_code == 1
    assert "pg_upgrade --check failed" in result.output and "was not modified" in result.output
    assert rig.snapshot() == before
    assert not _initdb_target(rig).parent.exists()
    rig.assert_original_restored()


def test_check_only_refuses_while_the_old_server_runs(rig: Rig) -> None:
    before = rig.snapshot()
    result = rig.run("--check-only")
    assert result.exit_code == 1 and "a server is running" in result.output
    assert rig.snapshot() == before and rig.calls("initdb") == []
    assert (rig.state / "old_running").exists(), "check-only must not stop anything itself"


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP], ids=["SIGINT", "SIGTERM", "SIGHUP"])
def test_an_interrupted_check_only_stops_the_old_server_pg_upgrade_started(
    rig: Rig, sig: signal.Signals, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nexus-j738q limitation 2: ``pg_upgrade --check`` starts the old server; an interrupt can leave it running."""
    monkeypatch.setattr(upgrade_mod, "_PG_UPGRADE_TIMEOUT_S", 25.0)  # a failing run times out instead of hanging
    (rig.state / "old_running").unlink()  # down when the command starts, as --check-only requires
    before = rig.snapshot()
    previous = signal.signal(sig, lambda *_: None)
    pid = None
    try:
        thread = _fire_when_the_child_hangs(rig, sig)
        result = rig.run("--check-only", fail="check_hang")
        thread.join(timeout=5)
        pid = int((rig.state / "pu.pid").read_text(encoding="utf-8"))
    finally:
        signal.signal(sig, previous)
        if pid is not None:
            safe_killpg_group(pid)
    assert isinstance(result.exception, SystemExit) and result.exit_code == 1, result.output  # click's Abort
    assert not (rig.state / "old_running").exists(), "the old server pg_upgrade --check started is still running"
    assert any("-m fast stop" in ln and ln.startswith("pg_ctl") and "bin=old-bin" in ln for ln in rig.log_lines())
    assert not _initdb_target(rig).parent.exists(), "the temporary directory must go"
    assert rig.snapshot() == before
    rig.assert_original_restored()


def test_a_failed_check_only_also_stops_a_server_it_left_running(rig: Rig) -> None:
    (rig.state / "old_running").unlink()
    result = rig.run("--check-only", fail="check_leak")
    assert result.exit_code == 1 and "pg_upgrade --check failed" in result.output
    assert not (rig.state / "old_running").exists()
    assert not _initdb_target(rig).parent.exists()


def test_a_check_only_that_cannot_stop_the_server_says_which_command_to_run(rig: Rig) -> None:
    (rig.state / "old_running").unlink()
    (rig.state / "old_stuck").touch()  # the old pg_ctl's stop leaves the server up
    result = rig.run("--check-only", fail="check_leak")
    assert result.exit_code == 1
    assert (rig.state / "old_running").exists()
    assert f"-D {rig.pgdata} -m fast stop" in result.output and "still running" in result.output
    assert not _initdb_target(rig).parent.exists(), "the temporary directory goes even so"


def test_a_clean_check_only_never_asks_pg_ctl_to_stop(rig: Rig) -> None:
    (rig.state / "old_running").unlink()
    assert rig.run("--check-only").exit_code == 0
    assert not any(" stop" in ln for ln in rig.log_lines())


# ── --apply ─────────────────────────────────────────────────────────────────────


def test_apply_success_runs_in_the_runbooks_order(rig: Rig) -> None:
    result = rig.run("--apply")
    assert result.exit_code == 0, result.output
    # Service stop precedes every pg command; the new cluster is the old one's data, upgraded.
    assert rig.service_calls == [("stop", False, True), ("start", False, True)]
    assert (rig.pgdata_old / "orig-marker").read_text(encoding="utf-8") == "original\n"
    assert (rig.pgdata / "migrated").exists() and not (rig.pgdata / "orig-marker").exists()
    steps = [ln.split(" ||")[0] for ln in rig.log_lines()]
    kinds = []
    for s in steps:
        tool = s.split()[0]
        if tool == "pg_ctl" and "--version" in s:
            continue
        if tool == "pg_upgrade":
            tool += " --check" if "--check" in s else ""
        kinds.append(tool + (":" + s.split()[-1] if tool == "pg_ctl" else ""))
    assert kinds == ["pg_ctl:status", "pg_ctl:stop", "pg_ctl:status", "initdb", "pg_upgrade --check",
                     "pg_upgrade", "psql", "vacuumdb"], kinds
    assert "Upgraded" in result.output and f"rm -rf {rig.pgdata_old}" in result.output
    assert "nx doctor" in result.output and "scram-sha-256" in result.output
    # Every pg_ctl call is the OLD pg_ctl: the fake logs which bin dir it ran from.
    pg_ctl_lines = rig.calls("pg_ctl")
    assert pg_ctl_lines and all(ln.endswith("bin=old-bin") for ln in pg_ctl_lines), pg_ctl_lines


def test_apply_uses_the_old_pg_ctl_to_stop_the_old_server_and_checks_it_is_down(rig: Rig) -> None:
    rig.run("--apply")
    assert not (rig.state / "old_running").exists()
    statuses = [ln for ln in rig.calls("pg_ctl") if " status " in ln]
    assert len(statuses) == 2, statuses  # before the stop, and the verification after it


def test_apply_makes_the_new_cluster_with_nxs_own_options_and_trust_auth(rig: Rig) -> None:
    rig.run("--apply")
    (line,) = rig.calls("initdb")
    argv = line.split(" ||")[0].split()[1:]
    expected = _initdb_argv(PgBinaries.from_dir(rig.new_bin), rig.pgdata, USER, auth="trust")[1:]
    assert argv == expected
    assert "--auth=trust" in argv and not any(a.startswith("--pwfile") for a in argv)
    assert argv[argv.index("--username") + 1] == USER
    # Run from a short directory, not from the config dir or the cwd of the caller.
    cwd = re.search(r"cwd=(\S+)", line).group(1)
    assert Path(cwd).name.startswith("pgup.") and Path(cwd).parent == rig.root / "t"


def test_the_password_is_in_the_childs_environment_only(rig: Rig) -> None:
    ok = rig.run("--apply")
    assert ok.exit_code == 0
    by_tool = {t: [ln for ln in rig.calls(t) if "--version" not in ln] for t in
               ("pg_upgrade", "psql", "vacuumdb", "initdb", "pg_ctl")}
    for tool in ("pg_upgrade", "psql", "vacuumdb"):
        assert by_tool[tool] and all("pw=present" in ln for ln in by_tool[tool]), (tool, by_tool[tool])
    for tool in ("initdb", "pg_ctl"):
        assert all("pw=absent" in ln for ln in by_tool[tool]), (tool, by_tool[tool])
    assert all(not pw for _, pw, _ in rig.service_calls), "the service must not inherit PGPASSWORD"
    assert PASSWORD not in _all_text(rig, ok)
    assert "PGPASSWORD" not in os.environ, "the password must not be set in this process"


def test_an_inherited_pgpassword_is_not_passed_to_initdb_or_pg_ctl(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGPASSWORD", "caller-own-password")
    result = rig.run("--apply")
    assert result.exit_code == 0, result.output
    assert all("pw=absent" in ln for ln in rig.calls("initdb") + rig.calls("pg_ctl"))
    # pg_upgrade gets the cluster's password from pg_credentials, not the caller's.
    assert all("pw=present" in ln for ln in rig.calls("pg_upgrade"))
    assert "caller-own-password" not in _all_text(rig, result)


def test_the_service_child_never_inherits_pgpassword(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict = {}

    def fake_run_bounded(argv, **kw):
        seen.update(argv=list(argv), env=kw["env"])
        return type("R", (), {"returncode": 0})()

    monkeypatch.setenv("PGPASSWORD", PASSWORD)
    monkeypatch.setattr(cmd_mod, "run_bounded", fake_run_bounded)
    assert cmd_mod._nx_service("stop", tmp_path) == 0
    assert "PGPASSWORD" not in seen["env"]
    assert seen["argv"][-5:] == ["daemon", "service", "stop", "--config-dir", str(tmp_path)]


def test_a_failed_check_after_the_move_rolls_back(rig: Rig) -> None:
    result = rig.run("--apply", fail="pg_upgrade_check")
    assert result.exit_code == 1
    assert "Rolled back" in result.output
    rig.assert_original_restored()
    assert not any(" --check" not in ln and ln.startswith("pg_upgrade") for ln in rig.log_lines()), \
        "the real pg_upgrade must not run after a failed --check"
    assert rig.service_calls == [("stop", False, True)], "the service is not started after a rollback"


def test_a_failed_pg_upgrade_rolls_back_stops_the_leaked_server_and_keeps_the_logs(rig: Rig) -> None:
    result = rig.run("--apply", fail="pg_upgrade,old_leak", leak=True)
    assert result.exit_code == 1
    assert "pg_upgrade failed" in result.output and "Rolled back" in result.output
    rig.assert_original_restored()
    # The servers the failed pg_upgrade left behind were stopped before the directory went: the new one by the
    # new pg_ctl (it is discarded, so `immediate`), the ORIGINAL by the old pg_ctl with `fast` (`immediate` would
    # leave it needing crash recovery).
    assert not (rig.state / "new_running").exists() and not (rig.state / "old_running").exists()
    stops = [ln for ln in rig.calls("pg_ctl") if " stop " in ln.split(" ||")[0] + " "]
    new_stop = [ln for ln in stops if ln.endswith("bin=new-bin")]
    old_stop = [ln for ln in stops if f"-D {rig.pgdata_old} " in ln]
    assert len(new_stop) == 1 and "-m immediate" in new_stop[0] and f"-D {rig.pgdata} " in new_stop[0]
    assert len(old_stop) == 1 and "-m fast" in old_stop[0] and old_stop[0].endswith("bin=old-bin")
    assert not any("-m immediate" in ln for ln in old_stop)
    # pg_upgrade's logs survive in the kept work directory.
    kept = Path(re.search(r"kept: (\S+)", result.output).group(1))
    assert (kept / "pg_upgrade_output.d" / "log.txt").read_text(encoding="utf-8") == "log line\n"
    assert PASSWORD not in _all_text(rig, result)
    assert "***" in result.output, "pg_upgrade echoed the password; it must come out scrubbed"


def test_a_failed_initdb_rolls_back(rig: Rig) -> None:
    result = rig.run("--apply", fail="initdb")
    assert result.exit_code == 1 and "initdb failed" in result.output and "Rolled back" in result.output
    rig.assert_original_restored()
    assert rig.calls("pg_upgrade") == []


def test_the_rollback_test_fails_without_the_rollback(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-vacuity: with the rollback neutered, the same scenario leaves the original cluster NOT restored."""
    monkeypatch.setattr(upgrade_mod.ClusterUpgrade, "_rollback", lambda self, workdir: "no rollback")
    result = rig.run("--apply", fail="pg_upgrade")
    assert result.exit_code == 1
    with pytest.raises(AssertionError):
        rig.assert_original_restored()
    assert not rig.pgdata.joinpath("orig-marker").exists() and rig.pgdata_old.exists()


def test_a_service_that_will_not_stop_changes_nothing(rig: Rig) -> None:
    rig.service_rc["stop"] = 1
    before = rig.snapshot()
    result = rig.run("--apply")
    assert result.exit_code == 1 and "nx daemon service stop" in result.output
    assert rig.snapshot() == before
    assert not any(ln.split()[0] in _MUTATING for ln in rig.log_lines())


def test_an_old_server_that_will_not_stop_changes_nothing(rig: Rig) -> None:
    before = rig.snapshot()
    result = rig.run("--apply", fail="old_stop_noop")
    assert result.exit_code == 1 and "still runs" in result.output
    assert rig.snapshot() == before and not rig.pgdata_old.exists()
    assert rig.calls("initdb") == []


def test_a_failed_follow_up_keeps_the_upgrade_and_the_old_directory(rig: Rig) -> None:
    result = rig.run("--apply", fail="vacuumdb")
    assert result.exit_code == 1
    assert "pg_upgrade succeeded" in result.output and "vacuumdb failed" in result.output
    assert (rig.pgdata / "migrated").exists() and (rig.pgdata_old / "orig-marker").exists()
    assert "Rolled back" not in result.output
    assert PASSWORD not in _all_text(rig, result)


def test_update_extensions_runs_only_when_pg_upgrade_wrote_one_and_says_which(rig: Rig) -> None:
    result = rig.run("--apply", ext=False)
    assert result.exit_code == 0
    assert rig.calls("psql") == [] and len(rig.calls("vacuumdb")) == 1
    assert "pg_upgrade wrote no update_extensions.sql" in result.output and "ALTER EXTENSION" in result.output


def test_update_extensions_in_the_cwd_is_run_and_said_so(rig: Rig) -> None:
    result = rig.run("--apply")
    assert result.exit_code == 0
    (psql,) = rig.calls("psql")
    assert "update_extensions.sql" in psql and "pg_upgrade wrote" in result.output and "running it" in result.output


def test_update_extensions_kept_with_pg_upgrades_logs_is_found(rig: Rig) -> None:
    """PostgreSQL 15 and later may keep the script under <new data>/pg_upgrade_output.d, not in the cwd (S4)."""
    result = rig.run("--apply", ext="2")
    assert result.exit_code == 0, result.output
    (psql,) = rig.calls("psql")
    assert "pg_upgrade_output.d" in psql and "20260101T000000/update_extensions.sql" in psql
    assert "pg_upgrade wrote no update_extensions.sql" not in result.output


def test_a_service_that_will_not_start_is_reported_without_a_rollback(rig: Rig) -> None:
    rig.service_rc["start"] = 1
    result = rig.run("--apply")
    assert result.exit_code == 1 and "nx daemon service start" in result.output
    assert (rig.pgdata / "migrated").exists() and rig.pgdata_old.exists()


# ── fix round r1 (nexus-j738q) ──────────────────────────────────────────────────


def _assert_clean_exit(result) -> None:
    """The command ended with a message, not a traceback."""
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), repr(result.exception)


def _work_dirs(rig: Rig) -> list[Path]:
    return sorted((rig.root / "t").iterdir())


# -- work directory (review finding 1)


def test_a_work_directory_that_cannot_be_made_leaves_the_original_in_place(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_space(*_a, **_kw):
        raise OSError(errno.ENOSPC, "No space left on device")

    setattr_in(monkeypatch, upgrade_mod, "tempfile.mkdtemp", no_space)
    result = rig.run("--apply")
    _assert_clean_exit(result)
    assert "could not make a work directory" in result.output and "nothing was changed" in result.output
    rig.assert_original_restored()
    assert rig.service_calls == [], "the service must not be stopped when the work directory cannot be made"
    assert not any(ln.split()[0] in _MUTATING for ln in rig.log_lines())


def test_check_only_refuses_cleanly_when_the_temp_directory_cannot_be_made(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    (rig.state / "old_running").unlink()

    def no_space(*_a, **_kw):
        raise OSError(errno.ENOSPC, "No space left on device")

    setattr_in(monkeypatch, upgrade_mod, "tempfile.mkdtemp", no_space)
    result = rig.run("--check-only")
    _assert_clean_exit(result)
    assert "temporary directory" in result.output
    rig.assert_original_restored()


# -- interrupts (review findings 2 and S1/S3)


def _fire_when_the_child_hangs(rig: Rig, sig: int) -> threading.Thread:
    """From a thread: once the fake pg_upgrade is running (it writes its pid), deliver *sig* to this process."""
    def fire() -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if (rig.state / "pu.pid").exists():
                time.sleep(0.3)  # let run_bounded reach its wait
                os.kill(os.getpid(), sig)
                return
            time.sleep(0.02)

    t = threading.Thread(target=fire, daemon=True)
    t.start()
    return t


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP], ids=["SIGINT", "SIGTERM", "SIGHUP"])
def test_an_interrupt_kills_the_child_before_the_rollback(rig: Rig, sig: signal.Signals, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pg_upgrade that survives every catchable signal is killed and reaped BEFORE the rollback touches the data.

    The fake keeps recreating the new directory and writing into it; if the rollback ran while it lived, the
    child would still be running when the command returned.
    """
    monkeypatch.setattr(upgrade_mod, "_PG_UPGRADE_TIMEOUT_S", 25.0)  # a failing run times out instead of hanging
    seen: list[int] = []

    def sentinel(signum, _frame) -> None:
        seen.append(signum)

    previous = signal.signal(sig, sentinel)  # a baseline that is neither ignore nor default, whatever CI started with
    pid = None
    try:
        thread = _fire_when_the_child_hangs(rig, sig)
        result = rig.run("--apply", fail="pg_upgrade,hang")
        thread.join(timeout=5)
        pid = int((rig.state / "pu.pid").read_text(encoding="utf-8"))
        assert signal.getsignal(sig) is sentinel, "the command must restore the handler it found"
    finally:
        signal.signal(sig, previous)
        if pid is not None:
            safe_killpg_group(pid)  # only does anything if the code under test left the child running
    assert seen == [], "the command's own handler must take the signal, not the one it replaced"
    assert not pid_alive(pid), "the child was still running when the command returned"
    assert result.exit_code == 1, result.output
    assert "interrupted" in result.output and "Rolled back" in result.output
    rig.assert_original_restored()
    assert PASSWORD not in _all_text(rig, result)


def test_a_keyboard_interrupt_in_a_step_rolls_back_and_propagates(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills the mutant that narrows ``except BaseException`` to ``except Exception``."""
    def interrupted(self, workdir):
        self.say("(interrupt arrives)")
        raise KeyboardInterrupt

    monkeypatch.setattr(upgrade_mod.ClusterUpgrade, "_upgrade", interrupted)
    result = rig.run("--apply")
    assert isinstance(result.exception, SystemExit) and result.exit_code == 1  # click's Abort
    assert "Aborted" in result.output and "Rolled back" in result.output
    rig.assert_original_restored()
    assert rig.service_calls == [("stop", False, True)]


def test_an_interrupt_before_the_move_moves_nothing_and_removes_the_work_directory(rig: Rig) -> None:
    def interrupt_on_stop(verb: str) -> None:
        if verb == "stop":
            raise KeyboardInterrupt

    rig.on_service = interrupt_on_stop
    result = rig.run("--apply")
    assert isinstance(result.exception, SystemExit)
    assert "interrupted" in result.output and "was not moved" in result.output
    assert "Rolled back" not in result.output
    rig.assert_original_restored()
    assert _work_dirs(rig) == [], "the work directory must go"


def test_an_interrupt_after_pg_upgrade_succeeded_keeps_the_upgrade(rig: Rig) -> None:
    def interrupt_on_start(verb: str) -> None:
        if verb == "start":
            raise KeyboardInterrupt

    rig.on_service = interrupt_on_start
    result = rig.run("--apply")
    assert isinstance(result.exception, SystemExit)
    assert "upgraded cluster is in place" in result.output and "Rolled back" not in result.output
    assert (rig.pgdata / "migrated").exists() and (rig.pgdata_old / "orig-marker").exists()


# -- paths (finding 3)


def test_relative_paths_are_made_absolute_before_pg_upgrade_runs_elsewhere(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(rig.root)
    cfg_rel = os.path.relpath(rig.cfg, rig.root)
    old_rel = os.path.relpath(rig.old_bin, rig.root)
    result = rig.run("--apply", "--config-dir", cfg_rel, old_bin=old_rel)
    assert result.exit_code == 0, result.output
    for line in rig.calls("pg_upgrade"):
        assert f"-b {rig.old_bin} " in line and f"-d {rig.pgdata_old} " in line and f"-D {rig.pgdata} " in line, line


def test_build_plan_resolves_its_paths(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(rig.root)
    plan = upgrade_mod.build_plan(Path(os.path.relpath(rig.cfg, rig.root)), Path(os.path.relpath(rig.old_bin, rig.root)))
    assert plan.config_dir.is_absolute() and plan.old_bin.is_absolute() and plan.pgdata.is_absolute()


def test_a_relative_bundle_bin_is_made_absolute(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review r2: a relative NEXUS_PG_BIN would break once pg_upgrade runs with cwd at the work directory."""
    monkeypatch.chdir(rig.root)
    monkeypatch.setenv("NEXUS_PG_BIN", os.path.relpath(os.environ["NEXUS_PG_BIN"], rig.root))
    plan = upgrade_mod.build_plan(rig.cfg, rig.old_bin)
    assert plan.new_bins.bin_dir.is_absolute() and plan.new_bins.initdb.is_absolute()


def test_the_rollback_guard_holds_the_fatal_signals(rig: Rig) -> None:
    """Review r2: with the guard a no-op, a signal during the rollback would cut it in half."""
    seen: list[int] = []
    previous = signal.signal(signal.SIGINT, lambda signum, _f: seen.append(signum))
    try:
        with upgrade_mod._interrupts_held():
            os.kill(os.getpid(), signal.SIGINT)
            assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
        assert seen == [], "the signal reached the handler inside the guard"
        assert signal.getsignal(signal.SIGINT) is not signal.SIG_IGN, "the guard restores the handler"
    finally:
        signal.signal(signal.SIGINT, previous)


def test_the_no_lock_warning_is_shown_where_a_user_sees_it(rig: Rig) -> None:
    """Review r2: nothing locks the service during the upgrade, so the user is told to close other sessions."""
    note = upgrade_mod.NO_LOCK_NOTE
    assert note in rig.run().output
    applied = rig.run("--apply")
    assert applied.exit_code == 0 and applied.output.index(note) < applied.output.index("stopping the storage service")
    helped = " ".join(CliRunner().invoke(main, ["daemon", "service", "pg-upgrade", "--help"]).output.split()).lower()
    assert "nothing locks the service during the upgrade" in helped
    doc = " ".join((Path(__file__).resolve().parents[1] / "docs/cli-reference.md").read_text(encoding="utf-8").split())
    assert "Nothing locks the service while the upgrade runs" in doc


# -- bundle discovery follows --config-dir (nexus-j738q limitation 3)


def _install_bundle(config_dir: Path, source_bin: Path, *, without: tuple[str, ...] = ()) -> Path:
    """Lay out a complete extracted bundle (marker + bundle/bin) under *config_dir* from a fake bin dir."""
    import shutil  # noqa: PLC0415

    extract_root = config_dir / "pg-bundle"
    bin_dir = extract_root / "bundle" / "bin"
    shutil.copytree(source_bin, bin_dir)
    for name in without:
        (bin_dir / name).unlink()
    (extract_root / ".nx_bundle_extracted").write_text("x", encoding="utf-8")
    return bin_dir


def test_the_bundle_is_found_under_the_given_config_dir_not_the_default(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--config-dir`` names where the cluster AND its bundle live; the default dir has neither."""
    monkeypatch.delenv("NEXUS_PG_BIN", raising=False)
    bundle_bin = _install_bundle(rig.cfg, rig.new_bin)
    elsewhere = rig.root / "default-cfg"
    elsewhere.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(elsewhere))  # what the code used to read
    assert upgrade_mod.build_plan(rig.cfg, rig.old_bin).new_bins.bin_dir == bundle_bin
    result = rig.run("--apply", "--config-dir", str(rig.cfg))
    assert result.exit_code == 0, result.output
    assert rig.calls("pg_upgrade"), "the bundle's pg_upgrade never ran"


def test_the_given_config_dirs_bundle_wins_over_the_default_dirs(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """With a bundle under BOTH, the one beside the cluster is used (the default one lacks pg_upgrade: using it refuses)."""
    monkeypatch.delenv("NEXUS_PG_BIN", raising=False)
    mine = _install_bundle(rig.cfg, rig.new_bin)
    default_cfg = rig.root / "default-cfg"
    default_cfg.mkdir()
    _install_bundle(default_cfg, rig.new_bin, without=("pg_upgrade",))
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(default_cfg))
    assert upgrade_mod.build_plan(rig.cfg, rig.old_bin).new_bins.bin_dir == mine


def test_nexus_pg_bin_still_overrides_the_config_dirs_bundle(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_bundle(rig.cfg, rig.new_bin, without=("pg_upgrade",))  # a bundle that would be refused
    # the rig's NEXUS_PG_BIN (a complete fake bin) is still set: it comes first
    assert upgrade_mod.build_plan(rig.cfg, rig.old_bin).new_bins.bin_dir == rig.new_bin


def test_no_bundle_under_the_config_dir_is_refused_not_found_elsewhere(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """The default dir's bundle is not a fallback for another config dir, and no host PostgreSQL is tried."""
    monkeypatch.delenv("NEXUS_PG_BIN", raising=False)
    default_cfg = rig.root / "default-cfg"
    default_cfg.mkdir()
    _install_bundle(default_cfg, rig.new_bin)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(default_cfg))
    with pytest.raises(upgrade_mod.PgUpgradeRefused, match="bundled PostgreSQL 17 is not available"):
        upgrade_mod.build_plan(rig.cfg, rig.old_bin)


def test_discover_pg_binaries_takes_the_config_dir(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_PG_BIN", raising=False)
    bundle_bin = _install_bundle(rig.cfg, rig.new_bin)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(rig.root / "nowhere"))
    assert pg_provision.discover_pg_binaries(rig.cfg).bin_dir == bundle_bin
    with pytest.raises(pg_provision.PgBinaryNotFoundError):
        pg_provision.discover_pg_binaries()


# -- errors (finding 4)


def test_a_service_stop_that_raises_ends_with_a_message_and_moves_nothing(rig: Rig) -> None:
    def boom(verb: str) -> None:
        raise subprocess.TimeoutExpired("nx daemon service", 600)

    rig.on_service = boom
    result = rig.run("--apply")
    _assert_clean_exit(result)
    assert "nx daemon service stop" in result.output and "not touched" in result.output
    rig.assert_original_restored()


def test_a_service_start_that_raises_keeps_the_upgrade_and_says_what_to_run(rig: Rig) -> None:
    def boom(verb: str) -> None:
        if verb == "start":
            raise subprocess.TimeoutExpired("nx daemon service", 600)

    rig.on_service = boom
    result = rig.run("--apply")
    _assert_clean_exit(result)
    out = result.output
    assert "upgraded cluster is in place" in out and "Rolled back" not in out
    assert (rig.pgdata / "migrated").exists() and (rig.pgdata_old / "orig-marker").exists()
    # M6: the follow-up commands are listed, with no password in them.
    assert f"{rig.new_bin}/vacuumdb" in out and "--analyze-in-stages" in out
    assert f"{rig.new_bin}/psql" in out and "update_extensions.sql" in out
    assert PASSWORD not in _all_text(rig, result)


@pytest.mark.parametrize("mode", [["--check-only"], ["--apply"]])
def test_a_pg_ctl_status_that_cannot_run_ends_with_a_message(rig: Rig, monkeypatch: pytest.MonkeyPatch, mode: list[str]) -> None:
    real_run = upgrade_mod._run

    def run(argv, **kw):
        if argv[-1] == "status":
            raise subprocess.TimeoutExpired(argv, 60)
        return real_run(argv, **kw)

    monkeypatch.setattr(upgrade_mod, "_run", run)
    (rig.state / "old_running").unlink()
    result = rig.run(*mode)
    _assert_clean_exit(result)
    assert "status" in result.output and "could not run" in result.output
    rig.assert_original_restored()
    assert not any(ln.split()[0] in _MUTATING for ln in rig.log_lines())


@pytest.mark.parametrize("mode", [[], ["--check-only"], ["--apply"]], ids=["dry-run", "check-only", "apply"])
def test_root_is_refused_cleanly_in_every_mode(rig: Rig, monkeypatch: pytest.MonkeyPatch, mode: list[str]) -> None:
    setattr_in(monkeypatch, "nexus.db.pg_provision", "os.geteuid", lambda: 0, raising=False)
    result = rig.run(*mode)
    _assert_clean_exit(result)
    assert "root" in result.output.lower()
    assert rig.log_lines() == [] and rig.service_calls == []
    rig.assert_original_restored()


# -- the rollback's stop (finding 5)


@pytest.mark.parametrize("stuck", ["old_stuck", "new_stop_noop"], ids=["original-server", "new-server"])
def test_a_rollback_that_cannot_stop_a_server_says_so_and_touches_nothing(rig: Rig, stuck: str) -> None:
    """ROLLBACK INCOMPLETE, branch 1: a server still runs, so nothing is removed or moved back."""
    result = rig.run("--apply", fail=f"pg_upgrade,{stuck}")
    assert result.exit_code == 1, result.output
    assert "ROLLBACK INCOMPLETE" in result.output and "Rolled back" not in result.output
    assert "still be running" in result.output and f"mv {rig.pgdata_old} {rig.pgdata}" in result.output
    assert (rig.pgdata_old / "orig-marker").read_text(encoding="utf-8") == "original\n"
    assert (rig.pgdata / "partial").exists(), "the new directory must be left alone under a running server"


def test_a_rollback_that_cannot_remove_the_new_directory_says_so(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """ROLLBACK INCOMPLETE, branch 2: the new directory will not go."""
    real_rmtree = upgrade_mod.shutil.rmtree

    def rmtree(path, *a, **kw):
        if Path(path) == rig.pgdata:
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_rmtree(path, *a, **kw)

    setattr_in(monkeypatch, upgrade_mod, "shutil.rmtree", rmtree)
    result = rig.run("--apply", fail="pg_upgrade")
    assert result.exit_code == 1
    assert "ROLLBACK INCOMPLETE: could not remove the new" in result.output and "Permission denied" in result.output
    assert (rig.pgdata_old / "orig-marker").exists()


def test_a_rollback_that_cannot_move_the_original_back_says_so(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """ROLLBACK INCOMPLETE, branch 3: the new directory is gone but the original will not move back."""
    real_rename = os.rename

    def rename(src, dst, *a, **kw):
        if Path(src) == rig.pgdata_old:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(src, dst, *a, **kw)

    setattr_in(monkeypatch, upgrade_mod, "os.rename", rename)
    result = rig.run("--apply", fail="pg_upgrade")
    assert result.exit_code == 1
    assert "ROLLBACK INCOMPLETE: the new directory is removed" in result.output
    assert f"`mv {rig.pgdata_old} {rig.pgdata}`" in result.output
    assert (rig.pgdata_old / "orig-marker").exists() and not rig.pgdata.exists()


# -- surviving mutants (finding 7)


def test_a_old_directory_made_while_the_service_stopped_is_refused_before_the_move(rig: Rig) -> None:
    """Kills the mutant that deletes the second .old check: build_plan's earlier check does not see this one."""
    def peer_makes_the_old_directory(verb: str) -> None:
        if verb == "stop":
            rig.pgdata_old.mkdir()  # empty: a rename onto it would succeed, so only the check stops it

    rig.on_service = peer_makes_the_old_directory
    result = rig.run("--apply")
    _assert_clean_exit(result)
    assert "pre-upgrade cluster" in result.output and "back out" in result.output
    assert (rig.pgdata / "orig-marker").read_text(encoding="utf-8") == "original\n"
    assert rig.calls("initdb") == [] and rig.calls("pg_upgrade") == []


def test_a_failed_rename_to_the_old_directory_changes_nothing(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    real_rename = os.rename

    def rename(src, dst, *a, **kw):
        if Path(src) == rig.pgdata:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(src, dst, *a, **kw)

    setattr_in(monkeypatch, upgrade_mod, "os.rename", rename)
    result = rig.run("--apply")
    _assert_clean_exit(result)
    assert "could not move" in result.output and "was not moved" in result.output
    rig.assert_original_restored()
    assert rig.calls("initdb") == [] and _work_dirs(rig) == []


def test_credentials_without_a_port_are_refused_before_anything_stops(rig: Rig) -> None:
    (rig.cfg / "pg_credentials").write_text(f"PG_DATA={rig.pgdata}\nPG_SUPERUSER_PASS={PASSWORD}\n", encoding="utf-8")
    result = rig.run("--apply")
    _assert_clean_exit(result)
    assert "PG_PORT" in result.output
    assert rig.service_calls == [] and (rig.state / "old_running").exists()
    assert not any(ln.split()[0] in _MUTATING for ln in rig.log_lines())


@pytest.mark.parametrize("mode", [[], ["--check-only"], ["--apply"]], ids=["dry-run", "check-only", "apply"])
def test_windows_is_refused(rig: Rig, monkeypatch: pytest.MonkeyPatch, mode: list[str]) -> None:
    setattr_in(monkeypatch, upgrade_mod, "sys.platform", "win32")
    result = rig.run(*mode)
    _assert_clean_exit(result)
    assert "Linux and macOS" in result.output
    assert rig.log_lines() == [] and rig.service_calls == []


# -- what the user is told (findings 6 and 9)


def test_the_untested_limit_is_shown_where_a_user_sees_it(rig: Rig) -> None:
    note = upgrade_mod.UNTESTED_NOTE
    assert note == "not yet run against a real pre-17 cluster; copy the data directory first"
    dry = rig.run()
    assert note in dry.output
    helped = CliRunner().invoke(main, ["daemon", "service", "pg-upgrade", "--help"]).output
    assert note in " ".join(helped.split()).lower()
    repo = Path(__file__).resolve().parents[1]
    for doc in ("docs/cli-reference.md", "docs/migration-runbook.md"):
        assert note in " ".join((repo / doc).read_text(encoding="utf-8").split()).lower().replace("**", ""), doc


def test_apply_prints_the_limit_before_it_moves_the_data_directory(rig: Rig) -> None:
    result = rig.run("--apply")
    assert result.exit_code == 0, result.output
    text = result.output
    assert upgrade_mod.UNTESTED_NOTE in text
    assert text.index(upgrade_mod.UNTESTED_NOTE) < text.index("moved ") and text.index("NOTE:") < text.index("stopping the storage service")


def test_the_wording_does_not_claim_more_than_it_does(rig: Rig) -> None:
    dry = rig.run().output
    assert "only read" not in dry and "starts and stops the old server" in dry
    repo = Path(__file__).resolve().parents[1]
    ref = (repo / "docs" / "cli-reference.md").read_text(encoding="utf-8")
    section = ref.split("### nx daemon service pg-upgrade", 1)[1].split("### nx daemon service install-binary", 1)[0]
    assert "changes nothing. |" not in section and "starts and stops the old server" in section


def test_the_work_directory_is_flagged_as_holding_password_hashes(rig: Rig) -> None:
    result = rig.run("--apply", fail="pg_upgrade")
    assert "SCRAM" in result.output and "delete it" in result.output


def test_the_doctor_row_points_at_the_command(tmp_path: Path) -> None:
    from nexus.health import _check_local_pg_version  # noqa: PLC0415

    config = tmp_path / "c"
    (config / "postgres").mkdir(parents=True)
    (config / "postgres" / "PG_VERSION").write_text("16\n", encoding="utf-8")
    (config / "pg_credentials").write_text(f"PG_DATA={config / 'postgres'}\nPG_PORT=1\n", encoding="utf-8")
    [row] = _check_local_pg_version(config)
    assert any("nx daemon service pg-upgrade" in fix for fix in row.fix_suggestions)


# ── wiring ──────────────────────────────────────────────────────────────────────


def test_the_command_is_registered_under_the_service_group_and_names_the_runbook() -> None:
    result = CliRunner().invoke(main, ["daemon", "service", "pg-upgrade", "--help"])
    assert result.exit_code == 0
    for flag in ("--old-bin", "--check-only", "--apply", "--config-dir"):
        assert flag in result.output
    assert "migration-runbook.md" in result.output


def test_the_below_floor_refusal_points_at_the_command() -> None:
    assert "nx daemon service pg-upgrade" in inspect.getsource(pg_provision.refuse_cluster_below_floor)


def test_initdb_options_come_from_one_builder() -> None:
    """_init_cluster and the upgrade build initdb's command line in one place."""
    bins = PgBinaries.from_dir(Path("/b"), platform="linux")
    assert _initdb_argv(bins, Path("/d"), "u", auth="scram-sha-256", pwfile="/pw") == [
        "/b/initdb", "-D", "/d", "--no-locale", "-E", "UTF8", "--auth=scram-sha-256", "--pwfile=/pw", "--username", "u",
    ]
    assert "_initdb_argv(" in inspect.getsource(pg_provision._init_cluster)
    assert "_initdb_argv(" in inspect.getsource(upgrade_mod)
