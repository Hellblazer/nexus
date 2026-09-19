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

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "ExpectationsUsageError",
    "expectations_already_blocked",
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
        except OSError:  # pragma: no cover — fail open, never block a stop
            return False
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
    return [line.split("\t") for line in text.splitlines() if line]


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

    Exit codes, quoted in AGENTS.md: 0 clean, 1 BLINDSPOT, 2 undeclared>0,
    3 no ledger. **3 is not a pass** -- absence of a ledger is not evidence
    of cleanliness, which is why it carries a note naming the two
    explanations and how to tell them apart.
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

    for row in rows:
        verb = row[1] if len(row) > 1 else ""
        if verb == "START" and len(row) > 3:
            agent_id = row[2]
            if agent_id not in stype:
                order.append(agent_id)
                stype[agent_id] = row[3]
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

    rows = _readable_rows(session_id)
    if rows is None:
        return OwesVerdict(False)
    file = expectations_file(session_id)
    enc = _type_enc(agent_type)
    lockdir = f"{file}.owes.{enc}.lock"

    held = _acquire_owes_lock(lockdir)
    if not held:
        return OwesVerdict(True, "lock-exhausted")

    try:
        verdict, credit, spent, owners = _read_type_credit(rows, agent_type, agent_id)
        if verdict == "self":
            # A CONSUMED row already names this agent: it is re-entering
            # after a crash between its claim and its stop. Still owes, and
            # must not consume a second unit.
            return OwesVerdict(True)
        if verdict != "new":
            return OwesVerdict(False)

        if _claim_credit(file, enc, agent_id, credit, spent, owners):
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
            return True  # fail open: an unusable lock must not block a stop
    return False


def expectations_census(session_id: str) -> LedgerReport:
    """The scripted retro census (nexus-hybv1) -- never hand-count.

    One ``AGENT`` line per agent that appears anywhere as a START or a
    terminal, in first-appearance order, carrying its type, its terminal
    classification and whether its dispatch was declared. Then
    ``EXPECTED_NO_START`` for every declared name with fewer STARTs than
    EXPECT rows, a ``ROWS`` tally, a ``CLASSIFIED`` tally and a
    ``BLINDSPOT`` line.

    Exit codes are 0 and 1 ONLY -- never 2. That vocabulary belongs to
    ``undeclared`` alone, and conflating them is how a census gets read as
    an audit. 1 means the walk examined nothing while the ledger declared
    dispatches.

    A terminal is classified rather than merely recorded, because BLOCKED
    followed by REPORTED is the success path of the whole guard: the agent
    was stopped, told why, and came back with its report. That is
    ``BLOCKED_RESOLVED``, and it is counted separately by whether the
    report arrived immediately or later.
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
    expect_rows: dict[str, int] = {}
    credit: dict[str, int] = {}
    start_count: dict[str, int] = {}
    stype: dict[str, str] = {}
    term: dict[str, str] = {}
    order: list[str] = []
    listed: set[str] = set()
    all_start: set[str] = set()
    res_immediate = res_later = 0

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
            expect_names.add(who)
            expect_rows[who] = expect_rows.get(who, 0) + 1
            credit[who] = credit.get(who, 0) + 1
        elif verb == "START":
            if who not in all_start:
                all_start.add(who)
                stype[who] = row[3] if len(row) > 3 else ""
                start_count[stype[who]] = start_count.get(stype[who], 0) + 1
                if who not in listed:
                    order.append(who)
                    listed.add(who)
        elif verb in ("REPORTED", "BLOCKED", "WOULDBLOCK"):
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

    expected_no_start = 0
    for name in sorted(expect_names):
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
    unidentified = sum(1 for i in identities if not i)

    order: list[str] = []
    stype: dict[str, str] = {}
    terminated: set[str] = set()
    for row in rows:
        verb = row[1] if len(row) > 1 else ""
        who = row[2] if len(row) > 2 else ""
        if verb == "START" and who not in stype:
            stype[who] = row[3] if len(row) > 3 else ""
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
    for ident in sorted(harness_ids):
        if ident not in stype:
            lines.append(f"UNDECLARED_TASK\t{ident}")
            undeclared_tasks += 1

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
