# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for RDR-084 Plan Library Growth.

Auto-save successful ad-hoc plans on the ``nx_answer`` plan-miss →
planner → ``plan_run`` success path so the plan library compounds
with usage instead of plateauing at the 14 seed templates.

Contracts being pinned:

  * On ad-hoc success: ``db.plans.save_plan`` is called with
    ``scope="personal"``, ``tags="ad-hoc,grown"``, ``outcome="success"``,
    ``ttl=<config ad_hoc_ttl>``, and project=cwd basename.
  * On matched-plan success (plan_id != 0): save is NOT called — the
    library already has this plan.
  * On plan_run error: save is NOT called — failed plans don't compound.
  * On save exception: the user still gets their answer — best-effort.
  * On successful save: the T1 cosine cache receives an ``upsert(row)``
    so the next paraphrase can match via cosine without a SessionStart.
  * Config key ``plans.ad_hoc_ttl`` overrides the 30-day default.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.plans.match import Match


def _ad_hoc_match(plan_json: str | None = None) -> Match:
    if plan_json is None:
        plan_json = json.dumps({
            "steps": [
                {"tool": "search", "args": {"query": "$intent", "corpus": "knowledge"}},
                {"tool": "summarize", "args": {"content": "$step1.ids"}},
            ],
        })
    return Match(
        plan_id=0,
        name="ad-hoc",
        description="what is the meaning of life",
        confidence=None,
        dimensions={},
        tags="ad-hoc",
        plan_json=plan_json,
        required_bindings=["intent"],
        optional_bindings=[],
        default_bindings={"intent": "what is the meaning of life"},
        parent_dims=None,
    )


def _matched_plan(plan_id: int = 5) -> Match:
    return Match(
        plan_id=plan_id,
        name="test-plan",
        description="test",
        confidence=0.55,
        dimensions={},
        tags="",
        plan_json=json.dumps({
            "steps": [
                {"tool": "search", "args": {"query": "$intent"}},
                {"tool": "summarize", "args": {"content": "$step1.ids"}},
            ],
        }),
        required_bindings=["intent"],
        optional_bindings=[],
        default_bindings={"intent": "q"},
        parent_dims=None,
    )


def _plan_run_success():
    """Return a MagicMock that behaves like a successful plan_run result."""
    result = MagicMock()
    result.steps = [{"text": "the answer is 42"}]
    return result


# ── Save-on-ad-hoc-success ────────────────────────────────────────────────────


class TestAdHocSaveOnSuccess:
    """RDR-084 core contract: successful ad-hoc plans are saved."""

    @pytest.mark.asyncio
    async def test_save_called_with_correct_kwargs(self):
        from nexus.mcp.core import nx_answer

        match = _ad_hoc_match()
        save_mock = MagicMock(return_value=999)

        db_stub = MagicMock()
        db_stub.plans.save_plan = save_mock
        db_stub.plans.get_plan = MagicMock(return_value={"id": 999, "query": "q"})

        with patch("nexus.plans.matcher.plan_match", return_value=[]), \
             patch("nexus.mcp.core._nx_answer_plan_miss", AsyncMock(return_value=match)), \
             patch("nexus.plans.runner.plan_run", AsyncMock(return_value=_plan_run_success())), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
            t2_ctx.return_value.__enter__.return_value = db_stub
            await nx_answer(question="what is the meaning of life")

        assert save_mock.called, "save_plan must be called on ad-hoc success"
        kwargs = save_mock.call_args.kwargs
        assert kwargs["query"] == "what is the meaning of life"
        assert kwargs["scope"] == "personal"
        assert kwargs["outcome"] == "success"
        assert "ad-hoc" in kwargs["tags"]
        assert "grown" in kwargs["tags"]
        # ttl is the configured default (30) absent an override
        assert kwargs["ttl"] == 30
        # plan_json is the normalized DAG
        assert json.loads(kwargs["plan_json"]).get("steps"), (
            "plan_json must round-trip as a non-empty steps DAG"
        )

    @pytest.mark.asyncio
    async def test_matched_plan_does_not_trigger_save(self):
        """plan_id != 0 means the library already has this plan — no save."""
        from nexus.mcp.core import nx_answer

        match = _matched_plan(plan_id=7)
        save_mock = MagicMock()
        db_stub = MagicMock()
        db_stub.plans.save_plan = save_mock

        with patch("nexus.plans.matcher.plan_match", return_value=[match]), \
             patch("nexus.plans.runner.plan_run", AsyncMock(return_value=_plan_run_success())), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
            t2_ctx.return_value.__enter__.return_value = db_stub
            await nx_answer(question="some question")

        assert not save_mock.called, (
            "save_plan must NOT be called when a library plan matched"
        )

    @pytest.mark.asyncio
    async def test_plan_run_error_skips_save(self):
        """If plan_run raises, the ad-hoc plan is NOT saved."""
        from nexus.mcp.core import nx_answer

        match = _ad_hoc_match()
        save_mock = MagicMock()
        db_stub = MagicMock()
        db_stub.plans.save_plan = save_mock

        with patch("nexus.plans.matcher.plan_match", return_value=[]), \
             patch("nexus.mcp.core._nx_answer_plan_miss", AsyncMock(return_value=match)), \
             patch(
                "nexus.plans.runner.plan_run",
                AsyncMock(side_effect=RuntimeError("boom")),
             ), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
            t2_ctx.return_value.__enter__.return_value = db_stub
            result = await nx_answer(question="bad")

        assert not save_mock.called, (
            "Failures must not compound into the library"
        )
        assert "error" in result.lower()

    @pytest.mark.asyncio
    async def test_save_exception_does_not_affect_answer(self):
        """save_plan raising must not prevent the user getting their answer."""
        from nexus.mcp.core import nx_answer

        match = _ad_hoc_match()
        save_mock = MagicMock(side_effect=RuntimeError("disk full"))
        db_stub = MagicMock()
        db_stub.plans.save_plan = save_mock

        with patch("nexus.plans.matcher.plan_match", return_value=[]), \
             patch("nexus.mcp.core._nx_answer_plan_miss", AsyncMock(return_value=match)), \
             patch("nexus.plans.runner.plan_run", AsyncMock(return_value=_plan_run_success())), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
            t2_ctx.return_value.__enter__.return_value = db_stub
            result = await nx_answer(question="q")

        # The answer text still got through — save failure is best-effort.
        assert "42" in result


# ── T1 cache propagation ──────────────────────────────────────────────────────


class TestT1CachePropagation:
    """RDR-084: a saved plan must upsert into the T1 cosine cache so the
    next paraphrase can match without waiting for SessionStart re-populate."""

    @pytest.mark.asyncio
    async def test_cache_upsert_called_on_save(self):
        from nexus.mcp.core import nx_answer

        match = _ad_hoc_match()
        stored_row = {"id": 123, "query": "q", "plan_json": match.plan_json}
        db_stub = MagicMock()
        db_stub.plans.save_plan = MagicMock(return_value=123)
        db_stub.plans.get_plan = MagicMock(return_value=stored_row)

        cache_stub = MagicMock()
        cache_stub.upsert = MagicMock(return_value=True)

        with patch("nexus.plans.matcher.plan_match", return_value=[]), \
             patch("nexus.mcp.core._nx_answer_plan_miss", AsyncMock(return_value=match)), \
             patch("nexus.plans.runner.plan_run", AsyncMock(return_value=_plan_run_success())), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=cache_stub):
            t2_ctx.return_value.__enter__.return_value = db_stub
            await nx_answer(question="q")

        assert cache_stub.upsert.called, (
            "T1 cache must receive the new plan so future paraphrases hit"
        )
        passed_row = cache_stub.upsert.call_args.args[0]
        assert passed_row.get("id") == 123

    @pytest.mark.asyncio
    async def test_cache_upsert_failure_does_not_fail_save(self):
        """Cache upsert is best-effort; the plan is still in T2."""
        from nexus.mcp.core import nx_answer

        match = _ad_hoc_match()
        db_stub = MagicMock()
        db_stub.plans.save_plan = MagicMock(return_value=456)
        db_stub.plans.get_plan = MagicMock(return_value={"id": 456, "query": "q"})

        cache_stub = MagicMock()
        cache_stub.upsert = MagicMock(side_effect=RuntimeError("t1 down"))

        with patch("nexus.plans.matcher.plan_match", return_value=[]), \
             patch("nexus.mcp.core._nx_answer_plan_miss", AsyncMock(return_value=match)), \
             patch("nexus.plans.runner.plan_run", AsyncMock(return_value=_plan_run_success())), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=cache_stub):
            t2_ctx.return_value.__enter__.return_value = db_stub
            result = await nx_answer(question="q")

        # Save succeeded; upsert failed; user still got an answer.
        assert db_stub.plans.save_plan.called
        assert "42" in result

    @pytest.mark.asyncio
    async def test_cache_unavailable_no_upsert_attempt(self):
        """When get_t1_plan_cache returns None, save proceeds without upsert."""
        from nexus.mcp.core import nx_answer

        match = _ad_hoc_match()
        db_stub = MagicMock()
        db_stub.plans.save_plan = MagicMock(return_value=789)
        db_stub.plans.get_plan = MagicMock(return_value={"id": 789})

        with patch("nexus.plans.matcher.plan_match", return_value=[]), \
             patch("nexus.mcp.core._nx_answer_plan_miss", AsyncMock(return_value=match)), \
             patch("nexus.plans.runner.plan_run", AsyncMock(return_value=_plan_run_success())), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
            t2_ctx.return_value.__enter__.return_value = db_stub
            await nx_answer(question="q")

        # save_plan still called; get_plan unused (no cache to feed).
        assert db_stub.plans.save_plan.called


# ── Config: plans.ad_hoc_ttl ──────────────────────────────────────────────────


class TestAdHocTtlConfig:
    """RDR-084: TTL defaults to 30 days; ``.nexus.yml#plans.ad_hoc_ttl``
    overrides it."""

    def test_default_ttl_is_30(self):
        from nexus.mcp.core import _load_ad_hoc_ttl

        with patch(
            "nexus.mcp.core.load_config",
            return_value={"plans": {"ad_hoc_ttl": 30}},
        ):
            assert _load_ad_hoc_ttl() == 30

    def test_config_override_wins(self):
        from nexus.mcp.core import _load_ad_hoc_ttl

        with patch(
            "nexus.mcp.core.load_config",
            return_value={"plans": {"ad_hoc_ttl": 7}},
        ):
            assert _load_ad_hoc_ttl() == 7

    def test_missing_plans_section_falls_back_to_30(self):
        from nexus.mcp.core import _load_ad_hoc_ttl

        with patch("nexus.mcp.core.load_config", return_value={}):
            assert _load_ad_hoc_ttl() == 30

    def test_load_config_failure_falls_back_to_30(self):
        from nexus.mcp.core import _load_ad_hoc_ttl

        with patch(
            "nexus.mcp.core.load_config",
            side_effect=RuntimeError("no config"),
        ):
            assert _load_ad_hoc_ttl() == 30

    def test_defaults_registry_has_ad_hoc_ttl(self):
        """The ``_DEFAULTS`` dict must carry ``plans.ad_hoc_ttl`` so
        config-less installs get the RDR-084 default automatically."""
        from nexus.config import _DEFAULTS

        assert _DEFAULTS.get("plans", {}).get("ad_hoc_ttl") == 30


# ── Live-SQLite round-trip (critic gap from PR #170) ─────────────────────────


class TestLiveSqliteRoundTrip:
    """End-to-end against a real on-disk SQLite T2Database.

    The other suites in this file mock ``_t2_ctx`` — that catches
    call-shape regressions but cannot catch cross-transaction bugs
    like ``save_plan`` returning an id that ``get_plan`` fails to
    resolve under the same ``_t2_ctx`` block. The critic in PR #170
    flagged this as a coverage gap; this suite closes it.

    Patches applied:
      * ``nexus.commands._helpers.default_db_path`` → tmp_path/memory.db
        so every ``_t2_ctx()`` opens the isolated fixture db.
      * ``plan_match`` → empty so nx_answer takes the plan-miss branch.
      * ``_nx_answer_plan_miss`` → returns a synthetic ad-hoc Match.
      * ``_plan_run`` → returns a trivial success.
      * ``get_t1_plan_cache`` → captures the row passed to ``upsert``.
    """

    @pytest.mark.asyncio
    async def test_save_plan_get_plan_cache_upsert_round_trips(
        self, tmp_path,
    ):
        from nexus.mcp.core import nx_answer

        # Real, isolated T2 on disk
        db_path = tmp_path / "memory.db"

        cache_stub = MagicMock()
        cache_stub.upsert = MagicMock(return_value=True)

        match = _ad_hoc_match()

        with patch(
            "nexus.mcp_infra.default_db_path",
            return_value=db_path,
        ), patch("nexus.plans.matcher.plan_match", return_value=[]), \
           patch(
               "nexus.mcp.core._nx_answer_plan_miss",
               AsyncMock(return_value=match),
           ), \
           patch(
               "nexus.plans.runner.plan_run",
               AsyncMock(return_value=_plan_run_success()),
           ), \
           patch("nexus.mcp.core.scratch", return_value="ok"), \
           patch("nexus.mcp_infra.get_t1_plan_cache", return_value=cache_stub):
            result = await nx_answer(question="what is nexus retrieval")

        # User still got their answer
        assert "42" in result

        # Real DB: the row is queryable after the command returned.
        from nexus.db.t2 import T2Database

        with T2Database(db_path) as verify_db:
            rows = verify_db.plans.list_active_plans(outcome="success")
        assert rows, "save_plan should have persisted at least one plan"
        saved = next(
            (r for r in rows if "ad-hoc" in (r.get("tags") or "")),
            None,
        )
        assert saved is not None, (
            "ad-hoc plan not found in T2 after save — save_plan → list desync"
        )
        assert saved["scope"] == "personal"
        assert "grown" in saved["tags"]
        assert saved["query"] == "what is nexus retrieval"
        # ttl_days default is 30 (no .nexus.yml override in tmp_path)
        assert saved["ttl"] == 30

        # Cache upsert received the exact row save_plan produced.
        assert cache_stub.upsert.called, (
            "T1 cache.upsert must fire after a successful save"
        )
        passed_row = cache_stub.upsert.call_args.args[0]
        assert passed_row["id"] == saved["id"], (
            f"cache saw id={passed_row.get('id')!r} but T2 has "
            f"id={saved['id']!r} — save_plan → get_plan desync"
        )
        assert passed_row["query"] == saved["query"]
        assert passed_row["plan_json"] == saved["plan_json"]

    @pytest.mark.asyncio
    async def test_save_plan_ttl_zero_disables_growth(self, tmp_path):
        """plans.ad_hoc_ttl = 0 in config disables growth entirely."""
        from nexus.mcp.core import nx_answer

        db_path = tmp_path / "memory.db"
        cache_stub = MagicMock()
        match = _ad_hoc_match()

        with patch(
            "nexus.mcp_infra.default_db_path",
            return_value=db_path,
        ), patch("nexus.plans.matcher.plan_match", return_value=[]), \
           patch(
               "nexus.mcp.core._nx_answer_plan_miss",
               AsyncMock(return_value=match),
           ), \
           patch(
               "nexus.plans.runner.plan_run",
               AsyncMock(return_value=_plan_run_success()),
           ), \
           patch("nexus.mcp.core.scratch", return_value="ok"), \
           patch("nexus.mcp_infra.get_t1_plan_cache", return_value=cache_stub), \
           patch(
               "nexus.mcp.core.load_config",
               return_value={"plans": {"ad_hoc_ttl": 0}},
           ):
            await nx_answer(question="ttl-zero-question")

        # ttl=0 must short-circuit save entirely
        from nexus.db.t2 import T2Database

        with T2Database(db_path) as verify_db:
            rows = verify_db.plans.list_active_plans(outcome="success")
        assert rows == [], (
            "ttl_days=0 must disable growth; no plan should have been saved"
        )
        assert not cache_stub.upsert.called
