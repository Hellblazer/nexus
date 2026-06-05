# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-149 P4 (bead nexus-8znyd): T1 consumes the leased registry.

Unit coverage for ``T1LeasePublisher`` and ``discover_t1_lease`` with an
injected clock and an injected ``session_resolver``, so the locked RF-2
transient-key -> session-id re-key protocol is exercised deterministically
without a real chroma server or a real SessionStart hook.

T1 stays MCP-lifespan-owned (NOT a supervised daemon, per the RDR
Decision); it CONSUMES the ``ServiceRegistry`` primitive. The publisher:

* publishes under a TRANSIENT key (the chroma ``server_pid``, unique among
  live owners, never ``"unknown"``) carrying the resolved-or-None
  session-id as a payload field;
* re-keys atomically to the session-id scope the instant
  ``session_resolver()`` first resolves non-None (CA-3);
* heartbeats its own lease each tick (self-heal, #1114);
* relinquishes only its own record on shutdown (CA-4 fencing safety).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus.daemon.service_registry import ServiceRegistry, mint_owner_token
from nexus.daemon.t1_lease import T1LeasePublisher, discover_t1_lease

_SERVER_PID = 4242
_SIBLING_SERVER_PID = 4343
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


def _publisher(
    registry: ServiceRegistry,
    *,
    server_pid: int = _SERVER_PID,
    session_resolver=lambda: None,
    owner_token: str | None = None,
) -> T1LeasePublisher:
    return T1LeasePublisher(
        registry=registry,
        server_pid=server_pid,
        host=_HOST,
        port=_PORT,
        version="1.0.0",
        session_resolver=session_resolver,
        owner_token=owner_token or mint_owner_token(),
    )


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    cd = tmp_path / "cfg"
    cd.mkdir(parents=True, exist_ok=True, mode=0o700)
    return cd


@pytest.fixture
def clock() -> _FakeClock:
    return _FakeClock()


class TestTransientPublish:
    def test_publish_keys_on_server_pid_when_session_unresolved(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        reg = _registry(config_dir, clock)
        pub = _publisher(reg, session_resolver=lambda: None)
        pub.publish()
        transient = config_dir / f"t1_addr.{_SERVER_PID}"
        assert transient.exists(), "no transient server_pid-keyed record"
        assert not pub.session_keyed
        assert pub.active_scope_key == str(_SERVER_PID)

    def test_no_record_is_ever_keyed_unknown(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        # CA-3 (i): the transient key is the server_pid, never the string
        # "unknown" that the legacy session-id fallback would have produced.
        reg = _registry(config_dir, clock)
        _publisher(reg, session_resolver=lambda: None).publish()
        assert not (config_dir / "t1_addr.unknown").exists()
        assert list(config_dir.glob("t1_addr.*")) == [
            config_dir / f"t1_addr.{_SERVER_PID}"
        ]

    def test_publish_keys_on_session_directly_when_already_resolved(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        # Warm session (claude -p subprocess inherits NX_SESSION_ID): the
        # session-id resolves at publish time, so there is no transient
        # window and no re-key needed.
        reg = _registry(config_dir, clock)
        pub = _publisher(reg, session_resolver=lambda: "sess-A")
        pub.publish()
        assert (config_dir / "t1_addr.sess-A").exists()
        assert not (config_dir / f"t1_addr.{_SERVER_PID}").exists()
        assert pub.session_keyed
        assert pub.active_scope_key == "sess-A"


class TestRekey:
    def test_tick_rekeys_transient_to_session_on_resolution(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        # CA-3: the instant session_resolver resolves non-None while still
        # transient-keyed, the next tick atomically re-keys.
        reg = _registry(config_dir, clock)
        sid: dict[str, str | None] = {"v": None}
        pub = _publisher(reg, session_resolver=lambda: sid["v"])
        pub.publish()
        transient = config_dir / f"t1_addr.{_SERVER_PID}"
        session = config_dir / "t1_addr.sess-A"
        assert transient.exists() and not session.exists()

        sid["v"] = "sess-A"
        pub.tick()

        assert session.exists(), "re-key did not write the session-id record"
        assert not transient.exists(), "re-key did not unlink the transient record"
        assert pub.session_keyed
        assert pub.active_scope_key == "sess-A"

    def test_rekey_is_idempotent_after_first_resolution(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        reg = _registry(config_dir, clock)
        pub = _publisher(reg, session_resolver=lambda: "sess-A")
        pub.publish()  # keyed on session directly
        gen_before = pub.record.generation
        clock.advance(1.0)
        pub.tick()  # heartbeat only, no re-key
        assert pub.record.generation == gen_before
        assert list(config_dir.glob("t1_addr.*")) == [config_dir / "t1_addr.sess-A"]

    def test_rekey_atomic_under_concurrent_sibling(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        # CA-3 (iii): two siblings of ONE session each publish transiently,
        # then both re-key to "sess-A". The session-key publish is flock-
        # serialized (monotonic generation), and each sibling unlinks only
        # its own server_pid transient record. End state: exactly one
        # session record, both transient records gone.
        reg = _registry(config_dir, clock)
        sid: dict[str, str | None] = {"v": None}
        a = _publisher(reg, server_pid=_SERVER_PID, session_resolver=lambda: sid["v"])
        b = _publisher(
            reg, server_pid=_SIBLING_SERVER_PID, session_resolver=lambda: sid["v"]
        )
        a.publish()
        b.publish()
        assert (config_dir / f"t1_addr.{_SERVER_PID}").exists()
        assert (config_dir / f"t1_addr.{_SIBLING_SERVER_PID}").exists()

        sid["v"] = "sess-A"
        a.tick()
        b.tick()

        records = sorted(p.name for p in config_dir.glob("t1_addr.*"))
        assert records == ["t1_addr.sess-A"], records
        # The session record carries a monotonic generation > 1 (b's publish
        # read a's session record and incremented).
        rec = reg.discover("sess-A")
        assert rec is not None and rec.generation == 2


class TestSelfHeal:
    def test_tick_self_heals_externally_deleted_record(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        # #1114: a transient loss of the record while the owner is alive is
        # repaired by the next heartbeat tick (re-stamp at the same
        # generation), unlike legacy session.py which had no re-assert.
        reg = _registry(config_dir, clock)
        pub = _publisher(reg, session_resolver=lambda: "sess-A")
        pub.publish()
        gen = pub.record.generation
        (config_dir / "t1_addr.sess-A").unlink()
        assert reg.discover("sess-A") is None

        pub.tick()

        healed = reg.discover("sess-A")
        assert healed is not None, "owner alive but record not self-healed"
        assert healed.generation == gen, "self-heal must preserve the generation"


class TestFencing:
    def test_stale_publisher_tick_is_fenced_not_clobbering(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        # CA-4: a slow predecessor's delayed tick must not clobber a newer,
        # higher-generation owner. tick() swallows StaleOwnerError.
        reg = _registry(config_dir, clock)
        pred = _publisher(reg, server_pid=_SERVER_PID, session_resolver=lambda: "sess-A")
        succ = _publisher(
            reg, server_pid=_SIBLING_SERVER_PID, session_resolver=lambda: "sess-A"
        )
        pred.publish()
        succ.publish()  # generation 2 takes over

        pred.tick()  # stale: fenced, writes nothing, does not raise

        rec = reg.discover("sess-A")
        assert rec is not None and rec.generation == 2
        assert pred.fenced

    def test_relinquish_only_unlinks_own_record(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        reg = _registry(config_dir, clock)
        pred = _publisher(reg, server_pid=_SERVER_PID, session_resolver=lambda: "sess-A")
        succ = _publisher(
            reg, server_pid=_SIBLING_SERVER_PID, session_resolver=lambda: "sess-A"
        )
        pred.publish()
        succ.publish()  # successor now owns sess-A

        pred.relinquish()  # predecessor shuts down late

        rec = reg.discover("sess-A")
        assert rec is not None, "predecessor relinquish clobbered successor record"
        assert rec.owner_token == succ.record.owner_token

    def test_relinquish_marks_shutdown_then_unlinks(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        reg = _registry(config_dir, clock)
        pub = _publisher(reg, session_resolver=lambda: "sess-A")
        pub.publish()
        assert (config_dir / "t1_addr.sess-A").exists()

        pub.relinquish()

        assert not (config_dir / "t1_addr.sess-A").exists()
        assert pub.record is None

    def test_tick_is_noop_after_relinquish(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        # The SIGTERM-path guard: relinquish() sets _record None; a heartbeat
        # tick that interleaves (loop already past the cancel point) must not
        # re-create the record via the self-heal path.
        reg = _registry(config_dir, clock)
        pub = _publisher(reg, session_resolver=lambda: "sess-A")
        pub.publish()
        pub.relinquish()

        pub.tick()  # must not raise, must not write

        assert reg.discover("sess-A") is None
        assert not (config_dir / "t1_addr.sess-A").exists()

    def test_rekey_state_advances_even_if_transient_relinquish_fails(
        self, config_dir: Path, clock: _FakeClock, monkeypatch
    ) -> None:
        # IMPORTANT-1: if relinquishing the transient record raises after the
        # session record is published, the publisher must still commit to the
        # session key (no re-key loop, no generation inflation). The transient
        # record then ages out via TTL.
        reg = _registry(config_dir, clock)
        sid: dict[str, str | None] = {"v": None}
        pub = _publisher(reg, session_resolver=lambda: sid["v"])
        pub.publish()

        real_relinquish = reg.relinquish

        def boom(record):
            raise OSError("simulated unlink failure")

        sid["v"] = "sess-A"
        monkeypatch.setattr(reg, "relinquish", boom)
        pub.tick()  # re-key; transient relinquish raises but is swallowed

        assert pub.session_keyed
        assert pub.active_scope_key == "sess-A"
        session_rec = reg.discover("sess-A")
        assert session_rec is not None and session_rec.generation == 1

        # A subsequent tick heartbeats the SESSION record (no re-key loop):
        # the generation does not inflate.
        monkeypatch.setattr(reg, "relinquish", real_relinquish)
        clock.advance(1.0)
        pub.tick()
        again = reg.discover("sess-A")
        assert again is not None and again.generation == 1


class TestDiscoverReader:
    def test_discover_resolves_session_keyed_endpoint(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        reg = _registry(config_dir, clock)
        pub = _publisher(reg, session_resolver=lambda: "sess-A")
        pub.publish()

        addr = discover_t1_lease("sess-A", config_dir=config_dir, clock=clock)
        assert addr == (_HOST, _PORT)

    def test_discover_none_when_no_session_record(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        reg = _registry(config_dir, clock)
        _publisher(reg, session_resolver=lambda: None).publish()  # transient only
        # A sibling resolving by session-id finds nothing during the
        # transient window (it falls back to env Path A in production).
        assert discover_t1_lease("sess-A", config_dir=config_dir, clock=clock) is None

    def test_discover_none_when_lease_expired(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        reg = _registry(config_dir, clock)
        _publisher(reg, session_resolver=lambda: "sess-A").publish()
        clock.advance(3.1)  # past TTL
        assert discover_t1_lease("sess-A", config_dir=config_dir, clock=clock) is None

    def test_discover_none_for_empty_session_id(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        assert discover_t1_lease(None, config_dir=config_dir, clock=clock) is None
        assert discover_t1_lease("", config_dir=config_dir, clock=clock) is None
