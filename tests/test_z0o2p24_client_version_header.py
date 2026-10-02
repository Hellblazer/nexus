# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 Phase 3 Step 2 (nexus-z0o2p.24): the vector, T2 (token store included), catalog and scratch clients name the client's version.

The engine logs ``X-Nexus-Client-Version`` on its ``ownerless_chunk_write_*`` lines, and ``absent``
when it is missing, so the log-only soak can tell which clients still write a chunk before its owner:
a request without the header comes from a client older than the release that added it. The
``User-Agent`` cannot do this (``Python-urllib/3.12``, ``python-httpx/0.28``: the transport, not the
product). A transport that dropped the header would make a current client look old, so each
transport the client uses has its own test:

* urllib: ``HttpVectorClient`` (``/v1/vectors/upsert-chunks``, ``/store-put`` and every other vector route);
* httpx: the T2 stores and the catalog client (``RefreshableHttpStoreMixin``), the token store (its own
  client) and the T1 scratch store.

The only writers of ``upsert-chunks`` and ``store-put`` in this repository are on the urllib transport.
Callers outside the repository (a conexus tool on httpx, say) send whatever they send; the engine's
log names them by the ABSENCE of this header.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

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


def test_the_token_store_sends_the_version() -> None:
    """``HttpTokenStore`` builds its own client (baked bearer), so it is not covered by the mixin."""
    from nexus.db.t2.http_token_store import HttpTokenStore

    store = HttpTokenStore(base_url="http://127.0.0.1:1", _token="bearer")
    try:
        request = store._client.build_request("GET", "/v1/tokens/list")
        assert request.headers[CLIENT_VERSION_HEADER] == client_version()
    finally:
        store._client.close()


def test_the_httpx_scratch_store_sends_the_version() -> None:
    from nexus.db.http_scratch_store import HttpScratchStore

    store = HttpScratchStore(base_url="http://127.0.0.1:1", _token="bearer")
    try:
        request = store._client.build_request("POST", "/v1/scratch/list")
        assert request.headers[CLIENT_VERSION_HEADER] == client_version()
    finally:
        store.close()


# -- the scope of the claim ---------------------------------------------------
#
# The header is sent by the shared client classes (``HttpVectorClient``, ``RefreshableHttpStoreMixin``,
# ``HttpTokenStore``, ``HttpScratchStore``), not by every engine request. Two pins keep the documents'
# claim at that scope: a census of every HTTP call site in ``src/nexus`` (a new one must be placed, so
# the claim cannot silently widen or narrow), and a check on how the documents state it.

#: Every module under ``src/nexus`` that makes an HTTP call and does NOT send the header, with why. The
#: census fails on a call site missing from here and from the importers, and on an entry here that has no
#: call site left. A hook is never a sender (``nexus.db``'s package init runs first: measured in
#: ``client_identity``'s docstring).
_NOT_SENDERS: dict[str, str] = {
    # hooks: latency on every prompt / tool call
    "nexus/hooks/_routing_lib.py": "hook",
    "nexus/hooks/mailbox_drain.py": "hook",
    "nexus/hooks/tuple_ledger_project.py": "hook",
    # probes and admin calls of the engine; none writes a chunk
    "nexus/commands/daemon.py": "engine probe: GET /health",
    "nexus/commands/doctor.py": "probes: health and the MinerU server",
    "nexus/daemon/binary_lifecycle.py": "engine probe: GET /version",
    "nexus/daemon/service_registry.py": "engine probe: loopback liveness",
    "nexus/daemon/storage_service_daemon.py": "engine probe and appliance admin calls",
    "nexus/db/data_token.py": "token mint (POST to the mint endpoint)",
    "nexus/db/http_engine_status.py": "engine probe: GET /v1/status",
    "nexus/db/managed_endpoint.py": "engine probe: GET /version",
    "nexus/health.py": "probes: PyPI, GET /v1/_whoami, health",
    "nexus/mcp/channel.py": "engine probe: GET /version",
    "nexus/migration/pg_read.py": "migration read of the engine's own tables",
    "nexus/upgrade_ladder/provisioning.py": "engine probes: GET /version, /health",
    # test seams over an in-process fake: no engine
    "nexus/db/inmemory_pipeline.py": "MockTransport over an in-process engine",
    "nexus/db/t2/http_centroid_store.py": "MockTransport test seam; real requests use the mixin's headers",
    # third parties, not the engine
    "nexus/aspect_readers.py": "third-party URLs (HTTPS stat)",
    "nexus/bib_enricher.py": "third party: bibliographic APIs",
    "nexus/bib_enricher_openalex.py": "third party: OpenAlex",
    "nexus/commands/catalog_cmds/backfill.py": "third-party URLs (ETag capture)",
    "nexus/commands/mineru.py": "MinerU server",
    "nexus/daemon/binary_install.py": "release download",
    "nexus/daemon/mineru_lifecycle.py": "MinerU server",
    "nexus/db/minilm_direct.py": "model download",
    "nexus/db/service_bge_model.py": "model download",
    "nexus/doctor_references.py": "third-party URLs (ETag)",
    "nexus/install_ping.py": "install ping, not the vector or T2 surface",
    "nexus/pdf_extractor.py": "MinerU server",
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


_HTTPX_CALLS = frozenset({"Client", "AsyncClient", "get", "post", "put", "delete", "request", "stream", "patch", "head"})
_URLLIB_CALLS = frozenset({"Request", "urlopen", "build_opener"})


def _makes_an_http_call(source: str) -> bool:
    """``httpx.<client or verb>(...)``, ``urllib.request.<Request|urlopen|build_opener>(...)`` or a bare
    ``urlopen(...)``. A module that only inherits a client class (the catalog client) has none."""
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute):
            base = f.value
            base_name = base.id if isinstance(base, ast.Name) else (base.attr if isinstance(base, ast.Attribute) else "")
            if base_name in ("httpx", "_httpx") and f.attr in _HTTPX_CALLS:
                return True
            if base_name == "request" and f.attr in _URLLIB_CALLS:
                return True
        elif isinstance(f, ast.Name) and f.id == "urlopen":
            return True
    return False


def _census() -> tuple[set[str], set[str]]:
    """``(modules making an HTTP call, modules importing client_identity)``, as ``nexus/...`` paths."""
    calls: set[str] = set()
    importers: set[str] = set()
    for path in (_SRC / "nexus").rglob("*.py"):
        if path.name == "client_identity.py":
            continue
        rel = path.relative_to(_SRC).as_posix()
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
])
def test_the_call_site_scan_sees_each_transport(source: str, expected: bool) -> None:
    assert _makes_an_http_call(source) is expected, source


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

    both = importers & set(_NOT_SENDERS)
    assert not both, f"listed as non-senders yet import the module: {sorted(both)}"
    assert not [m for m in importers if m.startswith("nexus/hooks/")], "a hook imports client_identity"
    assert not (importers - calls), f"import the module but make no HTTP call: {sorted(importers - calls)}"
    unplaced = calls - importers - set(_NOT_SENDERS)
    assert not unplaced, f"HTTP call sites that neither send the header nor say why not: {sorted(unplaced)}"
    stale = set(_NOT_SENDERS) - calls
    assert not stale, f"_NOT_SENDERS entries with no HTTP call site left: {sorted(stale)}"


#: How the documents state the scope. Each must (a) name the mechanism (the vector client) and an exception
#: (hook), and (b) never quantify over requests or calls ("every ...", "each ...", "all ..." followed within
#: the clause by "request" or "call") unless negated ("not by every"). A rewording that keeps both parts
#: and adds no quantifier still passes, which is the claim the documents may make.
_DOCUMENTS = (
    "docs/wire-contract-pending.md",
    "CHANGELOG.md",
    "src/nexus/db/client_identity.py",
    "docs/rdr/rdr-223-atomic-chunk-plus-owner-write.md",
)
_QUANTIFIED = re.compile(r"(?<!not by )\b(?:every|each|all)\b[^.;]{0,60}\b(?:requests?|calls?)\b", re.IGNORECASE)


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
        assert _scope_problems((root / rel).read_text()) == [], rel


@pytest.mark.parametrize("claim", [
    "X-Nexus-Client-Version is sent on every engine request (the hook calls aside; the vector client first).",
    "X-Nexus-Client-Version rides on each request to the engine; the vector client and the hook calls differ.",
    "The vector client sends X-Nexus-Client-Version on every vector-client, T2, catalog and scratch request (not the hook calls).",
    "X-Nexus-Client-Version goes on all requests, with hook calls excepted, via the vector client.",
])
def test_the_scope_check_rejects_a_quantified_claim(claim: str) -> None:
    assert _scope_problems(claim), claim


@pytest.mark.parametrize("claim", [
    "X-Nexus-Client-Version is sent by the shared client classes, the vector client among them; the hook calls do not send it.",
    "X-Nexus-Client-Version: not by every engine request. The vector client sends it; a hook does not.",
])
def test_the_scope_check_accepts_a_mechanism_claim(claim: str) -> None:
    assert _scope_problems(claim) == [], claim


def test_a_claim_that_drops_its_exception_is_rejected() -> None:
    assert _scope_problems("X-Nexus-Client-Version is sent by the shared client classes.")
