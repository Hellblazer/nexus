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
from tests.conftest import make_vector_test_client
@pytest.fixture(autouse=True)
def _stub_managed_service_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise the RDR-002 engine-version pin (nexus-aqbrk).

    These tests construct a real vector client against the session test
    engine, whose jar is built by `mvn package` and therefore carries NO
    stamped release_version — blank in source, stamped only at native-build
    time from the tag. VersionHandler reports release_version=null and the
    client's version pin FAIL-CLOSES by design:

        ManagedServiceIncompatible: ... reported no usable release_version on
        /version (got None) — a dev/unstamped or pre-release engine is older
        than the minimum this client supports (v0.1.57)

    That is an ENVIRONMENT artifact of testing against a dev build, not a
    substrate semantic, and the pin is NOT the subject of this file — dispatch
    is. Stubbed via the same seam tests/test_health.py already uses.

    RELATED: nexus-12m77 category (a) is the same collision at integration
    scope, where its recommended fix is to STAMP the test jar. If that lands,
    this stub becomes unnecessary and should be deleted rather than kept.
    """
    import nexus.db.managed_endpoint as _me

    monkeypatch.setattr(_me, "probe_managed_service", lambda **_kw: None)


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
        from nexus.db import make_t3
        from nexus.db.t3 import T3Database

        ephemeral = make_vector_test_client()
        result = make_t3(_client=ephemeral)
        assert isinstance(result, T3Database)
        assert result._client is ephemeral

    def test_cloud_mode_returns_service_client(self, monkeypatch) -> None:
        """Cloud mode + no injected client → the service client.

        P3: this carried a ``monkeypatch.setattr("chromadb.CloudClient", ...)``
        tripwire counting constructions. chromadb is no longer installed, so
        the tripwire could not be installed either — and the property it
        guarded ("the direct cloud serving leg is retired") is now guaranteed
        by the dependency's absence rather than by a counter. Module-absence is
        asserted in test_rdr155_p4b_deletion_gate.py; what this test still owns
        is the dispatch itself.
        """
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)

        from nexus.db import make_t3
        from nexus.db.http_vector_client import HttpVectorClient

        result = make_t3()
        assert isinstance(result, HttpVectorClient)
