# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared helpers for CLI command modules.

nexus-8g79.10: ``default_db_path`` was promoted to ``nexus.config``
so non-CLI modules (mcp_infra, health, collection_health,
collection_audit, context, operators/aspect_sql, merge_candidates,
console/routes/health) can resolve the canonical T2 path without
importing up from this CLI helpers module. Re-exported here for
back-compat with CLI command modules that import from
``commands._helpers`` directly.

The re-export is a thin wrapper (not ``from … import``) so test
monkeypatches on ``nexus.config.default_db_path`` reach the live
binding via attribute access at call time. The ``from x import y``
form captures ``y`` at import time and silently bypasses the patch.
"""
from contextlib import contextmanager
from pathlib import Path  # noqa: F401  -- preserved for back-compat callers
from typing import Any, Iterator

from nexus import config as _config

__all__ = ["default_db_path", "t2_handle"]


def default_db_path() -> Path:
    """Delegate to :func:`nexus.config.default_db_path` at call time."""
    return _config.default_db_path()


@contextmanager
def t2_handle() -> Iterator[Any]:
    """Open a T2 handle for the user-facing CLI memory / plan commands.

    RDR-120 P6 follow-up (nexus-w6txl) routed these through a ``T2Client`` so
    multi-process operators (host CLI + Cowork-bridged MCP server +
    dev-container CLI) shared a single arbitrated SQLite writer rather than
    each opening its own connection and racing the WAL. The daemon that
    arbitrated them is retired (nexus-i711w Stage 2 sub-stage B); in service
    mode Postgres is the arbiter and the concern does not arise.

    Yields a service-backed ``T2Database``. Tests monkeypatch this helper to
    yield an in-process ``T2Database`` fixture; call sites use
    ``.memory.<method>`` on the yielded object either way.

    Operator/debug paths that MUST work when the daemon is offline
    (``nx upgrade``, ``nx doctor``, ``_session_end_launcher``, etc.)
    continue to construct ``T2Database(default_db_path())`` directly
    with ``# epsilon-allow`` tokens — this helper is for the user-
    facing memory/plan surface only.

    Note: ``nx plan`` commands open T2 directly (epsilon-allow) and
    do NOT go through this helper — they must tolerate offline mode.
    """
    import click  # noqa: PLC0415 — deliberate function-local import: avoids click dependency at module import time

    # RDR-152 nexus-fjwxh: in SERVICE mode the Java service (PG) is the write
    # arbiter, so the SQLite single-writer T2 daemon is not in the picture —
    # route directly to a service-backed T2Database (its ``.memory`` is an
    # HttpMemoryStore with the same interface as ``T2Client.memory``). The
    # daemon-client path below is the SQLite-mode arbiter only.
    from nexus.db.storage_mode import StorageBackend, storage_backend_for  # noqa: PLC0415 — deliberate function-local import: circular-dep avoidance, db package imports commands surfaces

    if storage_backend_for("memory") == StorageBackend.SERVICE:
        import httpx  # noqa: PLC0415 — deliberate function-local import: branch-local, only on SERVICE path

        from nexus.db.t2 import T2Database  # noqa: PLC0415 — deliberate function-local import: branch-local, only on SERVICE path

        # Service mode routes T2Database to the HTTP service (PG arbiter), not a
        # raw SQLite writer, so the RDR-128 single-writer concern does not apply.
        #
        # nexus-00en9: two distinct service-down failure points, both of which
        # would otherwise reach Click as a raw traceback:
        #  (a) PRE-YIELD construction — HttpMemoryStore resolves its endpoint via
        #      resolve_service_config(), which raises RuntimeError fail-loud when
        #      no lease/env is discoverable (the common "service never started"
        #      case). Its message already names the operator fix.
        #  (b) POST-YIELD RPC — the endpoint resolved (a lease existed) but the
        #      service is unreachable/erroring when the actual RPC fires, raising
        #      an httpx transport or status error.
        try:
            db = T2Database(default_db_path(), run_migrations=False)  # epsilon-allow: service mode routes to HTTP service, not a raw SQLite writer
        except RuntimeError as exc:
            raise click.ClickException(
                f"T2 storage service unavailable: {exc}"
            ) from exc
        try:
            yield db
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            # Narrow to transport/status failures (unreachable/erroring service).
            # Decode/redirect/protocol httpx errors are service-side bugs that
            # should keep their traceback during go-live, not be aliased to a
            # reachability hint.
            raise click.ClickException(
                f"T2 storage service error: {exc}. "
                "Check the storage service: nx doctor"
            ) from exc
        finally:
            db.close()
        return

    # SQLite mode reached here via the T2 daemon, which arbitrated the single
    # SQLite writer across host CLI + MCP + dev-container processes. The daemon
    # is retired (nexus-i711w Stage 2 sub-stage B), so this branch FAILS LOUD.
    #
    # WHY FAIL LOUD RATHER THAN OPEN SQLITE DIRECTLY — the reason is NOT "the
    # branch is going away in sub-stage A anyway"; that is the scope-bleed
    # justification this staged deletion exists to forbid. It is that restoring
    # function here would mean constructing a raw ``T2Database(default_db_path())``
    # from the CLI, which this helper has NEVER done — pre-retirement it held a
    # ``T2Client``, not a connection. That is a NEW raw-SQLite site, and the
    # no-new-SQLite directive (2026-07-18) makes raising
    # ``tests/test_no_new_sqlite.py``'s EPSILON_CENSUS an explicit Hal decision
    # recorded on a bead, not a repair an implementer may make in passing.
    #
    # KNOWN AND DELIBERATE ASYMMETRY: the MCP side does keep direct SQLite arms
    # (``mcp_infra.t2_ctx``, ``mcp_infra.t2_index_write``). Those are GRANDFATHERED
    # census entries that retire with the stores in sub-stage A — not a licence
    # for a new one here. The practical consequence is real and worth stating
    # plainly: on a box holding ``NX_STORAGE_BACKEND=sqlite`` (the RDR-152
    # copy-not-move rollback lever), MCP ``memory_put`` still writes while
    # ``nx memory`` exits 1. That asymmetry, and whether the CLI should instead
    # reuse the grandfathered arm, is tracked on nexus-vw7zk.
    #
    # NOTE this blocks READS (``nx memory list/get/search``) as well as writes —
    # the yielded handle is the only path to the store, so there is no read-only
    # half to preserve without the same new connection.
    raise click.ClickException(
        "The T2 daemon that arbitrated SQLite-mode access has been retired, and "
        "this install is not on the storage service, so `nx memory` and `nx "
        "config` cannot reach T2 storage (reads included). Run `nx doctor` and, "
        "if it reports a pending substrate migration, `nx upgrade` to converge "
        "onto Postgres."
    )
