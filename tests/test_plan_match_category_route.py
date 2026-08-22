# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dimension-routed category plans (design memo 2026-08-21).

nx_answer had selected a builtin plan exactly ONCE in four months. The
cause is structural rather than tunable: MiniLM cosine measures topical
overlap, a category-level template is topic-free by construction, and a
real question is topic-bearing. Measured with zero grown-plan
competition, 3 of 5 verb-shaped probes put the intended builtin below
the 0.40 floor and ``debug-default`` died at RANK 1 — so no floor
setting admits the right plan without admitting everything above it, and
description rewrites were tried and made 5 of 8 pairs worse.

The route lifts the floor, and only the floor, for builtin templates
whose verb the caller derived, then picks by RELATIVE cosine inside that
small same-verb pool.

The three properties that keep this safe are pinned first, because each
one is a way the change could quietly do damage:

* it never fires when the cosine path scored something, so it cannot
  alter a call that already matched;
* the derived verb never becomes a ``dimensions`` filter, which would
  drop every grown plan and with it the entire live hit rate; and
* it still respects every correctness gate — only the floor is lifted.
"""
from __future__ import annotations

import pytest

from nexus.plans.matcher import _is_category_plan, plan_match
from nexus.plans.verb_infer import infer_verb


class _FakeCache:
    """Cosine cache returning caller-supplied (plan_id, distance) pairs."""

    is_available = True

    def __init__(self, hits):
        self._hits = hits
        self.removed: list[int] = []

    def query(self, intent, n):  # noqa: ARG002 — fixture returns a fixed pool
        return self._hits[:n]

    def remove(self, plan_id):
        self.removed.append(plan_id)


class _FakeLibrary:
    def __init__(self, rows):
        self._rows = {r["id"]: r for r in rows}
        self.metrics: list[tuple[int, float | None]] = []

    def get_plan(self, plan_id):
        return self._rows.get(plan_id)

    def increment_match_metrics(self, plan_id, confidence=None):
        self.metrics.append((plan_id, confidence))

    def search_plans(self, intent, limit=10, project=""):  # noqa: ARG002
        return []


def _row(plan_id, *, name, verb, tags="builtin-template", query="", **extra):
    row = {
        "id": plan_id,
        "name": name,
        "query": query or f"Description for {name}.",
        "plan_json": '{"steps": []}',
        "tags": tags,
        "verb": verb,
        "scope": "global",
        "dimensions": f'{{"scope":"global","verb":"{verb}"}}',
        "scope_tags": "",
        "project": "",
        "success_count": 1,
        "failure_count": 0,
    }
    row.update(extra)
    return row


# ── the safety properties ──────────────────────────────────────────────────


def test_route_does_not_fire_when_cosine_already_scored():
    """The route can only turn a miss into a hit. A call the cosine path
    answers must be byte-identical with and without a category verb."""
    rows = [
        _row(1, name="default", verb="research"),
        _row(2, name="grown-thing", verb="research", tags="ad-hoc,grown"),
    ]
    lib = _FakeLibrary(rows)
    # plan 2 at cosine 0.9 clears both the 0.40 floor and the 0.55 grown floor.
    cache = _FakeCache([(2, 0.1), (1, 0.8)])

    without = plan_match("anything", library=lib, cache=cache)
    with_verb = plan_match(
        "anything", library=_FakeLibrary(rows), cache=_FakeCache([(2, 0.1), (1, 0.8)]),
        category_verb="research",
    )

    assert [m.plan_id for m in without] == [2]
    assert [m.plan_id for m in with_verb] == [2]
    assert with_verb[0].confidence == pytest.approx(0.9)


def test_derived_verb_does_not_filter_grown_plans():
    """The single easiest way to break this. `dimensions` is a hard filter
    over ALL candidates; grown verbs are only ever research/analyze, so
    routing a derived `debug` through it would drop the entire 68% live
    hit rate. The category verb must not have that reach."""
    rows = [_row(2, name="grown-thing", verb="research", tags="ad-hoc,grown")]
    lib = _FakeLibrary(rows)
    cache = _FakeCache([(2, 0.1)])

    matches = plan_match("anything", library=lib, cache=cache, category_verb="debug")

    assert [m.plan_id for m in matches] == [2], (
        "a derived verb must not filter grown plans out of the cosine path"
    )


def test_route_still_honours_the_always_failing_gate():
    """Only the floor is lifted. Every correctness gate still applies."""
    rows = [_row(1, name="default", verb="debug", success_count=0, failure_count=5)]
    lib = _FakeLibrary(rows)
    cache = _FakeCache([(1, 0.8)])

    assert plan_match("q", library=lib, cache=cache, category_verb="debug") == []


def test_route_still_honours_unmet_typed_bindings():
    """find-by-author and type-scoped-search are the only two builtins of
    17 that die here, and they should keep dying: an author search with no
    author is not a plan the caller can run."""
    rows = [_row(
        1, name="find-by-author", verb="research",
        plan_json='{"steps": [], "required_bindings": ["author"]}',
    )]
    lib = _FakeLibrary(rows)
    cache = _FakeCache([(1, 0.8)])

    matches = plan_match(
        "q", library=lib, cache=cache, category_verb="research",
        available_bindings=frozenset({"intent"}),
    )
    assert matches == []


def test_no_category_verb_means_todays_behaviour():
    rows = [_row(1, name="default", verb="research")]
    lib = _FakeLibrary(rows)
    cache = _FakeCache([(1, 0.8)])  # cosine 0.2, below the floor

    assert plan_match("q", library=lib, cache=cache) == []


# ── the route itself ───────────────────────────────────────────────────────


def test_below_floor_builtin_is_routed_by_verb():
    rows = [_row(1, name="default", verb="research")]
    lib = _FakeLibrary(rows)
    cache = _FakeCache([(1, 0.8)])  # cosine 0.2 — far below the 0.40 floor

    matches = plan_match("q", library=lib, cache=cache, category_verb="research")

    assert [m.plan_id for m in matches] == [1]


def test_routed_match_carries_the_none_sentinel_at_index_zero():
    """confidence=None is the existing 'admitted, but not by a cosine
    score' sentinel. nx_answer's own re-check passes None unconditionally
    and choose_within_band returns matches[0] unchanged for it, so both
    downstream gates are satisfied by contract rather than by a second
    special case."""
    rows = [_row(1, name="default", verb="research")]
    lib = _FakeLibrary(rows)
    cache = _FakeCache([(1, 0.8)])

    matches = plan_match("q", library=lib, cache=cache, category_verb="research")

    assert matches[0].confidence is None


def test_route_records_raw_cosine_not_the_sentinel():
    """match_conf_sum is a permanent aggregate and must keep measuring
    embedding quality, not gating decisions."""
    rows = [_row(1, name="default", verb="research")]
    lib = _FakeLibrary(rows)
    cache = _FakeCache([(1, 0.75)])

    plan_match("q", library=lib, cache=cache, category_verb="research")

    assert lib.metrics == [(1, pytest.approx(0.25))]


def test_route_picks_the_best_relative_cosine_within_the_verb():
    """The crux of the design: absolute cosine is meaningless here (the
    whole pool is below the floor by construction) but the ORDER inside a
    same-verb pool still tracks which template fits, which is what lets a
    discovery-shaped question reach document-discovery instead of always
    landing on research-default."""
    rows = [
        _row(1, name="default", verb="research"),
        _row(2, name="document-discovery", verb="research"),
    ]
    lib = _FakeLibrary(rows)
    cache = _FakeCache([(1, 0.85), (2, 0.70)])  # 0.15 vs 0.30

    matches = plan_match("q", library=lib, cache=cache, category_verb="research")

    assert [m.plan_id for m in matches] == [2]


def test_route_ignores_plans_of_another_verb():
    rows = [_row(1, name="default", verb="debug")]
    lib = _FakeLibrary(rows)
    cache = _FakeCache([(1, 0.8)])

    assert plan_match("q", library=lib, cache=cache, category_verb="document") == []


def test_route_ignores_non_builtin_plans():
    """A below-floor grown plan must never be routed. The route exists for
    category-level templates; grown plans are instance-level and cosine is
    the right instrument for them."""
    rows = [_row(1, name="grown", verb="research", tags="ad-hoc,grown")]
    lib = _FakeLibrary(rows)
    cache = _FakeCache([(1, 0.8)])

    assert plan_match("q", library=lib, cache=cache, category_verb="research") == []


def test_route_uses_verb_synonym_classes():
    """{query, research, lookup} is one class, so a derived `research`
    reaches abstract-themes (query) and hybrid-factual-lookup (lookup).
    That is deliberate: the classifier picks the CLASS and relative cosine
    picks the member."""
    rows = [_row(1, name="hybrid-factual-lookup", verb="lookup")]
    lib = _FakeLibrary(rows)
    cache = _FakeCache([(1, 0.8)])

    matches = plan_match("q", library=lib, cache=cache, category_verb="research")

    assert [m.plan_id for m in matches] == [1]


# ── the predicate ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ("builtin-template", True),
        ("builtin-template,nexus-h33x8,fast-path", True),
        ("nexus-h33x8,builtin-template", True),
        ("", False),
        (None, False),
        ("ad-hoc,grown", False),
        # Comma-token, not substring — the naive `in` test _classify_origin
        # uses would call this a template.
        ("not-a-builtin-template-really", False),
    ],
)
def test_category_predicate_matches_comma_tokens(tags, expected):
    assert _is_category_plan(tags) is expected


# ── verb derivation ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        # The five verb-shaped probes from the measurement that motivated
        # this design. Each one previously reached no builtin at all.
        ("Review the changes on this branch", "analyze"),
        ("Research how the plan matcher decides", "research"),
        ("What documentation exists for the catalog?", "document"),
        ("Debug why the T1 scratch store returns nothing", "debug"),
        ("Analyze the tradeoffs between pgvector and Chroma", "analyze"),
        # Failure investigation outranks retrieval: these read as "what/why"
        # questions but are debug questions.
        ("Why does the indexer fail on empty files?", "debug"),
        ("What exception does the pooler raise?", "debug"),
        # Documentation outranks retrieval on the same logic.
        ("What docs cover the aspect queue?", "document"),
        # Retrieval, the broad default class.
        ("Which papers discuss membership churn?", "research"),
        ("Find documents about tumblers", "research"),
        ("How does the catalog resolve a tumbler?", "research"),
        # Plan lifecycle, anchored so an ordinary question containing
        # "plan" does not land here — that over-capture is why the meta
        # plans outrank real ones on the cosine path today.
        ("Author a new plan for corpus coverage", "plan-author"),
        ("What is the release plan for the engine?", "research"),
        # "debug" is a noun/adjective as often as a verb in this
        # codebase's vocabulary, so the bare word must lose to the more
        # specific documentation noun. It previously won, and since
        # debug-default is a real routable template that produced a wrong
        # HIT, not a harmless miss.
        ("What documentation exists for the debug logging system?", "document"),
        ("Where is the debug output written?", "debug"),
        # An unambiguous failure word still outranks documentation.
        ("What documentation explains this traceback?", "debug"),
        # A yes/no opener alone is NOT a retrieval question. Deriving a
        # verb for these routes a cosine miss to a confident, irrelevant
        # corpus search instead of falling through to the planner.
        ("Are we done here?", None),
        ("Should we merge this PR?", None),
        ("Can you take a look at this?", None),
        # ...but a yes/no question ABOUT STORED MATERIAL is retrieval, and
        # corpus-coverage-check is written for exactly that shape.
        ("Does the corpus have anything about tumblers at all?", "research"),
        ("Is there a paper on membership churn?", "research"),
        # Mid-sentence wh- forms: the same question with a preamble.
        ("In the catalog, what is a tumbler?", "research"),
        # No confident reading -> no routing -> today's behaviour.
        ("", None),
        ("   ", None),
        ("tumblers", None),
        ("catalog manifest", None),
    ],
)
def test_infer_verb(question, expected):
    assert infer_verb(question) == expected
