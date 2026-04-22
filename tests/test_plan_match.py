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
