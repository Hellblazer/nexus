# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx collection health`` composite report — RDR-087 Phase 3.4.

Tests decompose into three layers:

1. ``Telemetry.query_collection_stats`` — new T2 aggregate API pinned in
   isolation with seeded rows.
2. ``compute_collection_health`` orchestrator — every per-column
   computation is dependency-injected so the test can drive each
   outcome class deterministically without live T2/T3/catalog.
3. Formatters (human + JSON) and the ``nx collection health`` CLI
   wiring, asserting ``--sort`` and ``--format=json`` behaviour.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner


# ── Telemetry.query_collection_stats ────────────────────────────────────────


class TestTelemetryQueryCollectionStats:
    def _fresh_telemetry(self, tmp_path: Path):
        from nexus.db.t2.telemetry import Telemetry

        db_path = tmp_path / "memory.db"
        return Telemetry(db_path)

    def _seed(self, tm, collection: str, rows):
        """Rows: iterable of (age_days, raw, kept, top_distance)."""
        now = datetime.now(UTC)
        payload = []
        for i, (age, raw, kept, dist) in enumerate(rows):
            ts = (now - timedelta(days=age)).isoformat()
            payload.append(
                (ts, f"hash{i:04d}", collection, raw, kept, dist, 0.45),
            )
        tm.log_search_batch(payload)

    def test_empty_table_returns_none_placeholders(self, tmp_path) -> None:
        tm = self._fresh_telemetry(tmp_path)
        stats = tm.query_collection_stats("code__x")
        assert stats["row_count"] == 0
        assert stats["zero_hit_rate"] is None
        assert stats["median_top_distance"] is None
        tm.close()

    def test_zero_hit_rate_computed_over_window(self, tmp_path) -> None:
        tm = self._fresh_telemetry(tmp_path)
        # 4 in-window rows: 2 kept>0, 2 kept==0 → rate=0.5.
        # 1 out-of-window row: 45d old, should be ignored.
        self._seed(tm, "docs__a", [
            (1, 5, 3, 0.30),
            (2, 4, 2, 0.40),
            (3, 3, 3, 0.50),  # kept_count = 3 - 3 = 0? no: kept=3, dropped=0.
            (5, 5, 0, 0.20),  # kept=0
            (45, 2, 0, 0.10),  # out of window
        ])
        # Re-seed row #3 as genuinely kept==0 (my list had kept=3 above —
        # reorder so test matches the 0.5 rate assertion cleanly):
        tm.conn.execute("DELETE FROM search_telemetry")
        tm.conn.commit()
        self._seed(tm, "docs__a", [
            (1, 5, 3, 0.30),   # kept>0
            (2, 4, 0, 0.40),   # kept==0
            (3, 3, 2, 0.50),   # kept>0
            (5, 5, 0, 0.20),   # kept==0
            (45, 2, 0, 0.10),  # out of window
        ])
        stats = tm.query_collection_stats("docs__a", days=30)
        assert stats["row_count"] == 4
        assert stats["zero_hit_rate"] == pytest.approx(0.5)
        tm.close()

    def test_median_top_distance_ignores_raw_zero(self, tmp_path) -> None:
        """Median is computed only over rows with raw_count > 0."""
        tm = self._fresh_telemetry(tmp_path)
        self._seed(tm, "code__x", [
            (1, 5, 2, 0.20),
            (1, 3, 0, 0.40),
            (1, 4, 1, 0.60),
            (1, 0, 0, None),  # raw_count==0 — excluded
        ])
        stats = tm.query_collection_stats("code__x")
        # distances in raw>0 rows: 0.20, 0.40, 0.60 — median = 0.40.
        assert stats["median_top_distance"] == pytest.approx(0.40)
        tm.close()

    def test_other_collections_do_not_leak(self, tmp_path) -> None:
        tm = self._fresh_telemetry(tmp_path)
        self._seed(tm, "code__a", [(1, 5, 3, 0.30)])
        self._seed(tm, "code__b", [(1, 3, 0, 0.70)])
        stats_a = tm.query_collection_stats("code__a")
        stats_b = tm.query_collection_stats("code__b")
        assert stats_a["row_count"] == 1
        assert stats_a["zero_hit_rate"] == pytest.approx(0.0)
        assert stats_b["row_count"] == 1
        assert stats_b["zero_hit_rate"] == pytest.approx(1.0)
        tm.close()

    def test_rejects_zero_days(self, tmp_path) -> None:
        tm = self._fresh_telemetry(tmp_path)
        with pytest.raises(ValueError):
            tm.query_collection_stats("any", days=0)
        tm.close()


# ── compute_collection_health orchestrator ─────────────────────────────────


def _fake_catalog_stats(col: str) -> dict:
    data = {
        "code__alpha":  {"chunk_count": 120, "last_indexed": "2026-04-15", "orphan_count": 2},
        "docs__beta":   {"chunk_count": 30,  "last_indexed": "2026-04-10", "orphan_count": 0},
        "docs__stale":  {"chunk_count": 0,   "last_indexed": None,          "orphan_count": 0},
    }
    return data.get(col, {"chunk_count": 0, "last_indexed": None, "orphan_count": 0})


def _fake_telemetry_stats(col: str) -> dict:
    data = {
        "code__alpha": {"row_count": 50, "zero_hit_rate": 0.10, "median_top_distance": 0.35},
        "docs__beta":  {"row_count": 0,  "zero_hit_rate": None, "median_top_distance": None},
        "docs__stale": {"row_count": 8,  "zero_hit_rate": 1.00, "median_top_distance": 0.80},
    }
    return data.get(col, {"row_count": 0, "zero_hit_rate": None, "median_top_distance": None})


def _fake_projection_ranks(cols: list[str]) -> dict[str, int]:
    # code__alpha receives from 5 source collections, docs__beta from 2.
    return {"code__alpha": 1, "docs__beta": 2}


def _fake_hub_score(col: str) -> float | None:
    return {"code__alpha": 0.05, "docs__beta": 0.20}.get(col)


def _fake_chash_coverage(col: str) -> float | None:
    # code__alpha fully backfilled; docs__beta partial; docs__stale absent.
    return {
        "code__alpha": 1.0,
        "docs__beta": 0.75,
        "docs__stale": 0.0,
    }.get(col)


class TestComputeCollectionHealth:
    def test_rows_assemble_from_injected_fns(self) -> None:
        from nexus.collection_health import compute_collection_health

        rows = compute_collection_health(
            ["code__alpha", "docs__beta", "docs__stale"],
            catalog_stats_fn=_fake_catalog_stats,
            telemetry_stats_fn=_fake_telemetry_stats,
            projection_rank_fn=_fake_projection_ranks,
            hub_score_fn=_fake_hub_score,
            chash_coverage_fn=_fake_chash_coverage,
        )
        by_name = {r.name: r for r in rows}
        assert by_name["code__alpha"].chunk_count == 120
        assert by_name["code__alpha"].zero_hit_rate_30d == pytest.approx(0.10)
        assert by_name["code__alpha"].cross_projection_rank == 1
        assert by_name["code__alpha"].orphan_catalog_rows == 2
        assert by_name["code__alpha"].hub_domination_score == pytest.approx(0.05)
        # stale_source_ratio is the deferred column (nexus-8luh); placeholder.
        assert by_name["code__alpha"].stale_source_ratio == "—"

    def test_empty_telemetry_sets_placeholders(self) -> None:
        from nexus.collection_health import compute_collection_health

        rows = compute_collection_health(
            ["docs__beta"],
            catalog_stats_fn=_fake_catalog_stats,
            telemetry_stats_fn=_fake_telemetry_stats,
            projection_rank_fn=_fake_projection_ranks,
            hub_score_fn=_fake_hub_score,
            chash_coverage_fn=_fake_chash_coverage,
        )
        assert rows[0].zero_hit_rate_30d is None
        assert rows[0].median_query_distance_30d is None

    def test_missing_projection_rank_is_none(self) -> None:
        from nexus.collection_health import compute_collection_health

        rows = compute_collection_health(
            ["docs__stale"],  # not in projection map
            catalog_stats_fn=_fake_catalog_stats,
            telemetry_stats_fn=_fake_telemetry_stats,
            projection_rank_fn=_fake_projection_ranks,
            hub_score_fn=_fake_hub_score,
            chash_coverage_fn=_fake_chash_coverage,
        )
        assert rows[0].cross_projection_rank is None

    def test_missing_hub_score_is_none(self) -> None:
        from nexus.collection_health import compute_collection_health

        rows = compute_collection_health(
            ["docs__stale"],
            catalog_stats_fn=_fake_catalog_stats,
            telemetry_stats_fn=_fake_telemetry_stats,
            projection_rank_fn=_fake_projection_ranks,
            hub_score_fn=_fake_hub_score,
            chash_coverage_fn=_fake_chash_coverage,
        )
        assert rows[0].hub_domination_score is None


# ── Chash coverage (RDR-087 Phase 4.6) ─────────────────────────────────────


class TestChashCoverage:
    """Ratio of chash_index rows to T3 chunks, surfaced post-RDR-086."""

    def test_ratio_surfaces_on_rows(self) -> None:
        from nexus.collection_health import compute_collection_health

        rows = compute_collection_health(
            ["code__alpha", "docs__beta", "docs__stale"],
            catalog_stats_fn=_fake_catalog_stats,
            telemetry_stats_fn=_fake_telemetry_stats,
            projection_rank_fn=_fake_projection_ranks,
            hub_score_fn=_fake_hub_score,
            chash_coverage_fn=_fake_chash_coverage,
        )
        by_name = {r.name: r for r in rows}
        assert by_name["code__alpha"].chash_indexed_ratio == pytest.approx(1.0)
        assert by_name["docs__beta"].chash_indexed_ratio == pytest.approx(0.75)
        assert by_name["docs__stale"].chash_indexed_ratio == pytest.approx(0.0)

    def test_missing_coverage_is_none(self) -> None:
        from nexus.collection_health import compute_collection_health

        rows = compute_collection_health(
            ["code__unknown"],
            catalog_stats_fn=_fake_catalog_stats,
            telemetry_stats_fn=_fake_telemetry_stats,
            projection_rank_fn=_fake_projection_ranks,
            hub_score_fn=_fake_hub_score,
            chash_coverage_fn=_fake_chash_coverage,
        )
        assert rows[0].chash_indexed_ratio is None

    def test_human_output_hints_when_coverage_below_one(self) -> None:
        from nexus.collection_health import (
            compute_collection_health, format_health_table,
        )

        rows = compute_collection_health(
            ["code__alpha", "docs__beta", "docs__stale"],
            catalog_stats_fn=_fake_catalog_stats,
            telemetry_stats_fn=_fake_telemetry_stats,
            projection_rank_fn=_fake_projection_ranks,
            hub_score_fn=_fake_hub_score,
            chash_coverage_fn=_fake_chash_coverage,
        )
        out = format_health_table(rows, sort_by="name")
        # At least one row has ratio < 1.0 → hint line must appear.
        assert "backfill-hash" in out

    def test_human_output_no_hint_when_all_fully_covered(self) -> None:
        from nexus.collection_health import (
            compute_collection_health, format_health_table,
        )

        def _all_covered(_col: str) -> float:
            return 1.0

        rows = compute_collection_health(
            ["code__alpha"],
            catalog_stats_fn=_fake_catalog_stats,
            telemetry_stats_fn=_fake_telemetry_stats,
            projection_rank_fn=_fake_projection_ranks,
            hub_score_fn=_fake_hub_score,
            chash_coverage_fn=_all_covered,
        )
        out = format_health_table(rows, sort_by="name")
        assert "backfill-hash" not in out


# ── Formatters ─────────────────────────────────────────────────────────────


class TestFormatters:
    @pytest.fixture
    def rows(self):
        from nexus.collection_health import compute_collection_health

        return compute_collection_health(
            ["code__alpha", "docs__beta", "docs__stale"],
            catalog_stats_fn=_fake_catalog_stats,
            telemetry_stats_fn=_fake_telemetry_stats,
            projection_rank_fn=_fake_projection_ranks,
            hub_score_fn=_fake_hub_score,
            chash_coverage_fn=_fake_chash_coverage,
        )

    def test_human_format_contains_all_columns(self, rows) -> None:
        from nexus.collection_health import format_health_table

        out = format_health_table(rows, sort_by="name")
        for col in [
            "name", "chunk_count", "last_indexed", "zero_hit_rate",
            "median", "cross_projection", "orphan", "stale", "hub_domination",
        ]:
            assert col in out.lower()

    def test_human_format_renders_none_as_placeholder(self, rows) -> None:
        from nexus.collection_health import format_health_table

        out = format_health_table(rows, sort_by="name")
        # docs__beta has empty telemetry → two '—' cells in its row.
        lines = [l for l in out.split("\n") if "docs__beta" in l]
        assert lines, "docs__beta row missing from output"
        assert "—" in lines[0]

    def test_json_format_is_parseable(self, rows) -> None:
        from nexus.collection_health import format_health_json

        payload = json.loads(format_health_json(rows))
        assert isinstance(payload, dict)
        assert "collections" in payload
        assert "generated_at" in payload
        assert len(payload["collections"]) == 3
        names = {c["name"] for c in payload["collections"]}
        assert names == {"code__alpha", "docs__beta", "docs__stale"}

    def test_sort_by_chunk_count_desc(self, rows) -> None:
        from nexus.collection_health import format_health_table

        out = format_health_table(rows, sort_by="chunk_count")
        # code__alpha (120) first, docs__beta (30) second, docs__stale (0) last.
        idx_alpha = out.find("code__alpha")
        idx_beta = out.find("docs__beta")
        idx_stale = out.find("docs__stale")
        assert 0 <= idx_alpha < idx_beta < idx_stale

    def test_sort_rejects_unknown_column(self, rows) -> None:
        from nexus.collection_health import format_health_table

        with pytest.raises(ValueError):
            format_health_table(rows, sort_by="nonsense")


# ── CLI: `nx collection health` ────────────────────────────────────────────


class TestCollectionHealthCli:
    @pytest.fixture
    def runner(self):
        return CliRunner()

    def _stub(self, monkeypatch):
        monkeypatch.setattr(
            "nexus.collection_health._enumerate_collections",
            lambda: ["code__alpha", "docs__beta", "docs__stale"],
        )
        monkeypatch.setattr(
            "nexus.collection_health._catalog_stats_fn",
            _fake_catalog_stats,
        )
        monkeypatch.setattr(
            "nexus.collection_health._telemetry_stats_fn",
            _fake_telemetry_stats,
        )
        monkeypatch.setattr(
            "nexus.collection_health._projection_rank_fn",
            _fake_projection_ranks,
        )
        monkeypatch.setattr(
            "nexus.collection_health._hub_score_fn",
            _fake_hub_score,
        )

    def test_default_human_output(self, runner, monkeypatch) -> None:
        from nexus.cli import main

        self._stub(monkeypatch)
        result = runner.invoke(main, ["collection", "health"])
        assert result.exit_code == 0, result.output
        assert "code__alpha" in result.output

    def test_json_output(self, runner, monkeypatch) -> None:
        from nexus.cli import main

        self._stub(monkeypatch)
        result = runner.invoke(main, ["collection", "health", "--format", "json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert "collections" in payload

    def test_sort_flag(self, runner, monkeypatch) -> None:
        from nexus.cli import main

        self._stub(monkeypatch)
        result = runner.invoke(
            main, ["collection", "health", "--sort", "chunk_count"],
        )
        assert result.exit_code == 0, result.output

    def test_sort_rejects_unknown_value(self, runner, monkeypatch) -> None:
        from nexus.cli import main

        self._stub(monkeypatch)
        result = runner.invoke(
            main, ["collection", "health", "--sort", "nonsense"],
        )
        assert result.exit_code != 0
