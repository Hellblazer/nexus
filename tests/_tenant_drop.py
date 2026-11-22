# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Dropping a minted test tenant's partition leaves (RDR-225), split out of ``tests/_engine_substrate.py`` under its
file-size ceiling. ``_engine_substrate`` re-exports :func:`drop_test_tenant`, so callers import it from there as before.

It runs psql as the owning role over the substrate's direct PG access; the ``state`` mapping is the substrate's.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import structlog

# Bound at import, as _engine_substrate binds its own, so a test that patches subprocess.run does not reach it.
_REAL_SUBPROCESS_RUN = subprocess.run

_log = structlog.get_logger(__name__)


#: Backoff in seconds between ``drop_test_tenant``'s calls after ``nexus.drop_tenant_partitions`` gives up on its lock
#: waits. Each call spends up to about 4.5 s inside the function (five waits of at most 500 ms plus its own sleeps), so
#: four calls and these delays cover a holder for about 25 s.
DROP_LOCK_RETRY_DELAYS_S: tuple[float, ...] = (1.0, 2.0, 4.0)

#: The text ``nexus.drop_tenant_partitions`` raises when every lock attempt timed out (vectors-066).
_DROP_LOCK_GAVE_UP = "gave up after"

_LOCK_HOLDERS_SQL = """
SELECT a.pid::text || ' ' || a.backend_type || ' ' || coalesce(a.state, '-') || ' '
       || l.mode || ' ' || l.relation::regclass::text || ' '
       || left(regexp_replace(coalesce(a.query, ''), '\\s+', ' ', 'g'), 160)
  FROM pg_catalog.pg_locks l
  JOIN pg_catalog.pg_stat_activity a ON a.pid = l.pid
 WHERE l.granted AND l.locktype = 'relation' AND l.pid <> pg_catalog.pg_backend_pid()
   AND l.relation IN (SELECT c.oid FROM pg_catalog.pg_class c WHERE c.relnamespace = 'nexus'::regnamespace)
 ORDER BY a.pid, l.relation::regclass::text
 LIMIT 40;
"""


def _psql_as_owner(state: dict, sql: str, tenant: str) -> subprocess.CompletedProcess:
    return _REAL_SUBPROCESS_RUN(
        [str(Path(state["pg_bin"]) / "psql"), "-X", "-q", "-h", "127.0.0.1", "-p", str(state["pg_port"]),
         "-U", state["pg_user"], "-d", state["pg_dbname"], "-t", "-A", "-v", "ON_ERROR_STOP=1",
         "-v", f"tenant={tenant}", "-f", "-"],
        input=sql, capture_output=True, text=True, timeout=60,
        env={**os.environ, "PGCONNECT_TIMEOUT": "10"},
    )


def _log_lock_holders(state: dict, tenant: str) -> None:
    """Best effort: name the sessions holding locks on nexus relations, so a drop that gave up says who it waited on."""
    try:
        proc = _psql_as_owner(state, _LOCK_HOLDERS_SQL, tenant)
        holders = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    except Exception as exc:  # noqa: BLE001 - diagnostics must not mask the drop's own result
        holders = [f"holder query failed: {exc!r}"]
    _log.warning("test_tenant_drop_lock_holders", tenant=tenant, holders=holders)


def drop_test_tenant(state: dict, tenant: str, *, sleep: Callable[[float], None] = time.sleep) -> bool:
    """Drop a minted tenant's partition leaves, and the rows that reference them (RDR-225).

    Every tenant owns a leaf under each model partition of nexus.chunks and nexus.taxonomy_centroids, so a suite
    that mints one per test and never removes it grows its worker's PG without bound (past 2,000 tenants the
    engine's token insert outran its 1 s x 3 bound and answered 503 tenant_creation_busy). This calls
    ``nexus.drop_tenant_partitions`` over the substrate's direct PG access as the owning role: the function is not
    granted to any engine role.

    When the function gives up on its lock waits (another session held a lock it needs; on CI shard 4 of 2773105b83
    the holder was not identified), the call is retried after each of :data:`DROP_LOCK_RETRY_DELAYS_S`; any other
    failure fails at once. A final give-up logs the sessions holding locks on nexus relations.

    Never raises: it runs in fixture teardown, where an exception would replace the test's own result. A failure
    is logged as ``test_tenant_drop_failed`` and the call returns False.
    """
    for delay in (*DROP_LOCK_RETRY_DELAYS_S, None):
        try:
            proc = _psql_as_owner(state, "SELECT nexus.drop_tenant_partitions(:'tenant');", tenant)
        except Exception as exc:  # noqa: BLE001 - teardown must not mask the test's result
            _log.warning("test_tenant_drop_failed", tenant=tenant, error=repr(exc))
            return False
        if proc.returncode == 0:
            return True
        error = (proc.stderr or proc.stdout or "").strip()[-300:]
        if _DROP_LOCK_GAVE_UP not in error or delay is None:
            _log.warning("test_tenant_drop_failed", tenant=tenant, error=error)
            if _DROP_LOCK_GAVE_UP in error:
                _log_lock_holders(state, tenant)
            return False
        _log.info("test_tenant_drop_lock_retry", tenant=tenant, after_s=delay)
        sleep(delay)
    return False
