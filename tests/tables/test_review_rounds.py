# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-dv7gw: the review round contracts are ONE checked table.

Three bounds coexisted with nothing stating them together (deep review
T2 [24875] G3): code-review's one round plus one confirmation pass,
plan-audit's MAX_BLOCKING_ROUNDS, and rdr-gate's any-Critical rounds then
ship_blockers. Each constant now derives from
``src/nexus/tables/review-rounds.toml``, and the skills that state a number
are pinned to it (``tests/test_plugin_structure.py``).
"""
from __future__ import annotations

import pytest

from nexus.tables.review_rounds import (
    CONTRACTS,
    blocking_rounds,
    load_review_rounds,
    rule_for,
)


def test_table_loads_with_the_three_contracts() -> None:
    table = load_review_rounds()
    assert table.kind == "decision-table"
    assert set(table.dimensions["contract"].domain) == set(CONTRACTS) == {"code-review", "plan-audit", "rdr-gate"}
    assert list(table.dimensions["round"].domain) == ["1", "2", "3+"]


@pytest.mark.parametrize(
    ("contract", "round_no", "blocks_on", "next_by"),
    [
        ("rdr-gate", 1, "any-critical", "human"),
        ("rdr-gate", 2, "any-critical", "human"),
        ("rdr-gate", 3, "ship-blocker", "human"),
        ("rdr-gate", 11, "ship-blocker", "human"),
        ("plan-audit", 1, "blocks-planning", "orchestrator"),
        ("plan-audit", 2, "blocks-planning", "orchestrator"),
        ("plan-audit", 3, "nothing", "nobody"),
        ("code-review", 1, "ship-blocker", "orchestrator"),
        ("code-review", 2, "ship-blocker", "human"),
        ("code-review", 3, "ship-blocker", "human"),
    ],
)
def test_rule_for(contract: str, round_no: int, blocks_on: str, next_by: str) -> None:
    rule = rule_for(contract, round_no)
    assert (rule.blocks_on, rule.next_round_by) == (blocks_on, next_by)


def test_derived_constants_match_the_table() -> None:
    from nexus.commands.rdr import GATE_MAX_ANY_CRITICAL_ROUNDS
    from nexus.plans.audit_rounds import MAX_BLOCKING_ROUNDS

    assert MAX_BLOCKING_ROUNDS == blocking_rounds("plan-audit") == 2
    assert GATE_MAX_ANY_CRITICAL_ROUNDS == blocking_rounds("rdr-gate", "any-critical") == 2


def test_unknown_contract_or_round_is_refused() -> None:
    with pytest.raises(KeyError):
        rule_for("nope", 1)
    with pytest.raises(ValueError):
        rule_for("rdr-gate", 0)


def test_dynamic_dimensions_are_in_the_table_too() -> None:
    """Critique [24898] S1: budget_rounds tightening and the fix-introduced
    extension are contract terms, not prose."""
    assert rule_for("plan-audit", 1).budget_tightens and rule_for("plan-audit", 2).budget_tightens
    assert not rule_for("rdr-gate", 1).budget_tightens and not rule_for("code-review", 1).budget_tightens
    assert rule_for("code-review", 2).extension == "fix-introduced"
    assert all(rule_for(c, r).extension == "none" for c in ("plan-audit", "rdr-gate") for r in (1, 2, 3))
