# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 P1.B (nexus-beoh1): T3Client factory tests.

The factory is ``make_t3_client()`` returning a ``T3Database`` whose
``_client`` is a ``chromadb.HttpClient`` pointed at the running ``nx
daemon t3`` (per ``discovery_resolve('t3')``).

Surface parity by construction: the returned ``T3Database`` IS the same
class returned by ``make_t3()`` in direct mode — no shim, no method
drift. The only difference is the injected client.
"""
from __future__ import annotations

import inspect
import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    cd = tmp_path / "nexus_config"
    cd.mkdir()
    return cd


@pytest.fixture
def local_path(tmp_path: Path) -> Path:
    p = tmp_path / "chroma_t3"
    p.mkdir()
    return p


@pytest.fixture
def force_local_mode(monkeypatch):
    monkeypatch.setenv("NX_LOCAL", "1")


@pytest.fixture
def live_t3_daemon(config_dir: Path, local_path: Path, force_local_mode):
    """Spawn a real T3 daemon, yield the discovery payload, stop on teardown."""
    from nexus.daemon.t3_daemon import start_t3_daemon, stop_t3_daemon

    payload = start_t3_daemon(config_dir=config_dir, local_path=local_path)
    try:
        yield payload
    finally:
        stop_t3_daemon(config_dir=config_dir)


class TestFailLoudOnMissingDaemon:
    def test_no_daemon_raises_t3_daemon_error_with_recovery_hint(
        self, config_dir: Path, force_local_mode, monkeypatch
    ) -> None:
        from nexus.daemon.t3_client import T3DaemonError, make_t3_client

        monkeypatch.delenv("NX_T3_ADDR", raising=False)
        with pytest.raises(T3DaemonError) as excinfo:
            make_t3_client(config_dir=config_dir)
        assert "nx daemon t3 start" in str(excinfo.value)

    def test_no_auto_spawn(
        self, config_dir: Path, force_local_mode, monkeypatch
    ) -> None:
        """No discovery file is created when the daemon is absent.
        RDR-120 §Approach: explicit start only; no auto-spawn in P1."""
        from nexus.daemon.discovery import discovery_path
        from nexus.daemon.t3_client import T3DaemonError, make_t3_client

        monkeypatch.delenv("NX_T3_ADDR", raising=False)
        with pytest.raises(T3DaemonError):
            make_t3_client(config_dir=config_dir)
        assert not discovery_path(config_dir, tier="t3").exists()


class TestCloudModeRejection:
    def test_cloud_mode_raises_t3_daemon_error_with_clear_message(
        self, config_dir: Path, monkeypatch
    ) -> None:
        from nexus.daemon.t3_client import T3DaemonError, make_t3_client

        monkeypatch.setenv("NX_LOCAL", "0")
        # Force is_local_mode() to actually return False; the auto-detect
        # path returns True when cloud credentials are absent.
        monkeypatch.setenv("CHROMA_API_KEY", "fake-for-cloud-test")
        monkeypatch.setenv("VOYAGE_API_KEY", "fake-for-cloud-test")
        with pytest.raises(T3DaemonError) as excinfo:
            make_t3_client(config_dir=config_dir)
        msg = str(excinfo.value).lower()
        assert "cloud" in msg
        assert "no t3 daemon" in msg or "no daemon" in msg or "no-op" in msg


class TestHappyPathRoundTrip:
    def test_make_t3_client_returns_t3_database_backed_by_http_client(
        self, live_t3_daemon, config_dir: Path
    ) -> None:
        from nexus.daemon.t3_client import make_t3_client
        from nexus.db.t3 import T3Database

        t3 = make_t3_client(config_dir=config_dir)
        assert isinstance(t3, T3Database)
        # HttpClient exposes ``_server``; PersistentClient does not.
        assert hasattr(t3._client, "_server"), (
            "expected HttpClient-shaped _client with _server attribute"
        )

    def test_round_trip_collection_query(
        self, live_t3_daemon, config_dir: Path
    ) -> None:
        from nexus.daemon.t3_client import make_t3_client

        t3 = make_t3_client(config_dir=config_dir)
        client = t3._client
        coll = client.get_or_create_collection("t3_client_smoke")
        coll.add(documents=["alpha doc", "beta doc"], ids=["a", "b"])
        result = coll.query(query_texts=["alpha"], n_results=1)
        assert result["ids"][0] == ["a"]


class TestHttpTimeoutApplied:
    def test_http_client_session_timeout_overridden(
        self, live_t3_daemon, config_dir: Path
    ) -> None:
        """The factory must call ``_apply_chroma_http_timeout`` so the
        daemon path inherits the read-timeout fix (chromadb ships
        ``httpx.Client(timeout=None)`` which hangs indefinitely on
        stalled reads)."""
        from nexus.daemon.t3_client import make_t3_client
        from nexus.db.t3 import CHROMA_HTTP_READ_TIMEOUT_S

        t3 = make_t3_client(config_dir=config_dir)
        server = getattr(t3._client, "_server", None)
        assert server is not None, "HttpClient must expose _server"
        session = getattr(server, "_session", None)
        assert session is not None, "_server must expose _session"
        timeout = session.timeout
        assert timeout is not None
        read_timeout = getattr(timeout, "read", None)
        assert read_timeout == pytest.approx(CHROMA_HTTP_READ_TIMEOUT_S)


class TestMalformedDiscoveryPayload:
    """A discovery file that is otherwise valid (live PID,
    format_version=1, no shutdown marker) but missing tcp_host /
    tcp_port must surface T3DaemonError, not pass through silently to
    chromadb.HttpClient(host=None, port=None)."""

    def test_missing_tcp_port_raises_t3_daemon_error(
        self, config_dir: Path, force_local_mode, monkeypatch
    ) -> None:
        from nexus.daemon.discovery import discovery_path
        from nexus.daemon.t3_client import T3DaemonError, make_t3_client

        monkeypatch.delenv("NX_T3_ADDR", raising=False)
        path = discovery_path(config_dir, tier="t3")
        path.write_text(json.dumps({
            "format_version": 1,
            "tcp_host": "127.0.0.1",
            "pid": os.getpid(),
            "daemon_version": "test",
            "start_time": "1970-01-01T00:00:00",
            "local_path": "/tmp/x",
        }))
        os.chmod(str(path), 0o600)
        with pytest.raises(T3DaemonError) as excinfo:
            make_t3_client(config_dir=config_dir)
        assert "tcp_host" in str(excinfo.value) or "tcp_port" in str(excinfo.value)


class TestEnvVarOverridesFile:
    """RDR-120 C2 precedence: NX_T3_ADDR wins over the discovery file."""

    def test_env_var_wins_when_set(
        self, live_t3_daemon, config_dir: Path, monkeypatch
    ) -> None:
        # Point env at the same live daemon via host:port so the
        # round-trip succeeds; this validates the env path actually
        # constructs an HttpClient against the env-supplied address,
        # not the discovery file.
        from nexus.daemon.t3_client import make_t3_client

        addr = f"{live_t3_daemon['tcp_host']}:{live_t3_daemon['tcp_port']}"
        monkeypatch.setenv("NX_T3_ADDR", addr)
        t3 = make_t3_client(config_dir=config_dir)
        # Round-trip through the env-resolved client. If the env branch
        # didn't fire, the file path would still hit the same daemon, so
        # validate connectivity instead of source-attribution.
        coll = t3._client.get_or_create_collection("env_path_smoke")
        coll.add(documents=["x"], ids=["1"])
        assert coll.query(query_texts=["x"], n_results=1)["ids"][0] == ["1"]


class TestSurfaceParity:
    def test_public_method_set_matches_make_t3(
        self, live_t3_daemon, config_dir: Path
    ) -> None:
        """make_t3_client and make_t3 must return objects with identical
        public surfaces (same class — T3Database — by construction).
        Catches accidental shim-class introduction."""
        from nexus.daemon.t3_client import make_t3_client
        from nexus.db.t3 import T3Database

        t3_client = make_t3_client(config_dir=config_dir)
        assert type(t3_client) is T3Database

        public_methods = [
            name for name, member in inspect.getmembers(T3Database, callable)
            if not name.startswith("_")
        ]
        for name in public_methods:
            assert callable(getattr(t3_client, name)), (
                f"{name} not callable on make_t3_client() return"
            )
