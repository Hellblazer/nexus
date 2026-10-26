# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 1 (nexus-jypmj.1.14): the grown clone of plan 473 in the golden fixture's plan library,
and the label key that names it.

The baseline's plan library held only the 12 builtins, so the incident request (G06, the ART RDR-084
synthesis) selected ``research:default`` and scored as the right shape; the incident selected plan 473,
the seeded copy of ``lookup:hybrid-factual-lookup`` (RDR-232 line 1606). ``unacceptable_plans`` keys are
``<verb>:<strategy>``, which cannot name a grown plan (its strategy is a slug of its originating
question). These tests pin the pure parts, with no tenant: the label key, the labels that use it, that
the metric counts a selection of it, and the shape of the row the fixture saves. The measurement itself
runs inside the existing golden tenant test (``test_rdr232_golden_fixture.py``).

Which Phase 2 exit clause the clone stands for. It is stored ``ad-hoc,grown`` and its ``plan_json`` has no
answer-kind declaration, so it exercises ONE clause: "unknown-grown plans cannot bypass the gate" (RDR-232
lines ~487-498). It does NOT measure the clause "473 is ineligible" (a builtin-tagged row that declares
lookup), and its +1 wrong-shape admission is forced by construction (an exact-text repeat of one request),
not a measured rate. Plan 473's builtin origin is inferred (RDR-232 line 1606; T2 nexus_rdr/189-research-11),
not read live. ``TestPhase2Tripwire`` turns red when Phase 2 adds the declaration to the template the clone
copies, so that decision is made at a known point.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from bench.answer_shape_metrics import RunObservation, compute_measures
from bench.schema import load_golden_set
from tests import _rdr232_golden as g

LOOKUP_KEY = "lookup:hybrid-factual-lookup"
G06 = "G06-synthesis-art-rdr084"


def _golden():
    return load_golden_set(g.GOLDEN_PATH)


def _g06():
    return next(q for q in _golden() if q.qid == G06)


class TestLabelKey:
    def test_a_builtin_plan_keeps_its_verb_strategy_key(self) -> None:
        dims = {"verb": "lookup", "scope": "global", "strategy": "hybrid-factual-lookup"}
        assert g.plan_label_key(dims, "builtin-template,rdr-097") == LOOKUP_KEY
        assert g.plan_label_key(dims) == LOOKUP_KEY

    def test_a_grown_plan_gets_a_grown_prefixed_key_that_no_builtin_can_have(self) -> None:
        dims = {"verb": "lookup", "scope": "personal", "strategy": g.GROWN_473_STRATEGY}
        key = g.plan_label_key(dims, "ad-hoc,grown")
        assert key == g.GROWN_473_KEY == "grown:hybrid-factual-lookup-473"
        # a verb:strategy key for the same dims would not say it is grown
        assert g.builtin_plan_key(dims) != key
        # the real condition: no builtin is tagged grown, so every builtin key is a plain verb:strategy key
        paths = sorted(g.BUILTIN_PLANS.glob("*.yml"))
        assert len(paths) >= 10, f"builtin census came back with {len(paths)} templates"
        for path in paths:
            builtin = yaml.safe_load(path.read_text(encoding="utf-8"))
            tokens = [t.strip() for t in str(builtin.get("tags", "")).split(",")]
            assert "grown" not in tokens, f"{path.name} is tagged grown: {tokens}"
            key = g.plan_label_key(builtin["dimensions"], builtin.get("tags", ""))
            assert not key.startswith("grown:"), (path.name, key)

    def test_the_grown_token_is_matched_whole_not_as_a_substring(self) -> None:
        dims = {"verb": "lookup", "strategy": "x"}
        assert g.plan_label_key(dims, "overgrown,ungrown") == "lookup:x"


class TestLabels:
    def test_g06_names_the_grown_clone_as_unacceptable(self) -> None:
        assert g.GROWN_473_KEY in _g06().unacceptable_plans

    def test_the_clone_is_unacceptable_exactly_where_plan_473_is(self) -> None:
        # The clone has 473's body, so it is the wrong shape for every request 473 is the wrong shape for,
        # and for no other. A lookup request (473's own shape) must never list either.
        listed_lookup = {q.qid for q in _golden() if LOOKUP_KEY in q.unacceptable_plans}
        listed_clone = {q.qid for q in _golden() if g.GROWN_473_KEY in q.unacceptable_plans}
        assert listed_lookup == listed_clone
        assert len(listed_clone) >= 15, "non-vacuity: the label is on the requests that need it"
        for q in _golden():
            if q.answer_kind == "lookup":
                assert g.GROWN_473_KEY not in q.unacceptable_plans, q.qid

    def test_every_grown_key_a_label_uses_is_a_known_one(self) -> None:
        used = {p for q in _golden() for p in q.unacceptable_plans if p.startswith("grown:")}
        assert used and used <= g.GROWN_LABEL_KEYS, sorted(used - g.GROWN_LABEL_KEYS)


class TestMetricCountsTheClone:
    def test_selecting_the_clone_for_g06_is_a_wrong_shape_admission(self) -> None:
        q = _g06()
        clone = compute_measures([q], [RunObservation(
            G06, selected_plan=g.GROWN_473_KEY, candidate_plans=[g.GROWN_473_KEY, "research:default"])])
        floor = compute_measures([q], [RunObservation(
            G06, selected_plan="research:default", candidate_plans=["research:default"])])
        assert clone["wrong_shape_admission_rate"].value == 1.0
        assert floor["wrong_shape_admission_rate"].value == 0.0
        # recall is the blacklist reading: research:default is still an acceptable candidate
        assert clone["eligible_plan_recall"].value == 1.0

    def test_a_clone_that_is_the_only_candidate_lowers_eligible_recall(self) -> None:
        only = compute_measures([_g06()], [RunObservation(
            G06, selected_plan=g.GROWN_473_KEY, candidate_plans=[g.GROWN_473_KEY])])
        assert only["eligible_plan_recall"].value == 0.0


class TestBaselineCaveats:
    def test_the_caveats_state_the_blacklist_reading_and_the_single_request(self) -> None:
        from bench.answer_shape_metrics import BASELINE_CAVEATS

        text = " ".join(BASELINE_CAVEATS)
        # eligible_plan_recall: some candidate is not labeled unacceptable, and only unacceptable plans are labeled
        assert "eligible_plan_recall" in text and "not labeled unacceptable" in text
        assert "Only unacceptable plans are labeled" in text
        # the evidence-side measures score one request
        for name in ("primary_source_coverage", "facet_evidence_coverage", "scope_leakage"):
            assert name in text
        assert "n=1" in text

    def test_a_caveat_states_the_grown_clone_figure_is_forced_by_construction(self) -> None:
        from bench.answer_shape_metrics import BASELINE_CAVEATS

        assert len(BASELINE_CAVEATS) == 3
        text = g.grown_clone_caveat()
        assert text in BASELINE_CAVEATS
        for fact in (
            "forced by construction",       # the 0.1667 is the floor plus the one request
            "verbatim-repeat",              # the mechanism that selects the clone
            "one request",                  # its coverage
            "not a measured rate",
            "how plan 473 matched live",    # what it does not say
            "unknown-grown plans cannot bypass the gate",   # the one clause it stands for
            "473 is ineligible",            # the clause it does not measure
            "inferred",                     # 473's builtin origin was never read live
            "builtins-only floor",          # what the top-level plan-side measures are
        ):
            assert fact in text, fact


class TestReportLabels:
    """The report is what gets quoted: the grown-clone figures carry their caveat, the top-level
    plan-side measures say they are the floor, and the version is the schema's."""

    def _measures(self) -> tuple[dict, dict]:
        q = _g06()
        floor = compute_measures([q], [RunObservation(
            G06, selected_plan="research:default", candidate_plans=["research:default"])])
        clone = compute_measures([q], [RunObservation(
            G06, selected_plan=g.GROWN_473_KEY, candidate_plans=[g.GROWN_473_KEY, "research:default"])])
        return floor, clone

    def test_the_grown_clone_block_carries_the_construction_caveat(self) -> None:
        floor, clone = self._measures()
        side = g.plan_side_report(floor, clone)
        assert side["with_grown_473_clone"]["caveat"] == g.grown_clone_caveat()
        assert "forced by construction" in side["with_grown_473_clone"]["caveat"]
        assert side["with_grown_473_clone"]["measures"]["wrong_shape_admission_rate"]["value"] == 1.0
        assert side["builtins_only"]["measures"]["wrong_shape_admission_rate"]["value"] == 0.0
        assert "floor" in side["builtins_only"]["reading"]

    def test_the_top_level_plan_side_measures_are_labeled_the_builtins_only_floor(self) -> None:
        floor, _clone = self._measures()
        report = g.measures_report(floor)
        for name in g.PLAN_SIDE_MEASURES:
            assert report[name]["plan_library"] == g.FLOOR_READING and "floor" in g.FLOOR_READING
        # the evidence-side entries are not plan-library readings
        assert "plan_library" not in report["primary_source_coverage"]

    def test_the_fixture_report_reads_the_version_builds_only_on_request_and_resets_the_cache(self) -> None:
        # The report is built inside the tenant journey, so this reads its source: the version comes from
        # bench.schema, and the VCS-reading calls (check=True, they fail with no usable repository)
        # sit under the `if out:` that only the NX_RDR232_BASELINE_OUT path takes.
        import ast

        tree = ast.parse((Path(__file__).parent / "test_rdr232_golden_fixture.py").read_text(encoding="utf-8"))
        (emit,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_emit_baseline"]
        versions = [
            kv for d in ast.walk(emit) if isinstance(d, ast.Dict)
            for k, kv in zip(d.keys, d.values) if isinstance(k, ast.Constant) and k.value == "golden_set_version"
        ]
        assert len(versions) == 1 and isinstance(versions[0], ast.Name) and versions[0].id == "GOLDEN_SET_VERSION"

        guarded: set[int] = set()
        for node in ast.walk(emit):
            if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "out":
                guarded |= {id(n) for n in ast.walk(node)}
        calls = [n for n in ast.walk(emit) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr in {"head_sha", "tree_is_dirty"}]
        assert len(calls) == 2, "non-vacuity: both VCS-reading calls are in the report"
        assert all(id(c) in guarded for c in calls), "head_sha / tree_is_dirty must sit under `if out:`"


        # the process-global plan cache: reset before the first selection (the floor must not inherit an
        # earlier test's cache) and again in a `finally` (the run must not leave the clone in it)
        def is_reset(n: ast.AST) -> bool:
            return (isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                    and isinstance(n.value.func, ast.Name) and n.value.func.id == "reset_plan_cache_for_tests")

        def has_reset(nodes: list[ast.stmt]) -> bool:
            return any(is_reset(n) for n in nodes)

        tries = [n for n in emit.body if isinstance(n, ast.Try)]
        assert len(tries) == 1 and has_reset(tries[0].finalbody), "reset_plan_cache_for_tests in a finally"
        first_try = emit.body.index(tries[0])
        assert has_reset(emit.body[:first_try]), "reset_plan_cache_for_tests before the floor loop"
        observed = [n for n in ast.walk(tries[0]) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "observe_plan_selection"]
        assert observed, "non-vacuity: the selections are observed inside the try"
        assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                       and n.func.attr == "observe_plan_selection" for n in ast.walk(ast.Module(body=emit.body[:first_try], type_ignores=[])))


class TestPhase2Tripwire:
    def test_the_clone_plan_json_carries_no_answer_kind_declaration(self) -> None:
        # RDR-232 puts the declaration in the builtin plan_json (lines ~487-498). The clone copies the
        # hybrid-factual-lookup template, so when Phase 2 adds the declaration there the clone becomes a
        # declared-kind plan without anyone choosing it.
        lib = _RecordingLibrary()
        g.seed_grown_473_clone(lib, origin_question="Synthesize RDR-084 something.")
        body = json.loads(lib.calls[0]["plan_json"])
        declared = sorted(k for k in body if "kind" in k.lower())
        assert not declared, (
            f"plan_json now declares {declared}: Phase 2 (nexus-jypmj.2) added the answer-kind field to "
            "hybrid-factual-lookup.yml, which the grown 473 clone copies. DECIDE: strip the field from the "
            "clone (tests/_rdr232_golden.py seed_grown_473_clone) so it keeps exercising 'unknown-grown "
            "plans cannot bypass the gate', or keep it and re-record the baseline knowing the clone is then "
            "a declared-lookup plan and that clause is no longer measured. Then update this pin.")
        assert [s["tool"] for s in body["steps"]][:1] == ["search"], "non-vacuity: this is the lookup body"


class _RecordingLibrary:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def save_plan(self, **kw) -> int:
        self.calls.append(kw)
        return 42


class TestCloneRow:
    def _saved(self, question: str = "Synthesize RDR-084 something.") -> dict:
        lib = _RecordingLibrary()
        assert g.seed_grown_473_clone(lib, origin_question=question) == 42
        (call,) = lib.calls
        return call

    def _template(self) -> dict:
        return yaml.safe_load((g.BUILTIN_PLANS / "hybrid-factual-lookup.yml").read_text(encoding="utf-8"))

    def test_the_row_is_grown_the_way_nx_answer_saves_a_grown_plan(self) -> None:
        call = self._saved()
        assert call["tags"] == "ad-hoc,grown" and call["scope"] == "personal"
        assert call["scope_tags"] == g.GROWN_473_SCOPE_TAGS and call["outcome"] == "success"
        assert json.loads(call["dimensions"]) == {
            "scope": "personal", "strategy": g.GROWN_473_STRATEGY, "verb": "lookup"}
        assert (call["verb"], call["name"]) == ("lookup", g.GROWN_473_STRATEGY)

    def test_the_row_runs_as_plan_473_ran(self) -> None:
        call = self._saved()
        template = self._template()
        body = json.loads(call["plan_json"])
        assert body["steps"] == template["plan_json"]["steps"], "473's steps"
        assert body["required_bindings"] == template["required_bindings"]
        assert json.loads(call["default_bindings"]) == template["default_bindings"]
        assert [s["tool"] for s in body["steps"]] == [
            "search", "traverse", "store_get_many", "store_get_many", "rank", "generate"]

    def test_the_originating_question_is_the_one_given_and_never_473s_description(self) -> None:
        # `plans.query` is the key the seed loader reconciles the builtin on: saving the clone under the
        # builtin's description overwrote the builtin row (found while building this fixture).
        question = "Synthesize RDR-084 something."
        call = self._saved(question)
        assert call["query"] == question
        assert " ".join(call["query"].split()) != " ".join(str(self._template()["description"]).split())
        assert call["match_description"] == " ".join(str(self._template()["description"]).split())

    def test_an_origin_question_equal_to_a_builtin_description_is_refused(self) -> None:
        # saved under a builtin's description the clone overwrote that builtin row (found building it)
        template = self._template()
        for question in (str(template["description"]), "  " + " ".join(str(template["description"]).split()) + "\n"):
            lib = _RecordingLibrary()
            with pytest.raises(ValueError, match="overwrite"):
                g.seed_grown_473_clone(lib, origin_question=question)
            assert lib.calls == [], "nothing is saved"

    def test_every_builtin_description_is_a_collision(self) -> None:
        descriptions = g.builtin_descriptions()
        assert len(descriptions) >= 10, "non-vacuity: the builtin census"
        for d in sorted(descriptions):
            with pytest.raises(ValueError):
                g.seed_grown_473_clone(_RecordingLibrary(), origin_question=d)


class TestSelectionDrift:
    A = {"selected": "x", "candidates": ["x", "y"]}

    def test_no_movement(self) -> None:
        assert g.selection_drift({"q": self.A}, {"q": dict(self.A)}) == ([], [])

    def test_a_reordered_candidate_list_is_the_same_set(self) -> None:
        moved = {"selected": "x", "candidates": ["y", "x"]}
        assert g.selection_drift({"q": self.A}, {"q": moved}) == ([], [])

    def test_a_new_candidate_moves_the_set_not_the_selection(self) -> None:
        moved = {"selected": "x", "candidates": ["x", "y", "grown:z"]}
        assert g.selection_drift({"q": self.A}, {"q": moved}) == ([], ["q"])

    def test_a_new_selection_moves_the_selection(self) -> None:
        moved = {"selected": "grown:z", "candidates": ["grown:z", "x", "y"]}
        assert g.selection_drift({"q": self.A}, {"q": moved}) == (["q"], ["q"])

    def test_a_request_missing_from_one_side_is_moved(self) -> None:
        assert g.selection_drift({"q": self.A}, {}) == (["q"], ["q"])
