# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-134 coverage baseline for the collections the spike routed (nexus-7fku2.1.7;
``scripts/bench/taxonomy_coverage.py``): the page loop over ``next_after``, the coverage
arithmetic, the gate decision, the rebuild check against the saved spike output and the
rendering. Every case runs on a stub client; nothing here reaches an engine."""
from __future__ import annotations

import json

import httpx
import pytest

from bench.taxonomy_coverage import (
    GATE_THRESHOLD,
    PAGE_SIZE,
    EngineTooOld,
    count_unassigned,
    coverage_of,
    live_chunks,
    measure_collection,
    non_live_chunks,
    rebuild_check,
    render_table,
    build_report,
    routed_by_gate,
)


def _chash(i: int) -> str:
    return f"{i:064x}"


class _StubTaxonomy:
    """Serves ``total`` unassigned chashes in keyset order, the way the engine does:
    ``next_after`` is set only when a page comes back full."""

    def __init__(self, total: int, *, has_taxonomy: bool = True) -> None:
        self.hashes = [_chash(i) for i in range(total)]
        self.has_taxonomy = has_taxonomy
        self.calls: list[tuple[str, int, str | None]] = []

    def unassigned_chashes(self, collection: str, *, limit: int = 1000, after: str | None = None):
        self.calls.append((collection, limit, after))
        if not self.has_taxonomy:
            return {"chashes": [], "has_taxonomy": False, "next_after": None}
        start = 0 if after is None else self.hashes.index(after) + 1
        page = self.hashes[start:start + limit]
        nxt = page[-1] if len(page) >= limit else None
        return {"chashes": page, "has_taxonomy": True, "next_after": nxt}


class _RaisingTaxonomy:
    def __init__(self, status: int) -> None:
        self.status = status

    def unassigned_chashes(self, collection: str, *, limit: int = 1000, after: str | None = None):
        request = httpx.Request("POST", "http://engine/v1/taxonomy/assignments/unassigned")
        raise httpx.HTTPStatusError(
            "boom", request=request, response=httpx.Response(self.status, request=request))


class TestPaging:
    def test_pages_of_1000_are_followed_by_next_after_until_it_is_none(self) -> None:
        tax = _StubTaxonomy(2037)
        result = count_unassigned(tax, "docs__c")
        assert result.unassigned == 2037
        assert result.has_taxonomy is True
        assert result.pages == 3
        assert [(limit, after) for _, limit, after in tax.calls] == [
            (1000, None), (1000, _chash(999)), (1000, _chash(1999))]
        assert PAGE_SIZE == 1000

    def test_a_count_that_is_an_exact_multiple_ends_on_an_empty_page(self) -> None:
        tax = _StubTaxonomy(2000)
        result = count_unassigned(tax, "docs__c")
        assert result.unassigned == 2000
        assert result.pages == 3  # 1000, 1000, then the empty third page

    def test_a_fully_assigned_collection_is_one_empty_page(self) -> None:
        tax = _StubTaxonomy(0)
        result = count_unassigned(tax, "docs__c")
        assert (result.unassigned, result.has_taxonomy, result.pages) == (0, True, 1)

    def test_a_collection_with_no_centroids_is_recorded_as_no_taxonomy(self) -> None:
        result = count_unassigned(_StubTaxonomy(0, has_taxonomy=False), "docs__c")
        assert result.has_taxonomy is False
        assert result.unassigned == 0

    def test_a_422_reads_as_a_collection_the_engine_does_not_know(self) -> None:
        result = count_unassigned(_RaisingTaxonomy(422), "docs__c")
        assert result.registered is False
        assert result.unassigned == 0

    def test_a_404_means_the_engine_is_too_old_and_propagates(self) -> None:
        with pytest.raises(EngineTooOld):
            count_unassigned(_RaisingTaxonomy(404), "docs__c")

    def test_another_status_propagates_unchanged(self) -> None:
        with pytest.raises(httpx.HTTPStatusError):
            count_unassigned(_RaisingTaxonomy(500), "docs__c")

    def test_a_cursor_that_does_not_advance_is_refused(self) -> None:
        class _Stuck:
            def unassigned_chashes(self, collection, *, limit=1000, after=None):
                return {"chashes": [_chash(1)], "has_taxonomy": True, "next_after": _chash(1)}

        with pytest.raises(RuntimeError, match="advance"):
            count_unassigned(_Stuck(), "docs__c")


class TestCoverageArithmetic:
    def test_coverage_is_one_minus_unassigned_over_chunks(self) -> None:
        assert coverage_of(1000, 0) == 1.0
        assert coverage_of(1000, 500) == 0.5
        assert coverage_of(4, 1) == 0.75

    def test_no_chunks_has_no_coverage_and_does_not_divide(self) -> None:
        assert coverage_of(0, 0) is None

    def test_exactly_the_threshold_is_routed(self) -> None:
        assert GATE_THRESHOLD == 0.9
        row = measure_collection("docs__c", chunk_rows=[{"name": "docs__c", "dim": 768, "count": 1000}],
                                 taxonomy=_StubTaxonomy(100))
        assert row["coverage"] == 0.9
        assert row["above_0_9"] is True

    def test_just_under_the_threshold_is_not_routed(self) -> None:
        row = measure_collection("docs__c", chunk_rows=[{"name": "docs__c", "dim": 768, "count": 1000}],
                                 taxonomy=_StubTaxonomy(101))
        assert row["coverage"] == pytest.approx(0.899)
        assert row["above_0_9"] is False


class TestLiveChunks:
    def test_the_denominator_is_the_stats_count_of_the_one_dimension(self) -> None:
        rows = [{"name": "a", "dim": 768, "count": 12, "stored_count": 20},
                {"name": "b", "dim": 384, "count": 99}]
        assert live_chunks(rows, "a") == (12, [768])

    def test_a_collection_absent_from_the_stats_has_zero_chunks(self) -> None:
        assert live_chunks([{"name": "b", "dim": 384, "count": 99}], "a") == (0, [])

    def test_a_null_count_reads_as_zero(self) -> None:
        assert live_chunks([{"name": "a", "dim": 768, "count": None}], "a") == (0, [768])


class TestMeasureCollection:
    def test_a_half_assigned_collection_reads_below_the_gate(self) -> None:
        row = measure_collection("docs__c", chunk_rows=[{"name": "docs__c", "dim": 768, "count": 2000}],
                                 taxonomy=_StubTaxonomy(1000))
        assert row["live_chunks"] == 2000
        assert row["unassigned"] == 1000
        assert row["coverage"] == 0.5
        assert row["above_0_9"] is False
        assert row["status"] == "ok"

    def test_a_fully_assigned_collection_is_routed(self) -> None:
        row = measure_collection("docs__c", chunk_rows=[{"name": "docs__c", "dim": 768, "count": 40}],
                                 taxonomy=_StubTaxonomy(0))
        assert row["coverage"] == 1.0
        assert row["above_0_9"] is True

    def test_zero_chunks_is_never_routed_and_reads_no_page(self) -> None:
        tax = _StubTaxonomy(0)
        row = measure_collection("docs__c", chunk_rows=[{"name": "docs__c", "dim": 768, "count": 0}],
                                 taxonomy=tax)
        assert row["live_chunks"] == 0
        assert row["coverage"] is None
        assert row["above_0_9"] is False
        assert row["status"] == "no_chunks"
        assert tax.calls == []

    def test_a_422_with_a_listed_count_is_unregistered_and_not_routed(self) -> None:
        row = measure_collection("docs__c", chunk_rows=[{"name": "docs__c", "dim": 768, "count": 10}],
                                 taxonomy=_RaisingTaxonomy(422))
        assert row["coverage"] is None
        assert row["above_0_9"] is False
        assert row["status"] == "unregistered"

    def test_no_taxonomy_is_recorded_and_not_routed(self) -> None:
        row = measure_collection("docs__c", chunk_rows=[{"name": "docs__c", "dim": 768, "count": 10}],
                                 taxonomy=_StubTaxonomy(0, has_taxonomy=False))
        assert row["has_taxonomy"] is False
        assert row["coverage"] is None
        assert row["above_0_9"] is False
        assert row["status"] == "no_taxonomy"

    def test_two_dimensions_have_no_single_denominator_and_are_not_routed(self) -> None:
        rows = [{"name": "docs__c", "dim": 384, "count": 5}, {"name": "docs__c", "dim": 768, "count": 5}]
        tax = _StubTaxonomy(0)
        row = measure_collection("docs__c", chunk_rows=rows, taxonomy=tax)
        assert row["status"] == "multi_dim"
        assert row["coverage"] is None
        assert row["above_0_9"] is False
        assert row["dims"] == [384, 768]
        assert tax.calls == []

    def test_more_unassigned_than_live_is_flagged_not_clamped(self) -> None:
        """The unassigned read counts a chunk whose only owner is a tombstoned document;
        the live count does not. The row says so instead of hiding it."""
        row = measure_collection("docs__c", chunk_rows=[{"name": "docs__c", "dim": 768, "count": 3}],
                                 taxonomy=_StubTaxonomy(5))
        assert row["anomaly"] == "unassigned_exceeds_live"
        assert row["coverage"] == pytest.approx(1 - 5 / 3)
        assert row["above_0_9"] is False


class TestNonLiveBound:
    def test_non_live_is_stored_less_live(self) -> None:
        assert non_live_chunks([{"name": "a", "dim": 768, "count": 12, "stored_count": 20}], "a") == 8

    def test_an_engine_without_stored_count_has_none(self) -> None:
        assert non_live_chunks([{"name": "a", "dim": 768, "count": 12}], "a") == 0

    def test_a_listing_inflated_by_non_live_chunks_is_uncertain_not_a_plain_no(self) -> None:
        """100 live, 20 listed unassigned, 15 stored without a live owner: the true coverage is
        between 0.8 and 0.95, so the 0.9 gate cannot be called from these counts."""
        rows = [{"name": "docs__c", "dim": 768, "count": 100, "stored_count": 115}]
        row = measure_collection("docs__c", chunk_rows=rows, taxonomy=_StubTaxonomy(20))
        assert row["coverage"] == 0.8
        assert row["above_0_9"] is False
        assert row["coverage_upper"] == 0.95
        assert row["gate_uncertain"] is True

    def test_a_clear_miss_is_not_uncertain(self) -> None:
        rows = [{"name": "docs__c", "dim": 768, "count": 100, "stored_count": 105}]
        row = measure_collection("docs__c", chunk_rows=rows, taxonomy=_StubTaxonomy(50))
        assert row["gate_uncertain"] is False

    def test_a_pass_is_never_uncertain(self) -> None:
        rows = [{"name": "docs__c", "dim": 768, "count": 100, "stored_count": 110}]
        row = measure_collection("docs__c", chunk_rows=rows, taxonomy=_StubTaxonomy(5))
        assert row["above_0_9"] is True
        assert row["gate_uncertain"] is False


class TestRebuildCheck:
    saved = {"summary": {"routed_collections": 3, "fallback": {"routed": 2}},
             "prod": ["a", "b", "c", "d"]}

    def test_a_matching_rebuild_records_no_drift(self) -> None:
        check = rebuild_check(["a", "b", "c"], self.saved)
        assert check["drift"] is False
        assert check["expected_set_size"] == 3
        assert check["rebuilt_set_size"] == 3
        assert check["spike_per_question_routed"] == 2
        assert check["not_in_saved_prod"] == []

    def test_a_different_size_is_recorded_as_drift(self) -> None:
        check = rebuild_check(["a", "b"], self.saved)
        assert check["drift"] is True
        assert check["rebuilt_set_size"] == 2

    def test_a_collection_the_spike_never_saw_is_recorded_as_drift(self) -> None:
        check = rebuild_check(["a", "b", "z"], self.saved)
        assert check["drift"] is True
        assert check["not_in_saved_prod"] == ["z"]

    def test_an_expected_size_can_be_given_instead_of_the_saved_summary(self) -> None:
        check = rebuild_check(["a", "b"], self.saved, expected_size=2)
        assert check["expected_set_size"] == 2
        assert check["drift"] is False


def _row(name: str, chunks: int, unassigned: int | None, *, status: str = "ok", has_tax: bool = True) -> dict:
    cov = coverage_of(chunks, unassigned or 0) if status == "ok" else None
    return {"gate_uncertain": False, "collection": name, "live_chunks": chunks, "unassigned": unassigned, "has_taxonomy": has_tax,
            "coverage": cov, "above_0_9": cov is not None and cov >= 0.9, "status": status, "dims": [768]}


class TestReport:
    rows = [_row("a", 100, 0), _row("b", 100, 50), _row("c", 0, None, status="no_chunks"),
            _row("d", 10, None, status="no_taxonomy", has_tax=False)]

    def test_the_gate_keeps_only_collections_at_or_above_the_threshold(self) -> None:
        assert routed_by_gate(self.rows) == ["a"]

    def test_the_report_counts_below_the_gate(self) -> None:
        report = build_report(self.rows, engine="release 0.1.194", date="2026-11-17",
                              rebuild={"drift": False})
        assert report["engine"] == "release 0.1.194"
        assert report["measured"] == 4
        assert report["above_0_9"] == 1
        assert report["not_routed_by_gate"] == 3
        assert report["coverage_below_0_9"] == 1
        assert report["gate_uncertain_names"] == []
        assert report["not_routed_by_gate_names"] == ["b", "c", "d"]
        assert report["threshold"] == 0.9
        json.dumps(report)  # serialisable

    def test_the_table_has_the_five_columns_and_a_row_each(self) -> None:
        table = render_table(self.rows)
        lines = table.splitlines()
        assert lines[0] == "| collection | live chunks | unassigned | coverage | >= 0.9 |"
        assert len(lines) == 2 + len(self.rows)
        assert "| a | 100 | 0 | 1.000 | yes |" in lines
        assert "| b | 100 | 50 | 0.500 | no |" in lines
        assert "| c | 0 | - | - | no |" in lines
        assert "| d | 10 | - | - | no |" in lines
