# SPDX-License-Identifier: AGPL-3.0-or-later
"""The RDR-231 pair query, run as the local cluster's superuser (nexus-bivz3.28).

When the note-URI backfill (changeset ``note-uri-001-1``) halts the engine, the operator has to list the pairs
it names: one note stored under both ``chroma://`` and ``nx-note://`` as two live catalog documents. The engine
is down, so the query cannot go through it; it runs in the local cluster as a role that bypasses row-level
security. That role is ``$USER`` on Linux and macOS and ``nx_`` plus 16 hex of the SHA-256 of the service SID on
Windows (:func:`nexus.db.pg_provision.bootstrap_superuser` resolves both), and its password is in
``pg_credentials``. This module resolves the role, the port, the bundled ``psql`` and the password, runs the one
query, and hands the password to ``psql`` only as ``PGPASSWORD`` in that child's environment: never on argv, in
the output or in an error message.

``PAIR_SQL`` is the guard's own test and is the query ``docs/migration-runbook.md`` prints for the cloud
operator; ``tests/test_service_note_uri_pairs.py`` fails when the two drift.
"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import structlog

from nexus.db.pg_provision import (
    _PSQL_TIMEOUT_S,
    CREDENTIALS_FILENAME,
    NEXUS_DB_NAME,
    SUPERUSER_PASS_KEY,
    PgBinaries,
    PgBinaryNotFoundError,
    PgRootUserError,
    PgSmartAppControlError,
    _on_windows,
    _read_credentials,
    _run,
    bootstrap_superuser,
    discover_pg_binaries,
)

_log = structlog.get_logger(__name__)

#: The runbook page that holds the steps, for a message (a user with the wheel has no docs/ tree).
RUNBOOK_URL = "https://github.com/Hellblazer/nexus/blob/main/docs/migration-runbook.md"

#: The changeset the backfill runs as. Liquibase's own tables are in ``public``, not ``nexus``.
CHANGESET_ID = "note-uri-001-1"

PAIR_SQL = """\
WITH o AS (
  SELECT tenant_id, tumbler, source_uri,
         CASE WHEN starts_with(source_uri, 'chroma://')
              THEN 'nx-note://' || substr(source_uri, 10)
              ELSE regexp_replace(source_uri, '^(nxexp://[^/]*)/chroma://', '\\1/nx-note://') END AS new_uri,
         CASE WHEN starts_with(source_uri, 'chroma://')
              THEN split_part(substr(source_uri, 10), '/', 1)
              ELSE substring(source_uri from '^nxexp://[^/]*/chroma://([^/]*)') END AS coll
    FROM nexus.catalog_documents
   WHERE deleted_at IS NULL
     AND (starts_with(source_uri, 'chroma://') OR source_uri ~ '^nxexp://[^/]*/chroma://'))
SELECT o.tenant_id, o.tumbler AS chroma_row, t.tumbler AS nx_note_row, o.source_uri
  FROM o
  JOIN nexus.catalog_documents t
    ON t.tenant_id = o.tenant_id AND t.deleted_at IS NULL AND t.source_uri = o.new_uri
 WHERE NOT (starts_with(o.coll, 'rdr__') OR starts_with(o.coll, 'docs__') OR starts_with(o.coll, 'code__'))
 ORDER BY o.tenant_id, o.tumbler;"""

CHANGESET_SQL = f"SELECT exectype FROM public.databasechangelog WHERE id = '{CHANGESET_ID}'"

#: ``source_uri`` is the last column and the only one that could hold the separator, so a row splits on the first three.
_SEP = "|"
_COLUMNS = ("tenant_id", "chroma_row", "nx_note_row", "source_uri")

_FALLBACK_ROLE = "postgres"

#: ``run(argv, env)`` returns psql's stdout and raises :class:`subprocess.CalledProcessError` on a failure.
PsqlRun = Callable[[Sequence[str], "dict[str, str] | None"], str]


class NoteUriPairsError(Exception):
    """The pair query could not run. The message is the whole explanation for the operator; it never carries a secret."""


@dataclass(frozen=True)
class Pair:
    tenant_id: str
    chroma_row: str
    nx_note_row: str
    source_uri: str


@dataclass(frozen=True)
class PairsReport:
    role: str
    port: int
    pairs: tuple[Pair, ...]
    #: ``EXECUTED`` and so on from ``public.databasechangelog``; ``""`` when the changeset has not run; None when
    #: the table could not be read (a virgin database).
    changeset: str | None

    def lines(self) -> list[str]:
        out = [f"RDR-231 note-URI pairs in the local cluster (127.0.0.1:{self.port}, role {self.role}): {len(self.pairs)}"]
        if self.pairs:
            out.append("\t".join(_COLUMNS))
            out.extend("\t".join((p.tenant_id, p.chroma_row, p.nx_note_row, p.source_uri)) for p in self.pairs)
        if self.changeset is None:
            out.append(f"Changeset {CHANGESET_ID}: public.databasechangelog could not be read")
        elif self.changeset:
            out.append(f"Changeset {CHANGESET_ID}: {self.changeset}")
        else:
            out.append(f"Changeset {CHANGESET_ID}: not run")
        return out


def _default_run(argv: Sequence[str], env: dict[str, str] | None) -> str:
    kwargs = {} if env is None else {"env": env}
    return _run(list(argv), timeout=_PSQL_TIMEOUT_S, **kwargs).stdout


def _psql_argv(bins: PgBinaries, port: int, role: str, dbname: str, sql: str) -> list[str]:
    # -w never prompts; -X ignores a psqlrc; the password is not here, it is PGPASSWORD in the child's environment.
    return [
        str(bins.psql), "-X", "-w", "-h", "127.0.0.1", "-p", str(port), "-U", role, "-d", dbname,
        "-v", "ON_ERROR_STOP=1", "-t", "-A", "-F", _SEP, "-c", sql,
    ]


def _parse(stdout: str) -> tuple[Pair, ...]:
    rows = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split(_SEP, 3)
        if len(parts) != 4:
            raise NoteUriPairsError(f"psql returned a row that is not four columns: {line[:120]!r}")
        rows.append(Pair(*parts))
    return tuple(rows)


def _scrub(text: str, password: str) -> str:
    return text.replace(password, "***") if password else text


def _failure(exc: subprocess.CalledProcessError, *, role: str, port: int, password: str) -> NoteUriPairsError:
    stderr = _scrub((exc.stderr or "").strip(), password)
    tail = stderr[-400:]
    low = stderr.lower()
    if "connection refused" in low or "could not connect" in low or "is the server running" in low:
        return NoteUriPairsError(
            f"the local PostgreSQL cluster is not accepting connections on 127.0.0.1:{port}. A halted engine "
            "leaves the cluster running; if the cluster is down, `nx daemon service start` starts it.\n" + tail
        )
    if "password authentication failed" in low or "no password supplied" in low or "fe_sendauth" in low:
        recorded = "the recorded" if password else f"no {SUPERUSER_PASS_KEY} is recorded in pg_credentials, so no"
        return NoteUriPairsError(
            f"role {role} was refused: {recorded} password was used. nx reads the password from "
            f"{SUPERUSER_PASS_KEY} in pg_credentials; restore it from a backup of that file or see "
            f"step 1 of 'If the RDR-231 note-URI backfill halts startup' at {RUNBOOK_URL}\n" + tail
        )
    if "does not exist" in low and "relation" in low:
        return NoteUriPairsError(
            "the cluster has no nexus.catalog_documents table, so the engine has not migrated this database "
            "(nothing to list).\n" + tail
        )
    return NoteUriPairsError(f"psql as role {role} on 127.0.0.1:{port} failed:\n{tail}")


def _role_missing(exc: subprocess.CalledProcessError) -> bool:
    low = (exc.stderr or "").lower()
    return 'role "' in low and "does not exist" in low


def list_note_uri_pairs(
    config_dir: Path,
    *,
    platform: str | None = None,
    identity: Callable[[], str] | None = None,
    bins: PgBinaries | None = None,
    run: PsqlRun | None = None,
    dbname: str = NEXUS_DB_NAME,
) -> PairsReport:
    """Run the pair query in the local cluster under *config_dir*; raise :class:`NoteUriPairsError` on any failure.

    *platform*, *identity*, *bins*, *run* and *dbname* are test seams (the engine substrate's database is not
    named ``nexus``); production passes none of them.
    """
    creds_path = config_dir / CREDENTIALS_FILENAME
    try:
        creds = _read_credentials(creds_path)
    except (OSError, ValueError):
        raise NoteUriPairsError(
            f"no readable {CREDENTIALS_FILENAME} in {config_dir}: there is no nx-managed local cluster here. "
            "On a managed or cloud install there is none: run the query as the operator's diagnostic role"
            f"; the Cloud steps of 'If the RDR-231 note-URI backfill halts startup' are at {RUNBOOK_URL}"
        ) from None
    port_text = creds.get("PG_PORT", "")
    if not port_text.isdigit():
        raise NoteUriPairsError(f"{creds_path} has no usable PG_PORT: there is no local cluster to ask")
    port = int(port_text)
    password = creds.get(SUPERUSER_PASS_KEY, "")

    if bins is None:
        try:
            bins = discover_pg_binaries()
        except PgBinaryNotFoundError as exc:
            raise NoteUriPairsError(f"no PostgreSQL client to run the query with (nx never uses a host psql): {exc}") from None

    runner = run if run is not None else _default_run
    env = {**os.environ, "PGPASSWORD": password} if password else None
    role = bootstrap_superuser(platform=platform, identity=identity)

    def attempt(as_role: str, sql: str) -> str:
        return runner(_psql_argv(bins, port, as_role, dbname, sql), env)

    try:
        try:
            stdout = attempt(role, PAIR_SQL)
        except subprocess.CalledProcessError as exc:
            # A cluster provisioned from a process with neither USER nor LOGNAME (cron, a service manager) has the
            # superuser `postgres` (bootstrap_superuser's POSIX fallback). Windows has one derivation and no fallback.
            if _on_windows(platform) or role == _FALLBACK_ROLE or not _role_missing(exc):
                raise
            role = _FALLBACK_ROLE
            stdout = attempt(role, PAIR_SQL)
        pairs = _parse(stdout)
        try:
            changeset: str | None = attempt(role, CHANGESET_SQL).strip()
        except subprocess.CalledProcessError:
            changeset = None
    except subprocess.CalledProcessError as exc:
        raise _failure(exc, role=role, port=port, password=password) from None
    except subprocess.TimeoutExpired:
        raise NoteUriPairsError(f"psql on 127.0.0.1:{port} did not answer within {_PSQL_TIMEOUT_S:.0f} s") from None
    except (PgRootUserError, PgSmartAppControlError) as exc:
        raise NoteUriPairsError(str(exc)) from None
    except OSError as exc:
        raise NoteUriPairsError(f"could not start psql ({type(exc).__name__}): {_scrub(str(exc), password)}") from None
    _log.debug("note_uri_pairs_listed", role=role, port=port, pairs=len(pairs), changeset=changeset)
    return PairsReport(role=role, port=port, pairs=pairs, changeset=changeset)
