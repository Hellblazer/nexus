# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 P1.A (nexus-41unl): discovery resolver tests.

Covers ``discovery_path``, ``find_t3_daemon``, and ``discovery_resolve``
including the C2 precedence contract: env-var wins when set + non-empty,
file is fallback when env unset, and a set-but-unreachable env-var does
NOT fall through silently to the file.

Also covers nexus-md90p: T2-only liveness probe + pid fast-path.
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from nexus.daemon.discovery import (
    DaemonNotRunningError,
    discovery_path,
    discovery_resolve,
    find_t3_daemon,
)


def _live_payload(local_path: str = "/tmp/chroma_test") -> dict:
    return {
        "format_version": 1,
        "tcp_host": "127.0.0.1",
        "tcp_port": 9999,
        "pid": os.getpid(),  # current process is always alive
        "daemon_version": "test",
        "start_time": "1970-01-01T00:00:00",
        "local_path": local_path,
    }


def _stale_payload() -> dict:
    p = dict(_live_payload())
    p["pid"] = 2**31 - 1
    return p


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    cd = tmp_path / "nexus_config"
    cd.mkdir()
    return cd


@pytest.fixture(autouse=True)
def _clear_t3_env(monkeypatch):
    """Tests opt in to env-var presence; default is unset."""
    monkeypatch.delenv("NX_T3_ADDR", raising=False)
    monkeypatch.delenv("NX_T2_ADDR", raising=False)
    monkeypatch.delenv("NX_T2_SOCK", raising=False)


class TestDiscoveryPath:
    def test_t3_default_tier_returns_t3_addr_path(self, config_dir: Path) -> None:
        assert discovery_path(config_dir) == config_dir / f"t3_addr.{os.getuid()}"

    def test_t2_tier_returns_t2_addr_path(self, config_dir: Path) -> None:
        assert discovery_path(config_dir, tier="t2") == config_dir / f"t2_addr.{os.getuid()}"

    def test_unknown_tier_rejected(self, config_dir: Path) -> None:
        with pytest.raises(ValueError):
            discovery_path(config_dir, tier="t9")  # type: ignore[arg-type]


class TestFindT3Daemon:
    def test_returns_none_when_file_absent(self, config_dir: Path) -> None:
        assert find_t3_daemon(config_dir) is None

    def test_returns_payload_when_pid_alive(self, config_dir: Path) -> None:
        path = discovery_path(config_dir, tier="t3")
        path.write_text(json.dumps(_live_payload()))
        payload = find_t3_daemon(config_dir)
        assert payload is not None
        assert payload["pid"] == os.getpid()

    def test_stale_pid_returns_none_and_unlinks_file(self, config_dir: Path) -> None:
        path = discovery_path(config_dir, tier="t3")
        path.write_text(json.dumps(_stale_payload()))
        assert find_t3_daemon(config_dir) is None
        assert not path.exists()

    def test_shutdown_marker_returns_none(self, config_dir: Path) -> None:
        path = discovery_path(config_dir, tier="t3")
        payload = _live_payload()
        payload["status"] = "shutting_down"
        path.write_text(json.dumps(payload))
        assert find_t3_daemon(config_dir) is None

    def test_format_version_too_new_returns_none(self, config_dir: Path) -> None:
        path = discovery_path(config_dir, tier="t3")
        payload = _live_payload()
        payload["format_version"] = 999
        path.write_text(json.dumps(payload))
        assert find_t3_daemon(config_dir) is None

    def test_non_dict_payload_returns_none(self, config_dir: Path) -> None:
        path = discovery_path(config_dir, tier="t3")
        path.write_text(json.dumps(["not", "a", "dict"]))
        assert find_t3_daemon(config_dir) is None

    def test_garbage_file_returns_none(self, config_dir: Path) -> None:
        path = discovery_path(config_dir, tier="t3")
        path.write_text("<<< not json >>>")
        assert find_t3_daemon(config_dir) is None


class TestDiscoveryResolveT3:
    """RDR-120 C2 precedence: env-var wins when set + non-empty; file
    is fallback when env unset."""

    def test_env_var_wins_when_set(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Plant a live discovery file too; env must take precedence.
        path = discovery_path(config_dir, tier="t3")
        path.write_text(json.dumps(_live_payload()))
        monkeypatch.setenv("NX_T3_ADDR", "10.0.0.5:6000")

        resolved = discovery_resolve("t3", config_dir=config_dir)
        assert resolved["source"] == "env:NX_T3_ADDR"
        assert resolved["tcp_host"] == "10.0.0.5"
        assert resolved["tcp_port"] == 6000

    def test_empty_env_var_treated_as_unset(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = discovery_path(config_dir, tier="t3")
        path.write_text(json.dumps(_live_payload()))
        monkeypatch.setenv("NX_T3_ADDR", "")

        resolved = discovery_resolve("t3", config_dir=config_dir)
        assert resolved["source"] == "file"
        assert resolved["tcp_port"] == 9999

    def test_file_fallback_when_env_unset(self, config_dir: Path) -> None:
        path = discovery_path(config_dir, tier="t3")
        path.write_text(json.dumps(_live_payload()))
        resolved = discovery_resolve("t3", config_dir=config_dir)
        assert resolved["source"] == "file"

    def test_raises_when_neither_env_nor_file(self, config_dir: Path) -> None:
        with pytest.raises(DaemonNotRunningError) as excinfo:
            discovery_resolve("t3", config_dir=config_dir)
        msg = str(excinfo.value)
        assert "nx daemon t3 start" in msg

    def test_malformed_env_var_raises_value_error(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NX_T3_ADDR", "no-colon-here")
        with pytest.raises(ValueError):
            discovery_resolve("t3", config_dir=config_dir)

    def test_non_integer_port_raises_value_error(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NX_T3_ADDR", "host:not-a-port")
        with pytest.raises(ValueError):
            discovery_resolve("t3", config_dir=config_dir)


# ---------------------------------------------------------------------------
# RDR-149 P2: T2 lease-record resolution + reap-path normalization
# ---------------------------------------------------------------------------


def _t2_lease(pid: int, *, generation: int = 1, version: str = "1.2.3") -> dict:
    """A RDR-149 T2 lease record as ServiceRegistry writes it."""
    import time as _time

    return {
        "scope_key": str(os.getuid()),
        "generation": generation,
        "owner_token": "tok-xyz",
        "heartbeat_epoch": _time.time(),
        "ttl": 3.0,
        "endpoint": {
            "uds_path": "/tmp/t2.sock",
            "tcp_host": "127.0.0.1",
            "tcp_port": 5555,
            "pid": pid,
        },
        "version": version,
        "payload": {},
        "status": "live",
        "format_version": 1,
    }


class TestLeaseRecordResolution:
    def test_is_lease_record_detects_both_shapes(self) -> None:
        from nexus.daemon.discovery import is_lease_record

        assert is_lease_record(_t2_lease(os.getpid())) is True
        assert is_lease_record(_live_payload()) is False  # legacy
        assert is_lease_record({"endpoint": {}}) is False  # no generation
        assert is_lease_record("nope") is False

    def test_find_t2_resolves_fresh_lease_with_flat_endpoint(
        self, config_dir: Path
    ) -> None:
        from nexus.daemon.discovery import discovery_path, find_t2_daemon

        path = discovery_path(config_dir, tier="t2")
        path.write_text(json.dumps(_t2_lease(os.getpid())))
        resolved = find_t2_daemon(config_dir)
        assert resolved is not None
        # Endpoint fields lifted to the top level (client contract).
        assert resolved["uds_path"] == "/tmp/t2.sock"
        assert resolved["tcp_port"] == 5555
        assert resolved["pid"] == os.getpid()
        assert resolved["generation"] == 1
        assert resolved["version"] == "1.2.3"

    def test_find_t2_rejects_and_unlinks_expired_lease(
        self, config_dir: Path
    ) -> None:
        from nexus.daemon.discovery import discovery_path, find_t2_daemon

        path = discovery_path(config_dir, tier="t2")
        lease = _t2_lease(os.getpid())
        lease["heartbeat_epoch"] = 0.0  # ancient -> aged past TTL
        path.write_text(json.dumps(lease))
        assert find_t2_daemon(config_dir) is None
        assert path.exists() is False  # reaped

    def test_find_t2_rejects_shutdown_marker(self, config_dir: Path) -> None:
        from nexus.daemon.discovery import discovery_path, find_t2_daemon

        path = discovery_path(config_dir, tier="t2")
        lease = _t2_lease(os.getpid())
        lease["status"] = "shutting_down"
        path.write_text(json.dumps(lease))
        assert find_t2_daemon(config_dir) is None

    def test_find_t2_rejects_forward_incompatible_format(
        self, config_dir: Path
    ) -> None:
        from nexus.daemon.discovery import discovery_path, find_t2_daemon

        path = discovery_path(config_dir, tier="t2")
        lease = _t2_lease(os.getpid())
        lease["format_version"] = 2  # a newer client wrote this
        path.write_text(json.dumps(lease))
        assert find_t2_daemon(config_dir) is None

    def test_find_t2_legacy_payload_still_resolves(self, config_dir: Path) -> None:
        # Upgrade window: a still-running old daemon's legacy payload must
        # remain readable via the pid-liveness fallback.
        from nexus.daemon.discovery import discovery_path, find_t2_daemon

        path = discovery_path(config_dir, tier="t2")
        legacy = _live_payload()
        legacy["uds_path"] = "/tmp/legacy.sock"
        path.write_text(json.dumps(legacy))
        resolved = find_t2_daemon(config_dir)
        assert resolved is not None
        assert resolved["pid"] == os.getpid()


class TestNormalizeDiscoveryView:
    """The reap path (``_reap_predecessor_daemon``) inspects even a stale /
    unreachable predecessor, so it normalizes WITHOUT a freshness filter.
    These guard the pid / version / socket extraction that the version-aware
    graceful-drain + health-ping depend on (the P2 review Critical)."""

    def test_lease_view_lifts_pid_version_and_socket(self) -> None:
        from nexus.daemon.discovery import normalize_discovery_view

        view = normalize_discovery_view(_t2_lease(4242, version="9.9.9"))
        assert view["pid"] == 4242
        assert view["daemon_version"] == "9.9.9"  # lease ``version`` -> daemon_version
        assert view["uds_path"] == "/tmp/t2.sock"
        assert view["tcp_host"] == "127.0.0.1"
        assert view["tcp_port"] == 5555

    def test_legacy_payload_passes_through(self) -> None:
        from nexus.daemon.discovery import normalize_discovery_view

        legacy = _live_payload()
        assert normalize_discovery_view(legacy) == legacy

    def test_non_dict_yields_empty(self) -> None:
        from nexus.daemon.discovery import normalize_discovery_view

        assert normalize_discovery_view("garbage") == {}

    def test_health_ping_and_handshake_read_normalized_lease(self) -> None:
        # The reap discrimination helpers must work off the normalized view:
        # _peer_handshake reads daemon_version, _health_ping reads the socket
        # fields — both absent at the top level of a raw lease record.
        from nexus.daemon.discovery import normalize_discovery_view
        from nexus.daemon.t2_daemon import _peer_handshake

        view = normalize_discovery_view(_t2_lease(os.getpid(), version="7.0.0"))
        version, _reachable = _peer_handshake(os.getpid(), view)
        assert version == "7.0.0"  # would be None if it read raw lease["version"] wrongly


# ---------------------------------------------------------------------------
# nexus-md90p: T2-only liveness probe + pid fast-path
# ---------------------------------------------------------------------------


def _t2_lease_with_sock(
    pid: int, sock_path: str, *, expired: bool = False, generation: int = 1
) -> dict:
    """T2 lease record pointing at a UDS socket on tmp_path."""
    epoch = 0.0 if expired else time.time()
    return {
        "scope_key": str(os.getuid()),
        "generation": generation,
        "owner_token": "tok-abc",
        "heartbeat_epoch": epoch,
        "ttl": 3.0,
        "endpoint": {
            "uds_path": sock_path,
            "tcp_host": "127.0.0.1",
            "tcp_port": 5555,
            "pid": pid,
        },
        "version": "5.10.6",
        "payload": {},
        "status": "live",
        "format_version": 1,
    }


def _start_uds_listener(sock_path: str) -> threading.Thread:
    """Start a background thread that listens on an AF_UNIX socket.

    Returns the thread (already started). The socket is bound and listening
    before the function returns; connections are accepted and immediately
    closed so the probe is a pure connectivity check.
    """
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(5)
    srv.settimeout(5.0)

    def _serve() -> None:
        try:
            while True:
                try:
                    conn, _ = srv.accept()
                    conn.close()
                except socket.timeout:
                    break
                except OSError:
                    break
        finally:
            srv.close()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    return t


@pytest.fixture
def short_tmp(tmp_path: Path) -> Path:
    """Config dir under a short /tmp base to stay within AF_UNIX 104-byte limit."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    return cfg


@pytest.fixture
def uds_base() -> Path:
    """A short directory under /tmp for UDS sockets (AF_UNIX path <= 104 bytes)."""
    d = Path(tempfile.mkdtemp(dir="/tmp", prefix="nx_"))
    yield d
    # best-effort cleanup
    import shutil
    shutil.rmtree(str(d), ignore_errors=True)


class TestT2LivenessProbeAndPidFastPath:
    """nexus-md90p: discovery staleness = process liveness, not heartbeat-age alone.

    All cases are fully deterministic: real AF_UNIX sockets on tmp_path,
    injected heartbeat_epoch values for clock control, no sleeps.
    """

    def test_expired_lease_live_answering_uds_resolves_no_unlink(
        self, short_tmp: Path, uds_base: Path
    ) -> None:
        """Expired lease + pid alive + UDS answers -> resolves, file NOT unlinked."""
        from nexus.daemon.discovery import find_t2_daemon

        sock_path = str(uds_base / "t2.sock")
        _start_uds_listener(sock_path)

        path = discovery_path(short_tmp, tier="t2")
        lease = _t2_lease_with_sock(os.getpid(), sock_path, expired=True)
        path.write_text(json.dumps(lease))

        resolved = find_t2_daemon(short_tmp)

        assert resolved is not None, "Should resolve because UDS is answering"
        assert resolved["uds_path"] == sock_path
        assert path.exists(), "File must NOT be unlinked when UDS answered"

    def test_expired_lease_dead_pid_returns_none_and_unlinks(
        self, short_tmp: Path, uds_base: Path
    ) -> None:
        """Expired lease + dead pid -> None + file unlinked (regression-lock)."""
        from nexus.daemon.discovery import find_t2_daemon

        sock_path = str(uds_base / "dead.sock")
        # No listener bound; connect will be refused or fail.

        path = discovery_path(short_tmp, tier="t2")
        # Use a pid that is guaranteed not to exist: max int32
        lease = _t2_lease_with_sock(2**31 - 1, sock_path, expired=True)
        path.write_text(json.dumps(lease))

        result = find_t2_daemon(short_tmp)

        assert result is None
        assert not path.exists(), "File must be unlinked when pid is dead"

    def test_expired_lease_refused_connect_returns_none_and_unlinks(
        self, short_tmp: Path, uds_base: Path
    ) -> None:
        """Expired lease + pid alive + UDS not listening -> None + unlinked."""
        from nexus.daemon.discovery import find_t2_daemon

        sock_path = str(uds_base / "refused.sock")
        # No listener — connect will fail with ConnectionRefusedError or
        # FileNotFoundError

        path = discovery_path(short_tmp, tier="t2")
        lease = _t2_lease_with_sock(os.getpid(), sock_path, expired=True)
        path.write_text(json.dumps(lease))

        result = find_t2_daemon(short_tmp)

        assert result is None
        assert not path.exists(), "File must be unlinked when socket not answering"

    def test_fresh_lease_dead_pid_returns_none(
        self, short_tmp: Path, uds_base: Path
    ) -> None:
        """Fresh lease + dead pid -> None immediately (hard-kill fast-path)."""
        from nexus.daemon.discovery import find_t2_daemon

        sock_path = str(uds_base / "fresh_dead.sock")

        path = discovery_path(short_tmp, tier="t2")
        # Fresh lease (heartbeat_epoch = now) but corpse pid
        lease = _t2_lease_with_sock(2**31 - 1, sock_path, expired=False)
        path.write_text(json.dumps(lease))

        result = find_t2_daemon(short_tmp)

        assert result is None
        assert not path.exists(), (
            "file must be unlinked when pid is dead (fresh-lease fast-path)"
        )

    def test_fresh_lease_alive_pid_resolves(
        self, short_tmp: Path, uds_base: Path
    ) -> None:
        """Fresh lease + alive pid -> resolves (regression: existing happy path)."""
        from nexus.daemon.discovery import find_t2_daemon

        sock_path = str(uds_base / "fresh_alive.sock")

        path = discovery_path(short_tmp, tier="t2")
        lease = _t2_lease_with_sock(os.getpid(), sock_path, expired=False)
        path.write_text(json.dumps(lease))

        resolved = find_t2_daemon(short_tmp)

        assert resolved is not None
        assert resolved["uds_path"] == sock_path

    def test_t3_expired_lease_no_uds_probe(self, short_tmp: Path) -> None:
        """T3 expired lease -> None + unlinked WITHOUT attempting a UDS probe.

        The T3 path must be byte-for-byte identical to before this bead.
        No UDS probe is performed (T3 is TCP chroma).
        """
        from nexus.daemon.discovery import find_t3_daemon

        path = discovery_path(short_tmp, tier="t3")

        # T3 lease uses TCP, no uds_path; expired
        lease = {
            "scope_key": str(os.getuid()),
            "generation": 1,
            "owner_token": "tok-t3",
            "heartbeat_epoch": 0.0,  # ancient
            "ttl": 3.0,
            "endpoint": {
                "tcp_host": "127.0.0.1",
                "tcp_port": 8000,
                "pid": os.getpid(),
            },
            "version": "5.10.6",
            "payload": {},
            "status": "live",
            "format_version": 1,
        }
        path.write_text(json.dumps(lease))

        result = find_t3_daemon(short_tmp)

        assert result is None
        assert not path.exists(), "T3 expired lease must be unlinked"

    def test_expired_lease_warning_logged(
        self, short_tmp: Path, uds_base: Path
    ) -> None:
        """Warning t2_discovery_lease_stale_but_answering is emitted on UDS rescue."""
        from structlog.testing import capture_logs

        from nexus.daemon.discovery import find_t2_daemon

        sock_path = str(uds_base / "warn.sock")
        _start_uds_listener(sock_path)

        path = discovery_path(short_tmp, tier="t2")
        lease = _t2_lease_with_sock(os.getpid(), sock_path, expired=True)
        path.write_text(json.dumps(lease))

        with capture_logs() as captured:
            resolved = find_t2_daemon(short_tmp)

        assert resolved is not None
        events = [e.get("event", "") for e in captured]
        assert any(
            "t2_discovery_lease_stale_but_answering" in ev for ev in events
        ), f"Must log t2_discovery_lease_stale_but_answering; got: {events}"
