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
#: nexus-dgl8g follow-up: the override-caveat wording this template used to
#: carry ("cannot be told apart from a genuinely undeclared close") is
#: GONE, not softened -- an override close is now identified directly from
#: this session's own transcript (see :func:`_session_close_declarations`)
#: and reported under :data:`_OVERRIDE_CLOSE_NOTE_TMPL` instead, so a bead
#: only ever reaches this template once it is known NOT to be one.
_UNDECLARED_CLOSE_WARNING_TMPL = (
    "WARNING: {count} bead(s) closed this session with no review-completed "
    "marker naming both reviewers: {ids}\n"
)
_OVERRIDE_CLOSE_NOTE_TMPL = (
    "NOTE: {count} bead(s) closed this session under an explicit review-gate "
    "override (NX_REVIEW_GATE_OVERRIDE=1, found on the closing command "
    "itself in this session's transcript): {ids}\n"
)
#: The transcript could not be scanned for THIS session's own close
#: commands (missing, unreadable, or every line failed to parse) -- the
#: bd time-window list is reported UNSCOPED, exactly as it would have been
#: before this session-scoping existed, which means it can include a
#: SIBLING session's legitimate close. Said plainly rather than silently
#: falling back, per the bead's "never report a false clean" -- here
#: widened to "never report a false undeclared" either.
_SCOPE_FALLBACK_WARNING = (
    "WARNING: could not scope the close-gate reconciliation to this "
    "session (its own transcript could not be read) — the bead(s) below "
    "may include another session's legitimate close:\n"
)

#: nexus-dgl8g follow-up 2: Stop fires on EVERY assistant turn, not once per
#: session, so ``NX_CLOSE_GATE_DEADLINE_SECONDS``'s 3.5s default -- sized
#: for PreToolUse's 5s hard ceiling, a budget this hook does not share --
#: is the wrong number to inherit. Passed explicitly to ``_coverage``
#: rather than retuned at the env-var level, which every OTHER caller of
#: that function (the PreToolUse gate itself) would also pick up.
_STOP_COVERAGE_DEADLINE_SECONDS = 5.0

#: nexus-dgl8g follow-up 2: per-session memoization state lives beside the
#: RDR-184 ledger's own per-session files (``expectations.py``'s
#: ``_state_dir()``: ``XDG_STATE_HOME/nexus/<subdir>``), in a sibling
#: subdirectory rather than that same one -- this state has nothing to do
#: with the EXPECT/START ledger and mixing the two would make a reap of
#: one accidentally a reap of the other.
_CLOSE_GATE_STATE_SUBDIR = "close-gate-backstop"


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


def _close_gate_state_dir() -> Path:
    """Sibling of ``expectations._state_dir()``, own subdirectory.

    Same private-by-construction posture (``chmod`` reapplied on every
    call: the dir may predate a version that created it 0700, and this
    file names live bead ids).

    KNOWN RESIDUAL, not fixed here: unlike the RDR-184 ledger
    (``expectations_sweep()``), nothing reaps a session's file after the
    session ends -- one small JSON file per session, forever. Scoped out
    of this dispatch; a reap would mirror ``expectations_sweep()``'s own
    mtime-floor sweep over this sibling directory.
    """
    root = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    directory = Path(root) / "nexus" / _CLOSE_GATE_STATE_SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:  # pragma: no cover — a dir we cannot chmod is still usable
        pass
    return directory


def _close_gate_state_path(session_id: str) -> Path | None:
    """The per-session memoization file, or ``None`` for a path-unsafe id.

    Reuses ``expectations._SESSION_ID_RE`` -- the same charset the RDR-184
    ledger's own per-session filename already trusts -- rather than a
    second regex that could drift from it.
    """
    if not session_id or not _exp._SESSION_ID_RE.match(session_id):
        return None
    return _close_gate_state_dir() / f"{session_id}.json"


#: The state a fresh session (or a path-unsafe/corrupt one) starts from.
#: A fresh dict every call -- callers mutate their own copy.
def _empty_close_gate_state() -> dict:
    return {"offset": 0, "pending": {}, "resolved": {}}


def _read_close_gate_state(session_id: str) -> dict:
    """This session's memoized offset/pending/resolved state, or empty.

    Fail-open on every axis (missing file, corrupt JSON, wrong shape):
    the WORST this can do wrong is re-scan-from-zero and re-verify
    everything once, which is exactly what would have happened before
    this memoization existed -- never worse than the un-memoized
    baseline, never a reason to fail the hook.
    """
    path = _close_gate_state_path(session_id)
    if path is None:
        return _empty_close_gate_state()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return _empty_close_gate_state()
    if not isinstance(data, dict):
        return _empty_close_gate_state()
    offset = data.get("offset")
    pending = data.get("pending")
    resolved = data.get("resolved")
    return {
        "offset": offset if isinstance(offset, int) and offset >= 0 else 0,
        "pending": dict(pending) if isinstance(pending, dict) else {},
        "resolved": dict(resolved) if isinstance(resolved, dict) else {},
    }


def _write_close_gate_state(session_id: str, state: dict) -> None:
    """Best-effort atomic write (temp file + ``os.replace``), matching
    ``db.t1.publish_t1_session_lease``'s own pattern so a concurrent
    reader (there should not be one -- Stop hooks for one session do not
    overlap -- but the file lives beside others that assume this) never
    observes a torn write. A failure here loses only the memoization for
    this turn, never the hook itself.
    """
    path = _close_gate_state_path(session_id)
    if path is None:
        return
    try:
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(path)
    except OSError:
        pass


def _scan_transcript_tail(
    transcript_path: str, start_offset: int
) -> tuple[dict[str, bool], int] | None:
    """New close declarations found strictly AFTER *start_offset*.

    nexus-dgl8g follow-up 2: Stop fires on every assistant turn, so
    re-reading the WHOLE transcript every time (this function's own
    previous shape, ``_session_close_declarations``) means a turn deep
    into a long session pays for the growing prefix again on every single
    turn. *start_offset* is a raw BYTE offset from a PRIOR call's own
    ``tell()`` (persisted across process invocations in
    :func:`_read_close_gate_state`/:func:`_write_close_gate_state`);
    opened in BINARY mode specifically so that offset is unambiguous --
    text-mode ``seek``/``tell`` cookies are only valid against the SAME
    open stream that produced them, where a raw byte offset from a
    PRIOR process's read is exactly what persisting across turns needs.

    Returns ``(new_declarations, new_offset)`` where *new_declarations*
    maps a NEWLY-seen bead id to whether ITS OWN closing command carried
    an inline ``NX_REVIEW_GATE_OVERRIDE=1`` (OR'd if the same id appears
    more than once in the tail). Reuses
    ``pre_close_verification._bd_verbs``/``_bead_ids`` rather than
    re-implementing the close spellings, exactly as the single-pass
    version did.

    Returns ``None`` if the transcript cannot be read at all (missing,
    unreadable, or a decode failure never even producible from a valid
    JSONL file). Distinct from "readable, nothing new" (``({}, offset)``
    with ``new_declarations`` empty) -- the caller falls back to an
    UNSCOPED report only on the former, never treats the latter as
    anything but "nothing new to check".
    """
    from nexus.hooks.pre_close_verification import _bd_verbs, _bead_ids  # noqa: PLC0415 — deferred: see the other spawns in this module

    declarations: dict[str, bool] = {}
    try:
        with open(transcript_path, "rb") as fh:  # noqa: PTH123 — carried: binary, see the byte-offset note above
            fh.seek(start_offset)
            for raw in fh:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line or '"Bash"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "assistant":
                    continue
                message = entry.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not (
                        isinstance(block, dict)
                        and block.get("type") == "tool_use"
                        and block.get("name") == "Bash"
                    ):
                        continue
                    tool_input = block.get("input")
                    command = tool_input.get("command") if isinstance(tool_input, dict) else None
                    if not isinstance(command, str) or not command:
                        continue
                    verbs = _bd_verbs(command)
                    if not verbs.get("has_close_or_done"):
                        continue
                    is_override = bool(verbs.get("inline_override"))
                    for bid in _bead_ids(command):
                        declarations[bid] = declarations.get(bid, False) or is_override
            new_offset = fh.tell()
    except OSError:
        return None
    return declarations, new_offset


def _render_resolved_warning(resolved: dict[str, str]) -> str:
    """The persisted verdicts, rendered -- every turn, from CACHE, no I/O.

    A bead already resolved keeps being SHOWN each turn it stays that way
    (matching this file's other advisory nags, e.g. beads-in-progress,
    which re-warn every Stop while the condition persists) but is never
    RE-VERIFIED (nothing here spawns anything) -- "reported once" bounds
    the WORK, not the visibility.
    """
    override_ids = sorted(b for b, st in resolved.items() if st == "override")
    unchecked_ids = sorted(
        b for b, st in resolved.items() if st in ("unchecked", "t1-unreachable")
    )
    undeclared_ids = sorted(b for b, st in resolved.items() if st == "undeclared")

    warning = ""
    if override_ids:
        warning += _OVERRIDE_CLOSE_NOTE_TMPL.format(
            count=len(override_ids), ids=" ".join(override_ids)
        )
    if unchecked_ids:
        warning += _T1_CANNOT_CHECK_WARNING_TMPL.format(ids=" ".join(unchecked_ids))
    if undeclared_ids:
        warning += _UNDECLARED_CLOSE_WARNING_TMPL.format(
            count=len(undeclared_ids), ids=" ".join(undeclared_ids)
        )
    return warning


def _resolve_pending(
    session_id: str, transcript_path: str, pending: dict[str, bool], resolved: dict[str, str]
) -> str:
    """Confirm *pending* ids against bd + T1, mutating *resolved* in place
    and returning them (removed from *pending*, also mutated in place) --
    or a TRANSIENT cannot-check note if bd could not answer this turn,
    in which case *pending* is left untouched for the next turn to retry
    (bd being briefly unreachable is presumed transient, matching
    ``_beads_in_progress``'s own un-cached retry-every-turn posture
    elsewhere in this file).

    Every id THIS call DOES manage to ask bd about gets a TERMINAL
    ``resolved`` entry this turn, whatever the answer -- clean (not
    actually in bd's closed-in-window list; the close command may have
    failed, or bd has not caught up), override, covered/missing/
    incomplete via T1, or T1-unreachable/deadline. "Verified once, never
    re-checked" is deliberately taken to mean an INCONCLUSIVE T1 read
    counts as verified too: the alternative (retry indefinitely) can cost
    a full bd + nx round trip on every future turn for the life of the
    session if T1 stays flaky, which is the exact cost this follow-up
    exists to remove.
    """
    session_start = _session_start_dt(transcript_path)
    if session_start is None:
        return ""  # can't ask bd without a window; pending stays for next turn

    closed_ids = _bd_closed_since(session_start)
    if closed_ids is None:
        return _BD_CANNOT_CHECK_WARNING  # transient; pending left untouched

    closed_set = set(closed_ids)
    confirmed = [b for b in pending if b in closed_set]
    not_confirmed = [b for b in pending if b not in closed_set]

    override_now = [b for b in confirmed if pending[b]]
    non_override_now = [b for b in confirmed if not pending[b]]

    for bid in override_now:
        resolved[bid] = "override"
    for bid in not_confirmed:
        resolved[bid] = "clean"

    if non_override_now:
        from nexus.hooks import pre_close_verification as _pcv  # noqa: PLC0415 — deferred: see the other spawns in this module

        coverage = _pcv._coverage(
            non_override_now, session_id=session_id,
            deadline_seconds=_STOP_COVERAGE_DEADLINE_SECONDS,
        )
        if not coverage.get("t1_reachable"):
            for bid in non_override_now:
                resolved[bid] = "t1-unreachable"
        else:
            status = coverage.get("status", {})
            for bid in non_override_now:
                st = status.get(bid)
                if st == "covered":
                    resolved[bid] = "clean"
                elif st == "deadline":
                    resolved[bid] = "unchecked"
                else:
                    resolved[bid] = "undeclared"

    for bid in confirmed + not_confirmed:
        pending.pop(bid, None)
    return ""


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

    TWO FILTERS, TRANSCRIPT FIRST (nexus-dgl8g follow-up 2 -- this order
    is the opposite of the follow-up 1 shape, and deliberately so). Stop
    fires on EVERY assistant turn, not once per session, so the cost of
    this function IS the whole design, not an afterthought. The
    transcript scan is now a cheap, INCREMENTAL, memoized local read (a
    ``stat`` plus, only on growth, the bytes appended since the last
    call -- see :func:`_scan_transcript_tail` and the persisted state in
    :func:`_read_close_gate_state`); bd and T1 are process spawns
    (measured ~1.5-1.9s each, dominated by their own boot cost, not by
    what they are asked). So the transcript decides FIRST whether there
    is anything new to even ask bd/T1 about, and a turn with no new
    close-shaped Bash command touches neither: zero subprocesses. THIS
    session's own transcript is also what tells a sibling session's
    legitimate close (same bd, same time window, different session --
    bd's own record carries no per-close actor/session field, and this
    project runs several sessions against one shared bd database at
    once) apart from this session's own undeclared one, exactly as
    follow-up 1 established; that intersection still happens, just
    id-by-id inside :func:`_resolve_pending` rather than as one big list
    comparison, because the SET of pending ids memoizes down to "only
    ever the newly-declared ones" instead of recomputing the whole
    session's history every turn.

    Reuses :func:`nexus.hooks.pre_close_verification._coverage` for the
    marker read rather than re-implementing it -- same T1 scan, same
    reviewer-name matching, same ``review-completed`` tag rule -- with an
    explicit :data:`_STOP_COVERAGE_DEADLINE_SECONDS` rather than that
    function's own env-var default (3.5s, sized for PreToolUse's 5s
    ceiling, a budget this hook does not share).

    An override close (``NX_REVIEW_GATE_OVERRIDE=1``) is identified
    directly from this session's own transcript and reported under
    :data:`_OVERRIDE_CLOSE_NOTE_TMPL`, never reaching the T1 coverage
    check at all (override alone is what the PreToolUse gate's own
    ``_run_gate`` treats as sufficient, regardless of marker state). Only
    an INLINE override (``NX_REVIEW_GATE_OVERRIDE=1 bd close ...``) is
    visible this way; a persistent ``export`` set in an earlier Bash call
    is not -- narrower than the gate's own detection, not a claimed
    equivalence.

    A transcript that cannot be read AT ALL when there is genuinely
    nothing cached yet (see the fallback branch below) is reported
    UNSCOPED with :data:`_SCOPE_FALLBACK_WARNING` rather than silently
    saying nothing -- a missing transcript is not evidence every close in
    the window was legitimate either.
    """
    if stop_guard_mode() not in ("observe", "block"):
        return ""
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return ""
    transcript_path = str(payload.get("transcript_path") or "")
    if not transcript_path:
        return ""

    state = _read_close_gate_state(session_id)
    offset = state["offset"]
    pending = state["pending"]
    resolved = state["resolved"]

    try:
        size = os.path.getsize(transcript_path)
    except OSError:
        size = None

    if size is None:
        # Cannot even stat the transcript: no way to scope to this
        # session at all. Fall back to the OLD unscoped bd time-window
        # report, transient (never persisted into `resolved`) so normal
        # memoized operation resumes the moment the transcript is
        # readable again, rather than staying wedged on this branch.
        session_start = _session_start_dt(transcript_path)
        if session_start is None:
            return _render_resolved_warning(resolved)
        closed_ids = _bd_closed_since(session_start)
        if closed_ids is None:
            return _BD_CANNOT_CHECK_WARNING + _render_resolved_warning(resolved)
        unscoped = [b for b in closed_ids if b not in resolved]
        if not unscoped:
            return _render_resolved_warning(resolved)
        from nexus.hooks import pre_close_verification as _pcv  # noqa: PLC0415 — deferred: see the other spawns in this module

        coverage = _pcv._coverage(
            unscoped, session_id=session_id, deadline_seconds=_STOP_COVERAGE_DEADLINE_SECONDS
        )
        fallback = _SCOPE_FALLBACK_WARNING
        if not coverage.get("t1_reachable"):
            fallback += _T1_CANNOT_CHECK_WARNING_TMPL.format(ids=" ".join(sorted(unscoped)))
        else:
            status = coverage.get("status", {})
            unchecked = sorted(b for b in unscoped if status.get(b) == "deadline")
            undeclared = sorted(b for b in unscoped if status.get(b) not in ("covered", "deadline"))
            if unchecked:
                fallback += _T1_CANNOT_CHECK_WARNING_TMPL.format(ids=" ".join(unchecked))
            if undeclared:
                fallback += _UNDECLARED_CLOSE_WARNING_TMPL.format(
                    count=len(undeclared), ids=" ".join(undeclared)
                )
        return fallback + _render_resolved_warning(resolved)

    transient_note = ""
    if size > offset:
        tail = _scan_transcript_tail(transcript_path, offset)
        if tail is not None:
            new_declarations, new_offset = tail
            for bid, is_override in new_declarations.items():
                if bid in resolved:
                    continue  # already terminal; a re-close of a closed bead is not this backstop's concern
                pending[bid] = pending.get(bid, False) or is_override
            offset = new_offset
        # tail is None (became unreadable mid-scan): leave offset/pending
        # untouched, nothing more to do -- caught next turn once readable.

    if pending:
        transient_note = _resolve_pending(session_id, transcript_path, pending, resolved)

    _write_close_gate_state(
        session_id, {"offset": offset, "pending": pending, "resolved": resolved}
    )
    return transient_note + _render_resolved_warning(resolved)


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
