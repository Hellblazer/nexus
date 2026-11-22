# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The CLI HTTP layers' identity token (RDR-253 "The resolver", last sentences; Step 3 item 10).

Every ``nx`` call reads the token file of its nearest Claude ancestor process (a Bash child of the session, or the
MCP server itself) and sends the token in :data:`~nexus.identity.token_contract.IDENTITY_TOKEN_HEADER`:

* only a request the engine FENCES carries the token (:func:`is_fenced`: threads-entry and other memory writes and the
  mailbox claim operations; identity scratch has its own rule in ``http_scratch_store``: its WRITES carry it, its reads
  never do). An identity token the engine
  cannot resolve is a 401 on every route it rides, and an unfenced read must not depend on it;
* a process with no Claude ancestor, or an ancestor with no token file, sends none: behaviour is exactly as before;
* before it sends one, a process whose ``CLAUDE_CODE_SESSION_ID`` is not one the token file lists asks
  :func:`~nexus.identity.resolver.resolve_identity` whether that session still belongs to the identity the file names,
  and a Bash child started before a cross-seat ``/resume`` is refused with the resolver's message, sending nothing;
* a delegated child (``NX_IDENTITY_TOKEN`` names its token file) sends that file's token;
* a token the file records as past its expiry is not sent: the engine would answer 401 to **every** request,
  fenced or not, and an unfenced read must not break because the server has not refreshed yet. A fenced write then
  gets the engine's own ``403 identity_token_required``, which is the honest refusal;
* on **401** with a token attached the file is re-read once and the request retried once if the file now holds a
  different token (the server refreshed it, or the hook switched identity);
* on **409** ``generation_superseded`` the call stops with :class:`GenerationSupersededError`, which names the holder.
  A fenced write is never retried without a token, which JDR-002's 401 fallback would otherwise do.

The ancestor is discovered once per process, a miss included (a process with no Claude ancestor never walks the
process table again): the ancestry of a running process does not change. The registry lookup that names the token
file is repeated on every call, because its answer does change: an in-place ``/restart`` gives the same Claude pid a new
``startedAt``, hence a new token file, under any process that outlives it (the MCP server may; that it does across an in-place /restart is not yet measured).
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from nexus.identity import token_contract
from nexus.identity.hostname import describe_host
from nexus.identity.token_file import TokenFile, ancestor_token_path, identity_dir, read_token_file

NX_IDENTITY_TOKEN_ENV = "NX_IDENTITY_TOKEN"

_LOCK = threading.Lock()


class GenerationSupersededError(RuntimeError):
    """A fenced write was refused because another process holds the identity now (``409 generation_superseded``).
    Not an ``HTTPStatusError``: no retry ladder resends it. ``holder`` is the engine's ``{host, pid}`` of the
    current holder, when it sent one."""

    def __init__(self, identity: str | None, token_generation: int | None, current_generation: int | None,
                 holder: Mapping[str, Any] | None, detail: str = "") -> None:
        self.identity = identity
        self.token_generation = token_generation
        self.current_generation = current_generation
        self.holder = dict(holder) if holder else None
        where = f" (pid {holder.get('pid')} on {describe_host(holder.get('host'))})" if holder else ""
        super().__init__(
            f"this process no longer holds identity {identity}: generation {current_generation} is held by another "
            f"process{where}, and this process's token is for generation {token_generation}. It must stop writing "
            f"{identity}'s state. Restart the session, /resume it, or take the seat over with "
            f"`nx identity claim <seat> --take-over`. {detail}".strip())


class IdentityNotHeldError(RuntimeError):
    """A fenced write was refused because nobody holds the identity now (``409 identity_not_held``: it was released,
    ended or never bound). ``state`` is the engine's word for it; ``holder`` is set when the engine named one."""

    def __init__(self, identity: str | None, state: str | None, holder: Mapping[str, Any] | None = None) -> None:
        self.identity = identity
        self.state = state
        self.holder = dict(holder) if holder else None
        where = f" Its last holder was pid {holder.get('pid')} on {describe_host(holder.get('host'))}." if holder else ""
        super().__init__(f"identity {identity} is {state or 'not held'}: no process holds it, so this write is refused.{where} "
                         f"Bind it again by restarting or /resume-ing the session.")


def superseded_from_body(status: int, body: Any) -> GenerationSupersededError | IdentityNotHeldError | None:
    """The typed error for a ``409 generation_superseded`` or ``409 identity_not_held`` answer; None for every other."""
    if isinstance(body, Mapping) and status == 409 and body.get("error") == token_contract.NOT_HELD_CODE:
        holder = body.get("holder")
        return IdentityNotHeldError(body.get("identity"), body.get("state"), holder if isinstance(holder, Mapping) else None)
    if not isinstance(body, Mapping) or not token_contract.is_superseded(status, body.get("error")):
        return None
    holder = body.get("holder")
    return GenerationSupersededError(
        body.get("identity"), body.get("token_generation"), body.get("current_generation"),
        holder if isinstance(holder, Mapping) else None)


def superseded_from_response(resp: Any) -> GenerationSupersededError | IdentityNotHeldError | None:
    """:func:`superseded_from_body` for an httpx response; an unreadable body is never this error."""
    try:
        return superseded_from_body(resp.status_code, resp.json())
    except Exception:  # noqa: BLE001 — the generic error stands
        return None


_ANCESTOR: dict[str, Any] = {}


def _ancestor() -> tuple[int, Path] | None:
    """``(Claude pid, identity directory)`` of this process's nearest Claude ancestor, or None for a process with none.
    Found once and remembered, a miss too: it costs a walk of the process table."""
    with _LOCK:
        if "found" in _ANCESTOR:
            return _ANCESTOR["found"]
    from nexus import config as _nx_config  # noqa: PLC0415 — deferred: leaf import cost; a module attribute so a patched config dir reaches it
    from nexus.session import _command_name_of, find_immediate_claude_pid  # noqa: PLC0415

    pid = find_immediate_claude_pid()
    found = ((pid, identity_dir(_nx_config.nexus_config_dir()))
             if pid > 0 and _command_name_of(pid).lower().startswith("claude") else None)
    with _LOCK:
        _ANCESTOR["found"] = found
    return found


def _discover() -> Path | None:
    """The token file of this process's Claude ancestor: its pid, and the ``startedAt`` its registry file records.
    None when there is no Claude ancestor or no registry file for it (yet)."""
    explicit = (os.environ.get(NX_IDENTITY_TOKEN_ENV) or "").strip()
    if explicit:
        return Path(explicit)
    ancestor = _ancestor()
    return None if ancestor is None else ancestor_token_path(ancestor[1], ancestor[0])


def token_file_path() -> Path | None:
    """The path of this process's token file, named by the registry file of its Claude ancestor on EVERY call. The
    ancestor (a pid) is cached; the name is not: an in-place ``/restart`` keeps the pid and gives the registry file a new
    ``startedAt``, so the next generation's token is in a new file, and a long-lived process (the MCP server) that kept the
    first name it found kept reading the superseded generation's file (nexus-hczdo). The lookup is one small JSON read."""
    return _discover()


def reset_for_tests() -> None:
    global _RESOLVER_DOWN_UNTIL
    with _LOCK:
        _RESOLVER_DOWN_UNTIL = 0.0
        _ANCESTOR.clear()
        _VERIFIED.clear()


#: How long a process waits before it reads its registry and token file ONE more time when the first read found no
#: usable file (seconds). Claude Code writes its registry file as it restarts, so a read can land mid-write.
REREAD_DELAY_S: float = 0.01


def _pause(seconds: float) -> None:
    time.sleep(seconds)


def _read() -> tuple[Path | None, TokenFile | None]:
    """The token file path this process's registry names right now, and its record (None where there is no readable one)."""
    path = token_file_path()
    return path, (read_token_file(path) if path is not None else None)


def current(*, now: float | None = None) -> TokenFile | None:
    """This process's token file record when it holds a token that may be sent now, else None."""
    return _current_with_path(now=now)[1]


def recorded() -> TokenFile | None:
    """This process's token file record whether or not its token may be sent now: what the file says about the process
    (its identity, its session id) stays true after the token expires. :func:`current` is for sending."""
    record = _read()[1]
    if record is None and has_claude_ancestor():
        _pause(REREAD_DELAY_S)
        record = _read()[1]
    return record


def _current_with_path(*, now: float | None = None) -> tuple[Path | None, TokenFile | None]:
    path, record = _read()
    if record is None and has_claude_ancestor():
        _pause(REREAD_DELAY_S)         # a registry or token file Claude is mid-write on: read once more, by name, never by the last name
        path, record = _read()
    if record is None or not token_contract.usable(record.token, record.expires_at, now=now):
        return path, None
    return path, record


def has_claude_ancestor() -> bool:
    """Whether this process has a token file to look for: a Claude ancestor, or a delegated child's explicit file. A process
    with neither has no identity and is never made to wait for one."""
    return bool((os.environ.get(NX_IDENTITY_TOKEN_ENV) or "").strip()) or _ancestor() is not None


_MEMORY_WRITES = frozenset({"put", "put_or_merge", "merge", "import", "import_batch", "delete", "summaries", "restore"})
_MAILBOX_CLAIMS = frozenset({"in", "inp", "ack", "nack", "renew", "release"})


def is_fenced(method: str, path: str) -> bool:
    """Whether the engine fences this request (RDR-253 "Fencing"): a T2 memory write (the threads entry is the fenced
    project; the others ignore the token) or a mailbox claim operation. A read is never checked."""
    if method.upper() == "GET":
        return False
    parts = [p for p in path.split("/") if p]
    if len(parts) < 3 or parts[0] != "v1":
        return False
    if parts[1] == "memory":
        return parts[-1] in _MEMORY_WRITES
    if parts[1] == "tuples":
        return parts[-1] in _MAILBOX_CLAIMS
    return False


def is_send(method: str, path: str) -> bool:
    """A mailbox send (``out``): open to any caller, so never refused, but it carries the token when the file holds a live
    one, so ``sender_session`` is stamped from the identity's current binding (RDR-253 Step 2, ``TupleHandler``)."""
    parts = [p for p in path.split("/") if p]
    return method.upper() != "GET" and parts[:3] == ["v1", "tuples", "out"]


_VERIFIED: set[tuple[str, str]] = set()
_LEASE_OWNER = False
_RESOLVER_DOWN_UNTIL = 0.0
#: How long an engine that could not be asked is not asked again (seconds): a session-id check is a courtesy, and a
#: process whose engine is down must not pay a connect timeout on every write.
RESOLVER_BACKOFF_S: float = 30.0


def set_lease_owner(owner: bool) -> None:
    """Called by the MCP server's identity lease: this process owns the binding, so its spawn-time session id is not
    evidence of anything. After a cross-seat ``/resume`` the server still has the session id it started with (it never
    sees the new one), which the index maps to the old identity; checking it would refuse every write of the server."""
    global _LEASE_OWNER
    _LEASE_OWNER = owner


def _resolver_client() -> Any:
    from nexus.identity.bind_client import IdentityClient  # noqa: PLC0415 — deferred: only a session id the file does not list asks

    return IdentityClient.from_config(timeout=3.0)


def _verify_session(record: TokenFile, path: Path | None) -> None:
    """Refuse (raise the resolver's error) when this process's session id indexes to a different identity than its
    token file names: a Bash child started before a cross-seat ``/resume``. A session id the file lists is known
    without asking; an id it does not list (a ``/branch`` the lease has not yet recorded) is asked of the engine,
    once. An engine that cannot be asked does not block the write: the fence is the engine's, this is the early,
    readable refusal."""
    global _RESOLVER_DOWN_UNTIL
    sid = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if _LEASE_OWNER or time.monotonic() < _RESOLVER_DOWN_UNTIL:
        return
    if (not sid or sid == record.session_id or sid in record.session_ids or (sid, record.identity) in _VERIFIED
            or (os.environ.get(NX_IDENTITY_TOKEN_ENV) or "").strip()):
        return
    from nexus.identity.resolver import (  # noqa: PLC0415
        SOURCE_TOKEN_FILE, Answer, IdentityConflictError, resolve_identity,
    )

    ancestor = _ancestor()
    try:
        client = _resolver_client()
    except Exception:  # noqa: BLE001 — cannot ask: send, and let the engine's fence decide
        _RESOLVER_DOWN_UNTIL = time.monotonic() + RESOLVER_BACKOFF_S
        return
    try:
        got = resolve_identity(client=client, env={"CLAUDE_CODE_SESSION_ID": sid}, claude_pid=ancestor[0] if ancestor else None)
    except Exception:  # noqa: BLE001 — an engine that cannot answer does not block the write
        _RESOLVER_DOWN_UNTIL = time.monotonic() + RESOLVER_BACKOFF_S
        return
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    if got is not None and got.identity != record.identity:
        raise IdentityConflictError([*got.answers, Answer(SOURCE_TOKEN_FILE, record.identity, record.generation,
                                                          str(path))])
    with _LOCK:
        _VERIFIED.add((sid, record.identity))


def verified_current() -> TokenFile | None:
    """:func:`current`, after the session check (:func:`_verify_session`): the record of a token this process may send
    for its own session, else None. Raises ``IdentityConflictError`` for a process whose session id names another
    identity than its token file. What a fenced write and a seat call (RDR-253 option 2) both send the token of."""
    path, record = _current_with_path()
    if record is not None:
        _verify_session(record, path)
    return record


def identity_headers() -> dict[str, str]:
    """The header for a request the caller already knows is fenced (the scratch store's identity scope), with the same
    session check as :func:`fenced_identity_headers`."""
    record = verified_current()
    return {} if record is None else token_contract.headers(record.token)


def fenced_identity_headers(method: str, path: str) -> dict[str, str]:
    """The identity header for this request: the token of this process's token file when the engine fences the request
    (or it is a send, which only stamps) and the file holds a live token and the process's session still belongs to the
    file's identity; else ``{}``. A fenced request raises ``IdentityConflictError`` for a process whose session id names
    another identity than its token file; a send does not raise, it goes without the token."""
    if is_fenced(method, path):
        return identity_headers()
    if is_send(method, path):
        try:
            return identity_headers()
        except Exception:  # noqa: BLE001 — a send is open to anyone: a stale child sends it unstamped rather than not at all
            return {}
    return {}


def retry_headers_after_401(sent: Mapping[str, str], path: str = "") -> dict[str, str] | None:
    """After a 401 on a request that carried an identity token: re-read the file once. The complete headers to resend
    with: *sent* carrying the file's new token when it holds a different, usable one; for a send (``out``), which any
    caller may make, *sent* without the token when nothing newer exists (once: the stamp is a courtesy and the mail is
    not). None otherwise: a fenced write is never resent without its token."""
    attached = sent.get(token_contract.IDENTITY_TOKEN_HEADER)
    if not attached:
        return None
    record = current(now=time.time())
    if record is not None and record.token != attached:
        return {**sent, **token_contract.headers(record.token)}
    if is_send("POST", path):
        return {k: v for k, v in sent.items() if k != token_contract.IDENTITY_TOKEN_HEADER}
    return None


def request_with_identity(
    send: Callable[[Mapping[str, str]], Any], gate: Callable[[], None], auth: Callable[[], Mapping[str, str]], method: str,
    path: str
) -> Any:
    """Send one request with the identity token when it is fenced, and on a 401 resend once with the headers
    :func:`retry_headers_after_401` gives. *gate* runs before each attempt (the request gate); *auth* builds the
    ``Authorization`` headers and is called after the first gate (it may refresh a credential, which a request the gate
    is holding must not have done before it was admitted); *send* takes the complete headers and returns the response."""
    gate()
    resp = send(sent := {**auth(), **fenced_identity_headers(method, path)})
    if resp.status_code == 401 and (fresh := retry_headers_after_401(sent, path)):
        gate()
        resp = send(fresh)
    return resp
