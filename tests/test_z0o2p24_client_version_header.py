# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 Phase 3 Step 2 (nexus-z0o2p.24): every engine request names the client's version.

The engine logs ``X-Nexus-Client-Version`` on its ``ownerless_chunk_write_*`` lines, and ``absent``
when it is missing, so the log-only soak can tell which clients still write a chunk before its owner:
a request without the header comes from a client older than the release that added it. The
``User-Agent`` cannot do this (``Python-urllib/3.12``, ``python-httpx/0.28``: the transport, not the
product). A transport that dropped the header would make a current client look old, so each
transport the client uses has its own test:

* urllib: ``HttpVectorClient`` (``/v1/vectors/upsert-chunks``, ``/store-put`` and every other vector route);
* httpx: the T2 stores and the catalog client (``RefreshableHttpStoreMixin``), and the T1 scratch store.

The only writers of ``upsert-chunks`` and ``store-put`` in this repository are on the urllib transport.
Callers outside the repository (a conexus tool on httpx, say) send whatever they send; the engine's
log names them by the ABSENCE of this header.
"""
from __future__ import annotations

import httpx
import pytest

import nexus.db.http_vector_client as hvc
from nexus.db.client_identity import CLIENT_VERSION_HEADER, client_identity_headers, client_version


def test_the_header_name_is_the_one_the_engine_reads() -> None:
    from pathlib import Path

    handler = (
        Path(__file__).resolve().parents[1]
        / "service/src/main/java/dev/nexus/service/http/VectorHandler.java"
    ).read_text()
    assert f'CLIENT_VERSION_HEADER = "{CLIENT_VERSION_HEADER}"' in handler


def test_the_version_is_the_installed_distributions() -> None:
    from importlib.metadata import version

    assert client_version() == version("conexus")
    assert client_identity_headers() == {CLIENT_VERSION_HEADER: version("conexus")}


# ── urllib: HttpVectorClient ─────────────────────────────────────────────────


class _FakeResponse:
    status = 200
    headers: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return b'{"upserted": 1}'


@pytest.mark.parametrize("path", ["/v1/vectors/upsert-chunks", "/v1/vectors/store-put", "/v1/vectors/search"])
def test_the_urllib_transport_sends_the_version_on_every_vector_route(
    monkeypatch: pytest.MonkeyPatch, path: str,
) -> None:
    seen: list = []

    class _Opener:
        def open(self, req, timeout=None):
            seen.append(req)
            return _FakeResponse()

    monkeypatch.setattr(hvc, "_keepalive_opener", lambda: _Opener())
    monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: ("http://127.0.0.1:9", "tok"))

    hvc._request_once("POST", path, tenant="t1", timeout=5, body={"collection": "c"})

    assert len(seen) == 1
    # urllib stores header names capitalised ("X-nexus-client-version"); compare case-insensitively.
    sent = {k.lower(): v for k, v in seen[0].header_items()}
    assert sent[CLIENT_VERSION_HEADER.lower()] == client_version()


def test_a_write_through_the_public_client_method_carries_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole path, not just ``_request_once``: ``HttpVectorClient.upsert_chunks`` -> ``_post``."""
    seen: list = []

    class _Opener:
        def open(self, req, timeout=None):
            seen.append(req)
            return _FakeResponse()

    monkeypatch.setattr(hvc, "_keepalive_opener", lambda: _Opener())
    monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: ("http://127.0.0.1:9", "tok"))
    monkeypatch.setattr(
        "nexus.corpus.write_with_registration_retry", lambda collection, send, registrar=None: send(),
    )
    client = hvc.HttpVectorClient(tenant="t1")
    client.upsert_chunks("knowledge__x__bge-base-en-v15-768__v1", ["a" * 64], ["text"], [{}])

    writes = [r for r in seen if r.full_url.endswith("/v1/vectors/upsert-chunks")]
    assert writes, "the write reached the transport"
    sent = {k.lower(): v for k, v in writes[0].header_items()}
    assert sent[CLIENT_VERSION_HEADER.lower()] == client_version()


# ── httpx: the T2 stores and the catalog client, and the T1 scratch store ─────


def test_the_httpx_transport_sends_the_version_from_the_refreshable_store_base() -> None:
    from nexus.db.t2.http_memory_store import HttpMemoryStore

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={})

    store = HttpMemoryStore(base_url="http://127.0.0.1:9", _token="tok")
    store._client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        store._get("/v1/memory/list")
    finally:
        store.close()

    assert len(captured) == 1
    assert captured[0].headers[CLIENT_VERSION_HEADER] == client_version()


def test_the_catalog_client_inherits_the_header_from_the_same_base() -> None:
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin

    assert issubclass(HttpCatalogClient, RefreshableHttpStoreMixin)
    assert "_auth_headers" not in HttpCatalogClient.__dict__, (
        "the catalog client must not override _auth_headers: it would drop the version header"
    )


def test_the_httpx_scratch_store_sends_the_version() -> None:
    from nexus.db.http_scratch_store import HttpScratchStore

    store = HttpScratchStore(base_url="http://127.0.0.1:1", _token="bearer")
    try:
        request = store._client.build_request("POST", "/v1/scratch/list")
        assert request.headers[CLIENT_VERSION_HEADER] == client_version()
    finally:
        store.close()
