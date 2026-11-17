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


class TestPgLogReading:
    """S3 (critique 2026-11-17): the daemon's PostgreSQL logs to <pgdata>/pg.log (pg_provision: ``pg_ctl -l pg.log``),
    not to <pgdata>/log/. A read of the wrong place found 0 lines and called it a pass."""

    def test_the_daemons_pg_log_is_found(self, tmp_path):
        (tmp_path / "pg.log").write_text("x\n")
        assert [p.name for p in mvv.pg_log_files(tmp_path)] == ["pg.log"]

    def test_a_collector_directory_is_read_too(self, tmp_path):
        (tmp_path / "pg.log").write_text("x\n")
        (tmp_path / "log").mkdir()
        (tmp_path / "log" / "postgresql-1.log").write_text("y\n")
        assert sorted(p.name for p in mvv.pg_log_files(tmp_path)) == ["pg.log", "postgresql-1.log"]

    def test_no_log_at_all_is_an_empty_list_not_a_pass(self, tmp_path):
        assert mvv.pg_log_files(tmp_path) == []

    def test_lines_with_reads_only_what_was_written_after_the_start_offsets(self, tmp_path):
        log = tmp_path / "pg.log"
        log.write_text("old SECRET line\n")
        start = mvv.log_sizes([log])
        with log.open("a") as f:
            f.write("ERROR: invalid input syntax for type integer: \"SECRET\"\nunrelated\n")
        assert mvv.lines_with([log], "SECRET", start) == ['ERROR: invalid input syntax for type integer: "SECRET"']
        assert len(mvv.lines_with([log], "SECRET", {})) == 2


class TestMaskingVerdict:
    def _v(self, **kw):
        base = dict(pg_log_lines=2, engine_log_lines=0, failure_column_hits=0, masking_installed=True, masked_line_seen=True)
        base.update(kw)
        return mvv.masking_verdict(**base)

    def test_the_password_in_the_pg_log_and_nowhere_in_the_engines_output_passes(self):
        v = self._v()
        assert v["ok"] is True and v["checks"]["control_pg_log_carries_the_password"] is True

    def test_no_password_in_the_pg_log_means_the_control_did_not_fire(self):
        v = self._v(pg_log_lines=0)
        assert v["ok"] is False and "control" in v["why"]

    def test_a_password_in_an_engine_log_line_fails(self):
        v = self._v(engine_log_lines=1)
        assert v["ok"] is False and v["checks"]["no_engine_log_line_carries_the_password"] is False

    def test_a_password_in_a_failure_column_fails(self):
        v = self._v(failure_column_hits=1)
        assert v["ok"] is False and v["checks"]["no_failure_column_carries_the_password"] is False

    def test_masking_not_installed_or_no_masked_line_fails(self):
        assert self._v(masking_installed=False)["ok"] is False
        assert self._v(masked_line_seen=False)["ok"] is False


class TestPostmasterPid:
    def test_the_first_line_is_the_pid(self):
        assert mvv.postmaster_pid("12345\n/path/data\n1700000000\n5432\n") == 12345

    def test_an_unreadable_file_is_refused(self):
        with pytest.raises(mvv.MvvError):
            mvv.postmaster_pid("")
        with pytest.raises(mvv.MvvError):
            mvv.postmaster_pid("not-a-pid\n")


class TestCrashVerdict:
    def _v(self, **kw):
        base = dict(in_flight_at_kill=1, done_before_kill=35, done_after_recovery=35, moved_after_recovery=35000,
                    chunk_size=1000, total_chunks=87, rows_total=86040, rows_once=86040, rows_twice=0, rows_missing=0,
                    completed=True, recovery_logged=True)
        base.update(kw)
        return mvv.crash_verdict(**base)

    def test_an_aborted_chunk_rolled_back_and_the_move_finished(self):
        v = self._v()
        assert v["ok"] is True, v["why"]

    def test_no_chunk_in_flight_at_the_kill_proves_nothing(self):
        v = self._v(in_flight_at_kill=0)
        assert v["ok"] is False and "in flight" in v["why"]

    def test_rows_beyond_the_committed_chunks_mean_a_partial_chunk_survived(self):
        v = self._v(moved_after_recovery=35500)
        assert v["ok"] is False and "committed" in v["why"]

    def test_fewer_rows_than_committed_chunks_fails_too(self):
        assert self._v(moved_after_recovery=34000)["ok"] is False

    def test_chunks_that_went_backwards_fail(self):
        assert self._v(done_after_recovery=34, moved_after_recovery=34000)["ok"] is False

    def test_the_last_chunk_is_partial_and_the_rule_caps_at_the_total(self):
        v = self._v(done_before_kill=86, done_after_recovery=87, moved_after_recovery=86040)
        assert v["ok"] is True, v["why"]

    def test_a_crash_that_left_no_recovery_line_in_the_pg_log_fails(self):
        assert self._v(recovery_logged=False)["ok"] is False

    def test_a_row_twice_or_missing_or_an_unfinished_move_fails(self):
        assert self._v(rows_twice=1, rows_once=86039)["ok"] is False
        assert self._v(rows_missing=1, rows_once=86039)["ok"] is False
        assert self._v(completed=False)["ok"] is False
