# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-185 P1.1: the T2 schema rung — the first native ladder rung.

Adapts the proven ``apply_pending`` axis to the Rung Protocol as the
reference implementation (the SQLite ``user_version`` archetype: cheap,
on-open class):

- ``detect`` — READ-ONLY: stored ``_nexus_version`` row vs
  ``expected_t2_schema_version()``, with step-level truth from
  ``resolve_pending_steps`` (RDR-142 dry-run-truth) in the pending detail.
- ``converge`` — ``apply_pending`` under the cross-process migration
  flock. No behavior change to T2 migration itself; it now REPORTS
  through the ladder.
- ``verify`` — the stored row caught up to expected. ``apply_pending``
  refuses to stamp when any step deferred (``any_skipped``, the RDR-142
  guard at its source), so a deferral surfaces here as verify-fail and
  the runner records nothing — the ladder position never advances past
  deferred work.

Constructor injection: ``db_path_fn`` / ``expected_version_fn`` are the
test seams; production defaults resolve the real config-dir ``memory.db``
and the registry-aware expected version.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import structlog

from nexus.upgrade_ladder.protocol import ConvergeOutcome, ConvergeResult, ProgressReporter, RungStatus
from nexus.upgrade_ladder.registry import RUNG_T2_SCHEMA

_log = structlog.get_logger(__name__)


def _default_db_path() -> Path:
    from nexus.config import default_db_path  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    return default_db_path()


def _default_expected_version() -> str:
    from nexus.db.migrations import expected_t2_schema_version  # noqa: PLC0415 — deferred to avoid import cost

    return expected_t2_schema_version()


def _open(path: Path, *, ro: bool) -> sqlite3.Connection:
    """The rung's single connect site (read-only URI for detect/verify)."""
    target = f"file:{path}?mode=ro" if ro else str(path)
    return sqlite3.connect(target, uri=ro, check_same_thread=False)  # epsilon-allow: T2-schema ladder rung — same chicken-and-egg substrate bootstrap as nx upgrade (commands/upgrade.py); migration machinery cannot route through the daemon it migrates


class T2SchemaRung:
    """T2 schema as a ladder rung. See module docstring."""

    name: str = RUNG_T2_SCHEMA

    def __init__(
        self,
        *,
        db_path_fn: Callable[[], Path] | None = None,
        expected_version_fn: Callable[[], str] | None = None,
    ) -> None:
        self._db_path_fn = db_path_fn if db_path_fn is not None else _default_db_path
        self._expected_fn = (
            expected_version_fn if expected_version_fn is not None else _default_expected_version
        )

    # ── detect ───────────────────────────────────────────────────────────────

    def detect(self) -> RungStatus:
        expected = self._expected_fn()
        path = self._db_path_fn()
        if not path.exists():
            return RungStatus(
                applicable=True,
                converged=False,
                pending_detail=f"T2 database not initialized; schema will bootstrap to {expected}",
            )
        stored = self._stored_version(path)
        if stored is None:
            return RungStatus(
                applicable=True,
                converged=False,
                pending_detail=f"T2 database has no version row; schema will bootstrap to {expected}",
            )
        if self._caught_up(stored, expected):
            return RungStatus(applicable=True, converged=True)
        return RungStatus(
            applicable=True,
            converged=False,
            pending_detail=self._pending_detail(path, stored, expected),
        )

    def _pending_detail(self, path: Path, stored: str, expected: str) -> str:
        detail = f"T2 schema at {stored}, expected {expected}"
        try:
            from nexus.db.migrations import resolve_pending_steps  # noqa: PLC0415 — deferred to avoid import cost

            conn = _open(path, ro=True)
            try:
                steps = resolve_pending_steps(conn, expected)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 — detail enrichment is best-effort; the version comparison already established pending
            _log.debug("t2_schema_rung_step_resolution_failed", error=str(exc))
            return detail
        if steps:
            counts: dict[str, int] = {}
            for step in steps:
                counts[step.outcome.value] = counts.get(step.outcome.value, 0) + 1
            summary = ", ".join(f"{n} {outcome}" for outcome, n in sorted(counts.items()))
            detail += f" ({summary})"
        return detail

    # ── converge ─────────────────────────────────────────────────────────────

    def converge(self, report: ProgressReporter) -> ConvergeResult:
        """Run ``apply_pending`` under the migration flock.

        ``apply_pending`` refuses to stamp when any step raised
        ``MigrationRetry`` (the ``any_skipped`` guard) — when the stored
        version is still behind after a clean return, the remainder is the
        deferred class (precondition-blocked, retried on a later run) and
        this reports DEFERRED, never a false COMPLETED. A gate-class step
        raises ``MigrationError`` out of ``apply_pending`` itself and the
        runner records the rung FAILED.
        """
        from nexus.db.migrations import apply_pending, t2_migration_flock  # noqa: PLC0415 — deferred to avoid import cost

        expected = self._expected_fn()
        path = self._db_path_fn()
        path.parent.mkdir(parents=True, exist_ok=True)
        with t2_migration_flock(path.parent):
            conn = _open(path, ro=False)
            try:
                apply_pending(conn, expected)
            finally:
                conn.close()
        stored = self._stored_version(path)
        if stored is None or not self._caught_up(stored, expected):
            detail = (
                f"T2 schema at {stored or 'unknown'}, expected {expected}: one or more "
                "steps deferred on a precondition (retried on next run; see nx doctor)"
            )
            report.emit("t2_schema_rung_deferred", stored=stored, expected=expected)
            return ConvergeResult(ConvergeOutcome.DEFERRED, detail=detail)
        report.emit("t2_schema_rung_converged", expected=expected)
        return ConvergeResult(ConvergeOutcome.COMPLETED)

    # ── verify ───────────────────────────────────────────────────────────────

    def verify(self) -> bool:
        """Stored version caught up to expected. ``apply_pending`` never
        stamps past deferred work, so a deferral fails verification here
        and the runner records nothing."""
        path = self._db_path_fn()
        if not path.exists():
            return False
        stored = self._stored_version(path)
        return stored is not None and self._caught_up(stored, self._expected_fn())

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _caught_up(stored: str, expected: str) -> bool:
        from nexus.db.migrations import _parse_version  # noqa: PLC0415 — deferred to avoid import cost

        return _parse_version(stored) >= _parse_version(expected)

    @staticmethod
    def _stored_version(path: Path) -> str | None:
        try:
            conn = _open(path, ro=True)
        except sqlite3.Error:
            return None
        try:
            row = conn.execute(
                "SELECT value FROM _nexus_version WHERE key='cli_version'"
            ).fetchone()
            return row[0] if row else None
        except sqlite3.Error:
            return None  # table absent — un-bootstrapped
        finally:
            conn.close()
