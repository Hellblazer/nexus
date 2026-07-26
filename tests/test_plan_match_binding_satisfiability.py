# SPDX-License-Identifier: AGPL-3.0-or-later
"""The matcher must not offer a plan whose typed bindings cannot be satisfied.

Companion to ``test_nx_answer_typed_binding_autoalias``. That test covers
the runtime refusal; this one covers preventing the mis-match in the first
place.

Observed 2026-07-25: a type-agnostic research question matched builtin
plan 14 (``type-scoped-search``), which requires ``content_type``. The
plan is correct — its description says it is "useful when the caller knows
the artefact kind" — so the fault was that it was OFFERED at all for a
question specifying no type. Refusing at run time surfaces the mis-match;
declining to offer prevents it, letting ``nx_answer`` fall through to a
plan that can actually run.

The drop is deliberately observable. Silently shrinking the candidate pool
is the same class of defect as silently widening a retrieval, so a drop
increments a counter and is logged.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import structlog
from structlog.testing import capture_logs


@pytest.fixture()
def library(tmp_path: Path):
    from nexus.db.migrations import _add_plan_dimensional_identity
    from nexus.db.t2.plan_library import PlanLibrary

    lib = PlanLibrary(tmp_path / "plans.db")
    _add_plan_dimensional_identity(lib.conn)
    lib.conn.commit()
    return lib


def _seed_with_bindings(
    library, *, query: str, required: list[str],
    defaults: dict | None = None, name: str = "p",
    scope_tags: str | None = None,
) -> int:
    from nexus.plans.schema import canonical_dimensions_json

    plan = {"steps": [], "required_bindings": required}
    dims = {"verb": "research", "scope": "global", "strategy": name}
    return library.save_plan(
        query=query, plan_json=json.dumps(plan), tags="", project="nexus",
        dimensions=canonical_dimensions_json(dims),
        verb="research", scope="global", name=name,
        # default_bindings is its own column — Match.from_plan_row reads
        # row['default_bindings'], never plan_json.
        default_bindings=json.dumps(defaults) if defaults else None,
        scope_tags=scope_tags,
    )


class _FakeCache:
    def __init__(self, hits: list[tuple[int, float]] | None = None) -> None:
        self._hits = list(hits or [])

    @property
    def is_available(self) -> bool:
        return True

    def query(self, intent: str, n: int) -> list[tuple[int, float]]:
        return self._hits[:n]


def test_plan_requiring_unavailable_typed_binding_is_not_offered(library) -> None:
    """The defect: a type-scoped plan offered to a type-agnostic caller."""
    from nexus.plans.matcher import plan_match

    pid = _seed_with_bindings(
        library, query="search within a content type",
        required=["question", "content_type"], name="type-scoped",
    )
    matches = plan_match(
        intent="which papers discuss intermediate representations",
        library=library, cache=_FakeCache(hits=[(pid, 0.05)]),
        min_confidence=0.85, available_bindings=frozenset({"intent"}),
    )
    assert matches == [], (
        "a plan requiring content_type was offered to a caller that "
        "cannot supply one — this is the mis-match that produced a "
        "zero-result answer"
    )


def test_the_same_plan_is_offered_when_the_binding_is_available(library) -> None:
    """Non-vacuity: the drop is caused by the binding, not by the seed."""
    from nexus.plans.matcher import plan_match

    pid = _seed_with_bindings(
        library, query="search within a content type",
        required=["question", "content_type"], name="type-scoped",
    )
    matches = plan_match(
        intent="which papers discuss intermediate representations",
        library=library, cache=_FakeCache(hits=[(pid, 0.05)]),
        min_confidence=0.85,
        available_bindings=frozenset({"intent", "content_type"}),
    )
    assert [m.plan_id for m in matches] == [pid]


def test_a_plan_default_satisfies_the_binding(library) -> None:
    """A plan carrying its own default needs nothing from the caller."""
    from nexus.plans.matcher import plan_match

    pid = _seed_with_bindings(
        library, query="rdr-scoped search", required=["content_type"],
        defaults={"content_type": "rdr"}, name="rdr-scoped",
    )
    matches = plan_match(
        intent="which rdrs discuss this", library=library,
        cache=_FakeCache(hits=[(pid, 0.05)]), min_confidence=0.85,
        available_bindings=frozenset({"intent"}),
    )
    assert [m.plan_id for m in matches] == [pid]


def test_free_text_bindings_never_cause_a_drop(library) -> None:
    """concept/area/topic are aliasable from the question — always offered."""
    from nexus.plans.matcher import plan_match

    pid = _seed_with_bindings(
        library, query="analyse a concept", required=["question", "concept"],
        name="concept-plan",
    )
    matches = plan_match(
        intent="analyse this concept", library=library,
        cache=_FakeCache(hits=[(pid, 0.05)]), min_confidence=0.85,
        available_bindings=frozenset({"intent"}),
    )
    assert [m.plan_id for m in matches] == [pid]


def test_omitting_available_bindings_preserves_prior_behaviour(library) -> None:
    """Back-compat: callers that don't declare their bindings are unfiltered."""
    from nexus.plans.matcher import plan_match

    pid = _seed_with_bindings(
        library, query="search within a content type",
        required=["question", "content_type"], name="type-scoped",
    )
    matches = plan_match(
        intent="which papers discuss this", library=library,
        cache=_FakeCache(hits=[(pid, 0.05)]), min_confidence=0.85,
    )
    assert [m.plan_id for m in matches] == [pid]


def test_the_drop_is_logged_not_silent(library) -> None:
    """Shrinking the pool silently is the same defect class as widening."""
    from nexus.plans.matcher import plan_match

    pid = _seed_with_bindings(
        library, query="search within a content type",
        required=["content_type"], name="type-scoped",
    )
    # The suite configures structlog in cli mode (WARNING), and
    # make_filtering_bound_logger drops below-threshold calls BEFORE any
    # processor runs, so capture_logs alone would see nothing. Pin the
    # wrapper to INFO — the level MCP mode actually runs at
    # (logging_setup._resolve_level).
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    with capture_logs() as logs:
        plan_match(
            intent="which papers discuss this", library=library,
            cache=_FakeCache(hits=[(pid, 0.05)]), min_confidence=0.85,
            available_bindings=frozenset({"intent"}),
        )
    dropped = [e for e in logs
               if e.get("event") == "plan_match_unrunnable_binding_dropped"]
    assert dropped, "a candidate vanished from the pool with no log record"
    assert dropped[0]["detail"][0]["binding"] == "content_type"


def test_a_satisfiable_plan_still_wins_when_a_sibling_is_dropped(library) -> None:
    """The pool narrows to the runnable plan rather than emptying."""
    from nexus.plans.matcher import plan_match

    typed = _seed_with_bindings(
        library, query="search within a content type",
        required=["content_type"], name="type-scoped",
    )
    plain = _seed_with_bindings(
        library, query="search everything", required=["question"],
        name="plain",
    )
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    with capture_logs() as logs:
        matches = plan_match(
            intent="which papers discuss this", library=library,
            cache=_FakeCache(hits=[(typed, 0.03), (plain, 0.05)]),
            min_confidence=0.85, available_bindings=frozenset({"intent"}),
        )
    assert [m.plan_id for m in matches] == [plain], (
        "the better-scoring but unrunnable plan should be dropped and the "
        "runnable one returned, not an empty pool"
    )
    # Regression (nexus-eedj4, found in review): this is the COMMON case —
    # a runnable sibling wins — and it returns early, before the
    # fallthrough logging. The drop was invisible here while the
    # single-plan test above passed, so the pool shrank silently in
    # exactly the situation the record exists for.
    dropped = [e for e in logs
               if e.get("event") == "plan_match_unrunnable_binding_dropped"]
    assert dropped, (
        "a candidate was dropped on the winning-sibling path with no log "
        "record — the early return skipped the drain"
    )
    assert dropped[0]["detail"][0]["plan_id"] == typed
    # drained exactly once, not re-reported by a later return path
    assert len(dropped) == 1


def test_the_fts5_fallback_path_also_drops_unrunnable_plans(library) -> None:
    """No T1 cache: the keyword path must apply the same gate."""
    from nexus.plans.matcher import plan_match

    _seed_with_bindings(
        library, query="content type scoped search",
        required=["content_type"], name="type-scoped",
    )
    matches = plan_match(
        intent="content type scoped search", library=library, cache=None,
        available_bindings=frozenset({"intent"}),
    )
    assert matches == [], (
        "the FTS5 fallback offered a plan requiring content_type to a "
        "caller that cannot supply one"
    )


def test_the_fts5_fallback_still_offers_runnable_plans(library) -> None:
    """Non-vacuity for the FTS5 gate — the drop is the binding, not the path."""
    from nexus.plans.matcher import plan_match

    pid = _seed_with_bindings(
        library, query="content type scoped search",
        required=["question"], name="plain",
    )
    matches = plan_match(
        intent="content type scoped search", library=library, cache=None,
        available_bindings=frozenset({"intent"}),
    )
    assert [m.plan_id for m in matches] == [pid]


# ── nexus-czoeu: the two pre-existing counters share the same gap ──────────
#
# scope_conflict_drops and always_failing_drops were reported only on the
# fallthrough path, so like binding_drops before nexus-eedj4 they were
# invisible whenever a runnable candidate won. Both records also sat at
# DEBUG, which _resolve_level never emits in MCP (INFO) or CLI (WARNING)
# mode, so they were unobservable on the fallthrough path too.


def test_scope_conflict_drop_is_logged_when_a_sibling_wins(library) -> None:
    """RDR-091 I-4 added this record so operators could see scope degrading."""
    from nexus.plans.matcher import plan_match

    conflicting = _seed_with_bindings(
        library, query="scoped plan", required=["question"], name="scoped",
        scope_tags="code__other",
    )
    winner = _seed_with_bindings(
        library, query="matching plan", required=["question"], name="matching",
        scope_tags="rdr__nexus",
    )
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    with capture_logs() as logs:
        matches = plan_match(
            intent="scoped plan", library=library,
            cache=_FakeCache(hits=[(conflicting, 0.03), (winner, 0.05)]),
            min_confidence=0.85, scope_preference="rdr__nexus",
        )
    assert [m.plan_id for m in matches] == [winner], "fixture did not drop the scoped plan"
    assert any(
        e.get("event") == "plan_match_scope_conflict_fallthrough" for e in logs
    ), "scope conflict dropped a candidate on the winning-sibling path with no record"


def test_always_failing_drop_is_logged_when_a_sibling_wins(library) -> None:
    """nexus-vtp8h's skip must be visible when it is not the only candidate."""
    from nexus.plans.matcher import plan_match

    doomed = _seed_with_bindings(
        library, query="broken plan", required=["question"], name="broken",
    )
    for _ in range(3):
        library.increment_run_outcome(doomed, success=False)
    winner = _seed_with_bindings(
        library, query="working plan", required=["question"], name="working",
    )
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    with capture_logs() as logs:
        matches = plan_match(
            intent="broken plan", library=library,
            cache=_FakeCache(hits=[(doomed, 0.03), (winner, 0.05)]),
            min_confidence=0.85,
        )
    assert [m.plan_id for m in matches] == [winner], "fixture did not drop the failing plan"
    assert any(
        e.get("event") == "plan_match_always_failing_dropped" for e in logs
    ), "an always-failing plan was skipped on the winning-sibling path with no record"
