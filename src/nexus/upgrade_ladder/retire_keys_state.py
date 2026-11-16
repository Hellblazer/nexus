# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""What the ``retire-keys-sweep`` rung last measured, kept as one T2 note, and the ``nx doctor`` row that reads it
(RDR-236 Phase 4 Step 3, nexus-6pfgb.43).

WHY A NOTE. The rung's dry run is an engine job whose cost grows with the store, so ``nx doctor`` cannot run it
(bounded reads). The rung writes what each dry run saw (the counts, what blocks the sweep, the full text of the deferral)
as one note per tenant, in the project and the store the census progress note uses. Two readers need it:

* the rung itself, to print a residue's FULL deferral text once and a one-line deferral on every later ``nx upgrade``
  that reads the same counts (a deferral that repeats a screenful each run is noise, and the full text is on the
  doctor row);
* the ``Retired chunk keys`` doctor row, which counts the rows that still carry a retired key as of the last
  measurement, or answers from the rung's completion record. Not applicable on a box the rung has never measured
  (a virgin box has no chunk rows to carry one, and ``nx upgrade`` is what measures an install).

THE NOTE IS A MEASUREMENT, NOT A RECORD. It says what the last run saw and when. It never makes a rung converged and
is never consulted for that (the completion ledger does); a note that cannot be read is no note, and nothing here may
fail an upgrade or a doctor run.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from nexus.upgrade_ladder.census_progress import NoteStore, T2NoteStore, guarded

_log = structlog.get_logger(__name__)

#: The note's title, beside the census progress and deferral notes.
STATE_TITLE = "retire-keys-sweep-state"
STATE_TAGS = "upgrade-ladder,rdr236"

#: What the last measurement found: rows carrying retired keys behind a standing precondition, rows the sweep cannot
#: take (a blocked collection, a wrong-width chash), none at all, or the sweep ran and the confirming read found none.
BLOCKED, RESIDUE, CLEAN, SWEPT = "blocked", "residue", "clean", "swept"


def default_state_store() -> NoteStore:
    return T2NoteStore(STATE_TITLE, tags=STATE_TAGS)


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _text(value: object, limit: int = 2000) -> str:
    return str(value)[:limit]


def doctor_row() -> Any:
    """The ``Retired chunk keys`` row: one T2 note and one ledger read, so ``nx doctor`` stays bounded. Never raises."""
    from nexus.health import HealthResult  # noqa: PLC0415 — deferred: health imports a great deal

    label = "Retired chunk keys"
    note = guarded("retire_keys_state_read", default_state_store().load, None)
    recorded = guarded("retire_keys_record_read", _recorded_at, None)
    if note is None and recorded is None:
        return HealthResult(
            label=label, ok=True, skipped=True,
            detail="not applicable: the retired-key sweep has not measured this install (nx upgrade runs it)",
        )
    when = _text(note.get("measured_at", "an unknown time"), 40) if note else None
    if note is not None and note.get("outcome") in (BLOCKED, RESIDUE):
        rows = note.get("retired_key_rows")
        detail = (
            f"{rows if isinstance(rows, int) else '?'} chunk row(s) still carry retired metadata keys as of {when} "
            f"(the last `nx upgrade` measurement, not a live count): {_text(note.get('detail', ''))}"
        )
        fixes = ["Run: nx upgrade (re-measures, and sweeps once nothing stands in the way)"]
        if note.get("outcome") == BLOCKED:
            fixes.append(
                f"Read the counts and decide each residue: nx t3 retire-keys --tenant {_text(note.get('tenant') or '<tenant>', 80)}"
            )
        return HealthResult(label=label, ok=False, warn=True, detail=detail, fix_suggestions=fixes)
    if recorded is not None:
        return HealthResult(
            label=label, ok=True,
            detail=(
                f"the retire-keys-sweep rung verified zero retired-key rows (recorded {recorded}); a record is not a "
                "live count, and an older client can write one back"
            ),
        )
    return HealthResult(label=label, ok=True, detail=f"no chunk row carried a retired key when last measured ({when})")


def _recorded_at() -> str | None:
    from nexus.upgrade_ladder.auto_guard import completion_recorded_at, default_completion_record  # noqa: PLC0415
    from nexus.upgrade_ladder.registry import RUNG_RETIRE_KEYS_SWEEP  # noqa: PLC0415

    return completion_recorded_at(lambda: default_completion_record(RUNG_RETIRE_KEYS_SWEEP), RUNG_RETIRE_KEYS_SWEEP)
