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


def resolve_parent(environ: Mapping[str, str], identity_dir: Path, procs: Any) -> str | None:
    """The identity this process dispatches for: the token file ``NX_IDENTITY_TOKEN`` names (this process is itself a
    delegated child), else the token file of its nearest Claude ancestor. Local reads only."""
    from nexus.identity.token_file import ancestor_token_path, read_token_file  # noqa: PLC0415

    explicit = (environ.get("NX_IDENTITY_TOKEN") or "").strip()
    if explicit:
        record = read_token_file(Path(explicit))
        return record.identity if record is not None else None
    from nexus.session import find_immediate_claude_pid  # noqa: PLC0415

    pid = find_immediate_claude_pid()
    if pid <= 0:
        return None
    path = ancestor_token_path(identity_dir, pid, procs.sessions_dir)
    record = read_token_file(path) if path is not None else None
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
            IdentityApiError, IdentityClient, IdentityUnavailableError, evidence_body,
        )
        from nexus.identity.delegated import delegated_token_path  # noqa: PLC0415
        from nexus.identity.holder import Processes, epoch_seconds, normalise_instant  # noqa: PLC0415
        from nexus.identity.token_file import TokenFile, identity_dir as _identity_dir, write_token_file  # noqa: PLC0415

        try:
            procs = procs if procs is not None else Processes()
            if identity_dir is None:
                from nexus import config  # noqa: PLC0415

                identity_dir = _identity_dir(config.nexus_config_dir())
            parent = resolve_parent(os.environ if environ is None else environ, identity_dir, procs)
            if parent is None:
                return None
            pid = os.getpid()
            proc_start = normalise_instant(procs.os_start(pid))
            started_at = epoch_seconds(proc_start) * 1000 if proc_start else int(clock() * 1000)
            evidence = evidence_body(host=procs.host, pid=pid, started_at=started_at, proc_start=proc_start)
            owned = client is None
            engine = IdentityClient.from_config() if owned else client
            try:
                view = engine.create_delegated(parent, display_name=label, evidence=evidence)
            except BaseException:
                if owned:
                    engine.close()
                raise
            token, lifetime = token_contract.token_from_response(view)
            path = delegated_token_path(identity_dir, view["id"])
            write_token_file(path, TokenFile(
                identity=view["id"], generation=int(view["generation"]), token=token,
                expires_at=token_contract.expires_at(lifetime, now=clock()), session_id=None, pid=pid,
                started_at=started_at, proc_start=proc_start, host=procs.host, registry_dir=None))
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

        with self._lock:
            if self._closed:
                return False
            try:
                self._client.renew(self.identity, self._generation)
                if self._lifetime is not None and self._clock() >= self._issued + self._lifetime / 2:
                    view = self._client.bind(self.identity, "refresh", self._generation, self._evidence)
                    token, lifetime = token_contract.token_from_response(view)
                    if token:
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

    def finish(self) -> None:
        """Stop renewing, delete the token file, append ``end``. Runs on every exit and never raises (the dispatch's
        own outcome is not changed by it). Synchronous, as the dispatcher's other cleanup is: it may run inside a
        cancelled task."""
        from nexus.identity.bind_client import IdentityApiError, IdentityUnavailableError  # noqa: PLC0415
        from nexus.identity.token_file import delete_token_file  # noqa: PLC0415

        if self._task is not None:
            self._task.cancel()
        with self._lock:  # a renewal tick in flight finishes first and cannot rewrite the file afterwards
            self._closed = True
            delete_token_file(self.path)
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


def _finish_late(done: "asyncio.Future[DelegatedIdentity | None]") -> None:
    """End the identity a cancelled :func:`start_delegation` left behind, once its thread has finished."""
    if not done.cancelled() and done.exception() is None and done.result() is not None:
        done.result().finish()


async def start_delegation(env: MutableMapping[str, str], *, enabled: bool = True,
                           label: str | None = None) -> DelegatedIdentity | None:
    """The call site in ``claude_dispatch``: begin a delegated identity (off the loop), name its token file in *env*
    (the child's environment) and start renewing. None, with *env* untouched, when disabled or when there is no
    parent identity."""
    if not enabled:
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
                late.finish()
        raise
    if delegated is None:
        return None
    from nexus.claude_child_env import apply_delegated_identity  # noqa: PLC0415

    env.update(apply_delegated_identity(env, str(delegated.path)))
    delegated.start_renewal()
    return delegated
