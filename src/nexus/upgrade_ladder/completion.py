# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-185 P0.2 → RDR-186 .12: completion facts + THE derived ladder position.

RETIREMENT NOTE (RDR-186 .12, the D6 census ratchet): the SQLite
``CompletionStore``/``ladder.db`` that originally lived here is DELETED —
completion facts are engine-side (``nexus.ladder_completions`` via
:class:`~nexus.upgrade_ladder.http_store.HttpLadderStore`), and the
pre-engine window is served entirely in-process
(:class:`~nexus.upgrade_ladder.holder.InProcessCompletionHolder`; a crash
before the end-of-walk flush costs one idempotent re-derivation, RF-186-2 —
completion records are position bookkeeping, never truth). Any ladder.db
file left on disk by an earlier install is an orphaned stray for the P4
zero sweep; nothing reads or writes it any more.

What LIVES here — deliberately, and pinned by
``tests/upgrade/test_gap4_two_mechanisms.py``:

- :func:`derive_ladder_position` — the SINGLE ladder-position algorithm
  (Gap-4 mechanism 1). Never stored, never settable; every completion
  surface derives through this one function.
- :class:`CompletionRecord` — the fact shape every ledger serves.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


def derive_ladder_position(verified: frozenset[str], order: Sequence[str]) -> int:
    """THE single ladder-position derivation (Gap-4 mechanism 1): the max
    contiguous prefix of *order* whose rungs are all in *verified*.

    Module-level so every completion surface (the engine-backed store, the
    in-process holder via the runner) derives through ONE algorithm — a
    second copy would be a competing data authority, which
    ``test_gap4_two_mechanisms.py`` pins against. Never stored, never
    settable; a hole in the prefix pins the position below it regardless
    of later-rung records (RDR-142).

    NOT a completeness signal: a permanently-N/A rung (detect-and-skip
    mode, e.g. substrate-etl on a PG-native install) never gets a
    completion record, so the position pins below it forever even when
    the walk is genuinely done. Use ``LadderRunReport.converged`` for
    "is the ladder converged"; never test ``position == len(order)`` and
    never display position as user-facing "N of M" progress.
    """
    position = 0
    for rung_name in order:
        if rung_name not in verified:
            break
        position += 1
    return position


@dataclass(frozen=True)
class CompletionRecord:
    """One durable verified fact for one rung."""

    rung_name: str
    verified_at: str
    package_version: str
    detail: str = ""
