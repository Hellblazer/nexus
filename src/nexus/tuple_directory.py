# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-208 Phase 2 Step 2 (bead nexus-galkv.10): session-directory name
resolution -- resolving a `mailbox_send` `to` argument to a concrete
`mailbox/<address>` + `address_kind`, and the default `from` sender
identity.

Kept separate from `nexus.mcp.core` (rather than inlined into the
`mailbox_send` tool) so RDR-208 Phase 2 Step 4's `nx tuple directory`
CLI verb (bead nexus-galkv.11) can reuse the SAME resolution logic
instead of a second, independently-drifting copy.

`to` resolution (RDR-208, Sam's decision 1 --
T2 nexus_rdr/208-decision-gate-2026-09-14):
  - a session-id shape (a UUID, the shape `nexus.session`'s
    `str(uuid4())` session ids take) resolves directly,
    `address_kind="session"`;
  - an agent-id shape ("a" + 16 lowercase hex chars -- the ONLY shape the
    Agent tool's own opaque `agent_id` can take, confirmed against this
    session's own claimant id and the fixture id
    `tests/hooks/test_subagent_stop_hook.py:288` uses) resolves directly,
    `address_kind="agent"` -- NEVER through the directory;
  - anything else is a NAME: every live row of `directory/<name>` is read
    (paged with the `since` cursor until a short page) and its distinct
    `session_id` dims are collected. Zero holders is a
    :class:`DirectoryResolutionError` naming the name; one holder
    resolves to that session (several live rows of the SAME session --
    a re-armed watcher's new nonce beside its old row -- are one holder,
    not a conflict); more than one distinct holder is a
    :class:`DirectoryResolutionError` naming every holder, and nothing is
    written by a caller that raises before ever writing.

`from` resolution mirrors the bead's rejected-alternatives reasoning: the
default is the tuple-watch session marker for this MCP server's claude
ancestor (`nexus.tuple_watch._read_session_marker`, written synchronously
by SessionStart on every source -- see that module's own docstrings),
falling back to `NX_T1_SESSION_ID`. `resolve_active_session_id()` and a
bare `NX_T1_SESSION_ID` read are deliberately NOT used here: the former
returns the spawn-time `CLAUDE_CODE_SESSION_ID`, stale after an
in-process `/clear`; the latter lags the T2 re-lease mint. A caller
resolving neither is refused rather than stamping a `from` that might
already be stale.
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Any

from nexus.db.limits import MAX_QUERY_RESULTS

#: "a" + 16 lowercase hex chars -- the ONLY shape the Agent tool's own
#: opaque `agent_id` can take (it has no `name` parameter to produce
#: anything else). Confirmed against two REAL ids, not just the fixture:
#: this worktree's own claimant id ("a0b56337bc52ed773", from the
#: SubagentStart hook's "Claimant id:" line) and
#: `tests/hooks/test_subagent_stop_hook.py:288`'s fixture id
#: ("a16b397f79df79c42").
_AGENT_ID_RE = re.compile(r"^a[0-9a-f]{16}$")


class DirectoryResolutionError(RuntimeError):
    """`to` or `from_address` could not be resolved to exactly one
    address. Raised before any tuple is written, so a caller that lets
    this propagate has written nothing."""


def looks_like_session_id(value: str) -> bool:
    """True when *value* parses as a UUID."""
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def looks_like_agent_id(value: str) -> bool:
    """True when *value* is "a" + 16 lowercase hex chars."""
    return bool(_AGENT_ID_RE.fullmatch(value))


def resolve_send_address(to: str, tuples: Any) -> tuple[str, str]:
    """Resolve `mailbox_send`'s `to` into `(address, address_kind)`.

    *tuples* is an `HttpTupleStore` (or anything exposing the same `rd`
    signature) -- passed in rather than resolved here so the caller
    controls which T2 client/transaction this read runs against. A
    directory read that raises propagates unchanged: nothing about `to`
    was resolved, so the caller never reaches a write.
    """
    if not to:
        raise DirectoryResolutionError("to must not be empty")
    if looks_like_session_id(to):
        return to, "session"
    if looks_like_agent_id(to):
        return to, "agent"

    holders: set[str] = set()
    subspace = f"directory/{to}"
    since: tuple[str, str] | None = None
    while True:
        rows = tuples.rd(subspace, {"name": to}, n=MAX_QUERY_RESULTS, since=since)
        for row in rows:
            session_id = (row.dims or {}).get("session_id")
            if session_id:
                holders.add(session_id)
        if len(rows) < MAX_QUERY_RESULTS:
            break
        last = rows[-1]
        since = (last.created_at, last.id)

    if not holders:
        raise DirectoryResolutionError(f"no live holder for name {to!r}")
    if len(holders) > 1:
        ids = ", ".join(sorted(holders))
        raise DirectoryResolutionError(
            f"name {to!r} is held by more than one session ({ids}); resend to one of these session ids directly"
        )
    return next(iter(holders)), "session"


def validate_from_address(value: str) -> str:
    """A caller-supplied `from_address` override must itself be a session
    id or an agent id -- a subagent sharing its parent's MCP server would
    otherwise stamp the parent's session id, so it passes its OWN agent
    id here instead."""
    if looks_like_session_id(value) or looks_like_agent_id(value):
        return value
    raise DirectoryResolutionError(
        f"from_address {value!r} is neither a session id nor an agent id"
    )


def resolve_default_from(*, state_dir: Path | None = None, claude_pid: int | None = None) -> str:
    """Default `from` for `mailbox_send` when no `from_address` is given.

    *state_dir*/*claude_pid* default to `nexus.config.nexus_config_dir()`
    and `nexus.session.find_immediate_claude_pid()` respectively -- the
    parameters exist so a caller (tests; the eventual `nx tuple directory`
    CLI verb) can pin both without monkeypatching process introspection.
    """
    from nexus import config as _nx_config  # noqa: PLC0415 — deferred: MCP/CLI startup cost, rare path
    from nexus.session import find_immediate_claude_pid  # noqa: PLC0415 — deferred: MCP/CLI startup cost, rare path
    from nexus.tuple_watch import _read_session_marker  # noqa: PLC0415 — deferred: MCP/CLI startup cost, rare path

    sd = state_dir if state_dir is not None else _nx_config.nexus_config_dir()
    pid = claude_pid if claude_pid is not None else find_immediate_claude_pid()
    marker = _read_session_marker(sd, pid) if pid else None
    if marker:
        return marker

    env_id = os.environ.get("NX_T1_SESSION_ID", "").strip()
    if env_id:
        return env_id

    raise DirectoryResolutionError(
        "no tuple-watch session marker and NX_T1_SESSION_ID is unset; "
        "refusing to send with an unresolvable from"
    )
