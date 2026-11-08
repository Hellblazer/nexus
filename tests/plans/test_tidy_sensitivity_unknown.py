# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 2 review r1 F6 (nexus-rfvyy.7.11): when the catalog cannot say whether the matched chunks are
sensitive, ``nx_tidy`` says the sensitivity check was unavailable and nothing was consolidated. It does not call
that a genuine miss, and it does not claim the entries were "withheld as sensitive": no entry was found sensitive."""
from __future__ import annotations

import pytest

from nexus.mcp import core
from tests.plans.test_nx_answer_sensitivity import CANARY, world  # noqa: F401 — world is a fixture

pytestmark = pytest.mark.sensitivity_reads


def test_an_unreadable_catalog_is_not_a_genuine_miss_and_not_withheld_as_sensitive(world):
    world.catalog.unreadable = True

    block, n, reason, genuine, withheld = core._tidy_prefetch("gardens", "knowledge", sensitivity="exclude")

    assert (block, n) == ("", 0)
    assert genuine is False, "an outage is a retrieval failure, never 'nothing matched'"
    assert "withheld as sensitive" not in reason
    assert "sensitivity check was unavailable" in reason and "nothing was consolidated" in reason
    assert withheld == 0, "no entry was found sensitive: the count must not claim otherwise"


@pytest.mark.asyncio
async def test_nx_tidy_reports_the_failed_check_to_the_model_and_the_caller(world):
    world.catalog.unreadable = True

    out = await core.nx_tidy(topic="gardens", collection="knowledge")

    assert CANARY not in out
    assert "RETRIEVAL FAILURE" in out and "sensitivity check was unavailable" in out
    assert not [p for p in world.prompts if "nothing to consolidate" in p.split("Do NOT say")[0]], (
        "the prompt must not tell the model there is nothing to consolidate"
    )
    assert world.prompts and "UNKNOWN" in world.prompts[0]
