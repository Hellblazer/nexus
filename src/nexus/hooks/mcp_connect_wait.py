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

**The readiness signal.** ``nexus.mcp.core._t1_lifespan`` Branch 0 (T1
service path) mints this session's T1 token and calls
``nexus.db.t1.publish_t1_session_lease`` -- inside the same mint-or-borrow
critical section, before the lifespan's own ``yield`` -- and an MCP server
built on the ``mcp`` SDK's lifespan contract cannot answer ``initialize``
(the handshake Claude Code waits on before it will call any tool, and the
event this module's own callers read as "connected") until that ``yield``
returns and the transport's request loop starts. So on ``NEXUS_CONFIG_DIR``,
a fresh, non-expired ``t1_session_lease.<session_id>`` file existing for
THIS session's id is available no later than the moment ``nx-mcp`` can
begin serving -- it is written on the causal path to that moment, not a
proxy sampled after the fact. ``session_id`` here is Claude Code's own
SessionStart payload field, byte-identical to what ``nx-mcp`` itself
resolves at spawn via ``CLAUDE_CODE_SESSION_ID`` (``nexus.session.
resolve_active_session_id``'s tier 3, "harness-provided means correct AT
SPAWN") -- so waiting on THIS session's lease file, keyed on THIS payload's
id, cannot be satisfied by a stale lease left over from an unrelated prior
process; a lease for a DIFFERENT session id is simply never read (see
:class:`TestMcpConnectWait`'s wrong-session-id case).

The one gap this signal has: ``_t1_lifespan``'s DEFERRED-mint branch (the
storage service was unreachable at MCP startup, nexus-brw1s) never
publishes a lease at all, and the server still proceeds to serve every
non-T1 tool. A wait keyed on this signal times out on that box exactly as
it would on a genuinely dead server -- which is the correct, fail-open
answer for BOTH: this hook has no way to distinguish "server is slow" from
"server came up degraded" and does not need to; either way capping the wait
and moving on is right.

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
deferred-mint server does not hold up a session start for anywhere near
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

#: How long, in seconds, this verb waits for THIS session's T1 lease to
#: appear before giving up and failing open. See the module docstring for
#: the measured justification.
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


def _default_read_lease(session_id: str, config_dir: Path) -> str | None:
    from nexus.db.t1 import read_t1_session_lease  # noqa: PLC0415 — deferred for startup cost; only a startup dispatch that actually waits pays this

    return read_t1_session_lease(session_id, config_dir)


def wait_for_t1_lease(
    session_id: str,
    config_dir: Path,
    *,
    bound_seconds: float,
    poll_interval_seconds: float,
    read_lease: Callable[[str, Path], str | None] = _default_read_lease,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[bool, float]:
    """Poll *read_lease* for *session_id* until it returns a token or *bound_seconds* elapses.

    Returns ``(ready, elapsed_seconds)``. *read_lease* is
    :func:`nexus.db.t1.read_t1_session_lease` by default, which already
    treats a lease past its stored ``expires_at`` -- or one for a session id
    that never had one published -- as absent, so a stale or unrelated lease
    file is never mistaken for readiness (see that function's own docstring).

    *sleep* and *monotonic* are injected so a test can drive the whole loop
    without touching the wall clock: a fake ``monotonic`` that advances by
    ``poll_interval_seconds`` on every ``sleep`` call makes "ready after N
    polls" and "never ready, bound expires" both deterministic and fast.
    """
    start = monotonic()
    while True:
        token = read_lease(session_id, config_dir)
        if token:
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
    key a lease lookup on).
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
    ready, elapsed = wait_for_t1_lease(
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
