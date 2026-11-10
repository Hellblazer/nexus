# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f3yqd.1 / .7: the upgrade-boot write harness's decisions, without a cluster.

The harness itself runs on a lab host against real engines (its docstring); these pin the parts that decide a
verdict from what it sampled: the LSN arithmetic, the pci_ build timeline, the restart pass rule (kept vs
rebuilt by backend pid, adoption required), the rotated-log read, and the rot registry's shape contract.
"""
from __future__ import annotations

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
