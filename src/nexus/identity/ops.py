# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""What ``nx identity`` does (RDR-253 "Take-over", "Fencing", "Aliases and renames"; Step 4, nexus-hczdo.1.5).

``nexus.commands.identity_cmd`` is the Click layer; this module is the logic, against an :class:`IdentityEnv`: an
engine client, a view of the machine (:class:`~nexus.identity.holder.Processes`), the identity directory and the facts
about the caller's process ancestry. A test passes an env built from a fake registry tree and a stand-in client.

Every function returns the lines to print, or raises :class:`IdentityCommandError` with the reason it refused. The
guards live here so no caller can skip them:

* ``claim --take-over`` runs only from an interactive Claude session (registry ``entrypoint`` ``cli`` or
  ``claude-desktop``; a ``claude -p`` child has ``sdk-cli``), is refused when the holder's process is an ancestor of the
  caller's, and when the caller has no Claude ancestor with a registry file at all (``find_immediate_claude_pid`` falls
  back to the shell itself). It binds the caller's own Claude process as a new generation, writes its token file, then
  closes the caller's previous identity: ``end`` for an ephemeral, ``release`` for a seat.
* ``release`` carries none of those guards and binds nothing: on the holder's host it runs the holder check and is
  refused while the holder runs; for a holder on another host it is refused while the lease is live.
* ``purge --operator`` is refused for a live identity (and an unheld one, and a lapsed one with no recorded ``gone``); the
  engine enforces the same and audit-logs the rest.
"""
from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nexus.identity import token_contract
from nexus.identity.bind_client import (
    FILE_TOKEN_401_ADVICE, NO_TOKEN_ADVICE, OWN_TOKEN, OWN_TOKEN_401_ADVICE, SCOPE_DATA, IdentityApiError, IdentityClient, IdentityUnavailableError, evidence_body,
    seat_refusal_message,
)
from nexus.identity.delegated import check_dispatcher, sweep_delegated
from nexus.identity.holder import (
    GONE, INTERACTIVE_ENTRYPOINTS, OTHER_HOST, RUNNING, Processes, Verdict, check_holder, holder_process,
    normalise_instant, read_registry_entry,
)
from nexus.identity.hostname import bindings_here, describe_host, holder_is_local
from nexus.identity.sweep import sweep_host
from nexus.identity.token_file import TokenFile, read_token_file, token_path, update_token_file

_ID = re.compile(r"^i[0-9]+$")
SDK_ENTRYPOINT = "sdk-cli"
NX_IDENTITY_TOKEN_ENV = "NX_IDENTITY_TOKEN"
RECENT_ROWS = 5


class IdentityCommandError(Exception):
    """A command refused, or the engine did; the message is what the person reads."""


@dataclass(frozen=True)
class CallerFacts:
    """What the caller knows about its own process tree. *claude_pid* is the nearest ``claude*`` ancestor, which
    ``find_immediate_claude_pid`` replaces with the shell itself when there is none (0 when the chain cannot be walked);
    *ancestors* are the pids above the caller, nearest first."""

    claude_pid: int
    ancestors: tuple[int, ...]


def real_caller_facts() -> CallerFacts:
    from nexus.session import _ppid_of, find_immediate_claude_pid  # noqa: PLC0415 — deferred: heavy import

    chain: list[int] = []
    cur = _ppid_of(os.getpid())
    while cur and cur > 1 and cur not in chain and len(chain) < 64:
        chain.append(cur)
        cur = _ppid_of(cur)
    return CallerFacts(claude_pid=find_immediate_claude_pid(), ancestors=tuple(chain))


def _real_command_name(pid: int) -> str:
    from nexus.session import _command_name_of  # noqa: PLC0415 — deferred: heavy import

    return _command_name_of(pid)


def _real_own_token() -> TokenFile | None:
    """This process's live token, after the session check a fenced write makes: a Bash child started before a cross-seat
    ``/resume`` raises the resolver's ``IdentityConflictError`` rather than send a token that is not its session's."""
    from nexus.identity import token_header  # noqa: PLC0415 — deferred: CLI startup cost

    return token_header.verified_current()


@contextmanager
def _real_board_memory():
    from nexus.commands._helpers import t2_handle  # noqa: PLC0415 — deferred: CLI startup cost

    with t2_handle() as db:
        yield db.memory


@dataclass
class IdentityEnv:
    client: Any
    procs: Processes | Any
    identity_dir: Path
    environ: Mapping[str, str]
    caller: Callable[[], CallerFacts]
    clock: Callable[[], float] = time.time
    client_version: str | None = None
    #: the command name of a pid (``claude`` for a Claude process); "" when unknown. The real one reads the process table.
    command_name: Callable[[int], str] | None = None
    #: opens a T2 memory store (a context manager) for the threads board; None skips the board (a test with none).
    board_memory: Callable[[], AbstractContextManager[Any]] | None = None
    #: this process's own identity token file when it holds a live token (None: it holds none). A data credential makes
    #: and promotes seats through it (RDR-253 option 2); a tenant credential never reads it.
    own_token: Callable[[], TokenFile | None] | None = None

    @property
    def host(self) -> str:
        return self.procs.host

    @classmethod
    def real(cls) -> "IdentityEnv":
        from nexus import config  # noqa: PLC0415
        from nexus.identity.token_file import identity_dir  # noqa: PLC0415

        try:
            client = IdentityClient.from_config()
        except IdentityUnavailableError as exc:
            raise IdentityCommandError(f"no engine to talk to ({exc})") from exc
        try:
            from importlib.metadata import version  # noqa: PLC0415

            client_version: str | None = version("conexus")
        except Exception:  # noqa: BLE001 — a development tree has no installed distribution
            client_version = None
        return cls(client=client, procs=Processes(), identity_dir=identity_dir(config.nexus_config_dir()),
                   environ=os.environ, caller=real_caller_facts, client_version=client_version,
                   command_name=_real_command_name, board_memory=_real_board_memory, own_token=_real_own_token)


# ── engine access ───────────────────────────────────────────────────────────


def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except IdentityApiError as exc:
        raise IdentityCommandError(f"{exc.code or exc.status}: {exc.message}") from exc
    except IdentityUnavailableError as exc:
        raise IdentityCommandError(f"the engine is not answering ({exc})") from exc


def find(env: IdentityEnv, ref: str) -> dict[str, Any]:
    """The identity named by a seat name or an id."""
    if _ID.match(ref):
        return dict(_call(env.client.show, ref))
    found = _call(env.client.alias, ref)
    return dict(_call(env.client.show, found["identity"]))


def _name(view: Mapping[str, Any]) -> str:
    return str(view.get("name") or view.get("display_name") or view["id"])


def _holder_line(view: Mapping[str, Any]) -> str:
    holder = view.get("holder")
    if not isinstance(holder, Mapping):
        return "held by no process"
    return f"held by pid {holder.get('pid')} on {describe_host(holder.get('host'))}, process started {holder.get('proc_start')}"


# ── create, promote, rename, purge ──────────────────────────────────────────


#: What an engine's ``identity_token_required`` means for a create: a create names no identity, so no file can stand in.
CREATE_NO_TOKEN = ("no identity token was sent, and in cloud mode a seat is made only through a live session's own identity "
                   "token")


def _seat_call(env: IdentityEnv, call: Callable[[], Any], identity: str | None, *, token_source: str = OWN_TOKEN,
               no_token: str = NO_TOKEN_ADVICE, unauthorized: str = OWN_TOKEN_401_ADVICE) -> Any:
    """:func:`_call` for the seat calls; a data credential's refusals are said in the words of
    :func:`~nexus.identity.bind_client.seat_refusal_message`. A tenant credential's are _call's words, except
    ``bad_created_by``, which is said in the same one line. *token_source*, *no_token* and *unauthorized* word the lines
    that name the caller's token."""
    if getattr(env.client, "scope", None) != SCOPE_DATA:
        try:
            return call()
        except IdentityApiError as exc:
            # bad_created_by is the one refusal a tenant credential's seat call earns for itself; the rest are _call's words
            said = seat_refusal_message(exc, identity) if exc.code == "bad_created_by" else f"{exc.code or exc.status}: {exc.message}"
            raise IdentityCommandError(said) from exc
        except IdentityUnavailableError as exc:
            raise IdentityCommandError(f"the engine is not answering ({exc})") from exc
    try:
        return call()
    except IdentityApiError as exc:
        raise IdentityCommandError(seat_refusal_message(exc, identity, token_source=token_source, no_token=no_token,
                                                            unauthorized=unauthorized)) from exc
    except IdentityUnavailableError as exc:
        raise IdentityCommandError(f"the engine is not answering ({exc})") from exc


def _seat_token(env: IdentityEnv, token_file: Path | None = None, *, required: bool = False) -> TokenFile | None:
    """The identity token a seat call carries: none for a tenant credential (its calls are unchanged); for the data
    credential the token file *token_file* names (a session that is not the caller; it wins over the caller's own),
    else the caller's own, which the session check has passed. A file that holds no live token, or *required* with no
    token at all, is refused before any call."""
    if getattr(env.client, "scope", None) != SCOPE_DATA:
        return None
    if token_file is not None:
        record = read_token_file(token_file)
        if record is None or not token_contract.usable(record.token, record.expires_at, now=env.clock()):
            raise IdentityCommandError(f"{token_file} holds no live identity token")
        return record
    from nexus.identity.resolver import IdentityConflictError  # noqa: PLC0415 — deferred, as in own_identity

    try:
        record = env.own_token() if env.own_token is not None else None
    except IdentityConflictError as exc:
        raise IdentityCommandError(str(exc)) from exc
    if record is None and required:
        raise IdentityCommandError("this process has no live identity token, and in cloud mode a seat is made only through "
                                   "a live session's own identity token")
    return record


def _token_source(token_file: Path | None) -> str:
    return f"the token in {token_file}" if token_file is not None else OWN_TOKEN


def create(env: IdentityEnv, name: str, created_by: str = "sam") -> list[str]:
    token = _seat_token(env, required=True)
    view = _seat_call(env, lambda: env.client.create_seat(name, created_by=created_by, token=token), None,
                      no_token=CREATE_NO_TOKEN)
    stamped = view.get("created_by") or created_by   # in cloud mode the engine stamps identity:<id> from the token
    return [f"created seat {name} ({view['id']}), created by {stamped}"]


def _reserved(name: str) -> None:
    if _ID.match(name):
        raise IdentityCommandError(f"{name!r} is reserved: names that look like identity ids (i<digits>) cannot be aliases")


def promote(env: IdentityEnv, identity: str, name: str, token_file: Path | None = None) -> list[str]:
    _reserved(name)
    token = _seat_token(env, token_file)
    view = _seat_call(env, lambda: env.client.promote(identity, name, token=token), identity,
                      token_source=_token_source(token_file),
                      unauthorized=OWN_TOKEN_401_ADVICE if token_file is None else FILE_TOKEN_401_ADVICE)
    lines = [f"{view['id']} is now the seat {name}"]
    if "board_entries_retitled" in view:
        # Engine 0.1.198 on: the promote retitled the board entry in its own transaction, whoever ran the command, and
        # says what it did. An engine that answered is not second-guessed by a client write.
        if view.get("board_retitle") == "title_taken":
            lines.append(f"board entry keeps its title: an entry titled {name!r} already exists")
        elif view["board_entries_retitled"]:
            lines.append(f"board entry retitled to {name!r}")
        return lines
    # An older engine's promote does not retitle: the client does it, best effort, and only inside the session that
    # owns the entry (the board fence lets nobody else write it).
    if env.board_memory is not None:
        from nexus.identity.board import retitle_entry  # noqa: PLC0415 — deferred: CLI startup cost

        try:
            with env.board_memory() as memory:
                note = retitle_entry(memory, str(view["id"]), name)
        except Exception as exc:  # noqa: BLE001 — the engine promoted: no T2 handle never undoes that
            note = f"board entry not retitled: {type(exc).__name__}: {exc}"
        if note:
            lines.append(note)
    return lines


def rename(env: IdentityEnv, old: str, new: str) -> list[str]:
    _reserved(new)
    view = _call(env.client.rename, old, new)
    retitled = view.get("board_entries_retitled")
    return [f"renamed {view.get('renamed_from', old)} to {new} ({view['id']}); the old name keeps resolving for 7 days"
            + (f", {retitled} board entry retitled" if retitled else "")]


def purge(env: IdentityEnv, ref: str) -> list[str]:
    view = find(env, ref)
    state = view.get("state")
    if state in ("live", "unheld"):
        raise IdentityCommandError(
            f"{view['id']} is {state}: purge removes the state of an ended identity, or of a lapsed one whose holder is "
            f"recorded gone ('nx identity alive' records it)")
    if state == "lapsed" and not view.get("gone_at"):
        raise IdentityCommandError(
            f"{view['id']} is lapsed with no recorded gone: a sleeping host may still wake and renew it. "
            f"'nx identity alive' records a verdict for a holder on this host.")
    answer = _call(env.client.purge, view["id"])
    return [f"purged {view['id']}: {answer}"]


# ── show ────────────────────────────────────────────────────────────────────


def own_identity(env: IdentityEnv) -> dict[str, Any] | None:
    """The identity of the caller's own Claude process: the resolver's answer (an explicit delegated token, the
    session id, the process's token file), else the engine's binding for that process."""
    from nexus.identity.resolver import IdentityConflictError, resolve_identity  # noqa: PLC0415

    explicit = (env.environ.get(NX_IDENTITY_TOKEN_ENV) or "").strip()
    if explicit and read_token_file(Path(explicit)) is None:
        # A delegated child whose token file names no identity (a sentinel: its dispatcher made none) holds none. The
        # resolver also answers None here; returning early skips the binding lookup by pid below as well.
        return None
    facts = env.caller()
    claude_pid = facts.claude_pid if facts.claude_pid > 0 else None
    try:
        resolved = resolve_identity(client=env.client, env=env.environ, claude_pid=claude_pid,
                                    sessions_dir=env.procs.sessions_dir, identity_dir=env.identity_dir, host=env.host)
    except IdentityConflictError as exc:
        raise IdentityCommandError(str(exc)) from exc
    except IdentityApiError as exc:
        raise IdentityCommandError(f"{exc.code or exc.status}: {exc.message}") from exc
    if resolved is not None:
        return dict(_call(env.client.show, resolved.identity))
    if claude_pid is None:
        return None
    entry = read_registry_entry(claude_pid, env.procs.sessions_dir)
    if entry is None:
        return None
    for view in _call(bindings_here, env.client, env.host, claude_pid, env.identity_dir):
        holder = view.get("holder") or {}
        if holder.get("pid") == claude_pid and holder.get("started_at") == entry.started_at:
            return dict(view)
    return None


def show_view(env: IdentityEnv, ref: str | None, session: str | None) -> dict[str, Any]:
    if session:
        indexed = _call(env.client.session, session)
        return dict(_call(env.client.show, indexed["identity"]))
    if ref:
        return find(env, ref)
    view = own_identity(env)
    if view is None:
        raise IdentityCommandError("this process holds no identity (it has no bound Claude session ancestor)")
    return view


def show_lines(env: IdentityEnv, view: Mapping[str, Any]) -> list[str]:
    name = view.get("name")
    head = f"{view['id']} {name or '(no alias)'} ({view.get('kind')})"
    if view.get("display_name") and view.get("display_name") != name:
        head += f", display name {view['display_name']}"
    if view.get("parent"):
        head += f", delegated by {view['parent']}"
    from nexus.identity.board import board_title  # noqa: PLC0415 — deferred: CLI startup cost

    title = board_title(identity=str(view["id"]), alias=view.get("name"), display_name=view.get("display_name"))
    lines = [head,
             f"  board title {title}",
             f"  state {view.get('state')}, liveness {view.get('liveness')}, generation {view.get('generation')}, "
             f"lease {view.get('lease')}",
             f"  {_holder_line(view)}",
             f"  client version {view.get('client_version') or 'unknown'}"]
    if view.get("reserved_until"):
        lines.append(f"  reserved until {view['reserved_until']}")
    rows = _call(env.client.log, view["id"])
    lines.append("  recent binding rows:")
    for row in rows[-RECENT_ROWS:]:
        sid = f" session {row['session_id']}" if row.get("session_id") else ""
        lines.append(f"    #{row.get('seq')} {row.get('event')} generation {row.get('generation')}{sid}")
    if not rows:
        lines.append("    (none)")
    return lines


def _live_unbound(env: IdentityEnv, bound: set[tuple[Any, Any]]) -> list[Any]:
    """Registry entries of Claude processes running on this host that no binding names. A ``claude -p`` child
    (``entrypoint: sdk-cli``) is left out: the identity of a delegated child is bound to its dispatcher's process, and a
    tool-free child has none, so neither would ever carry a binding of its own and each would read as a pre-Phase-1
    client."""
    out: list[Any] = []
    try:
        files = sorted(Path(env.procs.sessions_dir).glob("*.json"))
    except OSError:
        return out
    for path in files:
        if not path.stem.isdigit():
            continue
        pid = int(path.stem)
        entry = read_registry_entry(pid, env.procs.sessions_dir)
        if entry is None or entry.entrypoint == SDK_ENTRYPOINT or not env.procs.alive(pid):
            continue
        started = normalise_instant(env.procs.os_start(pid))
        if entry.proc_start and started and entry.proc_start != started:
            continue  # the pid was reused: the registry file is a dead process's
        if (pid, entry.started_at) not in bound:
            out.append(entry)
    return out


def show_all_lines(env: IdentityEnv) -> list[str]:
    """The bindings held on this host and the live Claude processes here that have none (Phase 2's gate reads this:
    every live process must have a binding at the Step 5 release or later). The engine has no route that lists unheld
    seats, so only identities with a holder on this host appear."""
    views = _call(bindings_here, env.client, env.host, None, env.identity_dir)
    lines = [f"bindings on {describe_host(env.host)}:"]
    bound: set[tuple[Any, Any]] = set()
    for view in views:
        holder = view.get("holder") or {}
        bound.add((holder.get("pid"), holder.get("started_at")))
        lines.append(f"  {view['id']} {_name(view)} ({view.get('kind')}) {view.get('state')}, generation {view.get('generation')}, "
                     f"pid {holder.get('pid')}, client {view.get('client_version') or 'unknown'}")
    if not views:
        lines.append("  (none)")
    stray = _live_unbound(env, bound)
    if stray:
        lines.append("live Claude processes with no binding:")
        for entry in stray:
            lines.append(f"  pid {entry.pid} {entry.name or '(no name)'} ({entry.entrypoint}), session {entry.session_id}")
    else:
        lines.append("every live Claude process on this host has a binding (claude -p children are not counted)")
    return lines


#: The Step 5 release (conexus 8.5.0, tagged 2026-11-19): RDR-253 Phase 2's gate needs every binding at it or later.
PHASE2_CLIENT_FLOOR = "8.5.0"


def _release(version: Any) -> tuple[int, int, int] | None:
    """``X.Y.Z`` as a tuple; anything else (none, ``unknown``, a pre-release) is None, which the gate reads as below."""
    parts = str(version or "").strip().split(".")
    return tuple(int(p) for p in parts) if len(parts) == 3 and all(p.isdigit() for p in parts) else None  # type: ignore[return-value]


def phase2_gate(env: IdentityEnv, *, floor: str = PHASE2_CLIENT_FLOOR) -> tuple[bool, list[str]]:
    """RDR-253 Phase 2's gate, coded: open only when every live Claude process on this host (Desktop included; a
    ``claude -p`` child is not counted, its binding is its dispatcher's) holds a binding, and every binding held here
    reports a client at *floor* or later. Returns the verdict and the lines that say why, ending in ``PHASE2_GATE_OPEN``
    or ``PHASE2_GATE_CLOSED``."""
    views = _call(bindings_here, env.client, env.host, None, env.identity_dir)
    want = _release(floor)
    lines = [f"bindings on {describe_host(env.host)}:"]
    bound: set[tuple[Any, Any]] = set()
    blockers = 0
    for view in views:
        holder = view.get("holder") or {}
        bound.add((holder.get("pid"), holder.get("started_at")))
        version = view.get("client_version")
        have = _release(version)
        running = isinstance(holder.get("pid"), int) and env.procs.alive(holder["pid"])
        low = running and (want is None or have is None or have < want)
        blockers += low
        note = f": below {floor}" if low else "" if running else ": holder not running, not counted"
        lines.append(f"  {view['id']} {_name(view)} ({view.get('kind')}) pid {holder.get('pid')}, client {version or 'unknown'}{note}")
    if not views:
        lines.append("  (none)")
    stray = _live_unbound(env, bound)
    for entry in stray:
        lines.append(f"  no binding: pid {entry.pid} {entry.name or '(no name)'} ({entry.entrypoint}), session {entry.session_id}")
    blockers += len(stray)
    verdict = "PHASE2_GATE_OPEN" if not blockers else f"PHASE2_GATE_CLOSED {blockers} blocker(s)"
    lines.append(f"{verdict}: every live Claude process bound at client {floor} or later"
                 + ("" if not blockers else " does not hold"))
    return not blockers, lines


# ── alive ───────────────────────────────────────────────────────────────────


def judge(env: IdentityEnv, view: Mapping[str, Any]) -> Verdict | None:
    """The holder check for *view*'s holder; a delegated identity's holder is its dispatcher process."""
    holder = view.get("holder")
    if not isinstance(holder, Mapping):
        return None
    if view.get("kind") == "delegated":
        return check_dispatcher(holder, procs=env.procs, host=env.host, identity_dir=env.identity_dir,
                                identity=str(view.get("id")))
    return check_holder(holder, procs=env.procs, host=env.host, identity_dir=env.identity_dir)


def is_alive(env: IdentityEnv, identity_id: str) -> bool:
    """Whether the identity still has a live holder, for the code that reads liveness before it acts (RDR-253 Gap 4:
    worktree removal, tmux cleanup, the restart round). Read only: nothing is recorded, unlike ``nx identity alive``.

    An identity the engine does not know, one that is ended, released or unheld, and a holder the check finds gone are
    not alive. A holder running on this host, and one whose process cannot be judged (a pid that runs but whose start time
    cannot be read), are alive: a reader that cannot prove a holder gone must not act as if it were. A holder on another host
    is alive while its lease is (``state`` live). An engine that cannot be asked raises :class:`IdentityCommandError`."""
    try:
        view = env.client.show(identity_id)
    except IdentityApiError as exc:
        if exc.status == 404:
            return False
        raise IdentityCommandError(f"{exc.code or exc.status}: {exc.message}") from exc
    except IdentityUnavailableError as exc:
        raise IdentityCommandError(f"the engine is not answering ({exc})") from exc
    if view.get("state") not in ("live", "lapsed"):
        return False
    verdict = judge(env, view)
    if verdict is None or verdict.state == GONE:
        return False
    if verdict.state == OTHER_HOST:
        return view.get("state") == "live"
    return True


def alive_lines(env: IdentityEnv, ref: str | None) -> list[str]:
    if ref is None:
        return _sweep_lines(env)
    if ref.isdigit():
        views = _call(bindings_here, env.client, env.host, int(ref), env.identity_dir)
        if not views:
            raise IdentityCommandError(f"no binding for pid {ref} on {describe_host(env.host)}")
    else:
        views = [find(env, ref)]
    lines: list[str] = []
    for view in views:
        verdict = judge(env, view)
        label = f"{view['id']} {_name(view)}"
        if verdict is None:
            lines.append(f"{label}: {view.get('state')}, no process holds it")
            continue
        line = f"{label}: {verdict.state}: {verdict.reason}"
        if verdict.state == GONE:
            try:
                answer = env.client.gone(view["id"], int(view["generation"]), holder_process(view["holder"]))
                line += " (recorded)" if answer.get("recorded") else " (already recorded)"
            except IdentityApiError as exc:
                line += f" (not recorded: {exc.code or exc.status})"
            except IdentityUnavailableError as exc:
                line += f" (not recorded: the engine is not answering: {exc})"
        lines.append(line)
    return lines


def _sweep_lines(env: IdentityEnv) -> list[str]:
    facts = env.caller()
    entry = read_registry_entry(facts.claude_pid, env.procs.sessions_dir) if facts.claude_pid > 0 else None
    own = (facts.claude_pid, entry.started_at) if entry is not None else None
    report = _call(sweep_host, env.client, env.procs, host=env.host, own=own, identity_dir=env.identity_dir)
    if report.forbidden:
        return [f"checked {report.listed} bindings; this credential may not record gone verdicts"]
    delegated_gone = _call(sweep_delegated, env.client, env.procs, report.delegated_views, host=env.host, report=report,
                           identity_dir=env.identity_dir)
    if report.forbidden:
        return [f"checked {report.listed} bindings; this credential may not record gone verdicts"]
    gone = [*report.recorded_gone, *delegated_gone]
    return [f"{report.listed} bindings on {describe_host(env.host)}: {len(gone)} recorded gone"
            + (f" ({', '.join(gone)})" if gone else "")
            + f", {report.running} running, {report.unknown} not judged, {report.other_host} on other hosts, "
              f"{report.skipped_own} own, {report.skipped_delegated} delegated"]


# ── release ─────────────────────────────────────────────────────────────────


def _instant(text: object) -> float | None:
    """An engine instant (``2026-11-18T10:00:00Z``) as epoch seconds; None for anything else."""
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp()


def release(env: IdentityEnv, ref: str) -> list[str]:
    """Free a seat whose holder is gone (RDR "Take-over", release). No ancestry or interactivity guard: it binds
    nothing, so a person at a plain shell can run it."""
    view = find(env, ref)
    name = _name(view)
    if view.get("kind") != "seat":
        raise IdentityCommandError(f"{view['id']} is {view.get('kind')}, not a seat: release frees seats")
    if view.get("state") not in ("live", "lapsed"):
        return [f"{name} is not held ({view.get('state')}); nothing to release"]
    holder = view.get("holder") or {}
    evidence: dict[str, Any] = {"host": env.host}
    if holder_is_local(holder, host=env.host, identity_dir=env.identity_dir):
        verdict = judge(env, view)
        if verdict is None or verdict.state != GONE:
            why = verdict.reason if verdict else "it names no process"
            kind = "is still running" if verdict is not None and verdict.state == RUNNING else "cannot be judged to be gone"
            raise IdentityCommandError(
                f"{name} is held by pid {holder.get('pid')} on this host, which {kind} ({why}). Release frees only a gone "
                f"holder's seat. From an interactive Claude session, 'nx identity claim {name} --take-over' supersedes it.")
        evidence = evidence_body(host=env.host, holder_gone=holder_process(holder))
    else:
        until = _instant(view.get("reserved_until"))
        if view.get("state") == "live" or until is None or env.clock() < until:
            raise IdentityCommandError(
                f"{name} is held by pid {holder.get('pid')} on {describe_host(holder.get('host'))}, whose lease is live"
                + (f" (reserved until {view['reserved_until']})" if view.get("reserved_until") else "")
                + ". Release frees a seat held on another host only after its lapsed lease's 10-minute reservation.")
    _call(env.client.bind, view["id"], "release", int(view["generation"]), evidence)
    return [f"released {name} ({view['id']}): it is unheld and the next launch that names it binds it"]


# ── claim ───────────────────────────────────────────────────────────────────


def _claude_ancestor(env: IdentityEnv, facts: CallerFacts) -> Any:
    """The registry entry of the caller's nearest Claude ancestor, verified to be a running Claude process: file
    presence alone never decides (RDR "A holder that no longer exists"; a crashed Claude leaves its file behind, and
    ``find_immediate_claude_pid`` falls back to the shell itself). The entry must name this pid's ``startedAt`` and
    ``procStart``, the pid must run with that OS start time (the holder check's registry legs, reused), and its command
    must be ``claude``. None when any of that fails."""
    entry = read_registry_entry(facts.claude_pid, env.procs.sessions_dir) if facts.claude_pid > 0 else None
    if entry is None or entry.started_at is None:
        return None
    verdict = check_holder({"host": env.host, "pid": facts.claude_pid, "started_at": entry.started_at,
                            "proc_start": entry.proc_start}, procs=env.procs, host=env.host)
    if verdict.state == GONE:
        return None
    if env.command_name is not None:
        command = env.command_name(facts.claude_pid).lower()
        if command and not command.startswith("claude"):
            return None
    return entry


#: Seat states with no holder: ``claim`` binds these with ``start`` (RDR-253 Phase 2).
_UNHELD_STATES = frozenset({"unheld", "ended"})


def claim(env: IdentityEnv, ref: str) -> list[str]:
    """``claim <seat> --take-over``: supersede the seat's holder with the caller's own Claude process.

    A seat nobody holds (``unheld`` or ``ended``) is bound with ``start``, which every credential may send: a running
    session on a data-scoped credential (a Claude Desktop session, kestrel 2026-11-22) can claim a seat made for it
    without a new SessionStart. A held seat is taken over with ``takeover``, a tenant-credential event; if the seat
    became held between the read and a ``start``, the claim reads it again once and takes it over."""
    facts = env.caller()
    entry = _claude_ancestor(env, facts)
    if entry is None:
        raise IdentityCommandError(
            "no Claude session ancestor: take-over binds the Claude process it runs under, so run it from inside an "
            "interactive Claude Code session (a plain shell has none, and a registry file left by a Claude that has "
            "exited, or by a pid since reused, is not one)")
    if entry.entrypoint not in INTERACTIVE_ENTRYPOINTS:
        raise IdentityCommandError(
            f"take-over runs only from an interactive Claude session (entrypoint cli or claude-desktop); this Claude "
            f"process has entrypoint {entry.entrypoint!r} (a claude -p child is sdk-cli)")
    proc_start = normalise_instant(env.procs.os_start(facts.claude_pid))
    evidence = evidence_body(
        session_id=entry.session_id, host=env.host, pid=facts.claude_pid, started_at=entry.started_at,
        proc_start=proc_start, pane=(env.environ.get("TMUX_PANE") or "").strip() or entry.tmux,
        client_version=env.client_version, lease="unleased" if entry.desktop else "leased")
    for attempt in (1, 2):
        view = find(env, ref)
        name = _name(view)
        if view.get("kind") != "seat":
            raise IdentityCommandError(f"{view['id']} is {view.get('kind')}, not a seat: only a seat is claimed")
        holder = view.get("holder") or {}
        holder_pid = holder.get("pid")
        if (view.get("state") in ("live", "lapsed") and holder_is_local(holder, host=env.host, identity_dir=env.identity_dir)
                and holder_pid != facts.claude_pid and holder_pid in facts.ancestors):
            raise IdentityCommandError(
                f"{name} is held by pid {holder_pid}, an ancestor of this process: a child cannot take its parent's seat")
        previous = _previous(env, facts.claude_pid, entry.started_at)
        event = "start" if view.get("state") in _UNHELD_STATES else "takeover"
        try:
            bound = env.client.bind(view["id"], event, int(view["generation"]), evidence)
        except IdentityApiError as exc:
            if exc.status == 409 and attempt == 1 and (exc.code == "generation_conflict"
                                                        or (event == "start" and exc.code == "held")):
                continue  # the seat moved between the read and the bind: read it again, once (as the hook's bind does)
            raise IdentityCommandError(f"{exc.code or exc.status}: {exc.message}") from exc
        except IdentityUnavailableError as exc:
            raise IdentityCommandError(f"the engine is not answering ({exc})") from exc
        break
    token, lifetime = token_contract.token_from_response(bound)
    _write_token(env, bound, token, lifetime, entry, proc_start)
    verb = "claimed" if event == "start" else "took over"
    lines = [f"{verb} {name} ({view['id']}) at generation {bound['generation']}: pid {facts.claude_pid} holds it now"]
    if previous is not None and previous["id"] != view["id"]:
        lines.append(_close_previous(env, previous, evidence))
    return lines


def _previous(env: IdentityEnv, claude_pid: int, started_at: int) -> dict[str, Any] | None:
    for view in _call(bindings_here, env.client, env.host, claude_pid, env.identity_dir):
        holder = view.get("holder") or {}
        if view.get("kind") != "delegated" and holder.get("pid") == claude_pid and holder.get("started_at") == started_at:
            return dict(view)
    return None


def _close_previous(env: IdentityEnv, previous: Mapping[str, Any], evidence: Mapping[str, Any]) -> str:
    """An ephemeral identity the caller held ends; a seat it held is released and left unheld."""
    event = "end" if previous.get("kind") == "ephemeral" else "release"
    body = dict(evidence)
    prev_holder = previous.get("holder") or {}
    if holder_is_local(prev_holder, host=env.host, identity_dir=env.identity_dir,
                       process=(evidence.get("pid"), evidence.get("started_at"))):
        body["host"] = prev_holder["host"]   # a self-release names the holder row's own host, which may predate the stable host key
    try:
        env.client.bind(previous["id"], event, int(previous["generation"]), body if event == "release" else None)
    except IdentityApiError as exc:
        return f"note: the caller's previous identity {previous['id']} was not closed ({exc.code or exc.status}: {exc.message})"
    return f"closed the previous identity {previous['id']} ({event})"


def _write_token(env: IdentityEnv, bound: Mapping[str, Any], token: str | None, lifetime: float | None, entry: Any,
                 proc_start: str | None) -> None:
    """The caller's process token file, from the take-over's answer. The MCP server re-reads it on its heartbeat and
    adopts the new identity (RDR "Handover to the MCP server"). Session ids accumulate under one identity only."""
    path = token_path(env.identity_dir, entry.pid, entry.started_at)

    def record(current: TokenFile | None) -> TokenFile:
        earlier = current.session_ids if current is not None and current.identity == bound["id"] else ()
        ids = earlier + ((entry.session_id,) if entry.session_id and entry.session_id not in earlier else ())
        return TokenFile(identity=bound["id"], generation=int(bound["generation"]), token=token,
                         expires_at=token_contract.expires_at(lifetime, now=env.clock()), session_id=entry.session_id,
                         pid=entry.pid, started_at=entry.started_at, proc_start=proc_start, host=env.host,
                         registry_dir=str(env.procs.sessions_dir), session_ids=ids)

    update_token_file(path, record)
