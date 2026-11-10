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


@pytest.mark.parametrize(("boot", "total", "ceiling", "ceiling_boot", "verdict", "tripped"), [
    (_MIB64 + 1, _MIB64 + 1, None, _MIB64, "boot-over-ceiling", ["boot"]),       # just over the boot bound
    (_MIB64, 10 * _MIB64, None, _MIB64, "under-ceiling", []),                    # at the bound passes; total ungated
    (1, _MIB64 + 1, _MIB64, None, "over-ceiling", ["total"]),                    # the total bound alone
    (_MIB64 + 1, _MIB64 + 1, _MIB64, _MIB64, "boot-over-ceiling", ["boot", "total"]),
    (1, 1, None, None, "measured", []),                                          # no bound given
])
def test_ceiling_verdict(boot: int, total: int, ceiling: int | None, ceiling_boot: int | None, verdict: str,
                         tripped: list[str]) -> None:
    # nexus-f3yqd.1 (Sam accepted 2026-11-10): boot writes, migrate + ready of the upgrade boot, at most 64 MiB.
    assert _mod.ceiling_verdict(boot, total, ceiling, ceiling_boot) == (verdict, tripped)


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
    assert _mod.ceiling_verdict(rep.boot_wal_bytes, rep.total_wal_bytes, None, _MIB64) == ("under-ceiling", [])


def test_a_ready_boot_without_its_span_is_an_error_not_zero() -> None:
    with pytest.raises(_mod.HarnessError, match="no migrate/ready WAL"):
        _mod.boot_wal_bytes({"label": "upgrade", "reached_ready": True, "phases": {}})


def test_the_total_bound_passes_at_equality() -> None:
    assert _mod.ceiling_verdict(1, _MIB64, _MIB64, None) == ("under-ceiling", [])


@pytest.mark.parametrize(("reboot", "ceiling_reboot", "verdict", "tripped"), [
    (_MIB64 + 1, _MIB64, "reboot-over-ceiling", ["reboot"]),    # a rewrite that repeats on the same-version reboot
    (_MIB64, _MIB64, "under-ceiling", []),                     # at the bound passes
    (10 * _MIB64, None, "measured", []),                       # ungated without the flag
])
def test_reboot_ceiling(reboot: int, ceiling_reboot: int | None, verdict: str, tripped: list[str]) -> None:
    # Sam 2026-11-10 (via halcyon and wren): Leg A's plain same-version reboot writes at most 64 MiB over migrate + ready.
    assert _mod.ceiling_verdict(1, 1, None, None, reboot, ceiling_reboot) == (verdict, tripped)


def test_the_boot_bound_wins_over_the_reboot_bound() -> None:
    assert _mod.ceiling_verdict(_MIB64 + 1, 1, None, _MIB64, _MIB64 + 1, _MIB64) == (
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
