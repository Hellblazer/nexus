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
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import structlog

from nexus.daemon.service_registry import ServiceRegistryError
from nexus.db.pg_bundle import extracted_bin_dir
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
    _port_accepting,
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

#: psql runs with ``-0 -z``: every field and every record ends in a NUL, which PostgreSQL text cannot hold, so a tab,
#: newline or U+2028 in a ``source_uri`` cannot move a column boundary.
_NUL = "\0"
_COLUMNS = ("tenant_id", "chroma_row", "nx_note_row", "source_uri")

_FALLBACK_ROLE = "postgres"

#: The TCP probe gets this many single-shot tries, this far apart, before the port is called closed: one missed
#: loopback accept on a loaded or EDR-filtered Windows box must not hard-fail the halted-engine case.
_PROBE_TRIES = 3
_PROBE_PAUSE_S = 0.5

#: ``run(argv, env)`` returns psql's stdout and raises :class:`subprocess.CalledProcessError` on a failure.
PsqlRun = Callable[[Sequence[str], "dict[str, str]"], str]


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


def _default_run(argv: Sequence[str], env: dict[str, str]) -> str:
    return _run(list(argv), timeout=_PSQL_TIMEOUT_S, env=env).stdout


def _child_env(password: str) -> dict[str, str]:
    """The environment psql runs in: always built, never the bare ambient one.

    ``PGCLIENTENCODING=UTF8`` because a Windows psql otherwise takes the console's ANSI code page and fails (or
    garbles) a ``source_uri`` outside it; ``LC_ALL=C`` and ``lc_messages=C`` so the client's own messages, and the
    server's where it lets a startup option decide them, are English whatever the caller's locale. The password,
    when there is one, is ``PGPASSWORD`` here and nowhere else.
    """
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["PGCLIENTENCODING"] = "UTF8"
    env["PGOPTIONS"] = (env.get("PGOPTIONS", "").strip() + " -c lc_messages=C").strip()
    if password:
        env["PGPASSWORD"] = password
    return env


def _psql_argv(bins: PgBinaries, port: int, role: str, dbname: str, sql: str) -> list[str]:
    # -w never prompts; -X ignores a psqlrc; the password is not here, it is PGPASSWORD in the child's environment.
    return [
        str(bins.psql), "-X", "-w", "-h", "127.0.0.1", "-p", str(port), "-U", role, "-d", dbname,
        "-v", "ON_ERROR_STOP=1", "-t", "-A", "-0", "-z", "-c", sql,
    ]


def _parse(stdout: str) -> tuple[Pair, ...]:
    items = stdout.split(_NUL)
    if items and items[-1] == "":  # the last record's terminator
        items.pop()
    width = len(_COLUMNS)
    if len(items) % width:
        raise NoteUriPairsError(f"psql returned {len(items)} fields, which is not a whole number of {width}-column rows")
    return tuple(Pair(*items[i : i + width]) for i in range(0, len(items), width))


def _scrub(text: str, password: str) -> str:
    return text.replace(password, "***") if password else text


def _start_advice(local_install: bool) -> str:
    if local_install:
        return "A halted engine leaves the cluster running; if the cluster is down, `nx daemon service start` starts it."
    return (
        "This install is configured for a managed service, so this cluster is a leftover on disk, not the one "
        "that serves you: there is nothing to start."
    )


def _probe(accepting: Callable[[str, int], bool], port: int) -> bool:
    for n in range(_PROBE_TRIES):
        if accepting("127.0.0.1", port):
            return True
        if n + 1 < _PROBE_TRIES:
            time.sleep(_PROBE_PAUSE_S)
    return False


def _not_accepting(port: int, local_install: bool) -> NoteUriPairsError:
    return NoteUriPairsError(f"the local PostgreSQL cluster is not accepting connections on 127.0.0.1:{port}. " + _start_advice(local_install))


def _refused_roles(tried: Sequence[str], platform: str | None) -> str:
    if len(tried) > 1:
        names = f"neither role {tried[0]} nor role {tried[1]} was accepted"
    else:
        names = f"role {tried[0]} was refused"
    if _on_windows(platform):
        return (
            f"{names}. On Windows the superuser is derived from this account's service SID, so a cluster "
            "provisioned by another OS account has a different one: run this as the account that ran `nx init`"
        )
    return names


def _failure(
    exc: subprocess.CalledProcessError,
    *,
    tried: Sequence[str],
    port: int,
    password: str,
    local_install: bool,
    platform: str | None,
) -> NoteUriPairsError:
    stderr = _scrub((exc.stderr or "").strip(), password)
    tail = stderr[-400:]
    low = stderr.lower()
    if _refused(low):
        return _not_accepting(port, local_install)
    if _auth_failure(low):
        recorded = "" if password else f" No {SUPERUSER_PASS_KEY} is recorded in pg_credentials, so no password was used."
        return NoteUriPairsError(
            f"{_refused_roles(tried, platform)}.{recorded} nx reads the password from {SUPERUSER_PASS_KEY} in "
            "pg_credentials; restore it from a backup of that file, or see step 1 of 'If the RDR-231 note-URI "
            f"backfill halts startup' at {RUNBOOK_URL}\n" + tail
        )
    if "does not exist" in low and "relation" in low:
        return NoteUriPairsError(
            "the cluster has no nexus.catalog_documents table, so the engine has not migrated this database "
            "(nothing to list).\n" + tail
        )
    return NoteUriPairsError(f"psql as role {tried[-1]} on 127.0.0.1:{port} failed:\n{tail}")


def _refused(low: str) -> bool:
    return "connection refused" in low or ("could not connect" in low and "refused" in low)


def _auth_failure(low: str) -> bool:
    return (
        "password authentication failed" in low
        or "no password supplied" in low
        or "fe_sendauth" in low
        or ('role "' in low and "does not exist" in low)
    )


def _find_bins(config_dir: Path) -> PgBinaries:
    """The bundled client: NEXUS_PG_BIN, then the bundle extracted under *config_dir*, then the ambient config dir's."""
    if not os.environ.get("NEXUS_PG_BIN", "").strip():
        under = extracted_bin_dir(config_dir)
        if under is not None:
            found = PgBinaries.from_dir(under)
            if found.all_present():
                return found
    return discover_pg_binaries()


def list_note_uri_pairs(
    config_dir: Path,
    *,
    platform: str | None = None,
    identity: Callable[[], str] | None = None,
    bins: PgBinaries | None = None,
    run: PsqlRun | None = None,
    dbname: str = NEXUS_DB_NAME,
    accepting: Callable[[str, int], bool] | None = None,
    local_install: bool = True,
) -> PairsReport:
    """Run the pair query in the local cluster under *config_dir*; raise :class:`NoteUriPairsError` on any failure.

    *local_install* False (a managed install that left ``pg_credentials`` behind) only changes the advice in a
    message. *platform*, *identity*, *bins*, *run*, *dbname* and *accepting* are test seams (the engine substrate's
    database is not named ``nexus``); production passes none of them.
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
    if not (port_text.isascii() and port_text.isdigit()):  # isdigit() alone admits "²", which int() refuses
        raise NoteUriPairsError(f"{creds_path} has no usable PG_PORT: there is no local cluster to ask")
    port = int(port_text)
    password = creds.get(SUPERUSER_PASS_KEY, "")

    if bins is None:
        try:
            bins = _find_bins(config_dir)
        except PgBinaryNotFoundError as exc:
            raise NoteUriPairsError(f"no PostgreSQL client to run the query with (nx never uses a host psql): {exc}") from None

    runner = run if run is not None else _default_run
    env = _child_env(password)
    tried: list[str] = []

    def attempt(as_role: str, sql: str) -> str:
        return runner(_psql_argv(bins, port, as_role, dbname, sql), env)

    try:
        tried.append(bootstrap_superuser(platform=platform, identity=identity))
        # A refused connection is decided here, before psql, so that no message text has to be read for it.
        if not _probe(accepting if accepting is not None else _port_accepting, port):
            raise _not_accepting(port, local_install)
        try:
            stdout = attempt(tried[0], PAIR_SQL)
        except subprocess.CalledProcessError as exc:
            # A cluster provisioned from a process with neither USER nor LOGNAME (cron, a service manager) has the
            # superuser `postgres` (bootstrap_superuser's POSIX fallback). The bundle authenticates every hba line
            # with scram, and scram answers a role that does not exist exactly as a wrong password ("password
            # authentication failed"), so the role's absence is never in the message. psql exits 2 for every failure
            # to connect: retry as postgres on that, except a refused connection. Windows has one derivation.
            low = (exc.stderr or "").lower()
            if _on_windows(platform) or tried[0] == _FALLBACK_ROLE or exc.returncode != 2 or _refused(low):
                raise
            tried.append(_FALLBACK_ROLE)
            stdout = attempt(_FALLBACK_ROLE, PAIR_SQL)
        role = tried[-1]
        pairs = _parse(stdout)
        try:
            changeset: str | None = attempt(role, CHANGESET_SQL).strip(_NUL + " \r\n")
        except subprocess.CalledProcessError:
            changeset = None
    except NoteUriPairsError:
        raise
    except ServiceRegistryError as exc:
        raise NoteUriPairsError(f"the superuser's name could not be derived on this account: {exc}") from None
    except subprocess.CalledProcessError as exc:
        raise _failure(
            exc, tried=tried, port=port, password=password, local_install=local_install, platform=platform
        ) from None
    except subprocess.TimeoutExpired:
        raise NoteUriPairsError(f"psql on 127.0.0.1:{port} did not answer within {_PSQL_TIMEOUT_S:.0f} s") from None
    except (PgRootUserError, PgSmartAppControlError) as exc:
        raise NoteUriPairsError(str(exc)) from None
    except OSError as exc:
        raise NoteUriPairsError(f"could not start psql ({type(exc).__name__}): {_scrub(str(exc), password)}") from None
    _log.debug("note_uri_pairs_listed", role=role, port=port, pairs=len(pairs), changeset=changeset)
    return PairsReport(role=role, port=port, pairs=pairs, changeset=changeset)
