# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The RDR-184 orchestration ledger (RDR-215 bead nexus-q02nx.9).

The Python port of ``conexus/hooks/scripts/expectations.sh``, the
highest-blast-radius file in this epic: 1,827 lines whose own comments
document three successive rounds of concurrency fixes, and four public
exit-code contracts quoted verbatim in ``AGENTS.md`` as the caller-facing
API.

**This is a MOVE, not a rewrite** (RDR-215 Approach item 9). Everything a
caller can observe is reproduced rather than improved: the append-only TSV
format, the verb vocabulary (``EXPECT``/``START``/``BLOCKED``/``CONSUMED``/
``REPORTED``/``WOULDBLOCK``), every exit code, every stdout line shape, and
the ``.credit.<type>.<n>`` / ``.expect.lock`` sidecar names. The bash
library keeps running until bead ``nexus-q02nx.14`` repoints its consumers,
so the two must agree for the whole of Phase 2.

**The one thing that must not be "cleaned up".** The unit of credit is an
``os.symlink``, and correctness does not depend on any lock. The bash
file's own history is the argument: a name-based lock is STEALABLE, because
its stale reap is three separate steps (test, find, remove) and the lock can
change hands between them, so one racer can delete another's live lock and
both end up inside the critical section. POSIX offers no atomic
compare-and-delete on a path, so every variant of that shape has the same
window -- unfixable in kind rather than tunable. ``symlink(2)`` is atomic
and fails with ``EEXIST``, so for each of the ``credit`` slot names exactly
one racer on the host can ever win, whatever the lock is doing. Rounds 1 and
2 each reasoned about the critical section while the defect lived before it.

So: no ``threading.Lock``, no read-modify-write under a file lock, no
context manager that reclaims a slot on exit, and nothing that resolves the
link target as a path. Each of those reintroduces exactly what round 3
removed, and a test that only re-asserts the ceiling under load would keep
passing while it did.

**Failure direction**, fixed in advance and inherited: every consult helper
fails OPEN. A missing, unreadable or junk-bearing ledger must never block a
stop. The file is an enabling allowlist, not a gate on everything. Writers,
by contrast, raise :class:`ExpectationsUsageError` on invalid input -- the
bash exit code 2 -- because a malformed row silently reshapes the TSV for
every later reader.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import time

from nexus._hook_runtime._io import _emit
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "ExpectationsUsageError",
    "WORKFLOW_SUBAGENT_TYPE",
    "expectations_already_blocked",
    "expectations_append_row",
    "expectations_archive",
    "expectations_census",
    "expectations_expect",
    "expectations_file",
    "expectations_last_terminal",
    "expectations_mark_blocked",
    "expectations_owes_report",
    "expectations_reconcile",
    "expectations_start",
    "expectations_sweep",
    "expectations_undeclared",
]

#: Reap floor for both ledgers and their derived credit slots, in days.
#: HONEST RESIDUAL, carried from bash: reaping is by mtime, which only
#: refreshes on WRITE, so a session idle past the floor with a background
#: dispatch still pending can lose its ledger.
_REAP_DAYS = 7

#: TEST-ONLY FAULT-INJECTION SEAM, ``None`` in production.
#:
#: Called immediately before each slot-create attempt in
#: :func:`_claim_credit`. A race test sets it to a callback that runs a
#: SECOND claimant to completion, which CONSTRUCTS the losing interleaving
#: instead of hoping to observe it.
#:
#: This exists because the two obvious instruments both fail. A parallel
#: load test only SAMPLES interleavings: measured here, 16 concurrent
#: processes did not hit the window, so replacing the atomic create with a
#: check-then-act left its winners assertion green. An AST assertion never
#: EXECUTES the claim: it pins the shape of the code, so it refuses a
#: legitimate refactor and passes a rewrite that keeps the shape and loses
#: the property.
#:
#: One line that is ``None`` in production buys a race test that is
#: deterministic rather than flaky, and this project has already paid for
#: the alternative three times over (see the round 1/2/3 history above).
_CLAIM_INTERLEAVE: "Callable[[], None] | None" = None


def _test_delay(name: str) -> None:
    """A TEST-ONLY contention seam, driven by an environment variable.

    Three of these exist (``NX_EXPECT_LOCK_HOLD_DELAY_S``,
    ``NX_EXPECT_CLAIM_DELAY_S``, ``NX_EXPECT_APPEND_DELAY_S``) and they are
    NOT optional colour: the concurrency falsifiers in
    ``tests/hooks/test_subagent_stop_hook.py`` use them to WIDEN a specific
    window deterministically, so the exhaustion and orphan races reproduce
    on demand instead of on a box that happens to be loaded. Bead
    nexus-q02nx.9 ported the lock without them, and the result was not a
    red -- it was a concurrency test passing VACUOUSLY, because with no
    forced hold no racer ever exhausted its budget and "no over-block" was
    trivially true. Caught only by that test's own non-vacuity assert.

    The in-process :data:`_CLAIM_INTERLEAVE` seam does not replace these:
    it fires inside one interpreter, and these have to widen a window
    across separate PROCESSES.

    A missing or non-numeric value is a no-op, matching the bash regex
    guard. Nothing outside a test harness sets any of them.
    """
    raw = os.environ.get(name, "")
    try:
        delay = float(raw)
    except ValueError:
        return
    if delay >= 0:
        time.sleep(delay)

#: Verb vocabulary of the append-only TSV. Reproduced exactly; a reader in
#: the bash library, the e2e twin, or a test fixture may carry any of them.
VERBS = ("EXPECT", "START", "BLOCKED", "CONSUMED", "REPORTED", "WOULDBLOCK")

#: ``session_id`` is interpolated into a filesystem path, so it gets the
#: same defensive charset the bash port applies: a traversal-bearing id
#: (``../../x``) must never escape the private 0700 dir. Framework session
#: ids are UUID-shaped; the charset is deliberately wider but path-safe.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

#: ``:`` is admitted (nexus-qc4p1) because ``subagent_type`` is a legal name
#: and plugin-qualified types carry it (``conexus:code-review-expert``). It
#: is inert in every reader: fields are compared exactly, never globbed.
#: Tab and newline stay excluded, which is what keeps the TSV intact.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_:-]{0,63}$")

_MODES = ("background", "sync")

#: The ``agent_type`` the harness stamps on a SubagentStart payload for an
#: agent the WORKFLOW tool spawned (nexus-silj0), measured 2026-09-21 across
#: 11 STARTs from one Workflow-tool run (session 2109cc46, run
#: wf_baae5a4e-bfd: 1 enumerate + 7 trace + 3 verify agents). No PreToolUse
#: hook can write this class an EXPECT row in advance the way
#: ``hook_agent_dispatch_expect`` does for the Agent tool -- the fan-out
#: count is a property of the SCRIPT's execution (``pipeline()``/
#: ``parallel()`` fan out over data computed at runtime; a loop can be
#: budget-bounded or loop-until-dry), not knowable at PreToolUse time, and a
#: guessed EXPECT row would inflate the credit pool exactly the way a
#: duplicate hand-write does. Sam's ruling (2026-09-23, bead nexus-silj0,
#: option 2 of the bead's own candidates): this class gets its own bucket in
#: both the census and the undeclared audit -- counted and reported, never
#: folded into the ``undeclared`` deficit that exit code 2 exists to signal,
#: and never allowed to spend another type's EXPECT credit. Left alone,
#: every session that uses the Workflow tool at all ends with a non-zero
#: declaration audit, which is how the one signal that distinguishes a real
#: undeclared Agent dispatch from routine workflow use gets swamped into
#: noise.
WORKFLOW_SUBAGENT_TYPE = "workflow-subagent"


class ExpectationsUsageError(ValueError):
    """Invalid input to a ledger WRITER -- the bash library's exit code 2.

    Deliberately an exception rather than a returned code: every caller in
    this package runs inside ``_io.never_fail``, which turns it into the
    same silent, non-blocking outcome the bash hook produced, while the
    command tier's ledger verbs map it back to 2 (``entry.LEDGER_VERBS``).
    A writer must never append a malformed row -- it reshapes the TSV for
    every later reader, which is the failure this charset exists to stop.
    """


def _state_dir() -> Path:
    """Resolve and create the private state dir.

    ``chmod`` is applied on every call, as in bash: the dir may predate a
    version that created it 0700, and a ledger naming live agent ids should
    not be world-readable because of when it happened to be created.
    """
    root = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    directory = Path(root) / "nexus" / "orchestration"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:  # pragma: no cover — a dir we cannot chmod is still usable
        pass
    return directory


def _ts() -> str:
    """One timestamp shape everywhere: ISO-8601 UTC, second resolution.

    Second resolution is load-bearing rather than lazy -- the bash reader
    groups and compares rows on this field, and widening it would change
    what those comparisons mean.
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _append(file: str | Path, row: str) -> None:
    """One atomic ``O_APPEND|O_CREAT`` open under a private umask.

    No check-then-truncate: two callers racing the FIRST-EVER write to a
    session's file must not be able to wipe each other's row. The
    append-only, no-locks safety claim has to hold at creation time too,
    not merely once the file exists -- which is why the mode is passed to
    ``os.open`` rather than set afterwards.
    """
    fd = os.open(str(file), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (row + "\n").encode())
    finally:
        os.close(fd)


def expectations_append_row(
    session_id: str, verb: str, agent_id: str, detail: str = ""
) -> None:
    """Append one ``<ts>\t<verb>\t<agent_id>[\t<detail>]`` row.

    The bash called ``_expectations_append`` directly from
    ``subagent-stop.sh`` for the three verbs that have no named writer of
    their own -- ``REPORTED``, ``WOULDBLOCK`` and ``UNLANDEDWRITE``. Those
    are ledger rows like any other, so the port gives them a public door
    rather than having a second module reach through the underscore. The
    underscore then means what it says.

    The optional fourth field is inert to every exact-field reader, which
    is what lets ``REPORTED`` carry its resolution strength and
    ``UNLANDEDWRITE`` its ``<n> <tools>`` without any reader change.

    Raises :class:`ExpectationsUsageError` on a path-unsafe *session_id*,
    the same as :func:`expectations_file`. Callers in hook code swallow it:
    a ledger row is never worth failing a hook over.
    """
    row = f"{_ts()}\t{verb}\t{agent_id}"
    if detail:
        row = f"{row}\t{detail}"
    _append(expectations_file(session_id), row)


def expectations_file(session_id: str) -> str:
    """The per-session ledger path. Raises on a path-unsafe id."""
    if not session_id or not _SESSION_ID_RE.match(session_id):
        raise ExpectationsUsageError(
            f"expectations_file: invalid session_id {session_id!r} (path-safe charset only)"
        )
    return str(_state_dir() / f"{session_id}.expectations")


def expectations_expect(
    session_id: str, name: str, mode: str, dispatch_id: str = ""
) -> None:
    """The ORCHESTRATOR write path, called BEFORE the Agent dispatch.

    Write-before-dispatch is the load-bearing ordering (RDR-184): a row
    written after the dispatch races the subagent's own START. ``name`` is
    a SUBAGENT TYPE when written by the PreToolUse hook -- the only key
    both sides of the ledger can know. ``dispatch_id`` is optional and
    carried uninterpreted.
    """
    if not name or not _NAME_RE.match(name):
        raise ExpectationsUsageError(
            f"expectations_expect: invalid name {name!r} (agent-type charset)"
        )
    if mode not in _MODES:
        raise ExpectationsUsageError(
            f"expectations_expect: mode must be one of {_MODES}, got {mode!r}"
        )
    if "\t" in dispatch_id or "\n" in dispatch_id:
        raise ExpectationsUsageError("expectations_expect: tab/newline in dispatch_id")

    file = expectations_file(session_id)
    row = f"{_ts()}\tEXPECT\t{name}\t{mode}"
    if dispatch_id:
        row += f"\t{dispatch_id}"
    _append(file, row)


def expectations_start(session_id: str, agent_id: str, agent_type: str) -> None:
    """The SubagentStart stamp: non-load-bearing backfill.

    The payload cannot classify background-ness (cc-validation scenario
    27), so this records the framework-assigned ``agent_id`` for
    cross-checks and the retro audit rather than for the consult rule.
    """
    if not agent_id or not agent_type:
        raise ExpectationsUsageError(
            "expectations_start: session_id, agent_id and agent_type are required"
        )
    for value in (agent_id, agent_type):
        if "\t" in value or "\n" in value:
            raise ExpectationsUsageError(
                "expectations_start: tab/newline in agent_id/agent_type"
            )

    file = expectations_file(session_id)
    _append(file, f"{_ts()}\tSTART\t{agent_id}\t{agent_type}")


def _claim_credit(
    file: str,
    type_enc: str,
    agent_id: str,
    credit: int,
    spent: int,
    owners: list[str],
) -> bool:
    """Atomically claim one unit of this type's background credit.

    Returns True iff THIS call holds a unit -- either because it won one
    now, or because it already held one and is re-entering. False iff every
    unit was already spoken for by someone else.

    One unit == one slot symlink ``<file>.credit.<type_enc>.<n>`` for n in
    1..credit. The kernel, not a lock protocol, is what bounds the count.

    The link's TARGET is the claiming ``agent_id``, so the claim and the
    ownership stamp are the SAME atomic operation; a mkdir-then-write-owner
    pair would leave a window where a claimed slot is anonymous. The target
    is an IDENTITY, not a path -- these are dangling links by design, and
    anything that resolves or validates them breaks the mechanism.

    ``owners`` is the reconciliation input: one entry per already-recorded
    CONSUMED row, in file order. Slots are derived state and the rows are
    the durable record, so a ledger written by a build that predates slots
    has rows and no slots. Re-creating them first keeps a live session's
    accounting continuous across a plugin update instead of handing every
    in-flight type a fresh, fully-unspent pool. Idempotent, because every
    create onto an existing name simply loses.
    """
    base = f"{file}.credit.{type_enc}"

    for index, owner in enumerate(owners, start=1):
        try:
            os.symlink(owner, f"{base}.{index}")
        except OSError:
            pass  # already reconciled, or lost the race — both fine

    # Slots 1..spent are accounted for by the rows just reconciled, so the
    # first candidate is spent+1. Walking upward rather than from 1 keeps
    # the common case to a single syscall.
    for index in range(spent + 1, credit + 1):
        slot = f"{base}.{index}"
        if _CLAIM_INTERLEAVE is not None:  # test-only seam; None in production
            _CLAIM_INTERLEAVE()
        try:
            os.symlink(agent_id, slot)
            return True
        except FileExistsError:
            # Already claimed. If it is OUR claim, this is a re-entry after
            # a crash between the claim and its CONSUMED row: still owes,
            # and must not consume a second unit.
            try:
                if os.readlink(slot) == agent_id:
                    return True
            except OSError:  # pragma: no cover — vanished between calls
                continue
        except OSError:  # pragma: no cover — any other failure: try the next slot
            # The reconciliation loop above swallows every OSError and
            # continues, and bash's `ln -s ... 2>/dev/null` does the same
            # for the claim. Returning here instead made one transient
            # failure mid-search look like exhaustion.
            continue
    return False


def _archive_dir() -> Path:
    directory = (
        Path(os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state"))
        / "nexus"
        / "orchestration-archive"
    )
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:  # pragma: no cover
        pass
    return directory


def _readable_rows(session_id: str) -> list[list[str]] | None:
    """Every row of a session's ledger, or None if it cannot be read.

    The single fail-open seam for the consult helpers: a missing,
    unreadable or junk-bearing ledger must never block a stop, so every
    reader distinguishes "no ledger" (None) from "a ledger with no
    matching rows" ([]) and none of them raise.
    """
    try:
        file = expectations_file(session_id)
    except ExpectationsUsageError:
        return None
    try:
        text = Path(file).read_text()
    except OSError:
        return None
    # SPLIT ON \n ONLY, deliberately, to match `awk -F'\t'` with the
    # default RS="\n": awk leaves a literal \r attached to the last field
    # of a \r\n-terminated row, so a CRLF ledger's START type no longer
    # string-equals its EXPECT type and bash reports UNDECLARED on a
    # well-formed file. `splitlines()` strips \r cleanly and disagreed.
    #
    # This REPRODUCES A BASH DEFECT on purpose. The port's bar for Phase 2
    # is that the two agree exactly, because a live session can be written
    # by one and read by the other, and a port that silently "fixes" a
    # reader changes what a shared ledger means mid-flight. The writer here
    # only ever emits \n, so a CRLF ledger needs a hand edit or a Windows
    # tool to occur at all. Bead nexus-q02nx.14 deletes the bash library
    # and is where this should be corrected rather than matched.
    return [line.split("\t") for line in text.split("\n") if line]


def expectations_mark_blocked(session_id: str, agent_id: str, cause: str = "") -> None:
    """Append a BLOCKED row, optionally with a disclosed cause.

    The tab/newline guard is the same one ``expectations_start`` carries
    and for a sharper reason: a misaligned BLOCKED row would never match
    :func:`expectations_already_blocked`'s exact-field comparison, which
    silently defeats block-at-most-once.
    """
    if not agent_id:
        raise ExpectationsUsageError(
            "expectations_mark_blocked: session_id and agent_id are required"
        )
    for value in (agent_id, cause):
        if "\t" in value or "\n" in value:
            raise ExpectationsUsageError(
                "expectations_mark_blocked: tab/newline in agent_id/cause"
            )

    file = expectations_file(session_id)
    row = f"{_ts()}\tBLOCKED\t{agent_id}"
    if cause:
        row += f"\t{cause}"
    _append(file, row)


def expectations_already_blocked(session_id: str, agent_id: str) -> bool:
    """True iff a BLOCKED row exists for this exact agent_id.

    A missing file is False -- not blocked yet -- which composes with
    ``owes_report``'s fail-open into "never block".
    """
    if not session_id or not agent_id:
        return False
    rows = _readable_rows(session_id)
    if rows is None:
        return False
    return any(len(r) > 2 and r[1] == "BLOCKED" and r[2] == agent_id for r in rows)


def expectations_last_terminal(session_id: str, agent_id: str) -> str:
    """The LAST terminal verb recorded for this agent, or "".

    Terminal means REPORTED, BLOCKED or WOULDBLOCK. Last, not first: an
    agent can be blocked and then report, and the caller wants where it
    ended up. Never raises -- an unreadable ledger is "".
    """
    if not session_id or not agent_id:
        return ""
    rows = _readable_rows(session_id)
    if rows is None:
        return ""
    verb = ""
    for row in rows:
        if len(row) > 2 and row[1] in ("REPORTED", "BLOCKED", "WOULDBLOCK") and row[2] == agent_id:
            verb = row[1]
    return verb


def expectations_archive() -> None:
    """Copy each ledger into the archive dir, newest-wins. Never fails."""
    src, dst = _state_dir(), _archive_dir()
    for path in src.glob("*.expectations"):
        if not path.is_file():
            continue
        target = dst / path.name
        try:
            if not target.exists() or path.stat().st_mtime > target.stat().st_mtime:
                target.write_bytes(path.read_bytes())
                os.utime(target, (path.stat().st_atime, path.stat().st_mtime))
        except OSError:  # pragma: no cover — best effort, never fail the caller
            continue


def expectations_sweep() -> None:
    """Best-effort reap of ledgers and their credit slots past the floor.

    The slots are swept too, and the reason it is safe is RECONCILIATION,
    not the floor: a ledger's mtime refreshes on every append, so a session
    active past the floor keeps its ledger while its day-0 slots age out
    and are deleted. Any deleted slot that still has a backing CONSUMED row
    is re-created (indices 1..spent) on the next claim, so it cannot become
    a second spend. Slots above ``spent`` are orphans, and freeing them is
    the only reclaim that exists.

    An earlier version of the bash comment called deleting a live-ledger
    slot "impossible", which was false; the reason is stated here because a
    maintainer who trusted that word could make a change that is NOT safe.
    """
    cutoff = time.time() - _REAP_DAYS * 86400
    directory = _state_dir()
    for pattern, predicate in (
        ("*.expectations", Path.is_file),
        ("*.expectations.credit.*", Path.is_symlink),
    ):
        for path in directory.glob(pattern):
            try:
                if predicate(path) and path.lstat().st_mtime < cutoff:
                    path.unlink()
            except OSError:  # pragma: no cover — best effort
                continue


@dataclass(frozen=True)
class LedgerReport:
    """A reader's stdout lines plus its exit code.

    The ledger verbs are the only ones in this epic whose CONTRACT is the
    exit code -- ``undeclared`` 0/1/2/3, ``reconcile`` 0/2/4, ``census``
    0/1 -- and those codes are quoted verbatim in AGENTS.md, so they are a
    public API rather than an implementation detail. Carrying them in a
    value (instead of raising, or returning a bare int) keeps the Python
    caller able to read the lines AND the code, which is what
    ``stop_verification_hook``'s eventual port needs.

    ``note`` is stderr, never stdout: it must not land in a hook's decision
    channel.
    """

    lines: list[str] = field(default_factory=list)
    code: int = 0
    note: str = ""


def expectations_undeclared(session_id: str) -> LedgerReport:
    """The declaration-completeness retro audit (RDR-184 item .16).

    One ``UNDECLARED\t<agent_id>\t<agent_type>`` line per START whose type
    has no unspent EXPECT credit left. An EXPECT row of EITHER mode supplies
    credit, so a deliberately-declared sync dispatch stays audit-clean.

    A START whose type is exactly :data:`WORKFLOW_SUBAGENT_TYPE` (nexus-silj0,
    Sam's ruling) is pulled out of the audited population entirely, before
    ``checked``/``recognized``/``undeclared`` are computed: it is counted and
    reported on its own ``WORKFLOW\tchecked=<n>`` line, but it can neither
    land in ``undeclared`` (so it never drives exit code 2) nor spend a unit
    of some other type's EXPECT credit (it is never in the credit-consuming
    loop at all). The line is emitted only when ``n > 0`` and always
    immediately before ``SUMMARY``, so the "a populated result always ends
    with SUMMARY or BLINDSPOT" contract (asserted in
    ``TestTheEmptyShapeIsNotNarrowerThanThePopulatedOne``) is unchanged.

    Exit codes, quoted in AGENTS.md: 0 clean, 1 BLINDSPOT, 2 undeclared>0,
    3 no ledger. **3 is not a pass** -- absence of a ledger is not evidence
    of cleanliness, which is why it carries a note naming the two
    explanations and how to tell them apart. A session with ONLY workflow
    STARTs and no EXPECT rows is genuinely 0 (nothing Agent-tool-shaped to
    audit), not 1 BLINDSPOT (that code requires an EXPECT row with zero
    STARTs, and a workflow-only session has neither) -- the WORKFLOW line is
    what keeps that 0 from reading as "nothing happened" when 11 agents
    plainly did.
    """
    rows = _readable_rows(session_id)
    if rows is None:
        return LedgerReport(
            code=3,
            note=(
                f"NOTE — no ledger file for session '{session_id}': either this "
                "session dispatched no agents (legitimately nothing to audit) or "
                "the session id is wrong — cross-check with a known-good sid via "
                "expectations_census; absence is not evidence of cleanliness "
                "(nexus-8dr2u)"
            ),
        )

    order: list[str] = []
    stype: dict[str, str] = {}
    expect_types: set[str] = set()
    credit: dict[str, int] = {}
    seen_dispatch: set[str] = set()
    expect_total = 0
    workflow_order: list[str] = []
    workflow_seen: set[str] = set()

    for row in rows:
        verb = row[1] if len(row) > 1 else ""
        if verb == "START" and len(row) > 2:
            # A row with FEWER fields is still audited, because awk reads a
            # missing $4 as "" rather than skipping the row. Requiring 4
            # fields made a truncated START — a crash mid-write, or a
            # pre-slots-era ledger — report CLEAN, which inverts the
            # fail-closed contract of the one function whose entire job is
            # to surface that anomaly. Measured against bash: a 3-field
            # START gives rc=2 with `UNDECLARED\t<id>\t` there and gave
            # rc=0 here.
            agent_id = row[2]
            agent_type = row[3] if len(row) > 3 else ""
            if agent_type == WORKFLOW_SUBAGENT_TYPE:
                # Its own bucket (nexus-silj0): counted, but pulled out
                # before the credit-consuming loop below, so it can neither
                # become UNDECLARED nor spend another type's credit.
                if agent_id not in workflow_seen:
                    workflow_seen.add(agent_id)
                    workflow_order.append(agent_id)
                continue
            if agent_id not in stype:
                order.append(agent_id)
                stype[agent_id] = agent_type
        elif verb == "EXPECT" and len(row) > 2:
            # Dedupe by dispatch_id. The writing hook takes a BOUNDED lock,
            # so a double registration that outlasts the budget can append
            # the same dispatch twice -- and a duplicate EXPECT is not the
            # harmless nuisance a duplicate START is: it inflates the credit
            # pool and MASKS an undeclared start. The reader can settle it
            # unambiguously, so it does.
            dispatch_id = row[4] if len(row) > 4 else ""
            if dispatch_id and dispatch_id in seen_dispatch:
                continue
            if dispatch_id:
                seen_dispatch.add(dispatch_id)
            expect_types.add(row[2])
            credit[row[2]] = credit.get(row[2], 0) + 1
            expect_total += 1

    lines: list[str] = []
    checked = len(order)
    recognized = 0
    undeclared = 0
    for agent_id in order:
        agent_type = stype[agent_id]
        # `recognized` is a TALLY, not a gate (nexus-houpu): it records how
        # many STARTs had an EXPECT row of their type anywhere in the
        # ledger, which distinguishes an inert dispatch hook from a
        # partially-missed one. Every START is evaluated either way -- there
        # is no unrecognised class that skips the credit check.
        if agent_type in expect_types:
            recognized += 1
        if credit.get(agent_type, 0) > 0:
            credit[agent_type] -= 1
            continue
        lines.append(f"UNDECLARED\t{agent_id}\t{agent_type}")
        undeclared += 1

    if workflow_order:
        lines.append(f"WORKFLOW\tchecked={len(workflow_order)}")

    lines.append(
        f"SUMMARY\tchecked={checked} recognized={recognized} "
        f"unrecognized={checked - recognized} undeclared={undeclared}"
    )

    # The one false-clean shape left: the ledger declares dispatches and
    # records no START at all, so the walk examined nothing. Zero UNDECLARED
    # lines there means "nothing was checkable", never "compliant".
    if checked == 0 and expect_total > 0:
        lines.append(
            f"BLINDSPOT\tledger holds {expect_total} EXPECT row(s) and ZERO START "
            f"rows - the audit walked nothing, so undeclared={undeclared} is NOT "
            "evidence of compliance (check the SubagentStart stamp is registered "
            "and NX_ORCH_STOP_GUARD is not off)"
        )
        return LedgerReport(lines=lines, code=1)
    if undeclared > 0:
        return LedgerReport(lines=lines, code=2)
    return LedgerReport(lines=lines, code=0)


@dataclass(frozen=True)
class OwesVerdict:
    """Whether a stopping agent owes a completion report, and why.

    ``cause`` mirrors the bash ``EXPECTATIONS_OWES_CAUSE`` global, whose
    two values (``lock-exhausted``, ``credit-slot-orphan``) are asserted BY
    VALUE in ``tests/hooks/test_subagent_stop_hook.py`` and appended to the
    block reason the operator reads. It is "" whenever the verdict came
    from the ledger rather than from a degraded path.
    """

    owes: bool
    cause: str = ""


def _owes_lock_tries() -> int:
    raw = os.environ.get("NX_EXPECT_LOCK_TRIES", "10")
    tries = int(raw) if raw.isdigit() else 10
    return min(tries, 600)


def _type_enc(agent_type: str) -> str:
    """The round-2 colon encoding. ``:`` is legal in a subagent type and
    would otherwise appear in sidecar FILE NAMES."""
    return agent_type.replace(":", "__")


def _read_type_credit(rows: list[list[str]], agent_type: str, agent_id: str) -> tuple[str, int, int, list[str]]:
    """The consult rule's single ledger pass. Returns (verdict, credit, spent, owners).

    ``mixed`` wins over everything: a type that has ANY non-background
    EXPECT row is not a pure background pool, and guessing at a mixed pool
    is how an ordinary sync dispatch gets blocked.
    """
    seen_dispatch: set[str] = set()
    credit = 0
    mixed = False
    self_consumed = False
    owners: list[str] = []

    for row in rows:
        verb = row[1] if len(row) > 1 else ""
        if verb == "EXPECT" and len(row) > 2:
            dispatch_id = row[4] if len(row) > 4 else ""
            if dispatch_id and dispatch_id in seen_dispatch:
                continue
            if dispatch_id:
                seen_dispatch.add(dispatch_id)
            if row[2] == agent_type:
                if len(row) > 3 and row[3] == "background":
                    credit += 1
                else:
                    mixed = True
        elif verb == "CONSUMED" and len(row) > 3 and row[3] == agent_type:
            if row[2] == agent_id:
                self_consumed = True
            else:
                owners.append(row[2])

    if mixed:
        return "no", credit, len(owners), owners
    if self_consumed:
        return "self", credit, len(owners), owners
    if credit > len(owners):
        return "new", credit, len(owners), owners
    return "no", credit, len(owners), owners


def expectations_owes_report(
    session_id: str, agent_id: str, agent_type: str
) -> OwesVerdict:
    """The consult rule: does this stopping agent owe a completion report?

    TYPE-KEYED, because the type is the only key both sides of the ledger
    can know. Fails OPEN on every degraded input -- no session, bad
    charset, missing or unreadable ledger -- because a missing ledger must
    never block a stop.

    The lock is EFFICIENCY ONLY. Round 3 moved correctness to the atomic
    slot claim precisely because a name-based lock is stealable, so nothing
    below depends on holding it; it merely stops same-type racers doing
    redundant passes and racing onto the same slot name. Its one behavioural
    role is the exhaustion path, which blocks with a disclosed cause rather
    than consulting credit -- over-blocking is explicable, and a silent miss
    is the failure this subsystem exists to prevent.
    """
    if not session_id or not agent_id or not agent_type:
        return OwesVerdict(False)
    if not _NAME_RE.match(agent_type):
        return OwesVerdict(False)
    if "\t" in agent_id or "\n" in agent_id:
        return OwesVerdict(False)

    if _readable_rows(session_id) is None:
        return OwesVerdict(False)
    file = expectations_file(session_id)
    enc = _type_enc(agent_type)
    lockdir = f"{file}.owes.{enc}.lock"

    held = _acquire_owes_lock(lockdir)
    if not held:
        # OPERATOR-FACING, and load-bearing (bead nexus-q02nx.12 found it
        # missing from the first port): the ledger's 4th field records the
        # cause for an auditor, but the person watching an agent get blocked
        # has only this line to tell a precautionary block from a verified
        # one. Carried verbatim from the bash, which is why it is a message
        # field rather than prose assembled here --
        # `tests/hooks/test_subagent_stop_hook.py` pins five of its
        # substrings by value.
        _emit(
            "warning",
            "expectations_owes_lock_exhausted",
            message=(
                f"expectations: owes-report lock budget exhausted "
                f"({_owes_lock_tries()} tries) for type '{agent_type}'; no "
                f"credit consulted, fixed default = owes (BLOCK, "
                f"cause=lock-exhausted; fail-open direction per "
                f"nexus-bk974/nexus-4b8sz/nexus-7z7rj/nexus-plycy)"
            ),
        )
        return OwesVerdict(True, "lock-exhausted")

    # nexus-7z7rj test seam: widen the critical section so a test can force
    # the other racers to exhaust their try budget.
    _test_delay("NX_EXPECT_LOCK_HOLD_DELAY_S")

    try:
        # READ AFTER THE LOCK, NEVER BEFORE. Round 3's chosen fix is
        # "decision-only-under-lock": the ledger is read once the lock is
        # held, precisely so a decision is never made from data captured
        # before the wait. Reading first and deciding on those rows
        # reopens the window the lock exists to close — a same-type
        # dispatch whose EXPECT row lands DURING our lock-wait would be
        # invisible, and the verdict would be "does not owe" against a
        # ledger that plainly shows unspent credit. The ceiling invariant
        # still holds in that state, so no concurrency test keyed on it
        # would notice; what is lost is DETECTION, in the one direction
        # this subsystem exists to protect.
        rows = _readable_rows(session_id)
        if rows is None:  # vanished during the wait — fail open
            return OwesVerdict(False)
        verdict, credit, spent, owners = _read_type_credit(rows, agent_type, agent_id)
        if verdict == "self":
            # A CONSUMED row already names this agent: it is re-entering
            # after a crash between its claim and its stop. Still owes, and
            # must not consume a second unit.
            return OwesVerdict(True)
        if verdict != "new":
            return OwesVerdict(False)

        # nexus-ols6a test seam: widen READ -> CLAIM, so a test can force
        # every racer to decide from the SAME credit state. Without it,
        # switching the lock off removes exclusion but NOT simultaneity, and
        # the falsifier's detection still rides thread interleaving --
        # measured in bash as a 1-in-12 FALSE PASS against a deliberately
        # broken claim.
        _test_delay("NX_EXPECT_CLAIM_DELAY_S")

        if _claim_credit(file, enc, agent_id, credit, spent, owners):
            # nexus-ols6a test seam: widen CLAIM -> APPEND, the window in
            # which a killed claimant leaves an orphaned slot with no
            # CONSUMED row -- the exact state the orphan branch below is
            # written to detect.
            _test_delay("NX_EXPECT_APPEND_DELAY_S")
            _append(file, f"{_ts()}\tCONSUMED\t{agent_id}\t{agent_type}")
            return OwesVerdict(True)

        # Every slot is taken. Re-read: if the ROWS still say this type has
        # unspent credit, that state is provably inconsistent and the only
        # explanation is a claimant killed between its slot claim and its
        # CONSUMED row -- SubagentStop has a 10s hook timeout, so that kill
        # is a routine, load-correlated event rather than a rarity.
        # Deliberately NOT reclaiming the orphaned slot: that is the
        # check-then-act shape whose unfixability this module documents, in
        # a new costume.
        for attempt in (1, 2):
            fresh = _readable_rows(session_id) or []
            _, fresh_credit, fresh_spent, _ = _read_type_credit(fresh, agent_type, "")
            if fresh_credit <= fresh_spent:
                return OwesVerdict(False)
            if attempt == 2:
                break
            time.sleep(0.1)
        # Provably inconsistent: no slot left, yet the ledger says this type
        # still has unspent credit. FAIL LOUD in the guard's usual safe
        # direction (block, disclosed) rather than silently waving the stop
        # through -- the silent version is what let ONE killed hook cost TWO
        # unguarded stops instead of one.
        _emit(
            "warning",
            "expectations_credit_slot_orphan",
            message=(
                f"expectations: credit-slot orphan for type '{agent_type}' — "
                f"every credit slot is claimed but the ledger records only "
                f"{fresh_spent} of {fresh_credit} spent, so a claimant was "
                f"killed between its slot claim and its CONSUMED row "
                f"(SubagentStop has a 10s hook timeout). Blocking this stop "
                f"rather than silently passing it (cause=credit-slot-orphan; "
                f"nexus-ols6a)"
            ),
        )
        return OwesVerdict(True, "credit-slot-orphan")
    finally:
        try:
            os.rmdir(lockdir)
        except OSError:
            pass


def _acquire_owes_lock(lockdir: str) -> bool:
    """Best-effort mutual exclusion. True iff acquired (or disabled).

    Correctness must not depend on this -- the permanent test disables it
    and requires the ceiling to hold anyway.
    """
    if os.environ.get("NX_EXPECT_LOCK_DISABLE") == "1":
        return True
    # The stale reap is KNOWN-UNSAFE and kept deliberately: it can delete a
    # lock it does not own (test / find / remove are three steps and the
    # lock can change hands between them). Since round 3 that steal is
    # merely wasteful rather than incorrect, and removing it without a
    # replacement self-heal would trade a harmless inefficiency for a
    # session-long wedge when one holder is SIGKILLed.
    try:
        if os.path.isdir(lockdir) and (time.time() - os.stat(lockdir).st_mtime) > 60:
            os.rmdir(lockdir)
    except OSError:
        pass
    for _ in range(_owes_lock_tries()):
        try:
            os.mkdir(lockdir)
            return True
        except FileExistsError:
            time.sleep(0.1)
        except OSError:
            # NOT "return True". An earlier version treated any non-EEXIST
            # OSError -- EACCES, EROFS, ENOSPC, ENOTDIR on the state dir --
            # as "lock acquired" and proceeded unlocked with no diagnostic.
            # Bash does not distinguish the cause: every mkdir failure is
            # just a failed attempt, the loop runs its full budget, and
            # exhaustion takes the DISCLOSED path (a stderr line and
            # cause=lock-exhausted). Returning True silently inverted this
            # module's own stated direction -- over-blocking is explicable,
            # a silent miss is the failure this subsystem exists to prevent
            # -- for the one case where the lock is broken rather than
            # contended. Found in the bead .15 review; it was neither
            # announced in the RDR nor covered by a test in either
            # direction.
            time.sleep(0.1)
    return False


#: Bounded-call timeout for every ``nx tuple ...`` the census shells out to,
#: env-overridable exactly as bash's ``NX_EXPECT_CENSUS_NX_TIMEOUT_S``.
#: Bash needed a hand-rolled watchdog (``_expectations_run_bounded``)
#: because it has no built-in subprocess deadline; ``subprocess.run(timeout=)``
#: is the direct Python equivalent, so no watchdog/marker-file dance is
#: ported -- the OBSERVABLE contract (a bounded call, a named SPACE_FALLBACK
#: reason on expiry) is what is reproduced, not bash's mechanism for getting
#: there.
_NX_CENSUS_TIMEOUT_DEFAULT = "45"

#: The connected-space retention window bash keys ``SPACE_NEVER_RAN`` vs.
#: ``SPACE_OUTSIDE_WINDOW`` on: 90 days, matching
#: ``_EXPECTATIONS_LEDGER_RETENTION_S=$((90 * 24 * 3600))``.
_LEDGER_RETENTION_S = 90 * 24 * 3600


def _nx_census_timeout_s() -> tuple[str, float]:
    """The (raw string, numeric seconds) bounded-call timeout.

    Mirrors bash's ``${NX_EXPECT_CENSUS_NX_TIMEOUT_S:-45}``: unset or empty
    falls back to the default. NOT reproduced: a non-numeric override.
    Bash would pass the bogus string straight to `sleep` in its watchdog and
    degrade in its own (undefined) way; this falls back to the numeric
    default instead of raising, because there is no watchdog here for a
    bogus value to break. Nobody sets this to a non-numeric value in
    practice -- it is a raw seconds count -- so this is a considered
    non-reproduction, not an overlooked one.
    """
    raw = os.environ.get("NX_EXPECT_CENSUS_NX_TIMEOUT_S") or _NX_CENSUS_TIMEOUT_DEFAULT
    try:
        return raw, float(raw)
    except ValueError:
        return raw, float(_NX_CENSUS_TIMEOUT_DEFAULT)


def _scrub_reason(text: str) -> str:
    """``tr '\\n\\t' '  ' | tr -s ' '``: fold newlines/tabs to spaces, then
    squeeze runs of spaces to one -- so a multi-line CLI error never breaks
    the census's one-reason-per-line output. ``"no output"`` mirrors bash's
    ``${combined:-no output}``, triggered on an empty (not merely falsy)
    string, same as the shell's unset-or-null test."""
    text = text if text else "no output"
    return re.sub(r" +", " ", text.replace("\n", " ").replace("\t", " "))


def _run_nx_bounded(args: list[str], timeout_s: float) -> tuple[str, int]:
    """Run an ``nx`` subcommand with stdout+stderr merged, bounded by
    *timeout_s*. Returns ``(combined_output, returncode)``; a killed-by-
    deadline expiry returns rc ``124``, the same sentinel bash's
    ``_expectations_run_bounded`` uses, so both fallback branches below key
    on the identical value.
    """
    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_s,
        )
        return proc.stdout or "", proc.returncode
    except subprocess.TimeoutExpired as exc:
        return (exc.output or ""), 124
    except OSError as exc:  # pragma: no cover — nx vanished between the PATH
        # check and the spawn; not reachable through a real fixture.
        return str(exc), 127


def _tsv_newest_first_field(file: str) -> str:
    """The ledger's last row's first (timestamp) field, or "".

    ``tail -n 1 "$file" | cut -f1``: an append-only file's chronologically
    last row is its last line by construction, same ordering guarantee
    every other reader in this module relies on.
    """
    try:
        text = Path(file).read_text()
    except OSError:
        return ""
    lines = text.splitlines()
    if not lines:
        return ""
    return lines[-1].split("\t", 1)[0]


def _file_age_s(file: str) -> int | None:
    """Seconds since *file*'s mtime, or None on any stat failure."""
    try:
        mtime = os.stat(file).st_mtime
    except OSError:
        return None
    return int(time.time() - mtime)


def _parse_iso(ts: str) -> datetime.datetime | None:
    """``datetime.fromisoformat`` with a ``Z``-suffix normalised to
    ``+00:00`` first -- ``fromisoformat`` predates ``Z`` support on the
    Python versions this reproduces the bash's embedded ``python3 -c``
    block against."""
    if not ts:
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.datetime.fromisoformat(s)
    except ValueError:
        return None


def _census_space(session_id: str, file: str) -> list[str]:
    """Port of bash's ``_expectations_census_space``. See that function's
    header comment (``tests/e2e/lib/expectations.sh``) for the full
    contract: fail-open on every axis with a NAMED reason, and the
    vacuity rule that an empty subspace list is a BLINDSPOT rather than a
    silent clean read. NEVER raises; NEVER changes
    :func:`expectations_census`'s own exit code -- this is report, not
    verdict, same as the TSV half above.
    """
    target = f"ledger/{session_id}"

    if shutil.which("nx") is None:
        return ["SPACE_FALLBACK\treason=PATH has no nx"]

    timeout_raw, timeout_s = _nx_census_timeout_s()
    combined, rc = _run_nx_bounded(
        ["nx", "tuple", "list", "--prefix", "ledger/", "--json"], timeout_s
    )
    if rc == 124:
        return [f"SPACE_FALLBACK\treason=nx tuple list exceeded {timeout_raw}s"]
    if rc != 0:
        return [
            "SPACE_FALLBACK\treason=nx tuple list --prefix ledger/ failed "
            f"(rc={rc}): {_scrub_reason(combined)}"
        ]

    try:
        rows = json.loads(combined)
    except Exception:  # noqa: BLE001 — any parse failure is a named fallback
        return ["SPACE_FALLBACK\treason=unparseable JSON from nx tuple list"]
    if not isinstance(rows, list):
        return ["SPACE_FALLBACK\treason=nx tuple list --json did not return an array"]
    if not rows:
        return [
            "SPACE_BLINDSPOT\treason=subspace_list returned zero subspaces under "
            "ledger/ - the space walk examined nothing"
        ]

    found = next(
        (r for r in rows if isinstance(r, dict) and r.get("subspace") == target),
        None,
    )
    tsv_newest = _tsv_newest_first_field(file)
    age = _file_age_s(file)

    if found is not None:
        total = found.get("total", 0)
        newest = found.get("newest_created_at") or ""
        a, b = _parse_iso(newest), _parse_iso(tsv_newest)
        drift = f"{(b - a).total_seconds():.0f}" if a is not None and b is not None else "unknown"
        space_disp = newest if newest else "-"
        tsv_disp = tsv_newest if tsv_newest else "-"
        return [
            f"SPACE_PRESENT\tsubspace={target} total={total}",
            f"SPACE_AGE\tspace_newest={space_disp} tsv_newest={tsv_disp} drift_seconds={drift}",
        ]

    age_disp = age if age is not None else "unknown"
    if age is not None and age < _LEDGER_RETENTION_S:
        return [f"SPACE_NEVER_RAN\tsubspace={target} age_seconds={age_disp}"]
    return [f"SPACE_OUTSIDE_WINDOW\tsubspace={target} age_seconds={age_disp}"]


def _declares_verify(templates_json: str) -> str:
    """``"yes"``/``"no"``/``"error"`` -- does the connected engine's
    ``ledger/<session_id>`` template declare the ``verify`` dimension.
    The literal string ``"ledger/<session_id>"`` is deliberate: a template
    LISTING names its parameterised templates with the placeholder itself,
    not a real session id."""
    try:
        data = json.loads(templates_json)
    except Exception:  # noqa: BLE001 — any parse failure is the caller's to report
        return "error"
    templates = data.get("templates") if isinstance(data, dict) else None
    if not isinstance(templates, list):
        return "error"
    for t in templates:
        if isinstance(t, dict) and t.get("name") == "ledger/<session_id>":
            dims = t.get("dimensions")
            return "yes" if isinstance(dims, dict) and "verify" in dims else "no"
    return "no"


def _census_verify_absent(session_id: str) -> list[str]:
    """Port of bash's ``_expectations_census_verify_absent``. See that
    function's header comment for the full contract, including the
    below-floor-engine gate on the ``verify`` dimension. NEVER raises;
    NEVER changes :func:`expectations_census`'s own exit code.

    INHERITED ASYMMETRY, not introduced here: unlike :func:`_census_space`,
    bash's counterpart has no special-cased 124/timeout branch for either
    of its two bounded calls -- a deadline expiry falls straight into the
    generic ``rc != 0`` ``VERIFY_FALLBACK`` path below, exactly as it does
    in the shell.
    """
    target = f"ledger/{session_id}"

    if shutil.which("nx") is None:
        return ["VERIFY_FALLBACK\treason=PATH has no nx"]

    _, timeout_s = _nx_census_timeout_s()
    templates_json, rc = _run_nx_bounded(["nx", "tuple", "templates", "--json"], timeout_s)
    if rc != 0:
        return [
            "VERIFY_FALLBACK\treason=nx tuple templates --json failed "
            f"(rc={rc}): {_scrub_reason(templates_json)}"
        ]

    declares_verify = _declares_verify(templates_json)
    if declares_verify == "error":
        return ["VERIFY_FALLBACK\treason=unparseable JSON from nx tuple templates"]
    if declares_verify != "yes":
        return [
            "VERIFY_UNVERIFIABLE\treason=connected engine ledger template does "
            "not declare verify yet (below engine-service-v0.1.118)"
        ]

    rows_json, rc = _run_nx_bounded(
        ["nx", "tuple", "rd", target, "--pattern", "kind=report", "-n", "300", "--json"],
        timeout_s,
    )
    if rc != 0:
        return [
            f"VERIFY_FALLBACK\treason=nx tuple rd {target} --pattern kind=report "
            f"failed (rc={rc}): {_scrub_reason(rows_json)}"
        ]

    try:
        rows = json.loads(rows_json)
    except Exception:  # noqa: BLE001 — any parse failure is a named fallback
        return ["VERIFY_FALLBACK\treason=unparseable JSON from nx tuple rd"]
    if not isinstance(rows, list):
        return ["VERIFY_FALLBACK\treason=nx tuple rd --json did not return an array"]

    n = sum(
        1
        for r in rows
        if isinstance(r, dict) and (r.get("dims") or {}).get("verify") != "present"
    )
    return [f"VERIFY_ABSENT_COUNT\tn={n}"]


def expectations_census(session_id: str) -> LedgerReport:
    """The scripted retro census (nexus-hybv1) -- never hand-count.

    One ``AGENT`` line per agent that appears anywhere as a START or a
    terminal, in first-appearance order, carrying its type, its terminal
    classification and whether its dispatch was declared. Then
    ``EXPECTED_NO_START`` for every declared name with fewer STARTs than
    EXPECT rows, a ``ROWS`` tally, a ``CLASSIFIED`` tally and a
    ``BLINDSPOT`` line.

    A START whose type is exactly :data:`WORKFLOW_SUBAGENT_TYPE` (nexus-silj0)
    gets no ``AGENT`` line and never reaches ``all_start``/``order`` --
    it cannot become ``checked``, ``undeclared`` or a ``no_terminal`` ghost,
    and it cannot spend another type's EXPECT credit, because it is pulled
    out before any of that bookkeeping runs. It is still counted: a single
    ``WORKFLOW\tchecked=<n>`` line (n > 0 only) reports how many, placed
    before ``ROWS`` so a Workflow-tool-heavy session does not read as "the
    walk found nothing" merely because its agents are bucketed elsewhere.

    Exit codes are 0 and 1 ONLY -- never 2. That vocabulary belongs to
    ``undeclared`` alone, and conflating them is how a census gets read as
    an audit. 1 means the walk examined nothing while the ledger declared
    dispatches -- workflow STARTs never affect this either, since ``checked``
    excludes them.

    A terminal is classified rather than merely recorded, because BLOCKED
    followed by REPORTED is the success path of the whole guard: the agent
    was stopped, told why, and came back with its report. That is
    ``BLOCKED_RESOLVED``, and it is counted separately by whether the
    report arrived immediately or later.

    Two more lines are appended after everything above, RDR-205 Phase 4.1
    (nexus-em75s.19): one ``SPACE_*`` line (see :func:`_census_space`) and
    one ``VERIFY_*`` line (see :func:`_census_verify_absent`), cross-
    checking the ledger's TSV view against the connected engine's tuple
    space. Neither can change ``code`` -- they are report, not verdict,
    same as everything above -- and neither is printed at all unless the
    ledger file itself was readable (an absent/unsafe session_id returns
    before either runs, matching bash's placement after its own
    ``[[ -r "$file" ]] || return 0`` guard).
    """
    if not session_id:
        return LedgerReport()
    rows = _readable_rows(session_id)
    if rows is None:
        return LedgerReport()

    seen_exact: set[str] = set()
    seen_dispatch: set[str] = set()
    verb_rows: dict[str, int] = {}
    expect_names: set[str] = set()
    expect_order: list[str] = []
    expect_rows: dict[str, int] = {}
    credit: dict[str, int] = {}
    start_count: dict[str, int] = {}
    stype: dict[str, str] = {}
    term: dict[str, str] = {}
    order: list[str] = []
    listed: set[str] = set()
    all_start: set[str] = set()
    res_immediate = res_later = 0
    workflow_order: list[str] = []
    workflow_seen: set[str] = set()

    for row in rows:
        exact = "\t".join(row)
        if exact in seen_exact:  # nexus-3h0u6: exact-duplicate rows
            continue
        seen_exact.add(exact)
        verb = row[1] if len(row) > 1 else ""
        who = row[2] if len(row) > 2 else ""

        if verb == "EXPECT":
            dispatch_id = row[4] if len(row) > 4 else ""
            if dispatch_id and dispatch_id in seen_dispatch:
                continue
            if dispatch_id:
                seen_dispatch.add(dispatch_id)

        verb_rows[verb] = verb_rows.get(verb, 0) + 1

        if verb == "EXPECT":
            if who not in expect_names:
                expect_order.append(who)
            expect_names.add(who)
            expect_rows[who] = expect_rows.get(who, 0) + 1
            credit[who] = credit.get(who, 0) + 1
        elif verb == "START":
            agent_type = row[3] if len(row) > 3 else ""
            if agent_type == WORKFLOW_SUBAGENT_TYPE:
                # Its own bucket (nexus-silj0): still tallied into
                # start_count (so EXPECTED_NO_START stays correct if this
                # type is ever hand-declared), but never all_start/order --
                # that is what keeps it out of `checked`/`undeclared`/
                # `no_terminal`.
                start_count[agent_type] = start_count.get(agent_type, 0) + 1
                if who not in workflow_seen:
                    workflow_seen.add(who)
                    workflow_order.append(who)
            elif who not in all_start:
                all_start.add(who)
                stype[who] = agent_type
                start_count[agent_type] = start_count.get(agent_type, 0) + 1
                if who not in listed:
                    order.append(who)
                    listed.add(who)
        elif verb in ("REPORTED", "BLOCKED", "WOULDBLOCK"):
            if who in workflow_seen:
                # A terminal for a workflow agent: not part of the
                # declaration audit's population, and must not fall into
                # the "no-start ghost" branch below for lack of a stype
                # entry.
                continue
            if who not in listed:
                order.append(who)
                listed.add(who)
            if verb == "REPORTED":
                if term.get(who) == "BLOCKED_UNRESOLVED":
                    term[who] = "BLOCKED_RESOLVED"
                    if len(row) > 3 and row[3] == "later":
                        res_later += 1
                    else:
                        res_immediate += 1
                elif term.get(who) != "BLOCKED_RESOLVED":
                    term[who] = "REPORTED"
            elif verb == "BLOCKED":
                term[who] = "BLOCKED_UNRESOLVED"
            else:
                term[who] = "WOULDBLOCK"

    lines: list[str] = []
    cls: dict[str, int] = {}
    checked = len(all_start)
    recognized = undeclared = nostart = 0

    for agent_id in order:
        terminal = term.get(agent_id) or "NO_TERMINAL"
        agent_type = stype.get(agent_id, "")
        if not agent_type:
            lines.append(f"AGENT\t{agent_id}\t-\t{terminal}\tno-start")
            nostart += 1
        else:
            if agent_type in expect_names:
                recognized += 1
            if credit.get(agent_type, 0) > 0:
                credit[agent_type] -= 1
                declared = "declared"
            else:
                declared = "undeclared"
                undeclared += 1
            lines.append(f"AGENT\t{agent_id}\t{agent_type}\t{terminal}\t{declared}")
        cls[terminal] = cls.get(terminal, 0) + 1

    if workflow_order:
        lines.append(f"WORKFLOW\tchecked={len(workflow_order)}")

    expected_no_start = 0
    # FIRST-APPEARANCE order, not alphabetical. bash iterates an awk
    # associative array here, whose order is implementation-defined —
    # observed emitting file order, which alphabetical sorting reversed for
    # a two-name ledger. Neither "unspecified" nor "alphabetical" is
    # reproducible or meaningful, so this pins the one order a reader would
    # expect from an append-only log, and the differential compares these
    # lines as a SET because bash's own order is not a contract.
    for name in expect_order:
        if start_count.get(name, 0) < expect_rows.get(name, 0):
            lines.append(f"EXPECTED_NO_START\t{name}")
            expected_no_start += 1

    lines.append(
        "ROWS\texpect={} start={} reported={} blocked={} wouldblock={}".format(
            verb_rows.get("EXPECT", 0), verb_rows.get("START", 0),
            verb_rows.get("REPORTED", 0), verb_rows.get("BLOCKED", 0),
            verb_rows.get("WOULDBLOCK", 0),
        )
    )
    lines.append(
        "CLASSIFIED\treported={} blocked_resolved={} (immediate={} later={}) "
        "blocked_unresolved={} wouldblock={} no_terminal={} undeclared={} "
        "no_start={} expected_no_start={}".format(
            cls.get("REPORTED", 0), cls.get("BLOCKED_RESOLVED", 0),
            res_immediate, res_later, cls.get("BLOCKED_UNRESOLVED", 0),
            cls.get("WOULDBLOCK", 0), cls.get("NO_TERMINAL", 0),
            undeclared, nostart, expected_no_start,
        )
    )
    lines.append(
        f"BLINDSPOT\tchecked={checked} recognized={recognized} "
        f"unrecognized={checked - recognized}"
    )

    code = 1 if (checked == 0 and verb_rows.get("EXPECT", 0) > 0) else 0

    # SPACE_*/VERIFY_* lines, RDR-205 Phase 4.1 (nexus-em75s.19): printed
    # AFTER every TSV-side line above, and NEVER folded into `code` -- same
    # "report, not verdict" rule the TSV half already established. Only
    # reached here because `rows is not None` already proved the file
    # exists and is readable, matching bash's placement (after the awk
    # call, guarded by the same early `[[ -r "$file" ]] || return 0`).
    lines.extend(_census_space(session_id, expectations_file(session_id)))
    lines.extend(_census_verify_absent(session_id))

    return LedgerReport(lines=lines, code=code)


#: The per-task keys the harness has used for a background task's identity.
#: ``background_tasks``'s shape is documented as NOT YET STABLE and possibly
#: a mixed population, so the reader tries each in order rather than
#: assuming one. Bead nexus-q02nx.6 measured the live shape (2026-09-19,
#: CLI 2.1.278): a list of dicts carrying id/type/status/description, with
#: ``command`` on a shell task and ``agent_type`` on a subagent one --
#: genuinely mixed key sets, which is why this list stays permissive.
_TASK_ID_KEYS = ("agent_id", "id", "task_id", "taskId", "subagent_id")


def _harness_task_ids(payload: str) -> list[str] | None:
    """Identities from the harness's own ``background_tasks``, or None.

    None means ABSENT -- the key is missing or not a list -- which is NOT
    the same as an empty list and must not be treated as "no tasks
    running". Absent means the harness told us nothing, so there is no
    ground truth to reconcile against and the caller returns clean. An
    empty list means the harness affirmatively reports nothing running,
    which is what makes an outstanding START stranded.

    An entry with no recognisable identity is kept as "" rather than
    dropped: it still counts toward the harness's total, and silently
    discarding it would make an unidentifiable task look like no task.
    """
    try:
        data = json.loads(payload)
    except Exception:  # noqa: BLE001 — a junk payload must never block a stop
        data = {}
    if not isinstance(data, dict):
        data = {}
    tasks = data.get("background_tasks")
    if not isinstance(tasks, list):
        return None

    scrub = str.maketrans({"\t": " ", "\n": " ", "\r": " "})
    identities: list[str] = []
    for task in tasks:
        ident = ""
        if isinstance(task, dict):
            for key in _TASK_ID_KEYS:
                value = task.get(key)
                if value:
                    ident = str(value)
                    break
        elif isinstance(task, str):
            ident = task
        identities.append(ident.translate(scrub) if ident else "")
    return identities


def expectations_reconcile(session_id: str, payload: str) -> LedgerReport:
    """Cross-check outstanding STARTs against the harness's own ground truth.

    THE GAP THIS CLOSES: every other consult surface answers "did THIS agent
    report" from the ledger alone, and none of them can see an agent that
    never fired SubagentStop at all. A hook crash, an OOM kill or a
    harness-level SIGKILL between dispatch and stop all leave a START with
    no terminal row, which from the ledger's point of view is
    indistinguishable from "still legitimately running". The harness's own
    background-task list is INDEPENDENT ground truth: a task the harness no
    longer tracks while the ledger still calls it outstanding is a silent
    death the ledger alone could never detect.

    A START whose type is exactly :data:`WORKFLOW_SUBAGENT_TYPE` (nexus-silj0)
    is pulled out of the STRANDED population, counted, and reported on its
    own ``WORKFLOW\tchecked=<n>`` line instead, matching :func:`expectations_undeclared`
    and :func:`expectations_census`.

    MEASURED AGAINST THE REAL TRANSCRIPT, not assumed (nexus-silj0
    follow-up round 2): session ``2109cc46-2876-4409-b4f1-ac730d1cc5ed``'s
    own persisted Workflow state,
    ``<project>/2109cc46-.../workflows/wf_baae5a4e-bfd.json``, carries BOTH
    identities the tool uses for this one run -- ``"runId": "wf_baae5a4e-bfd"``
    (the ``^wf_[a-z0-9-]{6,}$``-shaped id the Workflow tool's own
    ``resumeFromRunId`` takes) AND ``"taskId": "w2bole9id"`` (a SEPARATE,
    opaque id with no ``wf_`` prefix). The transcript's own
    ``<task-notification>`` for this run, ``.jsonl`` line 604 (enqueue) /
    606 (delivered), carries ``<task-id>w2bole9id</task-id>`` -- the taskId,
    never the runId -- with
    ``<summary>Dynamic workflow "..." completed</summary>``, ONE
    notification for the whole 11-agent run, not one per agent. So the
    identity the harness would put in ``background_tasks`` for a live
    Workflow run is ``w2bole9id``-shaped: an opaque id in the SAME shape as
    an ordinary background bash task (line 602's ``bhuty03r9``) or an
    ordinary background Agent-tool dispatch (line 456's
    ``aca1589669650829e``) -- **not** the ``wf_``-prefixed runId an earlier
    round of this fix wrongly assumed was the harness-visible identity
    (corrected here; the runId is purely the tool's own internal
    resume-token, invisible outside the persisted workflow-state file and
    the tool's own return value).

    Under the corrected (``w2bole9id``-shaped) identity the finding is
    unchanged in substance: that id still never equals any workflow-subagent
    START's own ``agent_id``, so the unmodified check produced STRANDED for
    every workflow-subagent still mid-flight whenever reconcile ran WHILE
    the Workflow tool call was still executing -- a perfectly healthy run
    misread as several silent deaths (exit 4, the module's own worst case),
    because the check can never tell "this specific agent died" from "the
    harness only tracks the workflow at container granularity" -- neither
    the crashed case nor the healthy one ever has its own ``agent_id`` in
    ``harness_ids``. The check was therefore never a reliable per-agent
    liveness signal for this class to begin with, so excluding it loses no
    signal that was trustworthy.

    STILL AN OPEN GAP, left alone rather than guessed at: the container
    task's own identity still fails to match any START's ``agent_id`` and so
    still surfaces as ``UNDECLARED_TASK`` whenever the harness reports one.
    A `type` field DOES exist on real ``background_tasks`` entries --
    confirmed independently by nexus-q02nx.6 (``tests/mcp/test_hook_tools.py
    ::test_a_list_valued_field_survives_a_mixed_population``, a real
    measured ``Stop`` payload: ``{"id": "bm72q9d6v", "type": "shell", ...}``
    / ``{"id": "a1ea45d8d324ca24a", "type": "subagent", ...}``), and the
    harness's three DISTINCT notification-summary templates observed above
    ("Background command ... completed" / "Agent \"...\" finished" /
    "Dynamic workflow \"...\" completed") make a third, Workflow-specific
    ``type`` value plausible. But that 2026-09-19 measurement predates this
    session's 2026-09-21 workflow run and captured only the shell/subagent
    pair -- no source available to this repo shows the LITERAL string a
    Workflow task's ``type`` field carries, and the transcript (which never
    logs the raw hook-input JSON, only the human-rendered notification text)
    cannot supply it either. Coding a comparison against a guessed literal
    risks being silently ineffective (wrong value, so nothing changes and
    the gap looks closed when it is not) or too broad if guessed as a
    catch-all (masking a genuine undeclared background task in any session
    that also ran a workflow) -- either failure mode is worse than the
    documented, visible gap. Closing this needs one more real, captured
    ``background_tasks`` payload from a session whose Stop hook fired while
    a Workflow tool call was still outstanding -- this repo has no capture
    mechanism for that.

    Exit codes: 0 clean, 2 undeclared tasks, 4 STRANDED. **4 takes priority
    over 2** -- a silent death outranks a bookkeeping gap.
    """
    if not session_id or not payload:
        return LedgerReport()
    rows = _readable_rows(session_id)
    if rows is None:
        return LedgerReport()

    identities = _harness_task_ids(payload)
    if identities is None:
        return LedgerReport()  # ABSENT: no ground truth, nothing to reconcile

    harness_ids = {i for i in identities if i}
    harness_order = list(dict.fromkeys(i for i in identities if i))
    unidentified = sum(1 for i in identities if not i)

    order: list[str] = []
    stype: dict[str, str] = {}
    terminated: set[str] = set()
    workflow_order: list[str] = []
    workflow_seen: set[str] = set()
    for row in rows:
        verb = row[1] if len(row) > 1 else ""
        who = row[2] if len(row) > 2 else ""
        if verb == "START" and who not in stype and who not in workflow_seen:
            agent_type = row[3] if len(row) > 3 else ""
            if agent_type == WORKFLOW_SUBAGENT_TYPE:
                # Its own bucket (nexus-silj0): never checked for STRANDED,
                # since the check can't tell a healthy mid-flight instance
                # from a dead one for this class -- see the docstring.
                workflow_seen.add(who)
                workflow_order.append(who)
            else:
                stype[who] = agent_type
                order.append(who)
        elif verb in ("REPORTED", "BLOCKED", "WOULDBLOCK"):
            terminated.add(who)

    lines: list[str] = []
    outstanding = stranded = 0
    for agent_id in order:
        if agent_id in terminated:
            continue
        outstanding += 1
        if agent_id not in harness_ids:
            lines.append(f"STRANDED\t{agent_id}\t{stype[agent_id]}")
            stranded += 1

    undeclared_tasks = 0
    for ident in harness_order:  # first appearance; see census's note
        # `ident not in workflow_seen` is not a fix, only a guard against a
        # false positive when the harness DOES expose per-agent identities
        # for this class (a shape this module has never measured, but the
        # check should not fight it if it exists): a workflow-subagent's own
        # agent_id, if the harness ever reports one, is accounted for here
        # rather than misread as an undeclared task.
        if ident not in stype and ident not in workflow_seen:
            lines.append(f"UNDECLARED_TASK\t{ident}")
            undeclared_tasks += 1

    if workflow_order:
        lines.append(f"WORKFLOW\tchecked={len(workflow_order)}")

    lines.append(
        f"SUMMARY\toutstanding={outstanding} harness_tasks={len(harness_ids) + unidentified} "
        f"unidentified={unidentified} stranded={stranded} "
        f"undeclared_tasks={undeclared_tasks}"
    )

    if stranded > 0:
        return LedgerReport(lines=lines, code=4)
    if undeclared_tasks > 0:
        return LedgerReport(lines=lines, code=2)
    return LedgerReport(lines=lines, code=0)
