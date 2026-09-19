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

import os
import re
import time
from pathlib import Path

__all__ = [
    "ExpectationsUsageError",
    "expectations_expect",
    "expectations_file",
    "expectations_start",
]

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
