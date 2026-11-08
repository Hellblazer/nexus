# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The memory verbs: ``memory.get``, ``memory.list``, ``memory.search``.

RDR-242 Technical Design 6, 9 and 10; nexus-mdbds.7. They run in the parent,
over the same T2 store the MCP memory tools use (``nexus.mcp_infra.t2_ctx``,
then ``db.memory``), and return structured values, not the formatted MCP text.

The parent runs each verb on a helper thread and abandons it at the deadline,
so these verbs keep no deadline logic beyond handing the store request the
remaining budget as its timeout (so an abandoned call does not outlive the run
by the client default). A deadline already past raises ``VerbTimeout`` before
the request; a store timeout raises ``VerbTimeout`` too.

Decisions the RDR leaves open (T2 nexus/rdr242-interfaces):

* ``memory.get`` of a missing entry is nil. An ambiguous title prefix is a
  ``VerbError`` naming the candidates, never nil: a script must not read
  "several entries match" as "none does".
* ``memory.get`` and ``memory.list`` refuse an empty project or title (an
  empty title is a prefix of every title; ``list_entries`` reads an empty
  project as every project). ``memory.search`` takes ``""`` for every
  project, as the MCP tool does. Refusals are ``VerbArgError``.
* ``memory.search``'s ``limit`` is 1..300 (``MAX_QUERY_RESULTS``); outside it
  is refused, never clamped. nil means the store default, at most 300 rows.
* ``memory.search`` passes ``access="silent"``: the store's default,
  ``"track"``, increments ``access_count``, a write, and this runtime has no
  write verbs. The engine's resolve endpoint has no such option, so
  ``memory.get`` still counts one access on the entry it returns, exactly as
  the MCP ``memory_get`` does (see the hand-back of nexus-mdbds.7).
* A result over ``VERB_RESULT_CAP_BYTES`` is refused (``encode_result``),
  never truncated.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from nexus.db.limits import MAX_QUERY_RESULTS
from nexus.lua_runtime.verb_types import (
    JSONValue,
    ParamSpec,
    VerbArgError,
    VerbContext,
    VerbError,
    VerbSpec,
    VerbTimeout,
    encode_result,
)


def _remaining(deadline: float) -> float:
    """Seconds left of the run budget; ``VerbTimeout`` once none is."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise VerbTimeout("the run's wall-time budget is spent")
    return left


def _plain(row: dict[str, Any]) -> dict[str, JSONValue]:
    """A store row as plain JSON: scalar fields only."""
    return {
        k: v
        for k, v in row.items()
        if v is None or isinstance(v, (bool, int, float, str))
    }


def _within_cap(value: JSONValue) -> JSONValue:
    encode_result(value)  # raises VerbError past the cap; the value is not cut
    return value


def _call_store(verb: str, deadline: float, call):
    """Run ``call(timeout)`` with the remaining budget; map a store timeout."""
    timeout = _remaining(deadline)
    try:
        return call(timeout)
    except httpx.TimeoutException as exc:
        raise VerbTimeout(f"{verb}: the memory store did not answer in time") from exc


def _t2():
    from nexus.mcp_infra import t2_ctx  # noqa: PLC0415 — heavy import, parent only

    return t2_ctx()


def _get(project: str, title: str, *, deadline: float, ctx: VerbContext) -> JSONValue:
    if not title:
        raise VerbArgError("memory.get: title must not be empty")
    with _t2() as db:
        entry, candidates = _call_store(
            "memory.get",
            deadline,
            lambda t: db.memory.resolve_title(
                project, title, timeout=t, retry_read_timeout=False
            ),
        )
    if entry is not None:
        return _within_cap(_plain(entry))
    if candidates:
        names = ", ".join(repr(c["title"]) for c in candidates)
        raise VerbError(
            f"memory.get: ambiguous title prefix {title!r} in project {project!r}: "
            f"{len(candidates)} entries match ({names}); use the full title or a "
            "longer prefix"
        )
    return None


def _list(project: str, *, deadline: float, ctx: VerbContext) -> JSONValue:
    if not project:
        raise VerbArgError("memory.list: project must not be empty")
    with _t2() as db:
        rows = _call_store(
            "memory.list",
            deadline,
            lambda t: db.memory.list_entries(
                project=project, timeout=t, retry_read_timeout=False
            ),
        )
    return _within_cap([_plain(r) for r in rows])


def _search(
    query: str,
    project: str,
    limit: int | None = None,
    *,
    deadline: float,
    ctx: VerbContext,
) -> JSONValue:
    if not query.strip():
        raise VerbArgError("memory.search: query must not be blank")
    if limit is not None and not 1 <= limit <= MAX_QUERY_RESULTS:
        raise VerbArgError(
            f"memory.search: limit must be between 1 and {MAX_QUERY_RESULTS}, "
            f"got {limit}"
        )
    with _t2() as db:
        try:
            rows = _call_store(
                "memory.search",
                deadline,
                lambda t: db.memory.search(
                    query,
                    project=project or None,
                    access="silent",
                    timeout=t,
                    retry_read_timeout=False,
                ),
            )
        except ValueError as exc:  # the store's "no searchable terms" refusal
            raise VerbError(f"memory.search: {exc}") from exc
    return _within_cap([_plain(r) for r in rows[: limit or MAX_QUERY_RESULTS]])


VERBS: tuple[VerbSpec, ...] = (
    VerbSpec(
        "memory.get",
        (ParamSpec("project", "string"), ParamSpec("title", "string")),
        _get,
    ),
    VerbSpec("memory.list", (ParamSpec("project", "string"),), _list),
    VerbSpec(
        "memory.search",
        (
            ParamSpec("query", "string"),
            ParamSpec("project", "string"),
            ParamSpec("limit", "integer", optional=True),
        ),
        _search,
    ),
)
