# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-155 P4a.2 (nexus-1k8s1): ``make_t3()`` service dispatch.

History: RDR-120 P2/P6 made ``make_t3()`` route local mode through the
T3 chroma daemon (``nexus.daemon.t3_client.make_t3_client``). The
Phase-4a serving retire deletes that leg AND the direct CloudClient
leg: with no injected ``_client``, ``make_t3()`` returns the
pgvector-service-backed
:class:`~nexus.db.http_vector_client.HttpVectorClient` singleton in
BOTH modes. The injected-``_client`` short-circuit (the dry-run indexer
pattern, the test ephemeral-client pattern) is unchanged.

This file pins the dispatch table so a regression cannot quietly
reintroduce a direct Chroma client construction.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _pin_local_mode(monkeypatch):
    """RDR-120 §A8 / pin_local_mode_in_cloud_tests feedback: tests
    that depend on the local-vs-cloud branch must patch
    ``nexus.config.is_local_mode`` (not nexus.scoring.is_local_mode).
    The default for this file is local-mode True; cloud tests opt
    out explicitly."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)


@pytest.fixture(autouse=True)
def _reset_service_singleton():
    """Each test sees a fresh HttpVectorClient singleton."""
    from nexus.db.http_vector_client import reset_http_vector_client_for_tests

    reset_http_vector_client_for_tests()
    yield
    reset_http_vector_client_for_tests()


class TestServiceDispatch:
    def test_local_mode_returns_service_client(self) -> None:
        """Local mode + no injected client → the HttpVectorClient
        singleton (pgvector service), NOT a chroma daemon client."""
        from nexus.db import make_t3
        from nexus.db.http_vector_client import (
            HttpVectorClient,
            get_http_vector_client,
        )

        result = make_t3()
        assert isinstance(result, HttpVectorClient)
        assert result is get_http_vector_client()

    def test_storage_mode_env_has_no_effect(self, monkeypatch) -> None:
        """The RDR-120 ``NX_STORAGE_MODE`` values (daemon/direct) are
        inert post-cutover: every value lands on the service client."""
        from nexus.db import make_t3
        from nexus.db.http_vector_client import HttpVectorClient

        for value in ("daemon", "direct", "anything"):
            monkeypatch.setenv("NX_STORAGE_MODE", value)
            assert isinstance(make_t3(), HttpVectorClient)

    def test_injected_client_short_circuits_service_dispatch(self) -> None:
        """An injected ``_client`` (the dry-run indexer pattern, the
        test ephemeral-client pattern) must return the T3Database
        facade over that client — no service involvement."""
        import chromadb
        from nexus.db import make_t3
        from nexus.db.t3 import T3Database

        ephemeral = chromadb.EphemeralClient()
        result = make_t3(_client=ephemeral)
        assert isinstance(result, T3Database)
        assert result._client is ephemeral

    def test_cloud_mode_returns_service_client_no_cloudclient(
        self, monkeypatch
    ) -> None:
        """Cloud mode + no injected client → the service client too;
        ``chromadb.CloudClient`` is never constructed (the direct
        cloud serving leg is retired)."""
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)

        constructed = {"count": 0}

        class _TripwireCloudClient:
            def __init__(self, *args, **kwargs) -> None:
                constructed["count"] += 1

        monkeypatch.setattr("chromadb.CloudClient", _TripwireCloudClient)

        from nexus.db import make_t3
        from nexus.db.http_vector_client import HttpVectorClient

        result = make_t3()
        assert isinstance(result, HttpVectorClient)
        assert constructed["count"] == 0
