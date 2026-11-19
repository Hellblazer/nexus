# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A delegated identity for a ``claude -p`` child the operator dispatcher starts (RDR-253 "Delegation", Step 4,
nexus-hczdo.1.5).

The dispatcher is the process that runs ``claude -p``; the child acts as a delegated identity of its own
(``kind=delegated``, ``parent=<the dispatcher's own identity>``, generation 1, one ``start`` row), reads its parent's
identity-owned state and writes only its own. This module is the dispatcher's half:

* :meth:`DelegatedIdentity.begin` resolves the parent from what this process already holds locally (the token file
  ``NX_IDENTITY_TOKEN`` names, else its Claude ancestor's token file) and asks the engine for the delegated identity. With
  no parent identity it does nothing and makes no network call, so a dispatch from a plain shell, or in a test, is
  byte-identical to before. The binding records the DISPATCHER's pid and OS start time, because the dispatcher has no
  registry file: the SessionStart sweep and ``nx identity alive`` read them to record a ``gone`` verdict when the
  dispatcher dies (:mod:`nexus.identity.delegated`).
* the token goes to ``<config>/identity/delegated/<id>.token`` (mode 0600) and the child's environment names that file in
  ``NX_IDENTITY_TOKEN``, in place of the parent's (:func:`nexus.claude_child_env.apply_delegated_identity`); the child
  reads the token from the file, never from the variable.
* while the child runs a task renews the binding every minute and rewrites the file when it refreshes the token at half
  life; on every exit the dispatcher stops renewing, deletes the file and appends ``end`` (:meth:`finish`).

It is best effort by design: an engine that is down, or a credential that may not create a delegated identity, is one
warning, and the dispatch runs as it did before this step. A dispatch that grants the child no tools (no
``allowed_tools``, no ``mcp_servers``) gets none either: such a child cannot reach nexus, and the high-volume
stateless operators would pay three engine calls each for an identity nothing can use.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Callable, Mapping, MutableMapping
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

RENEW_INTERVAL_S: float = 60.0

#: How long the dispatcher waits for the ``end`` call on its way out (the call itself is bounded by the client's timeout).
END_TIMEOUT_S: float = 5.0


def resolve_parent_record(environ: Mapping[str, str], identity_dir: Path, procs: Any) -> Any:
    """The token file of the identity this process dispatches for: the one ``NX_IDENTITY_TOKEN`` names (this process is
    itself a delegated child), else its nearest Claude ancestor's; None when there is none. Local reads only."""
    from nexus.identity.token_file import ancestor_token_path, read_token_file  # noqa: PLC0415

    explicit = (environ.get("NX_IDENTITY_TOKEN") or "").strip()
    if explicit:
        return read_token_file(Path(explicit))
    from nexus.session import find_immediate_claude_pid  # noqa: PLC0415

    pid = find_immediate_claude_pid()
    if pid <= 0:
        return None
    path = ancestor_token_path(identity_dir, pid, procs.sessions_dir)
    return read_token_file(path) if path is not None else None


def resolve_parent(environ: Mapping[str, str], identity_dir: Path, procs: Any) -> str | None:
    """The identity this process dispatches for (:func:`resolve_parent_record`'s), or None."""
    record = resolve_parent_record(environ, identity_dir, procs)
    return record.identity if record is not None else None


class DelegatedIdentity:
    """One delegated identity: created, renewed, and ended by the dispatcher that owns it."""

    def __init__(self, *, client: Any, identity: str, generation: int, path: Path, token: str | None,
                 lifetime_s: float | None, evidence: Mapping[str, Any], clock: Callable[[], float] = time.time,
                 interval_s: float = RENEW_INTERVAL_S, owns_client: bool = False) -> None:
        self._client = client
        self._owns_client = owns_client
        self.identity = identity
        self._generation = generation
        self.path = path
        self._evidence = dict(evidence)
        self._clock = clock
        self._interval = interval_s
        self._lock = threading.Lock()
        self._closed = False
        self._token = token
        self._lifetime = lifetime_s
        self._issued = clock()
        self._task: asyncio.Future[None] | None = None

    # ── begin ───────────────────────────────────────────────────────────────

    @classmethod
    def begin(cls, *, label: str | None = None, environ: Mapping[str, str] | None = None, client: Any = None,
              procs: Any = None, identity_dir: Path | None = None, clock: Callable[[], float] = time.time,
              interval_s: float = RENEW_INTERVAL_S) -> "DelegatedIdentity | None":
        """Create the delegated identity and write its token file, or None (with at most a warning) when there is no
        parent identity or the engine will not. Blocking: call it off the event loop."""
        from nexus.identity import token_contract  # noqa: PLC0415
        from nexus.identity.bind_client import (  # noqa: PLC0415
            SCOPE_DATA, IdentityApiError, IdentityClient, IdentityUnavailableError, evidence_body,
        )
        from nexus.identity.delegated import delegated_token_path  # noqa: PLC0415
        from nexus.identity.holder import Processes, epoch_seconds, normalise_instant  # noqa: PLC0415
        from nexus.identity.token_file import TokenFile, identity_dir as _identity_dir, write_token_file  # noqa: PLC0415

        try:
            procs = procs if procs is not None else Processes()
            if identity_dir is None:
                from nexus import config  # noqa: PLC0415

                identity_dir = _identity_dir(config.nexus_config_dir())
            parent_record = resolve_parent_record(os.environ if environ is None else environ, identity_dir, procs)
            if parent_record is None:
                return None
            parent = parent_record.identity
            pid = os.getpid()
            proc_start = normalise_instant(procs.os_start(pid))
            started_at = epoch_seconds(proc_start) * 1000 if proc_start else int(clock() * 1000)
            evidence = evidence_body(host=procs.host, pid=pid, started_at=started_at, proc_start=proc_start)
            owned = client is None
            engine = IdentityClient.from_config() if owned else client
            try:
                # RDR-253 option 2: the data credential delegates only from the caller's own identity, proved by the
                # parent's token. A tenant credential's call is unchanged.
                on_behalf = ({"token": parent_record} if getattr(engine, "scope", None) == SCOPE_DATA
                             and token_contract.usable(parent_record.token, parent_record.expires_at, now=clock()) else {})
                view = engine.create_delegated(parent, display_name=label, evidence=evidence, **on_behalf)
            except BaseException:
                if owned:
                    engine.close()
                raise
            token, lifetime = token_contract.token_from_response(view)
            path = delegated_token_path(identity_dir, view["id"])
            try:
                write_token_file(path, TokenFile(
                    identity=view["id"], generation=int(view["generation"]), token=token,
                    expires_at=token_contract.expires_at(lifetime, now=clock()), session_id=None, pid=pid,
                    started_at=started_at, proc_start=proc_start, host=procs.host, registry_dir=None))
            except BaseException:
                # The identity exists and no child will ever carry its token: end it (best effort) rather than leave
                # it live until it lapses, and do not leak the client.
                try:
                    engine.bind(view["id"], "end", int(view["generation"]))
                except Exception:  # noqa: BLE001 — best effort; the dispatcher's lapse and the sweep are the backstop
                    pass
                if owned:
                    try:
                        engine.close()
                    except Exception:  # noqa: BLE001
                        pass
                raise
        except (IdentityApiError, IdentityUnavailableError) as exc:
            _log.warning("delegated_identity_not_created", error=f"{type(exc).__name__}: {exc}")
            return None
        except Exception as exc:  # noqa: BLE001 — a dispatch must never fail over identity
            _log.warning("delegated_identity_failed", error=f"{type(exc).__name__}: {exc}")
            return None
        return cls(client=engine, identity=view["id"], generation=int(view["generation"]), path=path, token=token,
                   lifetime_s=lifetime, evidence=evidence, clock=clock, interval_s=interval_s, owns_client=owned)

    # ── renewal ─────────────────────────────────────────────────────────────

    def start_renewal(self) -> None:
        """Renew in the background on the running loop until :meth:`finish`."""
        self._task = asyncio.ensure_future(self._renew_loop())

    async def _renew_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            if not await asyncio.to_thread(self.tick):
                return

    def tick(self) -> bool:
        """One renewal, and a refresh at half life (the token file is rewritten). False when the engine says the
        binding is no longer ours, or it was closed: the loop stops. A failed call is logged and tried again."""
        from nexus.identity import token_contract  # noqa: PLC0415
        from nexus.identity.bind_client import IdentityApiError, IdentityUnavailableError  # noqa: PLC0415
        from nexus.identity.token_file import TokenFile, read_token_file, write_token_file  # noqa: PLC0415

        if self._closed:
            return False
        try:
            self._client.renew(self.identity, self._generation)
            if self._lifetime is not None and self._clock() >= self._issued + self._lifetime / 2:
                view = self._client.bind(self.identity, "refresh", self._generation, self._evidence)
                token, lifetime = token_contract.token_from_response(view)
                if token:
                    with self._lock:  # only the file write is under the lock: finish() takes it for an instant, never behind HTTP
                        if self._closed:
                            return False
                        current = read_token_file(self.path)
                        base = current if current is not None else TokenFile(
                            identity=self.identity, generation=self._generation, token=None, expires_at=None,
                            session_id=None, pid=int(self._evidence["pid"]), started_at=int(self._evidence["started_at"]),
                            proc_start=self._evidence.get("proc_start"), host=self._evidence.get("host"))
                        write_token_file(self.path, TokenFile(**{**base.__dict__, "token": token,
                                                                 "expires_at": token_contract.expires_at(lifetime, now=self._clock())}))
                        self._token, self._lifetime, self._issued = token, lifetime, self._clock()
        except IdentityApiError as exc:
            if exc.status == 409 or exc.status == 404:
                _log.warning("delegated_identity_not_ours", identity=self.identity, code=exc.code)
                return False
            _log.warning("delegated_identity_renew_failed", identity=self.identity, error=str(exc))
        except IdentityUnavailableError as exc:
            _log.warning("delegated_identity_renew_failed", identity=self.identity, error=str(exc))
        return True

    # ── end ─────────────────────────────────────────────────────────────────

    def close_local(self) -> None:
        """Stop renewing and delete the token file. Instant and safe on the event loop: no HTTP, and the lock is held
        only for the flag and the unlink (a renewal in flight cannot rewrite the file afterwards)."""
        from nexus.identity.token_file import delete_token_file  # noqa: PLC0415

        if self._task is not None:
            self._task.cancel()
        with self._lock:
            self._closed = True
            delete_token_file(self.path)

    def _end(self) -> None:
        """Append ``end`` and close an owned client. HTTP, so never on the event loop; never raises."""
        from nexus.identity.bind_client import IdentityApiError, IdentityUnavailableError  # noqa: PLC0415

        try:
            self._client.bind(self.identity, "end", self._generation)
        except (IdentityApiError, IdentityUnavailableError) as exc:
            _log.warning("delegated_identity_end_failed", identity=self.identity, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            _log.warning("delegated_identity_end_failed", identity=self.identity, error=f"{type(exc).__name__}: {exc}")
        finally:
            if self._owns_client:
                try:
                    self._client.close()
                except Exception:  # noqa: BLE001
                    pass

    def finish(self) -> None:
        """Stop renewing, delete the token file, append ``end``, synchronously (a thread, a test, a callback). Never
        raises. The dispatcher uses :meth:`afinish`."""
        self.close_local()
        self._end()

    async def afinish(self, timeout_s: float | None = None) -> None:
        """What the dispatcher runs on every exit, on the event loop of an MCP server: the file goes at once, and the
        ``end`` call runs in a thread the cancellation cannot interrupt (a cancelled dispatch still ends its identity)
        while this waits at most *timeout_s* (default :data:`END_TIMEOUT_S`) for it: a hung engine stalls nobody
        else's call, and the thread is bounded by the client's own timeout. Never raises, but a cancellation of the
        caller during the wait propagates."""
        self.close_local()
        done = asyncio.ensure_future(asyncio.to_thread(self._end))
        try:
            await asyncio.wait_for(asyncio.shield(done), END_TIMEOUT_S if timeout_s is None else timeout_s)
        except asyncio.TimeoutError:
            _log.warning("delegated_identity_end_slow", identity=self.identity)


def _finish_late(done: "asyncio.Future[DelegatedIdentity | None]") -> None:
    """End the identity a cancelled :func:`start_delegation` left behind, once its thread has finished."""
    if not done.cancelled() and done.exception() is None and done.result() is not None:
        threading.Thread(target=done.result().finish, daemon=True).start()  # off the loop: this may run as it closes


def _name_no_identity(env: MutableMapping[str, str]) -> None:
    """The child gets no delegated identity: name a token file that does not exist in ``NX_IDENTITY_TOKEN``. The
    variable's presence is what stops the child's hook from binding and what makes ``nx`` send no token (the explicit
    file is read, found empty, and nothing is sent), so the child never falls back to a walk up to the parent's Claude
    process and its token file. Without it, that walk is stopped only by the child's own process being recognised as
    a Claude."""
    from nexus import config  # noqa: PLC0415
    from nexus.claude_child_env import apply_delegated_identity  # noqa: PLC0415
    from nexus.identity.delegated import no_identity_path  # noqa: PLC0415
    from nexus.identity.token_file import identity_dir  # noqa: PLC0415

    env.update(apply_delegated_identity(env, str(no_identity_path(identity_dir(config.nexus_config_dir())))))


async def start_delegation(env: MutableMapping[str, str], *, enabled: bool = True,
                           label: str | None = None) -> DelegatedIdentity | None:
    """The call site in ``claude_dispatch``: begin a delegated identity (off the loop), name its token file in *env*
    (the child's environment) and start renewing. When there is none (disabled, no parent identity, or the engine
    refused) the child's ``NX_IDENTITY_TOKEN`` names a file that does not exist, so it acts as no identity."""
    if not enabled:
        _name_no_identity(env)
        return None
    # The create runs in a thread that cannot be interrupted. A cancellation that lands while it runs must not leave
    # the identity it makes live and its token file on disk: the caller never receives the handle, so nothing else
    # would end it. So the cancelled call waits for the thread and finishes what it made, then re-raises.
    begun = asyncio.ensure_future(asyncio.to_thread(DelegatedIdentity.begin, label=label))
    try:
        delegated = await asyncio.shield(begun)
    except asyncio.CancelledError:
        try:
            late = await asyncio.shield(begun)
        except BaseException:  # noqa: BLE001 — cancelled again, or begin failed: the callback below covers the first
            begun.add_done_callback(_finish_late)
        else:
            if late is not None:
                await late.afinish()
        raise
    if delegated is None:
        _name_no_identity(env)
        return None
    from nexus.claude_child_env import apply_delegated_identity  # noqa: PLC0415

    env.update(apply_delegated_identity(env, str(delegated.path)))
    delegated.start_renewal()
    return delegated
