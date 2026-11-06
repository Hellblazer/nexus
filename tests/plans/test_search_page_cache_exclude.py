# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 2 review r1 F8 (nexus-rfvyy.7.11): the ``search`` page-turn cache (120 s) is bypassed for an
``exclude`` read. The cache key carries the posture, so an exclude page never mixes with an include one, but a
page cached before a document was flagged (another process can set the flag) would still be served under
``exclude`` for the rest of the TTL. A flag set now takes effect on the next exclude search."""
from __future__ import annotations

import pytest

from nexus.mcp import core
from tests.plans.test_nx_answer_sensitivity import world  # noqa: F401 — world is a fixture

pytestmark = pytest.mark.sensitivity_reads

_ARGS = {"query": "gardens", "corpus": "knowledge", "limit": 10, "threshold": float("inf"), "structured": True}


def test_two_exclude_searches_both_reach_the_engine(world):
    core._search_render(**_ARGS, sensitivity="exclude")
    first = len(world.engine.route_calls())
    core._search_render(**_ARGS, sensitivity="exclude")

    assert first > 0, "the first search reached the engine: the test is not vacuous"
    assert len(world.engine.route_calls()) == 2 * first, "the second exclude search was served from the page cache"


def test_the_control_two_include_searches_still_share_a_page(world):
    core._search_render(**_ARGS, sensitivity="include")
    first = len(world.engine.route_calls())
    core._search_render(**_ARGS, sensitivity="include")

    assert first > 0
    assert len(world.engine.route_calls()) == first, "the page-turn cache still serves include reads"


def test_an_exclude_page_is_never_stored_for_a_later_include_or_exclude_read(world):
    core._search_render(**_ARGS, sensitivity="exclude")
    assert core._page_cache == {}, "an exclude page must not be held in the cache at all"
