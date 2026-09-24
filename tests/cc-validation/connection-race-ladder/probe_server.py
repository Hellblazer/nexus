#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Probe MCP server for the RDR-215 interactive connection-race ladder (nexus-veh77).

Stands in for ``nx-mcp`` as the target of ``mcp_tool`` hooks. Every call to
``probe`` appends one JSON line carrying the hook's marker and a wall-clock
timestamp, so "the tool-tier hook ran" is read from this file and never from
the TUI. ``PROBE_START_DELAY`` sleeps before the MCP handshake is served,
which widens the window in which the server is spawned but not connected;
that is what makes the ladder able to see a race at all, since the real
server connects in well under a second.

``PROBE_BROKEN=1`` exits before serving, which is the negative control: the
client records the connection as failed and every hook aimed here is skipped.

**Barrier re-test (nexus-veh77 round 2, signal switched round 2b).** When
``PROBE_LEASE_CONFIG_DIR`` is set, this process publishes the readiness
signal ``nexus.hooks.mcp_connect_wait`` polls for. Round 2b switched that
signal from the T1 lease to ``nexus.mcp.connect_marker`` (Sam's review: a
T1-lease-keyed barrier stalled every session on a T1-degraded box for the
full bound, even though ``nx-mcp`` itself connects fine) -- so this probe
now writes a ``mcp_connect_marker.<session_id>`` file, byte-identical in
shape to ``nexus.mcp.connect_marker.publish_mcp_connect_marker``'s own
format, at the same point in its own timeline that the real ``nx-mcp``'s
marker publish falls: on the causal path to serving, right after
``PROBE_START_DELAY`` elapses and before the transport starts accepting
requests. ``session_id`` comes from ``CLAUDE_CODE_SESSION_ID``, which Claude
Code sets in every subprocess it spawns (including this one) -- the exact
env var ``nexus.session.resolve_active_session_id``'s tier 3 reads, and the
same value the SessionStart hook payload's own ``session_id`` field carries
for this same session. A missing session id or config dir is a silent no-op
(logged, never fatal): the probe still serves either way, since the point of
the barrier round is to see whether the WAITING hook changes the outcome,
not to make the probe depend on it.

**Import ordering is load-bearing for the barrier round specifically.**
``FastMCP`` is imported and ``mcp`` constructed BEFORE the delay/marker-
publish logic below, not after -- mirroring the real ``nx-mcp``'s own
shape, where ``mcp = FastMCP("nexus", lifespan=_t1_lifespan)`` is built at
IMPORT time and the marker publish happens INSIDE the lifespan coroutine
``mcp.run()`` later invokes, so by the time a real marker is published
there is nothing left before serving except the transport loop itself
starting. An earlier version of this probe imported ``FastMCP`` AFTER
publishing, so the publish briefly preceded the point the probe could
actually serve; measured directly (round 2b re-verify, rep 0): the marker
was visible ~135 ms after the barrier's poll (well within its 0.2 s poll
interval) but the probe's own ``serving`` line landed 179 ms after
publish -- a 44 ms window where the barrier had already released but the
probe was not yet actually serving, and the request that started in that
window missed its tool-tier hooks (RDR-215 finding (b): the skip is
decided per REQUEST, fixed at request-start, not retroactively cured by a
later-completing connection). The SAME gap was present in round 2's first
barrier run too (measured 172-184 ms there), it simply happened not to
straddle a poll boundary either time. Not a defect in the shipped verb or
the marker; a probe-fidelity gap this reorder closes.
"""
import json
import os
import sys
import time
import uuid
from typing import Any

from mcp.server.fastmcp import FastMCP

LOG = os.environ["PROBE_LOG"]


def _log(payload: dict) -> None:
    payload["ts"] = time.time()
    with open(LOG, "a") as f:
        f.write(json.dumps(payload, default=repr) + "\n")


def _publish_lease_if_configured() -> None:
    """Best-effort: write the same connect-marker file
    nexus.mcp.connect_marker.publish_mcp_connect_marker writes, keyed on
    this process's own CLAUDE_CODE_SESSION_ID, into PROBE_LEASE_CONFIG_DIR
    -- the barrier round's readiness signal (round 2b: the marker, not the
    T1 lease; see the module docstring)."""
    config_dir = os.environ.get("PROBE_LEASE_CONFIG_DIR", "").strip()
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if not config_dir or not session_id:
        _log({"event": "lease_publish_skipped", "config_dir": config_dir,
              "session_id": session_id})
        return
    try:
        d = os.path.abspath(config_dir)
        os.makedirs(d, mode=0o700, exist_ok=True)
        path = os.path.join(d, f"mcp_connect_marker.{session_id}")
        tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        now = time.time()
        payload = json.dumps(
            {"pid": os.getpid(), "published_at": now, "expires_at": now + 3600.0}
        ).encode("utf-8")
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        _log({"event": "lease_published", "path": path, "session_id": session_id})
    except OSError as exc:
        _log({"event": "lease_publish_failed", "error": repr(exc)})


# Built BEFORE the delay/marker-publish/serve sequence below -- see the
# module docstring's "import ordering is load-bearing" note.
mcp = FastMCP("probe")


@mcp.tool()
def probe(marker: Any = "", session_id: Any = "") -> str:
    """Record that a hook reached this server."""
    _log({"event": "probe_called", "marker": marker, "session_id": session_id})
    return "probe recorded"


if __name__ == "__main__":
    _DELAY = float(os.environ.get("PROBE_START_DELAY", "0"))
    _log({"event": "process_launched", "pid": os.getpid(), "start_delay_s": _DELAY})
    if os.environ.get("PROBE_BROKEN") == "1":
        _log({"event": "broken_exit"})
        sys.exit(3)
    if _DELAY:
        time.sleep(_DELAY)
        _log({"event": "start_delay_elapsed"})
    _publish_lease_if_configured()
    # Nothing left before serving except the transport loop itself starting
    # -- FastMCP and `mcp` were already built above, matching the real
    # nx-mcp's shape (see the module docstring).
    _log({"event": "serving"})
    mcp.run()
