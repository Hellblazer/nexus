# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx-hook mcp-connect-wait``: the interactive MCP connection barrier (RDR-215, nexus-veh77).

**The defect this closes.** Every conexus ``mcp_tool`` hook -- the RDR-184
``PreToolUse Agent|Task`` EXPECT writer, the ``SubagentStop`` tuple
projector, ``Stop`` verification, ``PostToolUse Write|Edit``, every
``SubagentStart`` entry -- is a no-op for any request Claude Code begins
before ``nx-mcp`` has connected: Claude Code reads that as "tool
unavailable" and silently skips the hook, indistinguishable from the hook
correctly firing. Headless ``claude -p`` happens to be safe because it
blocks turn 1 on the MCP connection attempt resolving; interactive Claude
Code does not -- turn 1 starts about 50 ms after submit, whatever state the
server is in. The only thing that closed the window before this hook
existed was incidental: a submitted prompt already waits for every
``SessionStart`` command hook, and those hooks happened to be slow enough,
on the one measured real session, to outlast ``nx-mcp``'s own connect time.

Measured interactively on both host shapes (T2
``nexus/veh77-interactive-ladder-results-2026-09-23``, harness
``tests/cc-validation/connection-race-ladder/``): with the server connecting
8 s after launch, every submit rung from 0 to 2000 ms missed every tool-tier
event, macOS 24/24 runs and WSL2 20/20. The skip is decided per MODEL
REQUEST, not per event -- a request that began before the connection stays
skipped even after the connection completes seconds later.

**The remedy.** Sam's ruling (bead ``nexus-veh77``, 2026-09-23): a
``SessionStart`` command-tier verb waits, bounded and fail-open, for THIS
session's ``nx-mcp`` to be up, so the barrier headless gets by accident
becomes deliberate for interactive too.

**The readiness signal is ``nexus.mcp.connect_marker``, NOT the T1 lease
(round 2, same day).** The first cut of this verb polled
``nexus.db.t1.read_t1_session_lease`` -- published inside
``nexus.mcp.core._t1_lifespan``'s T1 mint-or-borrow critical section, before
that lifespan's ``yield``. That reasoning holds only on the path where T1
mint SUCCEEDS. Enumerated (Sam's round-2 review) from every branch of
``_t1_lifespan`` that still reaches ``yield`` and serves every non-T1 tool
WITHOUT ever publishing a T1 lease: an inherited already-live token
(``USE_INHERITED``, no mint attempted), a no-resolvable-session-id process,
and -- the sharpest case -- a DEFERRED mint (nexus-brw1s: the storage
service is unreachable at MCP boot, so the mint is deferred to first T1 use
and the server starts anyway). That last one fires on precisely the boxes
already least healthy: a fresh install before ``nx daemon service start``
has ever run, a crashed or not-yet-ready local service, a cloud-mode box
with a transient auth or network failure. A T1-lease-keyed barrier would
have cost every one of those boxes the FULL 15 s bound on EVERY session
start, forever, until T1 was fixed -- even though ``nx-mcp`` itself connects
in well under a second on every one of them. That is backwards: it waits
for "T1 is healthy", not "``nx-mcp`` is serving".

So this verb now polls :func:`nexus.mcp.connect_marker.read_mcp_connect_marker`,
a signal published UNCONDITIONALLY -- independent of T1 mint outcome -- from
every branch of ``_t1_lifespan`` right before its own ``yield``. See that
module's docstring for the full enumeration and the file format. A missing
T1 lease now never costs this barrier more than the actual connect time.

**A short-bound heuristic for "``nx-mcp`` was never going to start at all"
(disabled by the user, or a spawn failure) was considered and rejected.**
See ``nexus.mcp.connect_marker``'s docstring for the reasoning: no signal on
this box cleanly discriminates that case from a legitimately slow first
boot, and the two need opposite treatment. The residual -- a genuinely
disabled or never-spawning ``nx-mcp`` still pays the full 15 s bound once
per session -- is accepted; that same session already gets a louder,
independent signal today (``nx-hook preflight``'s ``## nx Preflight:
FAILED`` marker, same matcher group) that nexus tooling is not working here
at all.

**Which SessionStart sources wait.** Only ``startup``. JDR-001
(``docs/rdr/joint/JDR-001-t1-three-scopes.md``) and its own
``nexus-ggvi0`` falsification record establish that the MCP server process
usually PERSISTS across ``/clear``, ``/resume``, ``/compact`` and a fork --
the connection this hook waits for already exists by the time any of those
sources fires, so waiting there only adds latency for no coverage gained.
``startup`` is the one source where the server is provably spawning fresh.
This module itself has no opinion about matcher scoping -- that lives in
``conexus/hooks/hooks.json``, which wires this verb under the ``startup``
matcher only -- but it defends the same boundary at the payload level (the
``source`` field), in case a future rewiring puts it under a broader
matcher by mistake: any ``source`` other than ``startup`` (including a
payload that carries none) is a fast no-op.

**The bound.** 15 seconds, chosen from the ladder's own measurements: the
one real ``nx-mcp`` connect time recorded from a live session's debug log
was 2.1 s (queued about 0.9 s behind other plugin servers), and every
``ladder_s8`` rung in the probe measurement connected by design at 8.15 s
(macOS) / 8.5 s (WSL2) and fired cleanly once the barrier had that much
room. 15 s is a little under 2x the widest measured connect (8.5 s) and
about 7x the one live-session connect (2.1 s) -- generous enough that a
merely-slow connect is covered, short enough that a genuinely dead or
never-spawning server does not hold up a session start for anywhere near
``upgrade-auto``'s own 30 s ceiling in the same ``SessionStart`` matcher
group. ``conexus/hooks/hooks.json``'s entry for this verb carries a timeout
above the bound (RDR-215 Contracts: the manifest timeout must exceed the
verb's own, or Claude Code kills the process before it can fail open on its
own terms).

**Fail-open, always.** This verb is not in
:data:`nexus._hook_runtime.entry.LEDGER_VERBS`, so ``entry.main`` forces
exit 0 regardless of what :func:`run` returns -- there is no verdict here to
propagate, only a wait. A timeout logs one line (``mcp_connect_wait_timed_out``,
via the hook log ``configure_hook_logging`` points at, never stdout -- stdout
is the decision channel) naming the session id and how long it waited, and
returns exactly the same :class:`~nexus._hook_runtime._io.HookResult` the
ready-in-time path returns.

**Test-only bound/poll overrides.** :data:`_TEST_BOUND_OVERRIDE_ENV` and
:data:`_TEST_POLL_OVERRIDE_ENV` let a test (or the connection-race ladder
harness re-run under nexus-veh77) drive :func:`run` through its real
dispatch path with a bound measured in tens of milliseconds instead of 15 s,
the same pattern ``nexus._hook_runtime.entry`` already uses for its own
verb-table and ledger-membership test overrides. Never set outside a test
or measurement process.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

from nexus._hook_runtime._io import HookResult, configure_hook_logging

#: How long, in seconds, this verb waits for THIS session's connect marker
#: to appear before giving up and failing open. See the module docstring
#: for the measured justification.
_BOUND_SECONDS: float = 15.0

#: Poll interval while waiting. Independent of (and much tighter than)
#: ``nexus.mcp.core._T1_HANDOFF_WATCH_INTERVAL_S`` (5 s) -- that loop tolerates
#: a multi-second delay noticing a handoff marker; this one is racing a
#: session's own first turn against a much shorter bound and should notice
#: readiness promptly.
_POLL_INTERVAL_SECONDS: float = 0.2

#: Test-only override for :data:`_BOUND_SECONDS`, read as a float. Malformed
#: or unset falls back to the real default -- never crashes the hook.
_TEST_BOUND_OVERRIDE_ENV = "_NX_HOOK_TEST_MCP_CONNECT_WAIT_BOUND_S"

#: Test-only override for :data:`_POLL_INTERVAL_SECONDS`, same contract.
_TEST_POLL_OVERRIDE_ENV = "_NX_HOOK_TEST_MCP_CONNECT_WAIT_POLL_S"


def _float_override(env_name: str, default: float) -> float:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value < 0:
        return default
    return value


def _resolve_bound_seconds() -> float:
    return _float_override(_TEST_BOUND_OVERRIDE_ENV, _BOUND_SECONDS)


def _resolve_poll_interval_seconds() -> float:
    return _float_override(_TEST_POLL_OVERRIDE_ENV, _POLL_INTERVAL_SECONDS)


def _default_read_ready(session_id: str, config_dir: Path) -> bool:
    from nexus.mcp.connect_marker import read_mcp_connect_marker  # noqa: PLC0415 — deferred for startup cost; only a startup dispatch that actually waits pays this

    return read_mcp_connect_marker(session_id, config_dir)


def wait_for_mcp_connect_marker(
    session_id: str,
    config_dir: Path,
    *,
    bound_seconds: float,
    poll_interval_seconds: float,
    read_ready: Callable[[str, Path], bool] = _default_read_ready,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[bool, float]:
    """Poll *read_ready* for *session_id* until it returns ``True`` or *bound_seconds* elapses.

    Returns ``(ready, elapsed_seconds)``. *read_ready* is
    :func:`nexus.mcp.connect_marker.read_mcp_connect_marker` by default,
    published UNCONDITIONALLY from ``nexus.mcp.core._t1_lifespan`` --
    independent of T1 mint/lease outcome -- so a T1-only degradation never
    shows up here as a missing signal (see that module's docstring for the
    enumerated cases this decoupling fixes).

    *sleep* and *monotonic* are injected so a test can drive the whole loop
    without touching the wall clock: a fake ``monotonic`` that advances by
    ``poll_interval_seconds`` on every ``sleep`` call makes "ready after N
    polls" and "never ready, bound expires" both deterministic and fast.
    """
    start = monotonic()
    while True:
        if read_ready(session_id, config_dir):
            return True, monotonic() - start
        elapsed = monotonic() - start
        if elapsed >= bound_seconds:
            return False, elapsed
        sleep(min(poll_interval_seconds, bound_seconds - elapsed))


def run(payload: dict | None) -> HookResult:
    """Wait for THIS session's ``nx-mcp`` to connect, bounded and fail-open.

    Reads ``session_id`` and ``source`` from *payload* exactly as
    :func:`nexus.hooks.session_start_verb.run` does (the same SessionStart
    stdin contract). A no-op -- no wait, no log line -- for any ``source``
    other than ``startup`` (see the module docstring's "which sources wait"
    section) and for a payload carrying no usable ``session_id`` (nothing to
    key a marker lookup on).
    """
    session_id: str | None = None
    source: str | None = None
    if payload is not None:
        sid = payload.get("session_id")
        session_id = sid if isinstance(sid, str) and sid else None
        src = payload.get("source")
        source = src if isinstance(src, str) and src else None

    if source != "startup" or not session_id:
        return HookResult(stdout=None)

    configure_hook_logging()
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred for startup cost; only a startup dispatch pays this

    bound = _resolve_bound_seconds()
    poll = _resolve_poll_interval_seconds()
    ready, elapsed = wait_for_mcp_connect_marker(
        session_id,
        nexus_config_dir(),
        bound_seconds=bound,
        poll_interval_seconds=poll,
    )
    if not ready:
        import structlog  # noqa: PLC0415 — deferred; only the timeout path logs

        structlog.get_logger(__name__).warning(
            "mcp_connect_wait_timed_out",
            session_id=session_id,
            waited_seconds=round(elapsed, 2),
            bound_seconds=bound,
        )
    return HookResult(stdout=None)
