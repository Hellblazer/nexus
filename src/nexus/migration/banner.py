# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-159 P1b (nexus-ue6g7.8): the degrade-LOUD read-surface banner.

While the ``migration.state`` sentinel reports ``migrating`` (or the terminal
``migrated-failed``), every read surface must prepend a LOUD warning so a user
never mistakes a partial mid-migration corpus for the whole knowledge base.
RDR-159 §"Read-surface coverage" enumerates and LOCKS the five surfaces that
wrap their return value with :func:`degrade_loud_when_migrating` (or, for the
``nx search`` CLI which echoes rather than returns, emit :func:`migration_banner`
directly): ``search``, ``store_get``, ``store_get_many``, ``nx_answer`` (at the
entry point, not inside the plan runner), and the ``nx search`` CLI.

The banner is additive — the surfaces still return whatever has landed
(monotonically improving as collections migrate), never a bare empty result.
For string results the banner is prepended as text; for structured (dict)
results it is attached under a ``migration_warning`` key so machine consumers
see it without a torn payload.
"""
from __future__ import annotations

import functools
import inspect
from typing import Any, Callable, TypeVar

from nexus.migration.state import (
    MIGRATED_FAILED,
    MIGRATING,
    MigrationState,
    read_state,
)

#: The dict key under which structured surfaces carry the warning.
MIGRATION_WARNING_KEY: str = "migration_warning"

_F = TypeVar("_F", bound=Callable[..., Any])


def _format_banner(state: MigrationState) -> str:
    done = state.collections_done
    total = state.collections_total
    if state.phase == MIGRATING:
        return (
            f"⚠️  knowledge migrating: {done}/{total} collections done, "
            "results incomplete until the upgrade completes"
        )
    # migrated-failed: keep the banner, point at the report + rollback.
    return (
        f"⚠️  knowledge migration FAILED: {done}/{total} collections done, "
        "results incomplete — see the migration report; roll back or re-run: "
        "nx migrate-to-service"
    )


def migration_banner() -> str | None:
    """Return the LOUD banner for the current phase, or ``None`` to stay silent.

    A banner is emitted only while ``migrating`` or on the terminal
    ``migrated-failed`` state. Any other phase (including the absent-sentinel
    ``not-migrating`` default) returns ``None`` so surfaces behave normally.
    """
    state = read_state()
    if state is None or state.phase not in (MIGRATING, MIGRATED_FAILED):
        return None
    return _format_banner(state)


def with_migration_banner(result: Any) -> Any:
    """Attach the migration banner to a surface result when migrating.

    * ``str`` result → banner prepended as a leading paragraph.
    * ``dict`` result → banner attached under ``migration_warning`` (a shallow
      copy; the landed payload is preserved verbatim).
    * anything else → returned untouched.

    A no-op (returns ``result`` unchanged) when no banner applies.
    """
    banner = migration_banner()
    if banner is None:
        return result
    if isinstance(result, dict):
        return {**result, MIGRATION_WARNING_KEY: banner}
    if isinstance(result, str):
        return f"{banner}\n\n{result}"
    return result


def degrade_loud_when_migrating(fn: _F) -> _F:
    """Wrap a read-surface tool so its return value carries the banner.

    Covers every return path of the wrapped function uniformly (the MCP read
    surfaces have many early returns). Async-aware: an async surface keeps its
    coroutine shape and the awaited result is wrapped. Placed UNDER ``@mcp.tool``
    so the registered tool is the wrapped callable; ``functools.wraps`` preserves
    the signature FastMCP introspects for the tool schema.
    """
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
            return with_migration_banner(await fn(*args, **kwargs))

        return _async_wrapper  # type: ignore[return-value]

    @functools.wraps(fn)
    def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        return with_migration_banner(fn(*args, **kwargs))

    return _sync_wrapper  # type: ignore[return-value]
