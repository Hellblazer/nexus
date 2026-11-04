# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-189 D1 / nexus-z1d0l.2.1: grown plans expire on disuse, survive with use.

``nx_answer`` grows a plan on a plan miss: ``save_plan(scope="personal",
tags="ad-hoc,grown", ttl=plans.ad_hoc_ttl)``. The engine drops a plan once
``now() - coalesce(last_used, created_at)`` exceeds ``ttl_days`` (PG clock,
``PlanRepository.notExpiredCondition``), and only the run-start bump (reached
through ``nx_answer`` on a plan-library match) stamps ``last_used``.

This file pins that chain end to end against the live engine. Java covers the
predicate alone (``PlanRepositoryTest.ttl_*``); the Python mock suite
(``test_rdr_084_plan_grow.py``) covers the call shape. Neither proves the
grown row carries a TTL the engine enforces, nor that a matched ``nx_answer``
run resets the disuse clock.

Clock strategy: expiry runs on the PG clock, which a Python-side freeze cannot
drive. Ages are fixed offsets from one captured instant, landed through the
fidelity-import surface (``import_plan`` writes ``created_at`` and
``last_used`` verbatim), with margins far from the 30-day boundary (400d /
200d against 30d, 25d and 1d inside it) so PG/Python skew cannot flip a result.
Storage is real; only the LLM/planner seams are patched.

What this does NOT drive: the TTL boundary itself. The PG clock cannot be moved, so no test here lets a row
age across 30 days. Survival is carried by ``last_used`` and ``use_count`` after a matched ``nx_answer`` run
(the disuse clock resets), and expiry by rows whose disuse age is already far past the TTL. The boundary
crossing (a row at 29d kept, at 31d dropped) rests on the Java ``PlanRepositoryTest.ttl_*`` tests.

Each test is one journey in one tenant (the python_unit tenant ceiling counts tests that mint one).
"""
from __future__ import annotations

import copy
import json
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.config import _DEFAULTS
from nexus.db.t2 import T2Database
from nexus.db.t2.http_plan_library import HttpPlanLibrary
from nexus.plans.match import Match

pytestmark = pytest.mark.t2_tenant

#: The reference instant, captured once. Every age below is an offset from it.
T0 = datetime.now(UTC)

_PLAN_JSON = json.dumps({
    "steps": [
        {"tool": "search", "args": {"query": "$intent", "corpus": "knowledge"}},
        {"tool": "summarize", "args": {"content": "$step1.ids"}},
    ],
})


def _iso(delta_days: float) -> str:
    return (T0 - timedelta(days=delta_days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plan_run_success() -> MagicMock:
    result = MagicMock()
    result.steps = [{"text": "the answer is 42"}]
    return result


def _match(plan_id: int, question: str) -> Match:
    return Match(
        plan_id=plan_id,
        name="ad-hoc" if plan_id == 0 else "grown-plan",
        description=question,
        confidence=None if plan_id == 0 else 0.9,
        dimensions={},
        tags="ad-hoc" if plan_id == 0 else "ad-hoc,grown",
        plan_json=_PLAN_JSON,
        required_bindings=["intent"],
        optional_bindings=[],
        default_bindings={"intent": question},
        parent_dims=None,
    )


async def _drive_nx_answer(
    db_path, question: str, *, plan_id: int, ttl_days: int | None = None, run_result: MagicMock | None = None,
) -> str:
    """Run the real ``nx_answer`` over real T2, patching only the planner/LLM seams.

    ``plan_id == 0`` takes the plan-miss branch (the grow path); a non-zero
    ``plan_id`` makes ``plan_match`` return that library row (the use path).
    ``load_config`` is pinned to the shipped defaults so the user's own config, ``.nexus.yml`` and ``NX_*``
    environment cannot move the TTL under test; ``ttl_days`` overrides ``plans.ad_hoc_ttl`` on top of that.
    ``run_result`` replaces the successful plan run (a run whose retrieval found nothing, for example).
    """
    from nexus.mcp.core import nx_answer

    config = copy.deepcopy(_DEFAULTS)
    if ttl_days is not None:
        config["plans"]["ad_hoc_ttl"] = ttl_days
    matches = [] if plan_id == 0 else [_match(plan_id, question)]
    with ExitStack() as stack:
        stack.enter_context(patch("nexus.mcp_infra.default_db_path", return_value=db_path))
        stack.enter_context(patch("nexus.plans.matcher.plan_match", return_value=matches))
        stack.enter_context(patch(
            "nexus.mcp.core._nx_answer_plan_miss",
            AsyncMock(return_value=_match(0, question)),
        ))
        stack.enter_context(patch(
            "nexus.plans.runner.plan_run", AsyncMock(return_value=run_result or _plan_run_success()),
        ))
        stack.enter_context(patch(
            "nexus.mcp.core._generalize_grown_match_description", AsyncMock(return_value=None),
        ))
        stack.enter_context(patch("nexus.mcp.core.scratch", return_value="ok"))
        stack.enter_context(patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None))
        stack.enter_context(patch("nexus.mcp.core.load_config", return_value=config))
        return await nx_answer(question=question)


def _grown_row(db: T2Database, question: str) -> dict:
    rows = [r for r in db.plans.list_active_plans(outcome="success") if r.get("query") == question]
    assert len(rows) == 1, f"expected exactly one grown row for {question!r}, got {len(rows)}"
    return rows[0]


def _importer(db: T2Database, grown: dict):
    """Import rows shaped like *grown* (scope, tags, ttl, verb), backdated by fixed offsets.

    ``permanent=True`` imports the row with ``ttl=None`` instead of the grown row's TTL.
    """
    def _import(
        query: str, *, created_days: float, last_used_days: float | None, permanent: bool = False,
    ) -> int:
        return db.plans.import_plan(
            project="",
            query=query,
            plan_json=grown["plan_json"],
            outcome="success",
            tags=grown["tags"],
            scope=grown["scope"],
            ttl=None if permanent else grown["ttl"],
            verb=grown.get("verb"),  # the engine rejects a NULL verb (409), as the grown row's is set
            # fts_vector is built from match_text, so a row is findable by search_plans only if it carries one.
            match_text=query,
            created_at=_iso(created_days),
            last_used=None if last_used_days is None else _iso(last_used_days),
        )
    return _import


def _active_queries(db: T2Database) -> dict[str, set[str]]:
    """The query text of every row each of the three TTL-filtered readers returns."""
    return {
        "list_active_plans": {r["query"] for r in db.plans.list_active_plans(outcome="success")},
        "list_plans": {r["query"] for r in db.plans.list_plans(limit=100)},
        # search_plans matches on words; every probe query below carries "ttlprobe".
        "search_plans": {r["query"] for r in db.plans.search_plans("ttlprobe", limit=50)},
    }


def _get(db: T2Database, plan_id: int) -> dict:
    row = db.plans.get_plan(plan_id)
    assert row is not None, f"plan {plan_id} is gone"
    return row


class TestGrownPlanCarriesTheTtl:
    @pytest.mark.asyncio
    async def test_plan_miss_grows_a_personal_ttl_row(self, tmp_path):
        db_path = tmp_path / "memory.db"
        default_ttl = _DEFAULTS["plans"]["ad_hoc_ttl"]
        assert default_ttl == 30, "the shipped plans.ad_hoc_ttl default is 30 days"

        # Every save_plan the grow path attempts, recorded while the real method still runs.
        attempted: list[str] = []
        real_save = HttpPlanLibrary.save_plan

        def spy(self, *args, **kwargs):
            attempted.append(str(kwargs.get("query")))
            return real_save(self, *args, **kwargs)

        with patch.object(HttpPlanLibrary, "save_plan", spy):
            await _drive_nx_answer(db_path, "ttlprobe grow default ttl", plan_id=0)
            await _drive_nx_answer(db_path, "ttlprobe grow override ttl", plan_id=0, ttl_days=7)
            await _drive_nx_answer(db_path, "ttlprobe grow disabled", plan_id=0, ttl_days=0)

        with T2Database(db_path) as db:
            default_row = _grown_row(db, "ttlprobe grow default ttl")
            override_row = _grown_row(db, "ttlprobe grow override ttl")
            # list_plans is TTL-filtered, so an empty answer for the ttl=0 question proves nothing on its own
            # (and the engine's CHECK ttl_days > 0 would reject a ttl=0 row anyway, which grow swallows).
            # The opt-out is therefore pinned at the call: grow never attempts the save. The two grown
            # questions above are the control that the spy sees a save when growth is on.
            disabled_rows = [r for r in db.plans.list_plans(limit=100) if r["query"] == "ttlprobe grow disabled"]

        tags = (default_row["tags"] or "").split(",")
        assert default_row["scope"] == "personal"
        assert "ad-hoc" in tags and "grown" in tags
        assert default_row["ttl"] == default_ttl
        # Fresh growth stamps created_at and leaves the row unused.
        assert not default_row.get("last_used")
        assert default_row.get("use_count", 0) == 0
        assert override_row["ttl"] == 7, "plans.ad_hoc_ttl overrides the default"
        assert "ttlprobe grow default ttl" in attempted and "ttlprobe grow override ttl" in attempted
        assert "ttlprobe grow disabled" not in attempted, "ttl 0 must not attempt a save"
        assert disabled_rows == []


class TestGrownPlanExpiresOnDisuse:
    @pytest.mark.asyncio
    async def test_unused_rows_past_ttl_drop_out_of_every_reader(self, tmp_path):
        db_path = tmp_path / "memory.db"
        grown_q = "ttlprobe shape source"
        await _drive_nx_answer(db_path, grown_q, plan_id=0)

        with T2Database(db_path) as db:
            imp = _importer(db, _grown_row(db, grown_q))
            # created 400d ago, never used: disuse age 400d > 30d.
            imp("ttlprobe never-used expired", created_days=400, last_used_days=None)
            # created 400d ago, last used 200d ago: disuse age 200d > 30d.
            imp("ttlprobe long-unused expired", created_days=400, last_used_days=200)
            # Controls proving the readers do return in-TTL rows, so absence above is expiry and not a
            # broken query: unused but young (1d), and old but used recently (25d disuse < 30d).
            imp("ttlprobe young unused kept", created_days=1, last_used_days=None)
            imp("ttlprobe old recently-used kept", created_days=400, last_used_days=25)
            # A permanent row (ttl NULL) is never expired, however old and unused.
            imp("ttlprobe permanent old", created_days=400, last_used_days=None, permanent=True)

            seen = _active_queries(db)

        for reader, queries in seen.items():
            assert "ttlprobe never-used expired" not in queries, reader
            assert "ttlprobe long-unused expired" not in queries, reader
            assert "ttlprobe young unused kept" in queries, reader
            assert "ttlprobe old recently-used kept" in queries, reader
            assert "ttlprobe permanent old" in queries, reader
            assert grown_q in queries, f"{reader}: the freshly grown row must still be active"


class TestGrownPlanSurvivesWithUse:
    @pytest.mark.asyncio
    async def test_a_matched_nx_answer_run_resets_the_disuse_clock(self, tmp_path):
        db_path = tmp_path / "memory.db"
        grown_q = "ttlprobe use shape source"
        await _drive_nx_answer(db_path, grown_q, plan_id=0)

        with T2Database(db_path) as db:
            imp = _importer(db, _grown_row(db, grown_q))
            # Both: created 400d ago (10x the TTL), last used 25d ago. Identical, so the only difference
            # between them is that one gets used through nx_answer below.
            used_id = imp("ttlprobe used by nx_answer", created_days=400, last_used_days=25)
            control_id = imp("ttlprobe control left alone", created_days=400, last_used_days=25)
            before_used = _get(db, used_id)
            before_control = _get(db, control_id)
            assert before_used["last_used"] == before_control["last_used"]
            assert before_used["use_count"] == before_control["use_count"] == 0

        # The run goes through nx_answer on a library match, so whichever run-start path the live
        # engine takes (composite telemetry write or the early increment_run_started) is the one tested.
        answer = await _drive_nx_answer(db_path, "ttlprobe used by nx_answer", plan_id=used_id)
        assert "42" in answer

        with T2Database(db_path) as db:
            used = _get(db, used_id)
            control = _get(db, control_id)

        assert used["use_count"] == 1
        assert control["use_count"] == 0
        assert control["last_used"] == before_control["last_used"], "an unused row's clock must not move"
        # The used row's disuse clock was reset to ~now (PG clock), nowhere near its 25d-old value. That
        # reset, not the row's 400d-old created_at, is what keeps it inside the TTL from here on.
        used_last = datetime.fromisoformat(str(used["last_used"]).replace("Z", "+00:00"))
        assert abs((datetime.now(UTC) - used_last).total_seconds()) < 600, used["last_used"]
        assert used["created_at"] == before_used["created_at"]
        assert used["ttl"] == 30

    # nexus-yr2um: the success test above drives a scored, successful run only. A failed run
    # (success=False) and an unscored one (success=None) reset the disuse clock too, through
    # different run-start branches: a verdict rides the composite record write
    # (TelemetryRepository.recordNxAnswerRunComplete -> incrementRunStartedIn), while a verdictless
    # run takes the per-call record path, where _nx_answer_ensure_run_started issues
    # increment_run_started. The spies record which branch fired; the row shows the reset.
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("label", "steps", "failures", "via_composite"),
        [
            # A retrieval step that ran and found nothing: the empty-retrieval guard records a failure.
            ("failed", [{"ids": [], "tumblers": []}, {"text": ""}], 1, True),
            # Every retrieval step skipped for a scope conflict: the caller's error, no verdict counted.
            ("unscored", [{"ids": [], "scope_conflict": {"plan_corpus": "code", "step_index": 0}}, {"text": ""}], 0, False),
        ],
    )
    async def test_failed_and_unscored_runs_reset_the_disuse_clock(
        self, tmp_path, label, steps, failures, via_composite,
    ):
        from nexus.db.t2.http_telemetry_store import HttpTelemetryStore

        db_path = tmp_path / "memory.db"
        grown_q = f"ttlprobe {label} shape source"
        await _drive_nx_answer(db_path, grown_q, plan_id=0)

        with T2Database(db_path) as db:
            imp = _importer(db, _grown_row(db, grown_q))
            used_id = imp(f"ttlprobe {label} run", created_days=400, last_used_days=25)
            before = _get(db, used_id)

        result = MagicMock()
        result.steps = steps
        real_started = HttpPlanLibrary.increment_run_started
        real_complete = HttpTelemetryStore.record_nx_answer_run_complete
        started: list[int] = []
        completed: list[object] = []

        def _spy_started(self, plan_id):
            started.append(plan_id)
            return real_started(self, plan_id)

        def _spy_complete(self, *args, **kwargs):
            completed.append(kwargs.get("plan_id"))
            return real_complete(self, *args, **kwargs)

        with patch.object(HttpPlanLibrary, "increment_run_started", _spy_started), \
                patch.object(HttpTelemetryStore, "record_nx_answer_run_complete", _spy_complete):
            await _drive_nx_answer(db_path, f"ttlprobe {label} run", plan_id=used_id, run_result=result)

        with T2Database(db_path) as db:
            used = _get(db, used_id)

        if via_composite:
            assert completed == [used_id] and started == [], (completed, started)
        else:
            assert started == [used_id] and completed == [], (completed, started)
        assert used["use_count"] == before["use_count"] + 1
        assert used["failure_count"] == before["failure_count"] + failures
        assert used["success_count"] == before["success_count"]
        used_last = datetime.fromisoformat(str(used["last_used"]).replace("Z", "+00:00"))
        assert abs((datetime.now(UTC) - used_last).total_seconds()) < 600, used["last_used"]
        assert used["created_at"] == before["created_at"]
