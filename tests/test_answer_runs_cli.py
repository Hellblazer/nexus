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

import pytest
from click.testing import CliRunner


def _steps_gated(result: dict, include_steps: bool) -> dict:
    """Strip ``steps`` from every row unless *include_steps* is True --
    matches the REAL ``HttpTelemetryStore.query_nx_answer_runs`` contract
    (a row only ever carries ``steps`` when the caller requested
    ``include_steps=True``; see that method's own docstring). RDR-196
    .p1e review-fix round 2 (code-review-expert T2 [23115]): a fake
    store that ignores ``include_steps`` and always returns ``steps``
    gives false test confidence -- a test invoking the CLI WITHOUT
    ``--steps`` could still "see" steps data no real engine would ever
    send for that call, papering over a classification bug that only
    manifests when steps are genuinely absent. Every fake store's
    ``query_nx_answer_runs`` in this file routes its return value
    through this helper so a fixture author cannot forget the gate
    (and rows with no ``"steps"`` key at all pass through unchanged,
    a no-op)."""
    if include_steps:
        return result
    gated = dict(result)
    gated["rows"] = [
        {k: v for k, v in r.items() if k != "steps"}
        for r in (result.get("rows") or [])
    ]
    return gated


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
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                return _steps_gated(dict(_EMPTY_RESULT), include_steps)

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
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                return _steps_gated(dict(_EMPTY_RESULT), include_steps)

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
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                return _steps_gated(_populated_result(), include_steps)

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
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                return _steps_gated(_populated_result(), include_steps)

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
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
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
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                captured["since"] = since
                captured["limit"] = limit
                return _steps_gated(dict(_EMPTY_RESULT), include_steps)

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


# ── RDR-196 .p1e (nexus-nyry9.11): executed/degenerate split ────────────────


def _mixed_executed_degenerate_result() -> dict:
    """Two executed rows (step_count > 0) and three degenerate rows
    (step_count == 0) spanning all three read-time-detectable classes:
    redacted (trace=False), (planner) error, and an unclassifiable
    'other' (e.g. a harness artifact)."""
    return {
        "rows": [
            {
                "id": 5, "question": "[redacted]", "plan_id": 3,
                "matched_confidence": 0.6, "step_count": 0,
                "final_text": "[redacted]", "cost_usd": None,
                "duration_ms": 1_500, "created_at": "2026-08-20T00:04:00Z",
            },
            {
                "id": 4, "question": "bad plan", "plan_id": None,
                "matched_confidence": None, "step_count": 0,
                "final_text": "Planner error: dispatch failed", "cost_usd": None,
                "duration_ms": 300, "created_at": "2026-08-20T00:03:00Z",
            },
            {
                "id": 3, "question": "harness probe", "plan_id": None,
                "matched_confidence": None, "step_count": 0,
                "final_text": "ok", "cost_usd": None,
                "duration_ms": 100, "created_at": "2026-08-20T00:02:00Z",
            },
            {
                "id": 2, "question": "second question", "plan_id": 7,
                "matched_confidence": 0.9, "step_count": 1,
                "final_text": "answer", "cost_usd": 0.01,
                "duration_ms": 4_000, "created_at": "2026-08-20T00:01:00Z",
            },
            {
                "id": 1, "question": "first question", "plan_id": 7,
                "matched_confidence": 0.9, "step_count": 2,
                "final_text": "answer", "cost_usd": 0.02,
                "duration_ms": 12_000, "created_at": "2026-08-20T00:00:00Z",
            },
        ],
        "total": 5,
        "oldest_created_at": "2026-08-20T00:00:00Z",
        "hit_count": 3,
        "fallback_count": 2,
        "avg_duration_ms": 3_580.0,
        "avg_cost_usd": 0.015,
        "latency_buckets": {
            "under_5s": 4, "5s_to_30s": 1, "30s_to_2min": 0,
            "2min_to_5min": 0, "over_5min": 0,
        },
    }


class TestExecutedDegenerateSplit:
    def test_human_output_separates_executed_from_degenerate_counts(
        self, monkeypatch,
    ) -> None:
        from nexus.commands.answer_runs import answer_runs_cmd

        class _MixedStore:
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                return _steps_gated(_mixed_executed_degenerate_result(), include_steps)

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _MixedStore(),
        )
        result = CliRunner().invoke(answer_runs_cmd, [])
        assert result.exit_code == 0, result.output
        assert "executed-ok 2" in result.output
        assert "executed-failed 0" in result.output
        assert "degenerate 3" in result.output
        assert "degenerate/redacted: 1" in result.output
        assert "degenerate/planner_error: 1" in result.output
        assert "degenerate/other: 1" in result.output

    def test_json_output_carries_executed_degenerate_counts_not_conflated(
        self, monkeypatch,
    ) -> None:
        """The falsifiable VERIFICATION item: a fixture with BOTH
        populations must show the counts kept SEPARATE, never summed or
        mixed into the whole-set aggregates (total/hit_count/etc. stay
        untouched -- they are the engine's own whole-set numbers)."""
        from nexus.commands.answer_runs import answer_runs_cmd

        class _MixedStore:
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                return _steps_gated(_mixed_executed_degenerate_result(), include_steps)

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _MixedStore(),
        )
        result = CliRunner().invoke(answer_runs_cmd, ["--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)

        assert payload["total"] == 5, "engine whole-set aggregate must pass through unchanged"
        assert payload["executed_ok_count"] == 2
        assert payload["executed_failed_count"] == 0
        assert payload["degenerate_count"] == 3
        assert payload["degenerate_breakdown"] == {
            "redacted": 1, "planner_error": 1, "other": 1,
        }
        assert (
            payload["executed_ok_count"]
            + payload["executed_failed_count"]
            + payload["degenerate_count"]
        ) == len(payload["rows"])

    def test_all_executed_rows_have_zero_degenerate_count(self, monkeypatch) -> None:
        from nexus.commands.answer_runs import answer_runs_cmd

        class _FakeStore:
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                return _steps_gated(_populated_result(), include_steps)

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _FakeStore(),
        )
        result = CliRunner().invoke(answer_runs_cmd, ["--json"])
        payload = json.loads(result.stdout)
        # _populated_result has one step_count=0 (planner-error) row and
        # one step_count=1 row (final_text="answer", not an error -> ok).
        assert payload["executed_ok_count"] == 1
        assert payload["executed_failed_count"] == 0
        assert payload["degenerate_count"] == 1
        assert payload["degenerate_breakdown"] == {"planner_error": 1}


def _mixed_ok_failed_degenerate_result() -> dict:
    """One executed-OK row, two executed-FAILED rows (one via
    final_text startswith 'Error:', one via a --steps-carried last
    step ok=False with a non-error final_text), one degenerate row."""
    return {
        "rows": [
            {
                "id": 40, "question": "degenerate", "plan_id": None,
                "matched_confidence": None, "step_count": 0,
                "final_text": "Planner error: x", "cost_usd": None,
                "duration_ms": 200, "created_at": "2026-08-21T00:03:00Z",
            },
            {
                "id": 30, "question": "failed via final_text", "plan_id": 9,
                "matched_confidence": 0.8, "step_count": 2,
                "final_text": "Error: plan execution boom", "cost_usd": 0.01,
                "duration_ms": 9_000, "created_at": "2026-08-21T00:02:00Z",
                "steps": [
                    {"step_index": 0, "operator": "search", "source": "sql",
                     "model": None, "input_tokens": 0, "output_tokens": 0,
                     "cost_usd": 0.0, "elapsed_ms": 50, "ok": True, "bundled_steps": []},
                    {"step_index": 1, "operator": "summarize", "source": "llm",
                     "model": "claude-sonnet-5-20260101", "input_tokens": 10,
                     "output_tokens": 5, "cost_usd": 0.01, "elapsed_ms": 500,
                     "ok": False, "bundled_steps": []},
                ],
            },
            {
                "id": 20, "question": "failed via last-step ok=False", "plan_id": 9,
                "matched_confidence": 0.8, "step_count": 1,
                # NOT an "Error:"-prefixed final_text -- the ONLY signal
                # this row is a failure is the last step's ok=False.
                "final_text": "[budget exhausted after step 1 of 2 — partial answer]",
                "cost_usd": None,
                "duration_ms": 3_000, "created_at": "2026-08-21T00:01:00Z",
                "steps": [
                    {"step_index": 0, "operator": "summarize", "source": "llm",
                     "model": None, "input_tokens": None, "output_tokens": None,
                     "cost_usd": None, "elapsed_ms": 3_000, "ok": False, "bundled_steps": []},
                ],
            },
            {
                "id": 10, "question": "genuine success", "plan_id": 9,
                "matched_confidence": 0.9, "step_count": 1,
                "final_text": "answer", "cost_usd": 0.02,
                "duration_ms": 1_000, "created_at": "2026-08-21T00:00:00Z",
                "steps": [
                    {"step_index": 0, "operator": "summarize", "source": "llm",
                     "model": "claude-sonnet-5-20260101", "input_tokens": 20,
                     "output_tokens": 10, "cost_usd": 0.02, "elapsed_ms": 1_000,
                     "ok": True, "bundled_steps": []},
                ],
            },
        ],
        "total": 4,
        "oldest_created_at": "2026-08-21T00:00:00Z",
        "hit_count": 3,
        "fallback_count": 1,
        "avg_duration_ms": 3_300.0,
        "avg_cost_usd": 0.015,
        "latency_buckets": {
            "under_5s": 4, "5s_to_30s": 0, "30s_to_2min": 0,
            "2min_to_5min": 0, "over_5min": 0,
        },
        "steps_supported": True,
    }


class TestExecutedFailedSplit:
    """RDR-196 .p1e review-fix (Important, code-review-expert T2 [23110]):
    a run that completed >=1 real step and then FAILED must be
    distinguished from a genuine success -- both in the top-level
    three-way count and in the --steps aggregates, which key off
    per-step ``ok`` and must not silently blend a failure's steps into
    'what a normal run costs'."""

    def test_json_three_way_split_counts(self, monkeypatch) -> None:
        from nexus.commands.answer_runs import answer_runs_cmd

        class _FakeStore:
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                return _steps_gated(_mixed_ok_failed_degenerate_result(), include_steps)

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _FakeStore(),
        )
        result = CliRunner().invoke(answer_runs_cmd, ["--json"])
        payload = json.loads(result.stdout)
        assert payload["executed_ok_count"] == 1
        assert payload["executed_failed_count"] == 2
        assert payload["degenerate_count"] == 1

    def test_default_steps_breakdown_excludes_failed_rows(self, monkeypatch) -> None:
        from nexus.commands.answer_runs import answer_runs_cmd

        class _FakeStore:
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                return _steps_gated(_mixed_ok_failed_degenerate_result(), include_steps)

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _FakeStore(),
        )
        result = CliRunner().invoke(answer_runs_cmd, ["--steps", "--json"])
        payload = json.loads(result.stdout)
        breakdown = payload["step_breakdown"]
        # Only the genuine-success row's one "summarize" step counts.
        assert breakdown["by_operator"]["summarize"]["count"] == 1
        assert breakdown["by_operator"]["summarize"]["total_cost_usd"] == pytest.approx(0.02)
        assert "search" not in breakdown["by_operator"], (
            "the failed row's 'search' step must not leak into the "
            "default (executed-ok-only) breakdown"
        )

    def test_include_failed_folds_failed_rows_into_breakdown(self, monkeypatch) -> None:
        from nexus.commands.answer_runs import answer_runs_cmd

        class _FakeStore:
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                return _steps_gated(_mixed_ok_failed_degenerate_result(), include_steps)

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _FakeStore(),
        )
        result = CliRunner().invoke(
            answer_runs_cmd, ["--steps", "--include-failed", "--json"],
        )
        payload = json.loads(result.stdout)
        breakdown = payload["step_breakdown"]
        # Now the failed rows' steps are folded in: 2 summarize (ok row +
        # error-final_text row) + 1 summarize (last-step-ok=False row) = 3.
        assert breakdown["by_operator"]["summarize"]["count"] == 3
        assert breakdown["by_operator"]["search"]["count"] == 1

    def test_human_output_shows_executed_failed_count(self, monkeypatch) -> None:
        from nexus.commands.answer_runs import answer_runs_cmd

        class _FakeStore:
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                return _steps_gated(_mixed_ok_failed_degenerate_result(), include_steps)

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _FakeStore(),
        )
        result = CliRunner().invoke(answer_runs_cmd, [])
        assert result.exit_code == 0, result.output
        assert "executed-ok 1" in result.output
        assert "executed-failed 2" in result.output

    def test_default_invocation_classifies_budget_exhausted_row_without_steps(
        self, monkeypatch,
    ) -> None:
        """RDR-196 .p1e review-fix round 2 (Important #1, code-review-
        expert T2 [23115]): a budget-exhausted row's ``final_text`` is
        the ``[budget exhausted after step N of M -- partial answer]``
        marker, NEVER "Error:"-prefixed, and its per-step ``ok`` flags
        are only observable when ``--steps`` fetched them. Before this
        fix such a row misclassified as executed-OK in the tool's
        DEFAULT (no ``--steps``) invocation, silently polluting
        ``executed_ok_avg_duration_ms``/``executed_ok_latency_buckets``
        with a near-deadline duration -- reproducing the exact 45x-style
        mislabeling this whole bead exists to prevent, in the very field
        built to prevent it.

        Builds ``final_text`` from the REAL shared constant
        (``nexus.mcp.core.NX_ANSWER_BUDGET_EXHAUSTED_MARKER_PREFIX``),
        not a retyped literal, so this test is structurally coupled to
        the actual emitter, not a look-alike string. The row carries NO
        ``steps`` key at all (the real contract for ``include_steps=
        False``, honoured here via ``_steps_gated`` even though this
        particular fixture never had a "steps" field to strip -- see
        the invocation below, which deliberately omits ``--steps``).
        """
        from nexus.commands.answer_runs import answer_runs_cmd
        from nexus.mcp.core import NX_ANSWER_BUDGET_EXHAUSTED_MARKER_PREFIX

        budget_row_result = {
            "rows": [{
                "id": 50, "question": "budget run", "plan_id": 5,
                "matched_confidence": 0.7, "step_count": 2,
                "final_text": (
                    f"{NX_ANSWER_BUDGET_EXHAUSTED_MARKER_PREFIX} after "
                    "step 2 of 3 — partial answer]"
                ),
                "cost_usd": 0.15, "duration_ms": 295_000,
                "created_at": "2026-08-21T01:00:00Z",
                # Deliberately NO "steps" key -- this is what the real
                # engine sends when include_steps=False was requested.
            }],
            "total": 1, "oldest_created_at": "2026-08-21T01:00:00Z",
            "hit_count": 1, "fallback_count": 0,
            "avg_duration_ms": 295_000.0, "avg_cost_usd": 0.15,
            "latency_buckets": {
                "under_5s": 0, "5s_to_30s": 0, "30s_to_2min": 0,
                "2min_to_5min": 0, "over_5min": 1,
            },
        }

        class _FakeStore:
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                assert include_steps is False, (
                    "this test invokes the CLI without --steps -- the "
                    "store must be asked for include_steps=False"
                )
                return _steps_gated(budget_row_result, include_steps)

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _FakeStore(),
        )
        result = CliRunner().invoke(answer_runs_cmd, ["--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["executed_ok_count"] == 0
        assert payload["executed_failed_count"] == 1
        # The failed row's long near-deadline duration must NOT pollute
        # the executed-ok-only page-scoped latency view.
        assert payload["executed_ok_avg_duration_ms"] is None
        assert payload["executed_ok_latency_buckets"] == {
            "under_5s": 0, "5s_to_30s": 0, "30s_to_2min": 0,
            "2min_to_5min": 0, "over_5min": 0,
        }

    def test_last_step_only_signal_without_steps_defaults_to_executed_ok(
        self, monkeypatch,
    ) -> None:
        """STATED RULE (``_row_is_failed``'s own docstring, RDR-196 .p1e
        review-fix round 2): when NONE of the three failure signals is
        observable -- ``final_text`` matches neither the "Error:" nor
        the budget-exhausted prefix, AND ``--steps`` was not requested
        so there is no per-step ``ok`` to check -- a row defaults to
        executed-OK. This is a documented, accepted blind spot (a
        failure whose ONLY real signal is a per-step ``ok=False`` is
        invisible without ``--steps``), not a silent one. This row's
        real underlying steps (never fetched here) WOULD show the last
        step's ``ok=False`` if ``--steps`` were passed -- proving the
        classification genuinely depends on data availability, not on
        this fixture happening to omit the signal by accident.
        """
        from nexus.commands.answer_runs import answer_runs_cmd

        ambiguous_row_result = {
            "rows": [{
                "id": 60, "question": "ambiguous", "plan_id": 6,
                "matched_confidence": 0.7, "step_count": 1,
                # Neither "Error:" nor the budget-exhausted prefix --
                # a hypothetical failure class with no text marker.
                "final_text": "partial synthesis, incomplete",
                "cost_usd": None, "duration_ms": 4_000,
                "created_at": "2026-08-21T01:01:00Z",
                # No "steps" key -- --steps was not requested. If it
                # HAD been, this row's one step would carry ok=False
                # (asserted separately below via the --steps variant).
            }],
            "total": 1, "oldest_created_at": "2026-08-21T01:01:00Z",
            "hit_count": 1, "fallback_count": 0,
            "avg_duration_ms": 4_000.0, "avg_cost_usd": None,
            "latency_buckets": {
                "under_5s": 1, "5s_to_30s": 0, "30s_to_2min": 0,
                "2min_to_5min": 0, "over_5min": 0,
            },
        }
        # The SAME logical row, but with its steps -- proving the
        # last-step ok=False signal genuinely exists and WOULD be
        # caught if --steps had fetched it.
        ambiguous_row_with_steps = {
            **ambiguous_row_result,
            "rows": [{
                **ambiguous_row_result["rows"][0],
                "steps": [{
                    "step_index": 0, "operator": "summarize", "source": "llm",
                    "model": None, "input_tokens": None, "output_tokens": None,
                    "cost_usd": None, "elapsed_ms": 4_000, "ok": False,
                    "bundled_steps": [],
                }],
            }],
            "steps_supported": True,
        }

        class _FakeStore:
            def __init__(self, with_steps: bool) -> None:
                self._with_steps = with_steps

            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                data = ambiguous_row_with_steps if self._with_steps else ambiguous_row_result
                return _steps_gated(data, include_steps)

        # Without --steps: no signal observable -> defaults to executed-ok
        # (the stated rule).
        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _FakeStore(with_steps=False),
        )
        result = CliRunner().invoke(answer_runs_cmd, ["--json"])
        payload = json.loads(result.stdout)
        assert payload["executed_ok_count"] == 1, (
            "with no observable failure signal, the row must default "
            "to executed-ok -- this IS the stated rule, not a bug"
        )
        assert payload["executed_failed_count"] == 0

        # With --steps: the last-step ok=False signal IS observable ->
        # correctly reclassified as executed-failed.
        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _FakeStore(with_steps=True),
        )
        result_with_steps = CliRunner().invoke(answer_runs_cmd, ["--steps", "--json"])
        payload_with_steps = json.loads(result_with_steps.stdout)
        assert payload_with_steps["executed_ok_count"] == 0
        assert payload_with_steps["executed_failed_count"] == 1


class TestExecutedOkLatencyBlock:
    """RDR-196 .p1e review-fix (S2, substantive-critic T2 [23111]): the
    engine's whole-set latency block must be explicitly labeled, and a
    NEW page-scoped executed-ok-only latency view must exist alongside
    it so a reader is never left to assume the whole-set numbers already
    describe 'normal run' behavior."""

    def test_json_carries_executed_ok_latency_fields(self, monkeypatch) -> None:
        from nexus.commands.answer_runs import answer_runs_cmd

        class _FakeStore:
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                return _steps_gated(_mixed_ok_failed_degenerate_result(), include_steps)

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _FakeStore(),
        )
        result = CliRunner().invoke(answer_runs_cmd, ["--json"])
        payload = json.loads(result.stdout)
        # Only the one genuine-success row (duration_ms=1_000) is
        # executed-ok in this fixture.
        assert payload["executed_ok_avg_duration_ms"] == pytest.approx(1_000.0)
        assert payload["executed_ok_latency_buckets"]["under_5s"] == 1
        assert sum(payload["executed_ok_latency_buckets"].values()) == 1
        # The whole-set engine block passes through completely unchanged.
        assert payload["avg_duration_ms"] == 3_300.0

    def test_human_output_labels_both_blocks_distinctly(self, monkeypatch) -> None:
        from nexus.commands.answer_runs import answer_runs_cmd

        class _FakeStore:
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                return _steps_gated(_mixed_ok_failed_degenerate_result(), include_steps)

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _FakeStore(),
        )
        result = CliRunner().invoke(answer_runs_cmd, [])
        assert result.exit_code == 0, result.output
        assert "engine aggregate: ALL rows incl. degenerate + failed" in result.output
        assert "executed-ok latency (page-scoped" in result.output

    def test_no_executed_ok_rows_omits_the_block(self, monkeypatch) -> None:
        """When every row is degenerate/failed, there is nothing to show
        -- the block must not print a misleading all-zero histogram."""
        from nexus.commands.answer_runs import answer_runs_cmd

        all_degenerate = dict(_EMPTY_RESULT)
        all_degenerate["rows"] = [{
            "id": 1, "question": "x", "plan_id": None,
            "matched_confidence": None, "step_count": 0,
            "final_text": "Planner error: x", "cost_usd": None,
            "duration_ms": 100, "created_at": "2026-08-21T00:00:00Z",
        }]
        all_degenerate["total"] = 1

        class _FakeStore:
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                return _steps_gated(all_degenerate, include_steps)

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _FakeStore(),
        )
        result = CliRunner().invoke(answer_runs_cmd, [])
        assert "executed-ok latency" not in result.output


class TestByPlanZeroSentinel:
    """RDR-196 .p1e review-fix (S3, substantive-critic T2 [23111]):
    plan_id=0 (the ad-hoc inline-planner sentinel) must get its own
    by_plan bucket, distinct from the real fallback (plan_id=None)
    bucket -- Python truthiness (`if plan_id else "fallback"`) used to
    collapse the two."""

    def test_plan_id_zero_gets_its_own_bucket_not_fallback(self, monkeypatch) -> None:
        from nexus.commands.answer_runs import answer_runs_cmd

        result_data = {
            "rows": [
                {
                    "id": 1, "question": "ad-hoc run", "plan_id": 0,
                    "matched_confidence": None, "step_count": 1,
                    "final_text": "ad-hoc answer", "cost_usd": 0.05,
                    "duration_ms": 2_000, "created_at": "2026-08-21T00:00:00Z",
                    "steps": [
                        {"step_index": 0, "operator": "summarize", "source": "llm",
                         "model": "claude-sonnet-5-20260101", "input_tokens": 50,
                         "output_tokens": 20, "cost_usd": 0.05, "elapsed_ms": 2_000,
                         "ok": True, "bundled_steps": []},
                    ],
                },
                {
                    "id": 2, "question": "genuine planner miss", "plan_id": None,
                    "matched_confidence": None, "step_count": 0,
                    "final_text": "Planner error: dispatch failed", "cost_usd": None,
                    "duration_ms": 300, "created_at": "2026-08-21T00:01:00Z",
                },
            ],
            "total": 2,
            "oldest_created_at": "2026-08-21T00:00:00Z",
            "hit_count": 0,
            "fallback_count": 2,
            "avg_duration_ms": 1_150.0,
            "avg_cost_usd": 0.05,
            "latency_buckets": {
                "under_5s": 2, "5s_to_30s": 0, "30s_to_2min": 0,
                "2min_to_5min": 0, "over_5min": 0,
            },
            "steps_supported": True,
        }

        class _FakeStore:
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                return _steps_gated(result_data, include_steps)

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _FakeStore(),
        )
        result = CliRunner().invoke(answer_runs_cmd, ["--steps", "--json"])
        payload = json.loads(result.stdout)
        by_plan = payload["step_breakdown"]["by_plan"]
        # plan_id=0 must appear as its OWN key ("0"), separate from
        # "fallback" -- and it must carry the ad-hoc row's real data
        # (only one row here has steps, so "fallback" never even gets a
        # by_plan entry -- the degenerate plan_id=None row has no steps).
        assert "0" in by_plan
        assert by_plan["0"]["run_count"] == 1
        assert by_plan["0"]["median_cost_usd"] == pytest.approx(0.05)
        assert "fallback" not in by_plan, (
            "the degenerate plan_id=None row carries no steps, so it "
            "never reaches _step_breakdown at all -- 'fallback' should "
            "not appear here just because plan_id=0 does"
        )


# ── RDR-196 .p1e: --steps per-step breakdown ─────────────────────────────────


def _steps_result(*, steps_supported: bool = True) -> dict:
    return {
        "rows": [
            {
                "id": 1, "question": "q1", "plan_id": 7,
                "matched_confidence": 0.9, "step_count": 2,
                "final_text": "answer", "cost_usd": 0.03,
                "duration_ms": 5_000, "created_at": "2026-08-20T00:00:00Z",
                "steps": [
                    {
                        "step_index": 0, "operator": "search", "source": "sql",
                        "model": None, "input_tokens": 0, "output_tokens": 0,
                        "cost_usd": 0.0, "elapsed_ms": 100, "ok": True,
                        "bundled_steps": [],
                    },
                    {
                        "step_index": 1, "operator": "summarize", "source": "llm",
                        "model": "claude-sonnet-5-20260101", "input_tokens": 100,
                        "output_tokens": 50, "cost_usd": 0.03, "elapsed_ms": 1_200,
                        "ok": True, "bundled_steps": [],
                    },
                ],
            },
        ],
        "total": 1,
        "oldest_created_at": "2026-08-20T00:00:00Z",
        "hit_count": 1,
        "fallback_count": 0,
        "avg_duration_ms": 5_000.0,
        "avg_cost_usd": 0.03,
        "latency_buckets": {
            "under_5s": 0, "5s_to_30s": 1, "30s_to_2min": 0,
            "2min_to_5min": 0, "over_5min": 0,
        },
        "steps_supported": steps_supported,
    }


class TestStepsFlag:
    def test_steps_flag_requests_include_steps(self, monkeypatch) -> None:
        from nexus.commands.answer_runs import answer_runs_cmd

        captured = {}

        class _CapturingStore:
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                captured["include_steps"] = include_steps
                return _steps_gated(_steps_result(), include_steps)

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _CapturingStore(),
        )
        result = CliRunner().invoke(answer_runs_cmd, ["--steps"])
        assert result.exit_code == 0, result.output
        assert captured["include_steps"] is True

    def test_without_steps_flag_include_steps_is_false(self, monkeypatch) -> None:
        from nexus.commands.answer_runs import answer_runs_cmd

        captured = {}

        class _CapturingStore:
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                captured["include_steps"] = include_steps
                return _steps_gated(dict(_EMPTY_RESULT), include_steps)

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _CapturingStore(),
        )
        CliRunner().invoke(answer_runs_cmd, [])
        assert captured["include_steps"] is False

    def test_json_step_breakdown_by_operator_and_source(self, monkeypatch) -> None:
        from nexus.commands.answer_runs import answer_runs_cmd

        class _FakeStore:
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                return _steps_gated(_steps_result(), include_steps)

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _FakeStore(),
        )
        result = CliRunner().invoke(answer_runs_cmd, ["--steps", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        breakdown = payload["step_breakdown"]
        assert breakdown["steps_supported"] is True
        assert breakdown["by_operator"]["search"]["count"] == 1
        assert breakdown["by_operator"]["search"]["total_cost_usd"] == 0.0
        assert breakdown["by_operator"]["summarize"]["total_cost_usd"] == pytest.approx(0.03)
        assert breakdown["by_source"]["sql"]["count"] == 1
        assert breakdown["by_source"]["llm"]["count"] == 1
        assert breakdown["by_plan"]["7"]["run_count"] == 1
        assert breakdown["by_plan"]["7"]["median_cost_usd"] == pytest.approx(0.03)
        assert breakdown["cost_consistency_violations"] == [], (
            "row cost_usd=0.03 == sum(step costs)=0.0+0.03 -- must agree "
            "within epsilon, not be flagged"
        )
        # RDR-196 .p1e review-fix (S1, substantive-critic T2 [23111]):
        # by_operator/by_source must ALSO carry median_cost_usd/
        # median_elapsed_ms, matching by_plan's existing shape (n=1
        # per bucket here, so median trivially equals the single value —
        # see test_by_operator_median_over_multiple_steps below for a
        # real multi-value median).
        assert breakdown["by_operator"]["search"]["median_cost_usd"] == pytest.approx(0.0)
        assert breakdown["by_operator"]["search"]["median_elapsed_ms"] == pytest.approx(100)
        assert breakdown["by_operator"]["summarize"]["median_cost_usd"] == pytest.approx(0.03)
        assert breakdown["by_source"]["llm"]["median_cost_usd"] == pytest.approx(0.03)

    def test_by_operator_median_over_multiple_steps(self, monkeypatch) -> None:
        """A real multi-value median (not just n=1) -- three 'summarize'
        steps across three rows with costs 0.01/0.02/0.09 -> median
        0.02, NOT the mean (0.04) and NOT the total (0.12)."""
        from nexus.commands.answer_runs import answer_runs_cmd

        def _row(row_id: int, cost: float, elapsed: int) -> dict:
            return {
                "id": row_id, "question": f"q{row_id}", "plan_id": 1,
                "matched_confidence": 0.9, "step_count": 1,
                "final_text": "answer", "cost_usd": cost,
                "duration_ms": elapsed, "created_at": "2026-08-21T00:00:00Z",
                "steps": [
                    {"step_index": 0, "operator": "summarize", "source": "llm",
                     "model": "claude-sonnet-5-20260101", "input_tokens": 10,
                     "output_tokens": 5, "cost_usd": cost, "elapsed_ms": elapsed,
                     "ok": True, "bundled_steps": []},
                ],
            }

        result_data = {
            "rows": [_row(1, 0.01, 100), _row(2, 0.09, 900), _row(3, 0.02, 200)],
            "total": 3, "oldest_created_at": "2026-08-21T00:00:00Z",
            "hit_count": 3, "fallback_count": 0,
            "avg_duration_ms": 400.0, "avg_cost_usd": 0.04,
            "latency_buckets": {
                "under_5s": 3, "5s_to_30s": 0, "30s_to_2min": 0,
                "2min_to_5min": 0, "over_5min": 0,
            },
            "steps_supported": True,
        }

        class _FakeStore:
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                return _steps_gated(result_data, include_steps)

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _FakeStore(),
        )
        result = CliRunner().invoke(answer_runs_cmd, ["--steps", "--json"])
        payload = json.loads(result.stdout)
        agg = payload["step_breakdown"]["by_operator"]["summarize"]
        assert agg["count"] == 3
        assert agg["total_cost_usd"] == pytest.approx(0.12)
        assert agg["median_cost_usd"] == pytest.approx(0.02)
        assert agg["median_elapsed_ms"] == pytest.approx(200)

    def test_json_cost_consistency_violation_flagged(self, monkeypatch) -> None:
        from nexus.commands.answer_runs import answer_runs_cmd

        mismatched = _steps_result()
        # Row cost_usd says 0.03 but the steps only sum to 0.0 -- a
        # genuine drop (e.g. the run-cost write happened but one step's
        # write silently failed) must be flagged, not silently accepted.
        mismatched["rows"][0]["cost_usd"] = 0.50

        class _FakeStore:
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                return _steps_gated(mismatched, include_steps)

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _FakeStore(),
        )
        result = CliRunner().invoke(answer_runs_cmd, ["--steps", "--json"])
        payload = json.loads(result.stdout)
        violations = payload["step_breakdown"]["cost_consistency_violations"]
        assert len(violations) == 1
        assert violations[0]["id"] == 1
        assert violations[0]["run_cost_usd"] == 0.50
        assert violations[0]["step_cost_sum_usd"] == pytest.approx(0.03)

    def test_steps_unsupported_by_engine_degrades_honestly(self, monkeypatch) -> None:
        """--steps against an engine that predates the read route must
        say so explicitly, never render an empty breakdown that looks
        identical to 'no steps were ever recorded'."""
        from nexus.commands.answer_runs import answer_runs_cmd

        class _OldEngineStore:
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                result = _populated_result()
                result["steps_supported"] = False
                return result

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _OldEngineStore(),
        )
        result = CliRunner().invoke(answer_runs_cmd, ["--steps"])
        assert result.exit_code == 0, result.output
        assert "does not support include_steps" in result.output

        json_result = CliRunner().invoke(answer_runs_cmd, ["--steps", "--json"])
        json_payload = json.loads(json_result.stdout)
        assert json_payload["step_breakdown"] == {"steps_supported": False}

    def test_unknown_step_cost_excluded_from_sum_but_count_shown(
        self, monkeypatch,
    ) -> None:
        """None cost renders as 'unknown' -- excluded from the sum, but
        its count is still surfaced, never silently dropped."""
        from nexus.commands.answer_runs import answer_runs_cmd

        result_data = _steps_result()
        # Both steps' costs unknown -> sum(steps) is also None (never a
        # fabricated 0.0 sum-of-nothing) -- so run.cost_usd=None agrees
        # trivially with step_sum=None below.
        result_data["rows"][0]["steps"][0]["cost_usd"] = None
        result_data["rows"][0]["steps"][1]["cost_usd"] = None
        result_data["rows"][0]["cost_usd"] = None

        class _FakeStore:
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                return _steps_gated(result_data, include_steps)

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _FakeStore(),
        )
        result = CliRunner().invoke(answer_runs_cmd, ["--steps", "--json"])
        payload = json.loads(result.stdout)
        by_op = payload["step_breakdown"]["by_operator"]
        assert by_op["summarize"]["unknown_cost_count"] == 1
        assert by_op["summarize"]["known_cost_count"] == 0
        assert by_op["summarize"]["total_cost_usd"] == 0.0
        assert payload["step_breakdown"]["cost_consistency_violations"] == [], (
            "both run.cost_usd and sum(steps) are None -- unknown vs "
            "unknown agrees trivially, not a violation"
        )


class TestPredictedCostColumn:
    """RDR-196 Phase 3 Step 1 (nexus-nyry9.20, code-review round 1
    dormancy item #2): --steps' by_plan entries carry a READ-TIME
    predicted cost (nexus.plans.cost_estimate.estimate_plan_cost against
    a price table built from the SAME telemetry store), computed fresh
    on every call and never persisted, alongside the recorded actual
    median_cost_usd."""

    def test_predicted_cost_computed_from_plan_shape(self, monkeypatch) -> None:
        from nexus.commands.answer_runs import answer_runs_cmd
        from nexus.plans.cost_estimate import STATIC_FALLBACK_COST_USD

        class _FakeStore:
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                return _steps_gated(_steps_result(), include_steps)

        class _FakePlanLibrary:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def get_plan(self, plan_id: int):
                assert plan_id == 7
                # A single strong-tier LLM operator step -> static
                # fallback (the 2 recorded "summarize" samples in
                # _steps_result are below MIN_HISTORY_SAMPLES, so history
                # doesn't engage).
                return {"id": 7, "plan_json": json.dumps({
                    "steps": [{"tool": "search"}, {"tool": "operator_summarize"}],
                })}

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _FakeStore(),
        )
        monkeypatch.setattr(
            "nexus.db.t2.http_plan_library.HttpPlanLibrary",
            _FakePlanLibrary,
        )
        result = CliRunner().invoke(answer_runs_cmd, ["--steps", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        entry = payload["step_breakdown"]["by_plan"]["7"]
        assert entry["predicted_cost_usd"] == pytest.approx(STATIC_FALLBACK_COST_USD)
        assert "static-fallback" in entry["predicted_basis"]
        # Estimate and actual sit side by side, not merged.
        assert entry["median_cost_usd"] == pytest.approx(0.03)

    def test_human_output_renders_predicted_cost_beside_actual(self, monkeypatch) -> None:
        from nexus.commands.answer_runs import answer_runs_cmd

        class _FakeStore:
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                return _steps_gated(_steps_result(), include_steps)

        class _FakePlanLibrary:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def get_plan(self, plan_id: int):
                return {"id": 7, "plan_json": json.dumps({
                    "steps": [{"tool": "search"}, {"tool": "operator_summarize"}],
                })}

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _FakeStore(),
        )
        monkeypatch.setattr(
            "nexus.db.t2.http_plan_library.HttpPlanLibrary",
            _FakePlanLibrary,
        )
        result = CliRunner().invoke(answer_runs_cmd, ["--steps"])
        assert result.exit_code == 0, result.output
        assert "predicted_cost_usd=" in result.output

    def test_plan_library_lookup_failure_degrades_to_none_not_a_crash(
        self, monkeypatch,
    ) -> None:
        from nexus.commands.answer_runs import answer_runs_cmd

        class _FakeStore:
            def query_nx_answer_runs(self, *, since=None, limit=20, include_steps=False):
                return _steps_gated(_steps_result(), include_steps)

        class _BrokenPlanLibrary:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def get_plan(self, plan_id: int):
                raise RuntimeError("plan library unreachable")

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            lambda: _FakeStore(),
        )
        monkeypatch.setattr(
            "nexus.db.t2.http_plan_library.HttpPlanLibrary",
            _BrokenPlanLibrary,
        )
        result = CliRunner().invoke(answer_runs_cmd, ["--steps", "--json"])
        assert result.exit_code == 0, result.output
        entry = json.loads(result.stdout)["step_breakdown"]["by_plan"]["7"]
        assert entry["predicted_cost_usd"] is None
        assert entry["predicted_basis"] == "unavailable"

    def test_add_predicted_costs_skips_fallback_key_directly(self) -> None:
        # Direct unit test of the helper (not through the CLI): the
        # "fallback" bucket (genuine planner-error miss, no plan_id at
        # all) has no plan JSON to fetch -- it must be named as such, not
        # silently attempted against the plan library.
        from nexus.commands.answer_runs import _add_predicted_costs

        by_plan = {"fallback": {"run_count": 1, "median_cost_usd": None, "median_elapsed_ms": None}}

        class _UnusedTelemetryStore:
            def query_nx_answer_runs(self, *, limit: int, include_steps: bool) -> dict:
                return {"rows": [], "steps_supported": True}

        _add_predicted_costs(by_plan, _UnusedTelemetryStore())
        assert by_plan["fallback"]["predicted_cost_usd"] is None
        assert by_plan["fallback"]["predicted_basis"] == "ad-hoc-no-plan-json"

    def test_add_predicted_costs_plan_not_found(self, monkeypatch) -> None:
        from nexus.commands.answer_runs import _add_predicted_costs

        by_plan = {"999": {"run_count": 1, "median_cost_usd": 0.1, "median_elapsed_ms": 100}}

        class _NoHistoryStore:
            def query_nx_answer_runs(self, *, limit: int, include_steps: bool) -> dict:
                return {"rows": [], "steps_supported": True}

        class _EmptyPlanLibrary:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def get_plan(self, plan_id: int):
                return None

        monkeypatch.setattr(
            "nexus.db.t2.http_plan_library.HttpPlanLibrary", _EmptyPlanLibrary,
        )
        _add_predicted_costs(by_plan, _NoHistoryStore())
        assert by_plan["999"]["predicted_cost_usd"] is None
        assert by_plan["999"]["predicted_basis"] == "plan-not-found"
