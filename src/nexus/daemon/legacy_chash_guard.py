# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guard: an engine never starts on a store that still holds 16-byte chash rows until a backup exists (nexus-f3yqd).

The engine's changeset ``hygiene-001-5`` deletes every chunk row whose chash is not 32 bytes: the 32-byte cutover has no
way back, so such a row's content cannot be resolved. A store that last ran an engine before 0.1.100 can still hold
them, and the next engine start would delete them at boot. Sam (2026-11-11): no deletion of user data without a copy
first, and no automatic backup. So before the supervisor spawns an engine against the bundled cluster it asks the
cluster, as the bootstrap superuser (``nexus.chunks`` has FORCE row-level security):

1. No ``public.databasechangelog``: no engine ever ran here; proceed.
2. A changelog row ``hygiene-001-5``: the delete already ran; proceed.
3. Count the rows whose chash is not 32 bytes in ``nexus.chunks`` and ``nexus.catalog_document_chunks``, by the
   column's type (bytea: ``octet_length(chash) <> 32``; text: ``length(chash) <> 64``). No ``nexus.chunks``: a store
   from before Postgres, which the migration runbook handles; proceed.
4. A count above 0: refuse. The refusal is written to ``legacy-chash-guard.json`` in the config directory with the
   counts and the time, Postgres stays up so ``nx backup`` can dump it, and the start exits 75. Once a dump in
   ``~/nexus-backups/`` newer than the refusal reads back (``pg_restore -l``), the next start proceeds.

Specified by wren (engine), word for word in the design relay; nothing here runs ``nx backup`` (its pin forbids it).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)

GUARD_FILENAME: str = "legacy-chash-guard.json"
#: nx backup's default directory under the home directory (its own constant cannot be imported: the backup pin).
BACKUP_DIRNAME: str = "nexus-backups"
DELETING_CHANGESET: str = "hygiene-001-5"
EXIT_REFUSED: int = 75
#: The two tables the deleting changeset reaches, and the label each count carries.
TABLES: tuple[tuple[str, str], ...] = (("chunks", "chunk rows"), ("catalog_document_chunks", "manifest rows"))

#: Runs one SQL statement and returns its tuple-only output (psql -tA); raises on an SQL or connection error.
RunSql = Callable[[str], str]


@dataclass(frozen=True)
class Verdict:
    """``proceed`` with a reason, or a refusal with the counts per table."""

    proceed: bool
    reason: str
    counts: dict[str, int]


def _bool(out: str) -> bool:
    return out.strip() in ("t", "true", "1")


def check(run_sql: RunSql) -> Verdict:
    """Ask the cluster whether starting an engine would delete legacy rows (wren's steps 1-4)."""
    if not _bool(run_sql("SELECT to_regclass('public.databasechangelog') IS NOT NULL")):
        return Verdict(True, "no engine has run against this store (no changelog)", {})
    if _bool(run_sql(f"SELECT EXISTS (SELECT 1 FROM public.databasechangelog WHERE id = '{DELETING_CHANGESET}')")):
        return Verdict(True, f"{DELETING_CHANGESET} already ran", {})
    types: dict[str, str] = {}
    for line in run_sql(
        "SELECT table_name || '|' || data_type FROM information_schema.columns "
        "WHERE table_schema = 'nexus' AND column_name = 'chash' "
        "AND table_name IN ('chunks', 'catalog_document_chunks')"
    ).splitlines():
        table, _, dtype = line.strip().partition("|")
        if table:
            types[table] = dtype
    if "chunks" not in types:
        return Verdict(True, "no nexus.chunks: a store from before Postgres, the migration runbook's path", {})
    counts: dict[str, int] = {}
    for table, _ in TABLES:
        dtype = types.get(table)
        if dtype is None:
            continue
        predicate = "octet_length(chash) <> 32" if dtype == "bytea" else "length(chash) <> 64"
        counts[table] = int(run_sql(f"SELECT count(*) FROM nexus.{table} WHERE {predicate}").strip() or 0)
    if any(counts.values()):
        return Verdict(False, f"{DELETING_CHANGESET} would delete these rows at engine start", counts)
    return Verdict(True, "no row with a 16-byte chash", counts)


def guard_path(config_dir: Path) -> Path:
    return config_dir / GUARD_FILENAME


def backup_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / BACKUP_DIRNAME


def record_refusal(config_dir: Path, verdict: Verdict, *, now: datetime | None = None) -> dict:
    """Write (or keep) the refusal record. An existing unresolved record keeps its first ``refused_at``, so a backup
    taken after the first refusal still counts on every later start."""
    prior = pending_refusal(config_dir)
    refused_at = prior["refused_at"] if prior else (now or datetime.now(UTC)).isoformat()
    record = {"refused_at": refused_at, "counts": verdict.counts, "changeset": DELETING_CHANGESET}
    guard_path(config_dir).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def verified_backup_since(
    refused_at: str, *, pg_restore: Path, home: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Path | None:
    """The newest dump in ``~/nexus-backups/`` written after ``refused_at`` that ``pg_restore -l`` reads, or None."""
    try:
        since = datetime.fromisoformat(refused_at).timestamp()
    except ValueError:
        return None
    folder = backup_dir(home)
    try:
        candidates = sorted((p for p in folder.glob("*.dump") if p.is_file() and p.stat().st_mtime > since),
                            key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for dump in candidates:
        try:
            done = runner([str(pg_restore), "-l", str(dump)], capture_output=True, timeout=300, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            _log.warning("legacy_chash_guard_backup_unreadable", path=str(dump), error=str(exc))
            continue
        if done.returncode == 0:
            return dump
        _log.warning("legacy_chash_guard_backup_unreadable", path=str(dump), returncode=done.returncode)
    return None


def mark_cleared(config_dir: Path, backup: Path, *, now: datetime | None = None) -> None:
    """Keep the record as history: when and by which backup the refusal cleared."""
    path = guard_path(config_dir)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = {}
    record.update({"cleared_at": (now or datetime.now(UTC)).isoformat(), "cleared_by": str(backup)})
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def refusal_text(record: dict, *, home: Path | None = None) -> str:
    """What the user reads: the counts, what would happen, and the one command that lets the start go on."""
    counts = record.get("counts") or {}
    labels = dict(TABLES)
    rows = ", ".join(f"{n} {labels.get(t, t)}" for t, n in counts.items() if n)
    return (
        f"Not starting the engine: this store still holds rows with 16-byte chash keys ({rows}). The engine's "
        f"{record.get('changeset', DELETING_CHANGESET)} migration deletes them at its first start, and that cannot be "
        "undone, so nexus will not start it until a copy exists.\n"
        f"Run `nx backup` now (Postgres is left running for it); it writes a verified dump to {backup_dir(home)}/. "
        "Then start again: the start goes on once a dump newer than this refusal "
        f"({record.get('refused_at')}) reads back."
    )


def read_record(config_dir: Path) -> dict | None:
    try:
        record = json.loads(guard_path(config_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def pending_refusal(config_dir: Path) -> dict | None:
    """The unresolved refusal, if any (for the CLI's start wait)."""
    record = read_record(config_dir)
    return record if record and record.get("refused_at") and not record.get("cleared_at") else None
