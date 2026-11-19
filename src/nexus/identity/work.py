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

**Who may stop it.** :func:`may_stop_lines` is the one check behind worktree removal and tmux cleanup: a resource is
stopped only when no identity other than the caller owns it with ``running`` work and a live holder, never by name
pattern or by a process listing. A resource no row names is unowned and is said to be.

A registrar's failure never fails the work it labels (:func:`register_own`, :func:`finish_own` return False and log),
and a process with no identity token registers nothing.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from nexus.identity.bind_client import IdentityApiError, IdentityClient, IdentityUnavailableError
from nexus.identity.ops import IdentityCommandError, IdentityEnv, find, is_alive

_log = structlog.get_logger(__name__)

KINDS: tuple[str, ...] = ("watcher", "wait", "labrun", "worktree", "tmux")
RUNNING = "running"
DONE = "done"

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


def register(client: Any, identity: str, kind: str, ref: str, state: str = RUNNING) -> Work:
    """Register the work, or change its state in place. An identity holds at most 200 rows (``409 work_limit``): the
    first refusal forgets this identity's own ``done`` rows (finished work has no further use) and the call is repeated
    once."""
    try:
        return Work.of(client.put_work(identity, kind, ref, state))
    except IdentityApiError as exc:
        if exc.code != "work_limit":
            raise
    for row in client.work_of(identity):
        if row.get("state") == DONE:
            client.remove_work(identity, str(row["kind"]), str(row["ref"]))
    return Work.of(client.put_work(identity, kind, ref, state))


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


def register_own(kind: str, ref: str, state: str = RUNNING, *, client: Any = None, identity: str | None = None) -> bool:
    """Register work for the identity this process holds. True when a row was written. No identity, no engine, or any
    refusal gives False and a log line: the work this labels must never fail because its label could not be written."""
    try:
        who = identity or caller_identity()
        if who is None:
            return False
        owned = client is None
        engine = IdentityClient.from_config() if owned else client
        try:
            register(engine, who, kind, ref, state)
        finally:
            if owned:
                engine.close()
        return True
    except Exception as exc:  # noqa: BLE001 — a label never fails its work
        _log.warning("identity_work_register_failed", kind=kind, ref=ref, state=state, error=f"{type(exc).__name__}: {exc}")
        return False


def finish_own(kind: str, ref: str, *, client: Any = None, identity: str | None = None) -> bool:
    """The registrar's clean end: ``done``. Same failure rule as :func:`register_own`."""
    return register_own(kind, ref, DONE, client=client, identity=identity)


# ── what ``nx identity work`` prints ────────────────────────────────────────


def _kind(kind: str) -> str:
    if kind not in KINDS:
        raise IdentityCommandError(f"kind must be one of {', '.join(KINDS)}")
    return kind


def _own(who: Callable[[], str | None] | None) -> str:
    identity = (who or caller_identity)()
    if identity is None:
        raise IdentityCommandError("this process holds no identity (it has no bound Claude session ancestor)")
    return identity


def register_lines(env: IdentityEnv, kind: str, ref: str, state: str = RUNNING, *,
                   who: Callable[[], str | None] | None = None) -> list[str]:
    """``nx identity work register|done``: a row for the caller. A process that holds no identity registers nothing and
    says so (exit 0): the verb is called from skills and scripts whose work must go on."""
    _kind(kind)
    identity = (who or caller_identity)()
    if identity is None:
        return ["no identity is bound to this process: nothing registered"]
    row = _call(register, env.client, identity, kind, ref, state)
    return [f"{identity} {row.state} {row.kind} {row.ref}"]


def forget_lines(env: IdentityEnv, kind: str, ref: str, *, who: Callable[[], str | None] | None = None) -> list[str]:
    _kind(kind)
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
              who: Callable[[], str | None] | None = None) -> dict[str, Any]:
    """``{"identity", "alive", "work": [row + "stale"]}``. A running row of an identity that is not alive is stale;
    *only_running* keeps the rows that are ``running`` and not stale."""
    identity = _target(env, seat, session, who)
    alive = _call(is_alive, env, identity)
    rows = []
    for w in _call(of, env.client, identity):
        stale = w.state == RUNNING and not alive
        if only_running and (w.state != RUNNING or stale):
            continue
        rows.append({"kind": w.kind, "ref": w.ref, "state": w.state, "updated_at": w.updated_at, "stale": stale})
    return {"identity": identity, "alive": alive, "work": rows}


def list_lines(report: Mapping[str, Any]) -> list[str]:
    head = f"{report['identity']}: {'alive' if report['alive'] else 'not alive'}"
    rows = report["work"]
    if not rows:
        return [head + ", no work"]
    return [head] + [
        f"  {r['state']:<7} {r['kind']:<9} {r['ref']}" + ("  (stale: the identity is not alive)" if r["stale"] else "")
        for r in rows]


def owners_lines(env: IdentityEnv, kind: str, ref: str) -> list[str]:
    _kind(kind)
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
    """Refuse (raise, naming the owners) when an identity other than the caller owns the resource with running work and
    a live holder; else return the lines to print. The caller's own rows never block it, a ``done`` row never blocks, and a
    stale one never blocks. Ownership is read from the work rows and liveness from the holder check: no name pattern, no
    process listing, no working directory."""
    _kind(kind)
    me = (who or caller_identity)()
    live = [w for w in _call(live_owners, env.client, kind, ref, _alive_of(env)) if w.identity != me]
    if live:
        named = "; ".join(f"{_label_of(env, w.identity)} (its holder is alive)" for w in live)
        raise IdentityCommandError(f"{kind} {ref} is owned by {named}: not stopping it. The owner ends it, or ends its "
                                   f"session first (`nx identity alive <seat>` says whether it runs).")
    rows = _call(owners, env.client, kind, ref)
    if not rows:
        return [f"{kind} {ref}: unowned (no identity registered it)"]
    stale = [w.identity for w in rows if w.state == RUNNING and w.identity != me]
    return [f"{kind} {ref}: no live owner other than this process" + (f" (stale rows ignored: {', '.join(stale)})" if stale else "")]


def run_command(env: IdentityEnv, kind: str, ref: str, argv: Sequence[str], *,
                who: Callable[[], str | None] | None = None,
                runner: Callable[[Sequence[str]], int] | None = None) -> int:
    """``nx identity work run``: register the work running, run *argv* (its streams are this process's), write ``done``
    when it exits, and return its exit status unchanged (128 + n for a signal). A process with no identity just runs it.
    The label is best effort: a refused registration is said once on stderr and the command still runs."""
    _kind(kind)
    if not argv:
        raise IdentityCommandError("give the command to run after --")
    identity = (who or caller_identity)()
    registered = False
    if identity is not None:
        try:
            register(env.client, identity, kind, ref, RUNNING)
            registered = True
        except (IdentityApiError, IdentityUnavailableError) as exc:
            _log.warning("identity_work_run_not_registered", kind=kind, ref=ref, error=str(exc))
    try:
        code = (runner or _spawn)(list(argv))
    finally:
        if registered and identity is not None:
            try:
                mark_done(env.client, identity, kind, ref)
            except (IdentityApiError, IdentityUnavailableError) as exc:
                _log.warning("identity_work_run_not_marked_done", kind=kind, ref=ref, error=str(exc))
    return code


def _spawn(argv: Sequence[str]) -> int:
    from nexus.util.nx_argv import argv_off_cwd  # noqa: PLC0415 — deferred: leaf import cost

    try:
        done = subprocess.run(argv_off_cwd(argv), check=False)
    except FileNotFoundError:
        raise IdentityCommandError(f"command not found: {argv[0]}") from None
    return 128 - done.returncode if done.returncode < 0 else done.returncode

