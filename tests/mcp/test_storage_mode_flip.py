# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-112 P3.1 (nexus-hpxl): MCP flip behind NX_STORAGE_MODE.

Tests the two integration seams Phase 3 cuts over:

- ``mcp_infra.get_t3()`` — under ``NX_STORAGE_MODE=daemon`` returns a
  ``T3Database`` backed by ``chromadb.HttpClient`` via the
  ``make_t3_client`` factory (nexus-7yd2). Under ``direct`` returns the
  existing direct-PersistentClient T3Database.
- ``mcp_infra.t2_ctx()`` — under ``daemon`` returns a ``T2Client``
  bound to the discovery-resolved address (UDS first, TCP fallback per
  RDR-112 §6). Under ``direct`` returns the existing ``T2Database``.

Auto-spawn is forbidden in daemon mode — the contract is fail-loud.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def reset_t3_singleton():
    """``get_t3`` caches a singleton. Reset before + after each test
    so daemon-mode + direct-mode tests don't pollute each other."""
    import nexus.mcp_infra as infra
    original = infra._t3_instance
    infra._t3_instance = None
    yield
    # Best-effort: don't fail teardown if shape changed.
    infra._t3_instance = original


@pytest.fixture
def force_direct_mode(monkeypatch):
    monkeypatch.setenv("NX_STORAGE_MODE", "direct")


@pytest.fixture
def force_daemon_mode(monkeypatch):
    monkeypatch.setenv("NX_STORAGE_MODE", "daemon")
    monkeypatch.setenv("NX_LOCAL", "1")  # daemon path is local-mode only


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
def live_t3_daemon(config_dir: Path, local_path: Path, force_daemon_mode):
    """Spawn a real T3 daemon; expose it to mcp_infra via NEXUS_CONFIG_DIR."""
    from nexus.daemon.t3_daemon import start_t3_daemon, stop_t3_daemon

    payload = start_t3_daemon(config_dir=config_dir, local_path=local_path)
    old_env = os.environ.get("NEXUS_CONFIG_DIR")
    os.environ["NEXUS_CONFIG_DIR"] = str(config_dir)
    try:
        yield payload
    finally:
        if old_env is None:
            os.environ.pop("NEXUS_CONFIG_DIR", None)
        else:
            os.environ["NEXUS_CONFIG_DIR"] = old_env
        stop_t3_daemon(config_dir=config_dir)


# ---------------------------------------------------------------------------
# get_t3() daemon branch
# ---------------------------------------------------------------------------


class TestGetT3DaemonMode:
    def test_returns_t3database_backed_by_httpclient(
        self, live_t3_daemon, reset_t3_singleton
    ) -> None:
        from nexus.db.t3 import T3Database
        import nexus.mcp_infra as infra

        t3 = infra.get_t3()
        assert isinstance(t3, T3Database)
        # HttpClient's ``_server`` has ``_session``; PersistentClient's
        # ``_server`` is a RustBindingsAPI with no ``_session``. This
        # is the smoke-test for the routing decision.
        server = getattr(t3._client, "_server", None)
        assert server is not None and hasattr(server, "_session"), (
            "daemon mode get_t3 must inject HttpClient "
            "(server with _session); got "
            f"{type(server).__module__}.{type(server).__name__}"
        )

    def test_round_trip_query_through_get_t3(
        self, live_t3_daemon, reset_t3_singleton
    ) -> None:
        import nexus.mcp_infra as infra

        t3 = infra.get_t3()
        coll = t3._client.get_or_create_collection("hpxl_smoke")
        coll.upsert(documents=["alpha", "beta"], ids=["a", "b"])
        result = coll.query(query_texts=["alpha"], n_results=1)
        assert result["ids"][0] == ["a"]


class TestGetT3DaemonModeFailLoud:
    def test_no_daemon_raises_t3_daemon_error(
        self, force_daemon_mode, config_dir: Path, monkeypatch,
        reset_t3_singleton,
    ) -> None:
        """No daemon running + daemon mode active → fail loud. No
        auto-spawn (matches RDR-112 §Incremental adoption contract)."""
        from nexus.daemon.t3_client import T3DaemonError
        import nexus.mcp_infra as infra

        monkeypatch.delenv("NX_T3_ADDR", raising=False)
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))
        with pytest.raises(T3DaemonError) as excinfo:
            infra.get_t3()
        assert "nx daemon t3 start" in str(excinfo.value)


class TestGetT3DirectMode:
    def test_returns_existing_persistent_client_path(
        self, force_direct_mode, reset_t3_singleton, monkeypatch
    ) -> None:
        """Direct mode keeps the existing make_t3() behaviour. The
        returned client must NOT have ``_server`` (PersistentClient
        does not; only HttpClient does)."""
        from nexus.db.t3 import T3Database
        import nexus.mcp_infra as infra

        monkeypatch.setenv("NX_LOCAL", "1")
        t3 = infra.get_t3()
        assert isinstance(t3, T3Database)
        # PersistentClient's ``_server`` is a RustBindingsAPI with no
        # ``_session`` attribute; HttpClient's server has one. The
        # session attribute is the discriminator.
        server = getattr(t3._client, "_server", None)
        assert not (server is not None and hasattr(server, "_session")), (
            "direct mode must use PersistentClient, got HttpClient-shaped "
            "client (server has _session attr)"
        )


# ---------------------------------------------------------------------------
# t2_ctx() daemon branch
# ---------------------------------------------------------------------------


class TestT2CtxDaemonMode:
    def test_env_var_resolves_to_uds_client(
        self, force_daemon_mode, monkeypatch, tmp_path: Path
    ) -> None:
        """``NX_T2_SOCK`` env var resolves to a T2Client bound to the
        UDS path (file resolution not consulted)."""
        from nexus.daemon.t2_client import T2Client
        import nexus.mcp_infra as infra

        sock_path = tmp_path / "fake-t2.sock"
        monkeypatch.setenv("NX_T2_SOCK", str(sock_path))
        client = infra.t2_ctx()
        assert isinstance(client, T2Client)
        assert client._uds_path == sock_path
        assert client._tcp_addr is None

    def test_env_var_addr_resolves_to_tcp_client(
        self, force_daemon_mode, monkeypatch
    ) -> None:
        """``NX_T2_ADDR=host:port`` resolves to a TCP T2Client."""
        from nexus.daemon.t2_client import T2Client
        import nexus.mcp_infra as infra

        monkeypatch.delenv("NX_T2_SOCK", raising=False)
        monkeypatch.setenv("NX_T2_ADDR", "127.0.0.1:65432")
        client = infra.t2_ctx()
        assert isinstance(client, T2Client)
        assert client._tcp_addr == ("127.0.0.1", 65432)
        assert client._uds_path is None

    def test_no_daemon_raises_daemon_not_running(
        self, force_daemon_mode, config_dir: Path, monkeypatch
    ) -> None:
        """Daemon mode + no env var + no discovery file → fail loud."""
        from nexus.daemon.discovery import DaemonNotRunningError
        import nexus.mcp_infra as infra

        monkeypatch.delenv("NX_T2_SOCK", raising=False)
        monkeypatch.delenv("NX_T2_ADDR", raising=False)
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))
        with pytest.raises(DaemonNotRunningError) as excinfo:
            infra.t2_ctx()
        assert "nx daemon t2 start" in str(excinfo.value)

    def test_path_resolver_under_daemon_still_rejected(
        self, force_daemon_mode, monkeypatch
    ) -> None:
        """Pre-existing safety check: ``_path_resolver=`` is meaningless
        under daemon mode (daemon owns the path). Must continue to
        reject loudly (RDR-112 P1 prereq, foundation review 2026-05-14)."""
        import nexus.mcp_infra as infra

        monkeypatch.setenv("NX_T2_SOCK", "/tmp/fake.sock")
        with pytest.raises(RuntimeError) as excinfo:
            infra.t2_ctx(_path_resolver=lambda: Path("/tmp/x"))
        assert "_path_resolver" in str(excinfo.value)


class TestT2CtxDirectMode:
    def test_returns_t2database_unchanged(
        self, force_direct_mode, monkeypatch, tmp_path: Path
    ) -> None:
        """Direct mode keeps the existing T2Database construction."""
        from nexus.db.t2 import T2Database
        import nexus.mcp_infra as infra

        monkeypatch.setattr(
            "nexus.mcp_infra.default_db_path",
            lambda: tmp_path / "memory.db",
        )
        ctx = infra.t2_ctx()
        assert isinstance(ctx, T2Database)
        ctx.close()


# ---------------------------------------------------------------------------
# UDS-then-TCP fallback (discovery file path)
# ---------------------------------------------------------------------------


class TestDiscoveryFileFallback:
    """When no env var is set, t2_ctx resolves via the discovery file
    and prefers UDS over TCP (RDR-112 §6)."""

    def test_file_resolution_prefers_uds_when_present(
        self, force_daemon_mode, config_dir: Path, monkeypatch, tmp_path: Path
    ) -> None:
        from nexus.daemon.discovery import discovery_path
        from nexus.daemon.t2_client import T2Client
        import nexus.mcp_infra as infra
        import os as _os

        monkeypatch.delenv("NX_T2_SOCK", raising=False)
        monkeypatch.delenv("NX_T2_ADDR", raising=False)
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))
        sock_path = tmp_path / "real-t2.sock"
        disc = discovery_path(config_dir, tier="t2")
        disc.write_text(json.dumps({
            "format_version": 1,
            "uds_path": str(sock_path),
            "tcp_host": "127.0.0.1",
            "tcp_port": 9700,
            "pid": _os.getpid(),
            "daemon_version": "test",
            "start_time": "1970-01-01T00:00:00",
        }))
        _os.chmod(str(disc), 0o600)

        client = infra.t2_ctx()
        assert isinstance(client, T2Client)
        assert client._uds_path == sock_path
        assert client._tcp_addr is None

    def test_file_resolution_falls_back_to_tcp_when_no_uds(
        self, force_daemon_mode, config_dir: Path, monkeypatch
    ) -> None:
        """Discovery payload without uds_path → T2Client bound to TCP."""
        from nexus.daemon.discovery import discovery_path
        from nexus.daemon.t2_client import T2Client
        import nexus.mcp_infra as infra
        import os as _os

        monkeypatch.delenv("NX_T2_SOCK", raising=False)
        monkeypatch.delenv("NX_T2_ADDR", raising=False)
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))
        disc = discovery_path(config_dir, tier="t2")
        disc.write_text(json.dumps({
            "format_version": 1,
            "tcp_host": "127.0.0.1",
            "tcp_port": 9701,
            "pid": _os.getpid(),
            "daemon_version": "test",
            "start_time": "1970-01-01T00:00:00",
        }))
        _os.chmod(str(disc), 0o600)

        client = infra.t2_ctx()
        assert isinstance(client, T2Client)
        assert client._tcp_addr == ("127.0.0.1", 9701)
        assert client._uds_path is None
