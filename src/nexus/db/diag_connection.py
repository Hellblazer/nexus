# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-182 P2.1: the ONLY product path to a ``nexus_diag`` session.

Boundary contract (critic-foundations Critical-1, 2026-07-12 — stated here so
it is never laundered again):

- The **mutation** boundary is DB-enforced by the role itself: ``nexus_diag``
  holds SELECT and nothing else, so INSERT/UPDATE/DELETE/DDL refuse at
  Postgres regardless of what any caller does
  (``tests/db/test_nexus_diag_role.py::TestMutationsRefuse``).
- The **content** boundary (RDR-182 §5: diagnostics may count store rows,
  never read row/document/note content) is NOT enforced by the role — the
  role has full-column SELECT + BYPASSRLS, because integrity probes must see
  what Liquibase VALIDATE sees (nexus-vounk: FORCE-RLS false-clean,
  demonstrated 0-vs-9). The content boundary is enforced HERE, at the single
  product choke point: :func:`run_diagnostic_sql` refuses to execute any
  statement that fails :func:`nexus.remediation.sql_lint
  .assert_read_only_diagnostics` — BEFORE any DB contact — and wraps every
  execution in ``SET TRANSACTION READ ONLY`` as defense-in-depth. Product
  code MUST NOT open ad-hoc ``nexus_diag`` sessions any other way; a direct
  psql/psycopg connection as ``nexus_diag`` bypasses the content lint and is
  a review-blocking defect. (Structural DB-level content scoping — count
  views instead of table SELECT — is tracked separately as a Phase-3 design
  question.)

Credentials: ``NX_DB_DIAG_USER`` / ``NX_DB_DIAG_PASS`` in ``pg_credentials``
are OPTIONAL keys — pre-P2.1 files lack them until the next ``provision()``
run (the fast idempotency path backfills the role + keys on already-running
clusters, so one re-run of ``nx init --service``/``guided-upgrade`` heals
them) — so resolution returns ``None`` and callers degrade cleanly, same
posture as the probe gates that consume this.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import structlog

_log = structlog.get_logger(__name__)

__all__ = [
    "DiagCredentials",
    "resolve_diag_credentials",
    "run_diagnostic_sql",
]

#: (argv, env) -> CompletedProcess. Injectable for unit tests.
PsqlRunner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class DiagCredentials:
    """Connection material for the nexus_diag diagnostic session."""

    port: int
    user: str
    password: str
    dbname: str = "nexus"
    host: str = "127.0.0.1"


def resolve_diag_credentials(
    creds_path: Path | None = None,
) -> DiagCredentials | None:
    """Read the diag role's credentials from ``pg_credentials``.

    ``None`` when the file is absent, unreadable (OSError/decode), or
    predates P2.1 (no ``NX_DB_DIAG_*`` keys / no port) — the caller degrades
    cleanly (a probe that cannot run never blocks; it reports itself as
    skipped, matching the chash-poison gate's posture).
    """
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — circular-dep avoidance (nexus.config)
    from nexus.db.pg_provision import (  # noqa: PLC0415 — circular-dep avoidance (nexus.db.pg_provision)
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
        _log.warning("diag_credentials_unreadable", path=str(creds_path), error=str(exc))
        return None
    user = creds.get("NX_DB_DIAG_USER", "")
    password = creds.get("NX_DB_DIAG_PASS", "")
    try:
        port = int(creds.get("PG_PORT", "0"))
    except ValueError:
        port = 0
    if not user or not password or port <= 0:
        return None
    return DiagCredentials(port=port, user=user, password=password)


def _default_psql_runner(argv: list[str], env: dict[str, str]):
    return subprocess.run(  # noqa: PLW1510 — returncode inspected by caller
        argv, env=env, capture_output=True, text=True, timeout=60,
    )


def run_diagnostic_sql(
    statements: Sequence[str],
    creds: DiagCredentials,
    *,
    psql_bin: Path | None = None,
    psql_runner: PsqlRunner | None = None,
) -> list[str]:
    """Execute read-only diagnostic *statements* as ``nexus_diag``.

    THE choke point: every statement is linted read-only + metadata-scoped
    (:mod:`nexus.remediation.sql_lint`) BEFORE any DB contact — a mutating or
    content-reading statement raises ``DiagnosticSqlViolation`` and nothing
    is executed. Each statement then runs in a session started with
    ``PGOPTIONS='-c default_transaction_read_only=on'`` — the whole-session
    equivalent of ``SET TRANSACTION READ ONLY`` (defense-in-depth: even a
    privilege-grant mistake cannot turn a diagnostic into a write; a
    stray-write attempt fails with ``read-only transaction``). No tenant GUC
    is set — nexus_diag is BYPASSRLS precisely so integrity counts see every
    tenant's rows (nexus-vounk).

    Returns the trimmed stdout of each statement, in order. A psql failure
    raises ``RuntimeError`` with the stderr (probes wrap this into their own
    degrade-cleanly reporting).
    """
    from nexus.remediation.sql_lint import assert_read_only_diagnostics  # noqa: PLC0415 — keep import cost off the CLI startup path

    assert_read_only_diagnostics(statements)

    if psql_bin is None:
        from nexus.db.pg_provision import discover_pg_binaries  # noqa: PLC0415 — circular-dep avoidance (nexus.db.pg_provision)

        psql_bin = discover_pg_binaries().psql
    runner = psql_runner if psql_runner is not None else _default_psql_runner

    outputs: list[str] = []
    for stmt in statements:
        argv = [
            str(psql_bin), "-h", creds.host, "-p", str(creds.port),
            "-U", creds.user, "-d", creds.dbname,
            "-v", "ON_ERROR_STOP=1", "-tAc", stmt,
        ]
        env = dict(
            os.environ,
            PGPASSWORD=creds.password,
            PGOPTIONS="-c default_transaction_read_only=on",
        )
        proc = runner(argv, env)
        if proc.returncode != 0:
            _log.warning(
                "diag_sql_failed", statement=stmt, stderr=(proc.stderr or "")[:200],
            )
            raise RuntimeError(
                f"diagnostic statement failed (psql exit {proc.returncode}): "
                f"{(proc.stderr or '').strip()[:200]}"
            )
        outputs.append(proc.stdout.strip())
    return outputs
