# SPDX-License-Identifier: AGPL-3.0-or-later
"""The review round contracts, read from ``review-rounds.toml`` (nexus-dv7gw).

One table for three bounds that were stated separately and drifted apart:
code-review's confirmation pass, plan-audit's blocking-round cap, and the
RDR gate's any-Critical rounds. The constants each consumer used to
hard-code (``nexus.plans.audit_rounds.MAX_BLOCKING_ROUNDS``,
``nexus.commands.rdr.GATE_MAX_ANY_CRITICAL_ROUNDS``) derive from here.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Final

from nexus.tables.load import Table, load_packaged_table

TABLE_RESOURCE: Final = "review-rounds.toml"
CONTRACTS: Final[tuple[str, ...]] = ("code-review", "plan-audit", "rdr-gate")
_OPEN_ROUND: Final = "3+"


@dataclass(frozen=True)
class RoundRule:
    contract: str
    round_label: str
    blocks_on: str
    next_round_by: str


@lru_cache(maxsize=1)
def load_review_rounds() -> Table:
    return load_packaged_table(TABLE_RESOURCE)


def _one(value: object) -> str | None:
    """A match/guard value, which the loader stores as a one-tuple."""
    if isinstance(value, tuple):
        return str(value[0]) if len(value) == 1 else None
    return None if value is None else str(value)


def _round_label(round_no: int) -> str:
    if round_no < 1:
        raise ValueError(f"round must be >= 1, got {round_no}")
    numbered = [d for d in load_review_rounds().dimensions["round"].domain if d.isdigit()]
    return str(round_no) if str(round_no) in numbered else _OPEN_ROUND


def rule_for(contract: str, round_no: int) -> RoundRule:
    """The rule that applies to *contract* in *round_no*."""
    if contract not in CONTRACTS:
        raise KeyError(contract)
    label = _round_label(round_no)
    for row in load_review_rounds().rows:
        if _one(row.match.get("contract")) == contract and _one(row.guard.get("round")) == label:
            emit = row.outcome if isinstance(row.outcome, dict) else {}
            return RoundRule(contract, label, str(emit["blocks_on"]), str(emit["next_round_by"]))
    raise KeyError((contract, label))


def blocking_rounds(contract: str, blocks_on: str | None = None) -> int:
    """Numbered rounds in which *contract* can block: all of them when
    *blocks_on* is None (anything but ``nothing``), else those whose rule
    blocks on exactly *blocks_on*."""
    if contract not in CONTRACTS:
        raise KeyError(contract)
    n = 0
    for label in load_review_rounds().dimensions["round"].domain:
        if not label.isdigit():
            continue
        rule = rule_for(contract, int(label))
        if (rule.blocks_on != "nothing") if blocks_on is None else (rule.blocks_on == blocks_on):
            n += 1
    return n
