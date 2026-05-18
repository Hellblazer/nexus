# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-112 P1.5.3 (nexus-7yd2): T3Client factory tests.

Tests the integration seam Phase 3 (nexus-hpxl) will use. The factory is
``make_t3_client()`` returning a ``T3Database`` whose ``_client`` is a
``chromadb.HttpClient`` pointed at the running ``nx daemon t3`` (per
``discovery_resolve('t3')`` from nexus-n8xg).

Surface parity by construction: the returned ``T3Database`` IS the same
class returned by ``make_t3()`` in direct mode — no shim, no method
drift. The only difference is the injected client.
"""
from __future__ import annotations

import inspect
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
    """Force ``is_local_mode()`` to return True regardless of env credentials."""
    monkeypatch.setenv("NX_LOCAL", "1")


@pytest.fixture
def live_t3_daemon(config_dir: Path, local_path: Path, force_local_mode):
    """Spawn a real T3 daemon for the test, yield the discovery payload,
    and stop the daemon on teardown."""
    from nexus.daemon.t3_daemon import start_t3_daemon, stop_t3_daemon

    payload = start_t3_daemon(config_dir=config_dir, local_path=local_path)
    try:
        yield payload
    finally:
        stop_t3_daemon(config_dir=config_dir)


# ---------------------------------------------------------------------------
# Fail-loud: no daemon running
# ---------------------------------------------------------------------------


class TestFailLoudOnMissingDaemon:
    def test_no_daemon_raises_t3_daemon_error_with_recovery_hint(
        self, config_dir: Path, force_local_mode, monkeypatch
    ) -> None:
        from nexus.daemon.t3_client import T3DaemonError, make_t3_client

        monkeypatch.delenv("NX_T3_ADDR", raising=False)
        with pytest.raises(T3DaemonError) as excinfo:
            make_t3_client(config_dir=config_dir)
        msg = str(excinfo.value)
        assert "nx daemon t3 start" in msg

    def test_no_auto_spawn(
        self, config_dir: Path, force_local_mode, monkeypatch
    ) -> None:
        """No discovery file is created when the daemon is absent.
        Matches T2 contract: 'no auto-spawn in daemon mode' per RDR-112."""
        from nexus.daemon.t3_client import T3DaemonError, make_t3_client
        from nexus.daemon.discovery import discovery_path

        monkeypatch.delenv("NX_T3_ADDR", raising=False)
        with pytest.raises(T3DaemonError):
            make_t3_client(config_dir=config_dir)
        assert not discovery_path(config_dir, tier="t3").exists()


# ---------------------------------------------------------------------------
# Cloud mode: factory is the semantic gate (per PLAN-AUDIT)
# ---------------------------------------------------------------------------


class TestCloudModeRejection:
    def test_cloud_mode_raises_t3_daemon_error_with_clear_message(
        self, config_dir: Path, monkeypatch
    ) -> None:
        from nexus.daemon.t3_client import T3DaemonError, make_t3_client

        monkeypatch.setenv("NX_LOCAL", "0")
        # Sneak past the credential auto-detection by faking credentials
        # exist; otherwise is_local_mode() defaults to True when keys are
        # absent.
        monkeypatch.setenv("CHROMA_API_KEY", "fake-for-cloud-test")
        monkeypatch.setenv("VOYAGE_API_KEY", "fake-for-cloud-test")
        with pytest.raises(T3DaemonError) as excinfo:
            make_t3_client(config_dir=config_dir)
        msg = str(excinfo.value).lower()
        assert "cloud" in msg
        assert "no t3 daemon" in msg or "no daemon" in msg or "no-op" in msg


# ---------------------------------------------------------------------------
# Happy path: real chroma subprocess, T3Database round-trip
# ---------------------------------------------------------------------------


class TestHappyPathRoundTrip:
    def test_make_t3_client_returns_t3_database_backed_by_http_client(
        self, live_t3_daemon, config_dir: Path
    ) -> None:
        import chromadb

        from nexus.daemon.t3_client import make_t3_client
        from nexus.db.t3 import T3Database

        t3 = make_t3_client(config_dir=config_dir)
        try:
            assert isinstance(t3, T3Database)
            # The injected client must be an HttpClient (or at least carry
            # an _server with _session — chromadb.HttpClient does, but
            # PersistentClient does not).
            assert hasattr(t3._client, "_server"), (
                "expected HttpClient-shaped _client with _server attribute"
            )
        finally:
            # T3Database has __exit__ but no close(); just let it go.
            pass

    def test_round_trip_collection_query(
        self, live_t3_daemon, config_dir: Path
    ) -> None:
        """list_collections + get_or_create_collection + add + query —
        the core happy-path Phase 3 will exercise via mcp_infra.get_t3()."""
        from nexus.daemon.t3_client import make_t3_client

        t3 = make_t3_client(config_dir=config_dir)
        client = t3._client
        coll = client.get_or_create_collection("t3_client_smoke")
        coll.add(documents=["alpha doc", "beta doc"], ids=["a", "b"])
        result = coll.query(query_texts=["alpha"], n_results=1)
        assert result["ids"][0] == ["a"]


# ---------------------------------------------------------------------------
# HTTP timeout override (nexus-jgjw)
# ---------------------------------------------------------------------------


class TestHttpTimeoutApplied:
    def test_http_client_session_timeout_overridden(
        self, live_t3_daemon, config_dir: Path
    ) -> None:
        """The factory must call ``_apply_chroma_http_timeout`` so the
        daemon path inherits the nexus-jgjw read-timeout fix (chromadb
        ships ``httpx.Client(timeout=None)`` which hangs indefinitely
        on a stalled response)."""
        from nexus.daemon.t3_client import make_t3_client
        from nexus.db.t3 import CHROMA_HTTP_READ_TIMEOUT_S

        t3 = make_t3_client(config_dir=config_dir)
        server = getattr(t3._client, "_server", None)
        assert server is not None, "HttpClient must expose _server"
        session = getattr(server, "_session", None)
        assert session is not None, "_server must expose _session"
        timeout = session.timeout
        assert timeout is not None, "session.timeout must not be None"
        # httpx.Timeout exposes .read; confirm it matches the constant.
        read_timeout = getattr(timeout, "read", None)
        assert read_timeout == pytest.approx(CHROMA_HTTP_READ_TIMEOUT_S)


# ---------------------------------------------------------------------------
# Surface parity vs make_t3() (direct-mode T3Database)
# ---------------------------------------------------------------------------


class TestSurfaceParity:
    def test_public_method_set_matches_make_t3(
        self, live_t3_daemon, config_dir: Path
    ) -> None:
        """make_t3_client and make_t3 must return objects with identical
        public method sets (same class — T3Database — by construction).
        Catches accidental shim-class introduction."""
        from nexus.daemon.t3_client import make_t3_client
        from nexus.db.t3 import T3Database

        t3_client = make_t3_client(config_dir=config_dir)
        # Both objects are T3Database; same class → same public surface.
        assert type(t3_client) is T3Database

        # Belt-and-suspenders: enumerate public methods of T3Database and
        # confirm every one is callable on the returned instance.
        public_methods = [
            name for name, member in inspect.getmembers(T3Database, callable)
            if not name.startswith("_")
        ]
        for name in public_methods:
            assert callable(getattr(t3_client, name)), (
                f"{name} not callable on make_t3_client() return"
            )
