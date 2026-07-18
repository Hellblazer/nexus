# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-185 P0.2 → RDR-186 .12: completion records + derived ladder position.

HISTORY NOTE (the .12 retirement): this file originally tested the SQLite
``CompletionStore``/``ladder.db``. That substrate is RETIRED — completion
facts live in the engine's ``nexus.ladder_completions`` table
(``HttpLadderStore``; durability semantics tested in the Java
``LadderHandlerTest``), and the pre-engine window is in-process only
(``InProcessCompletionHolder``; crash costs one idempotent re-derivation,
RF-186-2). What SURVIVES here is the substrate-independent contract:

- ``derive_ladder_position`` — THE single position derivation (Gap-4
  mechanism 1): max contiguous verified prefix, no stored position, no
  setter, made unrepresentable rather than merely guarded (RQ6/RDR-142).
- ``CompletionRecord`` — the fact shape every ledger serves.
"""
from __future__ import annotations

import pytest

from nexus.upgrade_ladder.completion import CompletionRecord, derive_ladder_position

ORDER = ("t2-schema", "substrate-etl", "third-rung")


# ── Position derivation (RQ6: max contiguous verified prefix) ────────────────


def test_empty_verified_set_position_is_zero() -> None:
    assert derive_ladder_position(frozenset(), ORDER) == 0


def test_full_prefix_position() -> None:
    assert derive_ladder_position(frozenset(ORDER), ORDER) == len(ORDER)


@pytest.mark.parametrize(
    ("verified", "expected"),
    [
        (set(), 0),
        ({"t2-schema"}, 1),
        ({"t2-schema", "substrate-etl"}, 2),
        ({"substrate-etl"}, 0),                    # gap: rung 1 missing
        ({"substrate-etl", "third-rung"}, 0),      # everything but the first
        ({"t2-schema", "third-rung"}, 1),          # hole at rung 2 pins at 1
    ],
)
def test_position_is_max_contiguous_verified_prefix(
    verified: set[str], expected: int
) -> None:
    assert derive_ladder_position(frozenset(verified), ORDER) == expected


def test_rows_outside_the_order_are_ignored() -> None:
    """Interim wrapped-verb rungs may record completions under names not in
    the canonical order — they never perturb the derived position."""
    assert derive_ladder_position(frozenset({"interim-wrapped-verb"}), ORDER) == 0
    assert (
        derive_ladder_position(frozenset({"interim-wrapped-verb", "t2-schema"}), ORDER)
        == 1
    )


def test_derivation_is_pure_no_write_surface() -> None:
    """The derivation is a pure function of (verified, order) — same inputs,
    same answer, nothing stored anywhere (the RDR-142 class stays
    unrepresentable; the AST pins live in test_gap4_two_mechanisms.py)."""
    inputs = frozenset({"t2-schema"})
    assert derive_ladder_position(inputs, ORDER) == derive_ladder_position(inputs, ORDER)


# ── The fact shape ───────────────────────────────────────────────────────────


def test_completion_record_is_frozen_and_position_free() -> None:
    record = CompletionRecord(
        rung_name="t2-schema", verified_at="t0", package_version="6.12.0", detail="ok"
    )
    with pytest.raises(Exception):
        record.rung_name = "other"  # type: ignore[misc] — frozen dataclass
    assert not any("position" in f for f in record.__dataclass_fields__), (
        "the fact shape must never grow a position field"
    )
