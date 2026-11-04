# SPDX-License-Identifier: AGPL-3.0-or-later
"""The answer-object family vocabularies, read from ``target-family-*.toml`` (RDR-189 P2, nexus-z1d0l.2.3).

Four checked tables: the kinds of value a required slot holds, the shapes of
witness a family may require, the per-obligation states of RDR-189 P4, and the
candidate/active/retired lifecycle of a declaration. ``nexus.plans.target_families``
validates every literal in a declaration against these, so a vocabulary change
is a reviewed edit to a table, never a free string.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Final

from nexus.tables.load import Table, load_packaged_table
from nexus.tables.resolve import resolve


@lru_cache(maxsize=1)
def load_slot_kinds() -> Table:
    return load_packaged_table("target-family-slot-kinds.toml")


@lru_cache(maxsize=1)
def load_witness_kinds() -> Table:
    return load_packaged_table("target-family-witness-kinds.toml")


@lru_cache(maxsize=1)
def load_obligation_states() -> Table:
    return load_packaged_table("target-family-obligation-states.toml")


@lru_cache(maxsize=1)
def load_lifecycle() -> Table:
    return load_packaged_table("target-family-lifecycle.toml")


#: Slot kinds, in table order.
SLOT_KINDS: Final[tuple[str, ...]] = load_slot_kinds().dimensions["kind"].domain
#: Witness kinds, in table order.
WITNESS_KINDS: Final[tuple[str, ...]] = load_witness_kinds().dimensions["kind"].domain
#: RDR-189 P4's obligation states, in the RDR's order.
OBLIGATION_STATES: Final[tuple[str, ...]] = load_obligation_states().dimensions["state"].domain
#: Declaration statuses.
STATUSES: Final[tuple[str, ...]] = load_lifecycle().dimensions["status"].domain


def _missing_witness_states() -> tuple[str, ...]:
    out: list[str] = []
    for row in load_obligation_states().rows:
        (state,) = row.guard["state"]
        if isinstance(row.outcome, dict) and row.outcome.get("on_missing_witness") == "allowed":
            out.append(state)
    return tuple(out)


#: The states a witness obligation's ``on_missing`` may name (never ``preserved``).
MISSING_WITNESS_STATES: Final[tuple[str, ...]] = _missing_witness_states()


def transition(status: str, event: str, approval: str) -> tuple[str, str]:
    """Resolve a lifecycle step: ``("to", <new status>)`` or ``("refuse", <reason>)``.

    Raises ``ValueError`` when the assignment is outside the table's domains.
    """
    result = resolve(load_lifecycle(), {"status": status, "event": event, "approval": approval})
    if result.row is None:
        raise ValueError(f"lifecycle: {result.refusal} for {(status, event, approval)!r}: {dict(result.detail)}")
    row = result.row
    if row.outcome_kind == "to":
        assert isinstance(row.outcome, dict)
        return ("to", str(row.outcome["status"]))
    return ("refuse", str(row.outcome))
