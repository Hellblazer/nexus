# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx answer-runs`` CLI (nexus-eho3u).

Same shape as tests/test_tier_status_cli.py's TestServiceModeReadParity:
a fake HttpTelemetryStore stands in for the engine, so these tests cover
the CLI's rendering and capability-honest degrade without requiring a real
engine round trip (that's tests/db/test_eho3u_nx_answer_runs_read.py's job).
"""
from __future__ import annotations

import json

from click.testing import CliRunner


_EMPTY_RESULT = {
    "rows": [], "total": 0, "oldest_created_at": "",
    "hit_count": 0, "fallback_count": 0,
    "avg_duration_ms": None, "avg_cost_usd": None,
    "latency_buckets": {
        "under_5s": 0, "5s_to_30s": 0, "30s_to_2min": 0,
        "2min_to_5min": 0, "over_5min": 0,
    },
}


def _populated_result() -> dict:
    return {
        "rows": [
            {
                "id": 2, "question": "second question", "plan_id": None,
                "matched_confidence": None, "step_count": 0,
                "final_text": "Planner error: x", "cost_usd": 0.0,
                "duration_ms": 4_000, "created_at": "2026-08-05T00:01:00Z",
            },
            {
                "id": 1, "question": "first question", "plan_id": 7,
                "matched_confidence": 0.9, "step_count": 1,
                "final_text": "answer", "cost_usd": 0.01,
                "duration_ms": 12_000, "created_at": "2026-08-05T00:00:00Z",
            },
        ],
        "total": 2,
        "oldest_created_at": "2026-08-05T00:00:00Z",
        "hit_count": 1,
        "fallback_count": 1,
        "avg_duration_ms": 8_000.0,
        "avg_cost_usd": 0.005,
        "latency_buckets": {
            "under_5s": 0, "5s_to_30s": 2, "30s_to_2min": 0,
            "2min_to_5min": 0, "over_5min": 0,
        },
    }


def _result_with_sentinel() -> dict:
    """A row carrying the plan_id=0 ad-hoc-Match sentinel (nexus-eho3u
    review fix) — a successful inline-planner run, NOT a matched plan."""
    return {
        "rows": [
            {
                "id": 1, "question": "ad-hoc success", "plan_id": 0,
                "matched_confidence": None, "step_count": 2,
                "final_text": "ad-hoc answer", "cost_usd": 0.002,
                "duration_ms": 2_000, "created_at": "2026-08-05T00:00:00Z",
            },
        ],
        "total": 1,
        "oldest_created_at": "2026-08-05T00:00:00Z",
        "hit_count": 0,
        "fallback_count": 1,
        "avg_duration_ms": 2_000.0,
        "avg_cost_usd": 0.002,
        "latency_buckets": {
            "under_5s": 1, "5s_to_30s": 0, "30s_to_2min": 0,
            "2min_to_5min": 0, "over_5min": 0,
        },
    }


class TestEmptyResult:
    def test_empty_prints_no_runs(self, monkeypatch) -> None:
        from nexus.commands.answer_runs import answer_runs_cmd

        class _EmptyStore:
            def query_nx_answer_runs(self, *, since=None, limit=20):
                return dict(_EMPTY_RESULT)

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _EmptyStore(),
        )
        result = CliRunner().invoke(answer_runs_cmd, [])
        assert result.exit_code == 0, result.output
        assert "no runs" in result.output

    def test_empty_json_wraps_the_zeroed_structure_in_the_query_envelope(
        self, monkeypatch,
    ) -> None:
        # nexus-eho3u review fix (critic S1): --json echoes since/limit/
        # captured_at around the store result, matching `nx tier-status
        # --json` parity — see the envelope test below for the populated
        # case's full rationale.
        from nexus.commands.answer_runs import answer_runs_cmd

        class _EmptyStore:
            def query_nx_answer_runs(self, *, since=None, limit=20):
                return dict(_EMPTY_RESULT)

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _EmptyStore(),
        )
        result = CliRunner().invoke(answer_runs_cmd, ["--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["since"] is None
        assert payload["limit"] == 20
        assert "captured_at" in payload
        for key, value in _EMPTY_RESULT.items():
            assert payload[key] == value


class TestPopulatedResult:
    def test_human_output_shows_totals_hit_fallback_and_buckets(
        self, monkeypatch,
    ) -> None:
        from nexus.commands.answer_runs import answer_runs_cmd

        class _FakeStore:
            def query_nx_answer_runs(self, *, since=None, limit=20):
                return _populated_result()

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _FakeStore(),
        )
        result = CliRunner().invoke(answer_runs_cmd, [])
        assert result.exit_code == 0, result.output
        assert "total: 2" in result.output
        assert "plan-match hit: 1" in result.output
        assert "inline-planner fallback: 1" in result.output
        assert "5s-30s" in result.output
        assert "first question" in result.output
        assert "second question" in result.output

    def test_json_output_wraps_the_store_result_in_the_query_envelope(
        self, monkeypatch,
    ) -> None:
        # nexus-eho3u review fix (critic S1): --json must echo its own
        # query envelope (since/limit/capture-time), matching the
        # `nx tier-status --json` parity the module docstring claims —
        # tier_status.py's _emit_report wraps scope/session_id/last_n/since
        # around the store rows the same way. An earlier version of this
        # test locked in an envelope-LESS shape (bare store result), which
        # is exactly the drift this fix corrects — so this test asserts the
        # envelope keys explicitly rather than re-freezing whatever the
        # command happens to emit.
        from nexus.commands.answer_runs import answer_runs_cmd

        class _FakeStore:
            def query_nx_answer_runs(self, *, since=None, limit=20):
                return _populated_result()

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _FakeStore(),
        )
        result = CliRunner().invoke(
            answer_runs_cmd, ["--since", "2026-08-01T00:00:00Z", "--limit", "20", "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["since"] == "2026-08-01T00:00:00Z"
        assert payload["limit"] == 20
        assert "captured_at" in payload, "captured_at must be present for baseline-snapshot diffing"
        # captured_at is a real, parseable ISO-8601 timestamp, not a stub.
        from datetime import datetime as _dt
        _dt.fromisoformat(payload["captured_at"])
        for key, value in _populated_result().items():
            assert payload[key] == value, f"store field {key!r} must pass through unchanged"

    def test_human_output_renders_ad_hoc_sentinel_as_fallback_not_plan_zero(
        self, monkeypatch,
    ) -> None:
        # nexus-eho3u review fix: plan_id=0 (the ad-hoc Match sentinel) must
        # render as "fallback", never the misleading "plan=0" a naive
        # `plan_id is not None` per-row check would produce.
        from nexus.commands.answer_runs import answer_runs_cmd

        class _SentinelStore:
            def query_nx_answer_runs(self, *, since=None, limit=20):
                return _result_with_sentinel()

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _SentinelStore(),
        )
        result = CliRunner().invoke(answer_runs_cmd, [])
        assert result.exit_code == 0, result.output
        assert "fallback" in result.output
        assert "plan=0" not in result.output
        assert "plan-match hit: 0" in result.output
        assert "inline-planner fallback: 1" in result.output

    def test_since_and_limit_are_forwarded(self, monkeypatch) -> None:
        from nexus.commands.answer_runs import answer_runs_cmd

        captured = {}

        class _CapturingStore:
            def query_nx_answer_runs(self, *, since=None, limit=20):
                captured["since"] = since
                captured["limit"] = limit
                return dict(_EMPTY_RESULT)

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _CapturingStore(),
        )
        result = CliRunner().invoke(
            answer_runs_cmd,
            ["--since", "2026-08-01T00:00:00Z", "--limit", "5"],
        )
        assert result.exit_code == 0, result.output
        assert captured == {"since": "2026-08-01T00:00:00Z", "limit": 5}


class TestCapabilityHonestDegrade:
    """nexus-eho3u, vw594-F3 precedent: pre-route engines say unavailable,
    never a silent zero. Mirrors test_tier_status_cli.py's 404/500 split."""

    def test_404_names_engine_skew(self, monkeypatch) -> None:
        import httpx

        from nexus.commands.answer_runs import answer_runs_cmd

        class _404Store:
            def query_nx_answer_runs(self, **_kw):
                resp = httpx.Response(404, request=httpx.Request("GET", "http://x/q"))
                raise httpx.HTTPStatusError("404", request=resp.request, response=resp)

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _404Store(),
        )
        result = CliRunner().invoke(answer_runs_cmd, [])
        assert result.exit_code == 0, result.output
        assert "predates the nx_answer_runs/query route" in result.output
        assert "total: 0" not in result.output

    def test_500_names_engine_error(self, monkeypatch) -> None:
        import httpx

        from nexus.commands.answer_runs import answer_runs_cmd

        class _500Store:
            def query_nx_answer_runs(self, **_kw):
                resp = httpx.Response(500, request=httpx.Request("GET", "http://x/q"))
                raise httpx.HTTPStatusError("500", request=resp.request, response=resp)

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _500Store(),
        )
        result = CliRunner().invoke(answer_runs_cmd, [])
        assert result.exit_code == 0, result.output
        assert "HTTP 500" in result.output
        assert "investigate the engine" in result.output

    def test_unreachable_names_service_unreachable(self, monkeypatch) -> None:
        from nexus.commands.answer_runs import answer_runs_cmd

        class _DeadStore:
            def query_nx_answer_runs(self, **_kw):
                raise ConnectionError("connection refused")

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _DeadStore(),
        )
        result = CliRunner().invoke(answer_runs_cmd, [])
        assert result.exit_code == 0, result.output
        assert "service unreachable" in result.output

    def test_json_degrade_is_structured(self, monkeypatch) -> None:
        import httpx

        from nexus.commands.answer_runs import answer_runs_cmd

        class _404Store:
            def query_nx_answer_runs(self, **_kw):
                resp = httpx.Response(404, request=httpx.Request("GET", "http://x/q"))
                raise httpx.HTTPStatusError("404", request=resp.request, response=resp)

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _404Store(),
        )
        result = CliRunner().invoke(answer_runs_cmd, ["--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["service_backed"] is True
        assert "predates" in payload["message"]
