# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The Stop verification hook (RDR-215 bead nexus-q02nx.13).

Port of ``conexus/hooks/scripts/stop_verification_hook.sh`` (contract map
row 16). Advisory only: it warns about uncommitted changes, open beads, an
RDR-184 ledger that still lists background agents the harness no longer
tracks, and (nexus-dgl8g) a close-gate reconciliation backstop -- beads
that moved to ``closed`` this session with no ``review-completed`` marker
naming both reviewers in this session's T1 scratch. **It can never emit
deny or block** -- "warns only" is the script's own stated contract, hard
enforcement belongs to the PreToolUse close gate, and :func:`run` has
exactly one stdout shape, ``decision: approve``, with or without a reason.

**THE CATALOG-SYNC LEG IS DELETED, not moved to a thread** (bead
nexus-q02nx.13 planned the move; the bead .16 critique found the premise
false). The bash called ``nx catalog sync`` at session close with its
result discarded by ``|| true``. That command has raised
``click.ClickException`` unconditionally since conexus 7.0.0 --
``commands/catalog.py``'s ``sync_cmd``, "retired: the nexus service's
Postgres is the sole catalog authority" -- so it was already dead when
the bash was written, and the git-backed ``~/.config/nexus/catalog`` it
looked for was itself retired at RDR-158 P4.

Moving a call that cannot succeed onto a daemon thread would have bought
nothing and cost something: the port briefly logged a warning on every
fire where the bash discarded silently, which is new permanent noise for
a permanently-failing command. The whole "a killed thread costs a
deferred sync, never a corrupt one, because the work is idempotent"
analysis in the first draft of this file was reasoning carefully about a
scenario that cannot occur -- inherited unexamined from a bash comment
("auto-commit + push if remote configured") that was stale before it was
written.

DEVIATION, STATED: bead .13's own wording was "moving nx catalog sync off
the synchronous path". Deleting it is a different outcome and this is the
notice. The edge it changes: a box still running a pre-7.0.0 conexus
generation, WITH a git-backed catalog relic, would have had that relic
auto-committed at session close and now will not. Postgres has been the
catalog authority since RDR-158 P4, so nothing reads that relic; it is a
file that stopped being written, not data that stops being saved.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from nexus._hook_runtime._config import stop_guard_mode
from nexus._hook_runtime._io import HookResult, _emit
from nexus.hooks.verification_config import read_verification_config
from nexus.hooks import expectations as _exp

__all__ = ["run"]

#: How many leading lines of the Stop payload's transcript this reads
#: looking for the session's own start timestamp (nexus-dgl8g), and the
#: byte cap on top of it. Bounded on BOTH axes: a transcript with many
#: short lines (the line cap alone) or one pathological giant first line
#: (the byte cap alone) must not turn a Stop hook into a slow read of a
#: file that can grow for the whole session. Measured on a real session
#: transcript (nexus-dgl8g's own): the second line already carries a
#: ``timestamp`` field, so 200 lines / 64KiB is generous headroom, not a
#: tight fit.
_TRANSCRIPT_SCAN_LINES = 200
_TRANSCRIPT_SCAN_BYTES = 65536

#: The reconcile exit code that means "the ledger lists background agents
#: the harness no longer tracks". Every other code, and every failure path,
#: leaves the warning empty and never touches the decision.
_RECONCILE_STRANDED = 4

_UNCOMMITTED_WARNING = (
    "WARNING: Uncommitted changes detected — consider committing before "
    "ending session\n"
)
_BEADS_WARNING = (
    "WARNING: Beads still in progress — consider closing or deferring "
    "before ending session\n"
)

#: nexus-dgl8g: the close-gate reconciliation backstop's own warnings.
#: "Cannot check" is stated plainly rather than folded into silence --
#: the bead's own words are "never report a false clean" -- but ONLY once
#: a real session window is established (see :func:`_session_start_dt`);
#: a synthetic/legacy payload with no ``transcript_path`` at all yields no
#: window to check ANYTHING against and stays silent, matching every other
#: missing-signal branch in this file (e.g. no ``session_id``).
_BD_CANNOT_CHECK_WARNING = (
    "WARNING: close-gate reconciliation could not check for undeclared "
    "closes this session — bd is unavailable or did not answer\n"
)
_T1_CANNOT_CHECK_WARNING_TMPL = (
    "WARNING: close-gate reconciliation could not verify review-completed "
    "markers for bead(s) closed this session (T1 unreachable or the check "
    "timed out): {ids}\n"
)
_UNDECLARED_CLOSE_WARNING_TMPL = (
    "WARNING: {count} bead(s) closed this session with no review-completed "
    "marker naming both reviewers: {ids} (an evidence-only override close "
    "via NX_REVIEW_GATE_OVERRIDE=1 cannot be told apart from a genuinely "
    "undeclared close using bd's or T1's own records — verify by hand)\n"
)


def _approve(reason: str = "") -> HookResult:
    """The only envelope this hook can produce.

    Key order and spacing match the script's ``printf`` byte for byte, and
    a reason is rendered through ``json.dumps`` exactly as the script's own
    escaping helper did.
    """
    if reason:
        return HookResult(
            stdout=json.dumps({"decision": "approve", "reason": reason})
        )
    return HookResult(stdout=json.dumps({"decision": "approve"}))


def _read_config() -> dict:
    """The verification block, read in-process (bead nexus-b5ugt).

    **The spawn this used to do is gone, and its own docstring called
    the shot.** It said: "Bead .17 moves pre_close_verification_hook.sh,
    the other consumer, onto this tier; once both are here the reader can
    become an imported function and this spawn goes with it." Both are
    here. It went.

    What forced the timing rather than leaving it as tidying: the script
    was located off ``$CLAUDE_PLUGIN_ROOT``, and an ``mcp_tool`` hook
    runs inside ``nx-mcp``, which does not get a usable one.
    ``conexus/.mcp.json`` declares the server env as
    ``{"CLAUDE_PLUGIN_ROOT": "${CLAUDE_PLUGIN_ROOT}"}`` and Claude Code
    does not expand ``${...}`` in an MCP ``env`` block, so the literal
    placeholder arrived, the path never resolved, and this returned
    ``{}`` — on_stop false, the session-end gate silently verifying
    nothing. Measured 2026-09-20.

    The alternative fix, making the hook locate the script more reliably,
    is the wrong direction: RDR-215 exists to eliminate the
    plugin-resident layer so a native Windows client becomes viable, and
    a repair that keeps the subprocess keeps the thing being eliminated.

    ``runpy`` was rejected earlier for rebinding a process-global
    ``sys.stdout`` inside a concurrently-serving MCP server. That
    objection dies with the subprocess: an imported function returns a
    value and touches no global stream.

    Still returns ``{}`` on any failure, matching the script's
    ``|| echo '{}'`` posture — a config reader that raised would take
    down the hook that called it.
    """
    try:
        return read_verification_config()
    except Exception:  # noqa: BLE001 — a hook must never fail; see the module docstring
        return {}


def _reconcile_warning(payload: dict) -> str:
    """The RDR-184 stranded-agent warning, or "".

    WARN-ONLY unconditionally, and gated on the same guard as the rest of
    the ledger machinery so a session that opted the whole guard off does
    not pay for this either. It runs independent of the ``on_stop``
    verification toggle: it is a distinct RDR-184 concern, not part of that
    feature.
    """
    if stop_guard_mode() not in ("observe", "block"):
        return ""
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return ""
    try:
        report = _exp.expectations_reconcile(session_id, json.dumps(payload))
    except Exception:  # noqa: BLE001 — every reconcile failure leaves the warning empty
        return ""
    if report.code != _RECONCILE_STRANDED:
        return ""
    joined = " | ".join(report.lines)
    return (
        "WARNING: expectations ledger reconciliation found background "
        "agent(s) the ledger still lists as outstanding but the harness no "
        "longer tracks (nexus-2v0v7) -- possible silent death, verify: "
        f"{joined}\n"
    )


def _session_start_dt(transcript_path: str):
    """The Stop payload's own session, anchored on its transcript's first
    timestamped line -- or ``None`` if it cannot be determined.

    nexus-dgl8g: this hook needs an honest "did THIS bead close during
    THIS session" window, and the RDR-184 ledger cannot supply one --
    its per-session file exists only once an Agent dispatch has written
    an EXPECT/START row, so a session that closes beads without ever
    dispatching a subagent has no ledger at all. The Stop/SubagentStop
    payload's own ``transcript_path`` (confirmed present on every hook
    event this repo has a fixture for -- see
    ``nexus.hooks.expectations._payload_transcript_path``'s docstring)
    names a JSONL transcript the harness itself writes, and Claude Code
    timestamps its own rows there; the first row carrying one is the
    session's actual start, independent of the ledger and of the
    machine-wide, last-writer-wins ``current_session`` file
    :func:`nexus.hooks.phase_review_close_gate._session_start_time` reads
    (unsuitable here for the reason its own AGENTS.md entry names: this
    project runs several sessions on one box at once).

    Bounded on both lines and bytes (:data:`_TRANSCRIPT_SCAN_LINES`,
    :data:`_TRANSCRIPT_SCAN_BYTES`) so a giant or slow-to-read transcript
    cannot turn a Stop hook into a long read; not finding a timestamped
    row inside that budget is treated the same as not finding the file at
    all -- ``None``, not an error.
    """
    if not transcript_path:
        return None
    read = 0
    try:
        with open(transcript_path, encoding="utf-8") as fh:  # noqa: PTH123 — carried: a plain path from the payload, no Path indirection needed for one bounded read
            for _ in range(_TRANSCRIPT_SCAN_LINES):
                line = fh.readline()
                if not line:
                    break
                read += len(line)
                if read > _TRANSCRIPT_SCAN_BYTES:
                    break
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(row, dict):
                    continue
                ts = row.get("timestamp")
                if isinstance(ts, str) and ts:
                    return _exp._parse_iso(ts)
    except OSError:
        return None
    return None


def _bd_closed_since(session_start) -> list[str] | None:
    """Bead ids bd reports closed at or after *session_start*.

    ``None`` means bd could not be consulted at all -- absent, a
    non-zero exit, or output that does not parse as the JSON array
    ``bd list --json`` promises -- which is NOT the same as an empty
    list (bd answered; nothing closed in the window). The caller keeps
    those two outcomes on separate branches so "bd could not answer"
    reports as a named cannot-check line rather than a silent, false
    "nothing to report" (the bead's own "never report a false clean").

    TWO THINGS MEASURED AGAINST THE REAL ``bd`` BINARY, not assumed
    (nexus-dgl8g). First: a bare ``bd list --status closed --json``
    defaults to ``--limit 50`` and, on this repo's own history, silently
    hides everything past that -- a genuinely undeclared close sitting
    past position 50 would never be seen, the opposite of the bead's own
    "never report a false clean". ``--limit 0`` is unlimited, and
    ``--closed-after`` pushes the date filter into bd's own query instead
    of pulling its whole closed-issue history over stdout to filter
    client-side -- measured 4.69s wall for a bare unfiltered call on this
    repo vs. 1.65-1.88s scoped, and the scoped figure held steady whether
    the window actually matched anything or not, so that remaining ~1.7s
    is bd's own per-invocation floor (its Dolt engine boot), not
    something a narrower query filters away further. Second:
    ``--closed-after`` is EXCLUSIVE of its own boundary value (a bead
    closed in the exact same second as *session_start* is NOT returned)
    -- so the argument passed is one second earlier than *session_start*,
    and the real boundary is still enforced by the ``>=`` comparison
    below against each row's own ``closed_at``, never by trusting bd's
    filter alone.
    """
    from datetime import timedelta  # noqa: PLC0415 — deferred: see the other imports in this module
    from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 — deferred: see the other spawns in this module

    if shutil.which("bd") is None:
        return None
    closed_after = (session_start - timedelta(seconds=1)).isoformat()
    try:
        proc = run_bounded(
            [
                "bd", "list", "--status", "closed",
                "--closed-after", closed_after,
                "--limit", "0", "--json",
            ],
            timeout=30,
        )
    except Exception:  # noqa: BLE001 — an unavailable/misbehaving bd is a cannot-check, not a crash
        return None
    if proc.returncode != 0:
        return None
    try:
        rows = json.loads(proc.stdout)
    except (ValueError, TypeError):
        return None
    if not isinstance(rows, list):
        return None

    ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        bead_id = row.get("id")
        closed_at = row.get("closed_at")
        if not isinstance(bead_id, str) or not bead_id:
            continue
        if not isinstance(closed_at, str):
            continue
        closed_dt = _exp._parse_iso(closed_at)
        if closed_dt is None:
            continue
        if closed_dt >= session_start:
            ids.append(bead_id)
    return ids


def _undeclared_close_warning(payload: dict) -> str:
    """nexus-dgl8g: the close-gate reconciliation backstop.

    Lists beads that moved to ``closed`` during THIS session with no
    ``review-completed`` marker in THIS session's T1 scratch naming both
    standing reviewers -- the same detective-not-preventive posture as
    :func:`_reconcile_warning`, for the class of close the PreToolUse
    gate structurally cannot see (bead nexus-dgl8g's own motivation:
    ``bd batch -f``, ``bd import <file>``, a dynamically-built ``bd sql``
    -- content off the command line the gate tokenizes). Gated on the
    same :func:`stop_guard_mode` as :func:`_reconcile_warning`, and for
    the same reason: both are RDR-184-orchestration-family checks, opted
    out of together, independent of the ``on_stop`` toggle that governs
    the git/beads UX nags below.

    Reuses :func:`nexus.hooks.pre_close_verification._coverage` for the
    marker read rather than re-implementing it -- same T1 scan, same
    reviewer-name matching, same ``review-completed`` tag rule. Marker
    reading and close-detection are two independently-maintained readers
    of two different sources (T1 scratch vs. bd's own record) and a
    second implementation of either would be exactly the drift this
    bead's own review markers exist to prevent.

    An override close (``NX_REVIEW_GATE_OVERRIDE=1``) is legitimate and
    LOOKS IDENTICAL here to a genuinely undeclared one: nothing bd records
    and nothing T1 records distinguishes the two (the override is read
    only from the PreToolUse hook's own process environment at close
    time, never persisted anywhere this reader can reach -- see
    ``pre_close_verification.py``'s own ``NX_REVIEW_GATE_OVERRIDE``
    references). Per the bead: state that limitation in the line rather
    than silently dropping such closes from the count, which would make
    an override indistinguishable from "checked and clean".

    A SECOND, WIDER LIMITATION worth naming here rather than discovering
    at review: bd's own record (``bd list --json``, confirmed above) has
    no per-close actor/session field, so the window is TIME-scoped only
    (``closed_at >= this session's start``), never SESSION-scoped. Per
    ``AGENTS.md``'s own "one session, one worktree" model this project
    runs several sessions against the SAME shared bd database at once, so
    a bead a SIBLING session closed (with its own valid marker, in its
    own T1 scope) inside this session's time window reads as undeclared
    here too -- this reader has no way to tell "closed by someone else,
    correctly" from "closed by me, without a marker". Not fixable from
    data bd exposes today; a false positive of this shape is a reason to
    check bd's own ``close_reason``/timing by hand, not evidence the
    close itself was actually undeclared.
    """
    if stop_guard_mode() not in ("observe", "block"):
        return ""
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return ""

    session_start = _session_start_dt(str(payload.get("transcript_path") or ""))
    if session_start is None:
        # No transcript to anchor a window on: there is nothing to check
        # AGAINST, not evidence of anything amiss. Matches this file's
        # other missing-signal branches (e.g. empty session_id above).
        return ""

    closed_ids = _bd_closed_since(session_start)
    if closed_ids is None:
        return _BD_CANNOT_CHECK_WARNING
    if not closed_ids:
        return ""

    from nexus.hooks import pre_close_verification as _pcv  # noqa: PLC0415 — deferred: see the other spawns in this module

    coverage = _pcv._coverage(closed_ids, session_id=session_id)
    if not coverage.get("t1_reachable"):
        return _T1_CANNOT_CHECK_WARNING_TMPL.format(ids=" ".join(sorted(closed_ids)))

    status = coverage.get("status", {})
    # "deadline" means the coverage phase's own wall-clock budget ran out
    # before it could look -- NOT CONFIRMED missing, just unchecked (see
    # pre_close_verification._deny_message's identical treatment of the
    # same status). Reported as cannot-check, not folded into undeclared.
    unchecked = sorted(b for b in closed_ids if status.get(b) == "deadline")
    undeclared = sorted(
        b for b in closed_ids if status.get(b) not in ("covered", "deadline")
    )

    warning = ""
    if unchecked:
        warning += _T1_CANNOT_CHECK_WARNING_TMPL.format(ids=" ".join(unchecked))
    if undeclared:
        warning += _UNDECLARED_CLOSE_WARNING_TMPL.format(
            count=len(undeclared), ids=" ".join(undeclared)
        )
    return warning


def _git_is_dirty(path: str | None = None) -> bool:
    from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 — deferred by this directory's convention only: the module runs inside nx-mcp as an mcp_tool hook, which has already imported structlog, so the deferral saves nothing here (nexus-rcoze review)

    if shutil.which("git") is None:
        return False
    args = ["git"] + (["-C", path] if path else []) + ["status", "--porcelain"]
    try:
        proc = run_bounded(args, timeout=30)
    except Exception:  # noqa: BLE001 — an unavailable git is not a warning
        return False
    return bool(proc.stdout.strip())


def _beads_in_progress() -> bool:
    from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 — deferred by this directory's convention only: the module runs inside nx-mcp as an mcp_tool hook, which has already imported structlog, so the deferral saves nothing here (nexus-rcoze review)

    if shutil.which("bd") is None:
        return False
    try:
        proc = run_bounded(
            ["bd", "list", "--status=in_progress"],
            timeout=30,
        )
    except Exception:  # noqa: BLE001 — an unavailable bd is not a warning
        return False
    return "in_progress" in proc.stdout


def run(payload: dict | None) -> HookResult:
    """Approve the stop, with any advisory warnings attached."""
    data = payload if isinstance(payload, dict) else {}
    reconcile = _reconcile_warning(data) + _undeclared_close_warning(data)

    config = _read_config()
    if config.get("on_stop") is not True:
        return _approve(reconcile)

    warnings = reconcile
    if _git_is_dirty():
        warnings += _UNCOMMITTED_WARNING

    if _beads_in_progress():
        warnings += _BEADS_WARNING

    return _approve(warnings)
