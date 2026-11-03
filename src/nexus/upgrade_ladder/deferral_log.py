# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A per-rung count of consecutive deferrals, kept across upgrade runs (nexus-0sm1t).

WHY. Since nexus-dfemn the ladder walk goes on past a DEFERRED rung, so the rungs
that do not depend on it are no longer starved. The deferring rung itself stays
pending, and under ``nx upgrade --auto`` nothing says so (``_run_ladder`` prints
deferrals only when interactive). A rung that defers on every run, a census that
grows with its table and times out each time, was never repaired and never named.
This log counts the consecutive runs each rung deferred, and ``nx doctor`` makes
its ``Upgrade ladder`` row lead with "REPEATEDLY DEFERRED:" (still a warning; the
exit code does not move) once a still-pending rung has deferred
:data:`ESCALATE_AFTER` runs in a row over at least :data:`ESCALATE_MIN_SPAN`. The
label says what is known: a rung that is slow but converging also defers each run
(the 900 s ``--auto`` budget cuts it), so the log cannot tell stuck from slow, and
the row sends the operator to the interactive run and the ``--auto`` child's log.
The RDR-192 rung's own starvation count (:mod:`nexus.upgrade_ladder.census_progress`)
is the model, and this is the generic form: any rung, whatever the reason it defers.

WHERE IT LIVES. One T2 memory note per tenant (project ``upgrade_ladder_state``,
title :data:`DEFERRAL_TITLE`), through the same :class:`T2NoteStore` the census
progress note uses: the engine's PG table, no SQLite, no local file. There is no
local flag or cache: the note is per tenant and a machine-local marker would
disagree with it as soon as two machines share a tenant.

WHAT A WALK DOES TO IT (:meth:`DeferralLog.record`, once per real walk). A walk
with no COUNTED deferral returns at once and touches no storage. A counted
deferral is a DEFERRED run that is not the deadline stop of a rung that never
started (``RungRun.deadline_stop``: the budget ran out ahead of that rung, so
it did not defer anything). For each counted deferral:

* when the rung has a completion newer than the entry's ``last``, the rung
  converged since the streak (here or on another machine): the streak restarts
  at 1 with ``since`` now;
* otherwise the count goes up by one, ``since`` stays at the first deferral of
  the streak, and ``last`` and the detail are the latest.

In the same write, any entry whose rung has a completion newer than its ``last``
is dropped. NOT_ATTEMPTED and failed runs leave entries alone: the rung was not
walked, or it failed loudly (which stops the upgrade and says so), and neither
is evidence that a streak ended.

The note is a T2 row any version or tool may have written. Only the documented
shape is read; an entry of any other shape is ignored and dropped by the next
write. Callers run every operation through :func:`~nexus.upgrade_ladder.census_progress.guarded`:
the log only ever raises a warning, it must never change an upgrade's outcome.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nexus.upgrade_ladder.census_progress import (
    PROGRESS_PROJECT,
    STARVED_AFTER,
    NoteStore,
    T2NoteStore,
    _safe_text,
)

if TYPE_CHECKING:
    from nexus.upgrade_ladder.completion import CompletionRecord
    from nexus.upgrade_ladder.runner import LadderRunReport, RungRun

#: The T2 note's title, beside the census progress note in ``PROGRESS_PROJECT``.
DEFERRAL_TITLE = "ladder-deferrals"

#: Consecutive deferrals at which the doctor row can escalate (the same bar as the
#: RDR-192 rung's own STARVED mark).
ESCALATE_AFTER = STARVED_AFTER

#: And the streak must be at least this old: a session start spawns ``nx upgrade --auto`` each time, so three
#: deferrals can fall inside one working hour.
ESCALATE_MIN_SPAN = timedelta(hours=24)

#: Longest detail kept in the note (a rung's detail can carry an engine error body).
_DETAIL_STORE_MAX = 300

#: Where the detached ``--auto`` child's stderr goes (``nexus.hooks.upgrade_auto._child_log_path``, which resolves it
#: with stdlib only; a test pins that the two agree).
CHILD_LOG_RELATIVE = Path("logs") / "upgrade-auto-child.log"


def t2_deferral_log() -> DeferralLog:
    """The production log: the T2 note named :data:`DEFERRAL_TITLE`."""
    return DeferralLog(T2NoteStore(DEFERRAL_TITLE, project=PROGRESS_PROJECT, tags="upgrade-ladder,deferrals"))


def counted_deferrals(report: LadderRunReport) -> list[RungRun]:
    """The runs of *report* that add to a streak: DEFERRED, and not the deadline stop of a rung that never started."""
    from nexus.upgrade_ladder.runner import RungOutcome  # noqa: PLC0415 — runner imports the registry; keep this module import-light

    return [run for run in report.runs if run.outcome is RungOutcome.DEFERRED and not run.deadline_stop]


def _parse(stamp: object) -> datetime | None:
    """An ISO-8601 stamp as an aware datetime (naive reads as UTC); None when it is not one."""
    if not isinstance(stamp, str):
        return None
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _span_text(span: timedelta) -> str:
    hours = max(0, int(span.total_seconds() // 3600))
    return f"{hours} h" if hours < 48 else f"{hours // 24} days"


@dataclass(frozen=True)
class RungDeferral:
    """One rung's streak of consecutive deferred runs."""

    rung: str
    count: int
    since: str
    last: str
    detail: str

    def span(self, now: datetime) -> timedelta | None:
        """How long the streak has run at *now*; None when ``since`` is not a stamp."""
        start = _parse(self.since)
        return None if start is None else now - start

    def escalated(self, now: datetime) -> bool:
        """At least :data:`ESCALATE_AFTER` consecutive deferrals AND a streak at least :data:`ESCALATE_MIN_SPAN` old."""
        span = self.span(now)
        return self.count >= ESCALATE_AFTER and span is not None and span >= ESCALATE_MIN_SPAN

    def describe(self, now: datetime, config_dir: Path) -> str:
        span = self.span(now)
        over = f" over {_span_text(span)}" if span is not None else ""
        return (
            f"rung '{_safe_text(self.rung)}' deferred {self.count} consecutive run(s){over}, since "
            f"{_safe_text(self.since)} (last {_safe_text(self.last)}: {_safe_text(self.detail, 200)}); "
            "a slow rung that is still making progress defers each run too. Run `nx upgrade` "
            f"(interactively, not --auto) to see why, or read the --auto child's log at {config_dir / CHILD_LOG_RELATIVE}"
        )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _entry_ok(entry: object) -> bool:
    return (
        isinstance(entry, dict) and _is_int(entry.get("n")) and entry["n"] >= 1
        and isinstance(entry.get("since"), str) and isinstance(entry.get("last"), str)
        and isinstance(entry.get("detail"), str)
    )


def _converged_since(entry: Mapping[str, Any] | None, completion: CompletionRecord | None) -> bool:
    """True when *completion* was verified later than the entry's ``last``: the rung converged after the streak."""
    if entry is None or completion is None:
        return False
    verified, last = _parse(completion.verified_at), _parse(entry["last"])
    return verified is not None and last is not None and verified > last


class DeferralLog:
    """The per-rung deferral streaks, in one note. Each call loads the note,
    changes it and writes it back, so nothing is held between calls."""

    def __init__(
        self, store: NoteStore, *, now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
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

    def record(self, report: LadderRunReport, completions: Mapping[str, CompletionRecord]) -> None:
        """Fold one finished walk into the streaks (see the module docstring). *completions* is the ledger's
        ``{rung: CompletionRecord}`` as of the end of the walk."""
        deferred = counted_deferrals(report)
        if not deferred:
            return  # nothing to add: no storage is touched
        entries = self._entries()
        before = {name: dict(e) for name, e in entries.items()}
        now = self._now()
        stamp, last = now.isoformat(timespec="seconds"), now.isoformat()  # ``last`` keeps the fraction: it is compared with a completion's
        for name in [n for n, e in entries.items() if _converged_since(e, completions.get(n))]:
            del entries[name]  # it converged after its last deferral; the deferrals below start over
        for run in deferred:
            prior = entries.get(run.name)
            entries[run.name] = {
                "n": (prior["n"] if prior else 0) + 1,
                "since": prior["since"] if prior else stamp,
                "last": last,
                "detail": run.detail[:_DETAIL_STORE_MAX],
            }
        if entries != before:
            self._store.save({"rungs": entries})

    def deferrals(self) -> dict[str, RungDeferral]:
        """Every rung with a streak, by name."""
        return {
            name: RungDeferral(name, e["n"], e["since"], e["last"], e["detail"])
            for name, e in self._entries().items()
        }

    def escalated(self) -> dict[str, RungDeferral]:
        """The rungs whose streak has reached :data:`ESCALATE_AFTER` over at least :data:`ESCALATE_MIN_SPAN`."""
        now = self._now()
        return {name: d for name, d in self.deferrals().items() if d.escalated(now)}

    def describe(self, deferral: RungDeferral) -> str:
        """The doctor's text for one escalated rung, at this log's clock."""
        from nexus import config as _config  # noqa: PLC0415 — resolved at call time (tests/AGENTS.md: setenv, never setattr)

        return deferral.describe(self._now(), Path(_config.nexus_config_dir()))
