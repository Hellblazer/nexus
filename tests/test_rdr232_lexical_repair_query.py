# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-0ihqj: the coverage repair's lexical leg.

A fact stated once inside a long multi-topic chunk can rank past the vector floor for its own question, so the
repair's facet-text query finds nothing new although the chunk is indexed. A facet whose text carries key terms
(quoted strings, identifiers) gets a lexical twin of its ``search`` repair query: ``lexical=True``, the key terms
alone as the text, the same target and filters. The twins run after every vector query and only in the query slots
the vector queries leave (the repair spends its existing budget), and their hits join after every vector hit.
These tests pin the extraction, the query construction, the runner path, the time share, the failure rules, the
scope. Why a twin carries the key terms and not the facet's whole text is the engine's: its lexical route matches
``chunk_tsv @@ plainto_tsquery('english', query)``, which ANDs every word (service/src/main/resources/db/changelog/
vectors-049-route-b-exact-keys.xml), and the RDR-217 journey (tests/test_rdr217_hybrid_search_journey.py) pins that
route against the real engine. A test here would mint a tenant to re-pin it.

Every example is synthetic and none comes from an evaluation set, held out or not: the extractor is not tuned to
one. (Review r1 of 4023af8d7 found three H29 answer tokens among the first version's examples, and a
number-with-the-word-before rule exercised only by one; both are gone, and a bare number is no longer a term.)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from nexus.plans.coverage_ledger import CoverageBoundary
from nexus.plans.coverage_repair import (
    MAX_LEXICAL_REPAIR_QUERIES,
    MAX_REPAIR_ITEMS,
    MAX_REPAIR_QUERIES,
    RepairTarget,
    build_repair_queries,
    key_terms,
)
from nexus.plans.evidence import ScopeConstraint
from nexus.plans.request_coverage import Facet, RequestCoverage
from nexus.plans.runner import PlanRunRetrievalRefusedError
from tests.test_rdr232_coverage_ledger import CODE_COLL, RDR_COLL, _cov, _structured, _tag_lines
from tests.test_rdr232_coverage_repair import FACET_TEXT, RepairDispatcher, _run, by_topic

@pytest.fixture(autouse=True)
def _no_catalog_lookup():
    """As in the repair tests: the runner's non-evidentiary hygiene asks the catalog, and these runner tests are
    tenant-free, so the lookup answers 'nothing excluded'."""
    with patch("nexus.plans.runner._resolve_non_evidentiary_chashes", return_value=set()):
        yield


# ── key_terms ────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("text", "expected"), [
    ("what does the retry_budget allow", ("retry_budget",)),
    ("which step runs tools/rotate-logs.py", ("tools/rotate-logs.py",)),
    ("what RDR-217 decided about nexus-0ihqj", ("RDR-217", "nexus-0ihqj")),
    ("how does HttpVectorClient page", ("HttpVectorClient",)),
    ("what does --max-hops change", ("--max-hops",)),
    ('what does the "warm cache" banner print', ("warm cache",)),
    ("what the \u201cnight shift\u201d rota covers", ("night shift",)),
    ("what the `nx t3 gc` verb expires", ("nx t3 gc",)),
    ("which host serves pool:primary", ("pool:primary",)),
    ("who owns retry_budget.", ("retry_budget",)),
    ("what lives under docs/rdr", ("docs/rdr",)),
    ("what does `nx` print", ("nx",)),
])
def test_key_terms_finds_exact_match_terms(text: str, expected: tuple[str, ...]) -> None:
    assert key_terms(text) == expected


@pytest.mark.parametrize("text", [
    FACET_TEXT.format(n=3),
    "what phase 2 decided",
    "what changed in 2024.",
    "the top 10 results, around 50 of them, at 10:30, version 1.4.2, ratio 0.5",
    "the multi-topic chunk and/or its neighbours, e.g. the long ones",
    "client/server, read/write/execute, input/output, yes/no/maybe and pros/cons",
    'the "default" value and the "fast" path',
    "how the coverage repair reads the ledger",
    "",
])
def test_prose_and_bare_numbers_have_no_key_terms(text: str) -> None:
    assert key_terms(text) == ()


def test_key_terms_are_ordered_deduplicated_and_capped_at_two() -> None:
    assert key_terms("does retry_budget, RDR-217, retry_budget again or max_hops apply") == ("retry_budget", "RDR-217")


def test_a_quoted_identifier_is_one_term_not_two() -> None:
    assert key_terms("what `retry_budget` allows") == ("retry_budget",)


def test_a_quote_too_long_to_be_a_term_does_not_pair_with_the_next_quote() -> None:
    long = "x" * 70
    assert key_terms(f'he said "{long}" and then also "warm cache" later') == ("warm cache",)


# ── build_repair_queries ─────────────────────────────────────────────────────────────────────────────────────


def _f(i: str, text: str) -> Facet:
    return Facet(i, text)


def test_the_twins_follow_every_vector_query_in_the_same_order() -> None:
    qs = build_repair_queries(
        [_f("f1", "what the retry_budget allows"), _f("f2", "how the ledger is read"), _f("f3", "who set max_hops")],
        primary_collections=[], plan_targets=[RepairTarget("docs", filters=(("where", "section_type!=references"),))],
        max_queries=6, limit=5, max_lexical=3,
    )
    assert [(q.facet_id, q.args["query"], q.args.get("lexical", False)) for q in qs] == [
        ("f1", "what the retry_budget allows", False),
        ("f2", "how the ledger is read", False),
        ("f3", "who set max_hops", False),
        ("f1", "retry_budget", True),
        ("f3", "max_hops", True),
    ]
    twin = qs[3]
    assert twin.tool == "search" and twin.origin == "plan"
    assert twin.args["corpus"] == "docs" and twin.args["limit"] == 5
    assert twin.args["where"] == "section_type!=references", "the twin keeps its target's filters"


def test_the_twins_use_only_the_slots_the_vector_queries_leave() -> None:
    facets = [_f(f"f{i}", f"what retry_budget_{i} allows") for i in range(1, 5)]
    one = [RepairTarget("docs")]
    both = [RepairTarget("docs"), RepairTarget("rdr")]
    full = build_repair_queries(facets, primary_collections=[], plan_targets=both, max_queries=6, limit=5)
    assert build_repair_queries(
        facets, primary_collections=[], plan_targets=both, max_queries=6, limit=5, max_lexical=3) == full, \
        "six vector queries leave no slot"
    qs = build_repair_queries(facets, primary_collections=[], plan_targets=one, max_queries=6, limit=5, max_lexical=3)
    vector = [q for q in qs if not q.args.get("lexical")]
    assert vector == build_repair_queries(facets, primary_collections=[], plan_targets=one, max_queries=6, limit=5)
    assert len(vector) == 4 and len(qs) == 6 <= MAX_REPAIR_QUERIES, "two slots left, two twins, the budget holds"
    qs = build_repair_queries(facets, primary_collections=[], plan_targets=one, max_queries=10, limit=5, max_lexical=3)
    assert sum(1 for q in qs if q.args.get("lexical")) == 3, "and never more twins than their own cap"


def test_no_twin_by_default_or_for_a_query_target() -> None:
    facet = [_f("f1", "what the retry_budget allows")]
    assert not any(q.args.get("lexical") for q in build_repair_queries(
        facet, primary_collections=["c"], plan_targets=[], max_queries=6, limit=5))
    qs = build_repair_queries(
        facet, primary_collections=[], plan_targets=[RepairTarget("rdr", "1.2")], max_queries=6, limit=5, max_lexical=3)
    assert [q.tool for q in qs] == ["query"], "a subtree target has no lexical route, so it gets no twin"


def test_the_same_terms_against_the_same_target_are_asked_once() -> None:
    qs = build_repair_queries(
        [_f("f1", "what retry_budget allows"), _f("f2", "who set retry_budget")], primary_collections=[],
        plan_targets=[RepairTarget("docs")], max_queries=6, limit=5, max_lexical=3,
    )
    assert [q.args["query"] for q in qs if q.args.get("lexical")] == ["retry_budget"]


def test_the_boundary_carries_the_lexical_cap_inside_the_unchanged_budget() -> None:
    boundary = CoverageBoundary(request=None)
    assert boundary.max_lexical_repair_queries == MAX_LEXICAL_REPAIR_QUERIES == 3
    assert (MAX_REPAIR_ITEMS, MAX_REPAIR_QUERIES) == (12, 6)


# ── the runner path ──────────────────────────────────────────────────────────────────────────────────────────

LEXICAL_FACET = "what the document says about the retry_budget"
OTHER_LEXICAL_FACET = "what the document says about the max_hops"
LEXICAL_HIT = "a long chunk about many things; among them, retry_budget is 42 attempts"
OTHER_LEXICAL_HIT = "another long chunk; max_hops is 7 hops"


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _request(*texts: str) -> RequestCoverage:
    facets = [Facet("f1", FACET_TEXT.format(n=1)), Facet("f2", FACET_TEXT.format(n=2))]
    facets += [Facet(f"f{3 + i}", t) for i, t in enumerate(texts)]
    return RequestCoverage(answer_kind="synthesis", facets=tuple(facets))


class LexicalDispatcher(RepairDispatcher):
    """The repair stub, plus lexical searches answered from ``lexical`` (query text to an answer: a list of hit texts,
    an exception, an error page, or a coroutine factory), and a vector repair search that returns ``vector_hits``."""

    def __init__(self, lexical: dict[str, Any], *, vector_hits: list[str] | None = None,
                 lexical_collection: str = RDR_COLL, topic: dict[str, str] | None = None) -> None:
        super().__init__(by_topic(topic={"f3": "42 attempts", "f4": "7 hops", **(topic or {})}),
                         repair_hits=vector_hits or [])
        self.lexical = lexical
        self.lexical_collection = lexical_collection
        for t in (LEXICAL_HIT, OTHER_LEXICAL_HIT):
            self.known[_h(t)] = t

    async def __call__(self, tool: str, args: dict) -> dict:
        if tool == "search" and args.get("lexical"):
            self.calls.append((tool, dict(args)))
            answer = self.lexical.get(args["query"], [])
            if callable(answer):
                answer = await answer()
            if isinstance(answer, BaseException):
                raise answer
            if isinstance(answer, dict):
                return answer
            return _structured(answer, self.lexical_collection)
        return await super().__call__(tool, args)

    def lexical_searches(self) -> list[dict[str, Any]]:
        return [a for t, a in self.calls if t == "search" and a.get("lexical")]


@pytest.mark.asyncio
async def test_a_hit_only_the_lexical_twin_finds_covers_the_facet() -> None:
    d = LexicalDispatcher({"retry_budget": [LEXICAL_HIT]})
    cov = _cov(await _run(d, CoverageBoundary(request=_request(LEXICAL_FACET))))
    assert [a["query"] for a in d.lexical_searches()] == ["retry_budget"]
    assert cov.repair.outcome == "completed"
    f3 = cov.ledger.finding("f3")
    new = next(a for a in cov.ledger.anchors if a.content == LEXICAL_HIT)
    assert f3 is not None and f3.state == "covered" and f3.evidence_ids == (new.item_id,)
    last = d.args_of("operator_check")[-1]
    assert _tag_lines(last["check_instruction"]) == ["f3"]
    assert [i["text"] for i in json.loads(last["items"])] == [LEXICAL_HIT]


@pytest.mark.asyncio
async def test_without_the_twin_the_same_run_finds_nothing_new() -> None:
    """The control: the lexical cap at 0 is the code before nexus-0ihqj, and the facet stays unresolved."""
    d = LexicalDispatcher({"retry_budget": [LEXICAL_HIT]})
    cov = _cov(await _run(d, CoverageBoundary(request=_request(LEXICAL_FACET), max_lexical_repair_queries=0)))
    assert d.lexical_searches() == []
    assert cov.repair.outcome == "no_new_evidence" and "f3" in cov.unresolved_facet_ids


@pytest.mark.asyncio
async def test_vector_hits_take_the_item_slots_before_a_twin_hit() -> None:
    """The twin runs and its hit is found, but the twin comes last (the query order, pinned by the builder tests
    above), so the item cap is already full of vector hits and the twin's hit is cut first."""
    many = [f"repair hit number {i:02d} with some text" for i in range(30)]
    d = LexicalDispatcher({"retry_budget": [LEXICAL_HIT]}, vector_hits=many)
    cov = _cov(await _run(d, CoverageBoundary(request=_request(LEXICAL_FACET))))
    assert len(d.lexical_searches()) == 1
    merged = [a.content for a in cov.ledger.anchors if a.content in many or a.content == LEXICAL_HIT]
    assert LEXICAL_HIT not in merged and len(merged) == MAX_REPAIR_ITEMS, "the vector hits fill the cap first"


@pytest.mark.asyncio
async def test_a_failed_twin_alone_is_not_a_failed_retrieval() -> None:
    """The vector repair query ran and found nothing new; only the twin failed. The outcome is the vector query's."""
    d = LexicalDispatcher({"retry_budget": RuntimeError("lexical backend down")})
    cov = _cov(await _run(d, CoverageBoundary(request=_request(LEXICAL_FACET))))
    assert len(d.lexical_searches()) == 1
    assert cov.repair.outcome == "no_new_evidence", cov.repair.outcome


@pytest.mark.asyncio
async def test_a_failed_twin_does_not_stop_the_twin_after_it() -> None:
    d = LexicalDispatcher({"retry_budget": RuntimeError("lexical backend down"), "max_hops": [OTHER_LEXICAL_HIT]})
    cov = _cov(await _run(d, CoverageBoundary(request=_request(LEXICAL_FACET, OTHER_LEXICAL_FACET))))
    assert [a["query"] for a in d.lexical_searches()] == ["retry_budget", "max_hops"]
    f4 = cov.ledger.finding("f4")
    assert f4 is not None and f4.state == "covered", "the second twin ran and its hit covered its facet"


@pytest.mark.asyncio
async def test_a_routeless_backend_error_page_is_skipped_and_logged() -> None:
    """On the real path a backend with no lexical route does not raise here: the search tool returns an ``Error:``
    string and the runner hands back an empty page with an ``error`` key."""
    page = {"ids": [], "error": "lexical leg unavailable on this backend"}
    d = LexicalDispatcher({"retry_budget": page, "max_hops": [OTHER_LEXICAL_HIT]})
    with capture_logs() as logs:
        cov = _cov(await _run(d, CoverageBoundary(request=_request(LEXICAL_FACET, OTHER_LEXICAL_FACET))))
    assert any(e["event"] == "nx_answer_coverage_repair_lexical_query_failed" for e in logs)
    assert len(d.lexical_searches()) == 2
    f4 = cov.ledger.finding("f4")
    assert f4 is not None and f4.state == "covered"


@pytest.mark.asyncio
async def test_a_hung_twin_is_cut_at_its_share_and_the_next_twin_still_runs() -> None:
    async def hang() -> list[str]:
        await asyncio.sleep(60)
        return [LEXICAL_HIT]

    d = LexicalDispatcher({"retry_budget": hang, "max_hops": [OTHER_LEXICAL_HIT]})
    started = time.monotonic()
    cov = _cov(await _run(
        d, CoverageBoundary(request=_request(LEXICAL_FACET, OTHER_LEXICAL_FACET)), deadline=started + 70.0))
    assert time.monotonic() - started < 30, "the hung twin was cut, not waited out"
    assert [a["query"] for a in d.lexical_searches()] == ["retry_budget", "max_hops"]
    f4 = cov.ledger.finding("f4")
    assert f4 is not None and f4.state == "covered", "the second twin had its own share of the time"


@pytest.mark.asyncio
async def test_a_refused_twin_ends_the_repair_like_any_refused_query() -> None:
    refusal = PlanRunRetrievalRefusedError.__new__(PlanRunRetrievalRefusedError)
    d = LexicalDispatcher({"retry_budget": refusal})
    cov = _cov(await _run(d, CoverageBoundary(request=_request(LEXICAL_FACET))))
    assert cov.repair.outcome == "retrieval_failed", cov.repair.outcome


@pytest.mark.asyncio
async def test_a_twin_hit_outside_the_callers_scope_is_dropped() -> None:
    d = LexicalDispatcher({"retry_budget": [LEXICAL_HIT]}, lexical_collection=CODE_COLL)
    cov = _cov(await _run(d, CoverageBoundary(request=_request(LEXICAL_FACET), scope=ScopeConstraint.parse("rdr"))))
    assert len(d.lexical_searches()) == 1
    assert cov.repair.outcome == "no_new_evidence" and "f3" in cov.unresolved_facet_ids


class _Confinement:
    """A document confinement with the engine filter: the repair stamps its allow list on every query."""

    sends_filter = True

    def __init__(self, allowed: set[str]) -> None:
        self.allowed = allowed

    def allow_list(self) -> list[str]:
        return sorted(self.allowed)

    def contains(self, key: str) -> bool:
        return key.lower() in self.allowed


@pytest.mark.asyncio
async def test_a_confined_runs_twin_carries_the_document_filter_and_its_outside_hit_is_dropped() -> None:
    allowed = {_h(OTHER_LEXICAL_HIT)}
    d = LexicalDispatcher({"retry_budget": [LEXICAL_HIT]})
    cov = _cov(await _run(d, CoverageBoundary(request=_request(LEXICAL_FACET), confinement=_Confinement(allowed))))
    twins = d.lexical_searches()
    assert len(twins) == 1 and twins[0]["chash_allow"] == sorted(allowed)
    assert all(a.content != LEXICAL_HIT for a in cov.ledger.anchors), "a hit outside the document never enters"
