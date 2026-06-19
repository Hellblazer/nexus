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
    """Open a T2 handle via the running T2 daemon.

    RDR-120 P6 follow-up (nexus-w6txl): user-facing CLI memory / plan
    commands route through ``T2Client`` so multi-process operators
    (host CLI + Cowork-bridged MCP server + dev-container CLI) share
    a single arbitrated SQLite writer rather than each opening their
    own connection and racing the WAL.

    Returns a context-managed ``T2Client`` connected to the running
    T2 daemon. Raises ``T2DaemonNotReachableError`` if the daemon is
    not running; the message names ``nx daemon t2 start`` as the
    operator fix.

    Tests monkeypatch this helper to yield an in-process
    ``T2Database`` fixture; the call sites use ``.memory.<method>``
    on the yielded object, which is identical between
    ``T2Database.memory`` (a :class:`MemoryStore`) and
    ``T2Client.memory`` (a :class:`_StoreProxy`).

    Operator/debug paths that MUST work when the daemon is offline
    (``nx upgrade``, ``nx doctor``, ``_session_end_launcher``, etc.)
    continue to construct ``T2Database(default_db_path())`` directly
    with ``# epsilon-allow`` tokens — this helper is for the user-
    facing memory/plan surface only.

    Note: ``nx plan`` commands open T2 directly (epsilon-allow) and
    do NOT go through this helper — they must tolerate offline mode.
    """
    import click

    # RDR-152 nexus-fjwxh: in SERVICE mode the Java service (PG) is the write
    # arbiter, so the SQLite single-writer T2 daemon is not in the picture —
    # route directly to a service-backed T2Database (its ``.memory`` is an
    # HttpMemoryStore with the same interface as ``T2Client.memory``). The
    # daemon-client path below is the SQLite-mode arbiter only.
    from nexus.db.storage_mode import StorageBackend, storage_backend_for

    if storage_backend_for("memory") == StorageBackend.SERVICE:
        import httpx

        from nexus.db.t2 import T2Database

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

    from nexus.daemon.t2_client import (
        T2ClientError,
        T2DaemonNotReachableError,
        T2SchemaVersionMismatchError,
        make_t2_client,
    )

    client = make_t2_client()
    try:
        yield client
    except T2ClientError as exc:
        # nexus-00en9: a REACHABLE daemon that returns an error frame mid-RPC
        # otherwise escapes as a raw traceback. T2ClientError is a sibling of the
        # discovery errors below (no inheritance), so this catch never shadows
        # them. Split by error_type so the operator gets the right remedy: a
        # frame-level ProtocolError signals version skew (same class
        # T2SchemaVersionMismatchError surfaces at handshake time), NOT transient
        # contention.
        if exc.error_type == "ProtocolError":
            raise click.ClickException(
                f"T2 protocol error (possible version skew): {exc.message}. "
                "Check versions: nx --version vs the running daemon, then "
                "restart it: nx daemon t2 restart"
            ) from exc
        raise click.ClickException(
            f"T2 store error ({exc.error_type}): {exc.message}. "
            "Retry, or check daemon state: nx daemon t2 status"
        ) from exc
    except T2DaemonNotReachableError:
        # T2Client connects lazily on first RPC; the error surfaces here,
        # not during make_t2_client() construction. Convert to a clean
        # one-liner so the user sees an actionable message, not a traceback.
        raise click.ClickException(
            "No T2 daemon discovery resolved. "
            "Start with: `nx daemon t2 start`"
        )
    except T2SchemaVersionMismatchError as exc:
        # __str__ already carries client/daemon version + recovery hint.
        raise click.ClickException(str(exc))
    finally:
        client.close()
