# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guard: an engine never starts on a store whose boot would delete legacy chash rows until a backup exists (nexus-f3yqd).

The engine's changeset ``hygiene-001-5`` deletes every ``nexus.chunks`` row whose chash is not 32 bytes (and the
manifest rows that reference them); ``hygiene-001-8`` deletes every ``nexus.catalog_document_chunks`` row whose chash is
not 32 bytes. The 32-byte cutover has no way back, so such a row's content cannot be resolved. A store that last ran an
engine before 0.1.100 can still hold them, and the next engine start would delete them at boot. Sam (2026-11-11): no
deletion of user data without a copy first, and no automatic backup. So before the supervisor spawns an engine against
the bundled cluster it asks the cluster, as the bootstrap superuser (the chunk table enforces row-level security even
for its owner):

1. No ``public.databasechangelog``: no engine ever ran here; proceed (and, once the engine is healthy, remember it).
2. Each table is gated on its own changeset: the chunk tables on ``hygiene-001-5``; the manifest only when both
   ``hygiene-001-5`` and ``hygiene-001-8`` ran (a boot can fail between them).
3. Count the rows whose chash is not 32 bytes, by the column's type and the era's tables: ``nexus.chunks`` when it
   exists, else the dim-sharded ``chunks_384/768/1024`` (a store from before ``vectors-004``); the manifest in every
   era. Bytea: ``octet_length(chash) <> 32``. Text: ``rdr180-3``'s decode rule (lowercase even-length hex decodes to
   half its length, anything else is carried as its UTF-8 bytes), so a value that decodes to exactly 32 bytes survives
   and is not counted.
4. A count above 0: refuse. The refusal creates a marker schema ``nx_legacy_guard_<token>`` in the cluster, is
   written to ``legacy-chash-guard.json`` in the config directory, Postgres stays up so ``nx backup`` can dump it, and
   the start exits 75. A dump clears the refusal only when ``pg_restore -l`` lists that marker schema (so it is a dump
   of this cluster taken after the refusal) and the table data the engine would delete.

Not being able to ask (Postgres in crash recovery, a timeout, no superuser password) is not a refusal and demands no
backup: the supervisor waits and asks again. The one exception is a marker, keyed to the cluster's data directory and
written only after a gate run SAW the deleting changesets applied, or saw no changelog and then a healthy engine.

Nothing here runs ``nx backup`` (its pin forbids it) or imports its module. Specified by wren (engine).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)

GUARD_FILENAME: str = "legacy-chash-guard.json"
MARKER_FILENAME: str = "legacy-chash-guard-seen.json"
#: nx backup's default directory under the home directory (its own constant cannot be imported: the backup pin).
BACKUP_DIRNAME: str = "nexus-backups"
DELETING_CHANGESET: str = "hygiene-001-5"
MANIFEST_CHANGESET: str = "hygiene-001-8"
EXIT_REFUSED: int = 75
SENTINEL_PREFIX: str = "nx_legacy_guard_"
_SENTINEL_NAME = re.compile(rf"{SENTINEL_PREFIX}[0-9a-f]{{12}}")
MANIFEST: str = "catalog_document_chunks"
SHARDED_CHUNKS: tuple[str, ...] = ("chunks_384", "chunks_768", "chunks_1024")
_CHASH_TABLES: tuple[str, ...] = ("chunks", *SHARDED_CHUNKS, MANIFEST)
#: One ``pg_restore -l`` gets this long, and one attempt looks at this many dumps, newest first (cost bound).
RESTORE_LIST_TIMEOUT_S: float = 60.0
MAX_DUMPS_PER_ATTEMPT: int = 5

#: Runs one SQL statement and returns its tuple-only output (psql -tA); raises on an SQL or connection error.
RunSql = Callable[[str], str]


class LegacyChashRefused(RuntimeError):
    """The engine was not started: legacy rows would be deleted and no verified backup cleared it."""


class GuardCannotAsk(RuntimeError):
    """The cluster could not be asked (crash recovery, a timeout, no superuser password). Not a refusal: the
    supervisor waits and asks again; no backup is demanded for it."""


class PgRestoreUnavailable(RuntimeError):
    """``pg_restore`` could not be run, so no dump could be read: the user's dump is not at fault."""


@dataclass(frozen=True)
class Verdict:
    """``proceed`` with a reason, or a refusal with the counts per table.

    ``kind`` says what the check saw: ``no_changelog``, ``no_tables``, ``applied`` (both deleting changesets ran),
    ``clean`` (nothing to delete yet), ``refuse``, ``cleared`` or ``marker``. ``required`` is what a backup must hold
    as table data: ``(table, prefix_ok)`` pairs, ``prefix_ok`` for a partitioned parent whose data sits in leaves."""

    proceed: bool
    reason: str
    counts: dict[str, int]
    kind: str = ""
    required: tuple[tuple[str, bool], ...] = field(default_factory=tuple)


def _bool(out: str) -> bool:
    return out.strip() in ("t", "true", "1")


def _predicate(dtype: str) -> str:
    """The rows the engine's delete reaches, by the column's type (hygiene-001-5/-8, and rdr180-3 for text)."""
    if dtype == "bytea":
        return "(octet_length(chash) <> 32 OR chash IS NULL)"
    width = ("CASE WHEN chash ~ '^[0-9a-f]+$' AND length(chash) % 2 = 0 THEN length(chash) / 2 "
             "ELSE octet_length(chash) END")
    return f"({width} <> 32 OR chash IS NULL)"


def check(run_sql: RunSql) -> Verdict:
    """Ask the cluster whether starting an engine would delete legacy rows."""
    if not _bool(run_sql("SELECT to_regclass('public.databasechangelog') IS NOT NULL")):
        return Verdict(True, "no engine has run against this store (no changelog)", {}, "no_changelog")
    ran = {s.strip() for s in run_sql(
        "SELECT id FROM public.databasechangelog "
        f"WHERE id IN ('{DELETING_CHANGESET}', '{MANIFEST_CHANGESET}')"
    ).splitlines() if s.strip()}
    chunks_done = DELETING_CHANGESET in ran
    manifest_done = chunks_done and MANIFEST_CHANGESET in ran
    if manifest_done:
        return Verdict(True, f"{DELETING_CHANGESET} and {MANIFEST_CHANGESET} already ran", {}, "applied")
    types: dict[str, str] = {}
    names = ", ".join(f"'{t}'" for t in _CHASH_TABLES)
    for line in run_sql(
        "SELECT table_name || '|' || data_type FROM information_schema.columns "
        f"WHERE table_schema = 'nexus' AND column_name = 'chash' AND table_name IN ({names})"
    ).splitlines():
        table, _, dtype = line.strip().partition("|")
        if table:
            types[table] = dtype
    chunk_tables = ["chunks"] if "chunks" in types else [t for t in SHARDED_CHUNKS if t in types]
    if not chunk_tables and MANIFEST not in types:
        return Verdict(True, "no chunk tables: a store from before the engine's schema", {}, "no_tables")
    targets = ([] if chunks_done else chunk_tables) + ([MANIFEST] if MANIFEST in types else [])
    counts: dict[str, int] = {}
    for table in targets:
        counts[table] = int(run_sql(f"SELECT count(*) FROM nexus.{table} WHERE {_predicate(types[table])}").strip() or 0)
    if any(counts.values()):
        required = tuple((t, t == "chunks") for t in targets if t == MANIFEST or t in SHARDED_CHUNKS
                         or counts.get(t))
        return Verdict(False, f"{DELETING_CHANGESET} would delete these rows at engine start", counts, "refuse",
                       required)
    return Verdict(True, "no row with a legacy-width chash", counts, "clean")


def guard_path(config_dir: Path) -> Path:
    return config_dir / GUARD_FILENAME


def backup_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / BACKUP_DIRNAME


def _write_json(path: Path, record: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def record_refusal(
    config_dir: Path, verdict: Verdict, *, sentinel: str | None = None, now: datetime | None = None,
) -> dict:
    """Write (or keep) the refusal record. An existing unresolved record keeps its first ``refused_at``, its marker
    table and its rejected dumps, so a backup taken after the first refusal still counts on every later start."""
    prior = pending_refusal(config_dir) or {}
    record = {
        "refused_at": prior.get("refused_at") or (now or datetime.now(UTC)).isoformat(),
        "counts": verdict.counts, "changeset": DELETING_CHANGESET,
        "sentinel": prior.get("sentinel") or sentinel,
        "required": [list(r) for r in verdict.required],
        "rejected": prior.get("rejected") or {},
    }
    _write_json(guard_path(config_dir), record)
    return record


# ── the marker schema ─────────────────────────────────────────────────────────────────────────────────────────────


def create_sentinel(run_sql: RunSql) -> str:
    """Create the marker a backup must contain, as the superuser; returns its name. An empty schema: ``pg_dump -Fc``
    lists it in the archive's table of contents, which is all ``pg_restore -l`` has to show, and it carries no data
    and no table (nothing here is persistent state: it is dropped when the refusal clears)."""
    name = f"{SENTINEL_PREFIX}{secrets.token_hex(6)}"
    run_sql(f"CREATE SCHEMA {name}")
    run_sql(f"COMMENT ON SCHEMA {name} IS 'nexus legacy-chash guard: a backup of this store must hold this schema'")
    return name


def drop_sentinel(run_sql: RunSql, name: str) -> None:
    if _SENTINEL_NAME.fullmatch(name):  # the name came from a file: never splice anything else into SQL
        run_sql(f"DROP SCHEMA IF EXISTS {name} CASCADE")


def _release_sentinel(config_dir: Path, run_sql: RunSql) -> None:
    """After a clear, drop the marker schema once (best effort; retried by the next gate run)."""
    record = read_record(config_dir)
    name = (record or {}).get("sentinel")
    if not record or not record.get("cleared_at") or not name or record.get("sentinel_dropped"):
        return
    try:
        drop_sentinel(run_sql, name)
        record["sentinel_dropped"] = True
        _write_json(guard_path(config_dir), record)
    except Exception as exc:  # noqa: BLE001 — a leftover empty table is harmless; the next gate run tries again
        _log.warning("legacy_chash_guard_sentinel_not_dropped", sentinel=name, error=str(exc))


# ── backups ──────────────────────────────────────────────────────────────────────────────────────────────────────


def _lists_table_data(listing: str, schema: str, table: str, *, prefix_ok: bool = False) -> bool:
    tail = r"(?:_\S+)?" if prefix_ok else ""
    return bool(re.search(rf"TABLE DATA {re.escape(schema)} {re.escape(table)}{tail}(?:\s|$)", listing))


def holds_required_data(listing: bytes | str, record: dict) -> bool:
    """Does this ``pg_restore -l`` listing show a dump of THIS cluster (the marker schema) that holds the table data the
    engine would delete?"""
    text = listing.decode("utf-8", "replace") if isinstance(listing, bytes) else listing
    sentinel = record.get("sentinel")
    if not sentinel or not _SENTINEL_NAME.fullmatch(sentinel) or not re.search(
        rf"\bSCHEMA - {re.escape(sentinel)}(?:\s|$)", text
    ):
        return False
    required = record.get("required") or [[MANIFEST, False]]
    return all(_lists_table_data(text, "nexus", t, prefix_ok=bool(p)) for t, p in required)


def _restore_env(pg_restore: Path) -> dict[str, str]:
    from nexus.db.pg_provision import _bundle_lib_env  # noqa: PLC0415 — deferred: heavy import

    return _bundle_lib_env([str(pg_restore)], None)


def _dump_key(dump: Path) -> str:
    stat = dump.stat()
    return f"{dump}|{stat.st_mtime_ns}|{stat.st_size}"


def verified_backup_since(
    record: dict, *, pg_restore: Path, home: Path | None = None, explicit: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> Path | None:
    """The newest dump written after the refusal whose ``pg_restore -l`` lists the marker schema and the data the engine
    would delete, or None. Looks in ``~/nexus-backups/``, or only at ``explicit`` (``--legacy-backup``). A dump already
    rejected (same path, mtime and size) is not read again; one attempt reads at most ``MAX_DUMPS_PER_ATTEMPT``.
    Raises :class:`PgRestoreUnavailable` when ``pg_restore`` cannot run, which says nothing about any dump."""
    if runner is None:
        from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 — deferred

        runner = run_bounded
    try:
        since = datetime.fromisoformat(record["refused_at"]).timestamp()
    except (KeyError, ValueError):
        return None
    rejected: dict = record.setdefault("rejected", {})
    try:
        pool = [explicit] if explicit is not None else list(backup_dir(home).glob("*.dump"))
        candidates = sorted((p for p in pool if p.is_file() and p.stat().st_mtime > since),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        candidates = [p for p in candidates if _dump_key(p) not in rejected][:MAX_DUMPS_PER_ATTEMPT]
    except OSError:
        return None
    env = _restore_env(pg_restore)
    for dump in candidates:
        try:
            done = runner([str(pg_restore), "-l", str(dump)], timeout=RESTORE_LIST_TIMEOUT_S, env=env, text=False)
        except subprocess.TimeoutExpired:
            _log.warning("legacy_chash_guard_backup_listing_timed_out", path=str(dump))
            continue
        except (OSError, subprocess.SubprocessError) as exc:
            raise PgRestoreUnavailable(f"{pg_restore}: {exc}") from exc
        if done.returncode in (126, 127):  # the shell's "cannot execute" / "not found": the loader, not the dump
            tail = (done.stderr or b"")
            tail = tail.decode("utf-8", "replace") if isinstance(tail, bytes) else tail
            raise PgRestoreUnavailable(f"{pg_restore} exited {done.returncode}: {tail.strip()[-200:]}")
        if done.returncode == 0 and holds_required_data(done.stdout, record):
            return dump
        reason = "unreadable" if done.returncode else "does not hold this store's marker schema and chunk data"
        rejected[_dump_key(dump)] = reason
        _log.warning("legacy_chash_guard_backup_rejected", path=str(dump), returncode=done.returncode, reason=reason)
    return None


def mark_cleared(config_dir: Path, backup: Path, *, now: datetime | None = None) -> None:
    """Keep the record as history: when and by which backup the refusal cleared."""
    try:
        record = json.loads(guard_path(config_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = {}
    record.update({"cleared_at": (now or datetime.now(UTC)).isoformat(), "cleared_by": str(backup)})
    _write_json(guard_path(config_dir), record)


def refusal_text(record: dict, *, home: Path | None = None) -> str:
    """What the user reads: the counts, what would happen, the marker schema's purpose, and the command to run."""
    counts = record.get("counts") or {}
    rows = ", ".join(f"{n} {_label(t)}" for t, n in counts.items() if n)
    sentinel = record.get("sentinel")
    marker = (
        f"This refusal created the empty schema {sentinel} in your nexus database: a backup counts only if it contains "
        "that schema, which proves it is a dump of this store taken after the refusal (a dump of another store, or an "
        "older one, is not accepted). "
        if sentinel else
        "The marker schema that binds a backup to this store could not be created yet; the supervisor retries it, and "
        "no backup is accepted until it exists. "
    )
    return (
        f"Not starting the engine: this store still holds rows with legacy-width chash keys ({rows}). The engine's "
        f"{record.get('changeset', DELETING_CHANGESET)} migration deletes them at its first start, and that cannot be "
        "undone, so nexus will not start it until a copy exists.\n"
        f"Run `nx backup` now (Postgres is left running for it); it writes a verified dump to {backup_dir(home)}/. "
        f"{marker}"
        "Then start again (`nx daemon service start`, or `nx upgrade`): the start goes on once a dump written after "
        f"this refusal ({record.get('refused_at')}) reads back with the marker schema and the chunk and manifest data. "
        "A dump written elsewhere (`nx backup --to PATH`): `nx daemon service start --legacy-backup PATH`. The refusal "
        "stands until a backup clears it, even if the rows have been removed since."
    )


_LABELS = {"chunks": "chunk rows", MANIFEST: "manifest rows"}


def _label(table: str) -> str:
    return _LABELS.get(table, f"chunk rows in {table}")


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


def is_cleared(config_dir: Path) -> bool:
    record = read_record(config_dir)
    return bool(record and record.get("cleared_at") and not record.get("spent_at"))


def _spend_clearance(config_dir: Path) -> None:
    """The deleting changesets were seen applied: the clearance did its job. A later refusal (a restored older
    database under this config directory) needs a backup of its own. Best effort."""
    record = read_record(config_dir)
    if not record or not record.get("cleared_at") or record.get("spent_at"):
        return
    record["spent_at"] = datetime.now(UTC).isoformat()
    try:
        _write_json(guard_path(config_dir), record)
    except OSError as exc:
        _log.warning("legacy_chash_guard_clearance_not_spent", error=str(exc))


def try_clear(
    config_dir: Path, *, pg_restore: Path, explicit: Path | None = None, home: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> Path | None:
    """The user's next start: clear a pending refusal with a verified backup of this cluster. Returns the backup
    it accepted (recorded in the guard file), or None when the refusal stands. Raises :class:`PgRestoreUnavailable`
    when ``pg_restore`` cannot run."""
    record = pending_refusal(config_dir)
    if record is None:
        return None
    try:
        found = verified_backup_since(record, pg_restore=pg_restore, home=home, explicit=explicit, runner=runner)
    finally:
        with contextlib.suppress(OSError):  # remember which dumps were rejected, so the next call skips them
            _write_json(guard_path(config_dir), record)
    if found is not None:
        mark_cleared(config_dir, found)
        _log.info("legacy_chash_guard_cleared", backup=str(found))
    return found


# ── the marker that a store was seen past the deleting changesets ────────────────────────────────────────────────


def read_marker(config_dir: Path, pg_data: str) -> dict | None:
    try:
        marker = json.loads((config_dir / MARKER_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return marker if isinstance(marker, dict) and marker.get("pg_data") == pg_data else None


def write_marker(config_dir: Path, pg_data: str, evidence: str, *, now: datetime | None = None) -> None:
    """Remember that this cluster (keyed to its data directory) was SEEN safe: ``applied`` (both deleting changesets
    in the changelog) or ``no_changelog`` followed by a healthy engine. Best effort."""
    try:
        _write_json(config_dir / MARKER_FILENAME,
                    {"pg_data": pg_data, "evidence": evidence, "at": (now or datetime.now(UTC)).isoformat()})
    except OSError as exc:
        _log.warning("legacy_chash_guard_marker_not_written", error=str(exc))


def gate(config_dir: Path, run_sql: RunSql, *, pg_data: str = "") -> Verdict:
    """The supervisor's step before it spawns an engine. Proceeds, or raises :class:`LegacyChashRefused` (rows
    would be deleted, no backup yet) or :class:`GuardCannotAsk` (the cluster could not be asked, and no marker says
    it was seen safe). A refusal the user cleared with a verified backup lets the start go on: the rows are still
    there until the engine's first boot deletes them, which is what the backup was taken for. After a refuse verdict
    nothing fails open: a record or marker schema that cannot be written is logged and the refusal is raised anyway."""
    from nexus.redact import redact_credentials  # noqa: PLC0415 — deferred

    try:
        verdict = check(run_sql)
    except Exception as exc:  # noqa: BLE001 — every way of not getting an answer is one case
        marker = read_marker(config_dir, pg_data)
        if marker is not None:
            _log.warning("legacy_chash_guard_proceeding_on_marker", evidence=marker.get("evidence"),
                         error=redact_credentials(str(exc)))
            return Verdict(True, "could not ask; this store was seen past the deleting changesets", {}, "marker")
        raise GuardCannotAsk(redact_credentials(f"{type(exc).__name__}: {exc}")) from exc
    if verdict.proceed:
        if verdict.kind == "applied":
            write_marker(config_dir, pg_data, "applied")
        _release_sentinel(config_dir, run_sql)
        if verdict.kind == "applied":
            _spend_clearance(config_dir)
        return verdict
    if is_cleared(config_dir):
        _log.warning("legacy_chash_guard_proceeding_after_backup", counts=verdict.counts,
                     backup=(read_record(config_dir) or {}).get("cleared_by"))
        _release_sentinel(config_dir, run_sql)
        return Verdict(True, "a verified backup cleared the refusal", verdict.counts, "cleared", verdict.required)
    prior = pending_refusal(config_dir) or {}
    sentinel = prior.get("sentinel")
    if not sentinel:
        try:
            sentinel = create_sentinel(run_sql)
        except Exception as exc:  # noqa: BLE001 — still a refusal; no backup is accepted until a marker exists
            _log.error("legacy_chash_guard_sentinel_not_created", error=redact_credentials(str(exc)))
    try:
        record = record_refusal(config_dir, verdict, sentinel=sentinel)
    except Exception as exc:  # noqa: BLE001 — after a refuse verdict nothing fails open
        _log.error("legacy_chash_guard_record_not_written", error=str(exc))
        record = {"refused_at": datetime.now(UTC).isoformat(), "counts": verdict.counts,
                  "changeset": DELETING_CHANGESET, "sentinel": sentinel}
    raise LegacyChashRefused(refusal_text(record))


def bundled_run_sql(config_dir: Path, creds: dict[str, str], *, psql_runner=None) -> RunSql:
    """A RunSql against the bundled cluster as its bootstrap superuser (FORCE RLS binds the table owner too; wren)."""
    from nexus.redact import redact_credentials  # noqa: PLC0415 — deferred

    def run(sql: str) -> str:
        # Everything that can fail (no bundle, no psql, an import) sits inside the call, so it reads as "could not ask".
        from nexus.db.pg_provision import (  # noqa: PLC0415 — deferred: heavy import
            NEXUS_DB_NAME,
            SUPERUSER_PASS_KEY,
            bootstrap_superuser,
            discover_pg_binaries,
        )
        from nexus.health import _run_psql  # noqa: PLC0415 — the shared psql helper

        psql = discover_pg_binaries(config_dir).psql
        port = int(creds.get("PG_PORT", "0") or 0)
        done = _run_psql(psql, "127.0.0.1", port, NEXUS_DB_NAME, bootstrap_superuser(), creds.get(SUPERUSER_PASS_KEY, ""),
                         sql, psql_runner=psql_runner, timeout=120)
        if done.returncode != 0:
            stderr = done.stderr if isinstance(done.stderr, str) else (done.stderr or b"").decode("utf-8", "replace")
            raise RuntimeError(f"legacy chash guard query failed: {redact_credentials(stderr.strip()[-300:])}")
        return done.stdout if isinstance(done.stdout, str) else done.stdout.decode("utf-8", "replace")

    return run


def pg_restore_path(config_dir: Path) -> Path:
    """The bundled ``pg_restore``; raises :class:`PgRestoreUnavailable` when no bundle can be found."""
    try:
        from nexus.db.pg_provision import discover_pg_binaries  # noqa: PLC0415 — deferred: heavy import

        return discover_pg_binaries(config_dir).bin_dir / ("pg_restore.exe" if os.name == "nt" else "pg_restore")
    except Exception as exc:  # noqa: BLE001 — a missing bundle is the same "cannot run pg_restore"
        raise PgRestoreUnavailable(str(exc)) from exc
