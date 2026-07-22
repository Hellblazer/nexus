# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-180 (nexus-jxizy.6): the local ADMIN-connection SQL runner.

The chash-rekey rung's VALIDATE step needs the table OWNER
(``nexus_admin``): ``ALTER TABLE ... VALIDATE CONSTRAINT`` scans every row
RLS-exempt (the nexus-1wjmq asymmetry — VALIDATE sees what a policy-subject
count cannot), and only the owner may run it. This is deliberately NOT a
Liquibase boot changeset (it would crash-loop un-rekeyed stores — the GH
#1390 shape) and NOT the ``nexus_svc`` role (not the owner).

Managed-cloud installs have no local ``pg_credentials`` — resolution
returns ``None`` and the caller reports the operator-step honestly.

SCOPE GUARD: unlike the diag choke point (read-only lint), this runner
executes DDL — so it accepts ONLY statements shaped like the rekey rung's
``VALIDATE CONSTRAINT`` set. Anything else raises before DB contact; new
admin operations must be added to the allowlist deliberately, with review.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import structlog

_log = structlog.get_logger(__name__)

#: The ONLY admin statement shape this runner executes.
_VALIDATE_RE = re.compile(
    r"^ALTER TABLE nexus\.[a-z0-9_]+ VALIDATE CONSTRAINT [a-z0-9_]+$"
)

PsqlRunner = Callable[[list[str], dict[str, str]], "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class AdminCredentials:
    port: int
    user: str
    password: str
    host: str = "127.0.0.1"
    dbname: str = "nexus"


def resolve_admin_credentials(
    creds_path: Path | None = None,
) -> AdminCredentials | None:
    """Read the admin role's credentials from ``pg_credentials``; ``None``
    when absent/unreadable (managed mode, pre-provision install)."""
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
        _log.warning("admin_credentials_unreadable", path=str(creds_path), error=str(exc))
        return None
    user = creds.get("NX_DB_ADMIN_USER", "")
    password = creds.get("NX_DB_ADMIN_PASS", "")
    try:
        port = int(creds.get("PG_PORT", "0"))
    except ValueError:
        port = 0
    if not user or not password or port <= 0:
        return None
    return AdminCredentials(port=port, user=user, password=password)


def _default_psql_runner(argv: list[str], env: dict[str, str]):
    return subprocess.run(  # noqa: PLW1510 — returncode inspected by caller
        argv, env=env, capture_output=True, text=True, timeout=600,
    )


def run_admin_sql(
    statements: Sequence[str],
    *,
    creds_path: Path | None = None,
    psql_bin: Path | None = None,
    psql_runner: PsqlRunner | None = None,
) -> bool | None:
    """Execute allowlisted admin *statements* via the local admin role.

    Returns ``True`` on success, ``None`` when no local admin path exists
    (managed mode — the caller surfaces the operator step), and raises
    ``RuntimeError`` on execution failure (never a silent partial).
    """
    for stmt in statements:
        if not _VALIDATE_RE.match(stmt):
            raise ValueError(
                f"admin statement outside the allowlisted VALIDATE shape: {stmt!r}"
            )
    creds = resolve_admin_credentials(creds_path)
    if creds is None:
        _log.info("admin_sql_no_local_credentials", note="managed mode — operator step")
        return None
    if psql_bin is None:
        from nexus.db.pg_provision import (  # noqa: PLC0415 — circular-dep avoidance
            PgBinaryNotFoundError,
            discover_pg_binaries,
        )

        try:
            psql_bin = discover_pg_binaries().psql
        except PgBinaryNotFoundError:
            _log.info("admin_sql_no_psql_binaries", note="cannot validate here")
            return None
    runner = psql_runner if psql_runner is not None else _default_psql_runner
    for stmt in statements:
        argv = [
            str(psql_bin), "-h", creds.host, "-p", str(creds.port),
            "-U", creds.user, "-d", creds.dbname,
            "-v", "ON_ERROR_STOP=1", "-tAc", stmt,
        ]
        # nexus-iytd3 loader guard — same RPATH-less-bundle class as
        # diag_connection.run_diagnostic_sql; see GH #1414 era-hop review.
        from nexus.db.pg_provision import _bundle_lib_env  # noqa: PLC0415 — circular-dep avoidance

        env = _bundle_lib_env(argv, None)
        env["PGPASSWORD"] = creds.password
        proc = runner(argv, env)
        if proc.returncode != 0:
            raise RuntimeError(
                f"admin statement failed (psql exit {proc.returncode}): {stmt} — "
                f"{(proc.stderr or '').strip()[:200]}"
            )
    _log.info("admin_sql_ok", statements=len(statements))
    return True
