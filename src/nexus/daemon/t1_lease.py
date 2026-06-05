# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-149 P4 (bead nexus-8znyd): T1 rides the leased service registry.

T1 is the per-MCP-process working-memory tier. Unlike T2/T3 it is **not**
a supervised daemon: it is owned by the MCP server's chroma lifespan
(``nexus.mcp.core._t1_chroma_lifespan`` Branch 3) and consumes the shared
``ServiceRegistry`` primitive directly. This module is that consumer.

The locked RF-2 re-key protocol (the one load-bearing correctness subtlety
of the T1 migration, tracked as CA-3):

1. At lifespan publish the owner does not yet know the Claude session-id on
   a cold top-level session: the SessionStart hook writes
   ``~/.config/nexus/current_session`` independently of the MCP lifespan
   (RDR-105 P4), so ``resolve_active_session_id()`` may still be ``None``.
   The publisher therefore claims a **transient** scope key = the chroma
   ``server_pid``: unique among live owners, never ``"unknown"``, never
   colliding across sessions. The resolved-or-None session-id rides as a
   payload field.
2. The heartbeat loop calls the injected ``session_resolver`` each tick.
   The instant it resolves non-None while the record is still
   transient-keyed, the publisher **atomically re-keys**: it publishes the
   session-id-keyed record (the primitive's per-scope election flock
   serializes the generation bump against any concurrent sibling), then
   relinquishes the transient record (which only ever unlinks the
   publisher's own ``server_pid`` key).
3. Readers resolve by session-id (:func:`discover_t1_lease`). The transient
   window covers all three reader classes: the owning MCP process (via the
   in-process ``_t1_state`` pointer), an MCP-dispatched ``claude -p``
   subprocess (via the inherited ``NX_T1_HOST``/``NX_T1_PORT`` env, RDR-105
   Path A), and a Claude-Code-spawned Bash sibling in the cold-start sliver
   before ``current_session`` is written. The sibling has no env and no
   resolvable session-id, so it matches the owner's transient record by the
   owner's immediate Claude ancestor pid (stamped in the payload, resolved
   identically on both sides via RF-6) -- session-targeted and TTL-bounded,
   so no cross-session mis-bind (:func:`discover_t1_transient_for_claude`,
   nexus-0x16i). No record is ever keyed ``"unknown"``.

Session-scoped N-per-user semantics are preserved: the session-id IS the
scope key (intentionally N owners per uid, one T1 server per session), so
unlike the uid-scoped T2/T3 tiers there is no one-owner-per-user election.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Optional

import structlog

from nexus.daemon.service_registry import (
    Clock,
    LeaseRecord,
    ServiceRegistry,
    StaleOwnerError,
    mint_owner_token,
)

_log = structlog.get_logger(__name__)

SessionResolver = Callable[[], Optional[str]]


def _t1_version() -> str:
    """The running package version, stamped on the lease for observability.

    T1 is not version-cycled (it is MCP-lifespan-owned; an upgrade cycles it
    by restarting the MCP server), but the lease ``version`` field is
    required by the primitive, so we stamp the real version for parity with
    T2/T3 discovery payloads.
    """
    try:
        from importlib.metadata import version

        return version("conexus")
    except Exception:
        return "0.0.0"


class T1LeasePublisher:
    """Owns one T1 chroma's lease, re-keyed transient ``server_pid`` ->
    ``session_id`` per the locked RF-2 protocol.

    Constructor-injected (no singletons): the MCP lifespan builds the
    ``ServiceRegistry`` and a ``session_resolver`` and hands them in; the
    conformance harness and unit tests inject a ``_FakeClock``-backed
    registry and a controllable resolver so the re-key window is exercised
    deterministically.
    """

    def __init__(
        self,
        *,
        registry: ServiceRegistry,
        server_pid: int,
        host: str,
        port: int,
        version: Optional[str] = None,
        session_resolver: SessionResolver,
        owner_token: Optional[str] = None,
        claude_pid: Optional[int] = None,
    ) -> None:
        self._registry = registry
        self._server_pid = server_pid
        self._transient_key = str(server_pid)
        self._endpoint: dict[str, Any] = {
            "host": host,
            "port": port,
            "server_pid": server_pid,
        }
        self._version = version or _t1_version()
        self._resolver = session_resolver
        # The owner's immediate Claude ancestor pid (RF-6). Carried in the
        # transient record's payload ONLY so a bare Bash sibling in the
        # cold-start window can target this owner's transient lease by the
        # same pid it resolves for itself (nexus-0x16i). Never a scope KEY
        # (keys stay server_pid/session-id); never a liveness probe (liveness
        # is lease freshness). Dropped once the lease re-keys to the session.
        self._claude_pid = claude_pid
        self._owner_token = owner_token or mint_owner_token()
        self._record: Optional[LeaseRecord] = None
        self._session_key: Optional[str] = None
        self._fenced: bool = False

    @property
    def owner_token(self) -> str:
        return self._owner_token

    @property
    def record(self) -> Optional[LeaseRecord]:
        return self._record

    @property
    def session_keyed(self) -> bool:
        """True once the lease is keyed on the session-id (re-key done)."""
        return self._session_key is not None

    @property
    def active_scope_key(self) -> str:
        """The scope key the live record is currently published under."""
        return self._session_key or self._transient_key

    @property
    def fenced(self) -> bool:
        """True if a heartbeat found a newer owner had taken the scope.

        For the transient ``server_pid`` key this cannot happen (the key is
        unique to this owner); for the session key it can if a sibling of the
        same session published a higher generation after our last publish.
        """
        return self._fenced

    def _payload(self, session_id: Optional[str]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "session_id": session_id,
            "server_pid": self._server_pid,
        }
        # Stamp the owner's immediate Claude ancestor pid into EVERY record,
        # transient and session-keyed alike (nexus-gff3g). A sibling shell whose
        # resolved session-id diverges from the owner's lease key cannot find the
        # lease by session-id, and the claude-ancestor-pid fallback is its only
        # path to its own T1. Divergence is the common case, not an edge case:
        # the MCP keys on NX_SESSION_ID while the SessionStart hook writes
        # current_session, and the two Claude-provided ids differ on resume,
        # with multiple concurrent frontends, and under version skew. The prior
        # behavior dropped the hint the instant a session-id resolved, so a warm
        # publish (NX_SESSION_ID set at MCP startup, the common config) never
        # recorded it and silently killed the fallback. Readers only ever use
        # claude_pid as a fallback AFTER the session-id path misses, so carrying
        # it on session-keyed records is free.
        if self._claude_pid is not None:
            payload["claude_pid"] = self._claude_pid
        return payload

    def publish(self) -> LeaseRecord:
        """Claim the scope, keying on the session-id if it already resolves,
        else on the transient ``server_pid``.

        Returns the published lease. On a warm session (the resolver returns
        non-None immediately, e.g. a ``claude -p`` subprocess that inherited
        ``NX_SESSION_ID``) there is no transient window and no later re-key.
        """
        session_id = self._resolve()
        scope_key = session_id or self._transient_key
        self._record = self._registry.publish(
            scope_key,
            endpoint=self._endpoint,
            version=self._version,
            owner_token=self._owner_token,
            payload=self._payload(session_id),
        )
        self._session_key = session_id or None
        _log.info(
            "t1_lease_published",
            scope_key=scope_key,
            session_keyed=self.session_keyed,
            server_pid=self._server_pid,
            generation=self._record.generation,
        )
        return self._record

    def tick(self) -> None:
        """One heartbeat step: re-stamp the lease, then re-key if the
        session-id has resolved while we are still transient-keyed.

        Idempotent and exception-safe. A heartbeat fenced by a newer owner
        sets :attr:`fenced` and writes nothing (CA-4). Called by the
        lifespan's async heartbeat loop and, in tests, directly.
        """
        if self._record is None or self._fenced:
            return
        try:
            self._record = self._registry.heartbeat(self._record)
        except StaleOwnerError:
            self._fenced = True
            _log.info(
                "t1_lease_fenced",
                scope_key=self.active_scope_key,
                owner_token=self._owner_token,
            )
            return
        if self._session_key is None:
            session_id = self._resolve()
            if session_id:
                self._rekey(session_id)

    def _rekey(self, session_id: str) -> None:
        """Atomically move the lease from the transient key to the session
        key. Write the session record first (under its own election flock,
        so a concurrent sibling's generation bump serializes), then relinquish
        the transient record (which only unlinks our own ``server_pid`` key).
        Ordering guarantees there is never a window with no record.
        """
        transient_record = self._record
        new_record = self._registry.publish(
            session_id,
            endpoint=self._endpoint,
            version=self._version,
            owner_token=self._owner_token,
            payload=self._payload(session_id),
        )
        # Commit the in-process pointer to the session record BEFORE
        # relinquishing the transient one: if the relinquish raises, the next
        # tick must heartbeat the session record (not re-enter _rekey and
        # inflate the generation / re-publish). The transient record then ages
        # out via TTL. A relinquish failure is best-effort; never re-key-loop.
        self._record = new_record
        self._session_key = session_id
        if transient_record is not None:
            try:
                self._registry.relinquish(transient_record)
            except Exception as exc:
                _log.debug(
                    "t1_lease_transient_relinquish_failed",
                    scope=self._transient_key,
                    error=str(exc),
                )
        _log.info(
            "t1_lease_rekeyed",
            from_scope=self._transient_key,
            to_scope=session_id,
            server_pid=self._server_pid,
            generation=new_record.generation,
        )

    def relinquish(self) -> None:
        """Release the lease on shutdown, marking it shutting-down first so
        discoverers stop resolving us immediately. Only unlinks the record if
        we still own it (a fenced predecessor must not clobber a successor).
        Idempotent.
        """
        if self._record is None:
            return
        record = self._record
        try:
            self._registry.mark_shutting_down(record)
            self._registry.relinquish(record)
        finally:
            self._record = None

    def _resolve(self) -> Optional[str]:
        try:
            sid = self._resolver()
        except Exception as exc:  # resolver is best-effort; never crash a tick
            _log.debug("t1_lease_session_resolve_failed", error=str(exc))
            return None
        if sid is None:
            return None
        sid = sid.strip()
        return sid or None


def discover_t1_lease(
    session_id: Optional[str],
    *,
    config_dir: Path,
    clock: Clock = time.time,
) -> Optional[tuple[str, int]]:
    """Resolve the live T1 endpoint for ``session_id``, or ``None``.

    The session-id-keyed read path that replaces the legacy
    ``find_immediate_claude_pid`` PPID-walk + ``read_t1_addr_for`` (RDR-149
    P4). Liveness is lease freshness (TTL), not pid: a dead owner's lease
    ages out, giving pid-reuse immunity. Returns ``None`` for an empty
    session-id, a missing/expired/shutdown lease, or a malformed endpoint;
    callers fall back to the env-passdown path (RDR-105 Path A) or fail loud
    at the next layer.
    """
    if not session_id or not session_id.strip():
        return None
    registry = ServiceRegistry(dir=config_dir, tier="t1", clock=clock)
    record = registry.discover(session_id.strip())
    if record is None:
        return None
    host = record.endpoint.get("host")
    port = record.endpoint.get("port")
    if not isinstance(host, str) or not isinstance(port, (int, float)):
        return None
    return host, int(port)


def discover_t1_transient_for_claude(
    claude_pid: int,
    *,
    config_dir: Path,
    clock: Clock = time.time,
) -> Optional[tuple[str, int]]:
    """Cold-start transient-window fallback for a bare Bash sibling (nexus-0x16i).

    During the sliver before the SessionStart hook writes ``current_session``,
    a sibling resolves no session-id, so :func:`discover_t1_lease` cannot find
    the owner. The owner's TRANSIENT record (still server_pid-keyed, no
    session-id) carries the owner's immediate Claude ancestor pid in its
    payload; both sides compute that pid identically (RF-6). This matches the
    fresh transient lease whose ``payload.claude_pid`` equals the sibling's own
    ``claude_pid`` and returns its endpoint.

    Session-targeted (a concurrent cold-starting session has a different
    immediate Claude ancestor, so its transient lease never matches) and
    TTL-bounded (only fresh leases are considered), so there is no
    cross-session mis-bind. Returns ``None`` if no matching fresh transient
    lease exists; the caller then fails loud (Path D). Once the owner re-keys
    to the session-id the transient record is gone and this returns ``None`` —
    by then the session-id path resolves.
    """
    if claude_pid <= 0:
        return None
    cfg = Path(config_dir)
    if not cfg.exists():
        return None
    now = clock()
    for path in cfg.glob("t1_addr.*"):
        try:
            record = LeaseRecord.from_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError):
            continue
        # nexus-gff3g: match transient AND session-keyed leases by claude_pid.
        # By the time control reaches here the session-id path
        # (discover_t1_lease) has already missed. A session-keyed lease whose
        # owner shares this sibling's immediate Claude ancestor pid is still the
        # sibling's own T1 — even though the owner's session-id label diverges
        # from what this process resolves (NX_SESSION_ID given to the MCP vs
        # current_session written by the SessionStart hook). The match stays
        # ancestor-pid-targeted (a different session has a different immediate
        # Claude ancestor) and TTL-bounded (only fresh leases), so there is no
        # cross-session mis-bind. The prior `session_id is not None: continue`
        # skip made this fallback inert for every warm/re-keyed lease.
        if record.payload.get("claude_pid") != claude_pid:
            continue
        if not record.is_fresh(now):
            continue
        host = record.endpoint.get("host")
        port = record.endpoint.get("port")
        if not isinstance(host, str) or not isinstance(port, (int, float)):
            continue
        return host, int(port)
    return None
