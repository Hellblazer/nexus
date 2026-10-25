# SPDX-License-Identifier: AGPL-3.0-or-later
"""T1 session-lease scanner for the nx console health panel."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SessionInfo:
    """Info about one live T1 session (lease-based liveness).

    Ported (nexus-8zfwv, 2026-08-07) off the RDR-149 P4 ``t1_addr.*``
    ``ServiceRegistry`` lease this used to scan -- ``T1LeasePublisher``, the
    only thing that ever published that format, is retired (deleted at
    ff744321). T1 is one shared nexus-service now, not a per-session
    chroma: a session's lease carries a bearer token + expiry
    (``nexus.db.t1.publish_t1_session_lease``), not a host/port/pid
    endpoint, so there is nothing left to TCP-probe or attribute to a pid.
    """

    session_id: str
    expires_at: float
    fresh: bool


def scan_sessions_sync(config_dir: Path) -> list[SessionInfo]:
    """Scan ``t1_session_lease.<session_id>`` files and report liveness.

    A session is live iff its lease file exists, parses, and is unexpired
    (using the same freshness margin ``nexus.db.t1.read_t1_session_lease``
    applies). Malformed/unreadable lease files are silently skipped (a
    display concern, not a correctness one -- ``nexus.health``'s
    orphan-T1-lease check is what reaps them).
    """
    if not config_dir.exists():
        return []

    from nexus.db.t1 import (  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
        _T1_LEASE_FRESHNESS_MARGIN_SECONDS,
        _T1_SESSION_LEASE_PREFIX,
    )

    now = time.time()
    results: list[SessionInfo] = []
    for path in config_dir.glob(f"{_T1_SESSION_LEASE_PREFIX}*"):
        session_id = path.name[len(_T1_SESSION_LEASE_PREFIX):]
        if not session_id:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            expires_at = float(data["expires_at"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue  # not a lease record (malformed / legacy / partial write)

        fresh = now < expires_at - _T1_LEASE_FRESHNESS_MARGIN_SECONDS
        results.append(SessionInfo(
            session_id=session_id,
            expires_at=expires_at,
            fresh=fresh,
        ))

    return results
