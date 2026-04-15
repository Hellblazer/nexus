# SPDX-License-Identifier: AGPL-3.0-or-later
"""SC-8 ART end-to-end demo — RDR-078 quality-lever assertion.

The "quality lever" claim for RDR-078 is that a typed-link traverse
step walks from an RDR seed to implementing code via catalog edges.
Until this test existed, SC-8 had no automated coverage — the claim
rode on a manual demo.

This test does not exercise real embeddings or network — it uses a
stub cache + an in-memory catalog. It pins the compositional path:

    plan_match(intent)  →  match with confidence ≥ threshold
    plan_run(match)     →  at least one traverse step executes
    traverse step       →  walks from RDR tumbler to implementing code
                           collection via the registered link_type(s)

Real-corpus calibration of ``min_confidence`` (PQ-2) is recorded in
T2 memory separately; this test pins the wiring.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture()
def library(tmp_path: Path):
    from nexus.db.migrations import _add_plan_dimensional_identity
    from nexus.db.t2.plan_library import PlanLibrary

    lib = PlanLibrary(tmp_path / "plans.db")
    _add_plan_dimensional_identity(lib.conn)
    lib.conn.commit()
    return lib


@pytest.fixture()
def catalog_with_rdr_to_code(tmp_path: Path):
    """A catalog with one RDR node linked to one code node via 'implements'.

    Returns ``(catalog, rdr_tumbler, code_tumbler, code_collection)``."""
    from nexus.catalog.catalog import Catalog

    cat_dir = tmp_path / "catalog"
    cat_dir.mkdir()
    cat = Catalog(catalog_dir=cat_dir, db_path=tmp_path / "cat.db")
    rdr_owner = cat.register_owner("nexus", "rdr")
    rdr = cat.register(rdr_owner, "RDR-078 seed")
    code_owner = cat.register_owner("nexus", "code")
    code_entry = cat.register(code_owner, "plan_runner.py")
    cat.link(rdr, code_entry, "implements", created_by="test")
    return cat, rdr, code_entry, "code__nexus"


class _StubCache:
    """Minimal in-session plan cache: exact-intent hit returns high confidence."""

    def __init__(self, hits: list[tuple[int, float]]) -> None:
        self._hits = hits
        self.is_available = True

    def query(self, intent: str, n: int) -> list[tuple[int, float]]:
        return list(self._hits[:n])


def test_sc8_warm_library_plan_match_clears_threshold_and_traverse_fires(
    library, catalog_with_rdr_to_code, monkeypatch,
) -> None:
    """SC-8 warm-library branch: plan_match returns a hit above
    min_confidence, plan_run dispatches a traverse step that walks from
    the RDR seed to the code collection via the registered link_type."""
    from nexus.plans.match import Match
    from nexus.plans.matcher import plan_match as _plan_match
    from nexus.plans.runner import plan_run
    from nexus.plans.schema import canonical_dimensions_json

    cat, rdr, code_entry, _ = catalog_with_rdr_to_code

    plan_json = {
        "steps": [
            {
                "tool": "traverse",
                "args": {
                    "seeds": [str(rdr)],
                    "link_types": ["implements"],
                    "depth": 1,
                    "direction": "out",
                },
            },
        ],
    }
    plan_id = library.save_plan(
        query="trace how RDR-078 plan_runner is implemented in code",
        plan_json=json.dumps(plan_json),
        tags="rdr-078,research",
        project="nexus",
        dimensions=canonical_dimensions_json(
            {"verb": "research", "scope": "global", "strategy": "default"},
        ),
        verb="research", scope="global", name="default",
    )

    # Warm cache: high cosine similarity → high confidence.
    cache = _StubCache([(plan_id, 0.05)])  # distance=0.05 → confidence=0.95

    matches = _plan_match(
        "trace how the plan_runner is implemented in code",
        library=library, cache=cache, min_confidence=0.85, n=3,
        project="nexus",
    )
    assert matches, "SC-8 warm-library plan_match must return a hit"
    m = matches[0]
    assert m.confidence is not None and m.confidence >= 0.85, (
        f"SC-8: confidence {m.confidence} must clear 0.85 threshold"
    )

    # Dispatcher stub: for the traverse tool, call Catalog.graph_many.
    traverse_calls: list[dict] = []

    def _dispatch(tool: str, args: dict):
        if tool == "traverse":
            traverse_calls.append(args)
            from nexus.catalog.tumbler import Tumbler
            seeds = [Tumbler.parse(s) for s in args["seeds"]]
            result = cat.graph_many(
                seeds=seeds,
                depth=args.get("depth", 1),
                link_types=args["link_types"],
                direction=args.get("direction", "both"),
            )
            return {
                "tumblers": [n["tumbler"] for n in result["nodes"]],
                "ids": [],
                "collections": sorted({
                    n.get("collection", "") for n in result["nodes"]
                    if n.get("collection")
                }),
            }
        return {"text": f"{tool}(stub)"}

    result = plan_run(m, {}, dispatcher=_dispatch)
    assert len(traverse_calls) == 1, (
        "SC-8: plan_run must dispatch at least one traverse step"
    )
    # Traversal output includes the code tumbler reached via 'implements'.
    step = result.steps[0]
    assert str(code_entry) in step["tumblers"], (
        f"SC-8: traverse must walk from RDR to code tumbler "
        f"(got {step['tumblers']!r})"
    )
