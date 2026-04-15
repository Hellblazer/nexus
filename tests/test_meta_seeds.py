# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for the four RDR-078 meta-seed plans (nexus-05i.7 / P4d).

SC-13 ships **four** meta-seeds (RDR text says three — the bead is
correct, RDR text is an undercount):

  * ``verb:plan-author,  scope:global, strategy:default``
  * ``verb:plan-promote, scope:global, strategy:propose``
  * ``verb:plan-inspect, scope:global, strategy:default``
  * ``verb:plan-inspect, scope:global, strategy:dimensions``
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml


SEEDS_DIR = (
    Path(__file__).resolve().parents[1] / "nx" / "plans" / "builtin"
)


META_SEEDS: tuple[tuple[str, str, str], ...] = (
    ("plan-author-default.yml",       "plan-author",  "default"),
    ("plan-promote-propose.yml",      "plan-promote", "propose"),
    ("plan-inspect-default.yml",      "plan-inspect", "default"),
    ("plan-inspect-dimensions.yml",   "plan-inspect", "dimensions"),
)


def _load(name: str) -> dict:
    return yaml.safe_load((SEEDS_DIR / name).read_text())


@pytest.fixture()
def library(tmp_path: Path):
    from nexus.db.t2.plan_library import PlanLibrary
    return PlanLibrary(tmp_path / "plans.db")


# ── File presence + schema validity ────────────────────────────────────────


def test_all_four_meta_seeds_present() -> None:
    for filename, _, _ in META_SEEDS:
        path = SEEDS_DIR / filename
        assert path.exists(), f"missing meta-seed: {path}"


def test_all_four_meta_seeds_validate_as_templates() -> None:
    from nexus.plans.schema import validate_plan_template

    for filename, _, _ in META_SEEDS:
        validate_plan_template(_load(filename))


def test_meta_seed_dimensions_match_named_tuple() -> None:
    for filename, expected_verb, expected_strategy in META_SEEDS:
        tmpl = _load(filename)
        assert tmpl["dimensions"]["verb"] == expected_verb
        assert tmpl["dimensions"]["scope"] == "global"
        assert tmpl["dimensions"]["strategy"] == expected_strategy


# ── SC-13: idempotent loader + count = 4 ──────────────────────────────────


def test_all_four_meta_seeds_load_idempotent(library) -> None:
    """Re-setup is a no-op; exactly four meta-seed rows exist after load."""
    from nexus.plans.seed_loader import load_seed_directory

    first = load_seed_directory(SEEDS_DIR, library=library)
    # Directory holds the five scenario seeds from P4b + four meta-seeds.
    assert len(first.inserted) == 9, f"unexpected: {first.inserted}"

    meta_rows = [
        r for r in library.list_active_plans(outcome="success")
        if r["verb"] in ("plan-author", "plan-promote", "plan-inspect")
    ]
    assert len(meta_rows) == 4, (
        f"SC-13 count = 4; got {len(meta_rows)}: "
        f"{[r['verb'] + '/' + (r.get('name') or '') for r in meta_rows]}"
    )

    # Second pass — no new inserts.
    second = load_seed_directory(SEEDS_DIR, library=library)
    assert second.inserted == []
    assert len(second.skipped_existing) == 9


def test_meta_seed_identities_collide_only_under_same_dimensions(
    library,
) -> None:
    """plan-inspect/default and plan-inspect/dimensions share verb+scope
    but differ on strategy — their canonical identities must differ so
    they coexist."""
    from nexus.plans.seed_loader import load_seed_directory

    load_seed_directory(SEEDS_DIR, library=library)

    rows = [
        r for r in library.list_active_plans(outcome="success")
        if r["verb"] == "plan-inspect"
    ]
    dims = {r["dimensions"] for r in rows if r["dimensions"]}
    assert len(dims) == 2, (
        f"plan-inspect should have 2 distinct dimension identities; "
        f"got {dims}"
    )


# ── Description quality ────────────────────────────────────────────────────


def test_meta_seed_descriptions_are_discriminative() -> None:
    for filename, expected_verb, expected_strategy in META_SEEDS:
        tmpl = _load(filename)
        description = tmpl["description"]
        assert len(description) >= 80, (
            f"{filename} description too short: {len(description)} chars"
        )
        # Verb surface keywords — each seed's description mentions
        # words that'd anchor a plan_match query.
        anchors = {
            "plan-author":  ("author", "writing", "draft"),
            "plan-promote": ("promote", "promotion", "candidate"),
            "plan-inspect": ("inspect", "metrics", "enumerate", "dimension"),
        }
        found = any(
            a.lower() in description.lower() for a in anchors[expected_verb]
        )
        assert found, (
            f"{filename} description lacks verb anchor; "
            f"expected one of {anchors[expected_verb]}"
        )


# ── Execution invariants (audit finding O-6) ───────────────────────────────


def test_plan_promote_propose_dag_runs(library) -> None:
    """Execute plan-promote/propose against an empty metrics library;
    runner must complete without error and return a ``generate`` step
    result — lifecycle-ops deferral means no promote-record is written."""
    from nexus.plans.match import Match
    from nexus.plans.runner import plan_run
    from nexus.plans.seed_loader import load_seed_directory

    load_seed_directory(SEEDS_DIR, library=library)
    row = next(
        r for r in library.list_active_plans(outcome="success")
        if r["verb"] == "plan-promote"
    )
    match = Match.from_plan_row(row)

    def dispatcher(tool: str, args: dict) -> dict:
        if tool == "plan_search":
            return {"ids": [], "tumblers": [], "text": "no plans"}
        if tool == "rank":
            return {"ids": [], "text": "empty ranking"}
        if tool == "generate":
            return {"text": "promotion-shortlist: (empty)", "citations": []}
        raise AssertionError(f"unexpected tool: {tool}")

    result = plan_run(
        match, {"threshold": 5, "limit": 20}, dispatcher=dispatcher,
    )
    assert result.final is not None
    assert "promotion-shortlist" in result.final["text"]


def test_plan_inspect_dimensions_executes(library) -> None:
    """Pins SC-13's dimensions variant execution invariant (O-6).

    The seed must run to completion under a realistic dispatcher and
    the final step output must carry the dimensions + usage payload
    that the seed's authoring contract promises.
    """
    from nexus.plans.match import Match
    from nexus.plans.runner import plan_run
    from nexus.plans.seed_loader import load_seed_directory

    load_seed_directory(SEEDS_DIR, library=library)
    row = next(
        r for r in library.list_active_plans(outcome="success")
        if r["verb"] == "plan-inspect" and r.get("name") == "dimensions"
    )
    assert row is not None, "plan-inspect/dimensions seed not loaded"

    match = Match.from_plan_row(row)

    def dispatcher(tool: str, args: dict) -> dict:
        if tool == "plan_search":
            return {
                "ids": ["p1", "p2", "p3"], "tumblers": [],
                "text": "three plans in library",
            }
        if tool == "extract":
            # Stand-in for the real dimension-usage extractor.
            return {
                "text": "verb=3 scope=3 strategy=2 domain=1",
                "dimensions": {
                    "verb": 3, "scope": 3, "strategy": 2, "domain": 1,
                },
                "usage": [
                    {"dimension": "verb", "count": 3},
                    {"dimension": "scope", "count": 3},
                    {"dimension": "strategy", "count": 2},
                    {"dimension": "domain", "count": 1},
                ],
                "citations": [],
            }
        if tool == "summarize":
            return {
                "text": "Dimensions report: 4 registered, 4 in use.",
                "citations": [],
            }
        raise AssertionError(f"unexpected tool: {tool}")

    result = plan_run(match, {}, dispatcher=dispatcher)
    assert result.final is not None
    assert "Dimensions report" in result.final["text"]

    # Pin the intermediate dimension-usage step — SC-13 contract.
    extract_step = result.steps[1]
    assert "dimensions" in extract_step
    assert len(extract_step["dimensions"]) >= 1
    assert "usage" in extract_step
    assert extract_step["usage"]
