# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit coverage for ``discover_t1_lease`` / ``discover_t1_by_claude_ancestor``
(nexus-yfh5x): the surviving read-path of ``nexus.daemon.t1_lease`` after
``T1LeasePublisher`` was retired as dead production code (confirmed by an
exhaustive grep: it was never constructed outside its own now-deleted test
suite). These two functions remain live -- ``discover_t1_lease`` is called
by ``nx doctor --check-t1`` (:mod:`nexus.commands.doctor`) -- so their
coverage survives the retirement, just re-pointed at ``ServiceRegistry.publish``
directly instead of the deleted publisher wrapper for building fixture state.

An injected clock (no real chroma server, no real SessionStart hook) drives
TTL-expiry assertions deterministically.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from nexus.daemon.service_registry import LeaseRecord, ServiceRegistry, mint_owner_token
from nexus.daemon.t1_lease import discover_t1_by_claude_ancestor, discover_t1_lease

_SERVER_PID = 4242
_HOST = "127.0.0.1"
_PORT = 54847


class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


def _registry(config_dir: Path, clock: _FakeClock) -> ServiceRegistry:
    return ServiceRegistry(
        dir=config_dir, tier="t1", clock=clock, ttl=3.0, heartbeat_interval=1.0
    )


def _publish_lease(
    registry: ServiceRegistry,
    *,
    scope_key: str,
    server_pid: int = _SERVER_PID,
    session_id: Optional[str] = None,
    claude_pid: Optional[int] = None,
    host: str = _HOST,
    port: int = _PORT,
) -> LeaseRecord:
    """Publish a ``t1_addr.<scope_key>`` record directly through the shared
    ``ServiceRegistry`` primitive -- the same call ``T1LeasePublisher.publish``
    used to make internally, without needing the (now-deleted) wrapper."""
    payload: dict[str, Any] = {"session_id": session_id, "server_pid": server_pid}
    if claude_pid is not None:
        payload["claude_pid"] = claude_pid
    return registry.publish(
        scope_key,
        endpoint={"host": host, "port": port, "server_pid": server_pid},
        version="1.0.0",
        owner_token=mint_owner_token(),
        payload=payload,
    )


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    cd = tmp_path / "cfg"
    cd.mkdir(parents=True, exist_ok=True, mode=0o700)
    return cd


@pytest.fixture
def clock() -> _FakeClock:
    return _FakeClock()


class TestDiscoverReader:
    def test_discover_resolves_session_keyed_endpoint(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        reg = _registry(config_dir, clock)
        _publish_lease(reg, scope_key="sess-A", session_id="sess-A")

        addr = discover_t1_lease("sess-A", config_dir=config_dir, clock=clock)
        assert addr == (_HOST, _PORT)

    def test_discover_none_when_no_session_record(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        reg = _registry(config_dir, clock)
        # Transient record only, keyed on server_pid, no session_id.
        _publish_lease(reg, scope_key=str(_SERVER_PID), session_id=None)
        # A sibling resolving by session-id finds nothing during the
        # transient window (it falls back to env Path A in production).
        assert discover_t1_lease("sess-A", config_dir=config_dir, clock=clock) is None

    def test_discover_none_when_lease_expired(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        reg = _registry(config_dir, clock)
        _publish_lease(reg, scope_key="sess-A", session_id="sess-A")
        clock.advance(3.1)  # past TTL
        assert discover_t1_lease("sess-A", config_dir=config_dir, clock=clock) is None

    def test_discover_none_for_empty_session_id(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        assert discover_t1_lease(None, config_dir=config_dir, clock=clock) is None
        assert discover_t1_lease("", config_dir=config_dir, clock=clock) is None


class TestTransientClaudeFallback:
    """nexus-0x16i: the cold-start transient-window fallback. A bare Bash
    sibling with no resolvable session-id finds the owner's transient lease
    by matching its own immediate Claude ancestor pid (RF-6)."""

    _CLAUDE_PID = 5150

    def test_matches_fresh_transient_lease_by_claude_pid(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        reg = _registry(config_dir, clock)
        _publish_lease(
            reg, scope_key=str(_SERVER_PID), session_id=None, claude_pid=self._CLAUDE_PID
        )
        addr = discover_t1_by_claude_ancestor(
            self._CLAUDE_PID, config_dir=config_dir, clock=clock
        )
        assert addr == (_HOST, _PORT)

    def test_no_match_for_different_claude_pid(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        # A concurrent cold-starting session has a different immediate Claude
        # ancestor; its transient lease must NOT be grabbed (no mis-bind).
        reg = _registry(config_dir, clock)
        _publish_lease(
            reg, scope_key=str(_SERVER_PID), session_id=None, claude_pid=self._CLAUDE_PID
        )
        assert (
            discover_t1_by_claude_ancestor(9999, config_dir=config_dir, clock=clock)
            is None
        )

    def test_matches_session_keyed_lease_by_claude_pid(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        # nexus-gff3g: a session-keyed lease whose owner shares this sibling's
        # immediate Claude ancestor pid IS the sibling's own T1 and must match.
        # The fallback only fires after the session-id path (discover_t1_lease)
        # has already missed, which happens whenever the owner's session-id
        # label diverges from what the sibling resolves (NX_SESSION_ID given to
        # the MCP vs current_session written by the SessionStart hook).
        reg = _registry(config_dir, clock)
        _publish_lease(
            reg, scope_key="sess-A", session_id="sess-A", claude_pid=self._CLAUDE_PID
        )
        assert discover_t1_by_claude_ancestor(
            self._CLAUDE_PID, config_dir=config_dir, clock=clock
        ) == (_HOST, _PORT)

    def test_no_match_for_session_keyed_lease_with_different_claude_pid(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        # Extending the fallback to session-keyed leases preserves the
        # no-cross-session-mis-bind property: a different immediate Claude
        # ancestor pid must NOT match, even for a session-keyed lease.
        reg = _registry(config_dir, clock)
        _publish_lease(
            reg, scope_key="sess-A", session_id="sess-A", claude_pid=self._CLAUDE_PID
        )
        assert (
            discover_t1_by_claude_ancestor(
                self._CLAUDE_PID + 1, config_dir=config_dir, clock=clock
            )
            is None
        )

    def test_tie_break_prefers_newest_heartbeat(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        # nexus-gff3g M1/O1: when >1 fresh lease shares the claude_pid (a brief
        # re-key overlap, or one Claude process owning multiple MCP servers),
        # the newest heartbeat wins deterministically rather than glob order.
        reg = _registry(config_dir, clock)
        _publish_lease(
            reg,
            scope_key="sess-old",
            session_id="sess-old",
            claude_pid=self._CLAUDE_PID,
            host="127.0.0.1",
            port=11111,
        )
        clock.advance(0.5)  # newer heartbeat, still inside the 3.0s TTL
        _publish_lease(
            reg,
            scope_key="sess-new",
            session_id="sess-new",
            claude_pid=self._CLAUDE_PID,
            host="127.0.0.1",
            port=22222,
        )
        # Both leases are fresh and share the pid; the newest-heartbeat one wins.
        assert discover_t1_by_claude_ancestor(
            self._CLAUDE_PID, config_dir=config_dir, clock=clock
        ) == ("127.0.0.1", 22222)

    def test_no_match_for_expired_transient_lease(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        reg = _registry(config_dir, clock)
        _publish_lease(
            reg, scope_key=str(_SERVER_PID), session_id=None, claude_pid=self._CLAUDE_PID
        )
        clock.advance(3.1)  # past TTL
        assert (
            discover_t1_by_claude_ancestor(
                self._CLAUDE_PID, config_dir=config_dir, clock=clock
            )
            is None
        )
