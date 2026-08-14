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

#: The ONLY admin statement shape this runner executes. Captures the table
#: name so the existence gate (F14a, nexus-o8dil.1) can probe it before
#: issuing the VALIDATE.
_VALIDATE_RE = re.compile(
    r"^ALTER TABLE (nexus\.[a-z0-9_]+) VALIDATE CONSTRAINT [a-z0-9_]+$"
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


@dataclass(frozen=True)
class AdminSqlResult:
    """Outcome of :func:`run_admin_sql_detailed` (nexus-o8dil.19 E1
    non-vacuity fix — the acceptance criterion recorded on nexus-o8dil.15).

    F14a's existence gate (nexus-o8dil.1) is correct and stays: a VALIDATE
    against a relation that has been dropped out from under the rung must
    be SKIPPED, never raised. What was missing is that "every statement was
    skipped" and "every statement genuinely validated" both collapsed into
    the same ``ok=True`` — a caller (``chash_rekey.py``'s ``_validate``)
    could not tell "the octet CHECKs are validated" from "there was nothing
    left to validate against, silently, forever" (RDR-191's schema unify is
    exactly the kind of retarget that can produce that gap if a table name
    is missed). ``ok`` preserves the legacy ``bool | None`` contract
    unchanged for :func:`run_admin_sql`; ``total`` / ``skipped`` /
    :attr:`fully_skipped` are the ADDITIONAL, non-vacuous signal a caller
    that cares must consult instead of trusting ``ok`` alone.
    """

    ok: bool | None
    total: int
    skipped: int

    @property
    def fully_skipped(self) -> bool:
        """True ONLY when every attempted statement's target relation was
        absent (``ok is True`` and ``skipped == total > 0``) — the ONE
        predicate a convergence gate may read as "not actually validated
        anything", never as clean. Vacuously ``False`` on an empty
        statement list (nothing was ever attempted — a different case from
        every attempted statement finding its relation gone) and on the
        managed-mode / no-local-path arm (``ok is None``), where no
        statement was attempted either."""
        return self.ok is True and self.total > 0 and self.skipped == self.total


def run_admin_sql_detailed(
    statements: Sequence[str],
    *,
    creds_path: Path | None = None,
    psql_bin: Path | None = None,
    psql_runner: PsqlRunner | None = None,
) -> AdminSqlResult:
    """Execute allowlisted admin *statements* via the local admin role.

    F14a (nexus-o8dil.1): each ``VALIDATE`` is existence-gated first — a
    ``SELECT to_regclass('<table>') IS NOT NULL`` probe over the SAME admin
    connection. A relation that has been dropped out from under the rung
    (the ``chash_index`` shape, RDR-187) is SKIPPED, never attempted: pre-fix,
    every such statement raised ``RuntimeError`` unconditionally, so once a
    tracked relation is gone ``nx upgrade`` exits non-zero FOREVER
    (``chash_rekey.py``'s own ``OCTET_CHECKS`` comment documents this exact
    shape happening once already). The probe failing to even RUN (permission
    denied, connection refused) is a DIFFERENT case — it is not evidence the
    relation is absent, so it still raises loud rather than silently
    skipping a VALIDATE that might genuinely be needed.

    Returns an :class:`AdminSqlResult` whose ``ok`` field carries the SAME
    ``True`` / ``None`` semantics :func:`run_admin_sql` has always exposed
    (never ``False`` — genuine failure raises), and whose ``total`` /
    ``skipped`` / ``fully_skipped`` let a caller distinguish a run where
    every statement's relation was absent from one that genuinely
    validated something (nexus-o8dil.19 E1 non-vacuity fix). Raises
    ``RuntimeError`` on execution failure (never a silent partial).
    """
    matches: list[tuple[str, str]] = []
    for stmt in statements:
        m = _VALIDATE_RE.match(stmt)
        if not m:
            raise ValueError(
                f"admin statement outside the allowlisted VALIDATE shape: {stmt!r}"
            )
        matches.append((stmt, m.group(1)))
    creds = resolve_admin_credentials(creds_path)
    if creds is None:
        _log.info("admin_sql_no_local_credentials", note="managed mode — operator step")
        return AdminSqlResult(ok=None, total=len(matches), skipped=0)
    if psql_bin is None:
        from nexus.db.pg_provision import (  # noqa: PLC0415 — circular-dep avoidance
            PgBinaryNotFoundError,
            discover_pg_binaries,
        )

        try:
            psql_bin = discover_pg_binaries().psql
        except PgBinaryNotFoundError:
            _log.info("admin_sql_no_psql_binaries", note="cannot validate here")
            return AdminSqlResult(ok=None, total=len(matches), skipped=0)
    runner = psql_runner if psql_runner is not None else _default_psql_runner
    # nexus-iytd3 loader guard — same RPATH-less-bundle class as
    # diag_connection.run_diagnostic_sql; see GH #1414 era-hop review.
    from nexus.db.pg_provision import _bundle_lib_env  # noqa: PLC0415 — circular-dep avoidance

    def _run(stmt: str) -> "subprocess.CompletedProcess[str]":
        argv = [
            str(psql_bin), "-h", creds.host, "-p", str(creds.port),
            "-U", creds.user, "-d", creds.dbname,
            "-v", "ON_ERROR_STOP=1", "-tAc", stmt,
        ]
        env = _bundle_lib_env(argv, None)
        env["PGPASSWORD"] = creds.password
        return runner(argv, env)

    skipped = 0
    for stmt, table in matches:
        exists_proc = _run(f"SELECT to_regclass('{table}') IS NOT NULL")
        if exists_proc.returncode != 0:
            raise RuntimeError(
                f"existence check failed (psql exit {exists_proc.returncode}) "
                f"for {table}: {(exists_proc.stderr or '').strip()[:200]}"
            )
        if (exists_proc.stdout or "").strip() != "t":
            skipped += 1
            _log.info(
                "admin_sql_relation_absent_skipping_validate",
                table=table, statement=stmt,
                note="the target relation does not exist — VALIDATE would "
                     "raise forever; skipping rather than crash-looping "
                     "nx upgrade",
            )
            continue
        proc = _run(stmt)
        if proc.returncode != 0:
            raise RuntimeError(
                f"admin statement failed (psql exit {proc.returncode}): {stmt} — "
                f"{(proc.stderr or '').strip()[:200]}"
            )
    _log.info("admin_sql_ok", statements=len(statements), skipped=skipped)
    return AdminSqlResult(ok=True, total=len(matches), skipped=skipped)


def run_admin_sql(
    statements: Sequence[str],
    *,
    creds_path: Path | None = None,
    psql_bin: Path | None = None,
    psql_runner: PsqlRunner | None = None,
) -> bool | None:
    """Execute allowlisted admin *statements* via the local admin role.

    Thin backward-compatible wrapper over :func:`run_admin_sql_detailed`,
    collapsing its :class:`AdminSqlResult` down to the legacy ``bool | None``
    shape: ``True`` on success (including a run where every statement was
    skipped as absent — skipping is not a FAILURE, but see the non-vacuity
    note below), ``None`` when no local admin path exists (managed mode —
    the caller surfaces the operator step), and ``RuntimeError`` raised on
    execution failure (never a silent partial).

    E1 NON-VACUITY (nexus-o8dil.19, acceptance recorded on nexus-o8dil.15):
    a caller that needs to distinguish "every statement's relation was
    absent" from "genuinely validated something" — e.g. a convergence gate
    deciding whether ``nx upgrade`` may report the octet CHECKs converged —
    MUST call :func:`run_admin_sql_detailed` and check
    :attr:`AdminSqlResult.fully_skipped` instead of trusting this
    function's bare ``True``. This wrapper exists for callers that only
    ever cared about "did it raise", unchanged.
    """
    return run_admin_sql_detailed(
        statements, creds_path=creds_path, psql_bin=psql_bin, psql_runner=psql_runner,
    ).ok
