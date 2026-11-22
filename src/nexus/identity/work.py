# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Background work an identity owns, and who may touch it (RDR-253 "Background work", "Who reads liveness"; Step 6,
nexus-hczdo.1.9).

A session registers each worktree it owns (``kind=worktree``, the absolute path), each tmux server (``kind=tmux``, the
``-L`` socket name) and its running watchers, waits and lab runs as ``identity_work`` rows, through
``POST /v1/identity/<id>/work``. The row is a label, not a fenced family (a lab run on another host has no token file),
and it lasts as long as the identity does.

**Who registers, who marks done.** The creator registers (the git-worktrees skill for a worktree, the sandbox for its
private tmux server, ``tuple_wait`` for a wait, the console for a watcher, ``nx identity work run`` for a lab run) and
the same party writes ``done`` when its work ends cleanly. A crash writes nothing, which is why no reader trusts a
``running`` row on its own.

**The stale rule (every reader).** A ``running`` row counts only while its identity is alive
(:func:`nexus.identity.ops.is_alive`: the holder check on this host, the lease for another host, an unreadable holder
counted as alive; an ended, released, gone or unknown identity is not alive). A ``running`` row of an identity that is not
alive is stale: :func:`running`, :func:`live_owners`, :func:`may_stop_lines` and the restart script's readiness report
all ignore it, so a crashed session's work is never read as busy forever. A ``done`` row never blocks anything.

A running ``wait``, ``labrun``, ``agent`` or ``watcher`` row of a live identity has three more witnesses
(:func:`process_stale_why`, in order). **The probe:** its process is gone or was replaced, read from a ``<host>:<pid>:<started>``
ref (``lab-remote``'s) or from the owner record a long-lived registrar writes under the identity directory
(:func:`record_owner`; a gone process's record is kept until the TTL reads the row stale, so the next registration cannot
take its witness). **The ghost rule:** a row nothing vouches for that was last written before its holder began (a restart
with the work in flight never ends it) is stale. **The TTL:** a row nobody wrote for 12 hours (:data:`ROW_TTL_S`) is stale.
A hand-registered row has the last two. The restart script lists a stale row and does not block on it.

**Who may stop it.** :func:`may_stop_lines` is the one check behind worktree removal and tmux cleanup: a resource is
stopped only when no identity other than the caller owns it with ``running`` work and a live holder, never by name
pattern or by a process listing. A resource no row names is unowned and is said to be.

A registrar's failure never fails the work it labels (:func:`register_own`, :func:`finish_own` return False and log),
and a process with no identity token registers nothing.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from nexus.identity.bind_client import IdentityApiError, IdentityClient, IdentityUnavailableError
from nexus.identity.ops import IdentityCommandError, IdentityEnv, find, is_alive

_log = structlog.get_logger(__name__)

KINDS: tuple[str, ...] = ("watcher", "wait", "labrun", "worktree", "tmux", "agent")
RUNNING = "running"
DONE = "done"

#: The kinds whose rows end with the process that registered them (a worktree or a tmux server outlives its session).
#: A running row of one of these gets the liveness probe of :func:`stale_why`.
PROBED_KINDS: tuple[str, ...] = ("wait", "labrun", "agent", "watcher")

#: A running row of a probed kind that no process vouches for (no owner record, no probe in its ref) reads stale once it
#: has not been written for this long. 12 hours: past the longest wait or lab run anyone starts by hand.
ROW_TTL_S = 12 * 3600.0

#: Seconds a process may have begun after the start a ref records and still be the same process: ``ps`` floors a start to
#: the second and the registrar read the clock after it began.
START_SKEW_S = 2

#: Seconds a row may predate its holder's start and still count as written by it (the engine's clock and the registry's
#: ``startedAt`` are two clocks).
HOLDER_SKEW_S = 10.0

#: ``<host>:<pid>:<started>``: the ref lab-remote registers its ``labrun`` row under (host = the machine's short name,
#: pid = lab-remote's own, started = epoch seconds at its start). The ref carries its own probe.
_PROBE_REF = re.compile(r"^(?P<host>[A-Za-z0-9_-]+):(?P<pid>[0-9]+):(?P<started>[0-9]+)$")

#: Where a registrar that stays alive records its process (see :func:`record_owner`), under the identity directory.
OWNER_DIR_NAME = "work-owners"

#: Whether an identity (by id) has a live holder: :func:`nexus.identity.ops.is_alive` bound to an env, or a stand-in.
Alive = Callable[[str], bool]


@dataclass(frozen=True)
class Work:
    """One ``identity_work`` row."""

    identity: str
    kind: str
    ref: str
    state: str
    updated_at: str | None = None

    @classmethod
    def of(cls, row: Mapping[str, Any]) -> "Work":
        return cls(str(row["identity"]), str(row["kind"]), str(row["ref"]), str(row["state"]), row.get("updated_at"))

    def label(self) -> str:
        return f"{self.kind} {self.ref}"


# ── the calls ───────────────────────────────────────────────────────────────


def register(client: Any, identity: str, kind: str, ref: str, state: str = RUNNING, *, purge: bool = True,
             probe: Any = None) -> Work:
    """Register the work, or change its state in place. An identity holds at most 200 rows (``409 work_limit``): the
    first refusal forgets this identity's own ``done`` rows (finished work has no further use), its ``worktree`` rows
    whose path no longer exists on this host (an agent's isolation worktree is removed by the harness, and nothing marks
    its row done), its ``wait`` rows for a session it no longer holds (a wait killed by a hangup or SIGKILL never wrote its
    end) and, when *probe* is given (an object with ``procs``, ``host``, ``identity_dir`` and ``clock``, as
    :class:`~nexus.identity.ops.IdentityEnv` has), its running rows that read stale (:func:`process_stale_why`: process gone,
    registered before this session began, not written for 12 hours), so orphans cannot fill the limit; the call is then
    repeated once. ``purge=False`` raises the refusal instead and deletes nothing: a hook has seconds, the purge is up to
    ~200 sequential calls."""
    try:
        return Work.of(client.put_work(identity, kind, ref, state))
    except IdentityApiError as exc:
        if exc.code != "work_limit" or not purge:
            raise
    session, started = holder_facts(client, identity)
    for row in client.work_of(identity):
        if (row.get("state") == DONE or worktree_gone(row) or wait_superseded(row, session)
                or (probe is not None and process_stale_why(probe, identity, Work.of(row), holder_started=started))):
            client.remove_work(identity, str(row["kind"]), str(row["ref"]))
    return Work.of(client.put_work(identity, kind, ref, state))


def holder_facts(client: Any, identity: str) -> tuple[str | None, float | None]:
    """``(session id, start in epoch seconds)`` of *identity*'s current holder; None for what it has none of or the engine
    cannot say. The start is the registry's ``startedAt`` (milliseconds) of the holding Claude process."""
    try:
        holder = client.show(identity).get("holder") or {}
    except (IdentityApiError, IdentityUnavailableError):
        return None, None
    if not isinstance(holder, Mapping):
        return None, None
    session = str(holder["session_id"]) if holder.get("session_id") else None
    began = holder.get("started_at")
    return session, (began / 1000.0 if isinstance(began, (int, float)) and not isinstance(began, bool) else None)


def current_session(client: Any, identity: str) -> str | None:
    """The session id of *identity*'s current holder, or None when it has none or the engine cannot say."""
    return holder_facts(client, identity)[0]


#: A Claude session id: a UUID. Only a ref of this shape names a session.
_SESSION_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def wait_superseded(row: Mapping[str, Any], session: str | None) -> bool:
    """A ``wait`` row for a session other than the identity's current one: that wait parked in an earlier session, which
    has ended or moved, so the row is left over. With no current session known, nothing is judged superseded. Only a ref
    that IS a session id (a UUID) is judged: a hand wait (``nx identity work register --kind wait --ref develop-ci``) and
    a CI wait (``ci:<branch>@<sha>``) name no session, and the liveness probe judges them (:func:`process_stale_why`)."""
    ref = str(row.get("ref") or "")
    return row.get("kind") == "wait" and session is not None and bool(_SESSION_ID.match(ref)) and ref != session


def worktree_gone(row: Mapping[str, Any]) -> bool:
    """A ``worktree`` row whose absolute path is not a directory on this host. A path on another host reads as gone
    here too, which is why only the work-limit purge and display filters use it, never may-stop."""
    ref = str(row.get("ref") or "")
    return row.get("kind") == "worktree" and os.path.isabs(ref) and not os.path.isdir(ref)


def mark_done(client: Any, identity: str, kind: str, ref: str) -> Work:
    """The registrar's own clean end of its work."""
    return register(client, identity, kind, ref, DONE)


def forget(client: Any, identity: str, kind: str, ref: str) -> bool:
    """Drop the row; True when there was one."""
    return bool(client.remove_work(identity, kind, ref).get("removed"))


def of(client: Any, identity: str) -> list[Work]:
    """Every row of *identity*, finished and stale ones included."""
    return [Work.of(r) for r in client.work_of(identity)]


def owners(client: Any, kind: str, ref: str) -> list[Work]:
    """Every identity's row for the resource."""
    return [Work.of(r) for r in client.work_owners(kind, ref)]


def running(client: Any, identity: str, alive: Alive) -> list[Work]:
    """*identity*'s running work, or nothing when the identity is not alive (its running rows are stale)."""
    if not alive(identity):
        return []
    return [w for w in of(client, identity) if w.state == RUNNING]


def live_owners(client: Any, kind: str, ref: str, alive: Alive) -> list[Work]:
    """The rows of the resource that still block: ``running`` work of an identity that is alive."""
    cache: dict[str, bool] = {}
    out: list[Work] = []
    for row in owners(client, kind, ref):
        if row.state != RUNNING:
            continue
        if row.identity not in cache:
            cache[row.identity] = alive(row.identity)
        if cache[row.identity]:
            out.append(row)
    return out


# ── the liveness probe of a running row ─────────────────────────────────────


def default_owner_dir() -> Path:
    """The identity directory of this machine's config; owner records live in its ``work-owners`` subdirectory."""
    from nexus import config  # noqa: PLC0415 — deferred: leaf import cost
    from nexus.identity.token_file import identity_dir  # noqa: PLC0415

    return identity_dir(config.nexus_config_dir())


def _owner_path(directory: Path, identity: str, kind: str, ref: str) -> Path:
    return Path(directory) / OWNER_DIR_NAME / (hashlib.sha256(f"{identity}\0{kind}\0{ref}".encode()).hexdigest() + ".json")


def record_owner(directory: Path, identity: str, kind: str, ref: str, *, pid: int, start: str | None, host: str,
                 procs: Any = None) -> None:
    """Record the process that registered a row and stays alive while its work runs (``nx ci wait``, ``nx tuple wait``,
    ``nx identity work run``): its pid, its OS start time and the host. The engine row has no column for it, and a hook's
    process exits at once, so only a registrar that outlives its registration writes one. The directory is private
    (``0700``, and the user's ACE on Windows). With *procs* it first prunes what no process owns any more
    (:func:`prune_owner_records`)."""
    from nexus._winsec import make_user_dir  # noqa: PLC0415 — deferred: leaf import cost

    records = Path(directory) / OWNER_DIR_NAME
    make_user_dir(records)
    if procs is not None:
        prune_owner_records(records, procs)
    path = _owner_path(directory, identity, kind, ref)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"pid": pid, "start": start, "host": host}), encoding="utf-8")
    os.replace(tmp, path)


def prune_owner_records(records: Path, procs: Any) -> None:
    """Remove the ``<hash>.<pid>.tmp`` a writer that died mid-write left, and the owner records whose process is gone or was
    replaced AND whose file is older than :data:`ROW_TTL_S`. A live process keeps its record however old (a standing wait
    older than any cutoff still runs). A record whose process is gone is the row's only witness (``its process is gone``)
    until the TTL rule reads the row stale by itself, so it is kept that long: this runs on every registration, the
    directory is shared by every identity, and a SIGKILLed ``nx ci wait`` would otherwise lose its witness to the next
    unrelated registration and read live for 12 hours. A record of another host, or one whose process cannot be judged,
    is kept. Best effort."""
    import time  # noqa: PLC0415 — deferred: only a registration's prune needs it

    here = getattr(procs, "host", "")
    expired = time.time() - ROW_TTL_S
    for path in list(records.glob("*")):
        try:
            if path.suffix == ".tmp":
                writer = path.name.split(".")[-2]
                if writer.isdigit() and not procs.alive(int(writer)):
                    path.unlink()
            elif path.suffix == ".json":
                record = json.loads(path.read_text(encoding="utf-8"))
                start = record.get("start")
                judged, gone = _process_gone(procs, here, str(record.get("host") or ""), int(record["pid"]),
                                             start=start if isinstance(start, str) else None, began=None)
                if judged and gone and path.stat().st_mtime < expired:
                    path.unlink()
        except (OSError, ValueError, TypeError, KeyError):
            continue


def read_owner(directory: Path, identity: str, kind: str, ref: str) -> dict[str, Any] | None:
    try:
        record = json.loads(_owner_path(directory, identity, kind, ref).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and isinstance(record.get("pid"), int) else None


def clear_owner(directory: Path, identity: str, kind: str, ref: str) -> None:
    try:
        _owner_path(directory, identity, kind, ref).unlink()
    except OSError:
        pass


def _short(host: object) -> str:
    return str(host or "").split(".")[0]


def _epoch(value: object) -> float | None:
    """An engine instant (an ISO string, or epoch seconds as a stand-in gives it) as epoch seconds."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp()
    return None


def _same_start(a: str, b: str) -> bool:
    """Two spellings of one process start: equal, or within :data:`START_SKEW_S` (a Linux ``lstart`` can jitter by a second)."""
    if a == b:
        return True
    from nexus.identity.holder import epoch_seconds  # noqa: PLC0415 — deferred: leaf import cost

    try:
        return abs(epoch_seconds(a) - epoch_seconds(b)) <= 1.0
    except ValueError:
        return False


def _process_gone(procs: Any, here: str, host: str, pid: int, *, start: str | None, began: float | None) -> tuple[bool, bool]:
    """``(judged, gone)`` for a process another party recorded. Not judged: the record names another host (this one's key, or
    the short form of the name a hand-written ref gives it: ``lab-remote`` writes ``gethostname()`` up to its first dot).
    A labrun ref carries only that short hostname, so two machines that share a short name still compare equal here until
    lab-remote writes the key, and the ref's pid is then judged against this machine's table. Owner records the registrar
    wrote carry the full host, which is the key from this release on. Gone: the pid
    runs no longer, or it runs but began at another time than recorded (the pid was reused): by *start* (the OS start time
    string) when the record has one, else by *began* (epoch seconds, with :data:`START_SKEW_S` of slack). A start time that
    cannot be read is never gone: a reader that cannot prove it must not say it."""
    from nexus.identity.hostname import machine_short_names  # noqa: PLC0415 — deferred: leaf import cost

    if _short(host) not in machine_short_names(here):
        return False, False
    if not procs.alive(pid):
        return True, True
    now_start = procs.os_start(pid)
    if now_start is None:
        return True, False
    if start is not None:
        return True, not _same_start(now_start, start)
    if began is not None:
        from nexus.identity.holder import epoch_seconds  # noqa: PLC0415 — deferred: leaf import cost

        try:
            return True, epoch_seconds(now_start) > began + START_SKEW_S
        except ValueError:
            return True, False
    return True, False


def process_stale_why(env: Any, identity: str, w: Work, *, now: float | None = None,
                      holder_started: float | None = None) -> str | None:
    """Why a running row of a probed kind reads stale, or None. Witnesses, in order. The ref, when it is
    ``<host>:<pid>:<started>`` (lab-remote's ``labrun`` row): its process gone or reused. An owner record the registrar
    wrote (:func:`record_owner`): the same. When a process on this host vouches for the row (it runs, and is the one
    recorded), the row is not stale however old. A row nothing vouches for (an agent's, which a hook registers and whose
    process exits at once; a hand-registered wait) is a ghost when it was last written before the current holder began
    (*holder_started*, epoch seconds): a restart with the work in flight never ends it. Otherwise a row nobody has written
    for :data:`ROW_TTL_S` is stale. *env* is anything with ``procs``, ``host``, ``identity_dir`` and ``clock``."""
    if w.state != RUNNING or w.kind not in PROBED_KINDS:
        return None
    gone_reason = "its process is gone"
    match = _PROBE_REF.match(w.ref)
    if match:
        judged, gone = _process_gone(env.procs, env.host, match["host"], int(match["pid"]), start=None, began=float(match["started"]))
        if judged:
            return gone_reason if gone else None
    record = read_owner(env.identity_dir, identity, w.kind, w.ref)
    if record is not None:
        judged, gone = _process_gone(env.procs, env.host, str(record.get("host") or ""), int(record["pid"]),
                                     start=record.get("start") if isinstance(record.get("start"), str) else None, began=None)
        if judged:
            return gone_reason if gone else None
    written = _epoch(w.updated_at)
    if written is not None and holder_started is not None and written < holder_started - HOLDER_SKEW_S:
        return "registered before this session started"
    if written is not None and (now if now is not None else env.clock()) - written > ROW_TTL_S:
        return f"not written for over {int(ROW_TTL_S // 3600)} h"
    return None


# ── the caller ──────────────────────────────────────────────────────────────


def caller_identity() -> str | None:
    """The identity this process holds, from its token file (``token_header.current``), else None. A process with no Claude
    ancestor, or no usable token, holds none. Never the resolver: a CLI that must not fail on a stale session id reads
    the file alone."""
    from nexus.identity import token_header  # noqa: PLC0415 — deferred: leaf import cost

    record = token_header.current()
    return record.identity if record is not None else None


def _label_of(env: IdentityEnv, identity: str) -> str:
    try:
        view = env.client.show(identity)
    except (IdentityApiError, IdentityUnavailableError):
        return identity
    name = view.get("name") or view.get("display_name")
    return f"{identity} ({name})" if name else identity


def _alive_of(env: IdentityEnv) -> Alive:
    return lambda identity: is_alive(env, identity)


def _call(fn: Callable[..., Any], *args: Any) -> Any:
    try:
        return fn(*args)
    except IdentityApiError as exc:
        raise IdentityCommandError(f"{exc.code or exc.status}: {exc.message}") from exc
    except IdentityUnavailableError as exc:
        raise IdentityCommandError(f"the engine is not answering ({exc})") from exc


# ── the registrars' side: never fail the work ───────────────────────────────


#: Seconds a registrar's engine call may take. A label must never delay what it labels.
LABEL_TIMEOUT_S = 2.0

#: Seconds the one retry of a failed end may take (:meth:`Label.finish`): the work is over, so a longer bound costs nothing.
FINISH_RETRY_TIMEOUT_S = 5.0


#: Kinds an engine answered ``bad_kind`` for in this process (an engine from before the kind), with the monotonic instant
#: the refusal lapses: not asked again meanwhile, so a process pays one failed call per kind per window, not one per
#: dispatch. It lapses (:data:`REFUSAL_TTL_S`) because a long-lived MCP server outlives an engine upgrade.
_REFUSED_KINDS: dict[str, float] = {}
REFUSAL_TTL_S = 600.0


def _monotonic() -> float:
    import time  # noqa: PLC0415 — deferred (a seam for tests)

    return time.monotonic()


def _refused(kind: str) -> bool:
    until = _REFUSED_KINDS.get(kind)
    if until is None:
        return False
    if _monotonic() < until:
        return True
    del _REFUSED_KINDS[kind]
    return False


def reset_for_tests() -> None:
    _REFUSED_KINDS.clear()


def _new_client(timeout: float) -> Any:
    """The engine client a registrar with no client of its own writes through (a seam: the suite refuses it, so no test
    writes a row to a real engine, tests/conftest.py ``_identity_work_reaches_no_real_engine``)."""
    return IdentityClient.from_config(timeout=timeout)


def register_own(kind: str, ref: str, state: str = RUNNING, *, client: Any = None, identity: str | None = None,
                 timeout: float = LABEL_TIMEOUT_S, owner_dir: Path | None = None, procs: Any = None,
                 purge: bool = True) -> bool:
    """Register work for *identity* (default: the one this process holds). True when a row was written. No identity, no
    engine, or any refusal gives False and a log line: the work this labels must never fail, or wait on a slow engine,
    because its label could not be written. An engine that predates a kind (``bad_kind``) is such a refusal, and this
    process does not ask it for that kind again (:data:`_REFUSED_KINDS`).

    *owner_dir* is for a registrar whose process stays alive while its work runs: the row's liveness probe is then this
    process (:func:`record_owner`), written when the row goes running and removed when it goes done. A hook, whose process
    exits at once, passes none, and passes ``purge=False``: at the 200-row limit it is refused rather than spend its few
    seconds deleting rows (:func:`register`)."""
    if _refused(kind):
        return False
    try:
        who = identity or caller_identity()
        if who is None:
            return False
        owned = client is None
        engine = _new_client(timeout) if owned else client
        label = norm_ref(kind, ref)
        try:
            register(engine, who, kind, label, state, purge=purge, probe=_probe_env(owner_dir, procs) if purge else None)
        finally:
            if owned:
                engine.close()
    except Exception as exc:  # noqa: BLE001 — a label never fails its work
        _note_refusal(kind, exc)
        _log.warning("identity_work_register_failed", kind=kind, ref=ref, state=state, error=f"{type(exc).__name__}: {exc}")
        return False
    if owner_dir is not None:
        _own_record(owner_dir, who, kind, label, state, procs)
    return True


def forget_own(kind: str, ref: str, *, client: Any = None, identity: str | None = None, timeout: float = LABEL_TIMEOUT_S) -> bool:
    """Remove this identity's row: the end of work that leaves nothing to show (a dispatched agent: the ready check ignores a
    done row, and one done row per dispatch would fill the 200-row limit). True when the row is gone (there was none, too).
    Same failure rule as :func:`register_own`."""
    if _refused(kind):
        return False
    try:
        who = identity or caller_identity()
        if who is None:
            # The row (if a start wrote one) stays until the reap closes it: say so, since a silent no-op here leaves no
            # trace of the end that did not write (nexus-hczdo).
            _log.warning("identity_work_forget_no_identity", kind=kind, ref=ref)
            return False
        owned = client is None
        engine = _new_client(timeout) if owned else client
        try:
            forget(engine, who, kind, norm_ref(kind, ref))
        finally:
            if owned:
                engine.close()
        return True
    except Exception as exc:  # noqa: BLE001 — a label never fails its work
        _note_refusal(kind, exc)
        _log.warning("identity_work_forget_failed", kind=kind, ref=ref, error=f"{type(exc).__name__}: {exc}")
        return False


def _note_refusal(kind: str, exc: BaseException) -> None:
    if isinstance(exc, IdentityApiError) and exc.code == "bad_kind":
        _REFUSED_KINDS[kind] = _monotonic() + REFUSAL_TTL_S


def _probe_env(owner_dir: Path | None, procs: Any) -> Any:
    """What :func:`process_stale_why` needs, for the purge a registrar's call may run at the work limit."""
    import time  # noqa: PLC0415 — deferred: only the rare purge needs it
    from types import SimpleNamespace  # noqa: PLC0415

    from nexus.identity.holder import Processes  # noqa: PLC0415

    table = procs if procs is not None else Processes()
    return SimpleNamespace(procs=table, host=table.host, identity_dir=owner_dir if owner_dir is not None else default_owner_dir(),
                           clock=time.time)


def _own_record(owner_dir: Path, identity: str, kind: str, ref: str, state: str, procs: Any) -> None:
    """The owner record of a row this process registered: written when it goes running, removed when it goes done. Best
    effort: the row is already written, and a record that is missing only leaves the row to the ghost and TTL rules."""
    try:
        if state != RUNNING:
            clear_owner(owner_dir, identity, kind, ref)
            return
        from nexus.identity.holder import Processes  # noqa: PLC0415 — deferred: leaf import cost

        table = procs if procs is not None else Processes()
        pid = os.getpid()
        record_owner(owner_dir, identity, kind, ref, pid=pid, start=table.os_start(pid), host=table.host, procs=table)
    except Exception as exc:  # noqa: BLE001 — as above
        _log.warning("identity_work_owner_record_failed", kind=kind, ref=ref, error=f"{type(exc).__name__}: {exc}")


def finish_own(kind: str, ref: str, *, client: Any = None, identity: str | None = None,
               timeout: float = LABEL_TIMEOUT_S, owner_dir: Path | None = None, procs: Any = None) -> bool:
    """The registrar's clean end: ``done``. Same failure rule as :func:`register_own`."""
    return register_own(kind, ref, DONE, client=client, identity=identity, timeout=timeout, owner_dir=owner_dir, procs=procs)


def _safe_owner_dir() -> Path | None:
    try:
        return default_owner_dir()
    except Exception as exc:  # noqa: BLE001 — a label never fails its work
        _log.warning("identity_work_owner_dir_unreadable", error=f"{type(exc).__name__}: {exc}")
        return None


class Label:
    """One registrar's row for one piece of work: registered running at :meth:`start`, ``done`` at :meth:`finish`.

    The identity is captured at start and used at finish, so a token that expired or changed in between never writes the
    end under another identity or not at all. :meth:`finish` runs once, writes nothing for a label that never started, and
    is skipped with ``skip=True`` for work another party took over (a superseded wait must not mark its successor's row
    done). :meth:`arm_sigterm` makes a TERM end the row too, then dies of the TERM as it would have."""

    def __init__(self, kind: str, ref: str, *, timeout: float = LABEL_TIMEOUT_S) -> None:
        self.kind, self.ref, self.timeout = kind, ref, timeout
        self.identity: str | None = None
        self._started = False
        self._finished = False
        self._previous: dict[int, Any] = {}

    def start(self) -> bool:
        try:
            self.identity = caller_identity()
        except Exception as exc:  # noqa: BLE001 — reading the token file never fails the work
            _log.warning("identity_work_caller_unreadable", error=f"{type(exc).__name__}: {exc}")
            return False
        self._started = bool(self.identity) and register_own(self.kind, self.ref, identity=self.identity, timeout=self.timeout,
                                                             owner_dir=_safe_owner_dir())
        return self._started

    def finish(self, *, skip: bool = False) -> None:
        if not self._started or self._finished:
            return
        self._finished = True
        self._disarm()
        if not skip and not finish_own(self.kind, self.ref, identity=self.identity, timeout=self.timeout,
                                       owner_dir=_safe_owner_dir()):
            # One retry on a longer bound: the end is the write a reader cannot replace (a missed one leaves the row for the
            # reap), and the work it labels is already over, so it delays nothing that waits on it.
            if not finish_own(self.kind, self.ref, identity=self.identity, timeout=FINISH_RETRY_TIMEOUT_S,
                              owner_dir=_safe_owner_dir()):
                _log.warning("identity_work_finish_failed", kind=self.kind, ref=self.ref, identity=self.identity)

    def arm_sigterm(self) -> None:
        """Mark the row done on SIGTERM or SIGHUP (a closed terminal), then let the signal do what it would have done: call
        the handler that was there before, or die of it. A signal this process ignores (``nohup`` sets SIGHUP to SIG_IGN)
        is left ignored, so arming never makes a process killable that was not. No-op off the main thread."""
        if not self._started or threading.current_thread() is not threading.main_thread():
            return

        def on_signal(signum: int, frame: Any) -> None:
            before = self._previous.get(signum)
            self.finish()                                                     # writes the end and restores every handler
            if callable(before):
                before(signum, frame)
                return
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

        for signum in _LABEL_SIGNALS:
            current = signal.getsignal(signum)
            if current == signal.SIG_IGN:
                continue
            self._previous[signum] = current
            signal.signal(signum, on_signal)

    def _disarm(self) -> None:
        for signum, before in self._previous.items():
            # None: a handler installed outside Python, which cannot be put back from here; the default is the honest
            # stand-in (it is what the process had before that handler, and what a TERM or HUP does unhandled).
            signal.signal(signum, before if before is not None else signal.SIG_DFL)
        self._previous = {}


#: The signals that end a :class:`Label`'s row before the process dies of them: TaskStop's TERM, a closed terminal's HUP.
_LABEL_SIGNALS: tuple[int, ...] = tuple(getattr(signal, n) for n in ("SIGTERM", "SIGHUP") if hasattr(signal, n))


# ── what ``nx identity work`` prints ────────────────────────────────────────


def _kind(kind: str) -> str:
    if kind not in KINDS:
        raise IdentityCommandError(f"kind must be one of {', '.join(KINDS)}")
    return kind


class OwnedByLiveIdentity(IdentityCommandError):
    """``may-stop`` refused because another identity owns the resource with running work and a live holder. A distinct
    type, so the CLI gives it its own exit status (:data:`EXIT_OWNED`) and a script keys on the status, never the text."""


#: ``nx identity may-stop``'s exit status for "a live identity owns it"; 1 is every other failure.
EXIT_OWNED = 3


def norm_ref(kind: str, ref: str) -> str:
    """The label as the registrar and the checker must both spell it: a worktree is its real absolute path (a relative
    path, a trailing slash or a symlinked prefix such as /tmp for /private/tmp would otherwise miss the row and read as
    unowned). Other kinds are labels, used as given."""
    return os.path.realpath(ref) if kind == "worktree" else ref


def _own(who: Callable[[], str | None] | None) -> str:
    identity = (who or caller_identity)()
    if identity is None:
        raise IdentityCommandError("this process holds no identity (it has no bound Claude session ancestor)")
    return identity


def register_lines(env: IdentityEnv, kind: str, ref: str, state: str = RUNNING, *,
                   who: Callable[[], str | None] | None = None) -> list[str]:
    """``nx identity work register|done``: a row for the caller. A process that holds no identity registers nothing and
    says so (exit 0): the verb is called from skills and scripts whose work must go on. ``done`` for work with no row is a
    no-op that says so (it never creates a finished row for work nobody registered)."""
    _kind(kind)
    ref = norm_ref(kind, ref)
    identity = (who or caller_identity)()
    if identity is None:
        return ["no identity is bound to this process: nothing registered"]
    if state == DONE and not any((w.kind, w.ref) == (kind, ref) for w in _call(of, env.client, identity)):
        return [f"{identity} has no {kind} {ref}: nothing to mark done"]
    row = _call(lambda: register(env.client, identity, kind, ref, state, probe=env))
    return [f"{identity} {row.state} {row.kind} {row.ref}"]


def forget_lines(env: IdentityEnv, kind: str, ref: str, *, who: Callable[[], str | None] | None = None) -> list[str]:
    _kind(kind)
    ref = norm_ref(kind, ref)
    identity = (who or caller_identity)()
    if identity is None:
        return ["no identity is bound to this process: nothing forgotten"]
    gone = _call(forget, env.client, identity, kind, ref)
    return [f"{identity} forgot {kind} {ref}" if gone else f"{identity} had no {kind} {ref}"]


def _target(env: IdentityEnv, seat: str | None, session: str | None, who: Callable[[], str | None] | None) -> str:
    if session:
        return str(_call(env.client.session, session)["identity"])
    if seat:
        return str(find(env, seat)["id"])
    return _own(who)


def list_rows(env: IdentityEnv, seat: str | None = None, session: str | None = None, *, only_running: bool = False,
              who: Callable[[], str | None] | None = None, reap_first: bool = False) -> dict[str, Any]:
    """``{"identity", "alive", "work": [row + "stale"]}``. A running row is stale when its identity is not alive, when it
    is a ``worktree`` whose path is not a directory on this host, when it is a ``wait`` for a session the identity no
    longer holds, or when it is a wait, lab run, agent or watcher whose registering process is gone or that nobody has
    written for 12 hours (:func:`process_stale_why`); the stale row carries ``why``. *only_running* keeps the rows that are
    ``running`` and not stale. *reap_first* closes the rows whose work has ended (:func:`nexus.identity.work_reap.reap`) before reading, and the
    report then carries them under ``"reaped"``."""
    identity = _target(env, seat, session, who)
    if reap_first:
        from nexus.identity.work_reap import reap  # noqa: PLC0415 — deferred: only a reaping read needs it

        reaped = _call(reap, env, identity)
    else:
        reaped = None
    alive = _call(is_alive, env, identity)
    current, started = holder_facts(env.client, identity)
    rows = []
    for w in _call(of, env.client, identity):
        row = {"kind": w.kind, "ref": w.ref}
        why = None
        if w.state == RUNNING:
            if not alive:
                why = "the identity is not alive"
            elif worktree_gone(row):
                why = "the worktree is gone"
            elif wait_superseded(row, current):
                why = "a wait from an earlier session"
            else:
                why = process_stale_why(env, identity, w, holder_started=started)
        if only_running and (w.state != RUNNING or why):
            continue
        entry = {"kind": w.kind, "ref": w.ref, "state": w.state, "updated_at": w.updated_at, "stale": bool(why)}
        if why:
            entry["why"] = why
        rows.append(entry)
    report: dict[str, Any] = {"identity": identity, "alive": alive, "work": rows}
    if reaped is not None:
        report["reaped"] = reaped
    return report


def list_lines(report: Mapping[str, Any]) -> list[str]:
    head = f"{report['identity']}: {'alive' if report['alive'] else 'not alive'}"
    reaped = [f"  closed  {r['kind']:<9} {r['ref']}  ({r['why']}; {r['closed']})" for r in report.get("reaped") or []]
    rows = report["work"]
    if not rows:
        return [head + ", no work"] + reaped
    return [head] + reaped + [
        f"  {r['state']:<7} {r['kind']:<9} {r['ref']}" + (f"  (stale: {_why_stale(report, r)})" if r["stale"] else "")
        for r in rows]


def _why_stale(report: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    return str(row.get("why") or "stale")


def owners_lines(env: IdentityEnv, kind: str, ref: str) -> list[str]:
    _kind(kind)
    ref = norm_ref(kind, ref)
    rows = _call(owners, env.client, kind, ref)
    if not rows:
        return [f"{kind} {ref}: unowned (no identity registered it)"]
    alive = _alive_of(env)
    lines = [f"{kind} {ref}:"]
    for w in rows:
        up = _call(alive, w.identity)
        note = "" if w.state != RUNNING else ("  (holder alive)" if up else "  (stale: holder not alive)")
        lines.append(f"  {_label_of(env, w.identity)} {w.state}{note}")
    return lines


def may_stop_lines(env: IdentityEnv, kind: str, ref: str, *, who: Callable[[], str | None] | None = None) -> list[str]:
    """Refuse (raise :class:`OwnedByLiveIdentity`, naming the owners) when an identity other than the caller owns the
    resource with running work and a live holder; else return the lines to print. The caller's own rows never block it,
    a ``done`` row never blocks, and a stale one never blocks. Ownership is read from the work rows and liveness from the
    holder check: no name pattern, no process listing, no working directory. A worktree's *ref* is its real path."""
    _kind(kind)
    ref = norm_ref(kind, ref)
    me = (who or caller_identity)()
    live = [w for w in _call(live_owners, env.client, kind, ref, _alive_of(env)) if w.identity != me]
    if live:
        named = "; ".join(f"{_label_of(env, w.identity)} (its holder is alive)" for w in live)
        unsure = ("" if me is not None else " This process holds no readable identity (none bound, or its token expired), "
                  "so it cannot tell whether it is the owner.")
        raise OwnedByLiveIdentity(f"{kind} {ref} is owned by {named}: not stopping it.{unsure} The owner ends it, or ends its "
                                  f"session first (`nx identity alive <seat>` says whether it runs).")
    rows = _call(owners, env.client, kind, ref)
    if not rows:
        return [f"{kind} {ref}: unowned (no identity registered it)"]
    stale = [w.identity for w in rows if w.state == RUNNING and w.identity != me]
    return [f"{kind} {ref}: no live owner other than this process" + (f" (stale rows ignored: {', '.join(stale)})" if stale else "")]


def run_command(env: IdentityEnv | Callable[[], IdentityEnv], kind: str, ref: str, argv: Sequence[str], *,
                who: Callable[[], str | None] | None = None,
                runner: Callable[[Sequence[str]], int] | None = None) -> int:
    """``nx identity work run``: register the work running, run *argv* (its streams are this process's), write ``done``
    when it exits, and return its exit status unchanged (128 + n for a signal). A process with no identity just runs it.

    The label is best effort in every part: *env* may be a factory, called inside the same guard as the registration, so
    an engine that cannot be reached, a refusal or an unexpected answer is logged and the command still runs. SIGTERM and
    SIGHUP sent to this process are forwarded to the child (:func:`_spawn`); SIGINT is not forwarded (a terminal's Ctrl-C
    already reaches the child through the process group) but is caught, so this process outlives the child. The row is
    marked done after the child ends."""
    _kind(kind)
    ref = norm_ref(kind, ref)
    if not argv:
        raise IdentityCommandError("give the command to run after --")
    engine: IdentityEnv | None = None
    identity: str | None = None
    try:
        identity = (who or caller_identity)()
        if identity is not None:
            engine = env() if callable(env) else env
            register(engine.client, identity, kind, ref, RUNNING, probe=engine)
            _own_record(engine.identity_dir, identity, kind, ref, RUNNING, engine.procs)
    except Exception as exc:  # noqa: BLE001 — a label never fails its work
        _log.warning("identity_work_run_not_registered", kind=kind, ref=ref, error=f"{type(exc).__name__}: {exc}")
        engine = None
    try:
        code = (runner or _spawn)(list(argv))
    finally:
        if engine is not None and identity is not None:
            try:
                mark_done(engine.client, identity, kind, ref)
                _own_record(engine.identity_dir, identity, kind, ref, DONE, engine.procs)
            except Exception as exc:  # noqa: BLE001 — as above
                _log.warning("identity_work_run_not_marked_done", kind=kind, ref=ref, error=f"{type(exc).__name__}: {exc}")
    return code


_FORWARDED = tuple(getattr(signal, n) for n in ("SIGTERM", "SIGINT", "SIGHUP") if hasattr(signal, n))


def _spawn(argv: Sequence[str]) -> int:
    """Run *argv* and return its status (128 + n for a child killed by signal n). SIGTERM and SIGHUP this process receives
    are passed on to the child, so a stopped ``work run`` stops what it wraps, then returns normally and the caller marks
    the row done. SIGINT is caught and dropped (see below). Handlers are installed on the main thread only, and put back
    afterwards."""
    from nexus.util.nx_argv import argv_off_cwd  # noqa: PLC0415 — deferred: leaf import cost

    # Handlers go in BEFORE the spawn, so a signal in that window is held and passed on once the child exists, not
    # lost with the child orphaned. SIGINT is not forwarded: a terminal's Ctrl-C already reaches the child through the
    # foreground process group, and this process only has to outlive it to mark the row done.
    holder: dict[str, Any] = {}
    pending: list[int] = []
    previous: dict[int, Any] = {}

    def forward(signum: int, frame: Any) -> None:
        if signum == signal.SIGINT:
            return
        child_now = holder.get("child")
        if child_now is None:
            pending.append(signum)
            return
        try:
            child_now.send_signal(signum)
        except OSError:
            pass

    if threading.current_thread() is threading.main_thread():
        for signum in _FORWARDED:
            if signal.getsignal(signum) == signal.SIG_IGN:
                continue                    # `nohup nx identity work run ...`: an ignored HUP stays ignored, here and in the child
            previous[signum] = signal.signal(signum, forward)
    try:
        try:
            child = subprocess.Popen(argv_off_cwd(argv))
        except FileNotFoundError:
            raise IdentityCommandError(f"command not found: {argv[0]}") from None
        except PermissionError:
            raise IdentityCommandError(f"command not executable: {argv[0]}") from None
        holder["child"] = child
        for signum in pending:
            forward(signum, None)
        code = child.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler if handler is not None else signal.SIG_DFL)   # None: set outside Python
    return 128 - code if code < 0 else code
