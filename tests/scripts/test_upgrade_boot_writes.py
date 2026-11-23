# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f3yqd.1 / .7: the upgrade-boot write harness's decisions, without a cluster.

The harness itself runs on a lab host against real engines (its docstring); these pin the parts that decide a
verdict from what it sampled: the LSN arithmetic, the pci_ build timeline, the restart pass rule (kept vs
rebuilt by backend pid, adoption required), the rotated-log read, and the rot registry's shape contract.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent.parent
_SCRIPT = _REPO / "scripts" / "upgrade_boot_writes.py"

spec = importlib.util.spec_from_file_location("upgrade_boot_writes", _SCRIPT)
_mod = importlib.util.module_from_spec(spec)
sys.modules["upgrade_boot_writes"] = _mod
spec.loader.exec_module(_mod)


def _s(t: float, lsn: int, *building: tuple[str, int]) -> object:
    return _mod.Sample(t, lsn, None, None, len(building), 0, tuple(building))


def test_lsn_bytes_reads_both_halves() -> None:
    assert _mod.lsn_bytes("0/0") == 0
    assert _mod.lsn_bytes("0/83786058") == 0x83786058
    assert _mod.lsn_bytes("1/0") == 1 << 32


def test_build_timeline_spans_each_index_and_pid() -> None:
    samples = [_s(0, 0), _s(1, 10, ("pci_a", 7)), _s(2, 30, ("pci_a", 7)), _s(3, 40, ("pci_b", 7)), _s(4, 50)]
    tl = _mod.build_timeline(samples)
    assert [(b["index"], b["pid"], b["start_s"], b["end_s"], b["wal_bytes"], b["left_progress"]) for b in tl] == [
        ("pci_a", 7, 1, 3, 30, True), ("pci_b", 7, 3, 4, 10, True)]


def test_build_timeline_marks_a_build_still_running_at_the_end() -> None:
    tl = _mod.build_timeline([_s(0, 0, ("pci_c", 9)), _s(1, 5, ("pci_c", 9))])
    assert tl == [{"index": "pci_c", "pid": 9, "start_s": 0, "end_s": 1, "wal_bytes": 5, "left_progress": False}]


_FIRST = [{"index": "pci_a", "pid": 10, "left_progress": True},
          {"index": "pci_c", "pid": 12, "left_progress": False}]


def test_a_rebuild_from_zero_is_red_in_graceful_mode() -> None:
    r = _mod.resume_analysis(_FIRST, [{"index": "pci_c", "pid": 20, "left_progress": True}], [], "graceful")
    assert not r["resume_ok"]
    assert r["killed_at_restart"] == ["pci_c"] and r["rebuilt_after_restart"] == ["pci_c"]
    assert r["built_twice"] == ["pci_c"] and r["survived_restart"] == []


def test_a_kept_and_adopted_build_is_green() -> None:
    r = _mod.resume_analysis(_FIRST, [{"index": "pci_c", "pid": 12, "left_progress": True}],
                             ["event=pci_anchor_adopted reason=missing index=pci_c leaf=x"], "graceful")
    assert r["resume_ok"], r["resume_why"]
    assert r["survived_restart"] == ["pci_c"] and r["adopted"] == ["pci_c"]


def test_a_kept_build_without_an_adoption_line_is_red() -> None:
    # e90c46349 kill leg: the backend outlived the SIGKILL, then the boot ended it and a new pid rebuilt it.
    second = [{"index": "pci_c", "pid": 12, "left_progress": True}, {"index": "pci_c", "pid": 30, "left_progress": True}]
    r = _mod.resume_analysis(_FIRST, second, [], "kill")
    assert not r["resume_ok"]
    assert "survived but never adopted: ['pci_c']" in r["resume_why"]
    assert r["built_twice"] == ["pci_c"]


def test_with_pg_expects_exactly_one_rebuild_of_the_in_flight_index() -> None:
    once = _mod.resume_analysis(_FIRST, [{"index": "pci_c", "pid": 20, "left_progress": True}], [], "with-pg")
    assert once["resume_ok"], once["resume_why"]
    finished_too = _mod.resume_analysis(
        _FIRST, [{"index": "pci_c", "pid": 20, "left_progress": True}, {"index": "pci_a", "pid": 20, "left_progress": True}],
        [], "with-pg")
    assert not finished_too["resume_ok"]
    assert "finished indexes rebuilt: ['pci_a']" in finished_too["resume_why"]


def test_logwatch_reads_a_rotated_log_from_its_start(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "storage_service_jar.log"
    log.write_text("old line\n" * 50, encoding="utf-8")
    watch = _mod.LogWatch(tmp_path)
    log.write_text("event=pci_builders_kept count=1 reason=only_run_always_pending\n", encoding="utf-8")  # rotated
    assert watch.seen("event=pci_builders_kept")


def test_logwatch_ignores_lines_written_before_the_boot(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "storage_service_jar.log"
    log.write_text("event=pci_reconcile_pass ran_to_end=true\n", encoding="utf-8")
    watch = _mod.LogWatch(tmp_path)
    assert not watch.seen(*_mod.RECONCILE_PASS)
    with log.open("a", encoding="utf-8") as f:
        f.write("event=pci_reconcile_pass leaves=8 ran_to_end=true\n")
    assert watch.seen(*_mod.RECONCILE_PASS)


@pytest.mark.parametrize("name", sorted(_mod.ROT_SHAPES))
def test_each_rot_shape_is_named_documented_and_checked(name: str) -> None:
    shape = _mod.ROT_SHAPES[name]
    assert shape.name == name
    assert "Source:" in shape.doc, "a shape names where it was found"
    assert shape.present.lstrip().upper().startswith("SELECT COUNT(*)"), "presence is one count"
    assert "INSERT INTO" in shape.plant.upper()


def test_a_shape_that_does_not_land_fails_the_run() -> None:
    class _NoRows:
        def sql(self, _q: str) -> list[list[str]]:
            return [["0"]]

    with pytest.raises(_mod.HarnessError, match="did not land"):
        _mod.plant_rot(_NoRows(), ["dimension_mismatch"])


def test_boot_work_absent_counts_as_empty() -> None:
    assert _mod.boot_work_empty(None)
    assert _mod.boot_work_empty({"version": "x"})
    assert _mod.boot_work_empty({"boot_work": []})
    assert not _mod.boot_work_empty({"boot_work": [{"kind": "pci_build"}]})


def test_a_restart_with_nothing_in_flight_is_red() -> None:
    # Review r1 blocker 1: with nothing in flight every rule held vacuously.
    for mode in ("graceful", "kill", "with-pg"):
        r = _mod.resume_analysis([{"index": "pci_a", "pid": 10, "left_progress": True}], [], [], mode)
        assert not r["resume_ok"], mode
        assert any("nothing" in w for w in r["resume_why"])


def test_a_kept_build_that_finished_before_the_second_boot_sampled_is_kept() -> None:
    # Review r1 blocker 3: the backend outlived the stop and finished before the resume boot's first sample.
    r = _mod.resume_analysis(_FIRST, [], ["event=pci_anchor_adopted reason=missing index=pci_c"], "graceful",
                             after_stop_pids={12})
    assert r["resume_ok"], r["resume_why"]
    assert r["survived_restart"] == ["pci_c"]


def test_a_kept_backend_that_a_new_pid_rebuilt_anyway_is_not_kept() -> None:
    r = _mod.resume_analysis(_FIRST, [{"index": "pci_c", "pid": 30, "left_progress": True}], [], "kill",
                             after_stop_pids={12})
    assert not r["resume_ok"]
    assert r["killed_at_restart"] == ["pci_c"] and r["rebuilt_after_restart"] == ["pci_c"]


def test_with_pg_refuses_two_rebuilds_of_one_index() -> None:
    second = [{"index": "pci_c", "pid": 20, "left_progress": True}, {"index": "pci_c", "pid": 21, "left_progress": True}]
    r = _mod.resume_analysis(_FIRST, second, [], "with-pg")
    assert not r["resume_ok"]
    assert "in-flight index rebuilt more than once: ['pci_c']" in r["resume_why"]


@pytest.mark.parametrize(("argv", "says"), [
    (["--rot", "dimension_mismatch", "--interrupt-build"], "--rot and --interrupt-build"),
    (["--restart-mid-queue", "3", "--no-second-boot"], "--restart-mid-queue needs the second boot"),
])
def test_flag_combinations_that_would_test_nothing_are_refused(argv: list[str], says: str, tmp_path: Path,
                                                                capsys: pytest.CaptureFixture[str]) -> None:
    jar = tmp_path / "nexus-service.jar"   # a jar exists, so only the combination can refuse the run
    jar.write_bytes(b"x")
    with pytest.raises(SystemExit) as e:
        _mod.main([*argv, "--new-jar", str(jar)])
    assert e.value.code == 2
    assert says in capsys.readouterr().err


def test_a_reconcile_pass_that_skipped_every_leaf_does_not_count() -> None:
    skipped = "event=pci_reconcile_pass ran_to_end=true leaves=8 processed_leaves=0 skipped_leaves=8"
    worked = "event=pci_reconcile_pass ran_to_end=true leaves=8 processed_leaves=8 skipped_leaves=0"
    assert not _mod.reconcile_pass_did_work([skipped])
    assert _mod.reconcile_pass_did_work([skipped, worked])


def test_an_unreadable_status_is_not_idle() -> None:
    assert _mod.status_says_idle(200, {"version": "x"})          # predates boot_work
    assert _mod.status_says_idle(404, None)                      # predates the route
    assert not _mod.status_says_idle(0, None)                    # no answer
    assert not _mod.status_says_idle(500, None)
    assert not _mod.status_says_idle(200, {"boot_work": [{"kind": "pci_build"}]})


def test_logwatch_reads_a_rotated_log_that_grew_past_the_old_offset(tmp_path: Path) -> None:
    # Review r1 (S9): a fresh file (new inode) is read from its start even when it is already longer.
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "storage_service_jar.log"
    log.write_text("x" * 10 + "\n", encoding="utf-8")
    watch = _mod.LogWatch(tmp_path)
    log.rename(logs / "storage_service_jar.log.1")
    log.write_text("event=pci_anchor_adopted reason=missing index=pci_c\n" + "y" * 200 + "\n", encoding="utf-8")
    assert watch.lines("event=pci_anchor_adopted")


_MIB64 = 64 * 1024 * 1024


@pytest.mark.parametrize(("boot", "ceiling_boot", "verdict", "tripped"), [
    (_MIB64 + 1, _MIB64, "boot-over-ceiling", ["boot"]),       # just over the boot bound
    (_MIB64, _MIB64, "under-ceiling", []),                     # at the bound passes
    (1, None, "measured", []),                                 # no bound given
])
def test_ceiling_verdict(boot: int, ceiling_boot: int | None, verdict: str, tripped: list[str]) -> None:
    # nexus-f3yqd.1 (Sam accepted 2026-11-10): boot writes, migrate + ready of the upgrade boot, at most 64 MiB.
    assert _mod.ceiling_verdict(boot, ceiling_boot) == (verdict, tripped)


def test_boot_wal_is_migrate_plus_ready_of_the_upgrade_boot() -> None:
    boots = [{"label": "upgrade", "phases": {"migrate": {"wal_bytes": 3}, "ready": {"wal_bytes": 4},
                                             "settle": {"wal_bytes": 1000}}},
             {"label": "resume", "phases": {"migrate": {"wal_bytes": 999_999}, "ready": {"wal_bytes": 0}}}]
    assert _mod.boot_wal_bytes(boots[0]) == 7
    assert _mod.boot_wal_bytes({"label": "upgrade", "phases": {"until_failure": {"wal_bytes": 9}}}) == 9


def test_the_gate_reads_the_upgrade_boot_not_the_last(monkeypatch: pytest.MonkeyPatch) -> None:
    # Review of 4891e48bd: on a keep-fix engine the resume boot's migrate phase carries the kept build's WAL;
    # gating the last boot instead of the first would fail exactly the green the fix proves.
    def phases(m: int, r: int) -> dict:
        return {"migrate": {"wal_bytes": m}, "ready": {"wal_bytes": r}, "settle": {"wal_bytes": 1}}

    boots = iter([{"label": "upgrade", "reached_ready": True, "phases": phases(3, 4), "total_wal_bytes": 8},
                  {"label": "reboot", "reached_ready": True, "phases": phases(10 * _MIB64, 0), "total_wal_bytes": 1}])
    monkeypatch.setattr(_mod, "measure_boot", lambda *_a, **_k: next(boots))

    class _Inst:
        def checkpoint_lsn(self) -> str:
            return "0/0"

    class _A:
        second_boot = True
        restart_mode = "graceful"
        _before: dict = {}

    rep = _mod.Report(prev_client="x", new_jar="j", rows_per_collection=1, checkpoint_lsn="0/0")
    _mod.boot_new(_Inst(), _A(), {}, rep)
    assert rep.boot_wal_bytes == 7
    assert rep.reboot_wal_bytes == 10 * _MIB64   # the plain reboot's own figure, the one --ceiling-reboot gates
    assert _mod.ceiling_verdict(rep.boot_wal_bytes, _MIB64) == ("under-ceiling", [])


def test_a_ready_boot_without_its_span_is_an_error_not_zero() -> None:
    with pytest.raises(_mod.HarnessError, match="no migrate/ready WAL"):
        _mod.boot_wal_bytes({"label": "upgrade", "reached_ready": True, "phases": {}})


def test_the_total_ceiling_is_retired_and_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Sam, T2 nexus/sam-ruling-upgrade-gate-move-pacing-2026-11-23: nothing after ready is gated.
    jar = tmp_path / "nexus-service.jar"
    jar.write_bytes(b"x")
    with pytest.raises(SystemExit) as e:
        _mod.main(["--ceiling", "67108864", "--new-jar", str(jar)])
    err = capsys.readouterr().err
    assert e.value.code == 2 and "--ceiling is retired" in err and "--ceiling-boot" in err


@pytest.mark.parametrize(("reboot", "ceiling_reboot", "verdict", "tripped"), [
    (_MIB64 + 1, _MIB64, "reboot-over-ceiling", ["reboot"]),    # a rewrite that repeats on the same-version reboot
    (_MIB64, _MIB64, "under-ceiling", []),                     # at the bound passes
    (10 * _MIB64, None, "measured", []),                       # ungated without the flag
])
def test_reboot_ceiling(reboot: int, ceiling_reboot: int | None, verdict: str, tripped: list[str]) -> None:
    # Sam 2026-11-10 (via halcyon and wren): Leg A's plain same-version reboot writes at most 64 MiB over migrate + ready.
    assert _mod.ceiling_verdict(1, None, reboot, ceiling_reboot) == (verdict, tripped)


def test_the_boot_bound_wins_over_the_reboot_bound() -> None:
    assert _mod.ceiling_verdict(_MIB64 + 1, _MIB64, _MIB64 + 1, _MIB64) == (
        "boot-over-ceiling", ["boot", "reboot"])


@pytest.mark.parametrize("argv", [
    ["--ceiling-reboot", "67108864", "--restart-mid-queue", "3"],   # would gate a resume boot
    ["--ceiling-reboot", "67108864", "--no-second-boot"],           # nothing to gate
])
def test_a_reboot_bound_that_cannot_apply_is_refused(argv: list[str], tmp_path: Path,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    jar = tmp_path / "nexus-service.jar"   # a jar exists, so only the combination can refuse the run
    jar.write_bytes(b"x")
    with pytest.raises(SystemExit) as e:
        _mod.main([*argv, "--new-jar", str(jar)])
    assert e.value.code == 2
    assert "--ceiling-reboot gates the same-version reboot" in capsys.readouterr().err


def test_jar_identity_is_the_sha256_of_the_bytes(tmp_path: Path) -> None:
    jar = tmp_path / "nexus-service.jar"
    jar.write_bytes(b"engine bytes")
    assert _mod.jar_identity(jar) == hashlib.sha256(b"engine bytes").hexdigest()
    assert _mod.jar_identity(tmp_path / "absent.jar") is None


def test_the_build_commit_comes_from_the_gate_jar_stamp() -> None:
    assert _mod.build_commit("0c6f9e1c5+3f2a") == "0c6f9e1c5"
    assert _mod.build_commit(None) is None
    assert _mod.commit_matches("0c6f9e1c5105ea39105b0b2b3febdf44099734a5", "0c6f9e1c5")
    assert not _mod.commit_matches("0c6f9e1c5105ea39105b0b2b3febdf44099734a5", "e90c46349")
    assert not _mod.commit_matches("0c6f9e1c5", None)       # an unstamped jar cannot match
    assert not _mod.commit_matches("0c6f", "0c6f9e1c5")    # too short to mean anything


def test_a_cached_gate_jar_with_the_same_engine_tree_is_accepted() -> None:
    # The 7.72.1 red control (2026-11-10): build-gate-jar.sh reused a jar stamped at an earlier commit whose service/
    # tree was identical; the engine under test was the right one.
    trees = {"aaaaaaa1": "t1", "bbbbbbb2": "t1", "ccccccc3": "t2"}
    tree = trees.get
    assert _mod.jar_identity_verdict("aaaaaaa1", "aaaaaaa1", tree)["how"] == "same commit"
    same = _mod.jar_identity_verdict("bbbbbbb2", "aaaaaaa1", tree)
    assert same["ok"] and same["how"] == "same service/ tree"
    assert not _mod.jar_identity_verdict("ccccccc3", "aaaaaaa1", tree)["ok"]     # a different engine
    assert not _mod.jar_identity_verdict("ccccccc3", None, tree)["ok"]           # no stamp


# Sam 2026-11-21 (T2 nexus/sam-ruling-3h-idle-rule-2026-11-21): a data move parked for a missing prerequisite, or
# paused by an operator, does not hold idle and is reported; a failed move still fails the leg. The harness boots the
# new jar on the old install's bundle (no nx upgrade, so no bundle refresh), so a 7.76.0 start leaves every registered
# move blocked on pg_background for good, which a real upgrade does not.

_BLOCKED = {"id": "data-move:repair-catalog-collection-model-version", "what": "blocked: pg_background is not installed",
            "reason_class": "missing_prerequisite"}
_PAUSED = {"id": "data-move:rdr229-residue-catalog-documents", "what": "paused: by sam"}
_WORKING = {"id": "data-move:rdr229-residue-catalog-documents", "what": "working: 3 of 51 chunks"}
_BLOCKED_OTHER = {"id": "data-move:x", "what": "blocked: retrying a transient error", "reason_class": "transient"}
_FAILED = {"id": "data-move:x", "what": "failed: check violation"}


def test_a_move_parked_for_a_missing_prerequisite_or_paused_does_not_hold_idle() -> None:
    assert _mod.status_says_idle(200, {"boot_work": [_BLOCKED]})
    assert _mod.status_says_idle(200, {"boot_work": [_PAUSED, _BLOCKED]})
    assert _mod.parked_moves({"boot_work": [_BLOCKED, {"kind": "pci_build"}, _PAUSED]}) == [_BLOCKED, _PAUSED]


def test_a_working_or_otherwise_blocked_move_or_any_other_entry_still_holds_idle() -> None:
    for entry in (_WORKING, _BLOCKED_OTHER, {"kind": "pci_build"}):
        assert not _mod.status_says_idle(200, {"boot_work": [entry]}), entry
        assert not _mod.status_says_idle(200, {"boot_work": [_BLOCKED, entry]}), entry
    # the engine's own worker entries are not moves (their ids have no "data-move:" prefix with a label)
    for ident in ("data-move-worker", "data-move-boot-work"):
        assert not _mod.status_says_idle(200, {"boot_work": [{"id": ident, "what": "blocked: x",
                                                              "reason_class": "missing_prerequisite"}]})


def test_a_failed_move_fails_the_leg() -> None:
    assert _mod.failed_moves({"boot_work": [_BLOCKED, _FAILED]}) == [_FAILED]
    assert not _mod.status_says_idle(200, {"boot_work": [_FAILED]})


# ── data-move pacing in the upgrade legs (Sam's choice 1 on ask rdr229-upgrade-gate-pacing) ────

import json  # noqa: E402
import types  # noqa: E402

from tests._module_seam import setattr_in  # noqa: E402


def _main_with_stubbed_boots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str],
                             boot_new=None) -> tuple[int, dict, dict]:  # noqa: ANN001
    """``main`` with the old state and the boots stubbed: what ``boot_new`` was handed, and the report JSON."""
    jar = tmp_path / "nexus-service.jar"
    jar.write_bytes(b"x")
    seen: dict = {}

    def fake_boot_new(inst, a, env, rep) -> None:  # noqa: ANN001
        seen.update(env=dict(env), sleep=a.data_move_sleep_ms)
        rep.boots = [{"label": "upgrade", "reached_ready": True,
                      **({"restarted_mid_queue_s": 1.0} if a.restart_mid_queue else {})}]
        if a.restart_mid_queue:
            rep.resume = {"resume_ok": True}
        if boot_new is not None:
            boot_new(inst, a, env, rep)

    monkeypatch.setattr(_mod, "build_old_state", lambda *_a, **_k: None)
    monkeypatch.setattr(_mod, "boot_new", fake_boot_new)
    monkeypatch.setattr(_mod, "jar_identity", lambda _j: "sha")
    setattr_in(monkeypatch, _mod, "subprocess.run", lambda *_a, **_k: None)
    monkeypatch.setattr(_mod.Install, "pgdata", property(lambda self: tmp_path / "pgdata"))
    out = tmp_path / "report.json"
    rc = _mod.main([*argv, "--new-jar", str(jar), "--work", str(tmp_path / "work"), "--json", str(out),
                    *([] if "--restart-mid-queue" in argv else ["--no-second-boot"])])
    return rc, seen, json.loads(out.read_text(encoding="utf-8"))


def test_main_strips_the_ambient_pause_and_the_boots_add_the_harness_own(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NX_DATA_MOVE_SLEEP_MS", "60000")   # an ambient value on the lab host
    monkeypatch.setenv("NX_SOMETHING_ELSE", "1")
    rc, seen, report = _main_with_stubbed_boots(monkeypatch, tmp_path, [])
    assert rc == 0, report
    assert "NX_DATA_MOVE_SLEEP_MS" not in seen["env"] and "NX_SOMETHING_ELSE" not in seen["env"]
    env = _mod.new_engine_env(seen["env"], Path("/j.jar"), seen["sleep"])
    assert env["NX_DATA_MOVE_SLEEP_MS"] == "0" and env["NEXUS_SERVICE_JAR"] == "/j.jar"
    assert env["NEXUS_CONFIG_DIR"] == seen["env"]["NEXUS_CONFIG_DIR"]


def test_the_report_records_the_pause_and_the_pacing_note(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                          capsys: pytest.CaptureFixture[str]) -> None:
    _rc, seen, report = _main_with_stubbed_boots(monkeypatch, tmp_path, ["--data-move-sleep-ms", "250"])
    assert seen["sleep"] == 250 and report["data_move_sleep_ms"] == 250 and isinstance(report["data_move_sleep_ms"], int)
    note = report["pacing_note"]
    assert "250 ms" in note and "nothing about production pacing" in note and "DataMoveWorker*IntegrationTest" in note
    assert note in capsys.readouterr().err   # the run's own output carries it too


def test_the_default_pause_is_zero_and_recorded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _rc, _seen, report = _main_with_stubbed_boots(monkeypatch, tmp_path, [])
    assert report["data_move_sleep_ms"] == 0 and report["move_wal_bytes"] == 0 and report["pre_move_wal_bytes"] == 0


@pytest.mark.parametrize("value", ["60001", "-1"])
def test_a_pause_outside_the_registry_bounds_is_refused(value: str, tmp_path: Path,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    jar = tmp_path / "nexus-service.jar"
    jar.write_bytes(b"x")
    with pytest.raises(SystemExit) as e:
        _mod.main(["--data-move-sleep-ms", value, "--new-jar", str(jar)])
    assert e.value.code == 2 and "--data-move-sleep-ms" in capsys.readouterr().err


def test_main_gates_migrate_and_ready_only_whatever_the_moves_wrote_after_ready(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    move = 2_100 * 1024 * 1024   # the chunks stamp on the fixture, RDR-229's per-row figure

    def boots(inst, a, env, rep) -> None:  # noqa: ANN001
        rep.boot_wal_bytes, rep.total_wal_bytes, rep.move_wal_bytes = 7, move + 7, move

    rc, _seen, report = _main_with_stubbed_boots(monkeypatch, tmp_path, ["--ceiling-boot", str(_MIB64)], boot_new=boots)
    assert (rc, report["verdict"], report["ceilings_tripped"]) == (0, "under-ceiling", [])
    assert report["move_wal_bytes"] == move and report["total_wal_bytes"] == move + 7   # reported beside the verdict

    def boot_over(inst, a, env, rep) -> None:  # noqa: ANN001
        rep.boot_wal_bytes, rep.move_wal_bytes = _MIB64 + 1, move

    rc, _seen, report = _main_with_stubbed_boots(monkeypatch, tmp_path, ["--ceiling-boot", str(_MIB64)], boot_new=boot_over)
    assert (rc, report["verdict"]) == (1, "boot-over-ceiling")


def test_a_move_still_working_at_the_deadline_is_red_not_a_failed_run(monkeypatch: pytest.MonkeyPatch,
                                                                     tmp_path: Path) -> None:
    def never(inst, a, env, rep) -> None:  # noqa: ANN001
        raise _mod.DataMoveNeverFinished("new engine (upgrade): a data move was still working when the max wait ran out")

    rc, _seen, report = _main_with_stubbed_boots(monkeypatch, tmp_path, [], boot_new=never)
    assert rc == 1 and report["verdict"] == "data-move-never-finished" and "still working" in report["error"]

    def other(inst, a, env, rep) -> None:  # noqa: ANN001
        raise _mod.HarnessError("new engine (upgrade) never reached idle within the max wait")

    rc, _seen, report = _main_with_stubbed_boots(monkeypatch, tmp_path, [], boot_new=other)
    assert rc == 2 and report["verdict"] == "error"


def _clock(monkeypatch: pytest.MonkeyPatch, step: float) -> dict:
    now = {"t": 0.0}

    def monotonic() -> float:
        now["t"] += step
        return now["t"]

    setattr_in(monkeypatch, _mod, "time.monotonic", monotonic)
    setattr_in(monkeypatch, _mod, "time.sleep", lambda _s: None)
    return now


@pytest.mark.parametrize(("working", "raises"), [(True, _mod.DataMoveNeverFinished), (False, _mod.HarnessError)])
def test_wait_idle_at_the_deadline_names_a_working_move(monkeypatch: pytest.MonkeyPatch, working: bool,
                                                        raises: type) -> None:
    _clock(monkeypatch, 10.0)
    status = {"boot_work": [_WORKING if working else {"kind": "pci_build"}]}
    monkeypatch.setattr(_mod, "sample", lambda _i, t0: _mod.Sample(1.0, 100, None, None, 0, 0, ()))
    monkeypatch.setattr(_mod, "http_status", lambda _p, _path: (200, status))
    with pytest.raises(raises) as e:
        _mod.wait_idle(object(), lambda: 1, 0.0, [], _mod.IdleRule(60.0, 1 << 20), 25.0, "new engine (upgrade)")
    assert type(e.value) is raises   # the idle case stays the plain exit-2 error, not the red subclass


# ── the harness's own moves-WAL figures ──

_STAMP = {"id": "data-move:rdr229-stamp-chunks", "what": "working: 12 of 860 chunks"}


def _sm(t: float, lsn: int, active: bool = False) -> object:
    x = _s(t, lsn)
    x.moves_active = active
    return x


def test_move_wal_runs_from_the_first_sample_that_saw_a_move_working_to_idle() -> None:
    samples = [_sm(0, 100), _sm(1, 150), _sm(2, 200, True), _sm(3, 5_000, True), _sm(4, 9_000)]
    assert _mod.move_wal_bytes(samples, samples[-1]) == 9_000 - 200   # everything after it, the vacuum tail included
    assert _mod.pre_move_wal_bytes(samples, samples[0], samples[-1]) == 200 - 100   # ready to the first sighting
    quiet = [_sm(0, 1), _sm(1, 9_000)]
    assert _mod.move_wal_bytes(quiet, quiet[-1]) == 0     # no move seen: nothing attributed to one
    assert _mod.pre_move_wal_bytes(quiet, quiet[0], quiet[-1]) == 8_999   # ... and the whole settle is "before"


def test_only_a_running_move_counts_as_moves_working() -> None:
    assert _mod.moves_working({"boot_work": [_STAMP]})
    assert _mod.moves_working({"boot_work": [_BLOCKED_OTHER]})
    for status in (None, {}, {"boot_work": []}, {"boot_work": [_BLOCKED, _PAUSED, _FAILED]},
                   {"boot_work": [{"kind": "pci_build"}, {"id": "data-move-worker", "what": "working: x"}]}):
        assert not _mod.moves_working(status), status


def test_boot_new_carries_the_moves_wal_into_the_report(monkeypatch: pytest.MonkeyPatch) -> None:
    move, pre = 2_100 * 1024 * 1024, 4096
    boots = iter([{"label": "upgrade", "reached_ready": True, "total_wal_bytes": move + 9, "move_wal_bytes": move,
                   "pre_move_wal_bytes": pre, "phases": {"migrate": {"wal_bytes": 3}, "ready": {"wal_bytes": 4}}},
                  {"label": "reboot", "reached_ready": True,
                   "phases": {"migrate": {"wal_bytes": 1}, "ready": {"wal_bytes": 1}}}])
    monkeypatch.setattr(_mod, "measure_boot", lambda *_a, **_k: next(boots))

    class _Inst:
        def checkpoint_lsn(self) -> str:
            return "0/0"

    class _A:
        second_boot = True
        restart_mode = "graceful"
        _before: dict = {}

    rep = _mod.Report(prev_client="x", new_jar="j", rows_per_collection=1, checkpoint_lsn="0/0")
    _mod.boot_new(_Inst(), _A(), {}, rep)
    assert rep.move_wal_bytes == move and rep.pre_move_wal_bytes == pre   # fails if boot_new stops copying either
    assert _mod.ceiling_verdict(rep.boot_wal_bytes, _MIB64, rep.reboot_wal_bytes, _MIB64) == ("under-ceiling", [])


# ── measure_boot, driven with the engine launch stubbed ──

_REGISTRY = [["rdr229-stamp-chunks", "nexus", "chunks", "100", "20000", "2026-11-23 01:00:00+00"]]


class _FakeProc:
    returncode = None

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        pass

    def wait(self, timeout: float | None = None) -> int:
        return 0


def _drive_measure_boot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, label: str = "upgrade", *,
                        restart: bool = False, sleep_ms: int = 0, max_wait: float = 600.0, status: dict | None = None,
                        registry: list | None = _REGISTRY, rows: list | None = (("86000",),)) -> tuple[dict, list, list]:
    """``measure_boot`` for one boot label with Popen, psql, HTTP and the idle wait faked. Returns the record, the env
    of every launched process, and the SQL it ran."""
    launched: list[dict] = []
    queries: list[str] = []
    setattr_in(monkeypatch, _mod, "subprocess.Popen", lambda argv, **kw: launched.append(kw["env"]) or _FakeProc())
    setattr_in(monkeypatch, _mod, "subprocess.run", lambda *_a, **_k: types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    setattr_in(monkeypatch, _mod, "time.sleep", lambda _s: None)
    tick = {"n": 0}

    def fake_sample(_inst, _t0) -> object:
        tick["n"] += 1
        return _mod.Sample(float(tick["n"]), 1000 * tick["n"], None, None, 0, 0, ())

    def fake_wait_idle(inst, port_fn, t0, samples, rule, deadline, what, extra=None, on_sample=None, parked=None):  # noqa: ANN001
        seen = fake_sample(inst, t0)
        seen.moves_active = True
        samples.append(seen)
        idle = fake_sample(inst, t0)
        samples.append(idle)
        if restart:
            raise _mod.MidQueueRestart
        if parked is not None:
            parked[:] = []
        return idle

    class _Inst:
        config_dir = tmp_path

        def service_port(self) -> int:
            return 4242

        def sql(self, q: str):  # noqa: ANN202
            queries.append(q)
            if "from nexus.data_moves" in q:
                return None if registry is None else registry
            if "greatest(" in q:
                return None if rows is None else [list(r) for r in rows]
            if "pg_current_wal_lsn" in q:
                return [["0/10"]]
            return []

    monkeypatch.setattr(_mod, "sample", fake_sample)
    monkeypatch.setattr(_mod, "port_open", lambda _p: True)
    monkeypatch.setattr(_mod, "http_status", lambda _p, path: (200, {"release_version": "x"} if path == "/version"
                                                               else (status or {"boot_work": []})))
    monkeypatch.setattr(_mod, "wait_idle", fake_wait_idle)
    monkeypatch.setattr(_mod, "hnsw_indexes", lambda _i: [])
    monkeypatch.setattr(_mod, "applied", lambda *_a: True)
    monkeypatch.setattr(_mod, "stop_new", lambda _e: None)
    monkeypatch.setattr(_mod, "run_doctor", lambda *_a, **_k: {})
    a = types.SimpleNamespace(new_jar=Path("/j.jar"), data_move_sleep_ms=sleep_ms, max_wait=max_wait,
                              restart_mid_queue=3 if restart else 0, restart_mode="graceful", reconcile_wait=False,
                              idle_window=60.0, idle_wal_bytes=1 << 20, collections=3, doctor_capture=None)
    env = {"NEXUS_CONFIG_DIR": str(tmp_path), "NX_LOCAL": "1"}   # what main hands over: stripped
    rec = _mod.measure_boot(_Inst(), a, env, label, "0/0")
    return rec, launched, queries


@pytest.mark.parametrize("label", ["upgrade", "reboot", "resume", "retry"])
def test_every_boot_of_the_new_engine_carries_the_pause(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                        label: str) -> None:
    # The production seam: it fails if measure_boot builds its env some other way, for any one boot label.
    _rec, launched, _q = _drive_measure_boot(monkeypatch, tmp_path, label, sleep_ms=7)
    assert len(launched) == 1
    assert launched[0]["NX_DATA_MOVE_SLEEP_MS"] == "7" and launched[0]["NEXUS_SERVICE_JAR"] == "/j.jar"
    assert launched[0]["NEXUS_CONFIG_DIR"] == str(tmp_path)


def test_the_sizing_check_runs_for_the_upgrade_boot_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rec, _l, queries = _drive_measure_boot(monkeypatch, tmp_path, "upgrade", max_wait=600.0)
    assert rec["move_pacing"]["pause_s_total"] == 0 and rec["move_pacing"]["lower_bound"] is True
    assert rec["move_pacing"]["moves"][0]["chunks"] == 860 and any("data_moves" in q for q in queries)
    assert rec["move_pacing"]["time_left_s"] <= 600.0
    for label in ("reboot", "resume", "retry"):
        rec, _l, queries = _drive_measure_boot(monkeypatch, tmp_path, label)
        assert "move_pacing" not in rec and not any("data_moves" in q for q in queries), label


def test_the_sizing_check_judges_the_time_left_not_the_whole_wait(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # 860 chunks x 1000 ms = 860 s of pauses against --max-wait 900: fits the whole wait, but not once the
    # (fake) clock has spent 50 s on each of the calls before the check
    _clock(monkeypatch, 50.0)
    with pytest.raises(_mod.HarnessError, match="s left before --max-wait"):
        _drive_measure_boot(monkeypatch, tmp_path, "upgrade", sleep_ms=1000, max_wait=900.0)


def test_production_pacing_stops_the_run_at_ready(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(_mod.HarnessError, match=r"rdr229-stamp-chunks 86000 rows / 100 per chunk x 60000 ms"):
        _drive_measure_boot(monkeypatch, tmp_path, "upgrade", sleep_ms=60_000)


def test_a_sizing_check_that_cannot_read_fails_loud(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(_mod.HarnessError, match="registry could not be read"):
        _drive_measure_boot(monkeypatch, tmp_path, registry=None)
    with pytest.raises(_mod.HarnessError, match="row count of nexus.chunks .* could not be read"):
        _drive_measure_boot(monkeypatch, tmp_path, rows=None)
    with pytest.raises(_mod.HarnessError, match="row count of nexus.chunks"):
        _drive_measure_boot(monkeypatch, tmp_path, rows=[("",)])
    rec, _l, _q = _drive_measure_boot(monkeypatch, tmp_path, registry=[])   # no open move: a real answer, not a failure
    assert rec["move_pacing"]["moves"] == []


def test_a_move_parked_in_boot_work_is_left_out_of_the_sizing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    parked = {"boot_work": [{"id": "data-move:rdr229-stamp-chunks", "what": "blocked: pg_background missing",
                             "reason_class": "missing_prerequisite"}]}
    # at 60 s a chunk the stamp alone would never fit; parked, it never runs, so nothing is sized
    rec, _l, queries = _drive_measure_boot(monkeypatch, tmp_path, sleep_ms=60_000, status=parked)
    assert rec["move_pacing"]["moves"] == []
    assert not any("greatest(" in q for q in queries)


def test_the_row_count_cannot_read_zero_through_row_level_security(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _rec, _l, queries = _drive_measure_boot(monkeypatch, tmp_path)
    count = next(q for q in queries if "greatest(" in q)
    assert 'count(*) from "nexus"."chunks"' in count and "reltuples" in count


def test_a_restarted_leg_records_the_move_wal_up_to_the_restart(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rec, _l, _q = _drive_measure_boot(monkeypatch, tmp_path, restart=True)
    assert rec["restarted_mid_queue_s"] > 0 and rec["move_wal_bytes"] > 0 and "pre_move_wal_bytes" in rec


def test_a_settled_boot_records_both_moves_wal_figures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rec, _l, _q = _drive_measure_boot(monkeypatch, tmp_path)
    assert rec["move_wal_bytes"] > 0 and rec["pre_move_wal_bytes"] >= 0
    assert rec["total_wal_bytes"] >= rec["move_wal_bytes"] + rec["pre_move_wal_bytes"] - 1


# ── open_moves and check_move_pacing alone ──

# The registered pacing of the RDR-229 stamps (vectors-070): chunks 100 rows / 20 s, the rest 1000 rows / 4 s.
_FIXTURE_MOVES = [
    {"label": "rdr229-stamp-catalog_documents", "table": "nexus.catalog_documents", "rows": 90, "chunk_size": 1000,
     "registered_sleep_ms": 4000, "registered_at": "t1"},
    {"label": "rdr229-stamp-catalog_document_chunks", "table": "nexus.catalog_document_chunks", "rows": 86_000,
     "chunk_size": 1000, "registered_sleep_ms": 4000, "registered_at": "t2"},
    {"label": "rdr229-stamp-chunks", "table": "nexus.chunks", "rows": 86_000, "chunk_size": 100,
     "registered_sleep_ms": 20_000, "registered_at": "t3"},
]


def test_production_pacing_on_the_fixture_fails_the_sizing_check_fast() -> None:
    with pytest.raises(_mod.HarnessError) as e:
        _mod.check_move_pacing(_FIXTURE_MOVES, None, 2400.0)   # what the harness did before: the registered sleep_ms
    msg = str(e.value)
    assert "rdr229-stamp-chunks 86000 rows / 100 per chunk x 20000 ms = 17200 s" in msg
    assert "2400 s left before --max-wait" in msg and "--data-move-sleep-ms" in msg


def test_a_zero_pause_passes_the_sizing_check_and_the_estimate_is_reported() -> None:
    est = _mod.check_move_pacing(_FIXTURE_MOVES, 0, 2400.0)
    assert est["pause_s_total"] == 0 and est["sleep_override_ms"] == 0 and est["lower_bound"] is True
    assert [m["chunks"] for m in est["moves"]] == [1, 86, 860]


def test_the_pause_adds_across_moves_and_is_judged_at_the_bound() -> None:
    # 1 + 86 + 860 = 947 chunks; at 2500 ms that is 2367.5 s, under 2400; at 2600 ms 2462.2 s, over
    assert _mod.check_move_pacing(_FIXTURE_MOVES, 2500, 2400.0)["pause_s_total"] == 2367.5
    with pytest.raises(_mod.HarnessError, match="2462 s"):
        _mod.check_move_pacing(_FIXTURE_MOVES, 2600, 2400.0)
    assert _mod.check_move_pacing([], None, 1.0)["pause_s_total"] == 0


def test_open_moves_reads_the_registry_and_counts_each_driving_table() -> None:
    queries: list[str] = []

    class _Inst:
        def sql(self, q: str) -> list[list[str]]:
            queries.append(q)
            if "from nexus.data_moves d" in q:
                return [["rdr229-stamp-chunks", "nexus", "chunks", "100", "20000", "2026-11-23 01:00:00+00"]]
            return [["86000"]]

    moves = _mod.open_moves(_Inst())
    assert moves == [{"label": "rdr229-stamp-chunks", "table": "nexus.chunks", "rows": 86_000, "chunk_size": 100,
                      "registered_sleep_ms": 20_000, "registered_at": "2026-11-23 01:00:00+00"}]
    assert "completed_at is null" in queries[0] and "s.supersedes = d.label" in queries[0]
    assert _mod.open_moves(_Inst(), frozenset({"rdr229-stamp-chunks"})) == []


def test_leg_a_no_longer_gates_the_total_in_the_skill() -> None:
    skill = (_REPO / ".claude" / "skills" / "engine-release" / "SKILL.md").read_text(encoding="utf-8")
    legs = [ln for ln in skill.splitlines() if "scripts/upgrade_boot_writes.py" in ln or "--json output/gate/" in ln]
    assert any("output/gate/A.json" in ln for ln in legs)
    assert not any("--ceiling " in ln for ln in legs), legs      # --ceiling-boot / --ceiling-reboot only
    for word in ("move_wal_bytes", "data_move_sleep_ms", "pre_move_wal_bytes", "nothing about production pacing",
                 "lower bound", "data-move-never-finished"):
        assert word in skill, word
    assert "Do not add `--ceiling` back" not in skill and "autovacuum the moves leave still count" not in skill
    assert "nothing legitimate writes in it" not in skill
