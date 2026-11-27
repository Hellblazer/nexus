# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 Phase 3 Step 2 (nexus-z0o2p.24): the vector, T2 (token store included), catalog and scratch clients name the client's version.

The engine logs ``X-Nexus-Client-Version`` on its ``ownerless_chunk_write_*`` lines, and ``absent``
when it is missing, so the log-only soak can tell which clients still write a chunk before its owner:
a request without the header comes from a client older than the release that added it. The
``User-Agent`` cannot do this (``Python-urllib/3.12``, ``python-httpx/0.28``: the transport, not the
product). A transport that dropped the header would make a current client look old, so each
transport the client uses has its own test:

* urllib: ``HttpVectorClient`` (``/v1/vectors/upsert-chunks`` and every other vector route);
* httpx: the T2 stores and the catalog client (``RefreshableHttpStoreMixin``), the token store (its own
  client) and the T1 scratch store.

The hook path (``mailbox_drain``) and ``db/http_engine_status`` send no
version header; none of them writes a chunk.

The only writer of ``upsert-chunks`` in this repository is on the urllib transport.
Callers outside the repository (a conexus tool on httpx, say) send whatever they send; the engine's
log names them by the ABSENCE of this header.
"""
from __future__ import annotations

import ast
import contextlib
import re
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

import nexus.db.http_vector_client as hvc
from nexus import correlation
from nexus.backup.cloud_export import ExportClient
from nexus.backup.cloud_import import ImportClient
from nexus.backup.pg_backup import BackupRefused
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.db.http_scratch_store import HttpScratchStore
from nexus.db.t2.http_memory_store import HttpMemoryStore
from nexus.db.t2.http_token_store import HttpTokenStore
from nexus.db.client_identity import CLIENT_VERSION_HEADER, client_identity_headers, client_version
from tests._changelog import changelog_text


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


@pytest.mark.parametrize("path", ["/v1/vectors/upsert-chunks", "/v1/vectors/search"])
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


def test_the_token_store_sends_the_version() -> None:
    """``HttpTokenStore`` builds its own client (baked bearer), so it is not covered by the mixin."""
    from nexus.db.t2.http_token_store import HttpTokenStore

    store = HttpTokenStore(base_url="http://127.0.0.1:1", _token="bearer")
    try:
        request = store._client.build_request("GET", "/v1/tokens/list")
        assert request.headers[CLIENT_VERSION_HEADER] == client_version()
    finally:
        store._client.close()


@pytest.mark.t2_tenant
def test_the_httpx_scratch_store_sends_the_version() -> None:
    from nexus.db.http_scratch_store import HttpScratchStore

    store = HttpScratchStore(base_url="http://127.0.0.1:1", _token="bearer")
    try:
        request = store._client.build_request("POST", "/v1/scratch/list")
        assert request.headers[CLIENT_VERSION_HEADER] == client_version()
    finally:
        store.close()


# -- RDR-167 Step 2 (nexus-dngtz.1.3): X-Correlation-ID on every engine client's wire ------------------
#
# One driver per sender: it builds the client (the BUILD happens before any id is bound in the late-bind
# tests) and returns a ``send`` that makes one real request through the client's own code path into a
# transport that records the request AS SENT. A driver that asserts from outside the request path would
# pass on a header the wire never carries. The census below pins that every module importing
# ``client_identity`` has a driver per ``client_identity_headers()`` call site.

_CID_HEADER = correlation.HEADER.lower()


class _Wire:
    """The requests that reached the transport, header names lower-cased."""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    def record(self, headers: object) -> None:
        self.sent.append({k.lower(): v for k, v in headers.items()})  # type: ignore[attr-defined]

    def handler(self, body: dict | None = None) -> Callable[[httpx.Request], httpx.Response]:
        def _handle(request: httpx.Request) -> httpx.Response:
            self.record(request.headers)
            return httpx.Response(200, json=body if body is not None else {})
        return _handle


@contextlib.contextmanager
def _bound(cid: str | None) -> Iterator[None]:
    """Bind *cid* for the block and reset it by token; ``None`` binds nothing."""
    if cid is None:
        yield
        return
    token = correlation.bind(cid)
    try:
        yield
    finally:
        correlation.reset(token)


class _Creds:
    def bearer(self) -> str:
        return "tok"

    def rejected(self, sent: str) -> bool:
        return False


def _client_over(monkeypatch: pytest.MonkeyPatch, wire: _Wire, body: dict | None = None) -> None:
    """Make every ``httpx.Client`` a production module builds from now on talk to *wire*'s transport. The
    production keyword arguments (``event_hooks`` included) are passed through unchanged."""
    real = httpx.Client
    transport = httpx.MockTransport(wire.handler(body))
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: real(*a, transport=transport, **kw))


def _drive_t3(mp: pytest.MonkeyPatch, wire: _Wire) -> Callable[[], None]:
    class _Opener:
        def open(self, req, timeout=None):
            wire.record(dict(req.header_items()))
            return _FakeResponse()

    mp.setattr(hvc, "_keepalive_opener", lambda: _Opener())
    mp.setattr(hvc, "_resolve_endpoint", lambda: ("http://127.0.0.1:9", "tok"))
    return lambda: hvc._request_once("POST", "/v1/vectors/search", tenant="t1", timeout=5, body={"collection": "c"})


def _drive_mixin(mp: pytest.MonkeyPatch, wire: _Wire) -> Callable[[], None]:
    store = HttpMemoryStore(base_url="http://127.0.0.1:9", _token="tok")
    store._client = httpx.Client(transport=httpx.MockTransport(wire.handler()))
    return lambda: store._get("/v1/memory/list")


def _drive_catalog(mp: pytest.MonkeyPatch, wire: _Wire) -> Callable[[], None]:
    client = HttpCatalogClient(
        base_url="http://127.0.0.1:9", _token="tok",
        client=httpx.Client(transport=httpx.MockTransport(wire.handler())),
    )
    return lambda: client._get("/owners")


def _drive_scratch(mp: pytest.MonkeyPatch, wire: _Wire) -> Callable[[], None]:
    _client_over(mp, wire)
    store = HttpScratchStore(base_url="http://127.0.0.1:9", _token="tok", session_id="s1", _session_token="st")
    return lambda: store._post("/v1/t1/scratch/list", {}, mutates=False)


def _drive_token(mp: pytest.MonkeyPatch, wire: _Wire) -> Callable[[], None]:
    _client_over(mp, wire)
    store = HttpTokenStore(base_url="http://127.0.0.1:9", _token="tok")
    return lambda: store._get("/v1/tokens/list")


def _export(wire: _Wire, body: dict | None = None):
    return ExportClient(
        "http://127.0.0.1:9", _Creds(),  # type: ignore[arg-type]
        http=httpx.Client(transport=httpx.MockTransport(wire.handler(body))), sleep=lambda _s: None,
    )


def _import(wire: _Wire, body: dict | None = None):
    return ImportClient(
        "http://127.0.0.1:9", _Creds(),  # type: ignore[arg-type]
        http=httpx.Client(transport=httpx.MockTransport(wire.handler(body))), sleep=lambda _s: None,
    )


def _drive_export_request(mp: pytest.MonkeyPatch, wire: _Wire) -> Callable[[], None]:
    client = _export(wire)
    return lambda: client._request("GET", "/v1/tenant/export/x/rows", what="a page")


def _drive_export_probe(mp: pytest.MonkeyPatch, wire: _Wire) -> Callable[[], None]:
    client = _export(wire, {"tenant_export_supported": True})
    return lambda: client.check_engine()


def _drive_export_delete(mp: pytest.MonkeyPatch, wire: _Wire) -> Callable[[], None]:
    client = _export(wire)
    return lambda: client._delete_quietly("sid")


def _drive_import_request(mp: pytest.MonkeyPatch, wire: _Wire) -> Callable[[], None]:
    client = _import(wire)
    return lambda: client._exchange("GET", "/v1/tenant/import/x", what="a part")


def _drive_import_probe(mp: pytest.MonkeyPatch, wire: _Wire) -> Callable[[], None]:
    client = _import(wire, {"tenant_import_supported": True})

    def send() -> None:
        with contextlib.suppress(BackupRefused):  # the probe's verdict on the body is not under test
            client.check_engine()

    return send


def _drive_import_delete(mp: pytest.MonkeyPatch, wire: _Wire) -> Callable[[], None]:
    client = _import(wire)
    return lambda: client._delete_quietly("sid", "/v1/tenant/import")


#: Driver name -> driver. The export and import drivers say in their ids that sending the id to the cloud
#: engine is intended: the id is a random per-invocation token, it names no tenant data, and the engine
#: logs it so a failed cloud backup call can be tied to its client log line.
_DRIVERS: dict[str, Callable[[pytest.MonkeyPatch, _Wire], Callable[[], None]]] = {
    "t3": _drive_t3,
    "mixin": _drive_mixin,
    "catalog": _drive_catalog,
    "scratch": _drive_scratch,
    "token": _drive_token,
    "export_request": _drive_export_request,
    "export_probe": _drive_export_probe,
    "export_delete": _drive_export_delete,
    "import_request": _drive_import_request,
    "import_probe": _drive_import_probe,
    "import_delete": _drive_import_delete,
}
#: The two clients that bake their headers into an ``httpx.Client`` when it is built.
_BAKED = ("scratch", "token")
_A, _B = "a" * 32, "b" * 32


def _sent_headers(name: str, mp: pytest.MonkeyPatch, *, built_under: str | None, sent_under: str | None) -> list[dict[str, str]]:
    assert correlation.current_correlation_id() is None, "a correlation id leaked in from another test"
    wire = _Wire()
    with _bound(built_under):
        send = _DRIVERS[name](mp, wire)
    with _bound(sent_under):
        send()
    assert wire.sent, f"{name}: no request reached the transport, so nothing was checked"
    return wire.sent


@pytest.mark.parametrize("name", list(_DRIVERS))
def test_a_bound_id_is_on_the_wire_of_every_sender(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Includes the cloud export and import clients (``export_*``, ``import_*``): sending the id to the
    cloud engine is intended. It is a random per-invocation token naming no tenant data, and the engine
    logs it so a failed cloud backup call can be tied to its client log line."""
    sent = _sent_headers(name, monkeypatch, built_under=_A, sent_under=_A)
    assert [h.get(_CID_HEADER) for h in sent] == [_A] * len(sent)
    assert all(h[CLIENT_VERSION_HEADER.lower()] == client_version() for h in sent), "the version rides along"


@pytest.mark.parametrize("name", list(_DRIVERS))
def test_no_header_is_sent_when_no_id_is_bound(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _sent_headers(name, monkeypatch, built_under=None, sent_under=None)
    assert all(_CID_HEADER not in h for h in sent)


@pytest.mark.parametrize("name", list(_DRIVERS))
def test_a_client_built_before_the_bind_sends_the_later_id(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Built with nothing bound, sent under B."""
    sent = _sent_headers(name, monkeypatch, built_under=None, sent_under=_B)
    assert [h.get(_CID_HEADER) for h in sent] == [_B] * len(sent)


@pytest.mark.parametrize("name", list(_DRIVERS))
def test_a_client_built_under_one_id_sends_the_next_one_not_the_old(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _sent_headers(name, monkeypatch, built_under=_A, sent_under=_B)
    assert [h.get(_CID_HEADER) for h in sent] == [_B] * len(sent)


@pytest.mark.parametrize("name", list(_DRIVERS))
def test_a_client_built_under_an_id_sends_no_id_once_it_is_unbound(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The stale-id case: the scratch and token clients bake ``client_identity_headers()`` at build time,
    so the id bound then is in their default headers; the request hook must remove it."""
    sent = _sent_headers(name, monkeypatch, built_under=_A, sent_under=None)
    assert all(_CID_HEADER not in h for h in sent)


@pytest.mark.parametrize("name", _BAKED)
def test_the_rebuilt_client_carries_the_hook_too(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A client built by ``_build_client`` under id A still sends B. That every rebuild (the lease rebind, the
    401 re-mint) uses ``_build_client`` is pinned by the one-construction test below."""
    wire = _Wire()
    _client_over(monkeypatch, wire)
    if name == "scratch":
        with _bound(_A):
            store = HttpScratchStore(base_url="http://127.0.0.1:9", _token="tok", session_id="s1", _session_token="st")
            store._client = store._build_client()
        send = lambda: store._post("/v1/t1/scratch/list", {}, mutates=False)  # noqa: E731
    else:
        with _bound(_A):
            store = HttpTokenStore(base_url="http://127.0.0.1:9", _token="tok")
            store._client = store._build_client()
        send = lambda: store._get("/v1/tokens/list")  # noqa: E731
    with _bound(_B):
        send()
    with _bound(None):
        send()
    assert [h.get(_CID_HEADER) for h in wire.sent] == [_B, None]


@pytest.mark.parametrize("rel", ["src/nexus/db/http_scratch_store.py", "src/nexus/db/t2/http_token_store.py"])
def test_each_hooked_store_builds_its_httpx_client_in_one_place_with_the_hook(rel: str) -> None:
    """The scratch and token stores keep one long-lived client; the correlation hook only covers it if every
    construction goes through the one call that passes ``event_hooks``. A second ``httpx.Client(...)`` would
    bypass the hook with every wire test still green."""
    tree = ast.parse((Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8"))
    calls = [c for c in ast.walk(tree) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
             and c.func.attr == "Client" and isinstance(c.func.value, ast.Name) and c.func.value.id == "httpx"]
    assert len(calls) == 1, f"{rel}: {len(calls)} httpx.Client(...) calls; the hook covers exactly one"
    assert any(kw.arg == "event_hooks" for kw in calls[0].keywords), f"{rel}: httpx.Client(...) has no event_hooks"


def test_the_identity_headers_carry_the_bound_id_and_the_version() -> None:
    with _bound(_A):
        assert client_identity_headers() == {CLIENT_VERSION_HEADER: client_version(), correlation.HEADER: _A}
    assert client_identity_headers() == {CLIENT_VERSION_HEADER: client_version()}


# -- the scope of the claim ---------------------------------------------------
#
# The header is sent by the shared client classes (``HttpVectorClient``, ``RefreshableHttpStoreMixin``,
# ``HttpTokenStore``, ``HttpScratchStore``), not by every engine request. Two pins keep the documents'
# claim at that scope: a census of the HTTP call sites in ``src/nexus`` (a new one must be placed, so
# the claim cannot silently widen or narrow), and a check on how the documents state it. A call site is
# a call that resolves, through the module's own imports, to an entry point of httpx, requests, aiohttp,
# urllib3, ``urllib.request`` or ``http.client``, a ``curl`` / ``wget`` command run through ``subprocess``
# or ``os``, or an ``importlib.import_module`` of one of those libraries (``_makes_an_http_call``). What it
# cannot see is ``getattr(httpx, name)`` and a command assembled from fragments.

#: Every module under ``src/nexus`` that makes an HTTP call and does NOT send the header, with why. The
#: census fails on a call site missing from here and from the importers, and on an entry here that has no
#: call site left. A hook is never a sender (``nexus.db``'s package init runs first: measured in
#: ``client_identity``'s docstring).
_NOT_SENDERS: dict[str, str] = {
    # hooks: latency on every prompt / tool call
    "nexus/hooks/mailbox_drain.py": "hook",
    # RDR-253 Step 3: the SessionStart hook's identity client; client_version rides in the bind and
    # renew bodies, and importing nexus.db would put its package init on every hook call
    "nexus/identity/bind_client.py": "identity bind: the version is in the request body",
    # RDR-252 Step 4: the group and membership routes; the session's client version already reaches the
    # engine on its identity bind and on every renewal (bind_client above), and nothing here writes a chunk
    "nexus/groups.py": "group and membership routes: the version rides the identity bind and renewal",
    # probes and admin calls of the engine; none writes a chunk
    "nexus/commands/daemon.py": "engine probe: GET /health",
    "nexus/commands/doctor.py": "probes: health and the MinerU server",
    "nexus/daemon/binary_lifecycle.py": "engine probe: GET /version",
    "nexus/daemon/service_registry.py": "engine probe: loopback liveness",
    "nexus/daemon/storage_service_daemon.py": "engine probe and appliance admin calls",
    "nexus/db/data_token.py": "token mint (POST to the mint endpoint)",
    "nexus/db/http_engine_status.py": "engine probe: GET /v1/status",
    "nexus/db/managed_endpoint.py": "engine probe: GET /version",
    "nexus/db/serving_engine_version.py": "engine probe: GET /version (the one probe; mcp/channel delegates to it and makes no call of its own), RDR-231 note-write guard",
    "nexus/db/pooled_http.py": "connection-pool transport of http_vector_client; the sender is that module's _request_once",
    "nexus/db/t2/bearer_transport.py": "builds the client kwargs and urllib opener a bearer request goes through (nexus-ec37o); the callers send the headers",
    "nexus/health.py": "probes: PyPI, GET /v1/_whoami, health",
    "nexus/upgrade_ladder/provisioning.py": "engine probes: GET /version, /health",
    # test seams over an in-process fake: no engine
    "nexus/db/inmemory_pipeline.py": "MockTransport over an in-process engine",
    "nexus/db/t2/http_centroid_store.py": "MockTransport test seam; real requests use the mixin's headers",
    # third parties, not the engine
    "nexus/aspect_readers.py": "third-party URLs (HTTPS stat)",
    "nexus/bib_enricher.py": "third party: bibliographic APIs",
    "nexus/bib_enricher_openalex.py": "third party: OpenAlex",
    "nexus/commands/catalog_cmds/backfill.py": "third-party URLs (ETag capture)",
    "nexus/_mineru_pid.py": "MinerU server (loopback /health identity probe)",
    "nexus/commands/mineru.py": "MinerU server",
    "nexus/daemon/binary_install.py": "release download",
    "nexus/daemon/mineru_lifecycle.py": "MinerU server",
    "nexus/db/minilm_direct.py": "model download",
    "nexus/db/service_bge_model.py": "model download",
    "nexus/doctor_references.py": "third-party URLs (ETag)",
    "nexus/install_ping.py": "install ping, not the vector or T2 surface",
    "nexus/operators/llm_api.py": "third party: the Anthropic or OpenAI-compatible LLM endpoint",
    "nexus/pdf_extractor.py": "MinerU server",
    "nexus/pdf_stack.py": "runs uv to install the [pdf] extra; its curl/irm string is install advice shown to the user, never run",
}

_SRC = Path(__file__).resolve().parents[1] / "src"
_CLIENT_IDENTITY = "client_identity"


def _imports_client_identity(source: str) -> bool:
    """True when the module imports ``client_identity`` by any spelling: ``from nexus.db.client_identity
    import x``, ``from nexus.db import client_identity``, a relative ``from .client_identity import x`` or
    ``from . import client_identity``, ``import nexus.db.client_identity [as ci]``, and
    ``importlib.import_module("nexus.db.client_identity")`` or ``__import__(...)``."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[-1] == _CLIENT_IDENTITY or any(
                a.name == _CLIENT_IDENTITY for a in node.names
            ):
                return True
        elif isinstance(node, ast.Import):
            if any(a.name.split(".")[-1] == _CLIENT_IDENTITY for a in node.names):
                return True
        elif isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name in ("import_module", "__import__") and any(
                isinstance(c, ast.Constant) and isinstance(c.value, str) and _CLIENT_IDENTITY in c.value
                for c in ast.walk(node)
            ):
                return True
    return False


#: The callables that open a connection or send a request, per HTTP library. A call is an HTTP call site when
#: its callee RESOLVES (through the module's own import table) to ``<module>.<name>`` with the pair listed
#: here, whatever the module or the name was spelled as locally: ``import requests``, ``import httpx as hx``,
#: ``from httpx import Client as C``, ``from urllib import request``, ``import http.client as hc`` all reach
#: the same entry. Constructors that only build a value (``httpx.Timeout``, ``httpx.MockTransport``) are
#: not call sites.
_HTTP_ENTRY_POINTS: dict[str, frozenset[str]] = {
    "httpx": frozenset({"Client", "AsyncClient", "get", "post", "put", "delete", "request", "stream", "patch", "head", "options"}),
    "requests": frozenset({"Session", "get", "post", "put", "delete", "request", "patch", "head", "options"}),
    "aiohttp": frozenset({"ClientSession", "request"}),
    "urllib3": frozenset({"PoolManager", "ProxyManager", "HTTPConnectionPool", "HTTPSConnectionPool", "connection_from_url", "request"}),
    "urllib.request": frozenset({"Request", "urlopen", "build_opener", "urlretrieve"}),
    "http.client": frozenset({"HTTPConnection", "HTTPSConnection"}),
}
_HTTP_LIBRARY_ROOTS = frozenset(m.split(".")[0] for m in _HTTP_ENTRY_POINTS)
#: The calls that run a command, for the ``curl`` / ``wget`` check.
_COMMAND_RUNNERS = frozenset({
    "subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call", "subprocess.check_output",
    "subprocess.getoutput", "subprocess.getstatusoutput", "os.system", "os.popen",
    "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
})
_HTTP_COMMAND = re.compile(r"^(?:\S*/)?(?:curl|wget)(?:\s|$)")


def _import_table(tree: ast.AST) -> dict[str, str]:
    """Local name -> the dotted name it stands for, from every ``import`` in the module (function-local
    ones too): ``import a.b`` binds ``a``; ``import a.b as x`` binds ``x`` to ``a.b``; ``from a import b
    [as x]`` binds ``b`` (or ``x``) to ``a.b``. A relative import names a module of this package, never
    an HTTP library, so it is left out."""
    table: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    table[a.asname] = a.name
                else:
                    head = a.name.split(".")[0]
                    table[head] = head
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for a in node.names:
                table[a.asname or a.name] = f"{node.module}.{a.name}"
    return table


def _resolve(node: ast.AST, table: dict[str, str]) -> str | None:
    """The dotted name a ``Name`` / ``Attribute`` chain stands for through ``table``, or None when its base
    is not an imported name (a local variable that happens to be called ``requests``, say)."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name) or node.id not in table:
        return None
    return ".".join([table[node.id], *reversed(parts)])


def _makes_an_http_call(source: str) -> bool:
    """True when the module calls an HTTP library entry point (``_HTTP_ENTRY_POINTS``), however it imported
    it; or runs ``curl`` / ``wget`` (a command-runner call in a module that carries such a command string
    anywhere, because the command may be built in a variable); or loads an HTTP library by
    ``importlib.import_module("httpx")`` / ``__import__``. A module that only inherits a client class (the
    catalog client) has none. Not seen, and stated as the limit: ``getattr(httpx, name)``, and a command
    assembled from fragments no string of which names the tool."""
    tree = ast.parse(source)
    table = _import_table(tree)
    runs_command = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        resolved = _resolve(node.func, table)
        if resolved is not None:
            module, _, attr = resolved.rpartition(".")
            if attr in _HTTP_ENTRY_POINTS.get(module, ()):
                return True
            runs_command = runs_command or resolved in _COMMAND_RUNNERS
        f = node.func
        loader = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        if loader in ("import_module", "__import__") and any(
            isinstance(c, ast.Constant) and isinstance(c.value, str) and c.value.split(".")[0] in _HTTP_LIBRARY_ROOTS
            for c in ast.walk(node)
        ):
            return True
    return runs_command and any(
        isinstance(n, ast.Constant) and isinstance(n.value, str) and _HTTP_COMMAND.match(n.value)
        for n in ast.walk(tree)
    )


def _census(src: Path = _SRC) -> tuple[set[str], set[str]]:
    """``(modules making an HTTP call, modules importing client_identity)``, as ``nexus/...`` paths."""
    calls: set[str] = set()
    importers: set[str] = set()
    for path in (src / "nexus").rglob("*.py"):
        if path.name == "client_identity.py":
            continue
        rel = path.relative_to(src).as_posix()
        source = path.read_text()
        if _makes_an_http_call(source):
            calls.add(rel)
        if _imports_client_identity(source):
            importers.add(rel)
    return calls, importers


@pytest.mark.parametrize("source", [
    "from nexus.db.client_identity import client_identity_headers",
    "from nexus.db import client_identity",
    "from nexus.db import client_identity as ci",
    "from .client_identity import client_identity_headers",
    "from ..db.client_identity import client_identity_headers",
    "from . import client_identity",
    "import nexus.db.client_identity",
    "import nexus.db.client_identity as ci",
    "import importlib\nimportlib.import_module('nexus.db.client_identity')",
    "from importlib import import_module\nimport_module('nexus.db.' + 'client_identity')",
    "__import__('nexus.db.client_identity')",
    "def f():\n    from nexus.db.client_identity import client_version\n",
])
def test_every_spelling_of_the_import_is_seen(source: str) -> None:
    assert _imports_client_identity(source), source


@pytest.mark.parametrize("source", [
    "import nexus.db.http_vector_client",
    "from nexus.db import http_scratch_store",
    "import importlib\nimportlib.import_module('nexus.db.other')",
    "# from nexus.db.client_identity import x\nX = 'client_identity'",
])
def test_an_unrelated_import_is_not_a_sender(source: str) -> None:
    assert not _imports_client_identity(source), source


@pytest.mark.parametrize("source, expected", [
    ("import httpx\nhttpx.get(u)", True),
    ("import httpx\nc = httpx.Client()", True),
    ("import urllib.request\nurllib.request.urlopen(u)", True),
    ("import urllib.request\nurllib.request.Request(u)", True),
    ("from urllib.request import urlopen\nurlopen(u)", True),
    ("import httpx\nclass S(httpx.Client):\n    pass", False),
    # every other library, and every way of spelling the import
    ("import requests\nrequests.post(u)", True),
    ("import requests\ns = requests.Session()", True),
    ("import http.client\nhttp.client.HTTPConnection(h)", True),
    ("import http.client\nhttp.client.HTTPSConnection(h)", True),
    ("from http.client import HTTPSConnection\nHTTPSConnection(h)", True),
    ("from http import client\nclient.HTTPConnection(h)", True),
    ("import http.client as hc\nhc.HTTPConnection(h)", True),
    ("import urllib3\nurllib3.PoolManager()", True),
    ("import aiohttp\naiohttp.ClientSession()", True),
    ("from httpx import Client\nClient()", True),
    ("from httpx import Client as C\nC()", True),
    ("from httpx import get\nget(u)", True),
    ("from httpx import AsyncClient\nAsyncClient()", True),
    ("import httpx as hx\nhx.post(u)", True),
    ("import httpx as _httpx\nwith _httpx.Client() as c:\n    pass", True),
    ("import urllib.request as ur\nur.urlopen(u)", True),
    ("from urllib import request\nrequest.urlopen(u)", True),
    ("from urllib.request import build_opener\nbuild_opener()", True),
    ("from urllib.request import Request\nRequest(u)", True),
    ("def f():\n    import httpx\n    httpx.get(u)", True),
    ("import importlib\nimportlib.import_module('requests')", True),
    # a command-line HTTP client
    ("import subprocess\nsubprocess.run(['curl', '-s', u])", True),
    ("import subprocess\ncmd = ['curl', '-s', u]\nsubprocess.check_output(cmd)", True),
    ("import subprocess as sp\nsp.Popen('wget -q ' + u, shell=True)", True),
    ("import os\nos.system('curl ' + u)", True),
    ("from subprocess import run\nrun(['/usr/bin/curl', u])", True),
    # not call sites
    ("import httpx\nt = httpx.Timeout(5.0)", False),
    ("import httpx\nt = httpx.MockTransport(h)", False),
    ("from httpx import Timeout\nTimeout(5)", False),
    ("def f(requests):\n    requests.post(u)", False),
    ("requests = []\nrequests.post(u)", False),
    ("import subprocess\nsubprocess.run(['git', 'status'])", False),
    ("HINT = 'curl -LsSf https://example.invalid | sh'", False),
    ("import http.client\nclass S(http.client.HTTPConnection):\n    pass", False),
    ("import importlib\nimportlib.import_module('nexus.db.other')", False),
])
def test_the_call_site_scan_sees_each_transport(source: str, expected: bool) -> None:
    assert _makes_an_http_call(source) is expected, source


def test_the_census_places_a_new_module_in_any_spelling(tmp_path: Path) -> None:
    """The census walks a tree and returns each module that makes an HTTP call, one per spelling; a module
    that imports ``client_identity`` and calls nothing is an importer and not a call site."""
    pkg = tmp_path / "nexus"
    pkg.mkdir()
    spellings = {
        "a_requests.py": "import requests\nrequests.post(u)\n",
        "b_http_client.py": "import http.client\nhttp.client.HTTPConnection(h)\n",
        "c_from_httpx.py": "from httpx import Client\nClient()\n",
        "d_alias.py": "import httpx as hx\nhx.get(u)\n",
        "e_ur.py": "import urllib.request as ur\nur.urlopen(u)\n",
        "f_curl.py": "import subprocess\nsubprocess.run(['curl', u])\n",
    }
    for name, body in spellings.items():
        (pkg / name).write_text(body)
    (pkg / "quiet.py").write_text("from nexus.db.client_identity import client_version\n")
    calls, importers = _census(tmp_path)
    assert calls == {f"nexus/{n}" for n in spellings}
    assert importers == {"nexus/quiet.py"}


def _placement_problems(calls: set[str], importers: set[str], not_senders: dict[str, str]) -> list[str]:
    """What is wrong with how the HTTP call sites are placed: each must import ``client_identity`` (and so
    send the header, checked on the wire above) or be named in *not_senders* with a reason."""
    problems: list[str] = []
    both = importers & set(not_senders)
    if both:
        problems.append(f"listed as non-senders yet import the module: {sorted(both)}")
    if [m for m in importers if m.startswith("nexus/hooks/")]:
        problems.append("a hook imports client_identity")
    if importers - calls:
        problems.append(f"import the module but make no HTTP call: {sorted(importers - calls)}")
    unplaced = calls - importers - set(not_senders)
    if unplaced:
        problems.append(f"HTTP call sites that neither send the header nor say why not: {sorted(unplaced)}")
    stale = set(not_senders) - calls
    if stale:
        problems.append(f"_NOT_SENDERS entries with no HTTP call site left: {sorted(stale)}")
    return problems


@pytest.mark.census
def test_every_http_call_site_is_a_sender_or_a_named_non_sender() -> None:
    """A new engine-facing call site fails here until it sends the header (and the documents' scope stays
    true) or is named in ``_NOT_SENDERS`` with a reason, so the claim cannot widen or narrow unseen."""
    calls, importers = _census()

    # Non-vacuity: the scan sees the known transports, and detection sees the known senders.
    assert len(calls) >= 25, f"the call-site scan found only {len(calls)} modules"
    assert {"nexus/hooks/mailbox_drain.py", "nexus/db/http_vector_client.py"} <= calls
    assert {
        "nexus/db/http_vector_client.py", "nexus/db/http_scratch_store.py",
        "nexus/db/t2/_refreshable_client.py", "nexus/db/t2/http_token_store.py",
    } <= importers

    assert _placement_problems(calls, importers, _NOT_SENDERS) == []


# -- the census, extended: every importer of client_identity has a wire driver per call site ----------------

#: Every module under ``src/nexus`` that imports ``client_identity``, with the drivers above that put one of
#: its ``client_identity_headers()`` call sites on the wire (one driver per site).
_WIRE_DRIVERS: dict[str, tuple[str, ...]] = {
    "nexus/db/http_vector_client.py": ("t3",),
    "nexus/db/t2/_refreshable_client.py": ("mixin",),
    "nexus/db/http_scratch_store.py": ("scratch",),
    "nexus/db/t2/http_token_store.py": ("token",),
    "nexus/backup/cloud_export.py": ("export_request", "export_probe", "export_delete"),
    "nexus/backup/cloud_import.py": ("import_request", "import_probe", "import_delete"),
}


def _header_call_sites(source: str) -> int:
    """How many times the module calls ``client_identity_headers`` (by name or as an attribute)."""
    count = 0
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            f = node.func
            if (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")) == "client_identity_headers":
                count += 1
    return count


def _wire_coverage_problems(src: Path = _SRC, drivers: dict[str, tuple[str, ...]] = _WIRE_DRIVERS) -> list[str]:
    """Problems in the wire coverage: an importer with no driver, a module whose driver count differs from
    its ``client_identity_headers()`` call-site count (a site added without a driver, or a driver kept for a
    site that went), a driver name that does not exist, and an entry for a module that no longer imports."""
    problems: list[str] = []
    seen: set[str] = set()
    for path in (src / "nexus").rglob("*.py"):
        if path.name == "client_identity.py":
            continue
        source = path.read_text()
        if not _imports_client_identity(source):
            continue
        rel = path.relative_to(src).as_posix()
        seen.add(rel)
        sites = _header_call_sites(source)
        if rel not in drivers:
            problems.append(f"{rel} imports client_identity and has no wire driver")
        elif len(drivers[rel]) != sites:
            problems.append(f"{rel}: {sites} client_identity_headers() call sites, {len(drivers[rel])} wire drivers")
    problems += [f"{m}: wire drivers for a module that no longer imports client_identity" for m in sorted(set(drivers) - seen)]
    problems += [f"{m}: unknown driver {d}" for m, ds in drivers.items() for d in ds if d not in _DRIVERS]
    return problems


def test_every_importer_of_client_identity_has_a_wire_driver_per_call_site() -> None:
    assert _wire_coverage_problems() == []
    # non-vacuity: the real tree has the six importers and the sites the bead names (1+1+1+1+3+3)
    assert sum(len(v) for v in _WIRE_DRIVERS.values()) == 10
    assert sorted(_WIRE_DRIVERS) == sorted(_census()[1])


def test_the_wire_census_fails_on_a_new_importer_and_on_a_new_call_site(tmp_path: Path) -> None:
    pkg = tmp_path / "nexus"
    pkg.mkdir()
    (pkg / "new_sender.py").write_text(
        "import httpx\nfrom nexus.db.client_identity import client_identity_headers\n"
        "httpx.get(u, headers=client_identity_headers())\n"
    )
    (pkg / "covered.py").write_text(
        "import httpx\nfrom nexus.db.client_identity import client_identity_headers\n"
        "httpx.get(u, headers=client_identity_headers())\nhttpx.get(u, headers=client_identity_headers())\n"
    )
    problems = _wire_coverage_problems(tmp_path, {"nexus/covered.py": ("t3",), "nexus/gone.py": ("t3", "nope")})
    assert any("new_sender.py imports client_identity and has no wire driver" in p for p in problems)
    assert any("covered.py: 2 client_identity_headers() call sites, 1 wire drivers" in p for p in problems)
    assert any("gone.py: wire drivers for a module that no longer imports" in p for p in problems)
    assert any("unknown driver nope" in p for p in problems)


def test_the_placement_check_fails_on_a_synthetic_sender_that_is_neither_importer_nor_named(tmp_path: Path) -> None:
    """A module that calls httpx, does not import ``client_identity`` and is not in ``_NOT_SENDERS`` is
    unplaced (the ``_census`` walk, run over a synthetic tree as the real one is)."""
    pkg = tmp_path / "nexus"
    pkg.mkdir()
    (pkg / "rogue.py").write_text("import httpx\nhttpx.post(u)\n")
    calls, importers = _census(tmp_path)
    assert _placement_problems(calls, importers, {}) == ["HTTP call sites that neither send the header nor say why not: ['nexus/rogue.py']"]
    assert _placement_problems(calls, importers, {"nexus/rogue.py": "named"}) == []


#: How the documents state the scope. Each must (a) name the mechanism (the vector client) and an exception
#: (hook), and (b) never quantify over requests or calls ("every ...", "each ...", "all ...", "any ..."
#: followed within the clause by "request" or "call", or a bare "on all") unless negated ("not by every",
#: "not on all"). The limit, stated: this is a keyword filter. A universal claim worded with none of those
#: words ("whatever the client sends", "without exception") passes it; the census above is what pins the
#: scope, and this only catches the wordings the documents have used. A rewording that keeps both parts and
#: adds no quantifier still passes, which is the claim the documents may make.
_DOCUMENTS = (
    "docs/wire-contract-pending.md",
    "CHANGELOG.md",
    "src/nexus/db/client_identity.py",
    "docs/rdr/rdr-223-atomic-chunk-plus-owner-write.md",
)
_QUANTIFIED = re.compile(
    r"(?<!not by )\b(?:every|each|all|any)\b[^.;]{0,60}\b(?:requests?|calls?)\b|(?<!not )\bon all\b",
    re.IGNORECASE,
)


def _passages(text: str) -> list[str]:
    flat = " ".join(text.split())
    return [
        flat[max(0, m.start() - 700): m.end() + 700]
        for m in re.finditer(re.escape(CLIENT_VERSION_HEADER), flat)
    ]


def _scope_problems(text: str) -> list[str]:
    passages = _passages(text)
    if not passages:
        return ["no mention of the header, so the pin would be vacuous"]
    problems = [
        f"quantifies over requests: ...{m.group(0)}..." for p in passages for m in [_QUANTIFIED.search(p)] if m
    ]
    if not any("hook" in p and re.search(r"vector[- ]client|HttpVectorClient", p, re.IGNORECASE) for p in passages):
        problems.append("no passage names the mechanism (the vector client) and an exception (a hook)")
    return problems


def test_the_documents_state_the_scope_by_mechanism_not_as_every_request() -> None:
    root = Path(__file__).resolve().parents[1]
    for rel in _DOCUMENTS:
        # CHANGELOG.md includes its unreleased fragments (nexus-3ceda.2), so a new entry is scanned too.
        text = changelog_text(root) if rel == "CHANGELOG.md" else (root / rel).read_text()
        assert _scope_problems(text) == [], rel


@pytest.mark.parametrize("claim", [
    "X-Nexus-Client-Version is sent on every engine request (the hook calls aside; the vector client first).",
    "X-Nexus-Client-Version rides on each request to the engine; the vector client and the hook calls differ.",
    "The vector client sends X-Nexus-Client-Version on every vector-client, T2, catalog and scratch request (not the hook calls).",
    "X-Nexus-Client-Version goes on all requests, with hook calls excepted, via the vector client.",
    "X-Nexus-Client-Version is added on any request the client makes to the engine (the vector client and the T2 stores among them; hook calls excepted).",
    "X-Nexus-Client-Version is sent on all engine traffic of the vector client; the hook calls are excepted.",
])
def test_the_scope_check_rejects_a_quantified_claim(claim: str) -> None:
    assert _scope_problems(claim), claim


@pytest.mark.parametrize("claim", [
    "X-Nexus-Client-Version is sent by the shared client classes, the vector client among them; the hook calls do not send it.",
    "X-Nexus-Client-Version: not by every engine request. The vector client sends it; a hook does not.",
    "X-Nexus-Client-Version: the vector client sends it; a hook does not, and it is not on all engine traffic.",
])
def test_the_scope_check_accepts_a_mechanism_claim(claim: str) -> None:
    assert _scope_problems(claim) == [], claim


def test_a_claim_that_drops_its_exception_is_rejected() -> None:
    assert _scope_problems("X-Nexus-Client-Version is sent by the shared client classes.")
