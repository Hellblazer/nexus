"""nexus-vtp8h: plan-library hygiene — executable-DAG validation at save,
matcher skip of always-failing plans, and the service-capable hygiene verb.

Drift-audit evidence: 77/116 pre-migration plans were null-verb bead-dumps;
plan 138 MATCHED at 0.66-0.70 then crashed the runner (unknown tool '').
Save-time validation kills the class at the door; the matcher skip stops
already-stored members from re-crashing; `nx plan hygiene` retires them
durably (disable, never delete) — and works in SERVICE mode, unlike the
SQLite-only `nx plan repair` group.
"""
from __future__ import annotations

import json

import pytest


# ── 1. save-time executable-step validation ──────────────────────────────────


class TestValidatePlanSteps:
    def _validate(self, plan_json: dict, **kw):
        from nexus.plans.schema import validate_plan_steps

        return validate_plan_steps(plan_json, **kw)

    def test_bead_dump_without_steps_rejected(self) -> None:
        from nexus.plans.schema import PlanTemplateSchemaError

        with pytest.raises(PlanTemplateSchemaError, match="steps"):
            self._validate({"phases": ["design", "implement"]}, require_steps=True)

    def test_empty_steps_rejected_when_required(self) -> None:
        from nexus.plans.schema import PlanTemplateSchemaError

        with pytest.raises(PlanTemplateSchemaError, match="steps"):
            self._validate({"steps": []}, require_steps=True)

    def test_step_without_tool_rejected(self) -> None:
        from nexus.plans.schema import PlanTemplateSchemaError

        with pytest.raises(PlanTemplateSchemaError, match="tool"):
            self._validate({"steps": [{"args": {"query": "x"}}]})

    def test_empty_tool_rejected(self) -> None:
        """The plan-138 crash shape: unknown tool ''."""
        from nexus.plans.schema import PlanTemplateSchemaError

        with pytest.raises(PlanTemplateSchemaError, match="tool"):
            self._validate({"steps": [{"tool": ""}]})

    def test_valid_steps_pass(self) -> None:
        self._validate({"steps": [
            {"tool": "search", "args": {"query": "$input"}},
            {"tool": "operator_summarize", "args": {"content": "$step1"}},
        ]}, require_steps=True)

    def test_traverse_sc16_still_enforced(self) -> None:
        from nexus.plans.schema import PlanTemplateSchemaError

        with pytest.raises(PlanTemplateSchemaError, match="SC-16"):
            self._validate({"steps": [
                {"tool": "traverse", "args": {"link_types": ["cites"], "purpose": "x"}},
            ]})


class TestPlanSaveValidation:
    def _save(self, plan_json: str, **kw):
        from nexus.mcp.core import plan_save

        return plan_save(
            query="q", plan_json=plan_json, verb="research", **kw,
        )

    def test_unparseable_json_refused(self) -> None:
        out = self._save("not json {")
        assert "not saved" in out.lower()
        assert "json" in out.lower()

    def test_bead_dump_refused(self) -> None:
        out = self._save(json.dumps({"phases": ["p1"], "beads": ["x"]}))
        assert "not saved" in out.lower()

    def test_empty_steps_refused(self) -> None:
        out = self._save(json.dumps({"steps": []}))
        assert "not saved" in out.lower()

    def test_step_missing_tool_refused(self) -> None:
        out = self._save(json.dumps({"steps": [{"do": "stuff"}]}))
        assert "not saved" in out.lower()


# ── 2. matcher skips always-failing plans ────────────────────────────────────


class TestMatcherSkipsAlwaysFailing:
    def _row(self, plan_id: int, *, failures: int, successes: int) -> dict:
        return {
            "id": plan_id,
            "query": "how does chunk dedup work",
            "plan_json": json.dumps({"steps": [{"tool": "search", "args": {}}]}),
            "tags": "",
            "dimensions": json.dumps({"verb": "research", "scope": "global"}),
            "scope_tags": "",
            "project": "",
            "verb": "research",
            "success_count": successes,
            "failure_count": failures,
        }

    def test_always_failing_skipped_on_fts5_path(self) -> None:
        from nexus.plans.matcher import plan_match

        class _Lib:
            def search_plans(self, intent, limit, project=""):
                return [
                    self_outer._row(138, failures=3, successes=0),
                    self_outer._row(200, failures=3, successes=1),
                ]

            def increment_match_metrics(self, *a, **k):
                pass

        self_outer = self
        matches = plan_match("how does chunk dedup work", library=_Lib(), cache=None)
        ids = [m.plan_id for m in matches]
        assert 138 not in ids, "0-success/3-failure plan must be skipped"
        assert 200 in ids, "a plan with ANY success is never skipped"

    def test_always_failing_skipped_on_cosine_path(self) -> None:
        from nexus.plans.matcher import plan_match

        rows = {
            138: self._row(138, failures=3, successes=0),
            200: self._row(200, failures=0, successes=0),  # new plan: kept
        }

        class _Lib:
            def get_plan(self, plan_id):
                return rows.get(plan_id)

            def search_plans(self, intent, limit, project=""):
                return []

            def increment_match_metrics(self, *a, **k):
                pass

        class _Cache:
            is_available = True

            def query(self, intent, n):
                return [(138, 0.1), (200, 0.15)]

            def remove(self, plan_id):
                pass

        matches = plan_match(
            "how does chunk dedup work", library=_Lib(), cache=_Cache(),
        )
        ids = [m.plan_id for m in matches]
        assert 138 not in ids
        assert 200 in ids


# ── 3. nx plan hygiene (service-capable, disable-not-delete) ─────────────────


class TestPlanHygieneVerb:
    def _library(self):
        rows = [
            # healthy
            {"id": 1, "query": "good", "verb": "research",
             "plan_json": json.dumps({"steps": [{"tool": "search"}]}),
             "success_count": 2, "failure_count": 0, "tags": "", "project": ""},
            # bead-dump (no steps)
            {"id": 2, "query": "dump", "verb": "research",
             "plan_json": json.dumps({"phases": ["a"]}),
             "success_count": 0, "failure_count": 0, "tags": "", "project": ""},
            # null-verb legacy
            {"id": 3, "query": "nullverb", "verb": "",
             "plan_json": json.dumps({"steps": [{"tool": "search"}]}),
             "success_count": 0, "failure_count": 0, "tags": "", "project": ""},
            # always-failing
            {"id": 4, "query": "failer", "verb": "query",
             "plan_json": json.dumps({"steps": [{"tool": "search"}]}),
             "success_count": 0, "failure_count": 5, "tags": "", "project": ""},
            # unparseable plan_json
            {"id": 5, "query": "corrupt", "verb": "query",
             "plan_json": "{not json",
             "success_count": 0, "failure_count": 0, "tags": "", "project": ""},
        ]

        class _Lib:
            def __init__(self) -> None:
                # instance-level (reviewer nit: a class-level mutable list
                # silently shares state across _Lib() instances)
                self.disabled: list[tuple[int, str]] = []

            def list_plans(self, limit=20, project="", *, include_disabled=False):
                return list(rows)

            def set_plan_disabled(self, plan_id, *, reason=""):
                self.disabled.append((plan_id, reason))
                return True

        return _Lib()

    def test_dry_run_reports_without_disabling(self) -> None:
        from nexus.commands.plan import _hygiene_scan

        lib = self._library()
        findings = _hygiene_scan(lib)
        assert sorted(f["id"] for f in findings) == [2, 3, 4, 5]
        assert lib.disabled == []

    def test_apply_disables_each_finding_with_reason(self) -> None:
        from nexus.commands.plan import _hygiene_apply, _hygiene_scan

        lib = self._library()
        findings = _hygiene_scan(lib)
        count = _hygiene_apply(lib, findings)
        assert count == 4
        disabled_ids = sorted(pid for pid, _ in lib.disabled)
        assert disabled_ids == [2, 3, 4, 5]
        assert all(reason for _, reason in lib.disabled)

    def test_healthy_plan_never_flagged(self) -> None:
        from nexus.commands.plan import _hygiene_scan

        findings = _hygiene_scan(self._library())
        assert all(f["id"] != 1 for f in findings)
