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

from pathlib import Path

import pytest
import yaml

from nexus.plans.matcher import plan_match
from nexus.plans.verb_infer import infer_verb

_BUILTIN_DIR = Path(__file__).parent.parent / "conexus" / "plans" / "builtin"

#: (question, expected plan name, what the cosine path did BEFORE the
#: route). Expectations are the template each question is FOR, decided by
#: reading the templates — not by running the code and writing down what
#: came out.
#:
#: The `before` column is measured, and it is the honest picture rather
#: than the flattering one. The route fixes the two questions that
#: previously matched NOTHING. It does not fix "Research how the plan
#: matcher decides..." — that one already matches the WRONG plan above
#: the floor (plan-inspect/default at 0.429, while the correct
#: research/default sits at 0.258, rank 5), and the route deliberately
#: never fires when cosine scored something. That is the memo's stated
#: known limitation, recorded here against a concrete case so it cannot
#: quietly become folklore that the route fixed everything.
#:
#: The cause of that miss is the meta plans (plan-author, plan-inspect,
#: plan-promote) absorbing any question containing the word "plan" —
#: and those four templates have no live invocation surface at all,
#: because plan_match is not an MCP tool (nexus-77cct). Retiring them
#: would fix this case as a side effect.
_PROBES = [
    ("Review the changes on this branch for correctness",
     "default", "review", "matched-correctly"),
    ("Research how the plan matcher decides which plan wins",
     "default", "research", "matched-wrong"),
    ("Debug why the T1 scratch store returns nothing",
     "default", "debug", "matched-nothing"),
    ("Which papers discuss membership churn?",
     "document-discovery", "research", "matched-nothing"),
    ("Does the corpus have anything about tumblers at all?",
     "corpus-coverage-check", "research", "matched-nothing"),
]


def _templates() -> list[dict]:
    rows = []
    for i, path in enumerate(sorted(_BUILTIN_DIR.glob("*.yml")), start=1):
        t = yaml.safe_load(path.read_text())
        dims = dict(t["dimensions"])
        dims["scope"] = "global"
        rows.append({
            "id": i,
            "name": t.get("name"),
            "query": t["description"],
            "plan_json": "{}",
            "tags": t.get("tags", ""),
            "verb": dims.get("verb"),
            "scope": "global",
            "dimensions": __import__("json").dumps(dims, sort_keys=True),
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
    ("question", "expected_name", "expected_verb", "before"),
    _PROBES,
    ids=[p[0][:40] for p in _PROBES],
)
def test_probe_reaches_its_intended_template(
    rows, cache, question, expected_name, expected_verb, before,
):
    """With the route on, each probe reaches the template written for it.

    The one exception is recorded as data rather than skipped: the
    "matched-wrong" probe is blocked upstream by a cosine hit the route is
    designed never to override, and asserting that explicitly is what
    keeps the limitation visible.
    """
    verb = infer_verb(question)
    assert verb is not None, f"{question!r} derived no verb — the route cannot fire"

    matches = plan_match(
        question, library=_Library(rows), cache=cache,
        category_verb=verb, n=5,
    )
    assert matches, f"{question!r} still matches nothing"
    got = _identify(rows, matches[0])

    if before == "matched-wrong":
        assert got != (expected_name, expected_verb), (
            "this probe is pinned as a KNOWN MISS: cosine returns a wrong "
            "plan above the floor and the route must not override it. If "
            "this now passes, the upstream cause was fixed — update the "
            "probe table and the design memo's known-limitation section."
        )
        return

    assert got == (expected_name, expected_verb), (
        f"{question!r} routed to {got}, expected "
        f"{(expected_name, expected_verb)} (derived verb {verb!r})"
    )


@pytest.mark.parametrize(
    ("question", "expected_name", "expected_verb", "before"),
    _PROBES,
    ids=[p[0][:40] for p in _PROBES],
)
def test_probe_baseline_without_the_route_is_what_the_memo_claims(
    rows, cache, question, expected_name, expected_verb, before,
):
    """Pin the BEFORE picture, because the design rests on it.

    Three of these matched nothing at all — four months of paying the
    inline planner for questions the library had a template for. One
    matched correctly already. One matched the wrong plan. A future change
    that makes the cosine path work on its own should surface here as a
    failure to investigate, not as silence.
    """
    baseline = plan_match(question, library=_Library(rows), cache=cache)

    if before == "matched-nothing":
        assert baseline == [], (
            f"{question!r} now matches without the route — if the cosine "
            f"path improved, the route may no longer be needed for it"
        )
    elif before == "matched-correctly":
        assert baseline
        assert _identify(rows, baseline[0]) == (expected_name, expected_verb)
    else:  # matched-wrong
        assert baseline
        assert _identify(rows, baseline[0]) != (expected_name, expected_verb)
