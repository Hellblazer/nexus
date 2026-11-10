# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline test of ``rehearse()``'s wiring (nexus-f3yqd.5, review finding F3).

The pure pieces (judge, verdict, watchdog) have their own tests; this file runs the one function that
joins them, against a fake world: a fake ``uv`` (found through ``_ambient_uv_dir``) that installs a
fake ``nx``, a ``--prepare-cmd`` stand-in that writes ``pg_credentials`` and a bundle ``bin/psql``, and
a fake ``psql`` that answers the driver's queries from files. Nothing here talks to a network, an
engine or a real PG. Each test pins one joint a reviewer's mutation could cut:

* the PCI step is a required step of a pre-PCI start (a not-run PCI step is a finding),
* ``nx upgrade``'s text reaches the DEFERRED check,
* a restart still running when ``nx upgrade`` returns is joined before ``doctor`` starts,
* every setup failure stops the run with its own reason, not the default one,
* the cleanup runs, and frees the store, whatever the run did (and keeps it under --keep-work, or
  while a postmaster still runs over it),
* a SIGTERM landing inside the cleanup is held until the cleanup is done,
* the write-rate "after" reading is taken before the restart thread is joined.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import stat
import sys
from pathlib import Path

import pytest

DRIVER = Path(__file__).resolve().parent.parent / "scripts" / "upgrade_rehearsal.py"


def _load():
    spec = importlib.util.spec_from_file_location("_upgrade_rehearsal_rh", DRIVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_upgrade_rehearsal_rh"] = module
    spec.loader.exec_module(module)
    return module


ur = _load()

INDEX = "pci_" + "a" * 24
SENTINEL = "rehearse-sentinel-hit"
CANDIDATE = "8.1.0"

# The fake uv: `tool install` puts the fake nx in $HOME/.local/bin and records the version it was
# asked for (`conexus==X` or the candidate spec). A state file can make it fail or lie.
UV = """#!/bin/sh
S=@S@
echo "uv $*" >> $S/calls.log
for a in "$@"; do last=$a; done
ver=${last#conexus==}
case "$*" in
  *--force*) [ -e $S/uv_force_version ] && ver=$(cat $S/uv_force_version) ;;
  *) [ -e $S/uv_fail_prev ] && { echo "no such version" >&2; exit 1; } ;;
esac
mkdir -p "$HOME/.local/bin"
cp $S/nx.sh "$HOME/.local/bin/nx"
chmod +x "$HOME/.local/bin/nx"
printf '%s' "$ver" > "$HOME/.nx_version"
echo "installed conexus $ver"
"""

# The fake nx. `upgrade`, `daemon service start` and `stop` source a script the test wrote (if any),
# so a test says what that command does. Every call is appended to calls.log.
NX = """#!/bin/sh
S=@S@
echo "nx $*" >> $S/calls.log
case "$*" in
  "--version") echo "conexus $(cat "$HOME/.nx_version")" ;;
  "upgrade") [ -e $S/upgrade.sh ] && . $S/upgrade.sh; touch $S/upgrade_returned ;;
  "doctor") echo "doctor: all checks passed" ;;
  search*) echo "result: @SENTINEL@" ;;
  "daemon service start") echo "service starting"; [ -e $S/start.sh ] && . $S/start.sh ;;
  "daemon service stop") echo "service stopping" ;;
  "daemon service stop --with-pg") echo "service stopping"; [ -e $S/cleanup.sh ] && . $S/cleanup.sh ;;
  *) echo "ok" ;;
esac
exit 0
"""

# The prepare stand-in: what a real seeder leaves behind, as far as the driver reads it.
PREPARE = """#!/bin/sh
S=@S@
work=$1
json=$2
echo "prepare $*" >> $S/calls.log
[ -e $S/prepare_fail ] && { echo "seeder broke" >&2; exit 3; }
mkdir -p "$work/config/pg-bundle/bundle/bin" "$work/pgdata/base" "$work/pgdata/pg_wal"
echo seed > "$work/pgdata/base/file"
cat > "$work/config/pg_credentials" <<EOF
PG_DATA=$work/pgdata
PG_PORT=54999
NX_DB_URL=jdbc:postgresql://127.0.0.1:54999/nexusdb
PG_SUPERUSER_PASS=hunter2-not-printed
EOF
cp $S/psql.sh "$work/config/pg-bundle/bundle/bin/psql"
chmod +x "$work/config/pg-bundle/bundle/bin/psql"
cat > "$json" <<EOF
{"verdict": "prepared", "collections": {"big": 60000}, "data_dir_bytes": 1000000}
EOF
"""

# The fake psql answers by query text. The progress view lists a build while $S/building exists.
PSQL = """#!/bin/sh
S=@S@
for a in "$@"; do q=$a; done
case "$q" in
  "select pg_current_wal_lsn"*)
    n=$(cat $S/lsn); n=$((n + 1048576)); echo $n > $S/lsn; printf '0/%X\\n' $n ;;
  "select wal_bytes"*) echo 5000 ;;
  "select coalesce(sum(write_bytes)"*) printf '1\\t2\\n' ;;
  "select count(*)"*) cat $S/rows ;;
  "select index_relid"*)
    [ -e $S/building ] && printf 'nexus.@INDEX@\\t4242\\tbuilding\\t1\\t10\\t1\\t10\\n' ;;
  "select c.relname"*) cat $S/validity ;;
esac
exit 0
"""

# nx upgrade that runs a pci_ build for `secs` half-seconds and ends with the index valid.
UPGRADE_BUILD = """touch $S/building
i=0
while [ $i -lt @N@ ]; do echo "upgrading step $i"; sleep 0.5; i=$((i + 1)); done
rm -f $S/building
printf '@INDEX@\\tt\\n' > $S/validity
echo "upgrade complete"
"""

# the second `daemon service start` (the restart; the first is start-old) waits, then logs the adoption
START_RESTART = """n=$(grep -c '^nx daemon service start' $S/calls.log)
if [ "$n" -ge 2 ]; then
  if [ -e $S/wait_for_upgrade ]; then  # the restart outlives nx upgrade by construction, not by timing
    i=0; while [ ! -e $S/upgrade_returned ] && [ $i -lt 600 ]; do sleep 0.1; i=$((i + 1)); done
  fi
  sleep @SLEEP@
  mkdir -p "$NEXUS_CONFIG_DIR/logs"
  echo "event=pci_anchor_adopted index=@INDEX@ reason=missing" >> "$NEXUS_CONFIG_DIR/logs/storage_service_native.log"
  echo "start done" >> $S/calls.log
fi
"""


def _script(path: Path, text: str, state: Path, **subs: str) -> Path:
    body = (
        text.replace("@S@", str(state))
        .replace("@SENTINEL@", SENTINEL)
        .replace("@INDEX@", INDEX)
    )
    for k, v in subs.items():
        body = body.replace(f"@{k}@", v)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class World:
    """The fake machine for one test: where the scripts and state files live, and the driver config."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = tmp_path
        self.state = tmp_path / "state"
        self.fakebin = tmp_path / "fakebin"
        self.repo = tmp_path / "repo"
        for d in (self.state, self.fakebin, self.repo):
            d.mkdir()
        (self.state / "lsn").write_text("16777216")
        (self.state / "rows").write_text("60000\n")
        (self.state / "validity").write_text("")
        _script(self.fakebin / "uv", UV, self.state)
        _script(self.state / "nx.sh", NX, self.state)
        _script(self.state / "psql.sh", PSQL, self.state)
        prepare = _script(self.state / "prepare.sh", PREPARE, self.state)
        self.prepare_cmd = f"{prepare} {{work}} {{json}}"
        monkeypatch.setattr(ur, "_ambient_uv_dir", lambda: str(self.fakebin))

    def upgrade_does(self, text: str, **subs: str) -> None:
        _script(self.state / "upgrade.sh", text, self.state, **subs)

    def restart_start_does(
        self, sleep_s: str, *, after_upgrade_returns: bool = False
    ) -> None:
        _script(self.state / "start.sh", START_RESTART, self.state, SLEEP=sleep_s)
        if after_upgrade_returns:
            self.flag("wait_for_upgrade")

    def flag(self, name: str, content: str = "1") -> None:
        (self.state / name).write_text(content)

    def calls(self) -> list[str]:
        return (self.state / "calls.log").read_text().splitlines()

    def cfg(self, **kw) -> object:
        base = {
            "work": self.root / "work",
            "repo": self.repo,
            "target_spec": f"conexus=={CANDIDATE}",
            "target_label": f"conexus {CANDIDATE}",
            "candidate": CANDIDATE,
            "prepare_cmd": self.prepare_cmd,
            "rows_floor": 1000,
            "sentinel_query": "any query",
            "sentinel_expect": SENTINEL,
            "silence_limit_s": 10.0,
            "ceilings": {"package-upgrade": 30.0, "doctor": 30.0, "search": 30.0},
            "upgrade_ceiling_override": 60.0,
            "pci_end_bound_s": 20.0,
        }
        base.update(kw)
        return ur.Config(**base)

    def run_dir(self, version: str) -> Path:
        return self.root / "work" / f"from-{version}"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_path, monkeypatch)


def _steps(verdict: dict) -> dict[str, dict]:
    return {s["name"]: s for s in verdict["steps"]}


def _calls_from(calls: list[str], prefix: str) -> list[int]:
    return [i for i, c in enumerate(calls) if c.startswith(prefix)]


# --- the PCI step is required of a pre-PCI start --------------------------------------------------


def test_a_pre_pci_start_whose_upgrade_fails_has_a_not_run_pci_step_that_is_a_finding(
    world: World,
) -> None:
    world.upgrade_does('echo "upgrade broke"\nexit 1\n')
    v = ur.rehearse("7.76.0", world.cfg())
    steps = _steps(v)
    assert steps["upgrade"]["status"] == "failed"
    # every step after the breach is not-run and says why, with the failed step's own reason
    for later in ("doctor", "search", ur.PCI_STEP):
        assert steps[later]["status"] == "not-run", later
        assert steps[later]["reason"] == "upgrade failed: exit code 1", (
            later,
            steps[later],
        )
    # the PCI step was due (a pre-PCI start): a step that did not run is named in the findings
    assert f"step {ur.PCI_STEP} did not run" in v["findings"]
    assert "step doctor did not run" in v["findings"]
    assert v["state"] == "FAILED"


def test_a_start_from_a_pci_release_does_not_owe_the_pci_step(world: World) -> None:
    v = ur.rehearse("8.0.0", world.cfg())
    assert ur.PCI_STEP not in _steps(v)
    assert v["findings"] == []
    assert v["state"] == "PASSED"
    assert v["pci_check"] is None


# --- nx upgrade's text reaches the DEFERRED check -------------------------------------------------


def test_deferred_work_in_nx_upgrade_output_is_a_finding_though_it_exits_zero(
    world: World,
) -> None:
    world.upgrade_does(
        'echo "rung rung-a: DEFERRED (engine not reachable)"\necho "done"\n'
    )
    v = ur.rehearse("8.0.0", world.cfg())
    assert _steps(v)["upgrade"]["status"] == "ok"
    assert any("reported DEFERRED work" in f for f in v["findings"]), v["findings"]
    assert v["state"] == "FAILED"


def test_a_clean_nx_upgrade_has_no_deferred_finding(world: World) -> None:
    world.upgrade_does('echo "all rungs applied"\n')
    v = ur.rehearse("8.0.0", world.cfg())
    assert v["findings"] == [] and v["state"] == "PASSED"


# --- every setup failure stops the run with its own reason ----------------------------------------


@pytest.mark.parametrize(
    ("flag", "content", "reason_has", "finding_has"),
    [
        ("uv_fail_prev", "1", "setup step install-prev failed", "install-prev"),
        ("prepare_fail", "1", "setup step prepare failed: exit code 3", "prepare"),
        (
            "uv_force_version",
            "9.9.9",
            "after package-upgrade nx --version printed",
            "not the candidate",
        ),
    ],
)
def test_a_setup_failure_stops_the_run_and_every_later_step_carries_its_reason(
    world: World, flag: str, content: str, reason_has: str, finding_has: str
) -> None:
    world.flag(flag, content)
    v = ur.rehearse("7.76.0", world.cfg())
    steps = _steps(v)
    later = [n for n in ("upgrade", "doctor", "search", ur.PCI_STEP) if n in steps]
    assert ur.PCI_STEP in steps
    for name in later:
        assert steps[name]["status"] == "not-run", name
        assert reason_has in steps[name]["reason"], (name, steps[name]["reason"])
        assert steps[name]["reason"] != "an earlier step failed", name
    assert any(finding_has in f for f in v["findings"]), v["findings"]
    assert v["state"] == "FAILED"
    # the upgrade itself never ran
    assert not _calls_from(world.calls(), "nx upgrade")


def test_a_start_old_failure_is_reported_as_the_reason(world: World) -> None:
    (world.state / "start.sh").write_text('echo "engine broke"\nexit 1\n')
    v = ur.rehearse("7.76.0", world.cfg())
    steps = _steps(v)
    assert steps["upgrade"]["reason"].startswith("setup step start-old failed"), steps[
        "upgrade"
    ]
    assert steps[ur.PCI_STEP]["reason"] == steps["upgrade"]["reason"]
    assert any("start-old" in f for f in v["findings"])


# --- the restart, the join and the cleanup --------------------------------------------------------


def test_a_restart_during_nx_upgrade_runs_to_a_pass_and_the_cleanup_frees_the_store(
    world: World,
) -> None:
    world.upgrade_does(UPGRADE_BUILD, N="8")
    world.restart_start_does("0")
    v = ur.rehearse("7.76.0", world.cfg())
    assert v["findings"] == [], v["findings"]
    assert v["state"] == "PASSED"
    steps = _steps(v)
    assert steps[ur.PCI_STEP]["status"] == "ok"
    pci = v["pci_check"]
    assert [o["index"] for o in pci["in_flight_at_restart"]] == [INDEX]
    assert pci["in_flight_at_restart"][0]["pid"] == 4242
    assert pci["log"]["adopted_indexes"] == [INDEX]
    assert pci["restart"]["upgrade_running_at_start"] is True
    assert [s["name"] for s in pci["restart"]["steps"]] == [
        "nx daemon service stop",
        "nx daemon service start",
    ]
    # the order of the calls: the restart sits inside nx upgrade, cleanup comes last
    calls = world.calls()
    up = _calls_from(calls, "nx upgrade")[0]
    stop = _calls_from(calls, "nx daemon service stop")[0]
    done = calls.index("start done")
    doctor = _calls_from(calls, "nx doctor")[0]
    assert up < stop < done < doctor
    assert calls[stop] == "nx daemon service stop"  # no --with-pg on the user's restart
    cleanup = _calls_from(calls, "nx daemon service stop --with-pg")
    assert len(cleanup) == 1 and cleanup[0] > doctor
    assert _calls_from(calls, "nx daemon aspect-worker stop --config-dir")
    # what the upgrade step measured: the rate and the row counts read from PG
    assert v["write_rate"]["wal_bytes"] > 0
    assert v["store_rows"] == {"before": 60000, "after": 60000}
    # the store's bulk is gone once the verdict is made; the evidence (logs, verdict) stays
    run = world.run_dir("7.76.0")
    assert not (run / "pgdata" / "base").exists()
    assert not (run / "pgdata" / "pg_wal").exists()
    assert (run / "logs" / "upgrade.log").exists()
    assert json.loads((run / "verdict.json").read_text())["state"] == "PASSED"


def test_a_restart_still_running_when_nx_upgrade_returns_is_joined_before_doctor(
    world: World,
) -> None:
    # nx upgrade lasts 4 s, long enough for the watcher to see the build; the restart's start waits
    # for nx upgrade to return, so it outlives the step whatever the box's load
    world.upgrade_does(UPGRADE_BUILD, N="8")
    world.restart_start_does("0.3", after_upgrade_returns=True)
    v = ur.rehearse("7.76.0", world.cfg())
    calls = world.calls()
    assert "start done" in calls
    assert calls.index("start done") < _calls_from(calls, "nx doctor")[0], (
        "doctor began while the restart was still running"
    )
    up = v["pci_check"]["nx_upgrade"]
    assert up["ended_during_restart"] is True, up
    assert not any("restart did not complete" in f for f in v["findings"]), v[
        "findings"
    ]
    assert not any("still running long after" in f for f in v["findings"])


def test_the_trigger_that_never_fires_makes_the_pci_step_a_failure_not_a_pass(
    world: World,
) -> None:
    world.upgrade_does('echo "quick"\n')  # no build is ever in flight
    v = ur.rehearse("7.76.0", world.cfg())
    assert _steps(v)[ur.PCI_STEP]["status"] == "failed"
    assert any("no pci_ index build was seen in flight" in f for f in v["findings"])
    assert v["state"] == "FAILED"
    # no build was seen, so the engine was never restarted (the cleanup's stop carries --with-pg)
    assert [c for c in world.calls() if c == "nx daemon service stop"] == []


def test_the_cleanup_runs_after_a_failed_run_and_the_store_is_reclaimed(
    world: World,
) -> None:
    world.flag("prepare_fail")
    ur.rehearse("8.0.0", world.cfg())
    calls = world.calls()
    # prepare failed before any store existed; the nx the old install left is still stopped
    assert _calls_from(calls, "nx daemon service stop --with-pg")
    assert _calls_from(calls, "nx daemon aspect-worker stop")


def test_a_run_whose_install_failed_runs_no_cleanup_because_there_is_no_nx(
    world: World,
) -> None:
    world.flag("uv_fail_prev")
    ur.rehearse("8.0.0", world.cfg())
    assert not _calls_from(world.calls(), "nx ")


def test_store_rows_lost_across_nx_upgrade_are_a_finding(world: World) -> None:
    world.upgrade_does('echo 100 > $S/rows\necho "rewrote"\n')
    v = ur.rehearse("8.0.0", world.cfg())
    assert v["store_rows"] == {"before": 60000, "after": 100}
    assert any("lost rows" in f for f in v["findings"]), v["findings"]
    assert v["state"] == "FAILED"


# --- a pending ceiling is a hang guard, never "no limit" -------------------------------------------


def _record_ceilings(monkeypatch: pytest.MonkeyPatch) -> dict[str, float | None]:
    seen: dict[str, float | None] = {}
    real = ur.run_step

    def spy(name, argv, **kw):
        seen[name] = kw["ceiling_s"]
        return real(name, argv, **kw)

    monkeypatch.setattr(ur, "run_step", spy)
    return seen


def test_a_pending_upgrade_ceiling_runs_under_the_hang_guard(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _record_ceilings(monkeypatch)
    world.upgrade_does('echo "quick"\n')
    # the pre-pci entry is None (pending): set here, so a real number in the table breaks nothing
    monkeypatch.setitem(ur.UPGRADE_CEILINGS_S, "pre-pci", None)
    v = ur.rehearse("7.76.0", world.cfg(upgrade_ceiling_override=None))
    assert seen["upgrade"] == ur.UPGRADE_PENDING_HANG_GUARD_S == 1800.0
    up = _steps(v)["upgrade"]
    assert up["ceiling_pending"] is True and up["ceiling_s"] is None
    assert v["state"] != "PASSED"  # pending is never a pass


def test_a_pending_upgrade_that_outruns_the_hang_guard_is_killed(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(ur.UPGRADE_CEILINGS_S, "pre-pci", None)
    monkeypatch.setattr(ur, "UPGRADE_PENDING_HANG_GUARD_S", 2.0)
    world.upgrade_does(UPGRADE_BUILD, N="40")  # 20 s of output: far past the 2 s guard
    # the killed build never finishes: keep the PCI end wait short
    v = ur.rehearse(
        "7.76.0", world.cfg(upgrade_ceiling_override=None, pci_end_bound_s=2.0)
    )
    up = _steps(v)["upgrade"]
    assert up["status"] == "ceiling", up
    assert "hang guard" in up["reason"] and "PENDING" in up["reason"]
    assert up["wall_s"] < 12.0  # killed near the guard, not left to run its 20 s
    assert v["state"] == "FAILED"


# --- the cleanup's own rules ------------------------------------------------------------------------


@pytest.fixture
def restore_signal_handlers():
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)
    ur._EXIT.__init__()


def test_a_sigterm_during_the_cleanup_does_not_cut_it_short(
    world: World, restore_signal_handlers: None
) -> None:
    ur.install_exit_handlers()
    (world.state / "driver.pid").write_text(str(os.getpid()))
    # the stack stop of the cleanup is where the signal lands
    (world.state / "cleanup.sh").write_text(
        "kill -TERM $(cat $S/driver.pid)\nsleep 1\n"
    )
    with pytest.raises(SystemExit) as exc:
        ur.rehearse("8.0.0", world.cfg())
    assert exc.value.code == 128 + signal.SIGTERM, (
        "the held signal ends the run once the cleanup is done"
    )
    calls = world.calls()
    stop = _calls_from(calls, "nx daemon service stop --with-pg")
    assert stop and _calls_from(calls, "nx daemon aspect-worker stop"), (
        "the worker stop after the signalled stack stop must still run"
    )
    run = world.run_dir("8.0.0")
    assert not (run / "pgdata" / "base").exists(), "and so must the store reclaim"


def test_a_sigterm_while_the_run_is_underway_still_unwinds_through_the_cleanup(
    world: World, restore_signal_handlers: None
) -> None:
    ur.install_exit_handlers()
    (world.state / "driver.pid").write_text(str(os.getpid()))
    world.upgrade_does("kill -TERM $(cat $S/driver.pid)\nsleep 5\n")
    with pytest.raises(SystemExit) as exc:
        ur.rehearse("8.0.0", world.cfg())
    assert exc.value.code == 128 + signal.SIGTERM
    calls = world.calls()
    assert _calls_from(calls, "nx daemon service stop --with-pg")
    assert not (world.run_dir("8.0.0") / "pgdata" / "base").exists()


def test_keep_work_keeps_the_store_the_default_frees(world: World) -> None:
    v = ur.rehearse("8.0.0", world.cfg(keep_work=True))
    assert v["state"] == "PASSED"
    run = world.run_dir("8.0.0")
    assert (run / "pgdata" / "base" / "file").exists()
    assert (run / "pgdata" / "pg_wal").is_dir()
    assert (run / "logs" / "upgrade.log").exists()


def test_a_surviving_postmaster_blocks_the_store_reclaim_and_is_a_finding(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    survivor = (
        "stray postmaster 4242 under x: still running 60s after SIGINT: SIGKILLed"
    )
    monkeypatch.setattr(ur, "_reap_postmasters", lambda run_dir: [survivor])
    v = ur.rehearse("8.0.0", world.cfg())
    run = world.run_dir("8.0.0")
    assert (run / "pgdata" / "base" / "file").exists(), (
        "a store with a postmaster over it is not deleted under it"
    )
    assert survivor in v["findings"] and v["state"] == "FAILED"


def test_the_write_rate_after_reading_is_taken_before_the_restart_thread_is_joined(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    real_take, real_join = ur.take_counters, ur.PciRestartTrigger.join

    def take(sql):
        events.append("take")
        return real_take(sql)

    def join(self, timeout):
        events.append("join")
        return real_join(self, timeout)

    monkeypatch.setattr(ur, "take_counters", take)
    monkeypatch.setattr(ur.PciRestartTrigger, "join", join)
    world.upgrade_does(UPGRADE_BUILD, N="8")
    world.restart_start_does("0.3", after_upgrade_returns=True)
    v = ur.rehearse("7.76.0", world.cfg())
    assert v["write_rate"] is not None
    # the reading before nx upgrade, the reading when it returns, then the join of the restart
    assert events[:3] == ["take", "take", "join"], events
