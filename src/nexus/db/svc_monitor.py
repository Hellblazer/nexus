# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-bb5c8: the nexus_svc-credentialed ``pg_monitor``-scoped query path.

``grants-004-monitor-wal-visibility``
(``service/src/main/resources/db/changelog/grants-nexus-svc.xml``) grants
``nexus_svc`` MEMBERSHIP in the built-in ``pg_monitor`` role so the engine
can read ``pg_ls_waldir()`` / ``pg_stat_*`` for WAL-retention visibility
(the RDR-191 Phase 4 always-copy trough window). That grant alone is not
necessarily usable privilege -- it depends on ``nexus_svc``'s INHERIT
attribute. nexus-v80f2 (2026-08-15) found this DIVERGED between deployment
postures and aligned it: NOINHERIT is now the posture in EVERY mode. The
CLOUD deployment's ``nexus_svc`` is ``NOINHERIT`` (measured live, conexus
relay [22485]) -- a deliberate security posture there, since ``nexus_svc``
holds several role memberships whose privileges must never become AMBIENT
just because the role holds them (RLS/BYPASSRLS adjacency); making it
INHERIT would make EVERY granted role's privileges ambient on every
connection, not just ``pg_monitor``'s. Local provisioning
(``src/nexus/db/pg_provision.py``'s ``_create_roles``, plus
``_backfill_svc_noinherit`` for an already-provisioned install) now issues
the same ``NOINHERIT`` clause, matching
``role-001-nexus-svc.xml``'s fallback bootstrap, which always has. Under
NOINHERIT, a plain ``nexus_svc``
session gets ``permission denied`` from ``pg_ls_waldir()`` until it
explicitly ``SET ROLE pg_monitor`` first -- exactly PostgreSQL's
documented behaviour for a NOINHERIT membership; under INHERIT the same
``SET ROLE`` is a harmless no-op (already-held privilege), so this module
issues it UNCONDITIONALLY and is correct in both postures without probing
which one it is talking to.

DECISION OF RECORD (nexus-bb5c8 relay, 2026-08-14): ``SET ROLE`` is the
SUPPORTED, documented, mechanized path here -- NOT ``ALTER ROLE nexus_svc
INHERIT`` (would make every OTHER granted role's privileges ambient too,
far beyond WAL visibility -- rejected, and moot for the cloud posture
this module must also work against) and NOT a ``SECURITY DEFINER``
wrapper function (adds an owned function surface for something ``SET
ROLE`` already does cleanly). This module is the ONE product-side place a
``nexus_svc`` session escalates to ``pg_monitor`` for a query, then drops
back -- sibling to ``nexus.db.admin_sql`` (``nexus_admin``-credentialed,
allowlisted DDL) and ``nexus.db.diag_connection`` (``nexus_diag``-
credentialed, read-only-lint-gated SELECT). No in-repo call site consumed
``pg_ls_waldir()`` before this module (swept clean at nexus-bb5c8 time,
2026-08-14) -- this closes that gap product-side; ``nx doctor
--check-wal-retention`` is the first consumer.

Credentials: ``NX_DB_USER`` / ``NX_DB_PASS`` in ``pg_credentials`` are the
``nexus_svc`` role's own login (written unconditionally by
``pg_provision.py``'s ``_write_credentials`` -- unlike ``nexus_diag``'s
optional keys, these have been present since the service role has
existed). Managed/BYO-Postgres deployments have no local
``pg_credentials`` file at all -- resolution returns ``None`` and callers
degrade to an explicit UNMEASURED report, never a false-clean silence.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import structlog

_log = structlog.get_logger(__name__)

__all__ = [
    "SvcCredentials",
    "resolve_svc_credentials",
    "monitor_scoped_query",
    "wal_retained_bytes",
    "wal_retention_report",
]

#: (argv, env) -> CompletedProcess. Injectable for unit tests.
PsqlRunner = Callable[..., "subprocess.CompletedProcess[str]"]

#: grants-004's own remedy text, restated here so a caller hitting this
#: error never has to go spelunking in the changelog to find the fix.
_GRANTS_004_REMEDY = (
    "nexus_svc must hold pg_monitor MEMBERSHIP before this call can "
    "succeed (grants-004-monitor-wal-visibility, "
    "service/src/main/resources/db/changelog/grants-nexus-svc.xml). "
    "REMEDY: confirm nexus_admin holds `pg_monitor WITH ADMIN OPTION` "
    "(docs/configuration.md prerequisite 2 -- the local bundle installer "
    "runs this automatically via src/nexus/db/pg_provision.py; bring-"
    "your-own/managed Postgres deployments must run it manually) and "
    "that the engine has applied its Liquibase migration at least once "
    "since."
)


@dataclass(frozen=True)
class SvcCredentials:
    port: int
    user: str
    password: str
    host: str = "127.0.0.1"
    dbname: str = "nexus"


def resolve_svc_credentials(creds_path: Path | None = None) -> SvcCredentials | None:
    """Read the nexus_svc role's own credentials from ``pg_credentials``.

    ``None`` when the file is absent, unreadable, or missing the
    NX_DB_USER/NX_DB_PASS/PG_PORT keys (managed/BYO deployment with no
    local bundle, or a pre-provision install) -- callers degrade cleanly,
    same posture as :func:`nexus.db.diag_connection.resolve_diag_credentials`.
    """
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — circular-dep avoidance
    from nexus.db.pg_provision import (  # noqa: PLC0415 — circular-dep avoidance
        CREDENTIALS_FILENAME,
        _read_credentials,
    )

    if creds_path is None:
        creds_path = nexus_config_dir() / CREDENTIALS_FILENAME
    if not creds_path.exists():
        return None
    try:
        creds = _read_credentials(creds_path)
    except (OSError, UnicodeDecodeError) as exc:
        _log.warning("svc_credentials_unreadable", path=str(creds_path), error=str(exc))
        return None
    user = creds.get("NX_DB_USER", "")
    password = creds.get("NX_DB_PASS", "")
    try:
        port = int(creds.get("PG_PORT", "0"))
    except ValueError:
        port = 0
    if not user or not password or port <= 0:
        return None
    return SvcCredentials(port=port, user=user, password=password)


def _default_psql_runner(argv: list[str], env: dict[str, str]):
    return subprocess.run(  # noqa: PLW1510 — returncode inspected by caller
        argv, env=env, capture_output=True, text=True, timeout=60,
    )


def _looks_like_set_role_refusal(stderr: str) -> bool:
    """Best-effort classification of the ``SET ROLE pg_monitor`` failure
    class specifically, so its error message can name the grants-004
    remedy instead of a generic psql-failure trace. Not security-load-
    bearing (SET ROLE is refused by Postgres regardless of what this
    returns) -- purely a message-quality heuristic."""
    lowered = stderr.lower()
    return "set role" in lowered and (
        "permission denied" in lowered or "does not exist" in lowered
    )


def monitor_scoped_query(
    creds: SvcCredentials,
    query: str,
    *,
    psql_bin: Path | None = None,
    psql_runner: PsqlRunner | None = None,
) -> str:
    """Run *query* as ``nexus_svc`` after ``SET ROLE pg_monitor``.

    TRUST CONTRACT: *query* MUST be a trusted, product-authored SQL
    literal -- never caller-, user-, or otherwise externally-supplied
    text. There is NO statement lint gating *query* the way
    :func:`nexus.db.diag_connection.run_diagnostic_sql` gates its
    statements before DB contact -- that lint's denylist rejects ``SET``/
    ``RESET`` outright (mutating-session-state shaped), which is exactly
    what THIS module's own ``SET ROLE`` escalation legitimately needs to
    issue every call. Reusing it here would either break every call
    (denylist rejects the module's own ``SET ROLE``) or require carving
    a ``SET ROLE pg_monitor``-shaped exception into a lint designed to
    reject session-state mutation -- weakening the guarantee for
    ``run_diagnostic_sql``'s actual callers for a module that has no
    caller-supplied SQL to gate in the first place. Every current
    call site (:func:`wal_retained_bytes`) passes a hardcoded string; a
    future call site MUST keep that contract, not thread external input
    into *query*. The read-only session GUC
    (``PGOPTIONS=-c default_transaction_read_only=on``) is the backstop
    for that contract, not a substitute for it: it stops *query* from
    writing even if the trust contract were violated, but a violation
    could still read arbitrary rows the pg_monitor role can see.

    Both statements ride the SAME psql invocation (two ``-c`` arguments),
    which is the same session -- ``SET ROLE`` set by the first ``-c``
    persists for the second. ``-v ON_ERROR_STOP=1`` means a refused ``SET
    ROLE`` aborts before *query* ever runs. The session is read-only
    (``PGOPTIONS=-c default_transaction_read_only=on``), the same
    whole-session guard :func:`nexus.db.diag_connection.run_diagnostic_sql`
    uses -- this path only ever samples monitoring views, never mutates.
    ``SET ROLE pg_monitor`` is issued UNCONDITIONALLY on every call: under
    the cloud posture's NOINHERIT it is load-bearing; under local
    provisioning's current INHERIT default (nexus-v80f2) it is a harmless
    no-op (the privilege is already held ambiently) -- this module never
    probes which posture it is talking to, by design.

    Returns the trimmed, tuples-only/unaligned stdout of *query*. Raises
    ``RuntimeError``: with the grants-004 remedy text when ``SET ROLE
    pg_monitor`` itself is refused (membership missing -- the changeset
    never applied, or applied against a pre-nexus-hzhgl engine), or with
    the raw stderr for any other psql failure (bad query, connection
    refused, etc.) -- never a silent partial.
    """
    if psql_bin is None:
        from nexus.db.pg_provision import discover_pg_binaries  # noqa: PLC0415 — circular-dep avoidance

        psql_bin = discover_pg_binaries().psql
    runner = psql_runner if psql_runner is not None else _default_psql_runner
    # nexus-iytd3 loader guard — same RPATH-less-bundle class as
    # admin_sql.run_admin_sql / diag_connection.run_diagnostic_sql.
    from nexus.db.pg_provision import _bundle_lib_env  # noqa: PLC0415 — circular-dep avoidance

    argv = [
        str(psql_bin), "-h", creds.host, "-p", str(creds.port),
        "-U", creds.user, "-d", creds.dbname,
        "-v", "ON_ERROR_STOP=1", "-t", "-A", "-q",
        "-c", "SET ROLE pg_monitor",
        "-c", query,
    ]
    env = _bundle_lib_env(argv, None)
    env["PGPASSWORD"] = creds.password
    env["PGOPTIONS"] = "-c default_transaction_read_only=on"
    proc = runner(argv, env)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        if _looks_like_set_role_refusal(stderr):
            _log.warning("svc_monitor_set_role_refused", stderr=stderr[:300])
            raise RuntimeError(
                f"svc_monitor: SET ROLE pg_monitor refused for nexus_svc. "
                f"{_GRANTS_004_REMEDY} Original error: {stderr[:300]}"
            )
        raise RuntimeError(
            f"svc_monitor query failed (psql exit {proc.returncode}): {stderr[:200]}"
        )
    return proc.stdout.strip()


def wal_retained_bytes(
    creds: SvcCredentials,
    *,
    psql_bin: Path | None = None,
    psql_runner: PsqlRunner | None = None,
) -> int:
    """Sample total retained WAL bytes via ``pg_ls_waldir()``.

    The RDR-191 Phase 4 trough-window motivation for grants-004: retained
    WAL eats into the always-copy trough's floor (trough = 11,360 MiB -
    retained WAL; breaches the 10,000 MiB floor past ~1,360 MiB retained).
    Raises ``RuntimeError`` (see :func:`monitor_scoped_query`) rather than
    ever returning a fabricated 0 on failure.
    """
    out = monitor_scoped_query(
        creds, "SELECT COALESCE(SUM(size), 0) FROM pg_ls_waldir()",
        psql_bin=psql_bin, psql_runner=psql_runner,
    )
    return int(out)


def wal_retention_report(
    *,
    creds_path: Path | None = None,
    psql_bin: Path | None = None,
    psql_runner: PsqlRunner | None = None,
) -> str:
    """Human-readable retained-WAL report for ``nx doctor`` / operator tooling.

    Degrades LOUD-IN-BAND, mirroring
    :func:`nexus.db.diag_connection.live_store_detail`'s tri-state
    discipline: absent nexus_svc credentials (managed/BYO deployment with
    nothing local to probe, or a pre-provision install) render UNMEASURED
    rather than a blank/clean-looking line; a SET-ROLE refusal or any
    other query failure renders UNMEASURED with the raised remedy text; only
    a genuine sample renders a byte count.
    """
    creds = resolve_svc_credentials(creds_path)
    if creds is None:
        return (
            "WAL retention: UNMEASURED (no local nexus_svc credentials -- "
            "managed/BYO deployment with nothing local to probe, or a "
            "pre-provision install; this is a local-service-only sample, "
            "not a missing-privilege signal)"
        )
    try:
        n = wal_retained_bytes(creds, psql_bin=psql_bin, psql_runner=psql_runner)
    except RuntimeError as exc:
        return f"WAL retention: UNMEASURED ({exc})"
    # GH #1414 misleading-surface class (round 2 review): SvcCredentials
    # hardcodes host="127.0.0.1" — this sample is ALWAYS of the local
    # Postgres this process can reach, never a remote/managed store, even
    # when the surrounding deployment is otherwise cloud-configured. The
    # success line must self-disclose that scope exactly as the
    # UNMEASURED branch above already does ("local-service-only sample"),
    # not just imply it via the absence of an error.
    return f"WAL retention (local service): {n} bytes retained"
