# SPDX-License-Identifier: AGPL-3.0-or-later
"""T1 lease discovery (RDR-149 P4 read-path residue).

This module used to own both the write path (``T1LeasePublisher``,
publishing to the ``ServiceRegistry``-backed ``t1_addr.<key>`` lease format
under the locked RF-2 transient-key -> session-id re-key protocol) and the
read path (:func:`discover_t1_lease`, :func:`discover_t1_by_claude_ancestor`).
The chroma-backed MCP lifespan branch that constructed ``T1LeasePublisher``
(``nexus.mcp.core._t1_lifespan`` Branch 3) died at RDR-155 P4b / RDR-158 P3;
an exhaustive grep confirmed zero production construction sites, so the
publisher and its dedicated tests were retired (nexus-yfh5x).

Nothing in production publishes the ``t1_addr.*`` format any more.
:func:`discover_t1_lease` is still called by ``nx doctor --check-t1``
(:mod:`nexus.commands.doctor`), so it stays live code, but it will find no
lease until that checker (and ``health._check_orphan_t1`` /
``console/watchers.py``'s ``t1_addr.*`` scan) is updated to match --
that consumer-surface breakage is nexus-8zfwv (P1: nothing publishes
``t1_addr.*`` any more, so ``--check-t1`` false-negatives on every
resolved session; empirically confirmed 2026-08-07 with live MCP
servers). Live T1 leasing today goes through the unrelated
:mod:`nexus.db.t1` mint/lease mechanism (``publish_t1_session_lease`` /
``read_t1_session_lease``), which this module does not touch.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import structlog

from nexus.daemon.service_registry import Clock, LeaseRecord, ServiceRegistry

_log = structlog.get_logger(__name__)


def discover_t1_lease(
    session_id: Optional[str],
    *,
    config_dir: Path,
    clock: Clock = time.time,
) -> Optional[tuple[str, int]]:
    """Resolve the live T1 endpoint for ``session_id``, or ``None``.

    The session-id-keyed read path that replaces the legacy
    ``find_immediate_claude_pid`` PPID-walk + ``read_t1_addr_for`` (RDR-149
    P4). Liveness is lease freshness (TTL), not pid: a dead owner's lease
    ages out, giving pid-reuse immunity. Returns ``None`` for an empty
    session-id, a missing/expired/shutdown lease, or a malformed endpoint;
    callers fall back to the env-passdown path (RDR-105 Path A) or fail loud
    at the next layer.
    """
    if not session_id or not session_id.strip():
        return None
    registry = ServiceRegistry(dir=config_dir, tier="t1", clock=clock)
    record = registry.discover(session_id.strip())
    if record is None:
        return None
    host = record.endpoint.get("host")
    port = record.endpoint.get("port")
    if not isinstance(host, str) or not isinstance(port, (int, float)):
        return None
    return host, int(port)


def discover_t1_by_claude_ancestor(
    claude_pid: int,
    *,
    config_dir: Path,
    clock: Clock = time.time,
) -> Optional[tuple[str, int]]:
    """Discover a live T1 endpoint by the sibling's immediate Claude ancestor pid.

    The fallback that fires when the session-id lookup
    (:func:`discover_t1_lease`) misses. It is LOAD-BEARING, not a narrow edge
    case: it covers two situations that both leave a Bash sibling unable to
    find its own T1 by session-id.

    * **Cold start** (nexus-0x16i): before the SessionStart hook writes
      ``current_session`` the sibling resolves no session-id at all, so the
      owner's still-transient (server_pid-keyed) lease is unfindable by
      session-id.
    * **Session-id divergence** (nexus-gff3g — the COMMON production case): the
      owner's MCP keyed its lease on its ``NX_SESSION_ID`` while the sibling
      resolves ``current_session`` (written by the SessionStart hook), and the
      two Claude-provided ids differ (resume, multiple concurrent frontends,
      version skew). ``discover_t1_lease`` then returns ``None`` for the
      sibling's id even though the owner's *session-keyed* lease is live.

    In both cases the owner's lease carries the owner's immediate Claude
    ancestor pid in its payload, and the sibling computes the same pid
    identically (RF-6). This matches the fresh (``status == "live"``, within
    TTL) lease — transient OR session-keyed — whose ``payload.claude_pid``
    equals ``claude_pid`` and returns its endpoint.

    Safety: ancestor-pid-targeted (a different session descends from a
    different immediate Claude process, so its lease never matches) and
    TTL-bounded (only fresh leases), so there is no cross-session mis-bind.
    When more than one fresh lease shares the pid (a brief re-key overlap, or
    one Claude process owning multiple MCP servers), the record with the
    newest ``heartbeat_epoch`` wins, so the choice is deterministic rather than
    dependent on ``glob`` ordering. Returns ``None`` if nothing matches; the
    caller then fails loud (Path D).
    """
    if claude_pid <= 0:
        return None
    cfg = Path(config_dir)
    if not cfg.exists():
        return None
    now = clock()
    best: Optional[tuple[float, str, int]] = None
    for path in cfg.glob("t1_addr.*"):
        try:
            record = LeaseRecord.from_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError):
            continue
        # Match transient AND session-keyed leases (nexus-gff3g): control only
        # reaches here after the session-id path missed, and a lease whose
        # owner shares this sibling's immediate Claude ancestor is the
        # sibling's own T1 regardless of how its session-id is labeled. The
        # prior ``session_id is not None: continue`` skip made this inert for
        # every warm/re-keyed lease.
        if record.payload.get("claude_pid") != claude_pid:
            continue
        if not record.is_fresh(now):
            continue
        host = record.endpoint.get("host")
        port = record.endpoint.get("port")
        if not isinstance(host, str) or not isinstance(port, (int, float)):
            continue
        # Deterministic tie-break: newest heartbeat wins (M1/O1).
        if best is None or record.heartbeat_epoch > best[0]:
            best = (record.heartbeat_epoch, host, int(port))
    if best is None:
        return None
    return best[1], best[2]
