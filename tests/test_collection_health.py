# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx collection health`` composite report — RDR-087 Phase 3.4.

Tests decompose into two layers:

1. ``compute_collection_health`` orchestrator — every per-column
   computation is dependency-injected so the test can drive each
   outcome class deterministically without live T2/T3/catalog.
2. Formatters (human + JSON) and the ``nx collection health`` CLI
   wiring, asserting ``--sort`` and ``--format=json`` behaviour.

The former layer 1 (``Telemetry.query_collection_stats`` pinned against
seeded SQLite rows) died with the SQLite telemetry store (nexus-i711w
Stage 2 sub-stage A2). The aggregate's semantics — 30d windowing,
zero_hit_rate, median over raw_count>0 rows only, days>=1 validation —
now live solely in the engine's ``TelemetryRepository.queryCollectionStats``
(GET /v1/telemetry/search/stats); see TelemetryRepositoryTest.java and
tests/db/test_http_telemetry_store.py for the surviving coverage.
"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner


# ── compute_collection_health orchestrator ─────────────────────────────────


def _fake_catalog_stats(col: str) -> dict:
    data = {
        "code__alpha":  {"chunk_count": 120, "last_indexed": "2026-04-15",
                         "orphan_count": 2, "stale_source_ratio": 0.4},
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


def _fake_chunk_count(col: str) -> int:
    """T3-sourced chunk counts (nexus-39zi). The real implementation
    reads ``coll.count()`` on the live ChromaDB collection; tests
    return deterministic fake values."""
    return {
        "code__alpha": 120,
        "docs__beta": 30,
        "docs__stale": 0,
    }.get(col, 0)


class TestComputeCollectionHealth:
    def test_rows_assemble_from_injected_fns(self) -> None:
        from nexus.collection_health import compute_collection_health

        rows = compute_collection_health(
            ["code__alpha", "docs__beta", "docs__stale"],
            catalog_stats_fn=_fake_catalog_stats,
            telemetry_stats_fn=_fake_telemetry_stats,
            projection_rank_fn=_fake_projection_ranks,
            hub_score_fn=_fake_hub_score,
        )
        by_name = {r.name: r for r in rows}
        assert by_name["code__alpha"].chunk_count == 120
        assert by_name["code__alpha"].zero_hit_rate_30d == pytest.approx(0.10)
        assert by_name["code__alpha"].cross_projection_rank == 1
        assert by_name["code__alpha"].orphan_catalog_rows == 2
        assert by_name["code__alpha"].hub_domination_score == pytest.approx(0.05)
        # nexus-agsq7: stale_source_ratio (index-age proxy) is wired from the
        # catalog; None when the catalog provides none.
        assert by_name["code__alpha"].stale_source_ratio == pytest.approx(0.4)
        assert by_name["docs__beta"].stale_source_ratio is None

    def test_empty_telemetry_sets_placeholders(self) -> None:
        from nexus.collection_health import compute_collection_health

        rows = compute_collection_health(
            ["docs__beta"],
            catalog_stats_fn=_fake_catalog_stats,
            telemetry_stats_fn=_fake_telemetry_stats,
            projection_rank_fn=_fake_projection_ranks,
            hub_score_fn=_fake_hub_score,
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
        )
        assert rows[0].hub_domination_score is None


# ── chunk_count sourcing (nexus-39zi) ──────────────────────────────────────


class TestChunkCountFromT3:
    """Chunk count must come from T3's live ``coll.count()``, not the
    catalog's ``SUM(chunk_count)`` column. The catalog drifts when a
    write path skips catalog registration (direct ``store_put``,
    cloud-side operations, pre-catalog tenants) — reporting catalog-
    sourced counts puts ``nx collection health`` out of sync with
    ``nx collection list``.
    """

    def test_chunk_count_fn_wins_over_catalog(self) -> None:
        """When both sources are present, ``chunk_count_fn`` takes
        precedence. Ground truth is T3, not the catalog cache."""
        from nexus.collection_health import compute_collection_health

        # Catalog reports 120; T3 reports 500. T3 must win.
        def _t3_chunk_count(col: str) -> int:
            return {"code__alpha": 500}.get(col, 0)

        rows = compute_collection_health(
            ["code__alpha"],
            catalog_stats_fn=_fake_catalog_stats,  # returns chunk_count=120
            telemetry_stats_fn=_fake_telemetry_stats,
            projection_rank_fn=_fake_projection_ranks,
            hub_score_fn=_fake_hub_score,
            chunk_count_fn=_t3_chunk_count,
        )
        assert rows[0].chunk_count == 500, (
            f"chunk_count_fn must override catalog-sourced count; "
            f"got {rows[0].chunk_count}"
        )

    def test_drift_case_catalog_says_zero_t3_says_positive(self) -> None:
        """The exact regression from 2026-04-18 live shakeout: catalog
        reports 0 for most production collections while T3 has real
        counts. Health must surface the real T3 count so the report
        cannot silently disagree with ``nx collection list``."""
        from nexus.collection_health import compute_collection_health

        def _catalog_zero(col: str) -> dict:
            return {"last_indexed": "2026-04-15", "orphan_count": 0}

        def _t3_has_chunks(col: str) -> int:
            return 63077  # like code__ART-8c2e74c0 on the live probe

        rows = compute_collection_health(
            ["code__ART-8c2e74c0"],
            catalog_stats_fn=_catalog_zero,
            telemetry_stats_fn=_fake_telemetry_stats,
            projection_rank_fn=_fake_projection_ranks,
            hub_score_fn=_fake_hub_score,
            chunk_count_fn=_t3_has_chunks,
        )
        assert rows[0].chunk_count == 63077

    def test_fallback_to_catalog_when_fn_not_injected(self) -> None:
        """Backward-compat: callers without ``chunk_count_fn`` still
        read from catalog stats. This preserves the pre-39zi signature
        for legacy test fixtures while production paths plumb T3."""
        from nexus.collection_health import compute_collection_health

        rows = compute_collection_health(
            ["code__alpha"],
            catalog_stats_fn=_fake_catalog_stats,
            telemetry_stats_fn=_fake_telemetry_stats,
            projection_rank_fn=_fake_projection_ranks,
            hub_score_fn=_fake_hub_score,
            # no chunk_count_fn — fall through to catalog
        )
        # _fake_catalog_stats returns chunk_count=120 for code__alpha
        assert rows[0].chunk_count == 120

    def test_default_catalog_stats_no_longer_returns_chunk_count(self) -> None:
        """The production ``_default_catalog_stats_fn`` dropped the
        ``chunk_count`` key after 39zi. Production catalog paths now
        return only ``last_indexed`` and ``orphan_count``; chunk count
        comes from the T3-sourced ``_default_chunk_count_fn``."""
        from nexus.collection_health import _default_catalog_stats_fn

        stats = _default_catalog_stats_fn("does-not-matter")
        assert "chunk_count" not in stats, (
            "catalog stats must not surface chunk_count any more — T3 is "
            "the source of truth, catalog drifted to 0 on prod (nexus-39zi)"
        )


# ── chash_indexed_ratio deletion (nexus-70vpz / RDR-187) ───────────────────


class TestChashIndexedRatioRemoved:
    """``chash_indexed_ratio`` was deleted outright, not repointed.

    RDR-187 dropped ``nexus.chash_index``; the surviving read path
    (``HttpChashIndex.count_for_collection``) serves from the same
    chunks tables the T3 chunk count itself reads, so the ratio's
    numerator and denominator were always identical — a health signal
    that could never signal (reads 1.000 on every collection,
    including zombies). Pinned here so the field cannot silently
    return without a replacement invariant behind it.
    """

    def test_row_has_no_chash_indexed_ratio_field(self) -> None:
        import dataclasses

        from nexus.collection_health import CollectionHealthRow

        field_names = {f.name for f in dataclasses.fields(CollectionHealthRow)}
        assert "chash_indexed_ratio" not in field_names

    def test_compute_collection_health_rejects_chash_coverage_fn(self) -> None:
        from nexus.collection_health import compute_collection_health

        with pytest.raises(TypeError):
            compute_collection_health(
                ["code__alpha"],
                catalog_stats_fn=_fake_catalog_stats,
                telemetry_stats_fn=_fake_telemetry_stats,
                projection_rank_fn=_fake_projection_ranks,
                hub_score_fn=_fake_hub_score,
                chash_coverage_fn=lambda _col: 1.0,
            )

    def test_json_output_omits_chash_indexed_ratio(self) -> None:
        from nexus.collection_health import (
            compute_collection_health, format_health_json,
        )

        rows = compute_collection_health(
            ["code__alpha"],
            catalog_stats_fn=_fake_catalog_stats,
            telemetry_stats_fn=_fake_telemetry_stats,
            projection_rank_fn=_fake_projection_ranks,
            hub_score_fn=_fake_hub_score,
        )
        payload = json.loads(format_health_json(rows))
        assert "chash_indexed_ratio" not in payload["collections"][0]

    def test_table_output_omits_chash_indexed_ratio_and_backfill_hint(self) -> None:
        from nexus.collection_health import (
            compute_collection_health, format_health_table,
        )

        rows = compute_collection_health(
            ["code__alpha", "docs__beta", "docs__stale"],
            catalog_stats_fn=_fake_catalog_stats,
            telemetry_stats_fn=_fake_telemetry_stats,
            projection_rank_fn=_fake_projection_ranks,
            hub_score_fn=_fake_hub_score,
        )
        out = format_health_table(rows, sort_by="name")
        assert "chash_indexed_ratio" not in out
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
        payload = json.loads(result.stdout)
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
