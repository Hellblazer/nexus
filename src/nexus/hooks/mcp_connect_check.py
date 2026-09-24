# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx-hook mcp-connect-check``: detect a MID-SESSION `nx-mcp` disconnect
(RDR-215, nexus-veh77 round 5).

**The gap this closes.** `nexus.hooks.mcp_connect_wait` (the SessionStart
barrier) protects only the START of a session -- it runs once and exits.
The 2026-09-20 incident this epic's own history records (a live `nx-mcp`
process exited and was respawned mid-session, the client binding stayed
stale about two minutes, every `mcp_tool` hook silently unavailable the
whole window) is not a `SessionStart` event, and nothing previously
watched for it: checked and confirmed (RDR-215 round-3 entry) that
`nexus.upgrade_finish.StaleProcess.restartable` excludes `mcp-host`
specifically BECAUSE Claude Code does not auto-reconnect stdio MCP
servers -- that is nexus's own automation avoiding the hazard, not a
detector of it happening for any other reason (an OS crash, an OOM kill,
a manual disconnect/reconnect) -- and the T1 handoff watcher watches for
a NEW session id, a different failure mode from a dropped connection on
the SAME one.

**What this verb does.** Wired on `UserPromptSubmit` (a command-tier
event, so it needs no live MCP server connection to fire -- the same
guarantee `nexus.hooks.mailbox_drain` already relies on for the identical
reason), it checks whether THIS session's `nx-mcp` connect marker
(`nexus.mcp.connect_marker`) both EXISTS and names a PID that is still
alive. A session that never connected in the first place stays silent --
that is the startup barrier's job, and warning here too would be a
duplicate, confusing signal. A session that WAS connected and no longer
is gets ONE visible note, not one per prompt: a small per-session state
file remembers whether this disconnect episode has already been
reported, and resets the moment the marker is seen alive again (so a
LATER disconnect, after a successful reconnect, gets its own fresh
warning).

**Why a separate verb, not folded into `mailbox-drain`.** Considered and
rejected: `mailbox_drain.py` is a 1200-line module with ONE documented
contract ("THE CONSUMER OF RECORD... the unconditional FLOOR" for RDR-205
mailbox delivery), a real network round trip against the tuple-space
engine bounded by its own `_TOTAL_BUDGET_S`, and an extensive existing
test suite built around that one concern. This check is unrelated
(session MCP-connection health, not mailbox delivery), touches no
network (two file reads, one syscall), and folding it in would force
reconciling two independent failure/budget/test philosophies inside one
already-large module for no shared benefit -- hooks under one event
already run in parallel (the SAME reason `mcp-connect-wait`'s own
SessionStart entries are separate hooks rather than one combined verb),
so there is no latency cost to keeping them apart, and a great deal of
clarity gained: this verb's own cost, own tests, and own failure mode are
each independently legible.

**Cost, on the hot path that runs on EVERY prompt.** One file read (the
connect marker), one file read/write (the tiny per-session state file),
and one `pid_alive` call -- ``os.kill(pid, 0)`` on POSIX, no subprocess,
no network. No engine round trip, unlike `mailbox-drain`'s own budget.

**`pid_alive` is `nexus.daemon.service_registry.pid_alive`, the ONE
shared implementation** ("Daemon-lifecycle fixes land in the shared
primitive, never one tier's copy," AGENTS.md) -- never a hand-rolled
`os.kill` here. See ``nexus.mcp.connect_marker``'s own docstring for why
there is no pid-reuse start-time guard (the cost budget above forbids the
subprocess/``/proc`` read that would need) and why that is consistent with
this project's own "liveness is lease freshness, not pid" doctrine
(`expires_at` bounds the residual; `pid_alive` layers a fast-reacting
signal on top through the shared primitive, exactly the pattern that
doctrine endorses).

**Native Windows, honestly.** RDR-218 measured `nx-mcp` running as a
genuine native Windows stdio process (not just inside the WSL2
appliance), so `pid_alive`'s Windows behavior is not purely academic
here -- but it was not independently re-verified for THIS call site.
Per Python's documented `os.kill` semantics, signal 0 on Windows collides
with `signal.CTRL_C_EVENT`, which is restricted to processes sharing the
SAME console; `nx-hook` and `nx-mcp` are unrelated, separately-spawned
processes, so that call most likely raises an `OSError` that does not
map to `ProcessLookupError`, which `pid_alive`'s own ambiguous-error-is-
alive default then reads as "alive" regardless of the real state. Net
effect: on native Windows this detector most likely degrades to SILENT
(never fires) rather than dangerous (`TerminateProcess` is never reached
for signal 0, so it cannot kill the target either). That degraded
posture is this repo's EXISTING `pid_alive` behavior, already relied on
by other consumers (`nexus.upgrade_finish`); sharpening it for native
Windows specifically belongs in `nexus.daemon.service_registry` per the
hot rule above, not duplicated here. The WSL2 appliance -- Linux under
the hood -- is unaffected; `os.kill(pid, 0)` there is the ordinary POSIX
call `pid_alive` was written for.

**Not a ledger verb.** Never appears in
:data:`nexus._hook_runtime.entry.LEDGER_VERBS`: there is no caller that
branches on an exit code, so `HookResult`'s default `exit_code=0` is
exactly right, and `entry.main` forces 0 for every non-ledger verb
regardless. Also never fails the dispatch open: every filesystem/pid
operation is wrapped so a surprise (a permissions error, a malformed
state file) degrades to silence rather than a stray traceback in front
of an unrelated prompt -- the same contract `mailbox_drain.py`'s own
module docstring states for itself.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from nexus._hook_runtime._io import HookResult, configure_hook_logging

# nexus.mcp.connect_marker is NOT imported at module scope: nexus/mcp/
# __init__.py eagerly imports nexus.mcp.core (the MCP server module) before
# ANY submodule of the package can be reached, pulling in structlog/rich/
# click/pygments regardless of which submodule you actually wanted --
# measured by tests/hooks/test_hook_runtime_thin.py::
# test_the_real_verb_modules_import_no_structlog, which imports every real
# VERB_TABLE module in a fresh interpreter and asserts none of them do
# this. nexus.hooks.mcp_connect_wait already defers this same import for
# the identical reason (see its ``_default_read_ready``); this module
# follows the same pattern.

#: The visible note (round 3's `_timeout_message` sibling): plain text in
#: the SessionStart-style context channel -- UserPromptSubmit stdout is
#: injected context the same way, per `mailbox_drain.py`'s own contract --
#: so both the model and the user see it.
_DISCONNECT_MESSAGE = (
    "nx-mcp is not connected to this session; conexus tool-tier hooks are "
    "being skipped. Restart Claude Code to reconnect."
)

#: Per-session detector state: whether this session has EVER seen a live
#: connect marker, and whether the CURRENT disconnect episode (if any) has
#: already been reported. Deliberately separate from the connect marker
#: itself (`mcp_connect_marker.<session_id>`) -- that file's identity is
#: "nx-mcp's own claim to be connected," owned and cleared by `nx-mcp`'s own
#: lifespan; this file is "what THIS detector has already told the user,"
#: owned and cleared only by this verb, and the two must never be confused
#: or unified, or a stale marker cleanup would silently reset the warn-once
#: state too.
_STATE_PREFIX = "mcp_connect_check_state."


def _state_path(session_id: str, config_dir: Path) -> Path:
    return config_dir / f"{_STATE_PREFIX}{session_id}"


@dataclass(frozen=True)
class _State:
    ever_connected: bool = False
    warned_since_last_connected: bool = False


def _read_state(path: Path) -> _State:
    """Missing, unreadable, or malformed all read as the zero state --
    fail-safe: the only consequence of losing this file is one possible
    extra "never connected" silence, never a spurious or duplicate warning.
    """
    try:
        raw = path.read_text()
    except OSError:
        return _State()
    try:
        data = json.loads(raw)
        return _State(
            ever_connected=bool(data.get("ever_connected", False)),
            warned_since_last_connected=bool(data.get("warned_since_last_connected", False)),
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        return _State()


def _write_state(path: Path, state: _State) -> None:
    """Best-effort, atomic temp-file + ``os.replace`` -- same pattern as
    :func:`nexus.mcp.connect_marker.publish_mcp_connect_marker`. A failed
    write costs only a possible repeat warning on the NEXT prompt, never a
    crash of this one.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
        tmp.write_text(json.dumps(asdict(state)))
        tmp.replace(path)
    except OSError:
        pass


def _decide(*, currently_connected: bool, state: _State) -> tuple[str | None, _State]:
    """Pure decision core, no I/O -- the whole warn-once-per-episode state
    machine, independently testable against synthetic states.

    Four cases, matching the bead's own proof requirement exactly:

    * Currently connected -> silent, and the episode flag resets (a LATER
      disconnect, after this reconnect, must warn again).
    * Never connected (``not state.ever_connected``) -> silent regardless
      of the current marker/pid state -- the startup barrier's own job.
    * Disconnected, ``ever_connected`` True, not yet warned this episode
      -> the one visible note, and the episode flag is set.
    * Disconnected, already warned this episode -> silent.
    """
    if currently_connected:
        return None, _State(ever_connected=True, warned_since_last_connected=False)
    if not state.ever_connected:
        return None, state
    if state.warned_since_last_connected:
        return None, state
    return _DISCONNECT_MESSAGE, _State(ever_connected=True, warned_since_last_connected=True)


def run(payload: dict | None) -> HookResult:
    """Warn once per disconnect episode when THIS session's `nx-mcp` has
    stopped answering, having previously connected.

    Reads ``session_id`` from *payload*, the same ``UserPromptSubmit``
    stdin contract :func:`nexus.hooks.mailbox_drain.run` already reads. A
    missing/non-string session id is a fast no-op -- nothing to key either
    the marker or the state file on.
    """
    session_id: str | None = None
    if payload is not None:
        sid = payload.get("session_id")
        session_id = sid if isinstance(sid, str) and sid else None
    if not session_id:
        return HookResult(stdout=None)

    configure_hook_logging()
    try:
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred for startup cost
        from nexus.daemon.service_registry import pid_alive  # noqa: PLC0415 — deferred for startup cost
        from nexus.mcp.connect_marker import read_mcp_connect_marker_info  # noqa: PLC0415 — deferred; see the module-scope comment above

        config_dir = nexus_config_dir()
        info = read_mcp_connect_marker_info(session_id, config_dir)
        connected = info is not None and pid_alive(info.pid)

        state_path = _state_path(session_id, config_dir)
        state = _read_state(state_path)
        message, new_state = _decide(currently_connected=connected, state=state)
        if new_state != state:
            _write_state(state_path, new_state)
    except Exception as exc:  # noqa: BLE001 — never the prompt's problem; see module docstring
        import structlog  # noqa: PLC0415 — deferred; only the failure path logs

        structlog.get_logger(__name__).warning(
            "mcp_connect_check_failed", session_id=session_id, error=str(exc),
        )
        return HookResult(stdout=None)

    return HookResult(stdout=message)
