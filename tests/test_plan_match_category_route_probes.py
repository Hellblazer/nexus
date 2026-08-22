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

#: What nx_answer can actually bind with no caller-supplied bindings.
#: Passing this is what makes the probes faithful — omitting it disables
#: the binding gate entirely and flatters the result.
_NX_ANSWER_BINDINGS = frozenset({"intent"})

#: (question, outcome-class). Each row records what the pipeline
#: MEASURABLY does, with nx_answer's real bindings in play. Written from
#: measurement, after two earlier versions of this table were written
#: from expectation and turned out to be wrong.
#:
#: - "routed": matched nothing before, reaches its template now. The
#:   route's actual win.
#: - "gate-fixed": matched a template before that could not run —
#:   review-default requires `changed_paths`, which it passes as a
#:   `subtree:` filter, so nx_answer aliased raw question prose into a
#:   tumbler filter and the plan returned no evidence at all. That was
#:   LIVE in production, independent of this route, and reads as a
#:   confident answer. The binding gate now drops it and the route sends
#:   the question to the other member of its verb class.
#: - "blocked-upstream": cosine returns a WRONG plan above the floor, so
#:   the route correctly declines to override it. The meta plans absorb
#:   any question containing "plan" (nexus-77cct: those four templates
#:   have no live invocation surface at all, so retiring them fixes this
#:   as a side effect).
#: - "declines": the verb derives, but every template in its pool is
#:   unrunnable, so nothing is offered and the caller falls through to
#:   the inline planner. debug-default is the sole debug template and it
#:   requires `failing_path` as a `subtree:` filter. Declining is the
#:   correct outcome — the alternative is running a plan whose evidence
#:   step cannot match anything — but it means the route does NOT help
#:   debug traffic until that template is fixed (nexus-7y4v0).
_PROBES = [
    ("Review the changes on this branch for correctness",
     "default", "analyze", "gate-fixed"),
    ("Research how the plan matcher decides which plan wins",
     "default", "research", "blocked-upstream"),
    ("Debug why the T1 scratch store returns nothing",
     "default", "debug", "declines"),
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
        category_verb=verb, n=5, available_bindings=_NX_ANSWER_BINDINGS,
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

    if outcome == "blocked-upstream":
        assert got != (expected_name, expected_verb), (
            "pinned as a KNOWN MISS: cosine returns a wrong plan above the "
            "floor and the route must not override it. If this now passes, "
            "the upstream cause was fixed — update this table and the "
            "design memo's known-limitation section."
        )
        return

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
        available_bindings=_NX_ANSWER_BINDINGS,
    )

    if outcome == "blocked-upstream":
        assert baseline
        assert _identify(rows, baseline[0]) != (expected_name, expected_verb)
    else:
        # "gate-fixed" included: review-default used to win this one and
        # run without evidence; the binding gate now drops it before the
        # route ever looks.
        assert baseline == [], (
            f"{question!r} now matches without the route — if the cosine "
            f"path improved, the route may no longer be needed for it"
        )
