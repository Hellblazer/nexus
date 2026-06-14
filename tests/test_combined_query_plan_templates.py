# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-156 P4 (nexus-zo0zt): consumer adoption of the combined-query primitives.

The ``find-by-author`` and ``type-scoped-search`` builtin plan templates are
repointed from the ``query``-tool catalog dance (+ ``store_get_many`` hydration)
onto ``search_metadata_scoped`` → ``summarize($step1.contents)``. These tests pin
the repointed step shape AND prove it runs end-to-end through ``plan_run``: the
new primitive is actually driven, and the inline ``contents`` flow to
``summarize`` without the chash-keyed ``store_get_many`` hydration.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

_BUILTIN = Path(__file__).parent.parent / "conexus" / "plans" / "builtin"
_AUTHOR = _BUILTIN / "find-by-author.yml"
_TYPE = _BUILTIN / "type-scoped-search.yml"


def _template(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _match(template: dict):
    from nexus.plans.match import Match

    pj = template["plan_json"]
    return Match(
        plan_id=1, name=template["name"], description=template["description"],
        confidence=0.9, dimensions=template["dimensions"], tags=template.get("tags", ""),
        plan_json=json.dumps(pj),
        required_bindings=list(template.get("required_bindings", []) or []),
        optional_bindings=list(template.get("optional_bindings", []) or []),
        default_bindings=dict(template.get("default_bindings", {}) or {}),
        parent_dims=None,
    )


class _FakeDispatcher:
    def __init__(self, outputs: list[dict]) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._outputs = list(outputs)

    async def __call__(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, args))
        return self._outputs.pop(0) if self._outputs else {"text": f"{tool}(stub)"}


# ── structural: step shape repointed onto the combined primitive ──────────────


class TestRepointedShape:
    def test_find_by_author_routes_through_metadata_scoped(self):
        steps = _template(_AUTHOR)["plan_json"]["steps"]
        assert [s["tool"] for s in steps] == ["search_metadata_scoped", "summarize"]
        assert steps[0]["args"]["author"] == "$author"
        assert steps[0]["args"]["corpus"] == "all"
        # contents flow directly — NO store_get_many (chash-keyed, would miss tumblers)
        assert steps[1]["args"]["inputs"] == "$step1.contents"
        assert all(s["tool"] != "store_get_many" for s in steps)

    def test_type_scoped_routes_through_metadata_scoped(self):
        steps = _template(_TYPE)["plan_json"]["steps"]
        assert [s["tool"] for s in steps] == ["search_metadata_scoped", "summarize"]
        assert steps[0]["args"]["content_type"] == "$content_type"
        assert steps[1]["args"]["inputs"] == "$step1.contents"
        assert all(s["tool"] != "store_get_many" for s in steps)

    def test_dimensions_unchanged(self):
        # Repoint must not move the routing target — plan_match still selects these.
        assert _template(_AUTHOR)["dimensions"]["strategy"] == "find-by-author"
        assert _template(_TYPE)["dimensions"]["strategy"] == "type-scoped"


# ── end-to-end: the primitive is actually driven, contents reach summarize ────


class TestRepointedExecution:
    @pytest.mark.asyncio
    async def test_find_by_author_drives_primitive_and_flows_contents(self):
        from nexus.plans.runner import plan_run

        step1_out = {
            "ids": ["1.2.3", "1.2.9"],
            "tumblers": ["1.2.3", "1.2.9"],
            "distances": [0.1, 0.4],
            "collections": ["knowledge__x__m__v1", "knowledge__x__m__v1"],
            "contents": ["Ada on analytical engines", "Ada on Bernoulli numbers"],
        }
        disp = _FakeDispatcher([step1_out, {"text": "summary"}])

        await plan_run(_match(_template(_AUTHOR)), {"author": "Ada Lovelace"},
                       dispatcher=disp, bundle_operators=False)

        assert [t for t, _ in disp.calls] == ["search_metadata_scoped", "summarize"]
        meta_args = disp.calls[0][1]
        assert meta_args["author"] == "Ada Lovelace"          # binding resolved
        assert meta_args["query"] == "Ada Lovelace"           # whole-token $author resolved
        assert meta_args["corpus"] == "all"
        # (structured=True is injected by the default dispatcher for _RETRIEVAL_TOOLS;
        #  registration is asserted in test_combined_query_mcp_tools.)
        # contents flow into summarize WITHOUT store_get_many hydration
        assert disp.calls[1][0] == "summarize"
        assert disp.calls[1][1]["inputs"] == step1_out["contents"]

    @pytest.mark.asyncio
    async def test_type_scoped_binds_content_type(self):
        from nexus.plans.runner import plan_run

        step1_out = {"ids": ["1.1.1"], "tumblers": ["1.1.1"], "distances": [0.2],
                     "collections": ["code__x__m__v1"], "contents": ["def f(): ..."]}
        disp = _FakeDispatcher([step1_out, {"text": "summary"}])

        await plan_run(_match(_template(_TYPE)),
                       {"question": "retry logic", "content_type": "code"},
                       dispatcher=disp, bundle_operators=False)

        meta_args = disp.calls[0][1]
        assert meta_args["content_type"] == "code"
        assert meta_args["query"] == "retry logic"
        assert disp.calls[1][1]["inputs"] == step1_out["contents"]
