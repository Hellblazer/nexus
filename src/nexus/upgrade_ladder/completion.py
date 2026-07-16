# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-185 P0.2: ladder-local completion records + derived ladder position.

LADDER-LOCAL machine state (independent-audit HIGH finding): this store
owns its own sqlite file (``ladder.db``), opened with raw
``sqlite3.connect`` and bootstrapped via ``CREATE TABLE IF NOT EXISTS``
on open. It is deliberately OUTSIDE the ``T2Database`` facade and OUTSIDE
``apply_pending`` — the completion store must exist before, and
independently of, the t2-schema rung whose completion it records (the
bootstrapping trap the audit flagged). It is exempt from RDR-158
retirement and must never be registered in ``migration/etl_registry.py``.

One durable "verified" fact per rung (the Flyway-history /
GitLab-Finished shape), written in ONE transaction per rung record, and
ONLY after the rung's ``verify()`` passed — the runner (P0.3) owns that
guard; this store just makes the RDR-142 bug class unrepresentable:
ladder position is DERIVED at read time as the max contiguous verified
prefix of the rung order (RQ6). No stored position, no setter.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rung_completions (
    rung_name       TEXT PRIMARY KEY,
    verified_at     TEXT NOT NULL,
    package_version TEXT NOT NULL,
    detail          TEXT NOT NULL DEFAULT ''
)
"""


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class CompletionRecord:
    """One durable verified fact for one rung."""

    rung_name: str
    verified_at: str
    package_version: str
    detail: str = ""


class CompletionStore:
    """The ladder's completion-record substrate. See module docstring.

    ``now_fn`` is an injectable clock seam (deterministic tests); the
    default stamps ISO-8601 UTC.
    """

    def __init__(self, db_path: Path, *, now_fn: Callable[[], str] | None = None) -> None:
        self._now_fn = now_fn if now_fn is not None else _utc_now_iso
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), isolation_level=None)  # epsilon-allow: ladder-local completion records (ladder.db) own their substrate — deliberately outside T2Database/apply_pending so the store exists before the t2-schema rung it records (RDR-185 audit; RDR-158-exempt)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA)

    # ── recording ────────────────────────────────────────────────────────────

    def record_verified(self, rung_name: str, *, package_version: str, detail: str = "") -> None:
        """Record that *rung_name*'s verify passed — ONE transaction per rung
        record (locked contract). Upserts: re-verification replaces the fact.

        Callers (the runner) must invoke this ONLY after ``Rung.verify()``
        returned True; there is no unverified-completion shape to store.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                """
                INSERT INTO rung_completions (rung_name, verified_at, package_version, detail)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(rung_name) DO UPDATE SET
                    verified_at = excluded.verified_at,
                    package_version = excluded.package_version,
                    detail = excluded.detail
                """,
                (rung_name, self._now_fn(), package_version, detail),
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    # ── reading / derivation ─────────────────────────────────────────────────

    def completions(self) -> dict[str, CompletionRecord]:
        """All recorded completion facts, keyed by rung name."""
        rows = self._conn.execute(
            "SELECT rung_name, verified_at, package_version, detail FROM rung_completions"
        ).fetchall()
        return {
            name: CompletionRecord(
                rung_name=name,
                verified_at=verified_at,
                package_version=package_version,
                detail=detail,
            )
            for name, verified_at, package_version, detail in rows
        }

    def verified_rungs(self) -> frozenset[str]:
        rows = self._conn.execute("SELECT rung_name FROM rung_completions").fetchall()
        return frozenset(name for (name,) in rows)

    def ladder_position(self, order: Sequence[str]) -> int:
        """DERIVED ladder position: the max contiguous prefix of *order*
        whose rungs all have a verified completion record.

        Never stored, never settable — a hole in the prefix pins the
        position below it regardless of any later-rung records (RDR-142:
        the pointer must never advance past deferred or failed work).
        """
        verified = self.verified_rungs()
        position = 0
        for rung_name in order:
            if rung_name not in verified:
                break
            position += 1
        return position

    # ── lifecycle ────────────────────────────────────────────────────────────

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "CompletionStore":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
