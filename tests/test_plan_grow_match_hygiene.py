# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-zgko: plan-grow match hygiene to prevent over-broad re-firing.

RDR-090 P1.3. Plans saved by the inline-planner via the RDR-084
plan-grow path inherit the originating question's match-text. The
match-text is good for paraphrases of the same question but bad for
unrelated questions that happen to share scaffolding ("which RDR…").
The spike found plan #67 (saved from Q1 'Which RDR introduced
catalog tumblers?') firing on Q3 (taxonomy/BERTopic) and Q4 (hooks
comparative) and dutifully returning rdr-049 for both.

The fix is a higher confidence floor on grown plans (tagged
``ad-hoc,grown``). High-cosine paraphrases still fire; loose
prefix-overlap matches drop.

Contract pinned here:

  - Grown plan + below-grown-floor cosine → drops from candidate pool.
  - Grown plan + above-grown-floor cosine → admits as today.
  - Library plan (non-grown) + above-min-confidence cosine → admits
    as today (the floor is grown-specific).
  - Caller-supplied ``min_confidence`` higher than the grown floor →
    the higher value wins (the floor is additive, not authoritative).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────


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
          tags: str = "", scope_tags: str | None = None) -> int:
    from nexus.plans.schema import canonical_dimensions_json
    dims_json = canonical_dimensions_json(dimensions) if dimensions else None
    return library.save_plan(
        query=query,
        plan_json=json.dumps({"steps": []}),
        tags=tags,
        project="personal",
        dimensions=dims_json,
        verb=(dimensions or {}).get("verb"),
        scope=(dimensions or {}).get("scope", "personal"),
        name=(dimensions or {}).get("strategy", "default"),
        scope_tags=scope_tags,
    )


class _FakeCache:
    def __init__(self, hits: list[tuple[int, float]] | None = None,
                 available: bool = True) -> None:
        self._hits = list(hits or [])
        self._available = available

    @property
    def is_available(self) -> bool:
        return self._available

    def query(self, intent: str, n: int) -> list[tuple[int, float]]:
        return self._hits[:n]


# ── Tests ─────────────────────────────────────────────────────────────────


def test_grown_plan_drops_below_grown_floor(library) -> None:
    """A 'grown' plan with cosine 0.50 (below the grown floor of 0.60)
    must not be returned, even though the caller's min_confidence is
    only 0.40 — this is the plan #67 leakage scenario.
    """
    from nexus.plans.matcher import plan_match

    plan_id = _seed(
        library,
        query="Which RDR introduced catalog tumblers?",
        dimensions={"verb": "research", "strategy": "rdr-introduced-catalog"},
        tags="ad-hoc,grown",
    )
    # cosine distance 0.50 → confidence 0.50 (below the grown floor)
    cache = _FakeCache(hits=[(plan_id, 0.50)])
    result = plan_match(
        intent="how does the BERTopic taxonomy work",
        library=library, cache=cache,
        min_confidence=0.40, n=5,
    )
    assert result == [], (
        f"grown plan with 0.50 cosine should drop below grown floor; "
        f"got {[(m.plan_id, m.confidence) for m in result]}"
    )


def test_grown_plan_admits_above_grown_floor(library) -> None:
    """High-cosine paraphrases of the originating question still fire.

    A grown plan with cosine 0.85 clears even the stricter grown floor.
    """
    from nexus.plans.matcher import plan_match

    plan_id = _seed(
        library,
        query="Which RDR introduced catalog tumblers?",
        dimensions={"verb": "research", "strategy": "rdr-introduced-catalog"},
        tags="ad-hoc,grown",
    )
    cache = _FakeCache(hits=[(plan_id, 0.15)])  # confidence 0.85
    result = plan_match(
        intent="what is the RDR that introduced catalog tumblers",
        library=library, cache=cache,
        min_confidence=0.40, n=5,
    )
    assert [m.plan_id for m in result] == [plan_id]


def test_library_plan_admits_at_default_floor(library) -> None:
    """Non-grown library plans use the caller's min_confidence directly
    — the grown floor must NOT apply to them. Regression guard against
    accidentally tightening every plan.
    """
    from nexus.plans.matcher import plan_match

    plan_id = _seed(
        library,
        query="research walkthrough",
        dimensions={"verb": "research", "strategy": "default"},
        tags="builtin-template,rdr-078",
    )
    cache = _FakeCache(hits=[(plan_id, 0.50)])  # confidence 0.50
    result = plan_match(
        intent="walkthrough", library=library, cache=cache,
        min_confidence=0.40, n=5,
    )
    assert [m.plan_id for m in result] == [plan_id], (
        "non-grown plan above caller's min_confidence must admit"
    )


def test_caller_min_confidence_above_grown_floor_wins(library) -> None:
    """When the caller passes a min_confidence higher than the grown
    floor, the higher value wins (the floor is additive, not
    authoritative).

    Use an intent with no FTS5 keyword overlap with the grown plan's
    query, so the FTS5 fallback can't re-admit the plan after the
    cosine path's tighter filter drops it.
    """
    from nexus.plans.matcher import plan_match

    plan_id = _seed(
        library,
        query="Which RDR introduced catalog tumblers?",
        dimensions={"verb": "research", "strategy": "rdr-intro"},
        tags="ad-hoc,grown",
    )
    # confidence 0.65 — above grown floor but below caller's 0.70.
    cache = _FakeCache(hits=[(plan_id, 0.35)])
    result = plan_match(
        intent="unrelated abstract pipeline topic",
        library=library, cache=cache,
        min_confidence=0.70, n=5,
    )
    assert result == [], (
        "caller's higher min_confidence must win over the grown floor"
    )


def test_grown_floor_does_not_break_fts5_fallback(library) -> None:
    """The FTS5 fallback path uses confidence=None as a sentinel meaning
    'skill-level gate decides'. Grown plans on the FTS5 path must still
    be admitted — the cosine-floor logic only applies to numeric
    confidence values.
    """
    from nexus.plans.matcher import plan_match

    _seed(
        library,
        query="catalog tumblers introduction RDR",
        dimensions={"verb": "research", "strategy": "rdr-intro"},
        tags="ad-hoc,grown",
    )
    # No cache → FTS5 fallback. Match returns confidence=None.
    result = plan_match(
        intent="catalog tumblers", library=library, cache=None,
        min_confidence=0.40, n=5,
    )
    # The FTS5 hit should pass through; the cosine-floor check is a
    # numeric comparison and confidence=None should not be excluded by it.
    assert result, "FTS5 fallback for grown plans must still admit"
    assert result[0].confidence is None


# ── nexus-mz5tv: unanchored grown plan must not be a cross-project attractor ──


def _seed_unanchored(library, *, query: str, tags: str, project: str = "") -> int:
    """Seed a plan with an explicit (possibly empty) project and NO
    scope_tags — the unanchored shape the mz5tv residual is about."""
    from nexus.plans.matcher import _is_unanchored_grown  # noqa: F401 — import-time guard that symbol exists
    return library.save_plan(
        query=query,
        plan_json=json.dumps({"steps": []}),
        # scope_tags forced "" below (explicit kwarg) — bypasses the #1069
        # project-inference path so this seeds the unanchored shape directly.
        tags=tags,
        project=project,
        dimensions=None,
        verb="research",
        scope="",
        name="default",
        scope_tags="",
    )


def test_unanchored_grown_dropped_under_scope_pref(library) -> None:
    """A grown plan with empty scope_tags AND empty project must NOT match
    a scoped caller — it has no provenance and would otherwise compete
    globally at the grown floor (the mz5tv residual)."""
    from nexus.plans.matcher import plan_match

    plan_id = _seed_unanchored(
        library, query="how does the projector replay events",
        tags="ad-hoc,grown", project="",
    )
    cache = _FakeCache(hits=[(plan_id, 0.15)])  # confidence 0.85, above grown floor
    result = plan_match(
        intent="how does the projector replay events",
        library=library, cache=cache,
        min_confidence=0.40, n=5,
        scope_preference="knowledge__nexus",
    )
    assert result == [], (
        f"unanchored grown plan must drop under a scope_preference; "
        f"got {[(m.plan_id, m.confidence) for m in result]}"
    )


def test_unanchored_grown_kept_without_scope_pref(library) -> None:
    """With NO caller scope, the unanchored grown plan still competes (the
    legitimate corpus:all -> corpus:all case) — the drop is scoped to an
    explicit scope_preference."""
    from nexus.plans.matcher import plan_match

    plan_id = _seed_unanchored(
        library, query="how does the projector replay events",
        tags="ad-hoc,grown", project="",
    )
    cache = _FakeCache(hits=[(plan_id, 0.15)])
    result = plan_match(
        intent="how does the projector replay events",
        library=library, cache=cache,
        min_confidence=0.40, n=5,
    )
    assert [m.plan_id for m in result] == [plan_id]


def test_grown_with_project_anchor_kept_under_scope_pref(library) -> None:
    """A non-empty ``project`` column is the escape hatch from the
    unanchored-grown drop: mz5tv targets EMPTY project only, so a grown
    plan with project set (scope_tags forced empty here) is NOT dropped.
    Production plans that take the #1069 path instead carry a non-empty
    scope_tags, which is the other escape hatch."""
    from nexus.plans.matcher import plan_match

    plan_id = _seed_unanchored(
        library, query="how does the projector replay events",
        tags="ad-hoc,grown", project="nexus",
    )
    cache = _FakeCache(hits=[(plan_id, 0.15)])
    result = plan_match(
        intent="how does the projector replay events",
        library=library, cache=cache,
        min_confidence=0.40, n=5,
        scope_preference="knowledge__nexus",
    )
    assert [m.plan_id for m in result] == [plan_id]


def test_non_grown_unanchored_kept_under_scope_pref(library) -> None:
    """The drop is grown-specific: a non-grown agnostic plan (empty
    scope_tags + empty project) stays neutral under a scope_preference."""
    from nexus.plans.matcher import plan_match

    plan_id = _seed_unanchored(
        library, query="how does the projector replay events",
        tags="ad-hoc", project="",
    )
    cache = _FakeCache(hits=[(plan_id, 0.15)])
    result = plan_match(
        intent="how does the projector replay events",
        library=library, cache=cache,
        min_confidence=0.40, n=5,
        scope_preference="knowledge__nexus",
    )
    assert [m.plan_id for m in result] == [plan_id]


def test_unanchored_grown_dropped_on_fts5_path(library) -> None:
    """The same drop applies on the FTS5 fallback path (no cache)."""
    from nexus.plans.matcher import plan_match

    _seed_unanchored(
        library, query="projector replay events catalog",
        tags="ad-hoc,grown", project="",
    )
    result = plan_match(
        intent="projector replay events", library=library, cache=None,
        min_confidence=0.40, n=5,
        scope_preference="knowledge__nexus",
    )
    assert result == [], (
        "unanchored grown plan must drop on the FTS5 path under a scope_pref"
    )


def test_unanchored_grown_kept_for_corpus_all_sentinel(library) -> None:
    """nexus-mz5tv review: ``scope_preference="all"`` is the corpus:all
    sentinel — NO real scope preference — so the unanchored-grown drop must
    NOT fire (it would lose the legitimate corpus:all -> corpus:all match)."""
    from nexus.plans.matcher import plan_match

    plan_id = _seed_unanchored(
        library, query="how does the projector replay events",
        tags="ad-hoc,grown", project="",
    )
    cache = _FakeCache(hits=[(plan_id, 0.15)])
    result = plan_match(
        intent="how does the projector replay events",
        library=library, cache=cache,
        min_confidence=0.40, n=5,
        scope_preference="all",
    )
    assert [m.plan_id for m in result] == [plan_id], (
        "corpus:all sentinel must not trigger the unanchored-grown drop"
    )
