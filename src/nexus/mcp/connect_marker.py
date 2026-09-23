# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The MCP connect-readiness marker (RDR-215, nexus-veh77 round 2).

**Why this exists, separately from the T1 lease.** `nexus.hooks.
mcp_connect_wait`'s first cut used `nexus.db.t1.publish_t1_session_lease`
as its readiness signal, reasoning that the lease is published on the
causal path to `nx-mcp` answering `initialize`. That is true on the
SUCCESSFUL-mint path, and only there. Enumerated from
`nexus.mcp.core._t1_lifespan` (Sam's review, 2026-09-23): the lease is
NEVER published in every one of these cases, all of which still reach
`yield` and serve every non-T1 tool normally --

- **USE_INHERITED** (an already-live `NX_T1_SESSION` inherited from a
  parent process): no mint attempt happens at all.
- **USE_LEASED, borrowing a lease published by a DIFFERENT, earlier
  process**: this process never publishes one of its own; a signal keyed
  on THIS process's own publish would need the earlier one already there,
  in which case it is there anyway (not a new stall) -- see below.
- **No resolvable session id**: `resolve_active_session_id()` returns
  `None`; there is nothing to key a lease on.
- **Deferred mint (nexus-brw1s)**: the storage service is unreachable
  (down, not yet started, cloud auth/network failure) at MCP boot. The
  mint is deferred to first T1 use; the server still starts and serves
  every non-T1 tool. This is the SHARPEST case: it fires on precisely the
  boxes already least healthy -- a fresh install before `nx daemon
  service start` has ever run, a crashed or not-yet-ready local service, a
  cloud-mode box with a transient auth or network failure -- and a
  T1-lease-keyed barrier would cost those boxes the FULL bound on EVERY
  session start, forever, until the operator fixes T1, even though
  `nx-mcp` itself connects in well under a second.

So a T1-lease-keyed barrier waits for "T1 is healthy", not "`nx-mcp` is
serving" -- the wrong question, and one that penalizes exactly the boxes a
startup barrier should be gentlest on.

**The fix.** This module's marker is published UNCONDITIONALLY, best-effort,
at every point `nexus.mcp.core._t1_lifespan` is about to `yield` -- every
branch, T1-healthy or not -- so its presence answers "has an `nx-mcp`
process for this session id reached the point past which it can serve
`initialize`", decoupled entirely from T1 mint/lease mechanics. A missing
lease now never costs the barrier more than the actual connect time: T1
being down no longer stalls SessionStart at all beyond however long
`nx-mcp`'s own non-T1 startup work takes.

**File shape**, deliberately smaller than the T1 lease's: no secret, so no
token field and no `0o600` need -- ``{"pid": <int>, "published_at": <unix
ts>, "expires_at": <unix ts>}`` at ``<config_dir>/mcp_connect_marker.
<session_id>``, atomic temp-file + ``os.replace`` (same pattern as
:func:`nexus.db.t1.publish_t1_session_lease`). ``expires_at`` guards only
against a genuinely ancient leftover from a killed process reusing the same
session id (astronomically unlikely -- session ids are per-conversation
UUIDs) outliving a fresh process's own wait; it is not a liveness protocol.

**A short-bound heuristic for "`nx-mcp` was never going to start at all"
(disabled by the user, or a spawn failure) was considered and rejected**
(Sam's round-2 ask). No signal on this box cleanly discriminates that case
from "this is the very first `nx-mcp` boot ever on this machine, and a
first-boot local PG init/migration run may legitimately take LONGER than
steady state" -- and the two need OPPOSITE treatment. A "have we ever seen
this marker before" flag would shorten the bound on exactly the highest-
value, most sympathetic case (a brand new user's very first session) to
protect against a rarer, self-inflicted one (a user who deliberately
disabled the nexus MCP server). Getting the direction wrong there is worse
than the residual it would guard against: a genuinely disabled or
never-spawning `nx-mcp` still pays the full bound once per session, which
is a bounded, session-start-only cost -- and that same session already
gets a LOUD, independent signal today (`nx-hook preflight`'s ``## nx
Preflight: FAILED`` marker, same `SessionStart` matcher group, probing `nx
--version` reachability) that nexus tooling is not working in this session
at all. No heuristic here; see `nexus.hooks.mcp_connect_wait`'s own
docstring for where this residual is recorded against the verb.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from uuid import uuid4

#: File-name prefix, mirroring ``nexus.db.t1``'s ``t1_session_lease.`` --
#: a distinct name so this marker is never confused with, or accidentally
#: read as, the T1 lease it replaces as a readiness signal.
_MARKER_PREFIX = "mcp_connect_marker."

#: Generous on purpose: this only guards against a genuinely ancient
#: leftover file (a killed process, an unlikely session-id reuse) outliving
#: any real wait window (the barrier's own bound is a small fraction of
#: this). Not a liveness protocol -- there is no owner to time out.
_DEFAULT_TTL_SECONDS: float = 3600.0


def _marker_path(session_id: str, config_dir: Path) -> Path:
    return config_dir / f"{_MARKER_PREFIX}{session_id}"


def publish_mcp_connect_marker(
    session_id: str,
    config_dir: Path,
    *,
    ttl_seconds: float = _DEFAULT_TTL_SECONDS,
) -> None:
    """Publish (or refresh) the connect-readiness marker for *session_id*.

    Called from every branch of ``nexus.mcp.core._t1_lifespan`` right
    before its own ``yield``, unconditionally -- never gated on T1 mint
    outcome. Atomic temp-file + ``os.replace``, matching
    :func:`nexus.db.t1.publish_t1_session_lease`'s own pattern, so a
    concurrent reader never observes a torn write. Idempotent: publishing
    twice for the same session id simply refreshes ``published_at``/
    ``expires_at``.
    """
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _marker_path(session_id, config_dir)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    now = time.time()
    payload = json.dumps(
        {"pid": os.getpid(), "published_at": now, "expires_at": now + ttl_seconds}
    ).encode("utf-8")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


def read_mcp_connect_marker(session_id: str, config_dir: Path) -> bool:
    """Has *session_id*'s `nx-mcp` published a fresh connect marker?

    A missing file, a malformed one, or one past its own ``expires_at`` all
    read as ``False`` -- fail-safe, matching
    :func:`nexus.db.t1.read_t1_session_lease`'s own posture.
    """
    path = _marker_path(session_id, config_dir)
    try:
        raw = path.read_text()
    except OSError:
        return False
    try:
        data = json.loads(raw)
        expires_at = float(data["expires_at"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return False
    return time.time() < expires_at


def clear_mcp_connect_marker(session_id: str, config_dir: Path) -> None:
    """Remove *session_id*'s marker, best-effort. Missing file is not an error.

    Called at every one of ``_t1_lifespan``'s teardown points, mirroring
    :func:`nexus.db.t1.clear_t1_session_lease`'s own unconditional-unlink-
    at-teardown contract -- safe here for the identical reason: nothing
    else will ever read or republish this exact session id's marker once
    this process's lifespan has ended.
    """
    path = _marker_path(session_id, config_dir)
    try:
        path.unlink()
    except OSError:
        pass
