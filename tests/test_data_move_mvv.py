# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""scripts/data_move_mvv.py: the pure parts of the RDR-246 Minimum Viable Validation driver (nexus-f3yqd.2.22).

The engine runs themselves are a lab-host job; these tests pin what a wrong number would hide: the test jar carries the
test-only changeset exactly once and nothing else changes, the percentile and sleep_ms rules, and the resume verdict.
"""
from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "data_move_mvv.py"
CHANGESET = Path(__file__).resolve().parent.parent / "service" / "src" / "test" / "resources" / "mvv" / "mvv-chunks-move.xml"


def _load():
    spec = importlib.util.spec_from_file_location("data_move_mvv", SCRIPT)
    assert spec is not None and spec.loader is not None, f"cannot load {SCRIPT}"
    mod = importlib.util.module_from_spec(spec)
    import sys

    sys.modules["data_move_mvv"] = mod
    spec.loader.exec_module(mod)
    return mod


mvv = _load()

MASTER = """<databaseChangeLog>
    <include file="db/changelog/taxonomy-023-coverage-counts-set-based.xml"/>
    <include file="db/changelog/grants-nexus-svc.xml"/>
    <include file="db/changelog/grants-nexus-diag.xml"/>
</databaseChangeLog>
"""


class TestPatchMaster:
    def test_include_lands_before_the_service_grants(self):
        out = mvv.patch_master(MASTER)
        lines = out.splitlines()
        mine = next(i for i, ln in enumerate(lines) if mvv.MVV_RESOURCE in ln)
        grants = next(i for i, ln in enumerate(lines) if "grants-nexus-svc.xml" in ln)
        assert mine < grants
        assert out.count(mvv.MVV_RESOURCE) == 1

    def test_a_master_without_the_anchor_is_refused(self):
        with pytest.raises(mvv.MvvError, match="grants-nexus-svc"):
            mvv.patch_master("<databaseChangeLog></databaseChangeLog>")

    def test_a_second_patch_is_refused_not_doubled(self):
        with pytest.raises(mvv.MvvError, match="already"):
            mvv.patch_master(mvv.patch_master(MASTER))


class TestMakeJar:
    def _jar(self, path: Path) -> None:
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("db/changelog/db.changelog-master.xml", MASTER)
            z.writestr("db/changelog/other.xml", "<x/>")
            z.writestr("dev/nexus/Main.class", b"\xca\xfe")

    def test_only_the_master_changes_and_the_changeset_is_added(self, tmp_path):
        src, dst = tmp_path / "in.jar", tmp_path / "out.jar"
        self._jar(src)
        mvv.make_mvv_jar(src, dst, CHANGESET)
        with zipfile.ZipFile(src) as a, zipfile.ZipFile(dst) as b:
            assert set(b.namelist()) - set(a.namelist()) == {mvv.MVV_RESOURCE}
            assert set(a.namelist()) <= set(b.namelist())
            for name in a.namelist():
                if name != "db/changelog/db.changelog-master.xml":
                    assert a.read(name) == b.read(name)
            assert mvv.MVV_RESOURCE in b.read("db/changelog/db.changelog-master.xml").decode()
            assert b.read(mvv.MVV_RESOURCE) == CHANGESET.read_bytes()
            assert len(b.namelist()) == len(set(b.namelist()))  # no duplicate entry for the master

    def test_the_changeset_registers_one_move_over_nexus_chunks(self):
        text = CHANGESET.read_text(encoding="utf-8")
        assert text.count("INSERT INTO nexus.data_moves") == 1
        assert "'nexus', 'chunks'" in text and "mvv-chunks-stamp" in text


class TestPercentile:
    def test_nearest_rank(self):
        xs = [float(i) for i in range(1, 101)]
        assert mvv.percentile(xs, 99) == 99.0
        assert mvv.percentile(xs, 50) == 50.0
        assert mvv.percentile([7.0], 99) == 7.0

    def test_empty_is_refused(self):
        with pytest.raises(mvv.MvvError):
            mvv.percentile([], 99)


class TestChooseSleep:
    def test_smallest_sleep_within_the_bound(self):
        # baseline p99 100 ms; the bound is 120 ms
        rows = [{"sleep_ms": 0, "p99_ms": 190.0}, {"sleep_ms": 100, "p99_ms": 125.0},
                {"sleep_ms": 250, "p99_ms": 119.0}, {"sleep_ms": 500, "p99_ms": 104.0}]
        pick = mvv.choose_sleep(rows, baseline_p99_ms=100.0, bound=0.20)
        assert pick["sleep_ms"] == 250 and pick["within_bound"] is True

    def test_none_within_the_bound_says_so_and_names_the_nearest(self):
        rows = [{"sleep_ms": 0, "p99_ms": 300.0}, {"sleep_ms": 500, "p99_ms": 150.0}]
        pick = mvv.choose_sleep(rows, baseline_p99_ms=100.0, bound=0.20)
        assert pick["within_bound"] is False and pick["sleep_ms"] == 500

    def test_the_bound_is_inclusive(self):
        pick = mvv.choose_sleep([{"sleep_ms": 10, "p99_ms": 120.0}], baseline_p99_ms=100.0, bound=0.20)
        assert pick["within_bound"] is True


class TestResumeVerdict:
    def test_resumed_from_the_last_committed_chunk(self):
        v = mvv.resume_verdict(done_at_kill=40, done_after_restart_first_seen=40, total=100, rows_total=100,
                               rows_once=100, rows_twice=0, rows_missing=0, completed=True)
        assert v["ok"] is True

    def test_a_restart_that_began_again_from_zero_is_not_a_resume(self):
        v = mvv.resume_verdict(done_at_kill=40, done_after_restart_first_seen=0, total=100, rows_total=100,
                               rows_once=100, rows_twice=0, rows_missing=0, completed=True)
        assert v["ok"] is False and "from the start" in v["why"]

    def test_a_row_moved_twice_fails(self):
        v = mvv.resume_verdict(done_at_kill=40, done_after_restart_first_seen=40, total=100, rows_total=100,
                               rows_once=99, rows_twice=1, rows_missing=0, completed=True)
        assert v["ok"] is False

    def test_a_kill_before_any_chunk_is_not_a_mid_move_kill(self):
        v = mvv.resume_verdict(done_at_kill=0, done_after_restart_first_seen=0, total=100, rows_total=100,
                               rows_once=100, rows_twice=0, rows_missing=0, completed=True)
        assert v["ok"] is False and "mid-move" in v["why"]

    def test_a_kill_after_the_last_chunk_is_not_a_mid_move_kill(self):
        v = mvv.resume_verdict(done_at_kill=100, done_after_restart_first_seen=100, total=100, rows_total=100,
                               rows_once=100, rows_twice=0, rows_missing=0, completed=True)
        assert v["ok"] is False and "mid-move" in v["why"]

    def test_a_re_created_run_is_not_a_resume(self):
        v = mvv.resume_verdict(done_at_kill=40, done_after_restart_first_seen=40, total=100, rows_total=100,
                               rows_once=100, rows_twice=0, rows_missing=0, completed=True, same_run_ids=False)
        assert v["ok"] is False and "re-created" in v["why"]

    def test_counts_that_do_not_add_up_fail(self):
        v = mvv.resume_verdict(done_at_kill=40, done_after_restart_first_seen=40, total=100, rows_total=100,
                               rows_once=90, rows_twice=0, rows_missing=0, completed=True)
        assert v["ok"] is False and "add up" in v["why"]

    def test_an_incomplete_move_fails(self):
        v = mvv.resume_verdict(done_at_kill=40, done_after_restart_first_seen=40, total=100, rows_total=100,
                               rows_once=60, rows_twice=0, rows_missing=40, completed=False)
        assert v["ok"] is False


def test_progress_counts_only_the_moves_own_boot_work_entry():
    """Any boot_work entry used to count as the move's progress; only data-move:<label> does (critique S, check 3)."""
    m = _load()
    assert m.move_in_boot_work([{"name": "data-move:mvv-chunks-stamp", "done": 3, "total": 87}], m.MOVE_LABEL)
    assert not m.move_in_boot_work([{"name": "pci:knowledge__1-1__bge__v1"}], m.MOVE_LABEL)
    assert not m.move_in_boot_work(None, m.MOVE_LABEL)
