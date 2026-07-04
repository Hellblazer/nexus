"""Tests for the index-throughput benchmark aggregator (nexus-duoak.2).

The parser consumes the ``--debug-timing`` stderr block emitted by
``nx index repo`` (``nexus.stage_timers.format_report``) plus the wall
clock recorded by run.sh, and emits one row per worker count.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aggregate import parse_debug_timing, parse_runs, format_table

# Exact shape of stage_timers.format_report output, embedded in run noise.
SAMPLE_LOG = """\
  [1180/1180] some-file.ts — 3 chunks  (0.6s)
  [post] Pruning misclassified chunks…
[debug-timing] per-stage totals across 1180 files:
  chunking_s      42.3s  ( 3.1%)
  embed_s        810.5s  (59.9%)
  upload_s       201.2s  (14.9%)
  hooks_s        280.0s  (20.7%)
  retry_s         19.0s  ( 1.4%)
  total         1353.0s
done
"""

EMPTY_LOG = "[debug-timing] no per-stage samples recorded\n"


class TestParseDebugTiming:
    def test_extracts_all_stages(self) -> None:
        stages = parse_debug_timing(SAMPLE_LOG)
        assert stages == {
            "chunking_s": 42.3,
            "embed_s": 810.5,
            "upload_s": 201.2,
            "hooks_s": 280.0,
            "retry_s": 19.0,
            "total_s": 1353.0,
            "n_files": 1180,
        }

    def test_hooks_share_derivable(self) -> None:
        stages = parse_debug_timing(SAMPLE_LOG)
        share = stages["hooks_s"] / stages["total_s"]
        assert share == pytest.approx(0.2069, abs=1e-3)

    def test_empty_block_raises(self) -> None:
        with pytest.raises(ValueError, match="no per-stage samples"):
            parse_debug_timing(EMPTY_LOG)

    def test_missing_block_raises(self) -> None:
        with pytest.raises(ValueError, match="debug-timing block not found"):
            parse_debug_timing("just some\nrandom output\n")


class TestParseRuns:
    def test_aggregates_per_worker_rows(self, tmp_path: Path) -> None:
        # run.sh writes <out>/w{N}.log + <out>/w{N}.wall (seconds, float)
        for workers, wall in ((1, 2400.0), (2, 1300.0)):
            (tmp_path / f"w{workers}.log").write_text(SAMPLE_LOG)
            (tmp_path / f"w{workers}.wall").write_text(f"{wall}\n")
        rows = parse_runs(tmp_path)
        assert len(rows) == 2
        assert [r["workers"] for r in rows] == [1, 2]
        assert rows[0]["wall_s"] == 2400.0
        assert rows[0]["s_per_file"] == pytest.approx(2400.0 / 1180, abs=1e-4)
        assert rows[0]["hooks_s"] == 280.0
        assert rows[0]["hooks_share"] == pytest.approx(280.0 / 1353.0, abs=1e-4)
        # scaling efficiency vs the 1-worker baseline
        assert rows[1]["speedup"] == pytest.approx(2400.0 / 1300.0, abs=1e-4)
        assert rows[0]["speedup"] == pytest.approx(1.0)

    def test_ignores_unrelated_files(self, tmp_path: Path) -> None:
        (tmp_path / "w1.log").write_text(SAMPLE_LOG)
        (tmp_path / "w1.wall").write_text("100.0\n")
        (tmp_path / "notes.txt").write_text("unrelated")
        rows = parse_runs(tmp_path)
        assert len(rows) == 1

    def test_missing_wall_file_raises(self, tmp_path: Path) -> None:
        (tmp_path / "w4.log").write_text(SAMPLE_LOG)
        with pytest.raises(ValueError, match="w4.wall"):
            parse_runs(tmp_path)


class TestFormatTable:
    def test_renders_one_line_per_worker(self, tmp_path: Path) -> None:
        (tmp_path / "w1.log").write_text(SAMPLE_LOG)
        (tmp_path / "w1.wall").write_text("2400.0\n")
        (tmp_path / "w8.log").write_text(SAMPLE_LOG)
        (tmp_path / "w8.wall").write_text("500.0\n")
        table = format_table(parse_runs(tmp_path))
        lines = table.splitlines()
        assert lines[0].split() == [
            "workers", "wall_s", "s/file", "speedup", "hooks_s", "hooks_share",
        ]
        assert len(lines) == 3  # header + 2 rows
        assert lines[1].split()[0] == "1"
        assert lines[2].split()[0] == "8"
        assert lines[2].split()[3] == "4.80"  # 2400/500
