# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end probes: the questions the category route exists to fix.

The unit suite next door pins the route's MECHANICS against a fake
cosine cache. This file pins its OUTCOME against the real bundled MiniLM
embedder and the real shipped templates, because the mechanics can all be
correct while the thing still fails to pick the right plan.

These are the five verb-shaped probes from the measurement that motivated
the design (T2 design-dimension-routed-category-plans-2026-08-21). Before
the route, three of the five scored below the 0.40 floor even with zero
competition, and debug-default died at RANK 1 — so every one of these was
a plan miss that paid the inline planner.

Hermetic: bundled ONNX MiniLM, no network, no service, no production
library. The fake library is built from the YAML on disk.

WHY THIS FILE MATTERS MORE THAN IT LOOKS. The design's load-bearing and
least-obvious claim is that cosine has no ABSOLUTE discriminating power
at this genericity but retains RELATIVE power inside a small same-verb
pool. That claim is what justifies ranking a pool whose scores are all
below the floor. It is falsifiable exactly here: if relative order were
noise, these probes would land on arbitrary members of their verb pool.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from nexus.plans.matcher import plan_match
from nexus.plans.verb_infer import infer_verb

_BUILTIN_DIR = Path(__file__).parent.parent / "conexus" / "plans" / "builtin"

def _nx_answer_bindings(question: str) -> frozenset[str]:
    """Exactly what nx_answer would declare for this question.

    Derived through nx_answer's own helper rather than hardcoded: it
    binds `intent` plus any TYPED value the question itself supplies
    (an author, a content type), so a hardcoded frozenset would quietly
    stop matching production the moment derivation improved — the same
    fidelity trap as stubbing plan_json to "{}".
    """
    from nexus.mcp.core import _nx_answer_caller_bindings

    _, available = _nx_answer_caller_bindings(
        question=question, scope="", bindings=None,
    )
    return available

#: (question, outcome-class). Each row records what the pipeline
#: MEASURABLY does, with nx_answer's real bindings in play. Written from
#: measurement, after two earlier versions of this table were written
#: from expectation and turned out to be wrong.
#:
#: - "routed": matched nothing before, reaches its template now. The
#:   route's actual win.
#: - "matched-correctly": cosine already reaches the right template. The
#:   review probe is here only after nexus-7y4v0: it matched
#:   review-default at 0.512 all along, but that plan passed
#:   `subtree: $changed_paths` — a tumbler filter — so nx_answer aliased
#:   raw question prose into it and the plan returned no evidence, live,
#:   reading as a confident answer. The template now takes free text, so
#:   the same match finally runs on real evidence.
#: The "blocked-upstream" class is GONE, and its departure is the point.
#: "Research how the plan matcher decides..." used to match the WRONG
#: plan above the floor — plan-inspect/default at 0.429, while the
#: intended research/default sat at 0.258, rank 5 — because the four
#: plan-meta templates absorbed any question containing the word "plan".
#: nexus-77cct retired them (they dispatched a `plan_match` MCP tool that
#: has never existed, so nothing ever invoked them successfully), and
#: this probe now reaches its template. Predicted before it was measured,
#: which is why the pin was written to FAIL when it started passing.
#: No probe is in a "declines" class any more. debug-default used to be
#: there — sole member of its verb pool and unrunnable, so debug traffic
#: got nothing — until nexus-7y4v0 took the `subtree:` scoping off it.
#: Three templates are still correctly unofferable to a bare question
#: (find-by-author needs an author, type-scoped-search a content_type,
#: traverse-then-generate seed tumblers), and that is right: each needs a
#: typed value no question carries.
_PROBES = [
    ("Review the changes on this branch for correctness",
     "default", "review", "matched-correctly"),
    ("Research how the plan matcher decides which plan wins",
     "default", "research", "routed"),
    ("Debug why the T1 scratch store returns nothing",
     "default", "debug", "routed"),
    ("Which papers discuss membership churn?",
     "document-discovery", "research", "routed"),
    ("Does the corpus have anything about tumblers at all?",
     "corpus-coverage-check", "research", "routed"),
]


def _templates() -> list[dict]:
    """Rows exactly as the seed loader would store them.

    plan_json goes through `desired_row_for_template` rather than being
    stubbed: required_bindings round-trip INSIDE plan_json, so a stubbed
    "{}" silently empties them and every binding gate becomes vacuous.
    An earlier version of this fixture did stub it, and hid the very
    defect these probes were written to catch (nexus-7y4v0).
    """
    from nexus.plans.seed_loader import desired_row_for_template

    rows = []
    for i, path in enumerate(sorted(_BUILTIN_DIR.glob("*.yml")), start=1):
        t = dict(yaml.safe_load(path.read_text()))
        dims = dict(t["dimensions"])
        dims["scope"] = "global"
        t["dimensions"] = dims
        desired = desired_row_for_template(t)
        rows.append({
            "id": i,
            "name": desired.name,
            "query": desired.query,
            "plan_json": desired.plan_json,
            "tags": desired.tags,
            "verb": desired.verb,
            "scope": "global",
            "dimensions": json.dumps(dims, sort_keys=True),
            "default_bindings": desired.default_bindings,
            "scope_tags": "",
            "project": "",
            "success_count": 1,
            "failure_count": 0,
            "_file": path.name,
        })
    return rows


class _RealEmbedCache:
    """Cosine cache over the real templates using the real embedder."""

    is_available = True

    def __init__(self, rows):
        from nexus.db.local_ef import LocalEmbeddingFunction
        from nexus.plans.match_text import _synthesize_match_text

        self._ef = LocalEmbeddingFunction()
        self._ids = [r["id"] for r in rows]
        texts = [
            _synthesize_match_text(
                description=r["query"], verb=r["verb"],
                name=r["name"], scope=r["scope"],
            )
            for r in rows
        ]
        self._vecs = self._ef(texts)

    @staticmethod
    def _cosine(a, b):
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        return dot / (na * nb) if na and nb else 0.0

    def query(self, intent, n):
        q = self._ef([intent])[0]
        scored = [
            (pid, 1.0 - self._cosine(q, v))
            for pid, v in zip(self._ids, self._vecs, strict=True)
        ]
        scored.sort(key=lambda x: x[1])
        return scored[:n]

    def remove(self, plan_id):  # pragma: no cover — nothing is deleted here
        pass


class _Library:
    def __init__(self, rows):
        self._rows = {r["id"]: r for r in rows}

    def get_plan(self, plan_id):
        return self._rows.get(plan_id)

    def increment_match_metrics(self, plan_id, confidence=None):
        pass

    def search_plans(self, intent, limit=10, project=""):  # noqa: ARG002
        return []


def _identify(rows, match) -> tuple[str, str]:
    """(name, verb) for *match*.

    Name alone is not an identity here: five templates are named
    "default" and only their verb tells them apart, which is exactly how
    the plan-inspect meta plan can masquerade as the research default in
    a failure message.
    """
    row = next(r for r in rows if r["id"] == match.plan_id)
    return (row["name"], row["verb"])


@pytest.fixture(scope="module")
def rows():
    return _templates()


@pytest.fixture(scope="module")
def cache(rows):
    return _RealEmbedCache(rows)


@pytest.mark.parametrize(
    ("question", "expected_name", "expected_verb", "outcome"),
    _PROBES,
    ids=[p[0][:40] for p in _PROBES],
)
def test_probe_outcome_with_the_route(
    rows, cache, question, expected_name, expected_verb, outcome,
):
    """What each probe does with the route on, by outcome class."""
    verb = infer_verb(question)
    assert verb is not None, f"{question!r} derived no verb — the route cannot fire"

    matches = plan_match(
        question, library=_Library(rows), cache=cache,
        category_verb=verb, n=5,
        available_bindings=_nx_answer_bindings(question),
    )

    if outcome == "declines":
        assert matches == [], (
            "every template in this verb pool is unrunnable, so the route "
            "must offer nothing rather than a plan whose evidence step "
            "cannot match. If this now returns a match, check the pool's "
            "bindings actually became satisfiable (nexus-7y4v0)."
        )
        return

    assert matches, f"{question!r} matches nothing"
    got = _identify(rows, matches[0])

    assert got == (expected_name, expected_verb), (
        f"{question!r} routed to {got}, expected "
        f"{(expected_name, expected_verb)} (derived verb {verb!r})"
    )


@pytest.mark.parametrize(
    ("question", "expected_name", "expected_verb", "outcome"),
    _PROBES,
    ids=[p[0][:40] for p in _PROBES],
)
def test_probe_baseline_without_the_route(
    rows, cache, question, expected_name, expected_verb, outcome,
):
    """The BEFORE picture the design rests on, pinned so it cannot drift.

    Three of these matched nothing at all — months of paying the inline
    planner for questions the library had a template for. A future change
    that makes the cosine path work on its own should surface here as a
    failure to investigate, not as silence.
    """
    baseline = plan_match(
        question, library=_Library(rows), cache=cache,
        available_bindings=_nx_answer_bindings(question),
    )

    if outcome == "matched-correctly":
        assert baseline
        assert _identify(rows, baseline[0]) == (expected_name, expected_verb)
    else:  # "routed" — the route's actual win
        assert baseline == [], (
            f"{question!r} now matches without the route — if the cosine "
            f"path improved, the route may no longer be needed for it"
        )
