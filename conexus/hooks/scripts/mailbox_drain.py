#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""UserPromptSubmit hook: drain this session's RDR-205 mailboxes and inject
what it finds (bead nexus-6konb.7, MM-2.2; design bead nexus-73vnw).

THE CONSUMER OF RECORD. Epic nexus-6konb delivers mailbox push in two
disjoint halves and this is the deterministic one. ``nx tuple watch``
(Phase 1) PINGS: it probes with a zero-timeout ``rd``, emits one line
naming the address, sender and tuple id, carries no body, and NEVER
claims. This hook CLAIMS, ACKS and RENDERS. They are never two renderers
of one row -- the watcher tells a session that mail exists, this hook is
what delivers and consumes it.

That split is what makes a lost ping harmless for live mail: the row sits
in the mailbox for its full retention window and this hook drains it at
the receiver's next prompt whether or not any watcher was ever armed.
Arming a Monitor is a request a model can decline or forget; this hook
fires on every prompt. So the watcher buys LATENCY and this hook is the
FLOOR.

WHERE THE FLOOR DOES NOT REACH, stated here because a reader of the
paragraph above would otherwise assume it is universal:

* A DEAD-LETTERED row is unclaimable by construction, so claim-and-ack
  cannot be its dedup and this hook cannot consume it. It is still
  surfaced ONCE, from a local seen-file, because the alternative is that
  a session with no watcher armed never learns the message existed at
  all. Purging it is a human act; this hook only says it is there.
* An INSTANCE-NAME address (the ``ListAgents`` row, e.g. ``nexus-19``) is
  drained only once something has REGISTERED it, because it exists in no
  environment variable anywhere -- MM-1.3 established that, which is why
  ``nx tuple watch`` takes it as an explicit ``--instance`` literal. The
  registry is PER-SESSION (nexus-6konb.9 defect fix, corrected from an
  earlier machine-wide design): ``nx tuple watch --instance NAME``
  writes ``<config>/tuple-watch/addresses.d/<session id>`` -- one address
  per line, keyed to the exact session that armed it, at spawn, from its
  own environment. This hook reads ONLY the file named by ITS OWN payload
  session id, never any other session's file and never a machine-wide
  one: the earlier design read a single shared ``<config>/tuple-
  watch/addresses`` file for every session, so on a box running more than
  one session the first one to prompt after arming claimed every other
  session's instance-addressed mail too. A missing per-session file is an
  empty registry, never a failure. Until a session's own file exists,
  mail sent to that instance name has no floor. The session id needs no
  registration: it arrives in this hook's own payload.

CONTRACT WITH THE PROMPT. stdout is injected context, so an empty mailbox
prints NOTHING and costs an idle prompt nothing. Every failure -- an
unresolvable endpoint, a slow engine, a malformed payload -- prints one
``SKIP`` line on stderr and exits 0. This hook must never block a prompt
and must never turn a transport problem into injected noise.

ORDER, and why it is not rd-then-render. A row is rendered only after its
``ack`` has succeeded: ``rd`` to see what is there, then ``in`` to claim,
then ``ack`` to consume, then render. Rendering anything ``rd`` merely
saw would tell the session it received mail that a peer claimed in the
meantime, or that is still sitting claimed-but-unconsumed and will return
to the mailbox when the lease lapses. That is the same read-then-write
hazard RDR-206 Step 1 closed inside the engine, appearing here between
two HTTP calls where no transaction can close it -- so the fix is to
trust only what ``ack`` confirmed.

RE-ARM (bead nexus-6konb.19). The SessionStart arm instruction can fail to
reach a session (measured 2026-09-14: ``nx hook session-start`` ran at a
resume and its output never reached the transcript), and nothing re-armed.
So after draining, this hook checks whether a live ``nx tuple watch``
process holds this session's own mailbox lock, by the pid the watcher writes
into the lock and that pid's command line. It never takes the lock itself: a
probe holding it even briefly could make a starting watcher refuse its own
mailbox. It stays silent on the first prompt it sees for a session, when the
SessionStart instruction (if it arrived) is in front of the model, and it
consults nothing SessionStart writes, because SessionStart output is what
can be lost. From the second prompt on, with no live watcher, it prints the
wheel's arm text from ``nx hook mailbox-arm``: at most once per 10 minutes
after a delivered instruction, once per minute after a failed attempt.

Stdlib only, no ``nexus`` import, endpoint through the shared
``_endpoint_resolve`` sibling (nexus-aginu): the same constraints the
``tuple_ledger_project.py`` hook runs under, for the same reason -- a
hook runs on boxes where the client package may be mid-upgrade.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _endpoint_resolve as _ep  # noqa: E402
import _tuple_size_limits as _sz  # noqa: E402

#: Whole-call wall-clock bound per HTTP call. A prompt is waiting on this
#: hook, so the ceiling is tight: a healthy engine answers a zero-timeout
#: probe in milliseconds, and anything slower is not worth a prompt's
#: latency. Enforced on the WHOLE call rather than passed to urlopen,
#: whose own timeout is per-socket-operation and is reset by every recv
#: -- an engine trickling bytes would otherwise hold the prompt open far
#: past this (the nexus-em75s.42 finding, same fix as the sibling hook).
_CALL_TIMEOUT_S = 2.0

#: Bound on the whole drain, across every address and every row. It must
#: stay well under the harness's own hook timeout (``hooks.json``: 10 s),
#: because that timeout is a KILL: anything this process has consumed but
#: not yet written is lost with it. Checked before every HTTP call, and
#: each call's own deadline is clamped to whatever remains, so a merely
#: slow engine cannot walk past the budget one under-cap call at a time.
_TOTAL_BUDGET_S = 6.0

#: Rows fetched per probe. The engine caps a read at 300; a prompt-time
#: drain wants far less, since anything beyond a handful is a backlog the
#: reader cannot absorb in one turn anyway and the rest keeps for the
#: next prompt.
_PROBE_N = 20

#: Live rows consumed per prompt, for the same reason.
_MAX_DELIVER = 10

#: RDR-208 Phase 2 Step 3: how long a ``cleared.<session id>`` record is kept
#: around unresolved before it is pruned, matching the mailbox template's own
#: retention. Past this, the mailbox it names has expired on the engine side
#: regardless, so the record is naming rows that are already gone.
_CLEARED_RECORD_RETENTION_S = 7.0 * 24.0 * 3600.0

#: Bound on how long the pending-file lock (see :func:`_pending_lock`) waits
#: for a concurrent holder before giving up and running unlocked. Small: this
#: closes a narrow interleaving window, it is not a queueing mechanism, and a
#: prompt is waiting on the whole hook.
_PENDING_LOCK_TIMEOUT_S = 2.0

#: Per-turn re-arm (bead nexus-6konb.19). This script cannot import nexus, so
#: it spells two wheel facts itself, each pinned against the wheel by
#: tests/hooks/test_mailbox_drain_hook.py: the watcher's lock name
#: (nexus.tuple_watch.lock_path) and the command a live watcher runs
#: (nexus.tuple_watch.WATCH_COMMAND_MARK). Drift in either costs at most a
#: wasted spawn, never a wrong instruction: ``nx hook mailbox-arm`` re-checks
#: liveness through the wheel's own lock path before it prints anything.
_LOCK_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_LOCK_PID = re.compile(r"\bpid=(\d+)")
_WATCH_COMMAND_MARK = "tuple watch"

#: This hook's own record per session, ``tuple-watch/rearm.<session id>``:
#: when it last delivered an arm instruction and when it last tried.
_REARM_STATE_PREFIX = "rearm."
#: Spacing after a delivered instruction. Bounds a session that never arms
#: to one reminder per interval.
_REARM_INTERVAL_S = 600.0
#: Spacing after a failed attempt (no nx, nx failed, nx timed out). Short,
#: because the failure this path exists for is a transient one.
_REARM_RETRY_S = 60.0

#: The whole hook stays under the harness's 10 s kill. The re-arm spawns only
#: into what the drain left of this ceiling, and not at all below the minimum.
_HOOK_CEILING_S = 9.0
_REARM_MIN_S = 1.5
_REARM_SPAWN_CAP_S = 5.0

_TENANT = _ep.DEFAULT_TENANT
_SAFE_ADDRESS_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-."
)


class _Skip(Exception):
    """Any resolution or transport failure. Caught once in main(), printed as
    one SKIP line on stderr, exit 0. A hook that cannot reach the engine is
    not an error the prompt should ever see."""


def _log_skip(reason: str) -> None:
    print(f"[mailbox-drain] SKIP: {reason}", file=sys.stderr)  # noqa: T201 — stderr is this hook's only diagnostic surface


def _valid_address(address: str) -> bool:
    """Path-safe and subspace-safe. An address becomes both a subspace name on
    the wire and a filename in the seen-store, so a traversal-bearing or
    otherwise odd one is dropped rather than sanitised -- silently repairing a
    bad address would drain the wrong mailbox."""
    return bool(address) and len(address) <= 128 and all(
        c in _SAFE_ADDRESS_CHARS for c in address
    )


def _config_dir() -> Path:
    return _ep.default_config_dir()


def _session_registry_path(config_dir: Path, session_id: str) -> Path:
    return config_dir / "tuple-watch" / "addresses.d" / session_id


def _seen_path(config_dir: Path, address: str) -> Path:
    return config_dir / "tuple-watch" / f"{address}.drained.json"


def _read_session_registry(config_dir: Path, session_id: str) -> list[str]:
    """The instance address(es) THIS session registered for itself via
    ``nx tuple watch --instance NAME`` (nexus-6konb.9 defect fix), one per
    line. Blank lines and ``#`` comments are ignored; anything unsafe is
    dropped. Keyed strictly to *session_id* -- never machine-wide -- so
    one session can never drain another session's instance-named mailbox.
    A missing or unreadable file is simply an empty registry -- never a
    failure, since the session-id address does not depend on it."""
    try:
        raw = _session_registry_path(config_dir, session_id).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []
    out: list[str] = []
    for line in raw.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if _valid_address(entry):
            out.append(entry)
    return out


def _cleared_record_path(config_dir: Path, session_id: str) -> Path:
    """``<config>/tuple-watch/cleared.<session_id>``, matching
    ``nexus.tuple_watch.cleared_record_path`` -- pinned against drift by
    :func:`test_rearm_naming_matches_the_wheel`'s sibling in the test module.
    *session_id* here is the reading session's OWN id: the record this hook
    reads was written FOR it, naming the mailbox(es) its own ``/clear``
    stranded (RDR-208 Phase 2 Step 3).
    """
    return config_dir / "tuple-watch" / f"cleared.{session_id}"


def _read_cleared_record(config_dir: Path, session_id: str) -> list[str]:
    """The mailbox(es) THIS session's ``/clear`` stranded, one per line, in
    the order :func:`nexus.tuple_watch.record_clear_and_write_session_marker`
    wrote them (the immediately-previous session first, then any chained
    further back). Blank lines and ``#`` comments are ignored. A malformed
    entry is dropped AND logged -- unlike the silent drop in
    :func:`_read_session_registry` -- because an operator-visible mailbox id
    landing in this record and failing validation is itself worth knowing
    about, not routine noise. A missing or unreadable file is simply no
    record, never a failure: most sessions never ``/clear``.
    """
    path = _cleared_record_path(config_dir, session_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []
    out: list[str] = []
    for line in raw.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if _valid_address(entry):
            out.append(entry)
        else:
            _log_skip(f"cleared record {path.name}: skipping malformed mailbox id {entry!r}")
    return out


def _delete_cleared_record(config_dir: Path, session_id: str) -> None:
    try:
        _cleared_record_path(config_dir, session_id).unlink(missing_ok=True)
    except OSError:
        pass


def _prune_stale_cleared_records(config_dir: Path, *, now: float) -> None:
    """Delete any ``cleared.*`` record whose file is older than the mailbox
    template's own 7-day retention: the mailbox it names has expired at the
    engine regardless of whether this hook ever confirmed it empty, so the
    record is naming rows that are already gone. Runs once per invocation,
    over every record in the directory -- not scoped to the current
    session's own record -- since a record can outlive the session that
    would ever read it again (e.g. a chained clear's now-unreachable id;
    see :func:`nexus.tuple_watch.record_clear_and_write_session_marker`).
    """
    watch_dir = config_dir / "tuple-watch"
    try:
        entries = list(watch_dir.iterdir())
    except OSError:
        return
    for path in entries:
        if not path.name.startswith("cleared."):
            continue
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age > _CLEARED_RECORD_RETENTION_S:
            try:
                path.unlink()
            except OSError:
                pass


def _mailbox_confirmed_empty(base_url: str, token: str, address: str, *, is_local: bool,
                             config_dir: Path, deadline: float) -> bool:
    """True iff *address* is safe to forget: a fresh ``rd`` probe shows no
    row other than dead-lettered ones (a row under another process's lease
    still counts as live -- it may yet be delivered by whoever holds it),
    and nothing of this hook's own is still in flight for it.

    PAGINATES past the first ``_PROBE_N``-row page, the same way the
    pending-id confirmation inside :func:`_drain_address` does (nexus-
    galkv.6 fix 1, gate audit round 2): a single page is not "the mailbox,"
    only its oldest ``_PROBE_N`` rows. Twenty or more dead-lettered rows
    ahead of one live row -- under another process's lease, say -- used to
    hide that live row from a one-page check entirely, deleting the record
    while a live claim still sat unconfirmed beyond the page. Walking
    forward with the engine's own ``(created_at, id)`` cursor until a SHORT
    page (fewer than ``_PROBE_N`` rows, the engine's own "nothing else"
    signal) closes that gap; a page whose OWN rows are already
    unconfirmable (budget spent, or the follow-up call itself fails) keeps
    the record rather than guessing.
    """
    import time  # noqa: PLC0415 — deferred: only this path needs a clock

    try:
        rows = _probe_page(base_url, token, address, is_local=is_local,
                           deadline=deadline, since=None)
    except _Skip:
        return False
    if any(r.get("claim_state") != "dead" for r in rows):
        return False
    complete = len(rows) < _PROBE_N
    while not complete:
        if time.monotonic() >= deadline:
            return False
        last = rows[-1]
        since = (str(last.get("created_at")), str(last.get("id")))
        try:
            rows = _probe_page(base_url, token, address, is_local=is_local,
                               deadline=deadline, since=since)
        except _Skip:
            return False
        if not rows:
            break
        if any(r.get("claim_state") != "dead" for r in rows):
            return False
        complete = len(rows) < _PROBE_N
    return not _read_pending(config_dir, address)


def _drain_named_mailbox(base_url: str, token: str, address: str, *, is_local: bool,
                         config_dir: Path, deadline: float, out: _Out) -> bool:
    """Drain one mailbox a cleared record names, and say whether it is safe
    to forget.

    Per the audit's delete rule (RDR-208 Phase 2 Step 3), ALL of these must
    hold, not just ``delivered == 0``:

    1. the claim loop ended on an EMPTY claim, not the deadline, not
       ``_MAX_DELIVER``, not an ack refusal;
    2. a fresh ``rd`` probe afterward shows no live row (dead-lettered rows
       do not count; a row under another process's lease does);
    3. the address's pending file is empty or missing.

    Any other ending -- including an unexpected exception, treated the same
    as this hook's own per-address handling in :func:`_drain_all` -- keeps
    the record for the next prompt to try again.
    """
    try:
        ending = _drain_address(base_url, token, address, is_local=is_local,
                                config_dir=config_dir, deadline=deadline, out=out)
    except _Skip as exc:
        _log_skip(f"cleared mailbox/{address}: {exc}")
        return False
    except Exception as exc:  # noqa: BLE001 — mirrors _drain_all's own per-address handling
        _log_skip(f"cleared mailbox/{address}: unexpected {type(exc).__name__}: {exc}")
        return False
    if ending != "empty":
        return False
    return _mailbox_confirmed_empty(base_url, token, address, is_local=is_local,
                                    config_dir=config_dir, deadline=deadline)


def _drain_cleared_record(base_url: str, token: str, session_id: str, *, is_local: bool,
                          config_dir: Path, deadline: float, out: _Out) -> None:
    """Drain every mailbox this session's cleared record names, inside the
    SAME budget as the session's own mailbox, and delete the record only
    when every named mailbox came back safe to forget. A record naming more
    than one mailbox (a chained clear) is all-or-nothing: partially draining
    it and pruning only the finished names would need per-name state this
    record does not carry, and leaving the whole record for one more pass is
    cheap -- an already-empty mailbox costs one quick probe next time.
    """
    import time  # noqa: PLC0415 — deferred: only this path needs a clock

    named = _read_cleared_record(config_dir, session_id)
    if not named:
        return
    all_confirmed = True
    for address in named:
        if time.monotonic() >= deadline:
            _log_skip(
                f"drain budget of {_TOTAL_BUDGET_S}s spent before reaching the "
                f"cleared record's mailbox/{address}; the record keeps until the "
                "next prompt",
            )
            all_confirmed = False
            break
        if not _drain_named_mailbox(base_url, token, address, is_local=is_local,
                                    config_dir=config_dir, deadline=deadline, out=out):
            all_confirmed = False
    if all_confirmed:
        _delete_cleared_record(config_dir, session_id)


def _read_seen(config_dir: Path, address: str) -> set[str]:
    try:
        data = json.loads(_seen_path(config_dir, address).read_text(encoding="utf-8"))
        return {str(x) for x in data.get("dead_surfaced", [])}
    except (OSError, ValueError, AttributeError):
        return set()


def _write_seen(config_dir: Path, address: str, dead_surfaced: set[str]) -> None:
    """Best-effort. Losing this file re-surfaces a dead row once more, which is
    noise; failing the drain over it would lose live mail, which is worse."""
    path = _seen_path(config_dir, address)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"dead_surfaced": sorted(dead_surfaced)}), encoding="utf-8",
        )
        tmp.replace(path)
    except OSError:
        pass


def _pending_path(config_dir: Path, address: str) -> Path:
    return config_dir / "tuple-watch" / f"{address}.pending.json"


def _read_pending(config_dir: Path, address: str) -> list[dict[str, str]]:
    try:
        raw = _pending_path(config_dir, address).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    out = []
    for e in entries:
        if isinstance(e, dict) and e.get("id") and e.get("rendered"):
            out.append({"id": str(e["id"]), "rendered": str(e["rendered"])})
    return out


def _save_pending(config_dir: Path, address: str, entries: list[dict[str, str]]) -> None:
    """Best-effort: failing to write this must never stop a drain."""
    path = _pending_path(config_dir, address)
    try:
        if not entries:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"entries": entries}), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


@contextlib.contextmanager
def _pending_lock(config_dir: Path, address: str):
    """Exclusive advisory lock over one address's ENTIRE pending-file
    handling for one drain pass (nexus-galkv.6 fix 3, gate audit round 2 --
    tightening the original per-call version this replaces).

    Yields ``True`` when the lock was acquired, ``False`` otherwise. FAILS
    CLOSED: the original version ran its caller UNLOCKED after a timeout,
    which the audit found could duplicate a delivery -- not merely leave a
    redundant or recovered pending entry, the risk the original docstring
    named. A caller that gets ``False`` must not touch the network for this
    address at all this pass; see :func:`_drain_address`, the sole caller,
    which wraps its ENTIRE body in this lock (not just each pending-file
    call) so "do not claim from that mailbox this pass" holds from the very
    first probe, not only from whichever pending-file call happens to run
    into the held lock.

    Two processes draining the SAME address concurrently used to be a rare
    edge case (two terminals resuming one session id); RDR-208 Phase 2 Step 3
    makes it the ordinary case for a ``/clear``'s stranded mailbox, since the
    new session's own drain and any still-live process holding the old
    session id both drain that address now.
    """
    path = config_dir / "tuple-watch" / f"{address}.pending.lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        yield False
        return
    locked = False
    deadline = time.monotonic() + _PENDING_LOCK_TIMEOUT_S
    try:
        while time.monotonic() < deadline:
            try:
                if sys.platform == "win32":
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                time.sleep(0.02)
        yield locked
    finally:
        if locked:
            try:
                if sys.platform == "win32":
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def _write_pending(config_dir: Path, address: str, tuple_id: str, rendered: str) -> None:
    """Record a claimed-but-not-yet-acked row, so its delivery survives a lost
    ack RESPONSE.

    A LIST keyed by tuple id, not a single record: a drain consumes several rows
    per prompt, so a single slot would let row B's record overwrite row A's while
    A was still unresolved, losing exactly the trace this file exists to keep.

    Called only from inside :func:`_drain_address`'s own ``_pending_lock``
    hold (nexus-galkv.6 fix 3): this function itself no longer takes the
    lock, since re-acquiring it per call left a window between calls for
    another process to interleave.
    """
    entries = [e for e in _read_pending(config_dir, address) if e["id"] != tuple_id]
    entries.append({"id": tuple_id, "rendered": rendered})
    _save_pending(config_dir, address, entries)


def _clear_pending(config_dir: Path, address: str, tuple_id: str) -> None:
    """See :func:`_write_pending`'s docstring: locked by the caller, not here."""
    entries = [e for e in _read_pending(config_dir, address) if e["id"] != tuple_id]
    _save_pending(config_dir, address, entries)


def _recover_pending(config_dir: Path, address: str, present_ids: set[str],
                     *, confirmed_complete: bool, out: _Out) -> None:
    """Deliver rows this hook consumed on an earlier prompt but never showed.

    The window is narrow and the consequence is total: ``ack`` reaches the
    engine, the engine consumes the row, and the response is lost. The client
    never learns the ack succeeded, so without this the row is gone from the
    mailbox and was never shown to anyone -- silent, permanent loss of a
    delivered message, the failure class this whole epic exists to prevent.

    Presence in the mailbox distinguishes the two cases:

    * the id is ABSENT -- the ack landed, the row is consumed, its delivery was
      lost. Deliver it now, and drop the record.
    * the id is PRESENT -- the ack never landed, so the row is still there and
      the normal path can deliver it. The record is KEPT, not cleared. Clearing
      here was a defect: it assumed this same drain would reach the live-claim
      loop for that row, and a drain whose budget runs out first would leave
      nothing anywhere to notice if the original ambiguous ack later landed at
      the engine on its own schedule. Keeping the record costs a redundant entry
      that the normal path clears on delivery; clearing it early costs the
      message.

    ABSENCE FROM ``present_ids`` IS ONLY MEANINGFUL WHEN ``confirmed_complete``
    IS TRUE (nexus-1kvk3). The engine's ``rd`` orders by created_at ascending,
    never excludes claimed or dead-lettered rows, and this hook only ever
    fetches a bounded number per call -- so on a backlog deeper than that
    bound, "not in the page(s) fetched so far" does not mean "not in the
    mailbox". Guessing absence there used to misread a row still sitting
    claimed-but-unresolved as an ack that landed, delivering (or dropping) it
    for the wrong reason. When the caller could not walk far enough to be
    sure, an unresolved id is treated exactly like a PRESENT one: kept, never
    guessed away.

    Called only from inside :func:`_drain_address`'s own ``_pending_lock``
    hold (nexus-galkv.6 fix 3): see :func:`_write_pending`'s docstring.
    """
    entries = _read_pending(config_dir, address)
    if not entries:
        return
    keep: list[dict[str, str]] = []
    for entry in entries:
        if entry["id"] in present_ids or not confirmed_complete:
            keep.append(entry)   # still there, or its absence is unconfirmed
        else:
            out.block(entry["rendered"])
    _save_pending(config_dir, address, keep)


def _post(base_url: str, token: str, route: str, body: dict[str, Any],
          *, is_local: bool, budget_s: float | None = None) -> dict[str, Any] | None:
    """POST and return the decoded body, or None on a 404. Raises :class:`_Skip`
    on a transport failure OR on any other non-2xx. Bounds the whole call, not
    each socket operation.

    ONLY 404 IS A CONFIRMED NEGATIVE, and the distinction is the difference
    between a duplicate and a lost message. Callers read ``None`` as "the engine
    answered and said no": on ``ack`` that clears the pending-ack safety record,
    and on ``rd`` it means the mailbox is empty. A 404 earns that reading -- it
    is ClaimNotFound, the engine's considered answer. A 500, 502, 401, 403 or 429
    does not. The engine may have COMMITTED the mutation and failed on the way
    out (``TupleHandler`` lets an unexpected exception fall through to a bare
    500), so treating it as a clean negative drops the only trace of a row that
    is already consumed, and the message is gone with nobody having seen it.
    Raising ``_Skip`` instead keeps the record and lets the next prompt recover.

    This also restores the pattern the sibling hook ``tuple_ledger_project.py``
    already follows -- it captures ``exc.code`` and refuses anything outside
    2xx -- which this hook's own docstring claims to share.
    """
    payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
    url = f"{base_url}{route}"
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    # Never spend longer than the drain has left: a sequence of calls each
    # just under the per-call cap would otherwise blow the whole budget while
    # every individual call looked fine.
    call_timeout = _CALL_TIMEOUT_S
    if budget_s is not None:
        call_timeout = max(0.1, min(_CALL_TIMEOUT_S, budget_s))
    outcome: dict[str, Any] = {}

    def _do() -> None:
        try:
            handlers: list[urllib.request.BaseHandler] = []
            if is_local:
                handlers.append(urllib.request.ProxyHandler({}))
            opener = urllib.request.build_opener(*handlers)
            with opener.open(req, timeout=call_timeout) as resp:  # noqa: S310 — fixed engine URL
                outcome["body"] = json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                outcome["body"] = None      # ClaimNotFound: a confirmed negative
            else:
                outcome["status"] = exc.code
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=_do, daemon=True)
    thread.start()
    thread.join(timeout=call_timeout)
    if thread.is_alive():
        raise _Skip(
            f"{route} exceeded its {call_timeout:.1f}s deadline; the prompt is not waiting for it",
        )
    if "error" in outcome:
        raise _Skip(f"transport failure on {route}: {outcome['error']}")
    if "status" in outcome:
        raise _Skip(
            f"engine returned HTTP {outcome['status']} on {route}; the outcome is "
            f"UNKNOWN, so nothing is treated as confirmed and any pending record is kept",
        )
    return outcome.get("body")


class _Out:
    """Writes each delivered block to stdout IMMEDIATELY, flushing every time.

    Buffering blocks in memory and printing once at the end is what made an
    already-consumed row losable: a transport failure on a LATER row discarded
    the whole list, and the harness's own hook timeout (hooks.json: 10 s) is a
    KILL, so anything consumed-but-unprinted died with the process. Streaming
    makes "acked implies delivered" hold even under a mid-drain SIGKILL, for
    every row already acked at the moment of the kill.
    """

    def __init__(self) -> None:
        self._opened = False

    def block(self, text: str) -> None:
        if not self._opened:
            # stdout IS the product here: a UserPromptSubmit hook's stdout
            # becomes the injected context. These are not diagnostics.
            print("## Mailbox (RDR-205): delivered at this prompt\n", flush=True)  # noqa: T201
            self._opened = True
        print(text, flush=True)  # noqa: T201


def _dims_of(row: dict[str, Any]) -> dict[str, Any]:
    """A row's dims, or an empty mapping when the engine sent something else.

    ``row.get("dims") or {}`` looks like it covers this and does not: it rescues
    None and {}, but a non-dict TRUTHY value (a string, a list) sails through and
    the next ``.get`` raises AttributeError. One such row used to poison its own
    address permanently — the per-address guard caught the crash, so the process
    and every other mailbox survived, but that address never drained again and
    nothing queued behind the bad row was ever delivered. Found by the
    test-validator at the nexus-6konb.8 close gate.

    Degrading to "?" for one malformed row is right where refusing is not: this
    hook is the CONSUMER OF RECORD and the floor under delivery, so a row it
    cannot pretty-print must still not stop the mail behind it.
    """
    dims = row.get("dims")
    return dims if isinstance(dims, dict) else {}


def _render_live(address: str, row: dict[str, Any]) -> str:
    dims = _dims_of(row)
    sender = dims.get("from", "?")
    kind = dims.get("kind") or "note"
    corr = dims.get("correlation_id") or "-"
    body = row.get("body")
    body_text = body if body else "(no body)"
    return (
        f"- from={sender} kind={kind} correlation_id={corr} "
        f"address=mailbox/{address} tuple_id={row.get('id', '?')}\n"
        f"  {body_text}"
    )


def _render_dead(address: str, row: dict[str, Any]) -> str:
    dims = _dims_of(row)
    return (
        f"- UNDELIVERABLE at mailbox/{address} tuple_id={row.get('id', '?')} "
        f"from={dims.get('from', '?')} attempts={row.get('attempts', '?')}: this row is "
        f"dead-lettered and can never be claimed, so nothing will deliver it. "
        f"Reported once. Purge it with `nx tuple stats mailbox/{address}` and the "
        f"engine's sweep, or ask the sender to resend."
    )


def _probe_page(base_url: str, token: str, address: str, *, is_local: bool,
                deadline: float, since: tuple[str, str] | None) -> list[dict[str, Any]]:
    """One page of ``rd`` on *address*, optionally continuing past *since*.

    *since* is the engine's own ``(created_at, id)`` cursor
    (``nexus.db.t2.http_tuple_store.rd``'s wire contract: ``{"created_at":
    ..., "id": ...}``), so a follow-up page picks up strictly after the
    previous page's last row instead of reading the same head of the address
    again.
    """
    import time  # noqa: PLC0415 — deferred: only this path needs a clock

    body: dict[str, Any] = {
        "subspace": f"mailbox/{address}",
        "keys_pattern": {"to": address},
        "n": _PROBE_N,
    }
    if since is not None:
        body["since"] = {"created_at": since[0], "id": since[1]}
    probe = _post(base_url, token, "/v1/tuples/rd", body, is_local=is_local,
                  budget_s=deadline - time.monotonic())
    return (probe or {}).get("tuples") or []


def _drain_claimant(address: str) -> str:
    """A claimant unique to THIS drain invocation (nexus-galkv.6 fix 2, gate
    audit round 2).

    The previous form, ``f"mailbox-drain-{address}"``, was derived from the
    address alone, so every process draining the same address presented the
    IDENTICAL claimant string. RDR-208 Phase 2 Step 3 makes two processes
    draining one address the ordinary case (a cleared-record drain and a
    still-live session's own drain can both reach it), and the engine's
    same-claimant retake (``TupleRepository.claimOnce``,
    ``service/src/main/java/dev/nexus/service/db/TupleRepository.java:694-
    706``) hands back the SAME claim to whoever presents a MATCHING claimant
    against an already-claimed, still-leased row -- correct for one
    process's own retry of an in-flight claim, wrong between two independent
    processes that happen to share a name. Including the pid and a random
    suffix makes that collision effectively impossible.
    """
    return f"mailbox-drain-{address}-{os.getpid()}-{secrets.token_hex(4)}"


def _drain_address(base_url: str, token: str, address: str, *, is_local: bool,
                   config_dir: Path, deadline: float, out: _Out) -> str:
    """Probe one address and deliver what it can, writing each row as it goes.

    Returns how the claim loop ENDED, one of:

    * ``"empty"`` -- either nothing was ever seen on the address (an empty
      probe, before any claim was attempted), or a claim attempt itself came
      back empty (a peer got there first, or the address genuinely has
      nothing left to claim). RDR-208 Phase 2 Step 3's cleared-record drain
      treats this as the one ending that MAY warrant forgetting the record,
      and only after its own confirming checks (see
      :func:`_drain_named_mailbox`) -- ``delivered == 0`` alone is not this
      signal, since it is also produced by ``"budget"``, ``"cap"`` and
      ``"ack_refused"`` below.
    * ``"budget"`` -- the drain's deadline was reached before a claim
      attempt (this prompt's own budget, not this address's).
    * ``"cap"`` -- ``_MAX_DELIVER`` was reached without ever seeing an empty
      claim; there may be more still on the address.
    * ``"ack_refused"`` -- a claim was made but its ``ack`` came back a
      confirmed negative (404 ClaimNotFound); the claimed row's lease lapses
      and it returns to the mailbox for a later drain to pick up.
    * ``"skipped"`` -- refused before any POST: an oversized address, OR
      (nexus-galkv.6 fix 3) this address's pending-file lock was held by
      another drain pass past ``_PENDING_LOCK_TIMEOUT_S`` -- FAIL CLOSED,
      never run unlocked; nothing is claimed from the address this pass.

    Every delivered row has already been written by the time this returns,
    so a failure part-way through cannot retract an earlier one. A
    :class:`_Skip` still propagates -- the caller stops the drain -- but what
    was already delivered stays delivered.
    """
    import time  # noqa: PLC0415 — deferred: only this path needs a clock

    # Size pre-check (bead nexus-r7xao): mirrors the engine's own per-field
    # caps for every field this hook itself constructs from *address* --
    # subspace, the "to" pattern value, and the claimant string below. An
    # oversized address is refused here, before any POST, the same as every
    # other precondition this hook checks before touching the network.
    claimant = _drain_claimant(address)
    size_reason = (
        _sz.check_field_size("subspace", f"mailbox/{address}", _sz.MAX_SUBSPACE_BYTES)
        or _sz.check_field_size("keys_pattern.to", address, _sz.MAX_FIELD_VALUE_BYTES)
        or _sz.check_field_size("claimant", claimant, _sz.MAX_CLAIMANT_BYTES)
    )
    if size_reason is not None:
        _log_skip(f"mailbox/{address}: oversized address, refused before any POST: {size_reason}")
        return "skipped"

    # nexus-galkv.6 fix 3: the WHOLE pass, from the first probe onward, runs
    # under this address's pending-file lock -- not just each individual
    # _write_pending/_clear_pending/_recover_pending call. A lock that could
    # not be taken FAILS CLOSED: nothing is claimed from the address this
    # pass, so a later, unlocked interleaving with whoever holds the lock is
    # never possible in the first place.
    with _pending_lock(config_dir, address) as acquired:
        if not acquired:
            _log_skip(
                f"mailbox/{address}: pending-file lock held by another drain "
                f"pass past {_PENDING_LOCK_TIMEOUT_S}s; not claiming from it "
                "this pass",
            )
            return "skipped"

        rows = _probe_page(base_url, token, address, is_local=is_local,
                           deadline=deadline, since=None)
        present_ids = {str(r.get("id")) for r in rows}
        dead_rows = [r for r in rows if r.get("claim_state") == "dead"]
        saw_any_row = bool(rows)
        # A full page (exactly _PROBE_N rows) means there might be more behind
        # it; anything shorter is the engine's own confirmation there is
        # nothing else.
        complete = len(rows) < _PROBE_N

        # PAGINATE PAST THE PROBE CEILING (nexus-1kvk3). ``rd`` orders by
        # created_at ascending and never excludes claimed or dead-lettered
        # rows (dead rows are purged only by a human, per this hook's own
        # contract above), so a backlog deeper than one page can rank a
        # pending row's presence check wrong: "not on the page(s) read so
        # far" is not "not in the mailbox". Walk forward with the engine's
        # own cursor ONLY as far as there is a pending id still unresolved
        # and the address might hold more than what has been read -- an
        # ordinary drain with no pending entries, or whose entries already
        # resolved on the first page, pays nothing for this loop at all.
        pending_ids = {e["id"] for e in _read_pending(config_dir, address)}
        unresolved = pending_ids - present_ids
        while unresolved and not complete:
            if time.monotonic() >= deadline:
                _log_skip(
                    f"mailbox/{address}: drain budget spent confirming "
                    f"{len(unresolved)} pending id(s) against a backlog deeper "
                    f"than {_PROBE_N} rows; kept for the next prompt rather "
                    "than guessed",
                )
                break
            last = rows[-1]
            since = (str(last.get("created_at")), str(last.get("id")))
            rows = _probe_page(base_url, token, address, is_local=is_local,
                               deadline=deadline, since=since)
            if not rows:
                complete = True
                break
            saw_any_row = True
            present_ids |= {str(r.get("id")) for r in rows}
            dead_rows.extend(r for r in rows if r.get("claim_state") == "dead")
            unresolved = pending_ids - present_ids
            if len(rows) < _PROBE_N:
                complete = True

        # A row this hook consumed on an earlier prompt but never managed to
        # deliver: the ack reached the engine and its RESPONSE did not, so
        # the row is gone from the mailbox and nothing else will ever show
        # it. Recover it here, before anything else, since it is already
        # lost from the engine's point of view -- ``complete`` says whether
        # that "gone" conclusion is actually confirmed, or just where this
        # probe's budget ran out.
        _recover_pending(config_dir, address, present_ids, confirmed_complete=complete, out=out)

        if dead_rows:
            seen = _read_seen(config_dir, address)
            fresh = [r for r in dead_rows if str(r.get("id")) not in seen]
            for row in fresh:
                out.block(_render_dead(address, row))
                seen.add(str(row.get("id")))
            if fresh:
                # Keep only ids still present, so the file cannot grow forever.
                present = {str(r.get("id")) for r in dead_rows}
                _write_seen(config_dir, address, seen & present)

        if not saw_any_row:
            return "empty"

        # LIVE DELIVERY. Deliberately NOT gated on a live-row count read off a
        # probe page: ``/v1/tuples/in`` claims the address's own oldest
        # unclaimed live row directly at the engine, unbounded by whatever
        # ``rd`` page this hook happened to read. A genuinely live row ranked
        # behind more dead or claimed rows than a page holds -- the
        # starvation nexus-1kvk3 names -- is still reachable this way; the
        # probe above only had to see it for dead-row surfacing and
        # pending-id resolution, never as a precondition for attempting a
        # claim. (The sibling starvation in the ``rd``-only watcher,
        # nexus-qw386, has no such escape hatch and needs its own
        # cursor-based fix.) Attempted up to _MAX_DELIVER times and stopped
        # the moment a claim comes back empty -- the engine's own
        # confirmation that nothing more is available, whether because a
        # peer got there first or the address is now empty.
        delivered = 0
        while delivered < _MAX_DELIVER:
            if time.monotonic() >= deadline:
                return "budget"
            claim = _post(base_url, token, "/v1/tuples/in", {
                "subspace": f"mailbox/{address}",
                "keys_pattern": {"to": address},
                "claimant": claimant,
                "lease_s": 30,
            }, is_local=is_local, budget_s=deadline - time.monotonic())
            if not claim or not claim.get("claim_id"):
                return "empty"  # a peer took it between rd and in, or the queue emptied
            row = claim.get("tuple") or {}
            row_id = str(row.get("id"))
            rendered = _render_live(address, row)
            # Recorded BEFORE the ack, so that an ack whose response is lost --
            # the engine consumed the row, the client never learned it -- leaves
            # a trace the next prompt can recover from. Without this the row is
            # gone from the engine and was never shown to anyone.
            _write_pending(config_dir, address, row_id, rendered)
            acked = _post(base_url, token, "/v1/tuples/ack", {
                "claim_id": claim["claim_id"],
                "claimant": claimant,
            }, is_local=is_local, budget_s=deadline - time.monotonic())
            if acked is None:
                # A clean refusal: the engine answered and said no. The lease
                # lapses and the row returns to the mailbox, so the normal path
                # will deliver it and this record would be a duplicate. Dropped
                # by id, so no other row's record is disturbed.
                _clear_pending(config_dir, address, row_id)
                return "ack_refused"
            out.block(rendered)
            _clear_pending(config_dir, address, row_id)
            delivered += 1
        return "cap"


def _resolve_endpoint(config_dir: Path) -> tuple[str, str, bool]:
    """Resolve ``(base_url, token, is_local_supervisor)``.

    CREDENTIAL POLICY, and why it is not the sibling ledger hook's. That hook
    (``tuple_ledger_project.py``) is a fire-and-forget write with no reader and
    no retry, so it deliberately refuses anything but a fresh tenant-scoped
    data-token lease. This hook is a SYNCHRONOUS call with a prompt waiting on
    it, the same shape as ``t2_prefix_scan.py`` and ``routing/_lib.py``, so it
    takes the same looser last resort those two take: a static ``service_token``
    from env or the persisted ``config.yml``. Refusing that would make the drain
    silently inert on a managed box onboarded with ``nx config set
    service_token`` and nothing else, which is a real, documented path -- the
    floor would not exist exactly where a user had done everything right.

    A fresh data-token lease for the resolved host still WINS over the static
    token wherever one exists, mirroring the real client's
    ``DataTokenManager.bearer_for``. Base-URL precedence comes from the shared
    module (nexus-aginu) rather than being re-derived here, since that
    precedence is what drifted between three hand-maintained copies before it
    was factored out.
    """
    try:
        base_url, is_local = _ep.resolve_base_url(config_dir)
    except _ep.EndpointUnresolvable as exc:
        raise _Skip(str(exc)) from exc

    data_token = _ep.read_data_token_lease(config_dir, base_url)
    if data_token:
        return base_url, data_token, is_local

    import os  # noqa: PLC0415 — deferred: only this path reads the environment

    token = os.environ.get("NX_SERVICE_TOKEN", "").strip()
    if not token:
        token = (_ep.read_config_yml_credentials(config_dir) or {}).get(
            "service_token", "",
        ).strip()
    if not token:
        # Through the module's own accessor, never off the raw lease dict: it
        # refuses a lease file that is not owner-only, because the token it
        # carries authorizes real engine writes and a group- or world-readable
        # lease means another local account could have read it too. Reading the
        # dict directly would silently skip that audit.
        try:
            token = _ep.read_local_supervisor_token(config_dir).strip()
        except _ep.EndpointUnresolvable:
            token = ""
    if not token:
        raise _Skip(
            f"resolved {base_url} but found no credential: no data-token lease, no "
            "NX_SERVICE_TOKEN, no persisted service_token, no local supervisor lease",
        )
    return base_url, token, is_local


def _watch_lock_path(config_dir: Path, session_id: str) -> Path:
    return config_dir / "tuple-watch" / (_LOCK_UNSAFE.sub("_", session_id) + ".lock")


def _rearm_state_path(config_dir: Path, session_id: str) -> Path:
    return config_dir / "tuple-watch" / f"{_REARM_STATE_PREFIX}{session_id}"


def _lock_pid(body: str) -> int | None:
    match = _LOCK_PID.search(body)
    return int(match.group(1)) if match else None


def _pid_is_watcher(pid: int | None) -> bool:
    """A live process running ``nx tuple watch``. The lock file outlives its
    watcher, so a dead pid is no watcher, and a live pid running anything
    else is a reused pid, also no watcher."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        pass
    except OSError:
        return False
    try:
        proc = subprocess.run(  # noqa: S603 S607 — fixed argv; ps resolved on PATH
            ["ps", "-p", str(pid), "-o", "command="],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return True  # cannot inspect it: trust the live pid rather than nag
    return _WATCH_COMMAND_MARK in proc.stdout


def _watcher_live(config_dir: Path, session_id: str) -> bool:
    try:
        body = _watch_lock_path(config_dir, session_id).read_text(encoding="utf-8")
    except OSError:
        return False
    return _pid_is_watcher(_lock_pid(body))


def _read_rearm_state(path: Path) -> dict[str, float] | None:
    """``None`` when this hook has never run for the session. An unreadable
    or malformed record reads as present and empty, so it can only make a
    reminder due, never suppress one."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    state: dict[str, float] = {}
    for key in ("last_rearm", "last_attempt"):
        try:
            state[key] = float(data.get(key, 0.0))
        except (TypeError, ValueError):
            state[key] = 0.0
    return state


def _write_rearm_state(path: Path, state: dict[str, float]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _since(now: float, then: float) -> float:
    """Seconds from *then* to *now*; a clock that went backwards reads as
    long ago, so it can only make a reminder due."""
    delta = now - then
    return delta if delta >= 0 else float("inf")


def _rearm_if_unwatched(config_dir: Path, session_id: str, *, started: float) -> None:
    """Re-issue the arm instruction when this session has no live watcher.

    Silent on the first prompt this hook sees for a session. The interval
    starts only once an instruction was actually printed; a failed attempt
    backs off for the short retry spacing instead.
    """
    if not _valid_address(session_id):
        return
    path = _rearm_state_path(config_dir, session_id)
    state = _read_rearm_state(path)
    if state is None:
        _write_rearm_state(path, {"last_rearm": 0.0, "last_attempt": 0.0})
        return
    if _watcher_live(config_dir, session_id):
        return
    now = time.time()
    if _since(now, state.get("last_rearm", 0.0)) < _REARM_INTERVAL_S:
        return
    if _since(now, state.get("last_attempt", 0.0)) < _REARM_RETRY_S:
        return
    remaining = _HOOK_CEILING_S - (time.monotonic() - started)
    if remaining < _REARM_MIN_S:
        return
    state["last_attempt"] = now
    _write_rearm_state(path, state)
    nx = shutil.which("nx")
    if nx is None:
        _log_skip("no live mailbox watcher for this session, and no nx on PATH to re-arm it")
        return
    try:
        proc = subprocess.run(  # noqa: S603 — nx resolved on PATH; argv built from a validated session id
            [nx, "hook", "mailbox-arm", "--session-id", session_id],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=min(remaining, _REARM_SPAWN_CAP_S),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log_skip(f"re-arm instruction unavailable: {type(exc).__name__}: {exc}")
        return
    text = proc.stdout.strip()
    if proc.returncode != 0 or not text:
        return
    state["last_rearm"] = now
    _write_rearm_state(path, state)
    sys.stdout.write(
        "No mailbox watch is running for this session; its SessionStart arm "
        "instruction may not have arrived.\n" + text + "\n"
    )
    sys.stdout.flush()


def main() -> int:
    """The hook entry point. NEVER raises, and never exits non-zero.

    The guarantee at the top of this file -- one SKIP line on stderr, exit 0 --
    is the whole contract with a waiting prompt, so it is enforced here rather
    than assumed from the per-address handlers below. Those cover the drain
    itself; this covers everything before and around it (reading the payload,
    resolving the config directory, reading the registry). A hook that runs on
    EVERY UserPromptSubmit has no business putting a traceback in front of
    someone who typed something unrelated.
    """
    try:
        return _drain_all()
    except Exception as exc:  # noqa: BLE001 — the contract above is the reason
        _log_skip(f"unexpected {type(exc).__name__} before any mailbox was drained: {exc}")
        return 0


def _drain_all() -> int:
    started = time.monotonic()
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    session_id = str(payload.get("session_id") or "").strip()

    config_dir = _config_dir()
    try:
        _prune_stale_cleared_records(config_dir, now=time.time())
    except Exception as exc:  # noqa: BLE001 — maintenance only, never the prompt's problem
        _log_skip(f"cleared-record prune: unexpected {type(exc).__name__}: {exc}")

    addresses: list[str] = []
    if _valid_address(session_id):
        addresses.append(session_id)
        addresses.extend(_read_session_registry(config_dir, session_id))
    # First-occurrence dedup: a registry naming this session's own id must not
    # make the hook drain it twice and render the same row in two blocks.
    addresses = list(dict.fromkeys(addresses))
    if not addresses:
        _log_skip("no address to drain: the payload carried no usable session id "
                  "and the address registry is empty")
        return 0

    try:
        base_url, token, is_local = _resolve_endpoint(config_dir)
    except _Skip as exc:
        _log_skip(f"no reachable tuple space: {exc}")
        return 0

    deadline = time.monotonic() + _TOTAL_BUDGET_S
    out = _Out()
    for address in addresses:
        if time.monotonic() >= deadline:
            _log_skip(f"drain budget of {_TOTAL_BUDGET_S}s spent before reaching "
                      f"mailbox/{address}; it keeps until the next prompt")
            break
        try:
            _drain_address(
                base_url, token, address, is_local=is_local,
                config_dir=config_dir, deadline=deadline, out=out,
            )
        except _Skip as exc:
            # Per ADDRESS, not around the whole loop: a transport failure on one
            # mailbox must not stop the others, and everything already delivered
            # has already been written and flushed, so nothing can be retracted.
            _log_skip(f"mailbox/{address}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 — see below
            # UNEXPECTED, and still not the prompt's problem. _Skip covers what
            # this hook anticipated; a malformed engine response (a non-dict body,
            # a row whose dims is not a dict) raises AttributeError or TypeError
            # instead, and before this landed that escaped as an exit-1 traceback
            # on whatever unrelated prompt the user had just typed -- flatly
            # contradicting the contract at the top of this file. The type is
            # named in the line so an unexpected failure stays diagnosable rather
            # than being quietly indistinguishable from a planned skip.
            _log_skip(f"mailbox/{address}: unexpected {type(exc).__name__}: {exc}")
            continue

    # RDR-208 Phase 2 Step 3: this session's own ``/clear`` record, if any --
    # the mailbox(es) a previous session id was stranded at -- drained after
    # the session's own addresses, inside the same overall budget.
    if _valid_address(session_id):
        try:
            _drain_cleared_record(
                base_url, token, session_id, is_local=is_local,
                config_dir=config_dir, deadline=deadline, out=out,
            )
        except Exception as exc:  # noqa: BLE001 — never the prompt's problem; the record keeps for the next pass
            _log_skip(f"cleared record for {session_id}: unexpected {type(exc).__name__}: {exc}")

    try:
        _rearm_if_unwatched(config_dir, session_id, started=started)
    except Exception as exc:  # noqa: BLE001 — the re-arm is advisory; a prompt never sees it fail
        _log_skip(f"re-arm check: unexpected {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
