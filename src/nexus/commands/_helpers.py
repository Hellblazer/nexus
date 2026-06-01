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
    from nexus.daemon.t2_client import (
        T2DaemonNotReachableError,
        T2SchemaVersionMismatchError,
        make_t2_client,
    )

    client = make_t2_client()
    try:
        yield client
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
