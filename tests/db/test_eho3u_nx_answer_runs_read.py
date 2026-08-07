# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx_answer_runs is READABLE (nexus-eho3u).

REPLACES the write-only gap the bead documents: every ``nx_answer`` call
wrote a row via ``POST /v1/telemetry/nx_answer_runs/record`` and nothing
ever read one back — ``import_nx_answer_run`` is ETL, not a live reader.
``GET /v1/telemetry/nx_answer_runs/query`` (TelemetryRepository.
queryNxAnswerRuns) is that read surface; this proves the full write -> read
loop against a real engine, same shape as tests/db/test_onjvy_read_routes.py
(gap 2, hook_failures) — the closest prior instance of exactly this bug
class (a telemetry table with a record route and no query route).

Requires an engine carrying the nexus-eho3u route, which the suite's
self-provisioned substrate builds from this checkout's own service/ tree.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from nexus.db.t2 import T2Database


@pytest.fixture()
def db(tmp_path: Path) -> Any:
    database = T2Database(tmp_path / "memory.db")
    yield database
    database.close()


def _unique_question(label: str) -> str:
    """A question text disjoint from every other test's and every other
    run of this test — nx_answer_runs has no per-test isolation key other
    than the row content itself, so aggregates over a shared tenant would
    pick up rows other tests (or a prior run in the same engine) wrote."""
    return f"eho3u-{label}-{time.time_ns()}"


# ── the write -> read loop ───────────────────────────────────────────────


def test_run_round_trips_with_its_fields(db: T2Database) -> None:
    """A recorded run comes back with the fields an operator needs."""
    question = _unique_question("roundtrip")
    db.telemetry.record_nx_answer_run(
        question=question, plan_id=42, matched_confidence=0.87,
        step_count=2, final_text="the answer", cost_usd=0.003,
        duration_ms=1_500,
    )

    result = db.telemetry.query_nx_answer_runs(limit=50)

    mine = [r for r in result["rows"] if r["question"] == question]
    assert len(mine) == 1, "the recorded run must be readable"
    row = mine[0]
    assert row["plan_id"] == 42
    assert row["matched_confidence"] == pytest.approx(0.87)
    assert row["step_count"] == 2
    assert row["final_text"] == "the answer"
    assert row["cost_usd"] == pytest.approx(0.003)
    assert row["duration_ms"] == 1_500
    assert row["created_at"].endswith("Z")


def test_hit_vs_fallback_split_reflects_plan_id(db: T2Database) -> None:
    """plan_id set = plan-match hit; plan_id null = inline-planner fallback
    — the exact figure the shakedown playbook's §4.5 baseline wants."""
    tag = _unique_question("hitfallback")
    db.telemetry.record_nx_answer_run(
        question=f"{tag}-hit", plan_id=1, matched_confidence=0.9,
        step_count=1, final_text="x", cost_usd=0.0, duration_ms=1_000,
    )
    db.telemetry.record_nx_answer_run(
        question=f"{tag}-fallback", plan_id=None, matched_confidence=None,
        step_count=0, final_text="", cost_usd=0.0, duration_ms=1_000,
    )

    result = db.telemetry.query_nx_answer_runs(limit=200)
    mine = [r for r in result["rows"] if tag in r["question"]]
    assert len(mine) == 2, "both the hit and the fallback rows must be readable"
    hit = next(r for r in mine if r["question"].endswith("-hit"))
    fallback = next(r for r in mine if r["question"].endswith("-fallback"))
    assert hit["plan_id"] == 1
    assert fallback["plan_id"] is None
    # hit_count/fallback_count are computed over the WHOLE tenant, not just
    # this test's rows, so assert the floor rather than an exact count.
    assert result["hit_count"] >= 1
    assert result["fallback_count"] >= 1


def test_plan_id_zero_sentinel_is_fallback_not_hit(db: T2Database) -> None:
    """nexus-eho3u review fix, real-engine seed via the RECORD route (not
    import): plan_id=0 is the synthetic ad-hoc Match sentinel every
    SUCCESSFUL inline-planner run carries (core.py::_nx_answer_plan_miss's
    ``Match(plan_id=0, name="ad-hoc", ...)``) — plans.id is BIGSERIAL, so 0
    can never be a real matched plan. An earlier `plan_id IS NOT NULL`
    predicate counted it as a HIT, inverting the plan-match-rate metric.

    hit_count/fallback_count are tenant-wide aggregates (other tests in
    this file and this session contribute rows), so this asserts the
    BEFORE/AFTER DELTA rather than an absolute count — a delta that is
    exact regardless of what else has landed in the tenant. Under the old
    predicate the hit delta would read 2 (the real hit AND the sentinel)
    and the fallback delta would read 1; this is the kill control.
    """
    tag = _unique_question("sentinel")
    before = db.telemetry.query_nx_answer_runs(limit=1)

    db.telemetry.record_nx_answer_run(
        question=f"{tag}-hit", plan_id=11, matched_confidence=0.9,
        step_count=1, final_text="answer", cost_usd=0.001, duration_ms=1_000,
    )
    db.telemetry.record_nx_answer_run(
        question=f"{tag}-sentinel", plan_id=0, matched_confidence=None,
        step_count=2, final_text="ad-hoc answer", cost_usd=0.002, duration_ms=2_000,
    )
    db.telemetry.record_nx_answer_run(
        question=f"{tag}-fallback", plan_id=None, matched_confidence=None,
        step_count=0, final_text="Planner error: x", cost_usd=0.0, duration_ms=3_000,
    )

    after = db.telemetry.query_nx_answer_runs(limit=200)
    mine = [r for r in after["rows"] if tag in r["question"]]
    assert len(mine) == 3, "all three rows (hit/sentinel/fallback) must be readable"
    sentinel_row = next(r for r in mine if r["question"].endswith("-sentinel"))
    assert sentinel_row["plan_id"] == 0, "the sentinel's plan_id=0 must survive the round trip verbatim"

    hit_delta = after["hit_count"] - before["hit_count"]
    fallback_delta = after["fallback_count"] - before["fallback_count"]
    assert hit_delta == 1, (
        "only the real matched plan (plan_id=11) increments hit_count — "
        f"got a delta of {hit_delta}"
    )
    assert fallback_delta == 2, (
        "plan_id=0 (sentinel) AND plan_id=None (genuine miss) both increment "
        f"fallback_count — got a delta of {fallback_delta}"
    )


def test_limit_caps_page_aggregates_stay_exact(db: T2Database) -> None:
    """total is computed over the whole filtered set, not the returned page
    — mirrors hookFailures_read_aggregatesIgnoreThePageLimit."""
    tag = _unique_question("cap")
    for i in range(3):
        db.telemetry.record_nx_answer_run(
            question=f"{tag}-{i}", plan_id=None, matched_confidence=None,
            step_count=0, final_text="", cost_usd=0.0, duration_ms=1_000,
        )

    capped = db.telemetry.query_nx_answer_runs(limit=1)
    assert len(capped["rows"]) == 1, "the page honours limit"
    assert capped["total"] >= 3, "the total does not shrink to the page size"


def test_since_filter_excludes_rows_recorded_before_it(db: T2Database) -> None:
    """`since` bounds the read to rows at or after the cutoff.

    Uses ``import_nx_answer_run`` for EXPLICIT created_at control rather
    than comparing against a client-captured wall-clock timestamp: the
    live-record path always stamps the ENGINE's own now(), and a
    since-just-before-the-call assertion is vulnerable to any client/server
    clock skew (measured non-hypothetically while developing this test —
    a few hundred ms of skew between this process and the engine process
    was enough to make a "just recorded" row read as older than a
    ``datetime.now()`` captured microseconds earlier on the client side).
    Explicit historical timestamps sidestep that entirely, matching the
    engine-side ``queryNxAnswerRuns_sinceFilterExcludesOlderRows`` test.
    """
    tag = _unique_question("since")
    db.telemetry.import_nx_answer_run(
        question=f"{tag}-old", plan_id=None, matched_confidence=None,
        step_count=0, final_text="", cost_usd=0.0, duration_ms=1_000,
        created_at="2020-01-01T00:00:00Z",
    )
    db.telemetry.import_nx_answer_run(
        question=f"{tag}-new", plan_id=None, matched_confidence=None,
        step_count=0, final_text="", cost_usd=0.0, duration_ms=1_000,
        created_at="2099-01-01T00:00:00Z",
    )

    recent = db.telemetry.query_nx_answer_runs(
        since="2030-01-01T00:00:00Z", limit=200,
    )
    recent_questions = {r["question"] for r in recent["rows"]}
    assert f"{tag}-new" in recent_questions, (
        "a row newer than `since` must appear in the since-filtered read"
    )
    assert f"{tag}-old" not in recent_questions, (
        "a row older than `since` must be excluded"
    )

    unbounded = db.telemetry.query_nx_answer_runs(limit=200)
    unbounded_questions = {r["question"] for r in unbounded["rows"]}
    assert {f"{tag}-old", f"{tag}-new"} <= unbounded_questions, (
        "no since filter means both rows are visible"
    )


def test_latency_buckets_and_averages_are_present(db: T2Database) -> None:
    """The §4.5 shakedown-baseline shape (fixed latency buckets, averages)
    crosses the wire as real numbers, not an omitted or null structure."""
    tag = _unique_question("buckets")
    db.telemetry.record_nx_answer_run(
        question=tag, plan_id=None, matched_confidence=None,
        step_count=0, final_text="", cost_usd=0.01, duration_ms=2_000,
    )

    result = db.telemetry.query_nx_answer_runs(limit=1)
    assert result["latency_buckets"]["under_5s"] >= 1
    assert set(result["latency_buckets"]) == {
        "under_5s", "5s_to_30s", "30s_to_2min", "2min_to_5min", "over_5min",
    }
    assert result["avg_duration_ms"] is not None
    assert result["avg_cost_usd"] is not None


def test_empty_tenant_shaped_query_returns_zeroed_not_error(db: T2Database) -> None:
    """A since cutoff that matches nothing returns a valid zeroed structure
    (never an exception, never an omitted key)."""
    result = db.telemetry.query_nx_answer_runs(
        since="2099-01-01T00:00:00Z", limit=10,
    )
    assert result["total"] == 0
    assert result["rows"] == []
    assert result["hit_count"] == 0
    assert result["fallback_count"] == 0
    assert result["avg_duration_ms"] is None
    assert result["avg_cost_usd"] is None


# ── the consumer: `nx answer-runs` surfaces runs in service mode ────────


def test_answer_runs_cli_reports_recorded_runs(
    db: T2Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`nx answer-runs` reads real service-side rows through the same
    HttpTelemetryStore this test seeds through directly."""
    from click.testing import CliRunner

    from nexus.commands.answer_runs import answer_runs_cmd

    tag = _unique_question("cli")
    db.telemetry.record_nx_answer_run(
        question=tag, plan_id=3, matched_confidence=0.5,
        step_count=1, final_text="cli answer", cost_usd=0.0, duration_ms=1_000,
    )

    runner = CliRunner()
    result = runner.invoke(answer_runs_cmd, ["--limit", "200"])

    assert result.exit_code == 0, result.output
    assert "plan-match hit" in result.output
    assert "inline-planner fallback" in result.output
    assert tag[:60] in result.output or tag in result.output
