# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 P2 (nexus-ut8zy): ``make_t3()`` honours ``NX_STORAGE_MODE``.

Pre-P2: ``make_t3()`` ignored ``NX_STORAGE_MODE`` and dispatched on
``is_local_mode()`` alone (PersistentClient or CloudClient).

P2: when ``NX_STORAGE_MODE=daemon`` AND local mode AND no injected
``_client``, ``make_t3()`` dispatches through
``nexus.daemon.t3_client.make_t3_client`` against the running T3
daemon. Cloud mode + an injected ``_client`` short-circuit the
daemon path so existing tests and the dry-run indexer keep working.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _pin_local_mode(monkeypatch):
    """RDR-120 §A8 / pin_local_mode_in_cloud_tests feedback: tests
    that depend on the local-vs-cloud branch must patch
    ``nexus.config.is_local_mode`` (not nexus.scoring.is_local_mode).
    The default for this file is local-mode True; cloud tests opt
    out explicitly."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)


class TestDaemonDispatch:
    def test_daemon_mode_routes_through_make_t3_client(
        self, monkeypatch
    ) -> None:
        """When NX_STORAGE_MODE=daemon and local, make_t3() must
        call nexus.daemon.t3_client.make_t3_client and return its
        result rather than building a PersistentClient."""
        monkeypatch.setenv("NX_STORAGE_MODE", "daemon")
        sentinel = object()
        called = {"count": 0}

        def fake_make_t3_client():
            called["count"] += 1
            return sentinel

        monkeypatch.setattr(
            "nexus.daemon.t3_client.make_t3_client", fake_make_t3_client
        )
        from nexus.db import make_t3

        result = make_t3()
        assert result is sentinel
        assert called["count"] == 1

    def test_direct_mode_does_not_route_through_daemon(
        self, monkeypatch
    ) -> None:
        """NX_STORAGE_MODE=direct (the default) must NOT call
        make_t3_client; make_t3 falls through to the PersistentClient
        path so the existing direct-mode code path is unchanged."""
        monkeypatch.setenv("NX_STORAGE_MODE", "direct")
        called = {"count": 0}

        def fake_make_t3_client():
            called["count"] += 1
            raise AssertionError(
                "make_t3_client must not be called in direct mode"
            )

        monkeypatch.setattr(
            "nexus.daemon.t3_client.make_t3_client", fake_make_t3_client
        )
        from nexus.db import make_t3
        from nexus.db.t3 import T3Database

        result = make_t3()
        assert isinstance(result, T3Database)
        assert called["count"] == 0

    def test_injected_client_short_circuits_daemon_dispatch(
        self, monkeypatch
    ) -> None:
        """An injected ``_client`` (the dry-run indexer pattern, the
        test ephemeral-client pattern) must short-circuit the daemon
        dispatch even when NX_STORAGE_MODE=daemon. Otherwise tests
        and the dry-run code path would crash with T3DaemonError
        when no daemon is running."""
        monkeypatch.setenv("NX_STORAGE_MODE", "daemon")

        def fake_make_t3_client():
            raise AssertionError(
                "daemon dispatch must be skipped when _client is injected"
            )

        monkeypatch.setattr(
            "nexus.daemon.t3_client.make_t3_client", fake_make_t3_client
        )
        import chromadb
        from nexus.db import make_t3
        from nexus.db.t3 import T3Database

        ephemeral = chromadb.EphemeralClient()
        result = make_t3(_client=ephemeral)
        assert isinstance(result, T3Database)
        assert result._client is ephemeral

    def test_cloud_mode_ignores_daemon_flag(self, monkeypatch) -> None:
        """Cloud mode has no local daemon; the daemon flag must be
        ignored so the cloud branch keeps using CloudClient.
        """
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
        monkeypatch.setenv("NX_STORAGE_MODE", "daemon")
        # Fake credentials so the cloud branch reaches the
        # T3Database constructor without exploding on missing keys;
        # we don't actually hit chromadb cloud because we patch the
        # client construction.
        monkeypatch.setattr(
            "nexus.config.get_credential",
            lambda key: {
                "chroma_tenant": "t", "chroma_database": "d",
                "chroma_api_key": "k", "voyage_api_key": "v",
            }.get(key, ""),
        )
        # Also patch the db package-level shim that re-imports
        # get_credential at function scope.
        monkeypatch.setattr(
            "nexus.db.get_credential",
            lambda key: {
                "chroma_tenant": "t", "chroma_database": "d",
                "chroma_api_key": "k", "voyage_api_key": "v",
            }.get(key, ""),
        )

        def fake_make_t3_client():
            raise AssertionError(
                "daemon dispatch must not fire in cloud mode"
            )

        monkeypatch.setattr(
            "nexus.daemon.t3_client.make_t3_client", fake_make_t3_client
        )

        # Stub the CloudClient so we don't make a real network call.
        class _FakeCloudClient:
            def __init__(self, *args, **kwargs) -> None: ...

        monkeypatch.setattr("chromadb.CloudClient", _FakeCloudClient)
        # Stub voyageai so the lazy import inside T3Database.__init__
        # doesn't try to authenticate.
        import sys
        import types
        fake_voyageai = types.SimpleNamespace(
            Client=lambda *a, **kw: object(),
        )
        monkeypatch.setitem(sys.modules, "voyageai", fake_voyageai)

        from nexus.db import make_t3
        result = make_t3()
        # The fake CloudClient is what landed; make_t3 did NOT route
        # through the daemon factory.
        assert isinstance(result._client, _FakeCloudClient)
