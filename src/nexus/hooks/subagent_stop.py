# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The SubagentStop orchestration guard (RDR-215 bead nexus-q02nx.12).

Port of ``conexus/hooks/scripts/subagent-stop.sh`` (contract map row 15),
the RDR-184 Gap 1 guard: block a NAMED BACKGROUND teammate's idle exactly
once when it never sent its orchestrator a completion report.

ONE SubagentStop hook covers BOTH sync and background dispatches
(scenario 21b: background teammates fire SubagentStop in the SPAWNER's
session -- the docs caveat is refuted). Ground truth for "owes a report"
is the ledger, written by the orchestrator BEFORE dispatch, because no
hook payload can classify background-ness (scenario 27).

THE DECISION TABLE, carried across unchanged. Every uncertain path fails
OPEN -- never block a stop on missing evidence; the ledger is an enabling
allowlist, not a gate:

====================================== ====================================
guard mode off/unknown                 nothing                (explicit opt-out)
guard mode unset                       block                  (DEFAULT-ON since 2026-07-17)
``stop_hook_active`` true              resolution stamp, nothing
agent not listed / sync / unnamed      nothing                (sync unblockable by construction)
BLOCKED row already present            resolution stamp, nothing
transcript missing/junk, scan crash    nothing                (fail-open)
report found in transcript             REPORTED row, nothing
...and writes failed, mode block       UNLANDEDWRITE + BLOCKED + block
otherwise, mode observe                WOULDBLOCK row, nothing
otherwise, mode block                  BLOCKED row + block
====================================== ====================================

**Exit 0 on literally every path**, and stdout is empty on all but the two
block shapes.

**THE TIMING BUDGET IS WHY THIS BEAD EXISTS.** SubagentStop has a 10 s
timeout, and the bash file's own header documents a routine load-correlated
SIGKILL from the harness inside the current bash-only budget -- despite a
measured 97 MB transcript scanning in 0.14 s. The scanning was never the
problem; the process tree was. Each invocation spawned bash, sourced the
ledger library, then spawned ``python3`` twice more for the two scans, and
under load those spawns are what ran out of clock. On the tool tier there
is no process at all: the server imports this module once and calls
:func:`run`. That is the mitigation, and
``tests/hooks/test_subagent_stop_hook.py`` keeps the timing assertion
rather than trusting the tier change to have delivered it.

**The envelope is rendered, not printf'd.** The bash built its block with
``printf '{"decision": "block", "reason": "%s"}'``, interpolating
``agent_type`` raw, so a type carrying a double quote or a backslash would
have emitted malformed JSON. That was never reachable: the block is gated
behind :func:`expectations_owes_report`, whose charset guard admits only
``[A-Za-z0-9_:-]``, and a refused type returns "does not owe" long before
the envelope is built. So this is NOT a fixed bug and the port changes no
observable behaviour -- for every type that can reach the envelope, the
bytes are identical, which
``tests/hooks/test_subagent_stop_module.py`` asserts against the script's
own strings. What changes is that the rendering no longer DEPENDS on a
charset guard two calls away for its correctness.
"""
from __future__ import annotations

from nexus._hook_runtime._config import stop_guard_mode
from nexus._hook_runtime._io import HookResult, stop_decision
from nexus.hooks import expectations as _exp
from nexus.hooks.subagent_stop_scans import report_verdict, writes_verdict

__all__ = ["run"]

#: The nudge an agent reads when it owes a report and sent none. Carried
#: byte for byte: it is the whole user-visible surface of this guard.
_OWES_REASON = (
    "You are the named background teammate {agent_type} and your orchestrator "
    "expects a completion report you have not sent. Use SubagentHandback (or "
    "SendMessage) now to report: outcome, artifacts (paths/commits/IDs), and "
    "anything blocking. Then stop."
)

#: nexus-plycy: an exhaustion-forced block was never verified against the
#: credit ledger. Name that, so an over-blocked agent's operator can tell it
#: apart from a genuine credit-backed verdict. The ledger's BLOCKED row
#: carries the same cause in its 4th field for later audit.
_LOCK_EXHAUSTED_NOTE = (
    " (NOTE: this block could not verify remaining report credit under lock "
    "contention -- treat it as a precaution, not a confirmed miss; see "
    "nexus-plycy.)"
)

#: nexus-piqm5: reported-but-writes-failed is the EXACT 2026-08-25 shape --
#: "review complete" delivered while every persistence call 401'd, findings
#: surviving only because the agent volunteered it.
_UNLANDED_REASON = (
    "You sent your completion report, but {detail} of your storage writes came "
    "back as errors, so those findings are NOT persisted -- a caller reading "
    "T1/T2/T3 will not see them. Retry the failed writes now. If they still "
    "fail, say so explicitly in a SendMessage and restate the findings inline "
    "so they are not lost. Then stop."
)

#: The cause recorded on the one block that is not an owes verdict.
_UNLANDED_CAUSE = "unlanded-write"


def _field(data: dict, key: str) -> str:
    """One payload field, scrubbed of the two characters that would reshape
    a TSV row. The bash did this inside its single ``python3 -c`` parse."""
    return str(data.get(key) or "").replace("\t", " ").replace("\n", " ")


def _append(session_id: str, verb: str, agent_id: str, detail: str = "") -> None:
    """Append a ledger row, swallowing a refusal.

    A ledger row is never worth failing a hook over, and the bash got the
    same posture free by not setting ``set -e``.
    """
    try:
        _exp.expectations_append_row(session_id, verb, agent_id, detail)
    except Exception:  # noqa: BLE001 — fail open; see the module docstring
        pass


def _stamp_resolution_if_reported(
    session_id: str, agent_id: str, agent_type: str, transcript: str, strength: str
) -> None:
    """The post-block resolution stamp (nexus-hybv1).

    Before this, a BLOCKED row was terminal FOREVER: the once-guard exits
    recorded nothing, so an agent that heeded the block and delivered its
    report was ledger-indistinguishable from one that died silent.
    Forensics found ALL 7 recorded blocks resolved with a real SendMessage
    17-26 s after the nudge, while the census read them as failures.

    ``strength`` keeps the causal evidence honest: ``immediate`` means the
    block round-trip itself produced the report (strong -- the guard
    demonstrably worked); ``later`` means a subsequent stop found a report
    that may have arrived for unrelated reasons (weak). It rides the 4th
    TSV field, inert to every exact-field reader.

    Every failure path stamps nothing. Never blocks, never raises.
    """
    if not (session_id and agent_id and agent_type):
        return
    if not _exp.expectations_owes_report(session_id, agent_id, agent_type).owes:
        return
    if not _exp.expectations_already_blocked(session_id, agent_id):
        return
    # Consecutive-duplicate guard (review 21032 finding 3): the scan is
    # whole-transcript, so every re-stop of a resolved agent would re-find
    # the same SendMessage and append another REPORTED forever. Stamp only
    # when the LAST terminal row is not already REPORTED -- real
    # interleavings (BLOCKED -> REPORTED) still record; idle re-stops of a
    # resolved agent add nothing.
    if _exp.expectations_last_terminal(session_id, agent_id) == "REPORTED":
        return
    if report_verdict(transcript) == "FOUND":
        _append(session_id, "REPORTED", agent_id, strength)


def run(payload: dict | None) -> HookResult:
    """Decide whether this stopping subagent is blocked for not reporting."""
    mode = stop_guard_mode()
    if mode not in ("observe", "block"):
        return HookResult()

    data = payload or {}
    session_id = _field(data, "session_id")
    agent_id = _field(data, "agent_id")
    agent_type = _field(data, "agent_type")
    transcript = _field(data, "agent_transcript_path")
    stop_active = bool(data.get("stop_hook_active"))

    if stop_active:
        # The immediate re-stop after a block round-trip: the agent was told
        # to report and stop again. Record whether it did (nexus-hybv1).
        _stamp_resolution_if_reported(
            session_id, agent_id, agent_type, transcript, "immediate"
        )
        return HookResult()

    if not (session_id and agent_id and agent_type):
        return HookResult()

    # nexus-4bqre.1: archive BEFORE the sweep, never after. The sweep reaps
    # ledgers older than 7 days; archiving afterwards would only preserve
    # what survived the reap, which defeats the purpose. The ordering is by
    # construction, which is why it rides this site rather than taking its
    # own hook registration.
    _exp.expectations_archive()
    _exp.expectations_sweep()

    verdict = _exp.expectations_owes_report(session_id, agent_id, agent_type)
    if not verdict.owes:
        return HookResult()

    if _exp.expectations_already_blocked(session_id, agent_id):
        # A later stop of a previously-blocked agent (a multi-round
        # teammate's round 2+): the once-guard still never re-blocks, but a
        # report sent since the block is stamped so the ledger reflects the
        # delivery outcome.
        _stamp_resolution_if_reported(
            session_id, agent_id, agent_type, transcript, "later"
        )
        return HookResult()

    report = report_verdict(transcript)
    writes = writes_verdict(transcript)
    unlanded = writes.startswith("UNLANDED ")
    detail = writes[len("UNLANDED ") :] if unlanded else ""

    # nexus-piqm5 Layer 1: record the unlanded-write fact BEFORE any branch,
    # so it lands under every mode and both report outcomes. The harm is
    # that the failure is invisible unless the agent narrated it, and a row
    # that only appeared on the blocking path would keep it invisible in
    # observe mode and for agents that did report.
    if unlanded:
        _append(session_id, "UNLANDEDWRITE", agent_id, detail)

    if report == "FOUND":
        # Census raw material: EXPECT (dispatched) x REPORTED (scan says
        # reported) x WOULDBLOCK (scan says not). A missed block -- an agent
        # whose SendMessage was a status ping rather than the real report --
        # shows up as a REPORTED row the orchestrator can cross-check.
        _append(session_id, "REPORTED", agent_id)
        if unlanded and mode == "block":
            # Block ONCE so the agent retries or states the failure. The
            # once-guard bounds a genuine outage to a single nudge: a retry
            # cannot succeed while the store is down, and must not loop.
            if not _exp.expectations_already_blocked(session_id, agent_id):
                _exp.expectations_mark_blocked(session_id, agent_id, _UNLANDED_CAUSE)
                return HookResult(
                    stdout=stop_decision(
                        "block", reason=_UNLANDED_REASON.format(detail=detail)
                    )
                )
        return HookResult()

    if report != "NOTFOUND":
        # SKIP or SCANERROR: the transcript was missing, unreadable, or the
        # scan crashed. Fail open.
        return HookResult()

    # Owes a report, none sent.
    if mode == "observe":
        # Measurement row -- same TSV shape, foreign verb, so readers of
        # EXPECT/BLOCKED ignore it. Never consumes the real once-guard.
        _append(session_id, "WOULDBLOCK", agent_id)
        return HookResult()

    _exp.expectations_mark_blocked(session_id, agent_id, verdict.cause)
    reason = _OWES_REASON.format(agent_type=agent_type)
    if verdict.cause == "lock-exhausted":
        reason += _LOCK_EXHAUSTED_NOTE
    return HookResult(stdout=stop_decision("block", reason=reason))
