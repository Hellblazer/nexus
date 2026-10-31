# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-232 (nexus-4xmpv, restored by nexus-jypmj): the inline planner's few-shot examples leave citation-traversal out
for a request that names exactly one source.

``tests/test_rdr232_eligibility.py`` pins ``eligible_matches`` with an explicit count. This pins the call site: that
``_nx_answer_plan_miss`` hands it ``RequestCoverage.names_one_source`` (1 or 0), so a rejected shape does not teach
the planner its structure. Tenant-free.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from nexus.mcp import core
from nexus.plans.match import Match
from nexus.plans.request_coverage import derive_request_coverage
from tests.test_rdr232_eligibility import CITATION_467, SYNTH_12


def _match(row: dict) -> Match:
    return Match(
        plan_id=row["id"], name=row["name"], description="d", confidence=0.5, dimensions=json.loads(row["dimensions"]),
        tags=row["tags"], plan_json=row["plan_json"], required_bindings=[], optional_bindings=[], default_bindings={},
        parent_dims=None,
    )


async def _few_shots(question: str) -> list[int]:
    seen: list[list[int]] = []

    def record(matches: Any) -> str:
        seen.append([m.plan_id for m in matches])
        return ""

    coverage = derive_request_coverage(question)
    with patch("nexus.mcp.core._get_collection_names", return_value=[]), \
         patch("nexus.mcp.core._format_plan_few_shot", side_effect=record), \
         patch("nexus.operators.dispatch.claude_dispatch", AsyncMock(side_effect=RuntimeError("planner stopped"))):
        with pytest.raises(Exception):  # noqa: B017, PT011 — the planner is stopped after the few-shots are chosen
            await core._nx_answer_plan_miss(
                question, few_shot_matches=[_match(CITATION_467), _match(SYNTH_12)], coverage=coverage,
                enforce_eligibility=True,
            )
    assert seen, "the few-shot block was built"
    return seen[0]


@pytest.mark.asyncio
async def test_the_planner_is_not_shown_citation_traversal_for_a_synthesis_request_that_names_one_source() -> None:
    assert derive_request_coverage("Synthesize RDR-084").narrows_to_one_source
    assert await _few_shots("Synthesize RDR-084") == [12]


@pytest.mark.asyncio
async def test_the_planner_is_shown_both_for_a_synthesis_request_that_names_two_sources() -> None:
    # the control: the same call, a different count
    assert not derive_request_coverage("Synthesize RDR-084 and RDR-085").names_one_source
    assert await _few_shots("Synthesize RDR-084 and RDR-085") == [467, 12]
