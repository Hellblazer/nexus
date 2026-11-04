# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-189 P2 (nexus-z1d0l.2.3): the family registry's vocabularies are checked tables.

Slot kinds, witness kinds, obligation states and the family lifecycle live in
``src/nexus/tables/target-family-*.toml``; the loader in
``nexus.plans.target_families`` reads its literals from them.
"""
from __future__ import annotations

import pytest

from nexus.tables import target_family_vocab as vocab
from nexus.tables.check import BLOCKING_CODES, check_table

TABLES = ("slot_kinds", "witness_kinds", "obligation_states", "lifecycle")


@pytest.mark.parametrize("name", TABLES)
def test_each_table_lints_clean_under_the_house_checker(name: str) -> None:
    table = getattr(vocab, f"load_{name}")()
    assert [f for f in check_table(table) if f.code in BLOCKING_CODES] == []


def test_vocabularies_are_the_decided_sets() -> None:
    assert vocab.SLOT_KINDS == ("text", "entity", "list", "claim", "reference")
    assert vocab.WITNESS_KINDS == ("item", "quote", "span")
    # RDR-189 P4's per-obligation states, verbatim
    assert vocab.OBLIGATION_STATES == (
        "pending", "preserved", "approximated", "weakened", "requiresReview", "unavailable", "failed",
    )


def test_a_missing_witness_never_reads_as_preserved() -> None:
    """RDR-189: no unsupported obligation may become ``preserved``."""
    assert set(vocab.MISSING_WITNESS_STATES) == {"weakened", "requiresReview", "unavailable", "failed"}
    assert "preserved" not in vocab.MISSING_WITNESS_STATES


@pytest.mark.parametrize(
    ("status", "event", "approval", "outcome"),
    [
        ("candidate", "activate", "recorded", ("to", "active")),
        ("candidate", "activate", "absent", ("refuse", "approval-not-recorded")),
        ("active", "activate", "recorded", ("refuse", "illegal-transition")),
        ("retired", "activate", "recorded", ("refuse", "illegal-transition")),
        ("active", "retire", "recorded", ("to", "retired")),
        ("candidate", "retire", "recorded", ("refuse", "illegal-transition")),
        ("retired", "retire", "absent", ("refuse", "illegal-transition")),
    ],
)
def test_lifecycle_transitions(status: str, event: str, approval: str, outcome: tuple[str, str]) -> None:
    assert vocab.transition(status, event, approval) == outcome
