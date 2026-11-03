# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A per-rung count of consecutive deferrals, kept across upgrade runs (nexus-0sm1t).

WHY. Since nexus-dfemn the ladder walk goes on past a DEFERRED rung, so the rungs
that do not depend on it are no longer starved. The deferring rung itself stays
pending, and under ``nx upgrade --auto`` nothing says so (``_run_ladder`` prints
deferrals only when interactive). A rung that defers on every run, a census that
grows with its table and times out each time, was never repaired and never named.
This log counts the consecutive runs each rung deferred, and ``nx doctor`` makes
its ``Upgrade ladder`` row lead with "STUCK:" (still a warning; the exit code does
not move) once a still-pending rung
has deferred :data:`ESCALATE_AFTER` runs in a row. The RDR-192 rung's own
starvation count (:mod:`nexus.upgrade_ladder.census_progress`) is the model, and
this is the generic form: any rung, whatever the reason it defers.

WHERE IT LIVES. One T2 memory note (project ``upgrade_ladder_state``, title
:data:`DEFERRAL_TITLE`), through the same :class:`T2NoteStore` the census progress
note uses: the engine's PG table, no SQLite, no local file.

WHAT A WALK DOES TO IT (:meth:`DeferralLog.record`, called once per real walk):

* DEFERRED: the rung's count goes up by one; its ``since`` stays at the first
  deferral of the streak; ``last`` and the detail are the latest;
* RECORDED, ALREADY_RECORDED, SKIPPED_NOT_APPLICABLE: the rung's entry is
  cleared, because the streak is over;
* NOT_ATTEMPTED, VERIFY_FAILED, FAILED: the entry is left as it was. The rung
  was not walked, or it failed loudly (which stops the upgrade and says so),
  and neither is evidence that a deferral streak ended.

The note is a T2 row any version or tool may have written. Only the documented
shape is read; an entry of any other shape is ignored and dropped by the next
write. Callers run every operation through :func:`~nexus.upgrade_ladder.census_progress.guarded`:
the log only ever raises a warning, it must never change an upgrade's outcome.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.upgrade_ladder.census_progress import (
    PROGRESS_PROJECT,
    STARVED_AFTER,
    NoteStore,
    T2NoteStore,
    _safe_text,
)

if TYPE_CHECKING:
    from nexus.upgrade_ladder.runner import LadderRunReport

#: The T2 note's title, beside the census progress note in ``PROGRESS_PROJECT``.
DEFERRAL_TITLE = "ladder-deferrals"

#: Consecutive deferrals at which the doctor row escalates (the same bar as the
#: RDR-192 rung's own STARVED mark).
ESCALATE_AFTER = STARVED_AFTER

#: Longest detail kept in the note (a rung's detail can carry an engine error body).
_DETAIL_STORE_MAX = 300


def t2_deferral_log() -> DeferralLog:
    """The production log: the T2 note named :data:`DEFERRAL_TITLE`."""
    return DeferralLog(T2NoteStore(DEFERRAL_TITLE, project=PROGRESS_PROJECT, tags="upgrade-ladder,deferrals"))


@dataclass(frozen=True)
class RungDeferral:
    """One rung's streak of consecutive deferred runs."""

    rung: str
    count: int
    since: str
    last: str
    detail: str

    @property
    def escalated(self) -> bool:
        return self.count >= ESCALATE_AFTER

    def describe(self) -> str:
        return (
            f"rung '{_safe_text(self.rung)}' deferred {self.count} consecutive run(s) since "
            f"{_safe_text(self.since)} (last {_safe_text(self.last)}: {_safe_text(self.detail, 200)}); "
            "run `nx upgrade` (interactively, not --auto) to see why it defers"
        )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _entry_ok(entry: object) -> bool:
    return (
        isinstance(entry, dict) and _is_int(entry.get("n")) and entry["n"] >= 1
        and isinstance(entry.get("since"), str) and isinstance(entry.get("last"), str)
        and isinstance(entry.get("detail"), str)
    )


class DeferralLog:
    """The per-rung deferral streaks, in one note. Each call loads the note,
    changes it and writes it back, so nothing is held between calls."""

    def __init__(
        self, store: NoteStore, *, now_fn: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._store = store
        self._now = now_fn

    def _entries(self) -> dict[str, dict[str, Any]]:
        """The well-formed entries of the stored note; anything else is dropped."""
        note = self._store.load()
        rungs = note.get("rungs") if isinstance(note, dict) else None
        if not isinstance(rungs, dict):
            return {}
        return {name: dict(e) for name, e in rungs.items() if isinstance(name, str) and _entry_ok(e)}

    def record(self, report: LadderRunReport) -> None:
        """Fold one finished walk into the streaks (see the module docstring)."""
        from nexus.upgrade_ladder.runner import RungOutcome  # noqa: PLC0415 — runner imports the registry; keep this module import-light

        clearing = {RungOutcome.RECORDED, RungOutcome.ALREADY_RECORDED, RungOutcome.SKIPPED_NOT_APPLICABLE}
        deferred = [run for run in report.runs if run.outcome is RungOutcome.DEFERRED]
        cleared = [run.name for run in report.runs if run.outcome in clearing]
        if not deferred and not cleared:
            return  # nothing this walk says about any streak (an empty ladder, or every rung not attempted or failed)
        entries = self._entries()
        before = {name: dict(e) for name, e in entries.items()}
        stamp = self._now().isoformat(timespec="seconds")
        for run in deferred:
            prior = entries.get(run.name)
            entries[run.name] = {
                "n": (prior["n"] if prior else 0) + 1,
                "since": prior["since"] if prior else stamp,
                "last": stamp,
                "detail": run.detail[:_DETAIL_STORE_MAX],
            }
        for name in cleared:
            entries.pop(name, None)
        if entries == before:
            return
        if entries:
            self._store.save({"rungs": entries})
        else:
            self._store.clear()

    def deferrals(self) -> dict[str, RungDeferral]:
        """Every rung with a streak, by name."""
        return {
            name: RungDeferral(name, e["n"], e["since"], e["last"], e["detail"])
            for name, e in self._entries().items()
        }

    def escalated(self) -> dict[str, RungDeferral]:
        """The rungs whose streak has reached :data:`ESCALATE_AFTER`."""
        return {name: d for name, d in self.deferrals().items() if d.escalated}
