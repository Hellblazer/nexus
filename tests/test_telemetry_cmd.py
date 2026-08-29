# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx telemetry baseline`` — playbook §4.5 fixed-shape telemetry baseline
(nexus-v0x32), fix round 1.

Structural tests only: every reader this command composes (nx_answer_runs,
tier_writes, relevance_log/relevance_stats, search_telemetry's collection
enumeration + per-collection stats, the drop meter, the catalog substrate
check) is faked in-memory here. The one genuine engine round trip for the
brand-new ``relevance/stats`` route lives in
``tests/db/test_http_telemetry_store.py::TestGetRelevanceStatsAgainstRealEngine``.

Fix round 1 changes covered here: nx_answer_runs' oldest/newest
created_at, per-figure ``window`` keys (no top-level ``since`` any more),
search_telemetry's unconditional LOWER BOUND caveat + per-collection
``zero_hit_rate``, and the substrate_check/relevance_log relabeling.
"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner


def _nar_result(
    total=0, hit=0, fallback=0, buckets=None,
    oldest_created_at="", newest_created_at=None,
):
    return {
        "total": total,
        "hit_count": hit,
        "fallback_count": fallback,
        "latency_buckets": buckets or {
            "under_5s": 0, "5s_to_30s": 0, "30s_to_2min": 0,
            "2min_to_5min": 0, "over_5min": 0,
        },
        "oldest_created_at": oldest_created_at,
        # query_nx_answer_runs(limit=1) always returns at most 1 row,
        # newest first -- rows[0] IS the newest_created_at source.
        "rows": [{"created_at": newest_created_at}] if newest_created_at else [],
    }


class _FakeStore:
    """Records-fake for every HttpTelemetryStore method the command calls."""

    def __init__(
        self,
        *,
        nar_since=None,
        nar_all_time=None,
        tier_rows=None,
        relevance_stats=None,
        retention_markers=None,
        collection_stats=None,
        raise_nar: Exception | None = None,
        raise_tier: Exception | None = None,
        raise_relevance: Exception | None = None,
        raise_retention: Exception | None = None,
        raise_collection_names: tuple[str, ...] = (),
    ) -> None:
        self.nar_since = nar_since if nar_since is not None else _nar_result()
        self.nar_all_time = nar_all_time if nar_all_time is not None else self.nar_since
        self.tier_rows = tier_rows or []
        self.relevance_stats = (
            relevance_stats if relevance_stats is not None
            else {"count": 0, "oldest": None, "newest": None}
        )
        self.retention_markers = retention_markers or {}
        self.collection_stats = collection_stats or {}
        self.raise_nar = raise_nar
        self.raise_tier = raise_tier
        self.raise_relevance = raise_relevance
        self.raise_retention = raise_retention
        self.raise_collection_names = raise_collection_names
        self.calls: list[tuple[str, dict]] = []

    def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
        self.calls.append(("query_nx_answer_runs", {"since": since, "limit": limit}))
        if self.raise_nar is not None:
            raise self.raise_nar
        return self.nar_since if since is not None else self.nar_all_time

    def query_tier_writes(self, *, session_id=None, since=None, last_n=None):
        self.calls.append(("query_tier_writes", {"since": since}))
        if self.raise_tier is not None:
            raise self.raise_tier
        return self.tier_rows

    def get_relevance_stats(self):
        self.calls.append(("get_relevance_stats", {}))
        if self.raise_relevance is not None:
            raise self.raise_relevance
        return self.relevance_stats

    def get_retention_markers(self, relations):
        self.calls.append(("get_retention_markers", {"relations": relations}))
        if self.raise_retention is not None:
            raise self.raise_retention
        return self.retention_markers

    def query_collection_stats(self, collection, *, days=30):
        self.calls.append(("query_collection_stats", {"collection": collection, "days": days}))
        if collection in self.raise_collection_names:
            raise RuntimeError(f"boom on {collection}")
        return self.collection_stats.get(collection, {"row_count": 0})


def _patch_store(monkeypatch, store: _FakeStore) -> None:
    monkeypatch.setattr(
        "nexus.db.t2.http_telemetry_store.HttpTelemetryStore", lambda: store,
    )


def _patch_collections(monkeypatch, names: list[str]) -> None:
    class _FakeT3:
        def list_collections(self):
            return [{"name": n, "count": 0} for n in names]

    monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())


def _patch_drop_meter(monkeypatch, *, total=0, rows=0, raises: Exception | None = None):
    if raises is not None:
        def _raise():
            raise raises
        monkeypatch.setattr("nexus.dropped_writes.count_drops", _raise)
        return

    from nexus.dropped_writes import DropSummary

    monkeypatch.setattr(
        "nexus.dropped_writes.count_drops",
        lambda: DropSummary(total=total, rows=rows),
    )


def _patch_catalog(monkeypatch, *, doc_count=None, raises: Exception | None = None):
    class _FakeCatalog:
        def stats(self):
            if raises is not None:
                raise raises
            return {"doc_count": doc_count} if doc_count is not None else {}

    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_reader", lambda: _FakeCatalog(),
    )


def _patch_all_ok(monkeypatch, *, store: _FakeStore | None = None) -> _FakeStore:
    """Wire every reader to a clean, empty-but-successful fake -- the
    baseline "everything answers, nothing is UNAVAILABLE" scenario."""
    store = store or _FakeStore()
    _patch_store(monkeypatch, store)
    _patch_collections(monkeypatch, [])
    _patch_drop_meter(monkeypatch)
    _patch_catalog(monkeypatch, doc_count=0)
    return store


class TestConsentLiteral:
    """The consent row is a permanent, known fact -- never UNAVAILABLE,
    never omitted, never reworded, and never wrapped in a window (it is
    not a windowed read)."""

    def test_consent_is_the_exact_retirement_literal(self, monkeypatch) -> None:
        from nexus.commands.telemetry_cmd import baseline_cmd

        _patch_all_ok(monkeypatch)
        result = CliRunner().invoke(baseline_cmd, ["--json"])
        assert result.exit_code == 0, result.stdout
        payload = json.loads(result.stdout)
        assert payload["consent"] == "RETIRED (nexus-lqqb2, 2026-08-28)"

    def test_consent_survives_every_other_reader_failing(self, monkeypatch) -> None:
        from nexus.commands.telemetry_cmd import baseline_cmd

        store = _FakeStore(
            raise_nar=ConnectionError("down"),
            raise_tier=ConnectionError("down"),
            raise_relevance=ConnectionError("down"),
            raise_retention=ConnectionError("down"),
        )
        _patch_store(monkeypatch, store)
        monkeypatch.setattr(
            "nexus.db.make_t3",
            lambda: (_ for _ in ()).throw(ConnectionError("down")),
        )
        _patch_drop_meter(monkeypatch, raises=OSError("down"))
        _patch_catalog(monkeypatch, raises=ConnectionError("down"))

        result = CliRunner().invoke(baseline_cmd, ["--json"])
        assert result.exit_code == 0, result.stdout
        payload = json.loads(result.stdout)
        assert payload["consent"] == "RETIRED (nexus-lqqb2, 2026-08-28)"
        # Every other figure degraded honestly -- never omitted, never a
        # fabricated zero.
        for group in ("nx_answer_runs", "tier_writes", "relevance_log", "search_telemetry", "drop_meter"):
            assert group in payload
        assert payload["substrate_check"]["catalog_doc_count"].startswith("UNAVAILABLE:")


class TestAllFiguresPresent:
    def test_json_has_all_seven_top_level_figures_plus_consent(self, monkeypatch) -> None:
        from nexus.commands.telemetry_cmd import baseline_cmd

        _patch_all_ok(monkeypatch)
        result = CliRunner().invoke(baseline_cmd, ["--json"])
        assert result.exit_code == 0, result.stdout
        payload = json.loads(result.stdout)
        for key in (
            "captured_at", "nx_answer_runs", "tier_writes",
            "relevance_log", "search_telemetry", "drop_meter", "consent",
            "substrate_check",
        ):
            assert key in payload, f"missing figure key: {key}"
        assert "since" not in payload, (
            "fix round 1: the single top-level `since` key is removed -- "
            "every figure carries its own `window` instead"
        )

    def test_text_form_renders_one_line_per_figure(self, monkeypatch) -> None:
        from nexus.commands.telemetry_cmd import baseline_cmd

        _patch_all_ok(monkeypatch)
        result = CliRunner().invoke(baseline_cmd, [])
        assert result.exit_code == 0, result.stdout
        for label in (
            "nx_answer runs:", "tier writes (", "relevance_log (",
            "search_telemetry (", "drop meter (", "consent:",
            "substrate check (",
        ):
            assert label in result.stdout, f"missing rendered line: {label}"


class TestWindowScoping:
    """Fix round 1, coordinator item 2: every figure carries its OWN
    window; --since applies ONLY to nx_answer_runs and tier_writes."""

    def test_json_every_figure_carries_its_own_window(self, monkeypatch) -> None:
        from nexus.commands.telemetry_cmd import baseline_cmd

        _patch_all_ok(monkeypatch)
        result = CliRunner().invoke(baseline_cmd, ["--json", "--since", "2026-08-21T00:00:00Z"])
        payload = json.loads(result.stdout)
        assert payload["nx_answer_runs"]["window"] == {"since": "2026-08-21T00:00:00Z"}
        assert payload["tier_writes"]["window"] == {"since": "2026-08-21T00:00:00Z"}
        # These five never observe --since -- always all-time.
        assert payload["relevance_log"]["window"] == "all-time"
        assert payload["search_telemetry"]["window"] == "all-time"
        assert payload["drop_meter"]["window"] == "all-time"
        assert payload["substrate_check"]["window"] == "all-time"

    def test_json_no_since_means_all_time_windows(self, monkeypatch) -> None:
        from nexus.commands.telemetry_cmd import baseline_cmd

        _patch_all_ok(monkeypatch)
        result = CliRunner().invoke(baseline_cmd, ["--json"])
        payload = json.loads(result.stdout)
        assert payload["nx_answer_runs"]["window"] == "all-time"
        assert payload["tier_writes"]["window"] == "all-time"

    def test_text_prints_window_on_every_line(self, monkeypatch) -> None:
        from nexus.commands.telemetry_cmd import baseline_cmd

        _patch_all_ok(monkeypatch)
        result = CliRunner().invoke(baseline_cmd, ["--json", "--since", "2026-08-21T00:00:00Z"])
        assert result.exit_code == 0
        text_result = CliRunner().invoke(baseline_cmd, ["--since", "2026-08-21T00:00:00Z"])
        assert "since 2026-08-21T00:00:00Z" in text_result.stdout
        assert "(all-time)" in text_result.stdout  # relevance_log / search_telemetry / etc.

    def test_unavailable_figure_still_carries_its_window(self, monkeypatch) -> None:
        from nexus.commands.telemetry_cmd import baseline_cmd

        store = _FakeStore(raise_nar=ConnectionError("down"))
        _patch_all_ok(monkeypatch, store=store)
        result = CliRunner().invoke(baseline_cmd, ["--json", "--since", "2026-08-21T00:00:00Z"])
        payload = json.loads(result.stdout)
        assert payload["nx_answer_runs"]["window"] == {"since": "2026-08-21T00:00:00Z"}
        assert payload["nx_answer_runs"]["total"].startswith("UNAVAILABLE:")


class TestNxAnswerRuns:
    def test_populated_reports_total_hit_fallback_and_buckets(self, monkeypatch) -> None:
        from nexus.commands.telemetry_cmd import baseline_cmd

        store = _FakeStore(
            nar_since=_nar_result(
                total=5, hit=3, fallback=2,
                buckets={"under_5s": 1, "5s_to_30s": 2, "30s_to_2min": 1, "2min_to_5min": 1, "over_5min": 0},
                oldest_created_at="2026-08-01T00:00:00Z",
                newest_created_at="2026-08-27T19:39:04Z",
            ),
        )
        _patch_all_ok(monkeypatch, store=store)
        result = CliRunner().invoke(baseline_cmd, ["--json"])
        payload = json.loads(result.stdout)
        nar = payload["nx_answer_runs"]
        assert nar["total"] == 5
        assert nar["since_count"] == 5
        assert nar["hit_count"] == 3
        assert nar["fallback_count"] == 2
        assert nar["latency_buckets"]["5s_to_30s"] == 2

    def test_oldest_and_newest_created_at_present_and_correct(self, monkeypatch) -> None:
        """Coordinator fix round 1, item 1 (Critical): this is the
        instrument behind 08-27's 'zero rows since <ts>' finding -- it
        must be present, not silently dropped after being fetched."""
        from nexus.commands.telemetry_cmd import baseline_cmd

        store = _FakeStore(
            nar_since=_nar_result(
                total=3, hit=2, fallback=1,
                oldest_created_at="2026-08-01T00:00:00Z",
                newest_created_at="2026-08-27T19:39:04Z",
            ),
        )
        _patch_all_ok(monkeypatch, store=store)
        result = CliRunner().invoke(baseline_cmd, ["--json"])
        payload = json.loads(result.stdout)
        nar = payload["nx_answer_runs"]
        assert nar["oldest_created_at"] == "2026-08-01T00:00:00Z"
        assert nar["newest_created_at"] == "2026-08-27T19:39:04Z"

        text_result = CliRunner().invoke(baseline_cmd, [])
        assert "newest=2026-08-27T19:39:04Z" in text_result.stdout
        assert "oldest=2026-08-01T00:00:00Z" in text_result.stdout

    def test_no_rows_reports_null_oldest_and_newest_not_omitted(self, monkeypatch) -> None:
        from nexus.commands.telemetry_cmd import baseline_cmd

        _patch_all_ok(monkeypatch, store=_FakeStore(nar_since=_nar_result()))
        result = CliRunner().invoke(baseline_cmd, ["--json"])
        payload = json.loads(result.stdout)
        nar = payload["nx_answer_runs"]
        assert "oldest_created_at" in nar
        assert "newest_created_at" in nar
        assert nar["oldest_created_at"] is None
        assert nar["newest_created_at"] is None

    def test_since_given_reports_both_all_time_total_and_since_count(self, monkeypatch) -> None:
        from nexus.commands.telemetry_cmd import baseline_cmd

        store = _FakeStore(
            nar_since=_nar_result(total=2, hit=1, fallback=1),
            nar_all_time=_nar_result(total=214, hit=29, fallback=4),
        )
        _patch_all_ok(monkeypatch, store=store)
        result = CliRunner().invoke(baseline_cmd, ["--json", "--since", "2026-08-21T00:00:00Z"])
        payload = json.loads(result.stdout)
        nar = payload["nx_answer_runs"]
        assert nar["total"] == 214, "all-time total must be a SEPARATE call from the since-scoped one"
        assert nar["since_count"] == 2

    def test_text_since_given_uses_plus_m_since_vocabulary(self, monkeypatch) -> None:
        """Coordinator's exact vocabulary: total N (+M since <since>)."""
        from nexus.commands.telemetry_cmd import baseline_cmd

        store = _FakeStore(
            nar_since=_nar_result(total=2, hit=1, fallback=1),
            nar_all_time=_nar_result(total=214, hit=29, fallback=4),
        )
        _patch_all_ok(monkeypatch, store=store)
        result = CliRunner().invoke(baseline_cmd, ["--since", "2026-08-21T00:00:00Z"])
        assert "total=214 (+2 since 2026-08-21T00:00:00Z)" in result.stdout

    def test_read_failure_renders_unavailable_and_stays_present_in_json(self, monkeypatch) -> None:
        import httpx

        from nexus.commands.telemetry_cmd import baseline_cmd

        resp = httpx.Response(404, request=httpx.Request("GET", "http://x/q"))
        exc = httpx.HTTPStatusError("404", request=resp.request, response=resp)
        store = _FakeStore(raise_nar=exc)
        _patch_all_ok(monkeypatch, store=store)

        result = CliRunner().invoke(baseline_cmd, ["--json"])
        assert result.exit_code == 0, result.stdout
        payload = json.loads(result.stdout)
        nar = payload["nx_answer_runs"]
        assert "nx_answer_runs" in payload  # never omitted
        assert nar["total"].startswith("UNAVAILABLE:")
        assert "predates the nx_answer_runs/query route" in nar["total"]
        assert nar["hit_count"].startswith("UNAVAILABLE:")
        assert nar["oldest_created_at"].startswith("UNAVAILABLE:")
        assert nar["newest_created_at"].startswith("UNAVAILABLE:")

        text_result = CliRunner().invoke(baseline_cmd, [])
        assert "UNAVAILABLE:" in text_result.stdout


class TestTierWrites:
    def test_by_tier_by_tool_by_agent_and_null_agent_share(self, monkeypatch) -> None:
        from nexus.commands.telemetry_cmd import baseline_cmd

        rows = [
            ("memory_put", "T2", "developer", "nexus", 5),
            ("store_put", "T3", None, "nexus", 3),
            ("scratch_put", "T1", "developer", None, 2),
        ]
        store = _FakeStore(tier_rows=rows)
        _patch_all_ok(monkeypatch, store=store)

        result = CliRunner().invoke(baseline_cmd, ["--json"])
        payload = json.loads(result.stdout)
        tw = payload["tier_writes"]
        assert tw["total"] == 10
        assert tw["by_tier"] == {"T2": 5, "T3": 3, "T1": 2}
        assert tw["by_tool"] == {"memory_put": 5, "store_put": 3, "scratch_put": 2}
        assert tw["by_agent"] == {"developer": 7, "<none>": 3}
        assert tw["null_agent_share"] == pytest.approx(3 / 10)

    def test_empty_rows_report_zero_total_and_null_share(self, monkeypatch) -> None:
        from nexus.commands.telemetry_cmd import baseline_cmd

        _patch_all_ok(monkeypatch, store=_FakeStore(tier_rows=[]))
        result = CliRunner().invoke(baseline_cmd, ["--json"])
        payload = json.loads(result.stdout)
        tw = payload["tier_writes"]
        assert tw["total"] == 0
        assert tw["null_agent_share"] is None

    def test_read_failure_renders_unavailable(self, monkeypatch) -> None:
        from nexus.commands.telemetry_cmd import baseline_cmd

        store = _FakeStore(raise_tier=ConnectionError("refused"))
        _patch_all_ok(monkeypatch, store=store)
        result = CliRunner().invoke(baseline_cmd, ["--json"])
        payload = json.loads(result.stdout)
        assert payload["tier_writes"]["total"].startswith("UNAVAILABLE:")
        assert "service unreachable" in payload["tier_writes"]["total"]


class TestRelevanceLog:
    def test_count_oldest_newest_and_retention_marker(self, monkeypatch) -> None:
        from nexus.commands.telemetry_cmd import baseline_cmd

        store = _FakeStore(
            relevance_stats={"count": 39, "oldest": "2019-01-01T00:00:00Z", "newest": "2026-08-27T19:39:04Z"},
            retention_markers={"nexus.relevance_log": 337},
        )
        _patch_all_ok(monkeypatch, store=store)
        result = CliRunner().invoke(baseline_cmd, ["--json"])
        payload = json.loads(result.stdout)
        rl = payload["relevance_log"]
        assert rl["count"] == 39
        assert rl["oldest"] == "2019-01-01T00:00:00Z"
        assert rl["newest"] == "2026-08-27T19:39:04Z"
        assert rl["retention_marker"] == 337
        assert rl["window"] == "all-time"

    def test_text_labels_count_as_server_side_sql(self, monkeypatch) -> None:
        """Coordinator fix round 1, item 4: relevance_log's count is the
        substrate-direct telemetry figure -- must say so."""
        from nexus.commands.telemetry_cmd import baseline_cmd

        _patch_all_ok(monkeypatch)
        result = CliRunner().invoke(baseline_cmd, [])
        assert "relevance_log (all-time): count=0 (server-side SQL)" in result.stdout

    def test_absent_marker_defaults_to_zero_not_unavailable(self, monkeypatch) -> None:
        from nexus.commands.telemetry_cmd import baseline_cmd

        _patch_all_ok(monkeypatch, store=_FakeStore(retention_markers={}))
        result = CliRunner().invoke(baseline_cmd, ["--json"])
        payload = json.loads(result.stdout)
        assert payload["relevance_log"]["retention_marker"] == 0

    def test_new_route_404_renders_unavailable_but_retention_marker_still_reads(
        self, monkeypatch,
    ) -> None:
        """A pre-v0x32 engine 404s on the NEW relevance/stats route but
        still answers the OLD retention/markers route -- the two calls
        fail independently."""
        import httpx

        from nexus.commands.telemetry_cmd import baseline_cmd

        resp = httpx.Response(404, request=httpx.Request("GET", "http://x/relevance/stats"))
        exc = httpx.HTTPStatusError("404", request=resp.request, response=resp)
        store = _FakeStore(
            raise_relevance=exc,
            retention_markers={"nexus.relevance_log": 42},
        )
        _patch_all_ok(monkeypatch, store=store)
        result = CliRunner().invoke(baseline_cmd, ["--json"])
        payload = json.loads(result.stdout)
        rl = payload["relevance_log"]
        assert rl["count"].startswith("UNAVAILABLE:")
        assert "predates the relevance/stats route" in rl["count"]
        assert rl["retention_marker"] == 42, (
            "the retention/markers route is unaffected by the new route's failure"
        )


class TestSearchTelemetry:
    def test_sums_row_counts_across_collections_and_counts_errors(self, monkeypatch) -> None:
        from nexus.commands.telemetry_cmd import baseline_cmd

        store = _FakeStore(
            collection_stats={
                "knowledge__a": {"row_count": 100, "zero_hit_rate": 0.1},
                "knowledge__b": {"row_count": 50, "zero_hit_rate": 0.2},
            },
            raise_collection_names=("knowledge__c",),
        )
        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore", lambda: store,
        )
        _patch_collections(monkeypatch, ["knowledge__a", "knowledge__b", "knowledge__c"])
        _patch_drop_meter(monkeypatch)
        _patch_catalog(monkeypatch, doc_count=0)

        result = CliRunner().invoke(baseline_cmd, ["--json"])
        payload = json.loads(result.stdout)
        st = payload["search_telemetry"]
        assert st["row_count_total"] == 150
        assert st["collections_examined"] == 3
        assert st["errors"] == 1

    def test_lower_bound_caveat_is_unconditional_even_with_zero_errors(self, monkeypatch) -> None:
        """substantive-critic round 1 SIGNIFICANT-1: LOWER BOUND is
        structural (list_collections() can miss collections), not gated
        on errors > 0."""
        from nexus.commands.telemetry_cmd import baseline_cmd

        store = _FakeStore(collection_stats={"knowledge__a": {"row_count": 10, "zero_hit_rate": 0.0}})
        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore", lambda: store,
        )
        _patch_collections(monkeypatch, ["knowledge__a"])
        _patch_drop_meter(monkeypatch)
        _patch_catalog(monkeypatch, doc_count=0)

        result_json = CliRunner().invoke(baseline_cmd, ["--json"])
        payload = json.loads(result_json.stdout)
        assert payload["search_telemetry"]["errors"] == 0
        assert payload["search_telemetry"]["lower_bound"] is True

        result_text = CliRunner().invoke(baseline_cmd, [])
        assert "LOWER BOUND" in result_text.stdout
        assert "0 collections unreadable" in result_text.stdout

    def test_zero_hit_rate_by_collection_json_and_worst_two_in_text(self, monkeypatch) -> None:
        """Exact 08-27 vocabulary: 'zero_hit_rate 0.524 knowledge__dt-papers,
        0.325 knowledge__knowledge' -- the two WORST (highest) readings."""
        from nexus.commands.telemetry_cmd import baseline_cmd

        store = _FakeStore(
            collection_stats={
                "knowledge__dt-papers": {"row_count": 10, "zero_hit_rate": 0.524},
                "knowledge__knowledge": {"row_count": 20, "zero_hit_rate": 0.325},
                "knowledge__clean": {"row_count": 30, "zero_hit_rate": 0.01},
            },
        )
        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore", lambda: store,
        )
        _patch_collections(monkeypatch, ["knowledge__dt-papers", "knowledge__knowledge", "knowledge__clean"])
        _patch_drop_meter(monkeypatch)
        _patch_catalog(monkeypatch, doc_count=0)

        result_json = CliRunner().invoke(baseline_cmd, ["--json"])
        payload = json.loads(result_json.stdout)
        zhr = payload["search_telemetry"]["zero_hit_rate_by_collection"]
        assert zhr == {
            "knowledge__dt-papers": 0.524,
            "knowledge__knowledge": 0.325,
            "knowledge__clean": 0.01,
        }

        result_text = CliRunner().invoke(baseline_cmd, [])
        assert "zero_hit_rate 0.524 knowledge__dt-papers, 0.325 knowledge__knowledge" in result_text.stdout
        assert "knowledge__clean" not in result_text.stdout.split("zero_hit_rate")[-1]

    def test_zero_hit_rate_null_sentinel_string_excluded(self, monkeypatch) -> None:
        """The engine sends the STRING "null" (not JSON null) for an
        empty-population zero_hit_rate (Map.of() cannot hold null) --
        must be filtered out, never treated as 0.0."""
        from nexus.commands.telemetry_cmd import baseline_cmd

        store = _FakeStore(
            collection_stats={"knowledge__empty": {"row_count": 0, "zero_hit_rate": "null"}},
        )
        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore", lambda: store,
        )
        _patch_collections(monkeypatch, ["knowledge__empty"])
        _patch_drop_meter(monkeypatch)
        _patch_catalog(monkeypatch, doc_count=0)

        result = CliRunner().invoke(baseline_cmd, ["--json"])
        payload = json.loads(result.stdout)
        assert payload["search_telemetry"]["zero_hit_rate_by_collection"] == {}

    def test_enumeration_failure_renders_the_whole_figure_unavailable(self, monkeypatch) -> None:
        from nexus.commands.telemetry_cmd import baseline_cmd

        _patch_all_ok(monkeypatch)
        monkeypatch.setattr(
            "nexus.db.make_t3",
            lambda: (_ for _ in ()).throw(RuntimeError("no vector service")),
        )
        result = CliRunner().invoke(baseline_cmd, ["--json"])
        payload = json.loads(result.stdout)
        st = payload["search_telemetry"]
        assert st["row_count_total"].startswith("UNAVAILABLE:")
        assert st["collections_examined"].startswith("UNAVAILABLE:")
        assert st["errors"].startswith("UNAVAILABLE:")


class TestDropMeter:
    def test_reports_count_drops_summary(self, monkeypatch) -> None:
        from nexus.commands.telemetry_cmd import baseline_cmd

        _patch_all_ok(monkeypatch)
        _patch_drop_meter(monkeypatch, total=4, rows=9)
        result = CliRunner().invoke(baseline_cmd, ["--json"])
        payload = json.loads(result.stdout)
        assert payload["drop_meter"]["total"] == 4
        assert payload["drop_meter"]["rows"] == 9
        assert payload["drop_meter"]["window"] == "all-time"

    def test_read_failure_renders_unavailable(self, monkeypatch) -> None:
        from nexus.commands.telemetry_cmd import baseline_cmd

        _patch_all_ok(monkeypatch)
        _patch_drop_meter(monkeypatch, raises=OSError("permission denied"))
        result = CliRunner().invoke(baseline_cmd, ["--json"])
        payload = json.loads(result.stdout)
        assert payload["drop_meter"]["total"].startswith("UNAVAILABLE:")
        assert payload["drop_meter"]["rows"].startswith("UNAVAILABLE:")


class TestSubstrateCheck:
    def test_reports_catalog_doc_count(self, monkeypatch) -> None:
        from nexus.commands.telemetry_cmd import baseline_cmd

        _patch_all_ok(monkeypatch)
        _patch_catalog(monkeypatch, doc_count=19_358)
        result = CliRunner().invoke(baseline_cmd, ["--json"])
        payload = json.loads(result.stdout)
        assert payload["substrate_check"]["catalog_doc_count"] == 19_358
        assert payload["substrate_check"]["window"] == "all-time"

    def test_text_labels_it_as_context_not_a_telemetry_anchor(self, monkeypatch) -> None:
        """Coordinator fix round 1, item 4."""
        from nexus.commands.telemetry_cmd import baseline_cmd

        _patch_all_ok(monkeypatch)
        _patch_catalog(monkeypatch, doc_count=19_358)
        result = CliRunner().invoke(baseline_cmd, [])
        assert "catalog_doc_count=19358 (engine SQL, context, not a telemetry anchor)" in result.stdout

    def test_no_doc_count_field_renders_unavailable(self, monkeypatch) -> None:
        from nexus.commands.telemetry_cmd import baseline_cmd

        _patch_all_ok(monkeypatch)
        _patch_catalog(monkeypatch, doc_count=None)
        result = CliRunner().invoke(baseline_cmd, ["--json"])
        payload = json.loads(result.stdout)
        assert payload["substrate_check"]["catalog_doc_count"].startswith("UNAVAILABLE:")

    def test_reader_failure_renders_unavailable(self, monkeypatch) -> None:
        from nexus.commands.telemetry_cmd import baseline_cmd

        _patch_all_ok(monkeypatch)
        _patch_catalog(monkeypatch, raises=ConnectionError("refused"))
        result = CliRunner().invoke(baseline_cmd, ["--json"])
        payload = json.loads(result.stdout)
        assert payload["substrate_check"]["catalog_doc_count"].startswith("UNAVAILABLE:")


class TestSharedStore:
    """Code-review round 1 Suggestion: one shared HttpTelemetryStore
    backs the whole capture, not five independent reconstructions."""

    def test_store_construction_failure_degrades_every_store_dependent_figure(
        self, monkeypatch,
    ) -> None:
        from nexus.commands.telemetry_cmd import baseline_cmd

        def _raise():
            raise RuntimeError("NX_SERVICE_PORT not set")

        monkeypatch.setattr("nexus.db.t2.http_telemetry_store.HttpTelemetryStore", _raise)
        _patch_collections(monkeypatch, [])
        _patch_drop_meter(monkeypatch)
        _patch_catalog(monkeypatch, doc_count=0)

        result = CliRunner().invoke(baseline_cmd, ["--json"])
        assert result.exit_code == 0, result.stdout
        payload = json.loads(result.stdout)
        assert payload["nx_answer_runs"]["total"].startswith("UNAVAILABLE:")
        assert payload["tier_writes"]["total"].startswith("UNAVAILABLE:")
        assert payload["relevance_log"]["count"].startswith("UNAVAILABLE:")
        assert payload["search_telemetry"]["row_count_total"].startswith("UNAVAILABLE:")
        # drop_meter and substrate_check do not depend on HttpTelemetryStore.
        assert payload["drop_meter"]["total"] == 0
