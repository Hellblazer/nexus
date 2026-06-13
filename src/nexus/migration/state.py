# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-159 P1a (nexus-ue6g7.6): the ``migration.state`` sentinel mechanism.

The load-bearing NEW mechanism RDR-159 introduces (§"Cross-process migration
state"). A single ``~/.config/nexus/migration.state`` file is the one source of
truth for "is this install mid-upgrade", written by the CLI migration process
and polled by every *separate, long-lived* process:

* the MCP read surfaces (``search`` / ``store_get`` / ``store_get_many`` / the
  ``nx_answer`` plan runner / the ``nx search`` CLI) — prepend a LOUD banner
  while ``phase == migrating``/``migrated-failed`` instead of serving a bare
  empty result;
* the aspect workers and ``nx index`` — suspend while ``phase == migrating``.

A CLI-local flag cannot reach those processes, so the state lives on disk and is
read with a cheap ``stat`` + ``read``.

**Atomicity (gate-3 condition).** Writes go through a ``.tmp`` sibling +
``os.rename`` — POSIX-atomic, so a concurrent poller observes either the old
file or the new one, never a torn payload. This is the deliberate rejection of
the ``phase_review_sentinel`` precedent (a bare ``write_text``), which is the
named anti-pattern.

**Derived progress.** ``collections_done`` / ``collections_total`` are RECOMPUTED
from live source-vs-target counts on every resumed run (the ETL upsert is
idempotent on ``(tenant, collection, chash)``), never trusted from the stale
marker — :func:`record_progress` writes absolute live counts, not increments.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

import structlog

from nexus.config import nexus_config_dir

_log = structlog.get_logger(__name__)

#: State-machine phases. ``not-migrating`` is the implicit default whenever the
#: sentinel file is absent (a fresh or post-unlock install).
NOT_MIGRATING: Final[str] = "not-migrating"
MIGRATING: Final[str] = "migrating"
MIGRATED: Final[str] = "migrated"
MIGRATED_FAILED: Final[str] = "migrated-failed"

Phase = Literal["not-migrating", "migrating", "migrated", "migrated-failed"]

_STATE_FILENAME: Final[str] = "migration.state"


@dataclass(frozen=True)
class MigrationState:
    """The on-disk sentinel payload.

    ``failure`` is serialized only when set (a non-empty triage message on the
    ``migrated-failed`` path); absent otherwise.
    """

    phase: str
    started_at: str | None
    collections_total: int
    collections_done: int
    failure: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "phase": self.phase,
            "started_at": self.started_at,
            "collections_total": self.collections_total,
            "collections_done": self.collections_done,
        }
        if self.failure is not None:
            payload["failure"] = self.failure
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MigrationState":
        return cls(
            phase=data["phase"],
            started_at=data.get("started_at"),
            collections_total=int(data["collections_total"]),
            collections_done=int(data["collections_done"]),
            failure=data.get("failure"),
        )


def state_path() -> Path:
    """Path to the sentinel under the (env-overridable) nexus config dir."""
    return nexus_config_dir() / _STATE_FILENAME


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def write_state(state: MigrationState) -> Path:
    """Atomically write ``state`` to the sentinel path.

    A ``.tmp`` sibling is written then ``os.rename``-d over the target (POSIX-
    atomic on the same filesystem), so a concurrent poller never reads a partial
    payload. NOT a bare ``write_text`` — that is the ``phase_review_sentinel``
    anti-pattern this mechanism exists to avoid.
    """
    target = state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Unique-per-pid temp sibling so two writers never clobber each other's
    # scratch file mid-rename.
    tmp = target.with_name(f"{_STATE_FILENAME}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    os.rename(tmp, target)
    return target


def read_state() -> MigrationState | None:
    """Return the current sentinel, or ``None`` if absent / unparseable.

    A missing file is the ``not-migrating`` default. An unparseable file is
    foreign/corrupt (atomic writes never leave a partial) and is treated as
    not-migrating so a poller fails soft to normal serving rather than wedging.
    """
    path = state_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:  # pragma: no cover - unexpected IO error
        _log.warning("migration_state_read_failed", error=str(exc))
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        _log.warning("migration_state_corrupt", path=str(path))
        return None
    if not isinstance(data, dict):
        return None
    try:
        return MigrationState.from_dict(data)
    except (KeyError, ValueError, TypeError):
        _log.warning("migration_state_schema_invalid", path=str(path))
        return None


def current_phase() -> str:
    """Return the current phase, or ``not-migrating`` when no sentinel exists."""
    state = read_state()
    return state.phase if state is not None else NOT_MIGRATING


def is_migrating() -> bool:
    """True iff a migration is actively in progress (``phase == migrating``)."""
    return current_phase() == MIGRATING


def begin_migration(
    collections_total: int, *, started_at: str | None = None
) -> MigrationState:
    """Enter the ``migrating`` phase with zero collections done.

    ``started_at`` defaults to UTC-now ISO-8601; tests pass an explicit value
    for a fixed clock.
    """
    state = MigrationState(
        phase=MIGRATING,
        started_at=started_at if started_at is not None else _utc_now_iso(),
        collections_total=collections_total,
        collections_done=0,
    )
    write_state(state)
    return state


def record_progress(
    *, collections_done: int, collections_total: int
) -> MigrationState:
    """Overwrite progress with DERIVED live counts, preserving ``started_at``.

    Called on a resumed run after detection recomputes source-vs-target counts.
    The values are absolute (live), never incremented from the possibly-stale
    marker. ``started_at`` is carried forward from the existing sentinel if one
    is present.
    """
    existing = read_state()
    started_at = existing.started_at if existing is not None else _utc_now_iso()
    state = MigrationState(
        phase=MIGRATING,
        started_at=started_at,
        collections_total=collections_total,
        collections_done=collections_done,
    )
    write_state(state)
    return state


def mark_migrated() -> MigrationState:
    """Enter the terminal ``migrated`` phase, preserving progress + start time."""
    existing = read_state()
    state = MigrationState(
        phase=MIGRATED,
        started_at=existing.started_at if existing is not None else _utc_now_iso(),
        collections_total=existing.collections_total if existing is not None else 0,
        collections_done=existing.collections_done if existing is not None else 0,
    )
    write_state(state)
    return state


def mark_failed(failure: str) -> MigrationState:
    """Enter the terminal ``migrated-failed`` phase with a triage message."""
    existing = read_state()
    state = MigrationState(
        phase=MIGRATED_FAILED,
        started_at=existing.started_at if existing is not None else _utc_now_iso(),
        collections_total=existing.collections_total if existing is not None else 0,
        collections_done=existing.collections_done if existing is not None else 0,
        failure=failure,
    )
    write_state(state)
    return state


def clear_state() -> None:
    """Remove the sentinel (UNLOCK on clean validation / escape hatch).

    Idempotent: a no-op when the sentinel is already absent. After clearing,
    ``current_phase()`` reports ``not-migrating`` and serving resumes normally.
    """
    try:
        state_path().unlink()
    except FileNotFoundError:
        return
