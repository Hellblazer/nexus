# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guard: an engine never starts on a store whose boot would delete legacy chash rows until a backup exists (nexus-f3yqd).

The engine's changeset ``hygiene-001-5`` deletes every ``nexus.chunks`` row whose chash is not 32 bytes (and the
manifest rows that reference them); ``hygiene-001-8`` deletes every ``nexus.catalog_document_chunks`` row whose chash is
not 32 bytes. The 32-byte cutover has no way back, so such a row's content cannot be resolved. A store that last ran an
engine before 0.1.91 can still hold them, and the next engine start would delete them at boot. Sam (2026-11-11): no
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
   written to ``legacy-chash-guard.json`` in the config directory (with the data directory and ``system_identifier`` it
   is about), Postgres stays up so ``nx backup`` can dump it, and the start exits 75. A dump clears the refusal only
   when ``pg_restore -l`` lists that marker schema (so it is a dump of this cluster taken after the refusal) and the
   table data the engine would delete, and ``pg_restore -a`` reads it to its end (``legacy_chash_backup``). While the
   refusal stands, the supervisor's wait keeps the marker schema in place and retires the record when the cluster is
   another one or no longer at risk.

Not being able to ask (Postgres in crash recovery, a timeout, no superuser password, a role that cannot see every row)
is not a refusal and demands no backup: the supervisor waits and asks again, and says why in
``legacy-chash-guard-wait.json`` (shown by the CLI). The one exception is a marker, keyed to the cluster's data directory and
written only after a gate run SAW the deleting changesets applied, or saw no changelog and then a healthy engine.

Nothing here runs ``nx backup`` (its pin forbids it) or imports its module. Specified by wren (engine).
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import structlog

from nexus.daemon.legacy_chash_backup import (
    MANIFEST as MANIFEST,
    MAX_DUMPS_PER_ATTEMPT as MAX_DUMPS_PER_ATTEMPT,
    SENTINEL_NAME,
    SENTINEL_PREFIX,
    PgRestoreUnavailable as PgRestoreUnavailable,
    backup_dir as backup_dir,
    holds_required_data as holds_required_data,
    rejected_paths,
    verified_backup_since as verified_backup_since,
)

_log = structlog.get_logger(__name__)

GUARD_FILENAME: str = "legacy-chash-guard.json"
MARKER_FILENAME: str = "legacy-chash-guard-seen.json"
WAIT_FILENAME: str = "legacy-chash-guard-wait.json"
DELETING_CHANGESET: str = "hygiene-001-5"
MANIFEST_CHANGESET: str = "hygiene-001-8"
EXIT_REFUSED: int = 75
SHARDED_CHUNKS: tuple[str, ...] = ("chunks_384", "chunks_768", "chunks_1024")
_CHASH_TABLES: tuple[str, ...] = ("chunks", *SHARDED_CHUNKS, MANIFEST)
#: A count stops here (a huge store must not run the query into its timeout); a count at the cap reads "at least".
COUNT_CAP: int = 1_000_000
#: A wait status older than this is from a supervisor that is gone.
WAIT_FRESH_S: float = 30.0

#: Runs one SQL statement and returns its tuple-only output (psql -tA); raises on an SQL or connection error.
RunSql = Callable[[str], str]


class LegacyChashRefused(RuntimeError):
    """The engine was not started: legacy rows would be deleted and no verified backup cleared it."""


class GuardCannotAsk(RuntimeError):
    """The cluster could not be asked (crash recovery, a timeout, no superuser password). Not a refusal: the
    supervisor waits and asks again; no backup is demanded for it."""


@dataclass(frozen=True)
class Verdict:
    """``proceed`` with a reason, or a refusal with the counts per table.

    ``kind`` says what the check saw: ``no_changelog``, ``no_tables``, ``applied`` (both deleting changesets ran),
    ``clean`` (nothing to delete yet), ``refuse``, ``cleared`` or ``marker``. ``required`` is what a backup must hold
    as table data: ``(table, prefix_ok)`` pairs, ``prefix_ok`` for a partitioned parent whose data sits in leaves.
    ``cluster`` is the cluster's ``system_identifier``: what a record is bound to."""

    proceed: bool
    reason: str
    counts: dict[str, int]
    kind: str = ""
    required: tuple[tuple[str, bool], ...] = field(default_factory=tuple)
    cluster: str = ""


def _bool(out: str) -> bool:
    """psql -tA prints exactly ``t`` or ``f`` for a boolean; anything else (empty output, a garbled line) is not an
    answer, and a guard that cannot tell never reads it as "no"."""
    answer = out.strip()
    if answer not in ("t", "f"):
        raise ValueError(f"expected t or f from the database, got {answer[:40]!r}")
    return answer == "t"


def _system_identifier(run_sql: RunSql) -> str:
    sid = run_sql("SELECT system_identifier FROM pg_control_system()").strip()
    if not sid.isdigit():
        raise ValueError(f"expected the cluster's system identifier, got {sid[:40]!r}")
    return sid


def _predicate(dtype: str) -> str:
    """The rows the engine's delete reaches, by the column's type (hygiene-001-5/-8, and rdr180-3 for text)."""
    if dtype == "bytea":
        return "(octet_length(chash) <> 32 OR chash IS NULL)"
    width = ("CASE WHEN chash ~ '^[0-9a-f]+$' AND length(chash) % 2 = 0 THEN length(chash) / 2 "
             "ELSE octet_length(chash) END")
    return f"({width} <> 32 OR chash IS NULL)"


def _count(run_sql: RunSql, table: str, dtype: str) -> int:
    """Rows the engine would delete, bounded: an EXISTS first (most stores answer 0 at once), then a count that stops
    at ``COUNT_CAP`` so a huge store cannot run the query into its timeout."""
    where = f"FROM nexus.{table} WHERE {_predicate(dtype)}"
    if not _bool(run_sql(f"SELECT EXISTS (SELECT 1 {where})")):
        return 0
    return int(run_sql(f"SELECT count(*) FROM (SELECT 1 {where} LIMIT {COUNT_CAP}) capped").strip())


def check(run_sql: RunSql) -> Verdict:
    """Ask the cluster whether starting an engine would delete legacy rows."""
    if not _bool(run_sql("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")):
        # Without it the chunk table's row-level security (and the catalog views) hide rows: every count reads 0.
        raise RuntimeError("the guard's database role is neither superuser nor BYPASSRLS, so it cannot see every row")
    sid = _system_identifier(run_sql)
    if not _bool(run_sql("SELECT to_regclass('public.databasechangelog') IS NOT NULL")):
        return Verdict(True, "no engine has run against this store (no changelog)", {}, "no_changelog", cluster=sid)
    ran = {s.strip() for s in run_sql(
        "SELECT id FROM public.databasechangelog "
        f"WHERE id IN ('{DELETING_CHANGESET}', '{MANIFEST_CHANGESET}')"
    ).splitlines() if s.strip()}
    chunks_done = DELETING_CHANGESET in ran
    manifest_done = chunks_done and MANIFEST_CHANGESET in ran
    if manifest_done:
        return Verdict(True, f"{DELETING_CHANGESET} and {MANIFEST_CHANGESET} already ran", {}, "applied", cluster=sid)
    types: dict[str, str] = {}
    names = ", ".join(f"'{t}'" for t in _CHASH_TABLES)
    for line in run_sql(  # the catalogs, not information_schema: that view hides what the role cannot touch
        "SELECT c.relname || '|' || format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'nexus' AND a.attname = 'chash' AND NOT a.attisdropped AND c.relkind IN ('r', 'p') "
        f"AND c.relname IN ({names})"
    ).splitlines():
        table, _, dtype = line.strip().partition("|")
        if table:
            types[table] = dtype
    chunk_tables = ["chunks"] if "chunks" in types else [t for t in SHARDED_CHUNKS if t in types]
    if not chunk_tables and MANIFEST not in types:
        return Verdict(True, "no chunk tables: a store from before the engine's schema", {}, "no_tables", cluster=sid)
    targets = ([] if chunks_done else chunk_tables) + ([MANIFEST] if MANIFEST in types else [])
    counts = {table: _count(run_sql, table, types[table]) for table in targets}
    if any(counts.values()):
        required = tuple((t, t == "chunks") for t in targets if t == MANIFEST or t in SHARDED_CHUNKS
                         or counts.get(t))
        return Verdict(False, f"{DELETING_CHANGESET} would delete these rows at engine start", counts, "refuse",
                       required, sid)
    return Verdict(True, "no row with a legacy-width chash", counts, "clean", cluster=sid)


def guard_path(config_dir: Path) -> Path:
    return config_dir / GUARD_FILENAME


def _write_json(path: Path, record: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def record_refusal(
    config_dir: Path, verdict: Verdict, *, sentinel: str | None = None, pg_data: str = "",
    now: datetime | None = None,
) -> dict:
    """Write (or keep) the refusal record. An existing unresolved record keeps its first ``refused_at``, its marker
    schema and its rejected dumps, so a backup taken after the first refusal still counts on every later start. The
    record names the cluster it is about (data directory and ``system_identifier``)."""
    prior = pending_refusal(config_dir) or {}
    record = {
        "refused_at": prior.get("refused_at") or (now or datetime.now(UTC)).isoformat(),
        "counts": verdict.counts, "changeset": DELETING_CHANGESET,
        "sentinel": prior.get("sentinel") or sentinel,
        "required": [list(r) for r in verdict.required],
        "rejected": prior.get("rejected") or {},
        "cluster": {"pg_data": pg_data, "system_identifier": verdict.cluster},
    }
    _write_json(guard_path(config_dir), record)
    return record


def _cluster_matches(record: dict, pg_data: str, sid: str) -> bool:
    """Is this record about the cluster in front of us? The ``system_identifier`` decides when both sides have one
    (a moved data directory is the same cluster); else the data directory; a record that names neither matches."""
    bound = record.get("cluster") or {}
    if bound.get("system_identifier") and sid:
        return bound["system_identifier"] == sid
    if bound.get("pg_data") and pg_data:
        return bound["pg_data"] == pg_data
    return True


# ── the marker schema ─────────────────────────────────────────────────────────────────────────────────────────────

_SENTINEL_COMMENT = "nexus legacy-chash guard: a backup of this store must hold this schema"


def create_sentinel(run_sql: RunSql) -> str:
    """Create the marker a backup must contain, as the superuser; returns its name. An empty schema: ``pg_dump -Fc``
    lists it in the archive's table of contents, which is all ``pg_restore -l`` has to show, and it carries no data
    and no table (nothing here is persistent state: it is dropped when the refusal clears)."""
    name = f"{SENTINEL_PREFIX}{secrets.token_hex(6)}"
    _create_schema(run_sql, name)
    return name


def _create_schema(run_sql: RunSql, name: str) -> None:
    """The schema and its comment in ONE statement string, which psql sends as one implicit transaction: a failure
    leaves no schema behind under a name nobody recorded."""
    run_sql(f"CREATE SCHEMA {name}; COMMENT ON SCHEMA {name} IS '{_SENTINEL_COMMENT}'")


def ensure_sentinel(run_sql: RunSql, name: str) -> None:
    """The record's marker schema exists in the cluster (it is gone after a restore-swap or a hand clean-up). One
    probe when it is there; the wait runs this every few seconds."""
    if not _valid_sentinel(name):  # the name came from a file: never splice anything else into SQL
        return
    if not _bool(run_sql(f"SELECT to_regnamespace('{name}') IS NOT NULL")):
        _create_schema(run_sql, name)


def _valid_sentinel(name: object) -> bool:
    return isinstance(name, str) and SENTINEL_NAME.fullmatch(name) is not None


def drop_sentinel(run_sql: RunSql, name: str) -> None:
    if _valid_sentinel(name):
        run_sql(f"DROP SCHEMA IF EXISTS {name} CASCADE")


def _release_sentinel(config_dir: Path, run_sql: RunSql) -> None:
    """After a clear, drop the marker schema once (best effort; retried by the next gate run)."""
    record = read_record(config_dir)
    name = (record or {}).get("sentinel")
    if not record or not record.get("cleared_at") or not name or record.get("sentinel_dropped"):
        return
    try:
        drop_sentinel(run_sql, name)
        _update_record(config_dir, record.get("refused_at"), sentinel_dropped=True)
    except Exception as exc:  # noqa: BLE001 — a leftover empty schema is harmless; the next gate run tries again
        _log.warning("legacy_chash_guard_sentinel_not_dropped", sentinel=name, error=str(exc))


# ── the record: states and updates ───────────────────────────────────────────────────────────────────────────────


def mark_cleared(config_dir: Path, backup: Path, refused_at: object, *, now: datetime | None = None) -> bool:
    """Keep the record as history: when and by which backup the refusal cleared. The write lands only on the pending
    refusal ``refused_at`` the backup was checked against: a record that was replaced, retired or already cleared
    while the dump was read is left as it is. Returns whether it wrote."""
    return _update_record(config_dir, refused_at, only_pending=True,
                          cleared_at=(now or datetime.now(UTC)).isoformat(), cleared_by=str(backup))


def _label(table: str, n: int) -> str:
    noun = {"chunks": "chunk row", MANIFEST: "manifest row"}.get(table, f"chunk row in {table}")
    return noun if n == 1 else noun.replace(" row", " rows", 1)


def _rows(n: int) -> str:
    return f"at least {n:,}" if n >= COUNT_CAP else f"{n}"


def refusal_text(record: dict, *, home: Path | None = None) -> str:
    """What the user reads: what is at risk and why, the command to run, the marker schema's purpose, and which dumps
    were looked at and not accepted."""
    counts = record.get("counts") or {}
    rows = ", ".join(f"{_rows(n)} {_label(t, n)}" for t, n in counts.items() if n)
    sentinel = record.get("sentinel")
    marker = (
        f"The checks: this refusal created the empty schema {sentinel} in your nexus database, and a backup counts only "
        "if it contains that schema, which proves it is a dump of this store taken after the refusal (a dump of "
        "another store, or an older one, is not accepted); it must hold the chunk and manifest data; and nexus reads "
        "the whole dump back before it goes on.\n"
        if sentinel else
        "The checks: the marker schema that binds a backup to this store could not be created yet; the supervisor "
        "retries it every few seconds, and no backup is accepted until it exists.\n"
    )
    tried = rejected_paths(record)
    checked = (
        "Checked and not accepted: " + "; ".join(f"{path} ({why})" for path, why in tried[-5:]) + ".\n" if tried else ""
    )
    return (
        f"Not starting the engine. Your database holds {rows} that the engine's next start deletes for good: they use "
        "an old key format the current engine cannot read, so those chunks and their manifest rows could be recovered "
        "only from a copy.\n"
        "Make the copy first: run `nx backup` (Postgres is left running for it; it writes a verified dump to "
        f"{backup_dir(home)}/).\nBackup guide: https://github.com/Hellblazer/nexus/blob/main/docs/operations/backup-restore.md\n"
        "Then start again (`nx daemon service start`, or `nx upgrade`): the start goes on once a dump written after "
        f"this refusal ({record.get('refused_at')}) passes the checks below. A dump written elsewhere "
        "(`nx backup --to PATH`): `nx daemon service start --legacy-backup PATH`. Reading a large dump to its end can "
        "take minutes; `nx daemon service start` has no time limit, `nx upgrade` does.\n"
        f"{marker}{checked}"
        "The refusal ends when a backup passes those checks, or when a check finds nothing left to delete. "
        f"(The deleting migration is {record.get('changeset', DELETING_CHANGESET)}.)"
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
    live = record and record.get("refused_at") and not record.get("cleared_at") and not record.get("retired_at")
    return record if live else None


def is_cleared(config_dir: Path) -> bool:
    record = read_record(config_dir)
    return bool(record and record.get("cleared_at") and not record.get("spent_at") and not record.get("retired_at"))


def _update_record(config_dir: Path, refused_at: object, *, only_pending: bool = False, **fields: object) -> bool:
    """Re-read the record and write ``fields`` into it, only when it is still the refusal ``refused_at`` (a record
    another start replaced, retired or cleared is not overwritten from a stale copy). Returns whether it wrote."""
    record = read_record(config_dir)
    if not record or record.get("refused_at") != refused_at or (
        only_pending and (record.get("cleared_at") or record.get("retired_at"))
    ):
        return False
    record.update(fields)
    try:
        _write_json(guard_path(config_dir), record)
    except OSError as exc:
        _log.warning("legacy_chash_guard_record_not_updated", error=str(exc))
        return False
    return True


def _spend_clearance(config_dir: Path) -> None:
    """The deleting changesets were seen applied: the clearance did its job. A later refusal (a restored older
    database under this config directory) needs a backup of its own. Best effort."""
    record = read_record(config_dir)
    if record and record.get("cleared_at") and not record.get("spent_at"):
        _update_record(config_dir, record.get("refused_at"), spent_at=datetime.now(UTC).isoformat())


def retire_record(config_dir: Path, reason: str, run_sql: RunSql | None = None) -> None:
    """A record that no longer describes this cluster or this risk is retired, never applied: it stays on disk as
    history. ``run_sql`` (when the marker schema is in the cluster at hand) drops the schema."""
    record = read_record(config_dir)
    if not record or record.get("retired_at"):
        return
    name = record.get("sentinel")
    if run_sql is not None and name and not record.get("sentinel_dropped"):
        try:
            drop_sentinel(run_sql, name)
        except Exception as exc:  # noqa: BLE001 — a leftover empty schema is harmless
            _log.warning("legacy_chash_guard_sentinel_not_dropped", sentinel=name, error=str(exc))
    _update_record(config_dir, record.get("refused_at"), retired_at=datetime.now(UTC).isoformat(),
                   retired_reason=reason)
    _log.warning("legacy_chash_guard_record_retired", reason=reason)


def try_clear(
    config_dir: Path, *, pg_restore: Path, explicit: Path | None = None, home: Path | None = None,
    runner=None,
) -> Path | None:
    """The user's next start: clear a pending refusal with a verified backup of this cluster. Returns the backup
    it accepted (recorded in the guard file), or None when the refusal stands. Raises :class:`PgRestoreUnavailable`
    when ``pg_restore`` cannot run."""
    record = pending_refusal(config_dir)
    if record is None:
        return None
    try:
        found = verified_backup_since(record, pg_restore=pg_restore, home=home, explicit=explicit, runner=runner)
    finally:  # remember which dumps were rejected, so the next call skips them, without undoing a concurrent clear
        current = read_record(config_dir)
        if current and current.get("refused_at") == record.get("refused_at"):
            _update_record(config_dir, record.get("refused_at"),
                           rejected={**(current.get("rejected") or {}), **record.get("rejected", {})})
    if found is not None and mark_cleared(config_dir, found, record.get("refused_at")):
        _log.info("legacy_chash_guard_cleared", backup=str(found))
        return found
    if found is not None:
        _log.warning("legacy_chash_guard_clearance_not_written", backup=str(found),
                     reason="the refusal changed while the dump was read")
    return None


def reconcile_pending(config_dir: Path, run_sql: RunSql, pg_data: str, *, recheck: bool) -> str:
    """Keep a pending refusal satisfiable and true. Returns ``none`` (no pending record), ``retired`` or ``pending``.

    A record about another cluster (a store wiped or re-initialised under this config directory, a restore-swap) is
    retired. The marker schema is (re)created when it is missing, so a backup can hold it. With ``recheck`` the check
    runs again, and a store no longer at risk (the rows were removed by hand) retires the record. Errors propagate: the
    caller is best-effort."""
    record = pending_refusal(config_dir)
    if record is None:
        return "none"
    sid = _system_identifier(run_sql)
    if not _cluster_matches(record, pg_data, sid):
        retire_record(config_dir, "the refusal was recorded for another cluster")
        return "retired"
    name = record.get("sentinel")
    if not name:
        name = create_sentinel(run_sql)
    else:
        ensure_sentinel(run_sql, name)
    _update_record(config_dir, record.get("refused_at"), only_pending=True, sentinel=name,
                   cluster={"pg_data": pg_data, "system_identifier": sid})
    if recheck:
        verdict = check(run_sql)
        if verdict.proceed:
            retire_record(config_dir, f"no longer at risk: {verdict.reason}", run_sql)
            return "retired"
    return "pending"


def wait_round(
    config_dir: Path, creds: dict[str, str], state: dict, *, recheck_every_s: float = 60.0,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """One round of the supervisor's wait for a backup: :func:`reconcile_pending`, with the full recheck once every
    ``recheck_every_s``. Best effort: ``unknown`` when the cluster could not be asked."""
    from nexus.redact import redact_credentials  # noqa: PLC0415 — deferred

    try:
        now = clock()
        due = now - state.get("checked", float("-inf")) >= recheck_every_s
        outcome = reconcile_pending(config_dir, bundled_run_sql(config_dir, creds), creds.get("PG_DATA", "").strip(),
                                    recheck=due)
        if due:
            state["checked"] = now
        return outcome
    except Exception as exc:  # noqa: BLE001 — the wait goes on; the next round asks again
        _log.warning("legacy_chash_guard_wait_round_failed", error=redact_credentials(str(exc)))
        return "unknown"


# ── the wait the user can see ────────────────────────────────────────────────────────────────────────────────────


def describe_cannot_ask(exc: BaseException) -> str:
    """Why the guard could not read the database, in plain words (the raw error, redacted, goes to the log)."""
    from nexus.redact import redact_credentials  # noqa: PLC0415 — deferred

    text = redact_credentials(str(exc))
    low = text.lower()
    if isinstance(exc, subprocess.TimeoutExpired) or "timed out" in low:
        return "a query to the database did not finish in time"
    if isinstance(exc, (FileNotFoundError, ImportError)) or "pgbinarynotfound" in type(exc).__name__.lower():
        return "the Postgres tools nexus bundles could not be run"
    if "cannot see every row" in low:
        return "nexus's database account cannot see every row; it has to be a superuser"
    if "password authentication failed" in low or "no password supplied" in low or "fe_sendauth" in low:
        return "Postgres did not accept the superuser password in pg_credentials"
    if "does not exist" in low and "role" in low:
        return "the database account nexus reads with does not exist in this cluster"
    if "starting up" in low or "recovery" in low or "shutting down" in low:
        return "Postgres is still starting up or recovering"
    if "connection refused" in low or "could not connect" in low or "no such file" in low:
        return "Postgres is not accepting connections"
    return f"the check failed ({text.strip().splitlines()[-1][:160] if text.strip() else type(exc).__name__})"


def waiting_text(cause: str, log_path: Path | str) -> str:
    return (
        "The engine has not started yet: nexus could not read your database to check it for rows that its first "
        f"start would delete for good ({cause}). No backup is needed while it waits. It asks again every few "
        "seconds and starts once the check can read the database. If this lasts more than a minute or two, check "
        f"that Postgres is healthy (`nx doctor`) and the superuser password in pg_credentials, and read {log_path}."
    )


def write_wait_status(config_dir: Path, cause: str, log_path: Path | str) -> str:
    """Record why the engine is waiting (the CLI shows it); returns the text. Best effort."""
    prior = _read_wait(config_dir) or {}
    status = {"cause": cause, "since": prior.get("since") or datetime.now(UTC).isoformat(), "log": str(log_path)}
    try:
        _write_json(config_dir / WAIT_FILENAME, status)
    except OSError as exc:
        _log.warning("legacy_chash_guard_wait_status_not_written", error=str(exc))
    return waiting_text(cause, log_path)


def _read_wait(config_dir: Path) -> dict | None:
    try:
        status = json.loads((config_dir / WAIT_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return status if isinstance(status, dict) else None


def clear_wait_status(config_dir: Path) -> None:
    with contextlib.suppress(OSError):
        (config_dir / WAIT_FILENAME).unlink()


def wait_status_text(config_dir: Path) -> str | None:
    """The reason a live supervisor is waiting, or None (no status, or one older than ``WAIT_FRESH_S``)."""
    status = _read_wait(config_dir)
    try:
        fresh = time.time() - (config_dir / WAIT_FILENAME).stat().st_mtime <= WAIT_FRESH_S
    except OSError:
        return None
    return waiting_text(status.get("cause", "unknown"), status.get("log", "")) if status and fresh else None


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
    it was seen safe). A refusal the user cleared with a verified backup of this cluster lets the start go on: the
    rows are still there until the engine's first boot deletes them, which is what the backup was taken for. A record
    about another cluster is retired, never applied. After a refuse verdict nothing fails open: a record or marker
    schema that cannot be written is logged and the refusal is raised anyway."""
    from nexus.redact import redact_credentials  # noqa: PLC0415 — deferred

    try:
        verdict = check(run_sql)
    except Exception as exc:  # noqa: BLE001 — every way of not getting an answer is one case
        marker = read_marker(config_dir, pg_data)
        if marker is not None:
            _log.warning("legacy_chash_guard_proceeding_on_marker", evidence=marker.get("evidence"),
                         error=redact_credentials(str(exc)))
            clear_wait_status(config_dir)
            return Verdict(True, "could not ask; this store was seen past the deleting changesets", {}, "marker")
        _log.warning("legacy_chash_guard_could_not_ask", error=redact_credentials(f"{type(exc).__name__}: {exc}"))
        raise GuardCannotAsk(describe_cannot_ask(exc)) from exc
    clear_wait_status(config_dir)
    if verdict.proceed:
        if verdict.kind == "applied":
            write_marker(config_dir, pg_data, "applied")
        if pending_refusal(config_dir) is not None:
            retire_record(config_dir, f"no longer at risk: {verdict.reason}", run_sql)
        _release_sentinel(config_dir, run_sql)
        if verdict.kind == "applied":
            _spend_clearance(config_dir)
        return verdict
    record = read_record(config_dir) or {}
    if (is_cleared(config_dir) or pending_refusal(config_dir) is not None) and not _cluster_matches(
        record, pg_data, verdict.cluster
    ):
        retire_record(config_dir, "the refusal was recorded for another cluster")
    if is_cleared(config_dir):
        _log.warning("legacy_chash_guard_proceeding_after_backup", counts=verdict.counts,
                     backup=(read_record(config_dir) or {}).get("cleared_by"))
        _release_sentinel(config_dir, run_sql)
        return Verdict(True, "a verified backup cleared the refusal", verdict.counts, "cleared", verdict.required)
    prior = pending_refusal(config_dir) or {}
    sentinel = prior.get("sentinel")
    try:
        if sentinel:
            ensure_sentinel(run_sql, sentinel)
        else:
            sentinel = create_sentinel(run_sql)
    except Exception as exc:  # noqa: BLE001 — still a refusal; no backup is accepted until a marker exists
        _log.error("legacy_chash_guard_sentinel_not_created", error=redact_credentials(str(exc)))
    try:
        record = record_refusal(config_dir, verdict, sentinel=sentinel, pg_data=pg_data)
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
