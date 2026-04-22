# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for ``plan_match`` — RDR-078 P1 (nexus-05i.2).

Covers:
  * SC-1 — high-confidence cosine return above threshold.
  * SC-11 — FTS5 fallback when T1 cache absent or empty
    (``Match.confidence is None``).
  * SC-12 — match_count + match_conf_sum increment per returned plan.
  * Dimensional filter narrows pool to plans whose dimensions ⊇ filter.
  * ``min_confidence`` rejects below-threshold cosine hits.
  * ``project`` argument scopes both T1 and FTS5 paths.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def library(tmp_path: Path):
    """Fresh PlanLibrary with the RDR-078 schema applied."""
    from nexus.db.migrations import _add_plan_dimensional_identity
    from nexus.db.t2.plan_library import PlanLibrary

    lib = PlanLibrary(tmp_path / "plans.db")
    _add_plan_dimensional_identity(lib.conn)
    lib.conn.commit()
    return lib


def _seed(library, *, query: str, dimensions: dict | None = None,
          tags: str = "", project: str = "nexus",
          scope_tags: str | None = None) -> int:
    """Insert one plan with the canonical dimensions JSON encoding."""
    from nexus.plans.schema import canonical_dimensions_json

    dims_json = canonical_dimensions_json(dimensions) if dimensions else None
    return library.save_plan(
        query=query,
        plan_json=json.dumps({"steps": []}),
        tags=tags,
        project=project,
        dimensions=dims_json,
        verb=(dimensions or {}).get("verb"),
        scope=(dimensions or {}).get("scope"),
        name=(dimensions or {}).get("strategy", "default"),
        scope_tags=scope_tags,
    )


class _FakeCache:
    """Scriptable plan-cache stand-in for the T1 cosine path."""

    def __init__(self, hits: list[tuple[int, float]] | None = None,
                 available: bool = True) -> None:
        self._hits = list(hits or [])
        self._available = available
        self.queries: list[tuple[str, int]] = []

    @property
    def is_available(self) -> bool:
        return self._available

    def query(self, intent: str, n: int) -> list[tuple[int, float]]:
        self.queries.append((intent, n))
        return self._hits[:n]


# ── T1 cosine path (SC-1) ───────────────────────────────────────────────────


def test_plan_match_returns_high_confidence_above_threshold(library) -> None:
    from nexus.plans.matcher import plan_match

    plan_id = _seed(library, query="how does projection quality work",
                    dimensions={"verb": "research", "scope": "global"})
    cache = _FakeCache(hits=[(plan_id, 0.05)])  # cosine distance 0.05 → conf 0.95

    matches = plan_match(
        intent="what's the mechanism for projection quality hub suppression",
        library=library, cache=cache,
        min_confidence=0.85, n=5,
    )
    assert len(matches) == 1
    assert matches[0].plan_id == plan_id
    assert matches[0].confidence is not None
    assert matches[0].confidence >= 0.85


def test_plan_match_filters_below_min_confidence(library) -> None:
    from nexus.plans.matcher import plan_match

    plan_id = _seed(library, query="x",
                    dimensions={"verb": "research", "scope": "global"})
    cache = _FakeCache(hits=[(plan_id, 0.4)])  # cosine 0.6 < 0.85

    matches = plan_match(
        intent="y", library=library, cache=cache, min_confidence=0.85,
    )
    assert matches == []


def test_plan_match_returns_top_n(library) -> None:
    from nexus.plans.matcher import plan_match

    a = _seed(library, query="a",
              dimensions={"verb": "research", "scope": "global"})
    b = _seed(library, query="b",
              dimensions={"verb": "research", "scope": "project"})
    cache = _FakeCache(hits=[(a, 0.05), (b, 0.10)])

    matches = plan_match(intent="x", library=library, cache=cache,
                         min_confidence=0.5, n=2)
    assert [m.plan_id for m in matches] == [a, b]


# ── FTS5 fallback (SC-11) ───────────────────────────────────────────────────


def test_plan_match_t1_unavailable_falls_back_to_fts5(library) -> None:
    from nexus.plans.matcher import plan_match

    _seed(library, query="research projection quality mechanism",
          dimensions={"verb": "research", "scope": "global"})

    # No cache provided → FTS5 path. Match.confidence is None.
    matches = plan_match(
        intent="projection quality mechanism",
        library=library, cache=None, min_confidence=0.85,
    )
    assert len(matches) >= 1
    assert all(m.confidence is None for m in matches)


def test_plan_match_t1_empty_cache_falls_back_to_fts5(library) -> None:
    from nexus.plans.matcher import plan_match

    _seed(library, query="research projection mechanism",
          dimensions={"verb": "research"})
    cache = _FakeCache(hits=[], available=True)  # cache up but empty

    matches = plan_match(
        intent="projection mechanism", library=library, cache=cache,
        min_confidence=0.85,
    )
    assert len(matches) >= 1
    assert all(m.confidence is None for m in matches)


@pytest.mark.asyncio
async def test_fts5_fallback_returns_match_objects_runnable(library) -> None:
    """FTS5 fallback Match must carry the same shape as the cosine path
    so plan_run accepts it without branching."""
    from nexus.plans.matcher import plan_match
    from nexus.plans.runner import plan_run

    _seed(library, query="research mechanism",
          dimensions={"verb": "research", "scope": "global"})
    matches = plan_match(intent="research mechanism", library=library,
                         cache=None, min_confidence=0.85)
    assert matches

    # plan_run on a 0-step plan never dispatches; just verifies the
    # shape contract holds.
    result = await plan_run(matches[0], {})
    assert result.steps == []


# ── Dimensional filter ──────────────────────────────────────────────────────


def test_plan_match_dimension_filter_excludes_non_superset(library) -> None:
    from nexus.plans.matcher import plan_match

    research = _seed(library, query="r1", dimensions={"verb": "research"})
    review = _seed(library, query="r2", dimensions={"verb": "review"})
    cache = _FakeCache(hits=[(research, 0.05), (review, 0.05)])

    matches = plan_match(
        intent="x", library=library, cache=cache,
        dimensions={"verb": "research"}, min_confidence=0.5,
    )
    assert [m.plan_id for m in matches] == [research]


def test_plan_match_dimension_filter_allows_superset(library) -> None:
    """A plan with extra dimensions still matches a narrower filter."""
    from nexus.plans.matcher import plan_match

    plan_id = _seed(library, query="r",
                    dimensions={"verb": "research", "scope": "global",
                                "strategy": "default"})
    cache = _FakeCache(hits=[(plan_id, 0.05)])

    matches = plan_match(
        intent="x", library=library, cache=cache,
        dimensions={"verb": "research"}, min_confidence=0.5,
    )
    assert len(matches) == 1


# ── Metrics (SC-12) ─────────────────────────────────────────────────────────


def test_plan_match_increments_match_count(library) -> None:
    from nexus.plans.matcher import plan_match

    plan_id = _seed(library, query="r",
                    dimensions={"verb": "research", "scope": "global"})
    cache = _FakeCache(hits=[(plan_id, 0.05)])

    plan_match(intent="x", library=library, cache=cache, min_confidence=0.5)

    row = library.get_plan(plan_id)
    assert row is not None
    assert row["match_count"] == 1
    assert row["match_conf_sum"] == pytest.approx(0.95, abs=1e-6)


def test_fts5_fallback_increments_match_count_only(library) -> None:
    """Confidence-less matches must not contribute to ``match_conf_sum``."""
    from nexus.plans.matcher import plan_match

    _seed(library, query="research mechanism",
          dimensions={"verb": "research"})
    matches = plan_match(intent="research mechanism", library=library,
                         cache=None, min_confidence=0.85)
    assert matches
    pid = matches[0].plan_id
    row = library.get_plan(pid)
    assert row is not None
    assert row["match_count"] == 1
    assert row["match_conf_sum"] == 0.0


# ── Stub parameter pinning (SC-TODO: Phase 2) ────────────────────────────────


def test_scope_preference_empty_is_a_no_op(library) -> None:
    """Empty scope_preference preserves pre-Phase-2b ordering exactly.

    Regression guard: existing callers that don't pass scope_preference
    must see the same behaviour.
    """
    from nexus.plans.matcher import plan_match

    plan_id = _seed(library, query="research projection quality",
                    dimensions={"verb": "research", "scope": "global"},
                    scope_tags="rdr__arcaneum")
    cache = _FakeCache(hits=[(plan_id, 0.05)])
    without_scope = plan_match(
        intent="projection quality", library=library, cache=cache,
        min_confidence=0.5,
    )
    cache_b = _FakeCache(hits=[(plan_id, 0.05)])
    with_empty_scope = plan_match(
        intent="projection quality", library=library, cache=cache_b,
        min_confidence=0.5,
        scope_preference="",
    )
    assert [m.plan_id for m in without_scope] == [m.plan_id for m in with_empty_scope]


# ── Scope-aware re-ranking (RDR-091 Phase 2b, nexus-bgs7) ───────────────────


def test_scope_conflict_filter_drops_mismatched_plans(library) -> None:
    """A plan tagged only for 'knowledge__delos' is dropped when
    the caller scope is 'rdr__arcaneum'. Zero-candidate outcome
    returns no hits (nx_answer falls through to inline planner)."""
    from nexus.plans.matcher import plan_match

    plan_id = _seed(library, query="something",
                    dimensions={"verb": "research", "variant": "a"},
                    scope_tags="knowledge__delos")
    cache = _FakeCache(hits=[(plan_id, 0.05)])
    result = plan_match(
        intent="something", library=library, cache=cache,
        min_confidence=0.5, scope_preference="rdr__arcaneum",
    )
    assert result == [], "conflict-only candidate must yield zero hits"


def test_scope_fit_boost_lifts_matching_plan_over_agnostic(library) -> None:
    """At equal base cosine, a plan whose scope_tags match the caller
    scope ranks above an agnostic (scope_tags='') plan."""
    from nexus.plans.matcher import plan_match

    matching = _seed(library, query="traction",
                     dimensions={"verb": "research", "variant": "m"},
                     scope_tags="rdr__arcaneum")
    agnostic = _seed(library, query="traction",
                     dimensions={"verb": "research", "variant": "a"},
                     scope_tags="")
    # Same base cosine for both.
    cache = _FakeCache(hits=[(agnostic, 0.10), (matching, 0.10)])
    result = plan_match(
        intent="traction", library=library, cache=cache,
        min_confidence=0.5, scope_preference="rdr__arcaneum",
    )
    assert [m.plan_id for m in result] == [matching, agnostic]


def test_scope_bare_family_prefix_matches_specific_tag(library) -> None:
    """Caller scope 'rdr__' is a prefix of tag 'rdr__arcaneum' — matches."""
    from nexus.plans.matcher import plan_match

    plan_id = _seed(library, query="q",
                    dimensions={"verb": "research"},
                    scope_tags="rdr__arcaneum")
    cache = _FakeCache(hits=[(plan_id, 0.10)])
    result = plan_match(
        intent="q", library=library, cache=cache,
        min_confidence=0.5, scope_preference="rdr__",
    )
    assert [m.plan_id for m in result] == [plan_id]


def test_scope_specific_caller_matches_bare_family_tag(library) -> None:
    """Caller scope 'rdr__arcaneum' against plan tag 'rdr__' (broader
    bare-family plan) still matches — broader plans serve narrower queries."""
    from nexus.plans.matcher import plan_match

    plan_id = _seed(library, query="q",
                    dimensions={"verb": "research"},
                    scope_tags="rdr__")
    cache = _FakeCache(hits=[(plan_id, 0.10)])
    result = plan_match(
        intent="q", library=library, cache=cache,
        min_confidence=0.5, scope_preference="rdr__arcaneum",
    )
    assert [m.plan_id for m in result] == [plan_id]


def test_agnostic_plan_passes_through_when_scope_requested(library) -> None:
    """A plan with empty scope_tags competes on base cosine alone when
    scope_preference is set — it is NOT filtered out (neutral weight)."""
    from nexus.plans.matcher import plan_match

    plan_id = _seed(library, query="q",
                    dimensions={"verb": "research"},
                    scope_tags="")
    cache = _FakeCache(hits=[(plan_id, 0.10)])
    result = plan_match(
        intent="q", library=library, cache=cache,
        min_confidence=0.5, scope_preference="rdr__arcaneum",
    )
    assert [m.plan_id for m in result] == [plan_id]


def test_specificity_tie_break_favors_narrower_plan(library) -> None:
    """Two matching plans with identical base cosine: the plan with
    fewer scope_tags (more specific) wins the tie-break."""
    from nexus.plans.matcher import plan_match

    narrow = _seed(library, query="q",
                   dimensions={"verb": "research", "variant": "n"},
                   scope_tags="rdr__arcaneum")
    broad = _seed(library, query="q",
                  dimensions={"verb": "research", "variant": "b"},
                  scope_tags="rdr__arcaneum,rdr__delos,rdr__nexus")
    cache = _FakeCache(hits=[(broad, 0.10), (narrow, 0.10)])
    result = plan_match(
        intent="q", library=library, cache=cache,
        min_confidence=0.5, scope_preference="rdr__arcaneum",
    )
    assert [m.plan_id for m in result] == [narrow, broad]


def test_multi_corpus_plan_matches_either_tag(library) -> None:
    """A bridging plan tagged 'rdr__arcaneum,knowledge__delos' matches
    when the caller scope prefix-matches EITHER tag (intersect semantics)."""
    from nexus.plans.matcher import plan_match

    bridge = _seed(library, query="q",
                   dimensions={"verb": "research"},
                   scope_tags="knowledge__delos,rdr__arcaneum")
    cache = _FakeCache(hits=[(bridge, 0.10)])
    result = plan_match(
        intent="q", library=library, cache=cache,
        min_confidence=0.5, scope_preference="rdr__arcaneum",
    )
    assert [m.plan_id for m in result] == [bridge]


def test_fts5_fallback_applies_scope_conflict_filter(library) -> None:
    """FTS5 fallback also drops conflicting plans when scope_preference is set."""
    from nexus.plans.matcher import plan_match

    _seed(library, query="something specific",
          dimensions={"verb": "research"}, scope_tags="knowledge__delos")
    # No cache → FTS5 fallback path.
    result = plan_match(
        intent="specific", library=library, cache=None,
        min_confidence=0.5, scope_preference="rdr__arcaneum",
    )
    assert result == [], "FTS5 path must also filter scope-conflicting plans"


def test_scope_fit_case_insensitive_prefix_match(library) -> None:
    """_scope_fit matches across case on both sides (nexus-yi7m).

    Live probe found a grown plan tagged ``knowledge__Delos`` (capital D,
    from a real mixed-case collection namespace) that did NOT match a
    caller scope ``knowledge__delos`` (lowercase, the ChromaDB
    convention) because startswith is case-sensitive. ChromaDB's naming
    convention doesn't use case to disambiguate, so the two should be
    treated as the same keyspace."""
    from nexus.plans.matcher import plan_match

    # Plan tagged with mixed-case real-collection form.
    plan_id = _seed(library, query="q",
                    dimensions={"verb": "research", "variant": "d"},
                    scope_tags="knowledge__Delos")
    cache = _FakeCache(hits=[(plan_id, 0.10)])
    # Caller uses the conventional lowercase form.
    result = plan_match(
        intent="q", library=library, cache=cache,
        min_confidence=0.5, scope_preference="knowledge__delos",
    )
    assert [m.plan_id for m in result] == [plan_id]


def test_scope_fit_case_insensitive_reverse_direction(library) -> None:
    """Reverse: caller mixed-case, plan lowercase; still matches."""
    from nexus.plans.matcher import plan_match

    plan_id = _seed(library, query="q",
                    dimensions={"verb": "research", "variant": "dl"},
                    scope_tags="knowledge__delos")
    cache = _FakeCache(hits=[(plan_id, 0.10)])
    result = plan_match(
        intent="q", library=library, cache=cache,
        min_confidence=0.5, scope_preference="knowledge__Delos",
    )
    assert [m.plan_id for m in result] == [plan_id]


def test_scope_fit_case_insensitive_bare_family_prefix(library) -> None:
    """Bare-family prefix matching remains case-insensitive too:
    caller ``knowledge__`` matches tag ``knowledge__Delos``."""
    from nexus.plans.matcher import plan_match

    plan_id = _seed(library, query="q",
                    dimensions={"verb": "research", "variant": "bf"},
                    scope_tags="knowledge__Delos")
    cache = _FakeCache(hits=[(plan_id, 0.10)])
    result = plan_match(
        intent="q", library=library, cache=cache,
        min_confidence=0.5, scope_preference="KNOWLEDGE__",
    )
    assert [m.plan_id for m in result] == [plan_id]


def test_scope_fit_preserves_stored_case(library) -> None:
    """Stored scope_tags value is untouched by case-insensitive compare.
    The Match object reports the real mixed-case name so plan_search /
    authoring output shows the actual collection reference."""
    from nexus.plans.matcher import plan_match

    plan_id = _seed(library, query="q",
                    dimensions={"verb": "research", "variant": "preserve"},
                    scope_tags="knowledge__Delos")
    cache = _FakeCache(hits=[(plan_id, 0.10)])
    result = plan_match(
        intent="q", library=library, cache=cache,
        min_confidence=0.5, scope_preference="knowledge__delos",
    )
    assert len(result) == 1
    assert result[0].scope_tags == "knowledge__Delos", (
        "stored case must be preserved; only comparison is folded"
    )


def test_scope_boost_formula_is_multiplicative_with_unequal_cosines(library) -> None:
    """Formula-pinning regression guard (RDR-091 critic follow-up).

    Phase 2b originally shipped an additive `confidence + weight * fit`
    formula; the RDR specifies multiplicative
    `confidence * (1 + weight * fit)`. The Phase 2b test suite used
    equal-cosine fixtures so the bug was undetectable in unit coverage.
    This case uses meaningfully unequal cosines so only the correct
    multiplicative formula produces the expected ordering.

    Numbers: specialized=0.79 matching, agnostic=0.82 neutral, w=0.15.
      * multiplicative: 0.79 * 1.15 = 0.9085 > 0.82   (specialized wins)
      * additive:       0.79 + 0.15 = 0.94   > 0.82   (also wins — so
                        we instead use a tighter case below)

    A tighter case exposes the difference: specialized=0.50 matching,
    agnostic=0.60 neutral, w=0.15:
      * multiplicative: 0.50 * 1.15 = 0.575  < 0.60   (agnostic wins)
      * additive:       0.50 + 0.15 = 0.65   > 0.60   (specialized wins)
    """
    from nexus.plans.matcher import plan_match

    specialized = _seed(library, query="q",
                        dimensions={"verb": "research", "variant": "s"},
                        scope_tags="rdr__arcaneum")
    agnostic = _seed(library, query="q",
                     dimensions={"verb": "research", "variant": "a"},
                     scope_tags="")
    # distance=0.50 → confidence=0.50 (specialized), distance=0.40 → 0.60 (agnostic).
    cache = _FakeCache(hits=[(specialized, 0.50), (agnostic, 0.40)])
    result = plan_match(
        intent="q", library=library, cache=cache,
        min_confidence=0.40, scope_preference="rdr__arcaneum",
    )
    # Multiplicative says agnostic (0.60) beats specialized (0.575).
    # Additive would flip the order.
    assert [m.plan_id for m in result][0] == agnostic, (
        "formula must be multiplicative; a small boost does not override "
        "a higher-cosine agnostic plan"
    )


def test_fts5_fallback_keeps_matching_and_agnostic(library) -> None:
    """FTS5 fallback keeps matching and agnostic plans when scope set."""
    from nexus.plans.matcher import plan_match

    matching = _seed(library, query="pressing matter",
                     dimensions={"verb": "research", "variant": "m"},
                     scope_tags="rdr__arcaneum")
    agnostic = _seed(library, query="pressing matter",
                     dimensions={"verb": "research", "variant": "a"},
                     scope_tags="")
    result = plan_match(
        intent="pressing", library=library, cache=None,
        min_confidence=0.5, scope_preference="rdr__arcaneum",
    )
    returned_ids = {m.plan_id for m in result}
    assert matching in returned_ids
    assert agnostic in returned_ids


# ── Phase 2c qualitative fixture pairs (RDR-091, nexus-svcg) ──────────────
#
# Five (scope, expected-plan) fixtures inspired by the RDR §Problem
# Statement and §Phase 2c. Authored against realistic unequal cosines,
# then the _SCOPE_FIT_WEIGHT constant was picked (0.15) so these pass
# together with all pre-Phase-2b tests.


def test_fixture_arcaneum_specialized_beats_generic_agnostic(library) -> None:
    """RDR §Problem Statement: scope='rdr__arcaneum-*', specialized plan
    at lower base cosine (0.79) should beat generic agnostic at higher
    base cosine (0.82) after the multiplicative scope-fit boost."""
    from nexus.plans.matcher import plan_match

    specialized = _seed(library,
                        query="arcaneum trade-off analysis",
                        dimensions={"verb": "compare", "variant": "arcaneum"},
                        scope_tags="rdr__arcaneum")
    generic = _seed(library,
                    query="decision lookup",
                    dimensions={"verb": "research", "variant": "generic"},
                    scope_tags="")
    # Generic has HIGHER base cosine than specialized (problem statement).
    cache = _FakeCache(hits=[(generic, 0.18), (specialized, 0.21)])
    result = plan_match(
        intent="arcaneum trade-offs", library=library, cache=cache,
        min_confidence=0.5, scope_preference="rdr__arcaneum-2ad2825c",
    )
    # After multiplicative boost: specialized = 0.79 * 1.15 = 0.9085;
    # generic stays 0.82. Specialized wins.
    assert [m.plan_id for m in result][0] == specialized


def test_fixture_code_plan_beats_cross_corpus_on_tie_break(library) -> None:
    """scope='code__nexus', at equal base cosine a code-only plan
    (1 tag) beats a cross-corpus plan (2 tags) via specificity tie-break."""
    from nexus.plans.matcher import plan_match

    code_only = _seed(library, query="q",
                      dimensions={"verb": "index", "variant": "code"},
                      scope_tags="code__nexus")
    cross_corpus = _seed(library, query="q",
                         dimensions={"verb": "index", "variant": "both"},
                         scope_tags="code__nexus,rdr__nexus")
    cache = _FakeCache(hits=[(cross_corpus, 0.12), (code_only, 0.12)])
    result = plan_match(
        intent="indexing", library=library, cache=cache,
        min_confidence=0.5, scope_preference="code__nexus",
    )
    assert [m.plan_id for m in result] == [code_only, cross_corpus]


def test_fixture_agnostic_fallback_unchanged_when_scope_empty(library) -> None:
    """scope='' (empty) is a hard no-op. Ordering matches the pre-Phase-2b
    behaviour (pure cosine rank). Regression guard for the empty path."""
    from nexus.plans.matcher import plan_match

    plan_a = _seed(library, query="q",
                   dimensions={"verb": "research", "variant": "a"},
                   scope_tags="rdr__arcaneum")
    plan_b = _seed(library, query="q",
                   dimensions={"verb": "research", "variant": "b"},
                   scope_tags="knowledge__delos")
    cache = _FakeCache(hits=[(plan_a, 0.10), (plan_b, 0.20)])
    result = plan_match(
        intent="q", library=library, cache=cache,
        min_confidence=0.5, scope_preference="",
    )
    # Pure cosine: plan_a (confidence 0.9) before plan_b (confidence 0.8).
    assert [m.plan_id for m in result] == [plan_a, plan_b]


def test_fixture_bridging_plan_matches_via_intersect(library) -> None:
    """scope='knowledge__delos', a bridging plan tagged
    'knowledge__delos,knowledge__arcaneum' passes (intersect semantics):
    caller scope prefix-matches at least one tag."""
    from nexus.plans.matcher import plan_match

    bridge = _seed(library, query="cross-paper synthesis",
                   dimensions={"verb": "compare", "variant": "bridge"},
                   scope_tags="knowledge__arcaneum,knowledge__delos")
    cache = _FakeCache(hits=[(bridge, 0.15)])
    result = plan_match(
        intent="cross-paper", library=library, cache=cache,
        min_confidence=0.5, scope_preference="knowledge__delos",
    )
    assert [m.plan_id for m in result] == [bridge]


def test_fixture_zero_candidate_scope_returns_empty(library) -> None:
    """scope='rdr__nonexistent', no plan with matching scope_tags:
    matcher returns []. nx_answer falls through to inline planner."""
    from nexus.plans.matcher import plan_match

    _seed(library, query="q1",
          dimensions={"verb": "research", "variant": "k"},
          scope_tags="knowledge__delos")
    _seed(library, query="q2",
          dimensions={"verb": "research", "variant": "c"},
          scope_tags="code__nexus")
    # Every seeded plan conflicts with 'rdr__nonexistent'.
    cache = _FakeCache(hits=[])
    result = plan_match(
        intent="q", library=library, cache=cache,
        min_confidence=0.5, scope_preference="rdr__nonexistent",
    )
    assert result == []


def test_context_parameter_is_a_no_op(library) -> None:
    """context dict is accepted but does not change results (Phase 2 stub)."""
    from nexus.plans.matcher import plan_match

    plan_id = _seed(library, query="research mechanism",
                    dimensions={"verb": "research"})
    cache_a = _FakeCache(hits=[(plan_id, 0.05)])
    cache_b = _FakeCache(hits=[(plan_id, 0.05)])

    without_ctx = plan_match(
        intent="mechanism", library=library, cache=cache_a, min_confidence=0.5,
    )
    with_ctx = plan_match(
        intent="mechanism", library=library, cache=cache_b, min_confidence=0.5,
        context={"user_context": "some extra context"},
    )

    assert [m.plan_id for m in without_ctx] == [m.plan_id for m in with_ctx], (
        "context parameter must be a no-op until Phase 2 ships"
    )
