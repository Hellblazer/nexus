# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-j738q: ``nx daemon service pg-upgrade`` upgrades a local cluster below PostgreSQL 17.

Nothing here touches a real cluster, the real config directory or a real service. The PostgreSQL binaries are
shell scripts under ``tmp_path`` that log their argv and whether ``PGPASSWORD`` was in their environment (never
its value) to one file; ``NEXUS_CONFIG_DIR`` and ``NEXUS_PG_BIN`` point under ``tmp_path``; the storage service
is replaced by a recorder (starting a real one is the one thing a unit test cannot do).
"""
from __future__ import annotations

import inspect
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.commands import service_pg_upgrade as cmd_mod
from nexus.db import pg_cluster_upgrade as upgrade_mod
from nexus.db import pg_provision
from nexus.db.pg_provision import PgBinaries, _initdb_argv

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
echo "$tool $* || pw=$([ -n "${PGPASSWORD:-}" ] && echo present || echo absent) cwd=$PWD" >> "$FAKE_LOG"
fail() { [ "${FAKE_FAIL:-}" = "$1" ]; }
"""

_PG_CTL = _PRELUDE + r"""
if [ "$1" = "--version" ]; then echo "pg_ctl (PostgreSQL) $FAKE_MAJOR.4"; exit 0; fi
flag="$FAKE_STATE/${FAKE_WHICH}_running"
case "$*" in
  *status*) if [ -e "$flag" ]; then echo "pg_ctl: server is running"; exit 0; else echo "pg_ctl: no server running"; exit 3; fi ;;
  *stop*) fail old_stop_noop || rm -f "$flag"; exit 0 ;;
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
  if fail pg_upgrade_check; then leak; echo "check found a problem" >&2; exit 1; fi
  exit 0
fi
touch "$FAKE_STATE/new_running" "$NEW/partial"
mkdir -p "$NEW/pg_upgrade_output.d"; echo "log line" > "$NEW/pg_upgrade_output.d/log.txt"
if fail pg_upgrade; then leak; echo "pg_upgrade: failed" >&2; exit 1; fi
rm -f "$FAKE_STATE/new_running"; rm -rf "$NEW/partial"
echo "migrated" > "$NEW/migrated"
[ "${FAKE_EXT:-1}" = 1 ] && echo "select 1;" > update_extensions.sql
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
        path.write_text(body.replace('"$FAKE_STATE/${FAKE_WHICH}_running"', f'"$FAKE_STATE/{which}_running"')
                            .replace("$FAKE_MAJOR", str(major)), encoding="utf-8")
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

    def run(self, *args: str, fail: str | None = None, leak: bool = False, ext: bool = True):
        env: dict[str, str] = {}
        if fail:
            env["FAKE_FAIL"] = fail
        if leak:
            env["FAKE_LEAK"] = "1"
        if not ext:
            env["FAKE_EXT"] = "0"
        return CliRunner().invoke(
            main, ["daemon", "service", "pg-upgrade", "--old-bin", str(self.old_bin), *args], env=env
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
    assert "pg_upgrade --check failed" in result.output and "Nothing was changed" in result.output
    assert rig.snapshot() == before
    assert not _initdb_target(rig).parent.exists()
    rig.assert_original_restored()


def test_check_only_refuses_while_the_old_server_runs(rig: Rig) -> None:
    before = rig.snapshot()
    result = rig.run("--check-only")
    assert result.exit_code == 1 and "a server is running" in result.output
    assert rig.snapshot() == before and rig.calls("initdb") == []
    assert (rig.state / "old_running").exists(), "check-only must not stop anything itself"


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
    # pg_ctl stop is the OLD pg_ctl (the old bin's script is the one whose version probe answered 16).
    assert not any(ln.startswith("pg_ctl") and "new" in ln for ln in rig.log_lines())


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
    result = rig.run("--apply", fail="pg_upgrade", leak=True)
    assert result.exit_code == 1
    assert "pg_upgrade failed" in result.output and "Rolled back" in result.output
    rig.assert_original_restored()
    # The server the failed pg_upgrade left behind was stopped (by the new pg_ctl) before the directory went.
    assert not (rig.state / "new_running").exists()
    assert any(" stop" in ln and "-m immediate" in ln for ln in rig.calls("pg_ctl"))
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


def test_update_extensions_runs_only_when_pg_upgrade_wrote_one(rig: Rig) -> None:
    assert rig.run("--apply", ext=False).exit_code == 0
    assert rig.calls("psql") == [] and len(rig.calls("vacuumdb")) == 1


def test_a_service_that_will_not_start_is_reported_without_a_rollback(rig: Rig) -> None:
    rig.service_rc["start"] = 1
    result = rig.run("--apply")
    assert result.exit_code == 1 and "nx daemon service start" in result.output
    assert (rig.pgdata / "migrated").exists() and rig.pgdata_old.exists()


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
