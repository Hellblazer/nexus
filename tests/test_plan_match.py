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
          tags: str = "", project: str = "nexus") -> int:
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


def test_scope_preference_is_a_no_op(library) -> None:
    """scope_preference is accepted but does not change results (Phase 2 stub).

    This test pins the current no-op contract so a future implementation
    knows exactly what it needs to replace. When Phase 2 scope ranking
    ships, this test must be updated to assert different behaviour.
    """
    from nexus.plans.matcher import plan_match

    plan_id = _seed(library, query="research projection quality",
                    dimensions={"verb": "research", "scope": "global"})
    cache = _FakeCache(hits=[(plan_id, 0.05)])

    without_scope = plan_match(
        intent="projection quality", library=library, cache=cache,
        min_confidence=0.5,
    )
    # Reset cache so second call sees the same hits
    cache_b = _FakeCache(hits=[(plan_id, 0.05)])
    with_scope = plan_match(
        intent="projection quality", library=library, cache=cache_b,
        min_confidence=0.5,
        scope_preference="rdr-080",
    )

    assert [m.plan_id for m in without_scope] == [m.plan_id for m in with_scope], (
        "scope_preference must be a no-op until Phase 2 ships"
    )


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
