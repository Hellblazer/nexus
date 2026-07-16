# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-185 P0.1: the Rung Protocol — the one seam every ladder rung implements.

A rung is one data transition in the single upgrade ladder (T2 schema,
substrate ETL, …). The seam is ``detect → converge → verify``:

- ``detect()`` is READ-ONLY — it is the ``nx doctor`` / dry-run surface
  (the ``resolve_pending_steps`` precedent) and must never perform work.
  A rung that is N/A for this install/mode reports ``applicable=False``
  and the walk skips it (the nexus-f0pmd detect→skip gate pattern).
- ``converge(report)`` does the work. It is idempotent and RESUMABLE
  (RDR-178): long rungs persist progress and return
  :attr:`ConvergeOutcome.RESUMABLE` per batch; a re-run continues from
  the persisted floor, never duplicating work. Failure is an exception,
  never a verdict.
- ``verify()`` is READ-ONLY and is the ONLY evidence that lets the runner
  record the rung complete (RDR-142: the position must never advance past
  deferred or failed work — the guard itself lives in the runner, P0.3).

Verdicts are frozen dataclasses (the ``migrations.py`` Migration/
StepOutcome precedent): a consumer can never "fix up" a verdict, the
settable-version bug class RDR-185 bans at every layer.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class RungStatus:
    """Read-only verdict from :meth:`Rung.detect`.

    ``applicable`` — does this rung apply to this install/mode at all
    (False = detect→skip, no consent, no work).
    ``converged`` — the data is already at this rung's target state.
    ``pending_detail`` — human-readable detail for the doctor surface when
    the rung is pending (what remains, roughly how much).
    """

    applicable: bool
    converged: bool
    pending_detail: str = ""

    @property
    def pending(self) -> bool:
        """True when the walk has work here: applicable and not converged."""
        return self.applicable and not self.converged


class ConvergeOutcome(str, Enum):
    """What a :meth:`Rung.converge` call achieved. Failure raises instead."""

    COMPLETED = "completed"    # target state reached; verify() should now pass
    RESUMABLE = "resumable"    # partial progress persisted; call again to continue


@dataclass(frozen=True)
class ConvergeResult:
    """Return of :meth:`Rung.converge` — completion or a resumable floor."""

    outcome: ConvergeOutcome
    detail: str = ""

    @property
    def completed(self) -> bool:
        return self.outcome is ConvergeOutcome.COMPLETED


@runtime_checkable
class ProgressReporter(Protocol):
    """Progress sink a rung emits batch events into during ``converge``.

    The runner (P0.3) supplies the production implementation; tests supply
    recorders. Structural typing only — any object with a conforming
    ``emit`` participates.
    """

    def emit(self, event: str, **fields: object) -> None:
        """Report one progress event (structlog-shaped: event + fields)."""
        ...


@runtime_checkable
class Rung(Protocol):
    """One data transition in the upgrade ladder. Structural — no base class.

    Implementations compose their dependencies via constructor injection;
    interim rungs MAY wrap existing upgrade verbs (Decision-Space option 2)
    behind this seam without reimplementing their bodies.
    """

    name: str

    def detect(self) -> RungStatus:
        """READ-ONLY: does this rung apply, and is it already converged?"""
        ...

    def converge(self, report: ProgressReporter) -> ConvergeResult:
        """Do (a batch of) the work. Idempotent; resumable; raises on failure."""
        ...

    def verify(self) -> bool:
        """READ-ONLY: is the target state actually reached? The runner records
        completion ONLY on a True return (RDR-142)."""
        ...
