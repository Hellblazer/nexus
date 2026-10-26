# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 1 (nexus-jypmj.1.3): the caller's corpus scope narrows plan-declared corpora.

Before this bead a plan-declared corpus beat the caller's ``scope`` (nexus-zs1d), so
``nx_answer(scope="rdr")`` on the factual-lookup builtin still searched ``corpus: all``. RDR-232
section 2a: a corpus-prefix or collection scope now narrows the resolved corpus of every
corpus-taking retrieval step; a step whose plan corpus has no overlap with the scope is a scope
conflict and is skipped (its output is an empty structured result plus a ``scope_conflict`` marker).

No test here mints a tenant. The runner is driven through ``plan_run`` with a recording stub
dispatcher; the ``nx_answer`` wiring through the same patched seams ``tests/test_nx_answer.py``'s
zero-evidence tests use (no storage). The single engine-backed claim, that the RDR-084 fixture's
vector stream now reads only ``rdr__`` collections, lives in the existing tenant test
``tests/test_rdr232_golden_fixture.py``.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from nexus.plans.bundle import _extract_tool_name, is_operator_tool
from nexus.plans.match import Match
from nexus.plans.runner import PlanRunStepRefError, plan_run

BUILTIN_DIR = Path(__file__).resolve().parents[1] / "conexus" / "plans" / "builtin"
RETRIEVAL = frozenset({
    "search", "query", "search_metadata_scoped", "search_topic_scoped",
    "search_graph_hop", "search_aspect_scoped",
})
ART_RDR_COLLECTION = "rdr__1-1__bge-base-en-v15-768__v1"
CODE_COLLECTION = "code__1-1__bge-base-en-v15-768__v1"
KNOWLEDGE_COLLECTION = "knowledge__nexus__bge-base-en-v15-768__v1"


# ── helpers ─────────────────────────────────────────────────────────────────


def _match(plan: dict, *, default_bindings: dict | None = None, dimensions: dict | None = None,
           required: list[str] | None = None) -> Match:
    return Match(
        plan_id=7, name="fixture", description="fixture", confidence=0.9,
        dimensions=dimensions or {"verb": "test"}, tags="", plan_json=json.dumps(plan),
        required_bindings=list(required or []), optional_bindings=[],
        default_bindings=default_bindings or {}, parent_dims=None,
    )


class _Recorder:
    """Stub dispatcher: records every call, answers with the structured retrieval shape."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, dict(args)))
        if tool in RETRIEVAL:
            return {
                "ids": ["c1"], "tumblers": ["1.1.1"], "distances": [0.1],
                "collections": [ART_RDR_COLLECTION], "chunk_collections": [ART_RDR_COLLECTION],
                "chunk_text_hash": ["h1"], "contents": ["body"],
            }
        if tool == "traverse":
            if not args.get("seeds"):
                return {"ids": [], "tumblers": [], "collections": []}
            return {"ids": ["g1"], "tumblers": ["1.1.2"], "collections": [CODE_COLLECTION]}
        if tool == "store_get_many":
            return {"contents": ["c"] if args.get("ids") else [], "missing": []}
        return {"text": f"{tool}(stub)"}

    def corpora(self) -> list[tuple[str, str]]:
        return [(t, a.get("corpus", "")) for t, a in self.calls if t in RETRIEVAL]


def _first_operator(steps: list[dict]) -> int | None:
    return next((i for i, s in enumerate(steps) if is_operator_tool(_extract_tool_name(s))), None)


def _run(match: Match, bindings: dict, *, cut: int | None = None, rec: _Recorder | None = None):
    rec = rec or _Recorder()
    result = asyncio.run(plan_run(
        match, bindings, dispatcher=rec, continuation_cut_at_step=cut, bundle_operators=False,
    ))
    return result, rec


def _builtin(strategy: str, verb: str) -> Match:
    for path in sorted(BUILTIN_DIR.glob("*.yml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if data["dimensions"]["strategy"] == strategy and data["dimensions"]["verb"] == verb:
            return _match(
                data["plan_json"], default_bindings=dict(data.get("default_bindings", {})),
                dimensions=data["dimensions"], required=list(data.get("required_bindings", [])),
            )
    raise LookupError(f"{verb}:{strategy}")


_BINDINGS = {k: f"v-{k}" for k in (
    "question", "intent", "concept", "symptom", "area", "criterion", "change_set", "document", "author",
)} | {"content_type": "rdr"}


# ── test 1: a named-RDR request with scope=rdr searches only rdr ────────────


@pytest.mark.parametrize(
    ("strategy", "verb", "searches"),
    [
        pytest.param("hybrid-factual-lookup", "lookup", 1, id="plan-473-shape-corpus-binding-all"),
        # research-default: two searches pinned `corpus: all`, one with taxonomy_domain prose and
        # one with code (observation 8): both narrow to rdr, neither is skipped.
        pytest.param("default", "research", 2, id="research-default-taxonomy-domain-steps"),
    ],
)
def test_scope_rdr_narrows_a_builtin_that_pins_corpus_all(strategy: str, verb: str, searches: int) -> None:
    match = _builtin(strategy, verb)
    steps = json.loads(match.plan_json)["steps"]
    result, rec = _run(
        match, {**_BINDINGS, "_nx_scope": "rdr"}, cut=_first_operator(steps),
    )
    corpora = rec.corpora()
    assert len(corpora) == searches, corpora
    assert all(c == "rdr" for _, c in corpora), corpora
    assert not any(isinstance(o, dict) and "scope_conflict" in o for o in result.steps)


# ── test 2: one pinned step conflicts and is skipped; the rest run narrowed ──

MULTI_STEP_PLAN = {
    "steps": [
        {"tool": "search", "args": {"query": "$intent", "corpus": "code"}},
        {"tool": "search", "args": {"query": "$intent", "corpus": f"knowledge,{ART_RDR_COLLECTION}"}},
        {"tool": "query", "args": {"question": "$intent", "corpus": "rdr,code"}},
        {"tool": "search", "args": {"query": "$intent"}, "scope": {"taxonomy_domain": "code"}},
        {"tool": "traverse", "args": {"seeds": ["$step1.tumblers", "$step2.tumblers"], "depth": 1}},
        {"tool": "store_get_many", "args": {
            "ids": ["$step1.ids", "$step2.ids", "$step3.ids"],
            "collections": ["$step1.collections", "$step2.collections", "$step3.collections"],
        }},
        {"tool": "summarize", "args": {"inputs": "$step6.contents"}},
    ],
}


def test_conflicting_step_is_skipped_and_disclosed_while_the_others_run_narrowed() -> None:
    result, rec = _run(_match(MULTI_STEP_PLAN), {"intent": "q", "_nx_scope": "rdr"}, cut=6)

    # step 1 (code) and step 4 (domain-derived corpus `code`) conflict: neither is dispatched.
    assert rec.corpora() == [("search", ART_RDR_COLLECTION), ("query", "rdr")]
    for i in (0, 3):
        out = result.steps[i]
        for field in ("ids", "tumblers", "collections", "chunk_collections", "chunk_text_hash",
                      "distances", "contents"):
            assert out[field] == [], (i, field, out)
        marker = out["scope_conflict"]
        assert marker["caller_scope"] == "rdr"
        assert marker["plan_corpus"] == "code"
    assert "scope_conflict" not in result.steps[1] and "scope_conflict" not in result.steps[2]
    assert result.steps[0]["scope_conflict"]["step_index"] == 0

    by_step = {d["step_index"]: d for d in result.scope_diagnostics}
    assert by_step[0]["outcome"] == "conflict" and by_step[3]["outcome"] == "conflict"
    assert by_step[1]["outcome"] == "narrowed" and by_step[1]["resolved_corpus"] == ART_RDR_COLLECTION
    assert by_step[2]["outcome"] == "narrowed" and by_step[2]["resolved_corpus"] == "rdr"
    assert by_step[3]["plan_corpus"] == "code"
    assert all(d["caller_scope"] == "rdr" and d["scope_kind"] == "corpus_prefix"
               for d in result.scope_diagnostics)
    # the skipped steps contributed no evidence to the later steps
    assert dict(rec.calls)["traverse"]["seeds"] == ["1.1.1"]


def test_collection_pinned_step_keeps_todays_behaviour_and_is_reported_unscoped() -> None:
    plan = {"steps": [{"tool": "search", "args": {"query": "q", "collection": CODE_COLLECTION}}]}
    result, rec = _run(_match(plan), {"_nx_scope": "rdr"})
    assert rec.calls[0][1]["collection"] == CODE_COLLECTION
    assert "corpus" not in rec.calls[0][1]
    assert [(d["step_index"], d["outcome"]) for d in result.scope_diagnostics] == [(0, "unscoped_collection_pin")]


# ── test 4: every retrieval step conflicts ──────────────────────────────────

ALL_CONFLICT_PLAN = {
    "steps": [
        {"tool": "search", "args": {"query": "$intent", "corpus": "code"}},
        {"tool": "query", "args": {"question": "$intent", "corpus": "docs"}},
        {"tool": "traverse", "args": {"seeds": ["$step1.tumblers", "$step2.tumblers"]}},
        {"tool": "store_get_many", "args": {
            "ids": ["$step1.ids", "$step2.ids"],
            "collections": ["$step1.collections", "$step2.collections"],
        }},
        {"tool": "summarize", "args": {"inputs": "$step4.contents"}},
    ],
}


def test_every_retrieval_step_conflicting_is_empty_retrieval_and_no_step_ref_error() -> None:
    from nexus.mcp.core import _nx_answer_is_empty_retrieval

    try:
        result, rec = _run(_match(ALL_CONFLICT_PLAN), {"intent": "q", "_nx_scope": "rdr"}, cut=4)
    except PlanRunStepRefError as exc:  # pragma: no cover - the failure this test exists to catch
        pytest.fail(f"a $stepN reference into a skipped step must resolve to the empty value: {exc}")
    assert rec.corpora() == []
    assert [d["outcome"] for d in result.scope_diagnostics] == ["conflict", "conflict"]
    assert all(isinstance(o.get("scope_conflict"), dict) for o in result.steps[:2])
    # the later steps ran on the empty value
    assert dict(rec.calls)["traverse"]["seeds"] == []
    assert dict(rec.calls)["store_get_many"]["ids"] == []
    # a run whose retrieval steps all conflicted takes the empty-retrieval path
    assert _nx_answer_is_empty_retrieval(result.steps[:2]) is True
    assert _nx_answer_is_empty_retrieval(result.steps) is True
    # ... and one conflicted step among steps that found evidence does not
    assert _nx_answer_is_empty_retrieval([result.steps[0], {"ids": ["c1"]}]) is False


# ── test 5: no caller scope: every builtin resolves the corpora it resolved before ──

#: Recorded from the unmodified runner at b3d57227d, before this bead: `verb:strategy` ->
#: (step index, tool, resolved corpus) for every retrieval step, run with the generic bindings
#: above and no `_nx_scope`. Builtin YAML pins stay exactly as they were (RDR-232 section 2a).
PRE_CHANGE_RESOLVED_CORPORA: dict[str, list[list]] = {
    "query:abstract-themes": [[0, "search", "all"]],
    "analyze:default": [[0, "search", "all"], [1, "search", "all"]],
    "research:citation-traversal": [[0, "query", "all"]],
    "research:corpus-coverage-check": [[0, "query", "knowledge,code,docs,rdr"]],
    "debug:default": [[0, "query", "all"]],
    "document:default": [[0, "query", "all"], [1, "search", "all"]],
    "research:document-discovery": [[0, "query", "knowledge,code,docs,rdr"]],
    "research:find-by-author": [[0, "search_metadata_scoped", "all"]],
    "lookup:hybrid-factual-lookup": [[0, "search", "all"]],
    "research:default": [[0, "search", "all"], [2, "search", "all"]],
    "review:default": [[0, "query", "all"]],
    "research:type-scoped": [[0, "search_metadata_scoped", "all"]],
}


def test_no_caller_scope_leaves_every_builtin_corpus_as_it_was() -> None:
    seen: dict[str, list[list]] = {}
    for path in sorted(BUILTIN_DIR.glob("*.yml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        dims = data["dimensions"]
        match = _builtin(dims["strategy"], dims["verb"])
        steps = data["plan_json"]["steps"]
        result, _rec = _run(match, dict(_BINDINGS), cut=_first_operator(steps))
        seen[f"{dims['verb']}:{dims['strategy']}"] = [
            [a["step_index"], a["tool"], a["corpus"]] for a in result.resolved_step_args
        ]
        assert result.scope_diagnostics == [], "no caller scope: nothing to report"
    assert len(seen) == len(PRE_CHANGE_RESOLVED_CORPORA) == 12, sorted(seen)
    assert seen == PRE_CHANGE_RESOLVED_CORPORA


# ── test 7: the continuation path applies the same narrowing ────────────────


def test_continuation_prefix_is_narrowed_and_a_fully_conflicted_prefix_falls_back_to_headless() -> None:
    from nexus.plans.continuation import classify_continuation_cut
    from nexus.plans.continuation_envelope import assemble_continuation_envelope

    plan = {"steps": [
        {"tool": "search", "args": {"query": "$intent", "corpus": "all"}},
        {"tool": "summarize", "args": {"content": "reduce the evidence"}},
    ]}
    steps = plan["steps"]
    result, rec = _run(_match(plan), {"intent": "q", "_nx_scope": "rdr"}, cut=1)
    assert rec.corpora() == [("search", "rdr")]
    cut = classify_continuation_cut(steps)
    envelope = assemble_continuation_envelope(
        cut=cut, steps=steps, bindings={"intent": "q", "_nx_scope": "rdr"},
        step_outputs=result.steps, plan_id=1, run_id=None,
    )
    assert envelope is not None, "a narrowed prefix with evidence still hands off"

    conflicted = {"steps": [
        {"tool": "search", "args": {"query": "$intent", "corpus": "code"}},
        {"tool": "summarize", "args": {"content": "reduce the evidence"}},
    ]}
    result2, rec2 = _run(_match(conflicted), {"intent": "q", "_nx_scope": "rdr"}, cut=1)
    assert rec2.corpora() == [], "the conflicting prefix step is skipped on the continuation path too"
    assert result2.scope_diagnostics[0]["outcome"] == "conflict"
    envelope2 = assemble_continuation_envelope(
        cut=classify_continuation_cut(conflicted["steps"]), steps=conflicted["steps"],
        bindings={"intent": "q", "_nx_scope": "rdr"}, step_outputs=result2.steps, plan_id=1, run_id=None,
    )
    assert envelope2 is None, "an empty (all-conflicted) prefix falls back to the headless run"


def test_continuation_suffix_resolution_runs_the_runners_caller_scope_helper() -> None:
    from nexus.plans import continuation_envelope as ce
    from nexus.plans import runner
    from nexus.plans.continuation import classify_continuation_cut

    steps = [
        {"tool": "search", "args": {"query": "x"}},
        {"tool": "summarize", "args": {"content": "reduce"}},
    ]
    cut = classify_continuation_cut(steps)
    seen: list[tuple[str, dict]] = []
    real = runner._apply_caller_scope_to_args

    def spy(tool, args, *, bindings):
        seen.append((tool, dict(bindings)))
        return real(tool, args, bindings=bindings)

    with patch.object(ce, "_apply_caller_scope_to_args", spy):
        ce._compose_shape_b(cut, steps, {"_nx_scope": "rdr"}, [{"ids": ["c1"]}])
    assert seen == [("summarize", {"_nx_scope": "rdr"})]


# ── test 6 and the narrowing rule, as pure functions ────────────────────────

COLLECTION = "rdr__1-1__bge-base-en-v15-768__v1"


def test_scope_discriminator() -> None:
    from nexus.plans.evidence import OwnerNameError, ScopeConstraint

    assert ScopeConstraint.parse("1.2") == ScopeConstraint("subtree", "1.2")
    assert ScopeConstraint.parse("1.2.4686").kind == "subtree"
    assert ScopeConstraint.parse("rdr") == ScopeConstraint("corpus_prefix", "rdr")
    assert ScopeConstraint.parse("knowledge__x").kind == "corpus_prefix", "a two-segment name is a prefix"
    assert ScopeConstraint.parse(COLLECTION) == ScopeConstraint("collection", COLLECTION)
    assert ScopeConstraint.parse("  ") == ScopeConstraint("none", "")
    assert ScopeConstraint.parse(None).kind == "none"

    asked: list[str] = []

    def lookup(name: str) -> str:
        asked.append(name)
        if name == "ART":
            return "1.61"
        raise OwnerNameError(f"{name} is not an owner")

    assert ScopeConstraint.parse("ART", name_lookup=lookup) == ScopeConstraint("subtree", "1.61")
    assert ScopeConstraint.parse("nosuch", name_lookup=lookup).kind == "corpus_prefix"
    # a corpus word, a collection name and a tumbler are never sent to the owner lookup
    for token in ("rdr", "knowledge", "all", COLLECTION, "1.2"):
        ScopeConstraint.parse(token, name_lookup=lookup)
    assert asked == ["ART", "nosuch"]


@pytest.mark.parametrize(
    ("plan_corpus", "scope", "status", "corpus"),
    [
        pytest.param(None, "rdr", "scope_applied", "rdr", id="none-gives-the-scope"),
        pytest.param("", "rdr", "scope_applied", "rdr", id="blank-gives-the-scope"),
        pytest.param("all", "rdr", "narrowed", "rdr", id="all-is-no-restriction"),
        pytest.param("all,code", "rdr", "narrowed", "rdr", id="all-inside-a-list"),
        pytest.param("knowledge,code,docs,rdr", "rdr", "narrowed", "rdr", id="prefix-list"),
        pytest.param(f"{COLLECTION},code", "rdr", "narrowed", COLLECTION, id="entry-under-scope"),
        pytest.param("code", "rdr", "conflict", None, id="no-overlap-conflicts"),
        pytest.param("code,docs", "rdr", "conflict", None, id="no-overlap-in-a-list-conflicts"),
        pytest.param("rdr", "rdr", "within_scope", "rdr", id="equal"),
        pytest.param(["knowledge", "rdr"], "rdr", "narrowed", "rdr", id="list-valued-binding"),
        pytest.param("knowledge", "knowledge__x", "narrowed", "knowledge__x", id="scope-under-entry"),
        pytest.param("knowledge__x", "knowledge", "within_scope", "knowledge__x", id="entry-under-scope-2seg"),
        pytest.param("knowledge,code", KNOWLEDGE_COLLECTION, "narrowed", KNOWLEDGE_COLLECTION,
                     id="full-collection-narrows-prefix-entry"),
        pytest.param(f"knowledge,knowledge__nexus,{CODE_COLLECTION}", KNOWLEDGE_COLLECTION, "narrowed",
                     KNOWLEDGE_COLLECTION, id="several-entries-collapse-to-one"),
        pytest.param(CODE_COLLECTION, KNOWLEDGE_COLLECTION, "conflict", None, id="collection-vs-collection"),
        pytest.param(COLLECTION, "rdr__1", "conflict", None, id="a-string-prefix-is-not-a-segment-match"),
        pytest.param("code", "all", "scope_all", "code", id="scope-all-restricts-nothing"),
        pytest.param(None, "all", "scope_all", "all", id="scope-all-with-no-plan-corpus"),
        pytest.param("code", "1.2", "subtree_not_applied", "code", id="subtree-is-never-a-corpus"),
        pytest.param(None, "1.2", "subtree_not_applied", None, id="subtree-with-no-plan-corpus"),
    ],
)
def test_narrow_corpus_rule(plan_corpus, scope: str, status: str, corpus: str | None) -> None:
    from nexus.plans.evidence import ScopeConstraint, narrow_corpus

    got = narrow_corpus(plan_corpus, ScopeConstraint.parse(scope))
    assert (got.status, got.corpus) == (status, corpus), got


def test_no_scope_changes_nothing() -> None:
    from nexus.plans.evidence import ScopeConstraint, narrow_corpus

    got = narrow_corpus("all", ScopeConstraint.parse(""))
    assert got.status == "no_scope" and got.corpus == "all"


# ── test 3 and the disclosure: the nx_answer wiring (no tenant) ─────────────


def _single_query_match(corpus: str | None, *, default_corpus: bool = True) -> Match:
    args: dict = {"question": "$intent"}
    if corpus is not None:
        args["corpus"] = corpus
    return Match(
        plan_id=21, name="single", description="single", confidence=0.9, dimensions={}, tags="",
        plan_json=json.dumps({"steps": [{"tool": "query", "args": args}]}),
        required_bindings=["intent"], optional_bindings=[], default_bindings={"intent": "q"},
        parent_dims=None,
    )


def _drive_nx_answer(match: Match, *, scope: str, structured: bool = False, plan_run_result=None,
                     db=None, bindings=None):
    """Call ``nx_answer`` with the matcher, the plan runner, T2 and the catalog patched away.

    *db* is the T2 stub (a ``MagicMock``) the run records into, for a test that reads the outcome
    counters; a fresh one otherwise."""
    import nexus.mcp.core as core

    query_calls: list[dict] = []

    def _query(**kwargs):
        query_calls.append(kwargs)
        if kwargs.get("structured"):
            return {"ids": ["c1"], "chunk_collections": [ART_RDR_COLLECTION], "chunk_text_hash": ["h"],
                    "distances": [0.1], "collections": [ART_RDR_COLLECTION]}
        return "canned query result"

    db_stub = db if db is not None else MagicMock(plans=MagicMock())
    db_stub.conn = MagicMock()
    patches = [
        patch("nexus.plans.matcher.plan_match", return_value=[match]),
        patch("nexus.mcp.core._get_catalog", return_value=None),
        patch("nexus.mcp.core._t2_ctx"),
        patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)),
        patch("nexus.mcp.core.scratch", return_value="ok"),
        patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None),
        patch("nexus.mcp.core.query", side_effect=_query),
    ]
    if plan_run_result is not None:
        patches.append(patch("nexus.plans.runner.plan_run", AsyncMock(return_value=plan_run_result)))
    from contextlib import ExitStack

    with ExitStack() as stack:
        mocks = [stack.enter_context(p) for p in patches]
        mocks[2].return_value.__enter__.return_value = db_stub
        out = asyncio.run(core.nx_answer(question="which documents discuss widgets", scope=scope,
                                         structured=structured, bindings=bindings))
    return out, query_calls


@pytest.mark.parametrize(
    ("plan_corpus", "scope", "expect_calls"),
    [
        pytest.param("knowledge,code,docs,rdr", "rdr", [{"corpus": "rdr", "subtree": ""}],
                     id="plan-pinned-to-four-prefixes-queries-rdr-only"),
        pytest.param(None, "rdr", [{"corpus": "rdr", "subtree": ""}], id="no-plan-corpus-takes-the-scope"),
        pytest.param("all", "rdr", [{"corpus": "rdr", "subtree": ""}], id="all-narrows"),
        pytest.param("code", "rdr", [], id="plan-pinned-to-code-conflicts-and-query-is-not-called"),
        pytest.param("knowledge,code,docs,rdr", "1.61",
                     [{"corpus": "knowledge,code,docs,rdr", "subtree": "1.61"}],
                     id="subtree-scope-filters-a-pinned-corpus-like-the-runner-does"),
        pytest.param("all", "1.61", [{"corpus": "all", "subtree": "1.61"}], id="subtree-scope-filters-corpus-all"),
        pytest.param(None, "1.61", [{"corpus": "knowledge,code,docs,rdr", "subtree": "1.61"}],
                     id="subtree-scope-filters-a-corpus-less-step-on-the-default"),
        pytest.param("code,docs", "1.2.4686", [{"corpus": "code,docs", "subtree": ""}],
                     id="document-depth-scope-is-not-passed-as-a-filter"),
        pytest.param("knowledge,code,docs,rdr", "",
                     [{"corpus": "knowledge,code,docs,rdr", "subtree": ""}], id="no-scope-unchanged"),
    ],
)
def test_single_query_fast_path_applies_the_caller_scope(plan_corpus, scope, expect_calls) -> None:
    out, calls = _drive_nx_answer(_single_query_match(plan_corpus), scope=scope)
    assert [{k: c.get(k) for k in ("corpus", "subtree")} for c in calls] == expect_calls
    if not expect_calls:
        assert "No matching evidence" in out and "scope conflict" in out
        assert "code" in out and "rdr" in out, "the notice names the plan corpus and the caller scope"


def test_single_query_conflict_envelope_is_a_no_evidence_non_answer_with_the_diagnostic() -> None:
    out, calls = _drive_nx_answer(_single_query_match("code"), scope="rdr", structured=True)
    assert calls == []
    assert out["answer_shape"] == "no_evidence"
    (diag,) = out["scope_diagnostics"]
    assert (diag["outcome"], diag["plan_corpus"], diag["caller_scope"]) == ("conflict", "code", "rdr")
    assert out["final_text"].startswith("[non-answer: no_evidence]")


def test_single_query_envelope_always_carries_scope_diagnostics() -> None:
    out, _ = _drive_nx_answer(_single_query_match("knowledge,code,docs,rdr"), scope="", structured=True)
    assert out["scope_diagnostics"] == []
    out, _ = _drive_nx_answer(_single_query_match("knowledge,code,docs,rdr"), scope="rdr", structured=True)
    (diag,) = out["scope_diagnostics"]
    assert (diag["outcome"], diag["resolved_corpus"]) == ("narrowed", "rdr")


def _plan_run_result(steps: list[dict], diagnostics: list[dict]) -> MagicMock:
    res = MagicMock()
    res.steps = steps
    res.step_records = []
    res.scope_diagnostics = diagnostics
    res.resolved_step_args = []
    res.budget_exhausted_at_step = None
    return res


CONFLICT_DIAG = {
    "step_index": 0, "tool": "search", "plan_corpus": "code", "caller_scope": "rdr",
    "scope_kind": "corpus_prefix", "outcome": "conflict", "resolved_corpus": None,
}
RAN_DIAG = {
    "step_index": 1, "tool": "search", "plan_corpus": "all", "caller_scope": "rdr",
    "scope_kind": "corpus_prefix", "outcome": "narrowed", "resolved_corpus": "rdr",
}

_MULTI_PLAN = _match(
    {"steps": [
        {"tool": "search", "args": {"query": "$intent", "corpus": "code"}},
        {"tool": "search", "args": {"query": "$intent", "corpus": "all"}},
        {"tool": "summarize", "args": {"inputs": "$step2.ids"}},
    ]},
    default_bindings={"intent": "q"}, required=["intent"],
)


def test_partial_conflict_adds_a_one_line_notice_after_the_shape_is_classified() -> None:
    steps = [
        {"ids": [], "tumblers": [], "scope_conflict": {"step_index": 0}},
        {"ids": ["c1"], "tumblers": ["1.1.1"], "chunk_collections": [ART_RDR_COLLECTION]},
        {"text": "The decision is X [1]. It was accepted on 2026-10-01 after review of the evidence."},
    ]
    res = _plan_run_result(steps, [CONFLICT_DIAG, RAN_DIAG])
    text, _ = _drive_nx_answer(_MULTI_PLAN, scope="rdr", plan_run_result=res)
    assert text.startswith("[scope conflict:"), text
    assert text.count("[scope conflict:") == 1 and "step 1" in text.splitlines()[0]
    assert "The decision is X" in text
    env, _ = _drive_nx_answer(_MULTI_PLAN, scope="rdr", structured=True, plan_run_result=res)
    assert env["answer_shape"] == "answered", "the notice is added after classify_answer_shape ran"
    assert [d["outcome"] for d in env["scope_diagnostics"]] == ["conflict", "narrowed"]
    assert env["final_text"].splitlines()[0].startswith("[scope conflict:")


def test_every_step_conflicting_gives_the_no_match_notice_that_names_the_conflict() -> None:
    steps = [
        {"ids": [], "tumblers": [], "scope_conflict": {"step_index": 0}},
        {"ids": [], "tumblers": [], "scope_conflict": {"step_index": 1}},
        {"text": "summary of nothing"},
    ]
    both = [CONFLICT_DIAG, {**CONFLICT_DIAG, "step_index": 1, "plan_corpus": "docs"}]
    res = _plan_run_result(steps, both)
    env, _ = _drive_nx_answer(_MULTI_PLAN, scope="rdr", structured=True, plan_run_result=res)
    assert env["answer_shape"] == "no_evidence"
    assert "No matching evidence found" in env["final_text"]
    assert "scope conflict" in env["final_text"]
    assert "code" in env["final_text"] and "docs" in env["final_text"] and "rdr" in env["final_text"]
    assert env["final_text"].count("scope conflict") == 1, "named once, not repeated by a second notice"
    assert len(env["scope_diagnostics"]) == 2




# ── nexus-jypmj.1.16: a catalog-subtree scope is a filter, whatever the step's corpus ───────────
#
# A subtree such as 1.2 used to be forwarded as `corpus`, which resolves to no collection. It now
# rides as the tool's own `subtree` argument on `query` and `search_metadata_scoped` for a step with
# no corpus, `all` or a list alike, and the corpus is left as it is. The other retrieval tools have no
# subtree filter: the step runs as it did, and the answer text says the scope did not reach it. A
# document-depth address (1.2.4686) is not a subtree filter at all and is disclosed, never passed.

DEFAULT_CORPUS = "knowledge,code,docs,rdr"
FILTER_TOOLS = ("query", "search_metadata_scoped")
NO_FILTER_TOOLS = ("search", "search_topic_scoped", "search_graph_hop", "search_aspect_scoped")


def _step(tool: str, corpus: object = None, **extra) -> dict:
    args: dict = {("question" if tool == "query" else "query"): "$intent", **extra}
    if corpus is not None:
        args["corpus"] = corpus
    return {"tool": tool, "args": args}


def _subtree_calls(plan: dict, scope: str = "1.2", **bindings):
    result, rec = _run(_match(plan), {"intent": "q", "_nx_scope": scope, **bindings})
    return result, rec


CORPUS_SHAPES = [
    pytest.param(None, id="no-corpus"),
    pytest.param("all", id="corpus-all"),
    pytest.param("code,docs", id="corpus-list"),
    pytest.param(["code", "docs"], id="corpus-list-valued"),
]


@pytest.mark.parametrize("tool", FILTER_TOOLS)
@pytest.mark.parametrize("corpus", CORPUS_SHAPES)
def test_a_subtree_filters_a_query_or_metadata_step_whatever_its_corpus(tool: str, corpus) -> None:
    result, rec = _subtree_calls({"steps": [_step(tool, corpus)]})
    (called, args), = rec.calls
    assert called == tool and args["subtree"] == "1.2", args
    if corpus is None:
        assert args["corpus"] == DEFAULT_CORPUS, "no corpus falls to the default (rdr included), never to the subtree"
    else:
        assert args["corpus"] == corpus, "a pinned corpus is left as it is"
    (diag,) = result.scope_diagnostics
    assert (diag["outcome"], diag["tool"], diag["scope_kind"]) == ("subtree_filtered", tool, "subtree")
    assert not any(isinstance(o, dict) and "scope_conflict" in o for o in result.steps), "no step is skipped"
    assert result.resolved_step_args[0]["subtree"] == "1.2"


@pytest.mark.parametrize("tool", NO_FILTER_TOOLS)
@pytest.mark.parametrize("corpus", CORPUS_SHAPES)
def test_a_step_on_a_tool_with_no_subtree_filter_runs_as_before_and_is_disclosed(tool: str, corpus) -> None:
    from nexus.plans.evidence import scope_unapplied_notice

    plan = {"steps": [_step(tool, corpus)]}
    result, rec = _subtree_calls(plan)
    _unscoped, rec0 = _run(_match(plan), {"intent": "q"})
    assert rec.calls == rec0.calls, "the step runs exactly as it does with no scope: not skipped, not filtered"
    assert "subtree" not in rec.calls[0][1]
    (diag,) = result.scope_diagnostics
    assert (diag["outcome"], diag["tool"], diag["caller_scope"]) == ("subtree_not_applied", tool, "1.2")
    assert tool in diag["detail"] and "no catalog subtree" in diag["detail"]
    assert not any(isinstance(o, dict) and "scope_conflict" in o for o in result.steps)
    notice = scope_unapplied_notice(result.scope_diagnostics)
    assert notice == f"[scope not applied: scope '1.2' could not filter step 1 ({tool}): {diag['detail']}]"


@pytest.mark.parametrize("tool", (*FILTER_TOOLS, *NO_FILTER_TOOLS))
@pytest.mark.parametrize("corpus", CORPUS_SHAPES)
def test_a_document_depth_scope_is_never_passed_as_a_filter_and_is_disclosed(tool: str, corpus) -> None:
    from nexus.plans.evidence import scope_unapplied_clauses

    plan = {"steps": [_step(tool, corpus)]}
    result, rec = _subtree_calls(plan, scope="1.2.4686")
    _unscoped, rec0 = _run(_match(plan), {"intent": "q"})
    assert rec.calls == rec0.calls, "the step runs as it would with no scope; the address is not truncated to an owner"
    (diag,) = result.scope_diagnostics
    assert (diag["outcome"], diag["detail"]) == ("subtree_not_applied", "a document-level address is not a subtree filter")
    assert scope_unapplied_clauses(result.scope_diagnostics) == [
        f"scope '1.2.4686' could not filter step 1 ({tool}): a document-level address is not a subtree filter"
    ]


def test_a_subtree_of_two_segments_is_an_owner_filter_and_three_is_a_document() -> None:
    from nexus.plans.evidence import ScopeConstraint

    assert not ScopeConstraint.parse("1.2").is_document_address
    assert ScopeConstraint.parse("1.2.4686").is_document_address
    assert not ScopeConstraint.parse("rdr").is_document_address


def test_a_pinned_collection_step_is_still_reported_unscoped_under_a_subtree() -> None:
    plan = {"steps": [{"tool": "query", "args": {"question": "q", "collection": CODE_COLLECTION}}]}
    result, rec = _subtree_calls(plan)
    assert "subtree" not in rec.calls[0][1]
    assert [d["outcome"] for d in result.scope_diagnostics] == ["unscoped_collection_pin"]


def test_a_taxonomy_domain_step_keeps_its_corpus_and_a_query_step_is_still_filtered() -> None:
    plan = {"steps": [
        {"tool": "query", "args": {"question": "$intent"}, "scope": {"taxonomy_domain": "code"}},
        {"tool": "search", "args": {"query": "$intent"}, "scope": {"taxonomy_domain": "code"}},
    ]}
    result, rec = _subtree_calls(plan)
    (_q, qargs), (_s, sargs) = rec.calls
    assert qargs["subtree"] == "1.2" and qargs["corpus"] != "1.2"
    assert "subtree" not in sargs and sargs["corpus"] != "1.2"
    assert [d["outcome"] for d in result.scope_diagnostics] == ["subtree_filtered", "subtree_not_applied"]


def test_a_step_with_its_own_subtree_keeps_it_and_says_the_scope_did_not_apply() -> None:
    plan = {"steps": [{"tool": "query", "args": {"question": "$intent", "subtree": "1.9"}}]}
    result, rec = _subtree_calls(plan)
    (_t, args), = rec.calls
    assert args["subtree"] == "1.9", "the plan's own catalog filter is not replaced by the caller's"
    (diag,) = result.scope_diagnostics
    assert diag["outcome"] == "subtree_not_applied" and "'1.9'" in diag["detail"]


def test_a_subtree_scope_alone_never_narrows_by_owner() -> None:
    plan = {"steps": [{"tool": "query", "args": {"question": "$intent"}}]}
    result, rec = _subtree_calls(plan)
    assert rec.calls[0][1]["corpus"] == DEFAULT_CORPUS, "no owner expansion of the corpus"
    assert all(d["scope_kind"] != "owner" for d in result.scope_diagnostics)


@pytest.mark.parametrize("corpus", [None, "all", "knowledge,code,docs,rdr", "code"])
@pytest.mark.parametrize("scope", ["1.2", "1.2.4686"])
def test_runner_and_single_query_fast_path_agree_on_args_and_outcome(corpus, scope: str) -> None:
    plan = {"steps": [_step("query", corpus)]}
    result, rec = _subtree_calls(plan, scope=scope)
    (_t, runner_args), = rec.calls
    env, fast_calls = _drive_nx_answer(_single_query_match(corpus), scope=scope, structured=True)
    (fast,) = fast_calls
    assert {k: runner_args.get(k, "") for k in ("corpus", "subtree")} == {k: fast[k] for k in ("corpus", "subtree")}
    (runner_diag,) = result.scope_diagnostics
    (fast_diag,) = env["scope_diagnostics"]
    assert runner_diag == fast_diag, "same outcome, same corpus fields, same detail"
    assert runner_diag["outcome"] == ("subtree_not_applied" if scope == "1.2.4686" else "subtree_filtered")


def test_subtree_filter_tool_set_is_the_set_of_corpus_tools_whose_signature_has_a_subtree_parameter() -> None:
    import inspect

    import nexus.mcp.core as core
    from nexus.plans import runner

    takes_subtree = {
        name for name in runner._CORPUS_QUERY_TOOLS
        if "subtree" in inspect.signature(getattr(core, name)).parameters
    }
    assert takes_subtree == set(runner._SUBTREE_FILTER_TOOLS) == set(FILTER_TOOLS)
    assert set(runner._CORPUS_QUERY_TOOLS) == set(FILTER_TOOLS) | set(NO_FILTER_TOOLS)


def test_every_builtin_query_and_metadata_step_is_filtered_by_a_subtree_scope_and_the_rest_are_named() -> None:
    from nexus.plans.evidence import scope_unapplied_clauses

    filtered = unapplied = 0
    for path in sorted(BUILTIN_DIR.glob("*.yml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        dims = data["dimensions"]
        match = _builtin(dims["strategy"], dims["verb"])
        steps = data["plan_json"]["steps"]
        result, rec = _run(match, {**_BINDINGS, "_nx_scope": "1.2"}, cut=_first_operator(steps))
        retrieval = [(t, a) for t, a in rec.calls if t in RETRIEVAL]
        assert len(retrieval) == len(result.scope_diagnostics) == len(result.resolved_step_args), path.name
        for (tool, args), diag in zip(retrieval, result.scope_diagnostics, strict=True):
            if tool in FILTER_TOOLS:
                assert args.get("subtree") == "1.2", (path.name, tool, args)
                assert diag["outcome"] == "subtree_filtered", (path.name, diag)
                filtered += 1
            else:
                assert "subtree" not in args, (path.name, tool, args)
                assert diag["outcome"] == "subtree_not_applied" and tool in diag["detail"], (path.name, diag)
                unapplied += 1
        assert len(scope_unapplied_clauses(result.scope_diagnostics)) == sum(
            1 for t, _a in retrieval if t not in FILTER_TOOLS
        ), path.name
        assert not any(isinstance(o, dict) and "scope_conflict" in o for o in result.steps), path.name
    expected_filtered = sum(1 for rows in PRE_CHANGE_RESOLVED_CORPORA.values() for _i, t, _c in rows if t in FILTER_TOOLS)
    expected_unapplied = sum(1 for rows in PRE_CHANGE_RESOLVED_CORPORA.values() for _i, t, _c in rows if t not in FILTER_TOOLS)
    assert (filtered, unapplied) == (expected_filtered, expected_unapplied)
    assert filtered >= 8 and unapplied >= 7, "non-vacuity: the sweep examined the builtins' real retrieval steps"


def test_a_subtree_filtered_step_records_its_subtree_in_the_resolved_args_and_the_basis_text() -> None:
    from nexus.mcp.core import _render_zero_evidence_step_basis_text

    plan = {"steps": [
        _step("query"),
        {"tool": "search", "args": {"query": "$intent", "corpus": "code"}},
    ]}
    result, _rec = _subtree_calls(plan)
    assert result.resolved_step_args[0]["subtree"] == "1.2"
    assert "subtree" not in result.resolved_step_args[1], "a step the subtree did not reach ran under no filter"
    text = _render_zero_evidence_step_basis_text(resolved_step_args=result.resolved_step_args, plan_json=None)
    assert f"step0=query(corpus={DEFAULT_CORPUS!r}, subtree='1.2', query='q')" in text
    assert "step1=search(corpus='code', query='q')" in text


def test_the_unapplied_scope_reaches_the_answer_text_from_a_real_runner_result() -> None:
    plan = {"steps": [
        _step("search", "all"),
        {"tool": "summarize", "args": {"content": "reduce the evidence"}},
    ]}
    result, _rec = _run(_match(plan, default_bindings={"intent": "q"}, required=["intent"]),
                        {"intent": "q", "_nx_scope": "1.2"}, cut=1)
    match = _match(plan, default_bindings={"intent": "q"}, required=["intent"])
    text, _ = _drive_nx_answer(match, scope="1.2", plan_run_result=result)
    line = "scope '1.2' could not filter step 1 (search): search takes no catalog subtree"
    assert text.count(line) == 1, text
    assert text.splitlines()[0].startswith("[scope not applied:"), text
    env, _ = _drive_nx_answer(match, scope="1.2", structured=True, plan_run_result=result)
    assert [d["outcome"] for d in env["scope_diagnostics"]] == ["subtree_not_applied"]
    assert line in env["final_text"]


def test_an_unapplied_document_scope_is_named_in_the_text_even_when_every_step_returned_nothing() -> None:
    steps = [{"ids": [], "tumblers": []}, {"text": "summary of nothing"}]
    diag = {
        "step_index": 0, "tool": "query", "plan_corpus": "all", "caller_scope": "1.2.4686",
        "scope_kind": "subtree", "outcome": "subtree_not_applied", "resolved_corpus": "all",
        "detail": "a document-level address is not a subtree filter",
    }
    res = _plan_run_result(steps, [diag])
    match = _match({"steps": [_step("query", "all"), {"tool": "summarize", "args": {"inputs": "$step1.ids"}}]},
                   default_bindings={"intent": "q"}, required=["intent"])
    text, _ = _drive_nx_answer(match, scope="1.2.4686", plan_run_result=res)
    assert "could not filter step 1 (query): a document-level address is not a subtree filter" in text
