# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Plan 10 (``hybrid-factual-lookup``) asks its search step for ``lexical=true``.

Measured 2026-11-19 (T2 ``nexus/plan10-lexical-measure-2026-11-19``): on 50 HERB
lookup questions gold-company reach at 20 went from 0.795 to 0.968 (paired
+0.173, 95% CI [+0.123, +0.227], 27 of 50 questions up, none down). The gain is
the cross-encoder rerank, which MCP ``search`` applies only when
``lexical=True``; the lexical leg itself returned 0 rows on every question.

No substrate: a YAML parse, a schema validation and two capability checks.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import yaml

from nexus.db.http_vector_client import HttpVectorClient
from nexus.mcp.core import search
from nexus.plans.schema import validate_plan_template

_PLAN = (
    Path(__file__).resolve().parents[1]
    / "conexus" / "plans" / "builtin" / "hybrid-factual-lookup.yml"
)


def _search_step() -> dict:
    template = yaml.safe_load(_PLAN.read_text())
    steps = template["plan_json"]["steps"]
    first = steps[0]
    assert first["tool"] == "search", "step 1 must stay the search step ($step1.* bindings depend on it)"
    return first


def test_search_step_carries_lexical_true() -> None:
    assert _search_step()["args"].get("lexical") is True


def test_search_step_keeps_its_other_arguments() -> None:
    args = _search_step()["args"]
    assert args["query"] == "$question"
    assert args["limit"] == "$limit"
    assert args["corpus"] == "$corpus"
    assert args["threshold"] == 2.0


def test_template_still_validates() -> None:
    validate_plan_template(yaml.safe_load(_PLAN.read_text()))


def test_search_tool_accepts_the_lexical_argument() -> None:
    """The runner dispatches the step's args as keywords; ``search`` must name ``lexical``."""
    assert "lexical" in inspect.signature(search).parameters


def test_the_service_backend_serves_the_lexical_leg() -> None:
    """``LexicalLegUnavailableError`` is raised only for a backend without these markers.

    ``make_t3()`` returns ``HttpVectorClient`` for every production install
    (RDR-155 P4a.2); ``T3Database`` (no markers) exists only behind an injected
    test client. So a ``lexical=true`` step cannot be refused for the backend.
    """
    assert HttpVectorClient.supports_hybrid_search is True
    assert HttpVectorClient.supports_server_rerank is True
