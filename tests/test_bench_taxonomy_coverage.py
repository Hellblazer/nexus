# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-134 coverage baseline for the collections the spike routed (nexus-7fku2.1.7;
``scripts/bench/taxonomy_coverage.py``): the page loop over ``next_after``, the coverage
arithmetic, the gate decision, the rebuild check against the saved spike output and the
rendering. Every case runs on a stub client; nothing here reaches an engine."""
from __future__ import annotations

import contextlib
import json
from pathlib import Path

import httpx
import pytest

from nexus.db.http_vector_client import VectorServiceError
from bench.taxonomy_coverage import (
    GATE_THRESHOLD,
    GATE_UNRESOLVED,
    Env,
    EngineRefused,
    main,
    partition,
    pin_engine,
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


def _row(name: str, chunks: int, unassigned: int | None, *, status: str = "ok", has_tax: bool | None = True,
         uncertain: bool = False, upper: float | None = None) -> dict:
    cov = coverage_of(chunks, unassigned or 0) if status == "ok" else None
    return {"gate_uncertain": uncertain, "collection": name, "live_chunks": chunks, "unassigned": unassigned,
            "has_taxonomy": has_tax, "coverage": cov, "coverage_upper": upper,
            "above_0_9": cov is not None and cov >= 0.9, "status": status, "dims": [768]}


class TestPartitionAndReport:
    rows = [
        _row("a", 100, 0),                                   # routed
        _row("a2", 100, 10),                                 # routed at exactly 0.9
        _row("b", 100, 50),                                  # definite miss
        _row("u", 100, 20, uncertain=True, upper=0.95),      # uncertain
        _row("c", 0, None, status="no_chunks", has_tax=None),
        _row("d", 10, None, status="no_taxonomy", has_tax=False),
        _row("e", 10, None, status="error", has_tax=None),
    ]

    def test_the_gate_keeps_only_collections_at_or_above_the_threshold(self) -> None:
        assert routed_by_gate(self.rows) == ["a", "a2"]

    def test_the_four_parts_add_up_to_the_candidates(self) -> None:
        part = partition(self.rows, spike_routed=36)
        assert (part["candidates"], part["routed_by_gate"], part["definite_miss"],
                part["uncertain"], part["not_measurable"]) == (7, 2, 1, 1, 3)
        assert part["routed_by_gate"] + part["definite_miss"] + part["uncertain"] + part["not_measurable"] == 7
        assert part["not_measurable_by_status"] == {"no_chunks": 1, "no_taxonomy": 1, "error": 1}

    def test_the_partition_line_and_the_range_are_stated(self) -> None:
        part = partition(self.rows, spike_routed=36)
        assert part["line"] == (
            "7 candidates: 2 routed by the gate; 1 definite coverage misses; 1 uncertain; "
            "3 not measurable or never routable (error 1, no_chunks 1, no_taxonomy 1)")
        assert (part["unrouted_low"], part["unrouted_high"]) == (1, 2)
        assert part["with_taxonomy"] == 4
        text = part["statement"]
        assert "Of 7 candidates (36 routed by the spike)" in text
        assert "would leave 1 to 2 of the spike-routed collections unrouted" in text
        assert GATE_UNRESOLVED in text
        assert "has_taxonomy was not read for no_chunks, multi_dim and error rows" in text

    def test_an_unknown_spike_count_is_not_invented(self) -> None:
        assert "(? routed by the spike)" in partition(self.rows)["statement"]

    def test_the_report_carries_the_partition_the_engine_and_the_host(self) -> None:
        report = build_report(self.rows, engine="release 0.1.194", date="2026-11-17",
                              rebuild={"drift": False, "spike_per_question_routed": 36},
                              host="engine.example")
        assert report["engine"] == "release 0.1.194"
        assert report["base_url_host"] == "engine.example"
        assert report["complete"] is True
        assert report["measured"] == 7
        assert report["errors"] == 1
        assert report["above_0_9"] == 2
        assert report["not_routed_by_gate"] == 5
        assert report["coverage_below_0_9"] == 1   # the definite miss only: the uncertain row is not counted
        assert report["gate_uncertain_names"] == ["u"]
        assert report["partition_line"] == report["partition"]["line"]
        assert "ghost" in report["bound_note"]
        assert report["threshold"] == 0.9
        json.dumps(report)  # serialisable

    def test_the_table_has_a_status_column_four_decimals_and_the_uncertain_marker(self) -> None:
        lines = render_table(self.rows).splitlines()
        assert lines[0] == "| collection | status | live chunks | unassigned | coverage | >= 0.9 |"
        assert len(lines) == 2 + len(self.rows)
        assert "| a | ok | 100 | 0 | 1.0000 | yes |" in lines
        assert "| a2 | ok | 100 | 10 | 0.9000 | yes |" in lines
        assert "| b | ok | 100 | 50 | 0.5000 | no |" in lines
        assert "| u | ok | 100 | 20 | 0.8000 | uncertain (upper 0.9500) |" in lines
        assert "| c | no_chunks | 0 | - | - | no |" in lines
        assert "| e | error | 10 | - | - | no |" in lines

    def test_a_coverage_just_under_the_gate_is_not_shown_as_the_gate(self) -> None:
        line = render_table([_row("x", 10000, 1001)]).splitlines()[2]
        assert "0.8999 | no |" in line

    def test_an_uncertain_row_is_marked_unresolved(self) -> None:
        rows = [{"name": "docs__c", "dim": 768, "count": 100, "stored_count": 115}]
        row = measure_collection("docs__c", chunk_rows=rows, taxonomy=_StubTaxonomy(20))
        assert row["gate_uncertain"] is True
        assert row["note"] == GATE_UNRESOLVED


class TestErrorRows:
    ROWS = [{"name": "docs__c", "dim": 768, "count": 10}]

    @pytest.mark.parametrize("status", [500, 503, 429])
    def test_a_server_error_is_recorded_and_not_raised(self, status: int) -> None:
        row = measure_collection("docs__c", chunk_rows=self.ROWS, taxonomy=_RaisingTaxonomy(status))
        assert row["status"] == "error"
        assert str(status) in row["error"]
        assert row["coverage"] is None and row["above_0_9"] is False
        assert row["live_chunks"] == 10

    def test_a_vector_service_error_is_recorded(self) -> None:
        class _Boom:
            def unassigned_chashes(self, collection, *, limit=1000, after=None):
                raise VectorServiceError("statement timeout")

        row = measure_collection("docs__c", chunk_rows=self.ROWS, taxonomy=_Boom())
        assert row["status"] == "error"
        assert "statement timeout" in row["error"]

    def test_a_transport_failure_is_recorded(self) -> None:
        class _Down:
            def unassigned_chashes(self, collection, *, limit=1000, after=None):
                raise httpx.ConnectError("connection refused")

        assert measure_collection("docs__c", chunk_rows=self.ROWS, taxonomy=_Down())["status"] == "error"

    def test_a_422_after_the_first_page_is_an_error_not_unregistered(self) -> None:
        class _Vanishes:
            def __init__(self) -> None:
                self.calls = 0

            def unassigned_chashes(self, collection, *, limit=1000, after=None):
                self.calls += 1
                if self.calls == 1:
                    return {"chashes": [_chash(i) for i in range(limit)], "has_taxonomy": True,
                            "next_after": _chash(limit - 1)}
                request = httpx.Request("POST", "http://engine/x")
                raise httpx.HTTPStatusError("gone", request=request,
                                            response=httpx.Response(422, request=request))

        row = measure_collection("docs__c", chunk_rows=[{"name": "docs__c", "dim": 768, "count": 5000}],
                                 taxonomy=_Vanishes())
        assert row["status"] == "error"

    def test_the_old_engine_error_is_still_fatal(self) -> None:
        with pytest.raises(EngineTooOld):
            measure_collection("docs__c", chunk_rows=self.ROWS, taxonomy=_RaisingTaxonomy(404))


class _StubVectors:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def collection_stats(self) -> list[dict]:
        return self.rows


class _MainTaxonomy(_StubTaxonomy):
    """Per-collection unassigned counts, and the collections that have topics."""

    def __init__(self, unassigned: dict[str, int | int], topics: list[str], failing: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        super().__init__(0)
        self.per = unassigned
        self.topics = topics
        self.failing = failing

    def get_distinct_collections(self) -> list[str]:
        return self.topics

    def unassigned_chashes(self, collection: str, *, limit: int = 1000, after: str | None = None):
        if collection in self.failing:
            request = httpx.Request("POST", "https://secret-engine.example/v1/taxonomy/assignments/unassigned")
            raise httpx.HTTPStatusError("Server error '500' for url 'https://secret-engine.example/v1/x'",
                                        request=request, response=httpx.Response(500, request=request))
        n = self.per[collection]
        return {"chashes": [_chash(i) for i in range(n)][:limit], "has_taxonomy": True, "next_after": None}


def _env(*, local: bool = False, base_url: str | None = "https://secret-engine.example/api",
         version: tuple[int, int, int] | None = (0, 1, 194), prose: list[str] | None = None,
         tax: _MainTaxonomy | None = None, stats: list[dict] | None = None, log: list[str] | None = None) -> Env:
    log = log if log is not None else []

    def _url() -> str:
        log.append("url")
        if base_url is None:
            raise RuntimeError("no endpoint")
        return base_url

    @contextlib.contextmanager
    def _open():
        log.append("taxonomy")
        yield tax

    def _vectors():
        log.append("vectors")
        return _StubVectors(stats or [])

    return Env(
        is_local_mode=lambda: local,
        service_base_url=_url,
        engine_version=lambda: (log.append("version"), version)[1],
        vector_client=_vectors,
        open_taxonomy=_open,
        prose_collections=lambda: (log.append("prose"), prose or [])[1],
    )


def _saved(tmp_path: Path, n: int, prod: list[str]) -> Path:
    path = tmp_path / "saved.json"
    path.write_text(json.dumps({"summary": {"routed_collections": n, "fallback": {"routed": n - 1}}, "prod": prod}))
    return path


def _run(tmp_path: Path, env: Env, saved: Path, *extra: str) -> tuple[int, Path]:
    out = tmp_path / "out.json"
    code = main(["--saved", str(saved), "--out", str(out), "--date", "2026-11-17", *extra], env=env)
    return code, out


class TestMainGuard:
    def test_local_mode_is_refused_before_any_read(self, tmp_path: Path, capsys) -> None:
        log: list[str] = []
        code, out = _run(tmp_path, _env(local=True, log=log), _saved(tmp_path, 1, ["a"]))
        assert code == 3
        assert log == []  # no endpoint resolved, no version read, no client built
        assert not out.exists()
        assert "local mode" in capsys.readouterr().err

    def test_an_unresolvable_service_url_is_refused(self, tmp_path: Path, capsys) -> None:
        log: list[str] = []
        code, out = _run(tmp_path, _env(base_url=None, log=log), _saved(tmp_path, 1, ["a"]))
        assert code == 3
        assert "vectors" not in log and "taxonomy" not in log
        assert not out.exists()
        assert "no service URL" in capsys.readouterr().err

    def test_an_engine_below_the_unassigned_route_is_refused(self, tmp_path: Path) -> None:
        code, _ = _run(tmp_path, _env(version=(0, 1, 131)), _saved(tmp_path, 1, ["a"]))
        assert code == 3

    def test_allow_local_lets_a_rehearsal_through(self, tmp_path: Path) -> None:
        coll = "docs__a__m__v1"
        tax = _MainTaxonomy({coll: 0}, [coll])
        env = _env(local=True, prose=[coll], tax=tax, stats=[{"name": coll, "dim": 768, "count": 4}])
        code, out = _run(tmp_path, env, _saved(tmp_path, 1, [coll]), "--allow-local")
        assert code == 0
        assert json.loads(out.read_text())["measured"] == 1

    def test_the_engine_is_read_first_and_recorded_with_a_scrubbed_host(self, tmp_path: Path, capsys) -> None:
        coll = "docs__a__m__v1"
        log: list[str] = []
        tax = _MainTaxonomy({coll: 1}, [coll])
        env = _env(prose=[coll], tax=tax, stats=[{"name": coll, "dim": 768, "count": 4}], log=log)
        code, out = _run(tmp_path, env, _saved(tmp_path, 1, [coll]))
        assert code == 0
        assert log.index("version") < log.index("vectors") < log.index("taxonomy")
        report = json.loads(out.read_text())
        assert report["engine"] == "release 0.1.194"
        assert report["base_url_host"] == "secret-engine.example"
        assert "/api" not in json.dumps(report)  # the host only, no path
        assert "engine: release 0.1.194 at secret-engine.example" in capsys.readouterr().err


class TestMainRun:
    COLLS = ["docs__a__m__v1", "docs__b__m__v1", "docs__c__m__v1"]

    def _stats(self) -> list[dict]:
        return [{"name": c, "dim": 768, "count": 100} for c in self.COLLS]

    def test_one_collection_erroring_keeps_the_others_and_the_exit_is_non_zero(self, tmp_path: Path, capsys) -> None:
        a, b, c = self.COLLS
        tax = _MainTaxonomy({a: 0, b: 50, c: 0}, self.COLLS, failing={b})
        env = _env(prose=self.COLLS, tax=tax, stats=self._stats())
        code, out = _run(tmp_path, env, _saved(tmp_path, 3, self.COLLS))
        assert code == 1
        report = json.loads(out.read_text())
        rows = {r["collection"]: r for r in report["collections"]}
        assert rows[b]["status"] == "error"
        assert "secret-engine.example" not in rows[b]["error"]  # the base URL is scrubbed from the message
        assert rows[a]["above_0_9"] is True and rows[c]["above_0_9"] is True
        assert report["errors"] == 1 and report["complete"] is True
        assert report["partition"]["not_measurable_by_status"] == {"error": 1}
        assert "errored" in capsys.readouterr().err

    def test_a_fatal_error_still_writes_what_was_measured(self, tmp_path: Path) -> None:
        a, b, _ = self.COLLS

        class _OldEngine(_MainTaxonomy):
            def unassigned_chashes(self, collection, *, limit=1000, after=None):
                if collection == b:
                    request = httpx.Request("POST", "https://x/y")
                    raise httpx.HTTPStatusError("nope", request=request,
                                                response=httpx.Response(404, request=request))
                return super().unassigned_chashes(collection, limit=limit, after=after)

        tax = _OldEngine({a: 0}, self.COLLS)
        env = _env(prose=self.COLLS, tax=tax, stats=self._stats())
        out = tmp_path / "out.json"
        with pytest.raises(EngineTooOld):
            main(["--saved", str(_saved(tmp_path, 3, self.COLLS)), "--out", str(out), "--date", "2026-11-17"], env=env)
        report = json.loads(out.read_text())
        assert report["complete"] is False
        assert [r["collection"] for r in report["collections"]] == [a]

    def test_an_empty_rebuilt_set_exits_non_zero(self, tmp_path: Path, capsys) -> None:
        tax = _MainTaxonomy({}, [])  # nothing has topics
        env = _env(prose=self.COLLS, tax=tax, stats=self._stats())
        code, out = _run(tmp_path, env, _saved(tmp_path, 3, self.COLLS))
        assert code == 1
        assert json.loads(out.read_text())["measured"] == 0
        assert "empty" in capsys.readouterr().err

    def test_the_partition_and_the_range_are_printed_and_stored(self, tmp_path: Path, capsys) -> None:
        a, b, c = self.COLLS
        tax = _MainTaxonomy({a: 0, b: 50, c: 20}, self.COLLS)
        stats = [{"name": a, "dim": 768, "count": 100}, {"name": b, "dim": 768, "count": 100},
                 {"name": c, "dim": 768, "count": 100, "stored_count": 115}]
        env = _env(prose=self.COLLS, tax=tax, stats=stats)
        code, out = _run(tmp_path, env, _saved(tmp_path, 3, self.COLLS))
        assert code == 0
        report = json.loads(out.read_text())
        assert report["partition_line"].startswith("3 candidates: 1 routed by the gate; 1 definite coverage misses; 1 uncertain; 0 not measurable")
        shown = capsys.readouterr().out
        assert report["partition_line"] in shown
        assert "Of 3 candidates (2 routed by the spike)" in shown
        assert "would leave 1 to 2" in shown
        assert "uncertain (upper 0.9500)" in shown

    def test_drift_still_exits_non_zero_only_under_strict(self, tmp_path: Path) -> None:
        a = self.COLLS[0]
        tax = _MainTaxonomy({a: 0}, [a])
        env = _env(prose=[a], tax=tax, stats=self._stats())
        saved = _saved(tmp_path, 3, self.COLLS)
        assert _run(tmp_path, env, saved)[0] == 0
        assert _run(tmp_path, env, saved, "--strict")[0] == 2


def test_the_pin_returns_the_build_and_host() -> None:
    pin = pin_engine(_env(version=None), allow_local=False)
    assert (pin.build, pin.host) == ("unstamped", "secret-engine.example")
    with pytest.raises(EngineRefused):
        pin_engine(_env(local=True), allow_local=False)
