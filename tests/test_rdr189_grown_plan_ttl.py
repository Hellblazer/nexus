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
"""
from __future__ import annotations

import json
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.db.t2 import T2Database
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


async def _drive_nx_answer(db_path, question: str, *, plan_id: int, ttl_days: int | None = None) -> str:
    """Run the real ``nx_answer`` over real T2, patching only the planner/LLM seams.

    ``plan_id == 0`` takes the plan-miss branch (the grow path); a non-zero
    ``plan_id`` makes ``plan_match`` return that library row (the use path).
    ``ttl_days`` overrides ``plans.ad_hoc_ttl`` through the config loader.
    """
    from nexus.config import load_config
    from nexus.mcp.core import nx_answer

    matches = [] if plan_id == 0 else [_match(plan_id, question)]
    with ExitStack() as stack:
        stack.enter_context(patch("nexus.mcp_infra.default_db_path", return_value=db_path))
        stack.enter_context(patch("nexus.plans.matcher.plan_match", return_value=matches))
        stack.enter_context(patch(
            "nexus.mcp.core._nx_answer_plan_miss",
            AsyncMock(return_value=_match(0, question)),
        ))
        stack.enter_context(patch(
            "nexus.plans.runner.plan_run", AsyncMock(return_value=_plan_run_success()),
        ))
        stack.enter_context(patch(
            "nexus.mcp.core._generalize_grown_match_description", AsyncMock(return_value=None),
        ))
        stack.enter_context(patch("nexus.mcp.core.scratch", return_value="ok"))
        stack.enter_context(patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None))
        if ttl_days is not None:
            real = load_config()
            overridden = {**real, "plans": {**(real.get("plans") or {}), "ad_hoc_ttl": ttl_days}}
            stack.enter_context(patch("nexus.mcp.core.load_config", return_value=overridden))
        return await nx_answer(question=question)


def _grown_row(db: T2Database, question: str) -> dict:
    rows = [r for r in db.plans.list_active_plans(outcome="success") if r.get("query") == question]
    assert len(rows) == 1, f"expected exactly one grown row for {question!r}, got {len(rows)}"
    return rows[0]


_GROWN_TTL = object()


def _importer(db: T2Database, grown: dict):
    def _import(
        query: str, *, created_days: float, last_used_days: float | None, ttl: object = _GROWN_TTL,
    ) -> int:
        return db.plans.import_plan(
            project="",
            query=query,
            plan_json=grown["plan_json"],
            outcome="success",
            tags=grown["tags"],
            scope=grown["scope"],
            ttl=grown["ttl"] if ttl is _GROWN_TTL else ttl,
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


class TestGrownPlanCarriesTheTtl:
    @pytest.mark.asyncio
    async def test_plan_miss_grows_a_personal_ttl_row(self, tmp_path):
        question = "ttlprobe grow default ttl"
        await _drive_nx_answer(tmp_path / "memory.db", question, plan_id=0)

        with T2Database(tmp_path / "memory.db") as db:
            row = _grown_row(db, question)
        tags = (row["tags"] or "").split(",")
        assert row["scope"] == "personal"
        assert "ad-hoc" in tags and "grown" in tags
        assert row["ttl"] == 30, "the default plans.ad_hoc_ttl is 30 days"
        # Fresh growth stamps created_at and leaves the row unused.
        assert not row.get("last_used")
        assert row.get("use_count", 0) == 0

    @pytest.mark.asyncio
    async def test_config_override_sets_the_grown_ttl(self, tmp_path):
        question = "ttlprobe grow override ttl"
        await _drive_nx_answer(tmp_path / "memory.db", question, plan_id=0, ttl_days=7)

        with T2Database(tmp_path / "memory.db") as db:
            assert _grown_row(db, question)["ttl"] == 7

    @pytest.mark.asyncio
    async def test_ttl_zero_opts_out_of_growth(self, tmp_path):
        question = "ttlprobe grow disabled"
        await _drive_nx_answer(tmp_path / "memory.db", question, plan_id=0, ttl_days=0)

        with T2Database(tmp_path / "memory.db") as db:
            assert [r for r in db.plans.list_plans(limit=100) if r["query"] == question] == []


class TestGrownPlanExpiresOnDisuse:
    @pytest.mark.asyncio
    async def test_unused_rows_past_ttl_drop_out_of_every_reader(self, tmp_path):
        db_path = tmp_path / "memory.db"
        grown_q = "ttlprobe shape source"
        await _drive_nx_answer(db_path, grown_q, plan_id=0)

        with T2Database(db_path) as db:
            grown = _grown_row(db, grown_q)
            imp = _importer(db, grown)
            # created 400d ago, never used: disuse age 400d > 30d.
            imp("ttlprobe never-used expired", created_days=400, last_used_days=None)
            # created 400d ago, last used 200d ago: disuse age 200d > 30d.
            imp("ttlprobe long-unused expired", created_days=400, last_used_days=200)
            # Controls proving the readers do return in-TTL rows, so absence above is expiry and not a
            # broken query: unused but young (1d), and old but used recently (25d disuse < 30d).
            imp("ttlprobe young unused kept", created_days=1, last_used_days=None)
            imp("ttlprobe old recently-used kept", created_days=400, last_used_days=25)

            seen = _active_queries(db)

        for reader, queries in seen.items():
            assert "ttlprobe never-used expired" not in queries, reader
            assert "ttlprobe long-unused expired" not in queries, reader
            assert "ttlprobe young unused kept" in queries, reader
            assert "ttlprobe old recently-used kept" in queries, reader
            assert grown_q in queries, f"{reader}: the freshly grown row must still be active"

    @pytest.mark.asyncio
    async def test_permanent_row_is_unaffected_by_disuse(self, tmp_path):
        db_path = tmp_path / "memory.db"
        grown_q = "ttlprobe permanent shape source"
        await _drive_nx_answer(db_path, grown_q, plan_id=0)

        with T2Database(db_path) as db:
            grown = _grown_row(db, grown_q)
            _importer(db, grown)(
                "ttlprobe permanent old", created_days=400, last_used_days=None, ttl=None,
            )
            seen = _active_queries(db)

        for reader, queries in seen.items():
            assert "ttlprobe permanent old" in queries, reader


class TestGrownPlanSurvivesWithUse:
    @pytest.mark.asyncio
    async def test_a_matched_nx_answer_run_resets_the_disuse_clock(self, tmp_path):
        db_path = tmp_path / "memory.db"
        grown_q = "ttlprobe use shape source"
        await _drive_nx_answer(db_path, grown_q, plan_id=0)

        with T2Database(db_path) as db:
            imp = _importer(db, _grown_row(db, grown_q))
            # Both: created 400d ago (10x the TTL), last used 25d ago (5d from expiry). Identical, so
            # the only difference between them is that one gets used through nx_answer below.
            used_id = imp("ttlprobe used by nx_answer", created_days=400, last_used_days=25)
            control_id = imp("ttlprobe control left alone", created_days=400, last_used_days=25)
            before_used = db.plans.get_plan(used_id)
            before_control = db.plans.get_plan(control_id)
            assert before_used["last_used"] == before_control["last_used"]
            assert before_used["use_count"] == before_control["use_count"] == 0

        # The run goes through nx_answer on a library match, so whichever run-start path the live
        # engine takes (composite telemetry write or the early increment_run_started) is the one tested.
        answer = await _drive_nx_answer(db_path, "ttlprobe used by nx_answer", plan_id=used_id)
        assert "42" in answer

        with T2Database(db_path) as db:
            used = db.plans.get_plan(used_id)
            control = db.plans.get_plan(control_id)
            active = {r["id"] for r in db.plans.list_active_plans(outcome="success")}

        assert used["use_count"] == 1
        assert control["use_count"] == 0
        assert control["last_used"] == before_control["last_used"], "an unused row's clock must not move"
        # The used row's disuse clock was reset to ~now (PG clock), nowhere near its 25d-old value.
        used_last = datetime.fromisoformat(str(used["last_used"]).replace("Z", "+00:00"))
        assert abs((datetime.now(UTC) - used_last).total_seconds()) < 600, used["last_used"]
        # Survival is by disuse, not age: created_at is 400d old against a 30d TTL, yet the row is active
        # and its created_at is untouched.
        assert used_id in active and control_id in active
        assert used["created_at"] == before_used["created_at"]
        assert used["ttl"] == 30
