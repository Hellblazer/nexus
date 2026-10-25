# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 1 (nexus-jypmj.1.2): the reserved bindings ``source``,
``owner`` and ``project``, and the ``SourceRef`` / owner-qualifier inputs.

``nx_answer`` takes the named-source request through ``bindings`` (the
RDR adds no MCP parameter). Three names in that dict are the caller's
evidence request, not plan bindings: ``source`` (an RDR id or catalog
tumbler), ``owner`` and ``project`` (the owner qualifier). They must never
reach plan binding resolution or the matcher's availability set, no plan
may declare them, and an already-saved plan that does is excluded from
matching (logged, not rewritten, not deleted). A catalog-subtree ``scope``
with no owner binding is itself the owner qualifier; two qualifiers agree
when one tumbler is a prefix of the other.

Round-2 gate critique observation 7: "Reserved binding names source, owner,
project: existing library and grown plans not enumerated; behaviour for an
already-saved plan declaring one is unstated." Test 3 pins that behaviour.
The live-library census (0 of 15 plans) is in the bead's comments.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog
from structlog.testing import capture_logs

from nexus.mcp.core import _nx_answer_caller_bindings
from nexus.plans.match import Match

RESERVED = ("source", "owner", "project")

Q_PLAIN = "how does the catalog resolve a tumbler"


# ── Test 1: reserved keys never reach binding resolution or plan_match ───────


@pytest.mark.parametrize("name", RESERVED)
def test_reserved_binding_is_not_a_run_binding_or_available(name: str) -> None:
    run, avail = _nx_answer_caller_bindings(
        question=Q_PLAIN, scope="",
        bindings={name: "RDR-084", "concept": "tumblers"},
    )
    assert name not in run, "reserved key reached plan binding resolution"
    assert name not in avail, (
        "reserved key was advertised to plan_match as an available binding"
    )
    assert run["concept"] == "tumblers", "non-reserved binding was lost"
    assert "concept" in avail


def test_all_three_reserved_keys_together_leave_the_rest_untouched() -> None:
    run, avail = _nx_answer_caller_bindings(
        question=Q_PLAIN, scope="",
        bindings={"source": "RDR-084", "owner": "1.2", "project": "ART"},
    )
    assert run == {"intent": Q_PLAIN}
    assert avail == frozenset({"intent"})


def _make_match() -> Match:
    return Match(
        plan_id=1, name="test-plan", description="test plan", confidence=0.75,
        dimensions={}, tags="",
        plan_json=json.dumps({"steps": [
            {"tool": "search", "args": {"query": "$intent", "corpus": "knowledge"}},
            {"tool": "summarize", "args": {"inputs": "$step1.ids"}},
        ]}),
        required_bindings=["intent"], optional_bindings=[],
        default_bindings={"intent": "test question"}, parent_dims=None,
    )


@pytest.mark.t2_tenant
@pytest.mark.asyncio
async def test_nx_answer_hands_neither_plan_match_nor_plan_run_a_reserved_key(
    tmp_path: Path,
) -> None:
    """End to end through ``nx_answer``: the matcher's availability set and
    the runner's bindings carry none of the three names, while the evidence
    request still reaches the envelope; and the envelope's evidence request
    is None when no reserved key was given (a subtree scope alone is not
    one). One tenant for both claims."""
    import nexus.mcp_infra as _infra
    import nexus.plans.runner as _runner
    from nexus.plans.runner import PlanResult
    from tests.test_nx_answer import _fake_t2_ctx

    match_mock = MagicMock(return_value=[_make_match()])
    run_mock = AsyncMock(return_value=PlanResult(steps=[{"text": "final answer"}]))
    with (
        patch("nexus.plans.matcher.plan_match", match_mock),
        patch.object(_infra, "get_t1_plan_cache",
                     return_value=MagicMock(is_available=False)),
        patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
        patch("nexus.mcp.core.scratch", MagicMock()),
        patch("nexus.mcp.core._get_catalog", return_value=None),
        patch.object(_runner, "plan_run", run_mock),
    ):
        from nexus.mcp.core import nx_answer
        env = await nx_answer(
            "q", structured=True,
            bindings={"source": "RDR-084", "owner": "1.2", "concept": "x"},
        )

    available = match_mock.call_args.kwargs["available_bindings"]
    run_bindings = run_mock.call_args.args[1]
    for name in RESERVED:
        assert name not in available
        assert name not in run_bindings
    assert "concept" in available and run_bindings["concept"] == "x"
    assert isinstance(env, dict)
    request = env["evidence_request"]
    assert request["source"] == {"kind": "rdr", "value": "RDR-084"}
    assert request["owner"]["status"] == "resolved"
    assert request["owner"]["tumbler"] == "1.2"

    # No reserved key: a subtree scope alone is not an evidence request.
    with (
        patch("nexus.plans.matcher.plan_match", return_value=[_make_match()]),
        patch.object(_infra, "get_t1_plan_cache",
                     return_value=MagicMock(is_available=False)),
        patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
        patch("nexus.mcp.core.scratch", MagicMock()),
        patch.object(_runner, "plan_run",
                     AsyncMock(return_value=PlanResult(steps=[{"text": "a"}]))),
    ):
        env = await nx_answer("q", structured=True, scope="1.2")

    assert env["evidence_request"] is None, (
        "a subtree scope alone is not an evidence request"
    )


# ── Test 2: saving a plan that declares a reserved name is refused ───────────


def _plan_declaring(name: str, where: str) -> dict:
    plan: dict = {"steps": [
        {"tool": "search", "args": {"query": "$intent", "corpus": "knowledge"}},
    ]}
    if where == "required_bindings":
        plan["required_bindings"] = ["question", name]
    elif where == "optional_bindings":
        plan["optional_bindings"] = [name]
    elif where == "default_bindings":
        plan["default_bindings"] = {name: "x"}
    elif where == "step_arg_reference":
        plan["steps"][0]["args"]["query"] = f"${name}"
    else:  # pragma: no cover
        raise AssertionError(where)
    return plan


WHERES = ("required_bindings", "optional_bindings", "default_bindings",
          "step_arg_reference")
CASES = [(n, w) for n in RESERVED for w in WHERES]


@pytest.mark.parametrize(("name", "where"), CASES)
def test_validate_plan_steps_refuses_with_the_key_named(name: str, where: str) -> None:
    from nexus.plans.schema import PlanTemplateSchemaError, validate_plan_steps

    with pytest.raises(PlanTemplateSchemaError) as exc:
        validate_plan_steps(_plan_declaring(name, where), require_steps=True)
    assert f"'{name}'" in str(exc.value)
    assert "reserved" in str(exc.value)


@pytest.mark.parametrize(("name", "where"), CASES)
def test_plan_save_tool_refuses_with_the_key_named(name: str, where: str) -> None:
    from nexus.mcp.core import plan_save

    out = plan_save(
        query="q", plan_json=json.dumps(_plan_declaring(name, where)),
        verb="research",
    )
    assert "not saved" in out.lower()
    assert f"'{name}'" in out
    assert "reserved" in out
    assert "evidence request" in out
    # The generic non-executable-plan tail would misdescribe a well-formed plan.
    assert "executable retrieval plans" not in out
    assert "non-empty steps list" not in out


@pytest.mark.parametrize("near_miss", ["sources", "owner_name", "projects", "Source"])
def test_a_near_name_is_not_reserved(near_miss: str) -> None:
    """Non-vacuity: the refusal is the exact names, not a prefix match."""
    from nexus.plans.schema import validate_plan_steps

    plan = _plan_declaring(near_miss, "required_bindings")
    validate_plan_steps(plan, require_steps=True)


@pytest.fixture()
def library(tmp_path: Path):
    from nexus.db.t2 import T2Database

    return T2Database(tmp_path / "m.db").plans


@pytest.mark.t2_tenant
def test_the_library_save_path_refuses_a_reserved_name(library) -> None:
    """The library's save path is the choke point every writer (MCP tool,
    CLI, seed loader, the grow path) shares. ``default_bindings`` is its own
    column, so ``plan_json`` validation cannot see it; ``plan_json``
    declarations are refused too. Every reserved name, both ways, one tenant."""
    from nexus.plans.evidence import ReservedBindingError

    def count() -> int:
        return len(library.list_plans(limit=1000, include_disabled=True))

    before = count()
    for name in RESERVED:
        with pytest.raises(ReservedBindingError, match=f"'{name}'"):
            library.save_plan(
                query="q", plan_json=json.dumps({"steps": []}), verb="research",
                default_bindings=json.dumps({name: "x"}),
            )
        assert count() == before, f"default_bindings {name!r}: a row was written"
        with pytest.raises(ReservedBindingError, match=f"'{name}'"):
            library.save_plan(
                query="q", verb="research",
                plan_json=json.dumps(_plan_declaring(name, "required_bindings")),
            )
        assert count() == before, f"plan_json {name!r}: a row was written"


# ── Test 3: an already-saved plan declaring one is excluded, logged, kept ────


class _FakeCache:
    def __init__(self, hits: list[tuple[int, float]]) -> None:
        self._hits = list(hits)

    @property
    def is_available(self) -> bool:
        return True

    def query(self, intent: str, n: int) -> list[tuple[int, float]]:
        return self._hits[:n]


def _import_plan(library, *, query: str, plan: dict, name: str,
                 defaults: dict | None = None) -> int:
    """Seed a row the save path would now refuse: the migration import path
    writes ``plan_json`` verbatim, which is exactly how a pre-existing or
    imported plan reaches the library."""
    from nexus.plans.schema import canonical_dimensions_json

    dims = {"verb": "research", "scope": "global", "strategy": name}
    return library.import_plan(
        project="nexus", query=query, plan_json=json.dumps(plan),
        outcome="success", tags="", created_at="2026-10-25T00:00:00Z",
        name=name, verb="research", scope="global",
        dimensions=canonical_dimensions_json(dims),
        default_bindings=json.dumps(defaults) if defaults else None,
        # import writes match_text verbatim and the engine's FTS vector is
        # generated from it, so an empty one is invisible to the FTS path.
        match_text=query,
    )


def _info_logging() -> None:
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )


@pytest.mark.t2_tenant
def test_a_saved_plan_declaring_a_reserved_name_is_excluded_logged_and_kept(
    library,
) -> None:
    """Every (path, name, where) case runs against one library, one tenant.
    Each case deletes its two rows afterwards, so the matcher sees exactly
    one bad and one good row per case."""
    from nexus.plans.matcher import plan_match

    _info_logging()
    for path in ("t1", "fts"):
        for name, where in (
            ("source", "required_bindings"),
            ("owner", "optional_bindings"),
            ("project", "default_bindings"),
            ("source", "step_arg_reference"),
        ):
            case = f"path={path} name={name!r} where={where}"
            plan = _plan_declaring(name, where)
            defaults = plan.pop("default_bindings", None)
            bad = _import_plan(
                library, query="reserved name plan about tumblers", plan=plan,
                name="bad", defaults=defaults,
            )
            good_plan = {"steps": [], "required_bindings": ["question"]}
            # A distinct query: an identical one would collide with the first.
            good = _import_plan(
                library, query="reserved name plan about tumblers, clean",
                plan=good_plan, name="good",
            )
            assert good != bad, case

            cache = _FakeCache([(bad, 0.01), (good, 0.05)]) if path == "t1" else None
            with capture_logs() as logs:
                matches = plan_match(
                    intent="reserved name plan about tumblers", library=library,
                    cache=cache, min_confidence=0.85,
                    available_bindings=frozenset({"intent"}),
                )

            ids = [m.plan_id for m in matches]
            assert good in ids, f"{case}: non-vacuity: the clean sibling must still match"
            assert bad not in ids, f"{case}: a plan declaring {name!r} was offered"

            excluded = [e for e in logs
                        if e.get("event") == "plan_match_reserved_binding_excluded"]
            assert len(excluded) == 1, (case, excluded)
            assert excluded[0]["detail"] == [{"plan_id": bad, "bindings": [name]}], case

            kept = library.get_plan(bad)
            assert kept is not None, f"{case}: the excluded plan was deleted"
            assert kept["disabled_at"] is None, f"{case}: the excluded plan was disabled"
            assert json.loads(kept["plan_json"]) == plan, (
                f"{case}: the excluded plan was rewritten"
            )
            # Next case starts from an empty library (import is not an upsert).
            library.delete_plan(bad)
            library.delete_plan(good)

    # ``available_bindings=None`` means "no typed-binding filtering"; it must
    # not also switch off the reserved-name exclusion.
    bad = _import_plan(
        library, query="owner scoped lookup", name="bad",
        plan=_plan_declaring("owner", "required_bindings"),
    )
    matches = plan_match(
        intent="owner scoped lookup", library=library,
        cache=_FakeCache([(bad, 0.01)]), min_confidence=0.85,
    )
    assert matches == [], "exclusion was switched off without an availability set"


# ── Test 4: the owner qualifier, from scope and owner ────────────────────────


def _names(table: dict[str, str]):
    def lookup(name: str) -> str:
        from nexus.plans.evidence import OwnerNameError

        try:
            return table[name]
        except KeyError:
            raise OwnerNameError(f"{name!r} names no registered owner") from None
    return lookup


def test_scope_and_owner_agree_and_resolve_with_the_longer() -> None:
    from nexus.plans.evidence import resolve_owner_qualifier

    res = resolve_owner_qualifier(
        owner="1.2", project=None, scope="1.2.4686", name_lookup=_names({}),
    )
    assert res.status == "resolved"
    assert res.tumbler == "1.2.4686"


def test_agreement_does_not_depend_on_which_side_is_longer() -> None:
    from nexus.plans.evidence import resolve_owner_qualifier

    res = resolve_owner_qualifier(
        owner="1.2.4686", project=None, scope="1.2", name_lookup=_names({}),
    )
    assert (res.status, res.tumbler) == ("resolved", "1.2.4686")


def test_scope_and_owner_in_different_subtrees_conflict_and_pick_nothing() -> None:
    from nexus.plans.evidence import resolve_owner_qualifier

    res = resolve_owner_qualifier(
        owner="1.2", project=None, scope="1.3", name_lookup=_names({}),
    )
    assert res.status == "conflict"
    assert res.tumbler is None, "a conflict must not pick a side"
    assert dict(res.contributions) == {"owner": "1.2", "scope": "1.3"}
    assert "1.2" in res.detail and "1.3" in res.detail


def test_a_prefix_is_a_segment_prefix_not_a_string_prefix() -> None:
    """1.2 is not a prefix of 1.20: comparing strings would call these
    compatible and silently resolve ART's RDR to another owner."""
    from nexus.plans.evidence import resolve_owner_qualifier

    res = resolve_owner_qualifier(
        owner="1.2", project=None, scope="1.20", name_lookup=_names({}),
    )
    assert res.status == "conflict"


def test_a_subtree_scope_alone_is_the_owner_qualifier() -> None:
    from nexus.plans.evidence import resolve_owner_qualifier

    res = resolve_owner_qualifier(
        owner=None, project=None, scope="1.2", name_lookup=_names({}),
    )
    assert (res.status, res.tumbler) == ("resolved", "1.2")
    assert dict(res.contributions) == {"scope": "1.2"}


@pytest.mark.parametrize("scope", ["rdr", "knowledge", "code__1-1__model-code__v1", ""])
def test_a_corpus_scope_is_never_an_owner_qualifier(scope: str) -> None:
    """``scope="rdr"`` alone is never a project selector (RDR section 1)."""
    from nexus.plans.evidence import resolve_owner_qualifier

    res = resolve_owner_qualifier(
        owner=None, project=None, scope=scope, name_lookup=_names({}),
    )
    assert res.status == "none"
    assert res.tumbler is None


def test_a_corpus_scope_does_not_disturb_an_owner_binding() -> None:
    from nexus.plans.evidence import resolve_owner_qualifier

    res = resolve_owner_qualifier(
        owner="1.2", project=None, scope="rdr", name_lookup=_names({}),
    )
    assert (res.status, res.tumbler) == ("resolved", "1.2")


def test_owner_and_project_that_disagree_conflict() -> None:
    from nexus.plans.evidence import resolve_owner_qualifier

    res = resolve_owner_qualifier(
        owner="1.1", project="ART", scope=None,
        name_lookup=_names({"ART": "1.2"}),
    )
    assert res.status == "conflict"
    assert res.tumbler is None


def test_an_unknown_project_is_unresolved_not_guessed() -> None:
    from nexus.plans.evidence import resolve_owner_qualifier

    res = resolve_owner_qualifier(
        owner=None, project="NOPE", scope=None, name_lookup=_names({}),
    )
    assert res.status == "unresolved"
    assert res.tumbler is None
    assert "NOPE" in res.detail


def test_an_owner_qualifier_shallower_than_an_owner_is_unresolved() -> None:
    from nexus.plans.evidence import resolve_owner_qualifier

    res = resolve_owner_qualifier(
        owner="1", project=None, scope=None, name_lookup=_names({}),
    )
    assert res.status == "unresolved"


# ── SourceRef ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("raw", "kind", "value"), [
    ("RDR-084", "rdr", "RDR-084"),
    ("rdr-84", "rdr", "RDR-084"),
    ("RDR 084", "rdr", "RDR-084"),
    ("rdr_232", "rdr", "RDR-232"),
    (84, "rdr", "RDR-084"),
    ("1.2.4686", "tumbler", "1.2.4686"),
    ("1.2.4686.3", "tumbler", "1.2.4686.3"),
])
def test_source_ref_parses_an_rdr_id_or_a_document_tumbler(raw, kind, value) -> None:
    from nexus.plans.evidence import SourceRef

    ref = SourceRef.parse(raw)
    assert (ref.kind, ref.value) == (kind, value)


def test_source_ref_rdr_exposes_its_number() -> None:
    from nexus.plans.evidence import SourceRef

    assert SourceRef.parse("RDR-084").number == 84
    assert SourceRef.parse("1.2.4686").number is None


@pytest.mark.parametrize("raw", ["", "  ", "the ART paper", "1.2", "RDR-","rdr-x1", None, "RDR-084-foo"])
def test_source_ref_refuses_what_is_not_a_source(raw) -> None:
    from nexus.plans.evidence import SourceRef, SourceRefError

    with pytest.raises(SourceRefError):
        SourceRef.parse(raw)


def test_a_malformed_source_is_disclosed_not_fatal() -> None:
    from nexus.plans.evidence import build_evidence_request

    req = build_evidence_request(
        {"source": "the ART paper"}, scope="", name_lookup=_names({}),
    )
    wire = req.to_wire()
    assert wire["source"] is None
    assert "the ART paper" in wire["source_error"]
    assert any("source" in line for line in req.disclosures())


# ── Test 5: project ART resolves through catalog owner identity to 1.2 ───────


@pytest.mark.t2_tenant
def test_project_art_resolves_to_owner_1_2_on_a_fixture_catalog() -> None:
    """Real engine catalog, per-test tenant: ``nexus`` registers first (1.1),
    ``ART`` second (1.2). A curator sharing the name ``nexus`` exists too, as
    in the live catalog, so the repo-preferred rule is exercised."""
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    from nexus.plans.evidence import build_evidence_request, catalog_name_lookup

    with HttpCatalogClient() as cat:
        nexus_owner = cat.register_owner("nexus", "repo", repo_hash="fixturehash1")
        art_owner = cat.register_owner("ART", "repo", repo_hash="fixturehash2")
        cat.register_owner("nexus", "curator")
        assert (str(nexus_owner), str(art_owner)) == ("1.1", "1.2")

        lookup = catalog_name_lookup(cat)
        art = build_evidence_request(
            {"source": "RDR-084", "project": "ART"}, scope="rdr",
            name_lookup=lookup,
        )
        assert art.owner.status == "resolved"
        assert art.owner.tumbler == "1.2"
        assert dict(art.owner.contributions) == {"project": "1.2"}

        nx = build_evidence_request(
            {"source": "RDR-084", "project": "nexus"}, scope="",
            name_lookup=lookup,
        )
        assert nx.owner.tumbler == "1.1", "repo owner must beat the same-named curator"

        missing = build_evidence_request(
            {"source": "RDR-084", "project": "NoSuchProject"}, scope="",
            name_lookup=lookup,
        )
        assert missing.owner.status == "unresolved"
        assert missing.owner.tumbler is None

        no_qualifier = build_evidence_request(
            {"source": "RDR-084"}, scope="rdr", name_lookup=lookup,
        )
        assert no_qualifier.owner.status == "none", (
            "no qualifier and scope=rdr must stay ambiguous, not default to nexus"
        )


# ── Test 6: text-mode disclosure placement (tenant-free) ─────────────────────
#
# nx_answer's docstring promises a degraded text answer STARTS with
# ``[budget exhausted ...]`` or ``[non-answer: <shape>]``. The evidence
# request's disclosure lines are folded into the same composition, so they
# must follow a leading marker and precede only ordinary prose.

_DISCLOSURE = "[evidence request: source "


async def _text_answer(plan_result, *, bindings: dict) -> str:
    from nexus.mcp.core import nx_answer

    match = Match(
        plan_id=0, name="ad-hoc", description="q", confidence=None,
        dimensions={}, tags="ad-hoc",
        plan_json=json.dumps({"steps": [
            {"tool": "search", "args": {"query": "$intent"}},
        ]}),
        required_bindings=["intent"], optional_bindings=[],
        default_bindings={"intent": "q"}, parent_dims=None,
    )
    db_stub = MagicMock()
    db_stub.telemetry._supports_nx_answer_run_complete.return_value = False
    with patch("nexus.plans.matcher.plan_match", return_value=[]), \
         patch("nexus.mcp.core._nx_answer_plan_miss", AsyncMock(return_value=match)), \
         patch("nexus.plans.runner.plan_run", AsyncMock(return_value=plan_result)), \
         patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
         patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
         patch("nexus.mcp.core.scratch", return_value="ok"), \
         patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
        t2_ctx.return_value.__enter__.return_value = db_stub
        out = await nx_answer(question="q", bindings=bindings)
    assert isinstance(out, str)
    return out


def _plan_result(steps: list[dict]) -> MagicMock:
    result = MagicMock()
    result.steps = steps
    result.dropped_reduce_steps = []
    result.step_records = []
    return result


@pytest.mark.asyncio
async def test_a_disclosure_precedes_the_prose_of_a_normal_answer() -> None:
    text = await _text_answer(
        _plan_result([{"text": "the answer is 42"}]),
        bindings={"source": "not a source"},
    )
    assert _DISCLOSURE in text, "the evidence-request disclosure was dropped"
    assert text.startswith(_DISCLOSURE), text
    assert text.endswith("the answer is 42"), text


@pytest.mark.asyncio
async def test_a_disclosure_never_goes_ahead_of_a_non_answer_marker() -> None:
    from nexus.plans.answer_shape import NON_ANSWER_NOTICE_PREFIX

    retrieval_only = _plan_result([
        {"ids": ["c1"], "collections": ["rdr__1-1"], "distances": [0.1],
         "chunk_text_hash": ["c1"]},
        {"contents": ["body"], "missing": [], "section_types": ["class"]},
    ])
    text = await _text_answer(retrieval_only, bindings={"source": "not a source"})
    assert text.startswith(NON_ANSWER_NOTICE_PREFIX), (
        "the evidence notice displaced the leading non-answer marker: " + text
    )
    assert _DISCLOSURE in text, "the notice was dropped from a non-answer"


@pytest.mark.asyncio
async def test_no_disclosure_when_no_reserved_key_is_given() -> None:
    """Non-vacuity for the two placement tests: the line comes from the
    reserved key, not from the composition."""
    text = await _text_answer(
        _plan_result([{"text": "the answer is 42"}]), bindings={},
    )
    assert text == "the answer is 42"
