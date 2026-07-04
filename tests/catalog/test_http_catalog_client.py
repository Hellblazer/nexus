# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-152 bead nexus-gmiaf.18 (P2.7): HttpCatalogClient + factory seam tests.

Tests:
1. _resolve_config raises cleanly when NX_SERVICE_PORT/TOKEN are absent
2. Constructor produces correct base_url + headers from override args
3. Each major category of HTTP verbs (GET/POST) routes correctly
4. Factory seam: make_catalog_reader returns HttpCatalogClient when env set
5. Factory seam: make_catalog_writer returns _ServiceCatalogWriter when env set
6. _ServiceCatalogWriter enforces CATALOG_WRITE_OPS whitelist
7. Guarded methods raise NotImplementedError (rebuild, defrag, compact, sync, pull)
8. Fake server round-trip exercising the REAL routes from CatalogHandler

Route alignment verified against CatalogHandler.java switch cases (bead nexus-gmiaf.18).
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.db.service_endpoint import resolve_service_config as _resolve_config

# Wave review (the h8rf6.3 -> 49523e16 lesson): fixture chashes must be
# REPRESENTATIVE -- 32-char catalog prefixes of real 64-char sha256 digests.
# The original short literals ("abc123") made [:32] truncation a structural
# no-op, so a wrong-length wire payload was invisible to every test here.
# CHUNK_SHA_* are the full 64-char forms for fixtures standing in for T3
# chunk_text_hash metadata; CHASH_* are their catalog-side 32-char prefixes.
CHUNK_SHA_A = "2ccea837b4713a233eea0914ad7adda8bcbbbeccd9ac45e217cab14843229eb2"  # sha256("fake-chunk-A")
CHUNK_SHA_B = "6756d390c50dd95257ad481c8ab3669f93838ed7e8f3cf334a8bbf1281d8e3b2"  # sha256("fake-chunk-B")
CHASH_A = CHUNK_SHA_A[:32]
CHASH_B = CHUNK_SHA_B[:32]
from nexus.daemon.catalog_write_shim import CATALOG_WRITE_OPS


# ── helpers ───────────────────────────────────────────────────────────────────

def _fake_tumbler() -> str:
    return "1.1.1"


def _entry_dict(**kwargs: Any) -> dict:
    """Minimal server response dict that _to_entry accepts.

    nexus-8y1tm: ``file_path``/``source_uri`` defaults are the literal
    values ``tests/catalog/test_shape_parity_tripwire.py`` seeds onto its
    local ``doc_a`` fixture — this lets client-side exact-match filters
    (``by_file_path``, ``by_source_uri``, ``find_by_file_path``,
    ``resolve_path``) find a match against the fake server too, without a
    stateful fake.

    nexus-u26b4: ``metadata``/``source_mtime``/``bib_*`` added so the
    ``/list``-backed ``descendants()`` parity entry (which does NOT go
    through ``_to_entry()`` — it forwards the raw wire dict) sees the same
    Java-normalized document-row shape (metadata as a parsed nested dict,
    the full ``bib_*`` field set) as local ``Catalog.descendants()``'s now
    -normalized rows. Harmless to every other ``_entry_dict()`` consumer:
    they all go through ``_to_entry()``, which collapses to a
    ``CatalogEntry`` dataclass regardless of which wire keys were present.
    """
    base = {
        "tumbler": _fake_tumbler(),
        "title": "Test Doc",
        "content_type": "paper",
        "chunk_count": 0,
        "file_path": "src/alpha.py",
        "source_uri": "file:///tmp/nexus-test/alpha.py",
        "metadata": {"key": "value"},
        "source_mtime": 0.0,
        "bib_year": 0,
        "bib_authors": "",
        "bib_venue": "",
        "bib_citation_count": 0,
    }
    base.update(kwargs)
    return base


class FakeCatalogHandler(BaseHTTPRequestHandler):
    """Routes matching the real CatalogHandler.java switch cases exactly."""

    #: nexus-gaou3: last body POSTed to /collections/rename (for cross_model assertions).
    last_rename_body: dict[str, Any] = {}
    #: nexus-gaou3: when True, /collections/rename 409s a plain (cross_model-absent)
    #: rename, mirroring the server's collision guard so the client's error
    #: propagation can be asserted.
    rename_conflicts: bool = False

    #: RDR-168 P3 wire-semantics regression coverage.
    get_ops: list[str] = []          # ops seen by do_GET, in order
    post_ops: list[str] = []         # ops seen by do_POST, in order
    last_link_body: dict[str, Any] = {}
    #: from_tumbler value for which /link_query reports NO existing link (absent path).
    link_absent_from: str = "9.9.9"
    #: when set, /list returns this many docs for a content_type-filtered request
    #: (CatalogHandler returns ALL matching rows ignoring limit/offset — used to prove
    #: the client issues a single request and does not loop).
    list_content_type_count: int = 0
    #: /link response shape: None omits the key (old-JAR skew), bool sets created (njrcn.3).
    link_created: "bool | None" = True

    @classmethod
    def reset_log(cls) -> None:
        cls.get_ops = []
        cls.post_ops = []
        cls.last_link_body = {}
        cls.list_content_type_count = 0

    def log_message(self, *args: Any) -> None:
        pass  # suppress test noise

    def _send_json(self, body: Any, code: int = 200) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length:
            return json.loads(self.rfile.read(length))
        return {}

    def _query_params(self) -> dict[str, str]:
        qs = urlparse(self.path).query
        return {k: v[0] for k, v in parse_qs(qs).items()} if qs else {}

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        op = path.removeprefix("/v1/catalog")
        FakeCatalogHandler.get_ops.append(op)

        if op == "/stats":
            # nexus-8y1tm: full CatalogRepository.stats() shape (7 keys) —
            # doc_count/link_count/owner_count were the only 3 pre-existing
            # keys here; collection_count/chunk_count/links_by_type/
            # by_content_type added so HttpCatalogClient.stats() (a pure
            # passthrough of this response) shape-matches Catalog.stats().
            self._send_json({
                "doc_count": 7, "link_count": 3, "owner_count": 2,
                "collection_count": 2, "chunk_count": 5,
                "links_by_type": {"cites": 1, "relates": 1},
                "by_content_type": {"code": 5, "prose": 2},
            })
        elif op == "/show":
            self._send_json(_entry_dict())
        elif op == "/list":
            params = self._query_params()
            if params.get("content_type") and FakeCatalogHandler.list_content_type_count:
                # Mirror CatalogHandler: the content_type branch ignores limit/offset and
                # returns ALL matching rows in one response.
                n = FakeCatalogHandler.list_content_type_count
                self._send_json({"documents": [_entry_dict() for _ in range(n)], "count": n})
            else:
                # nexus-u26b4: second doc has EMPTY metadata (vs the first's
                # populated dict) — descendants() parity needs this
                # heterogeneity to mirror local Catalog.descendants()'s
                # seeded mix (doc_a has a real ``meta=``, every other seeded
                # descendant does not); _to_entry()-based consumers
                # (by_content_type, all_documents, ...) are unaffected since
                # they collapse to a CatalogEntry dataclass regardless.
                self._send_json({
                    "documents": [
                        _entry_dict(),
                        _entry_dict(title="Second", metadata={}),
                    ],
                    "count": 2,
                })
        elif op == "/search":
            self._send_json({"documents": [_entry_dict()], "count": 1})
        elif op == "/resolve":
            self._send_json({"documents": [_entry_dict()]})
        elif op == "/links":
            params = self._query_params()
            direction = params.get("direction", "both")
            # njrcn.5: mirror the server-side type filter (single link_type or link_types IN).
            requested = None
            if params.get("link_types"):
                requested = {t for t in params["link_types"].split(",") if t}
            elif params.get("link_type"):
                requested = {params["link_type"]}
            out_row = {"from_tumbler": "1.1.1", "to_tumbler": "1.1.2", "link_type": "cites"}
            # nexus-u26b4: the in-direction row was previously hardcoded empty
            # (see the links_to EXCLUSIONS/REGISTRY history in
            # test_shape_parity_tripwire.py) — a real inbound-link row so
            # direction=in|both are wire-faithful like direction=out already was.
            in_row = {"from_tumbler": "1.1.3", "to_tumbler": "1.1.2", "link_type": "cites"}
            match = requested is None or "cites" in requested
            if direction == "out":
                self._send_json({"links_from": [out_row] if match else [], "links_to": []})
            elif direction == "in":
                self._send_json({"links_from": [], "links_to": [in_row] if match else []})
            else:
                self._send_json({
                    "links_from": [out_row] if match else [],
                    "links_to": [in_row] if match else [],
                })
        elif op == "/link_query":
            params = self._query_params()
            if params.get("from_tumbler") == FakeCatalogHandler.link_absent_from:
                self._send_json({"links": [], "count": 0})
            else:
                self._send_json({"links": [{"from_tumbler": "1.1.1", "to_tumbler": "1.1.2", "link_type": "cites"}], "count": 1})
        elif op == "/manifest/get":
            self._send_json({"rows": [{"position": 0, "chash": CHASH_A}], "count": 1})
        elif op == "/manifest/chashes":
            self._send_json({"chashes": [CHASH_A, CHASH_B]})
        elif op == "/manifest/orphans":
            params = self._query_params()
            dim = int(params.get("dim", "0"))
            self._send_json({
                "dim": dim,
                "count": 2,
                "orphans": [
                    {"doc_id": "1.1.1", "position": 0, "chash": CHASH_A,
                     "collection": "knowledge__o__minilm-l6-v2-384__v1"},
                    {"doc_id": "1.1.1", "position": 1, "chash": CHASH_B,
                     "collection": "knowledge__o__minilm-l6-v2-384__v1"},
                ],
            })
        elif op == "/collections/list":
            # nexus-8y1tm: full CatalogRepository.collRow() shape (10 keys) —
            # owner_id added so collections_by_owner's client-side filter
            # (c.get("owner_id") == owner_id) has something to match ("1.1" is
            # the tumbler_prefix every fixture owner in this file uses).
            # legacy_grandfathered is an int (0/1) on the wire (collRow's
            # ``legcy`` param is a boxed Integer column, not a boolean) —
            # deliberately NOT coerced to a Python bool here (see
            # nexus-8y1tm KNOWN DRIFT note on get_collection/list_collections).
            self._send_json({"collections": [{
                "name": "code__test__voyage-code-3__v1", "content_type": "code",
                "owner_id": "1.1", "embedding_model": "voyage-code-3",
                "model_version": "1", "display_name": "code__test__voyage-code-3__v1",
                "legacy_grandfathered": 0, "superseded_by": "", "superseded_at": "",
                "created_at": "2026-07-01T00:00:00+00:00",
            }]})
        elif op == "/collections/get":
            # nexus-8y1tm: echo the requested name; full collRow shape.
            params = self._query_params()
            name = params.get("name")
            if not name:
                self._send_json({"error": "name required"}, 400)
            else:
                self._send_json({
                    "name": name,
                    "owner_id": "1.1",
                    "content_type": "code",
                    "embedding_model": "voyage-code-3",
                    "model_version": "1",
                    "display_name": name,
                    # legacy_grandfathered: int (0/1) on the wire, matching
                    # CatalogRepository.collRow's boxed-Integer column — see
                    # the KNOWN DRIFT note where this is registered/excluded.
                    "legacy_grandfathered": 0 if "__" in name else 1,
                    "superseded_by": "", "superseded_at": "",
                    "created_at": "2026-07-01T00:00:00+00:00",
                })
        elif op == "/collections/for_tuple":
            self._send_json({"name": "code__test__voyage-code-3__v1"})
        elif op == "/collections/owner-root":
            params = self._query_params()
            name = params.get("name")
            if not name:
                self._send_json({"error": "name query param required"}, 400)
            else:
                self._send_json({"owner_id": "1.1", "repo_root": "/tmp/nexus-test"})
        elif op == "/collections/health":
            params = self._query_params()
            coll = params.get("collection")
            if not coll:
                self._send_json({"error": "collection query param required"}, 400)
            else:
                self._send_json({
                    "last_indexed": "2026-07-01T00:00:00+00:00",
                    "orphan_count": 1,
                    "stale_source_ratio": 0.0,
                })
        elif op == "/coverage":
            self._send_json({"coverage": [{"content_type": "code", "total": 1, "linked": 1}]})
        elif op == "/docs/distinct-collections":
            self._send_json({"collections": ["code__test__voyage-code-3__v1"]})
        elif op == "/docs/collection-counts":
            self._send_json({"counts": {"code__test__voyage-code-3__v1": 2}})
        elif op == "/docs/orphaned":
            # nexus-8y1tm: CatalogRepository.orphanedDocs() narrow 4-key shape
            # (tumbler/title/content_type/file_path, all str) — NOT the full
            # doc-row shape _entry_dict() produces.
            self._send_json({"documents": [{
                "tumbler": "1.1.9", "title": "Orphan",
                "content_type": "code", "file_path": "src/orphan.py",
            }]})
        elif op == "/docs/absolute-paths":
            self._send_json({"documents": [{
                "tumbler": "1.1.8",
                "file_path": "/abs/path/doc.txt",
                "physical_collection": "code__test__voyage-code-3__v1",
            }]})
        elif op == "/owners/all-with-roots":
            self._send_json({"owners": [{
                "tumbler_prefix": "1.1", "name": "myrepo", "owner_type": "repo",
                "repo_hash": "fakehash", "description": "", "repo_root": "/tmp/nexus-test",
                "head_hash": "",
            }]})
        elif op == "/owners/list":
            self._send_json({"owners": [{"tumbler_prefix": "1.1", "name": "myrepo"}]})
        elif op == "/owners/by_repo":
            self._send_json({"tumbler_prefix": "1.1", "name": "myrepo"})
        elif op == "/owners/by_name":
            # nexus-8y1tm: owner_type "curator" so curator_owner_tumbler_by_name's
            # client-side filter (o.get("owner_type") == "curator") has a match.
            self._send_json({"owners": [
                {"tumbler_prefix": "1.1", "name": "myrepo", "owner_type": "curator"},
            ]})
        elif op == "/owners/show":
            params = self._query_params()
            prefix = params.get("tumbler_prefix")
            if not prefix:
                self._send_json({"error": "tumbler_prefix required"}, 400)
            else:
                self._send_json({
                    "tumbler_prefix": prefix, "name": "myrepo", "owner_type": "repo",
                    "repo_hash": "fakehash", "description": "", "repo_root": "/tmp/nexus-test",
                    "head_hash": "",
                })
        elif op == "/resolve_span":
            params = self._query_params()
            chash = params.get("span_chash", "")
            coll  = params.get("collection", "")
            if chash == "deadbeef" * 4 and coll == "knowledge__o__bge-768__v1":
                self._send_json({
                    "chunk_text": "hello span world",
                    "metadata":   {"lang": "en"},
                    "chunk_hash": chash,
                })
            elif chash == "feeded00" * 4:  # _MISSING_32
                self.send_response(404)
                self.end_headers()
            else:
                self._send_json({
                    "chunk_text": "generic chunk text",
                    "metadata":   {},
                    "chunk_hash": chash,
                })
        elif op == "/resolve_chash":
            params = self._query_params()
            chash = params.get("chash", "")
            if chash == "00000000" * 4:
                self.send_response(404)
                self.end_headers()
            else:
                self._send_json({
                    "chash":               chash,
                    "chunk_hash":          chash,
                    "physical_collection": "knowledge__o__bge-768__v1",
                    "doc_id":              "1.2.3",
                    "chunk_text":          "resolved chunk body",
                    "metadata":            {"source": "test"},
                })
        else:
            self._send_json({"error": f"unknown GET op: {op}"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        op = path.removeprefix("/v1/catalog")
        body = self._read_body()
        FakeCatalogHandler.post_ops.append(op)

        if op == "/doc/register":
            self._send_json({"tumbler": _fake_tumbler()})
        elif op == "/register":
            self._send_json({"ok": True})
        elif op == "/update":
            self._send_json({"updated": 1})
        elif op == "/delete":
            self._send_json({"deleted": 1})
        elif op == "/link":
            FakeCatalogHandler.last_link_body = body
            resp: dict = {"ok": True}
            if FakeCatalogHandler.link_created is not None:
                resp["created"] = FakeCatalogHandler.link_created
            self._send_json(resp)
        elif op == "/unlink":
            self._send_json({"deleted": 1})
        elif op == "/traverse":
            self._send_json({"nodes": [_entry_dict()], "edges": [{"from_tumbler": "1.1.1", "to_tumbler": "1.1.2", "link_type": "cites"}]})
        elif op == "/manifest/write":
            self._send_json({"ok": True, "count": len(body.get("rows", []))})
        elif op == "/manifest/append":
            self._send_json({"ok": True, "count": len(body.get("rows", []))})
        elif op == "/manifest/purge":
            self._send_json({"deleted": 1})
        elif op == "/manifest/get_many":
            self._send_json({"manifests": {
                "1.1.1": [{"position": 0, "chash": CHASH_A, "line_start": 1, "line_end": 9}],
            }})
        elif op == "/manifest/docs_for_chashes":
            # Real server: {"tumblers": [tumbler_string, ...]} (flat list, SELECT DISTINCT)
            self._send_json({"tumblers": ["1.1.1"]})
        elif op == "/manifest/backfill":
            self._send_json({"stamped": 7})
        elif op == "/owners/upsert":
            self._send_json({"ok": True})
        elif op == "/owners/head_hash":
            self._send_json({"updated": 1})
        elif op == "/collections/upsert":
            self._send_json({"ok": True})
        elif op == "/collections/supersede":
            self._send_json({"updated": 5})
        elif op == "/collections/rename":
            # RDR-164 P3: consolidated endpoint returns per-table re-home counts.
            # nexus-gaou3: stash the body so tests can assert cross_model threading.
            FakeCatalogHandler.last_rename_body = body
            if FakeCatalogHandler.rename_conflicts and body.get("cross_model") is not True:
                self._send_json({"error": "target collection already exists"}, code=409)
            else:
                self._send_json({"renamed": {"catalog_documents": 3,
                                             "catalog_collections_inserted": 1,
                                             "catalog_collections_deleted": 1}})
        elif op == "/import/owner":
            self._send_json({"imported": 1})
        elif op == "/import/document":
            self._send_json({"imported": 1})
        elif op == "/import/link":
            self._send_json({"imported": 1})
        elif op == "/verify/relation-counts":
            # echo a count for each requested whitelisted relation
            rels = body.get("relations", [])
            self._send_json({"counts": {r: 42 for r in rels}})
        elif op == "/docs/chunk-counts":
            ids = body.get("doc_ids", [])
            self._send_json({i: 3 for i in ids} if ids else {})
        elif op == "/links/from-batch":
            tumblers = body.get("tumblers", [])
            self._send_json(
                {t: [{"from_tumbler": t, "link_type": "cites"}] for t in tumblers}
                if tumblers else {}
            )
        elif op == "/resolve_many":
            ids = body.get("doc_ids", [])
            if not ids:
                self._send_json({"entries": {}})
            else:
                self._send_json({"entries": {i: _entry_dict(tumbler=i) for i in ids}})
        elif op == "/owners/by_type":
            owner_type = body.get("owner_type")
            if not owner_type:
                self._send_json({"error": "owner_type required"}, 400)
            else:
                self._send_json({"owners": [
                    {"tumbler_prefix": "1.1", "name": "myrepo", "owner_type": owner_type},
                ]})
        else:
            self._send_json({"ok": True})


def start_fake_server() -> tuple[HTTPServer, str]:
    """Start a local fake catalog HTTP server; return (server, base_url)."""
    server = HTTPServer(("127.0.0.1", 0), FakeCatalogHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.05)  # brief wait for the thread to reach serve_forever
    return server, f"http://127.0.0.1:{port}"


# ── _resolve_config tests ─────────────────────────────────────────────────────

class TestResolveConfig:
    def test_missing_port_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NX_SERVICE_PORT", raising=False)
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        with pytest.raises(RuntimeError, match="NX_SERVICE_PORT"):
            _resolve_config()

    def test_non_integer_port_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NX_SERVICE_PORT", "not_a_port")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        with pytest.raises(RuntimeError, match="NX_SERVICE_PORT must be an integer"):
            _resolve_config()

    def test_missing_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NX_SERVICE_PORT", "9090")
        monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="NX_SERVICE_TOKEN"):
            _resolve_config()

    def test_valid_config_returns_tuple(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NX_SERVICE_HOST", "10.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "9090")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok123")
        host, port, token = _resolve_config()
        assert host == "10.0.0.1"
        assert port == 9090
        assert token == "tok123"

    def test_default_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NX_SERVICE_HOST", raising=False)
        monkeypatch.setenv("NX_SERVICE_PORT", "9090")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        host, _, _ = _resolve_config()
        assert host == "127.0.0.1"


# ── HttpCatalogClient round-trip tests ───────────────────────────────────────

@pytest.fixture(scope="module")
def fake_server():
    server, url = start_fake_server()
    yield url
    server.shutdown()


@pytest.fixture
def client(fake_server: str):
    with HttpCatalogClient(
        base_url=fake_server,
        tenant="tenant_abc",
        _token="test_tok",
    ) as c:
        yield c


class TestHttpCatalogClientRoundTrip:
    def test_is_initialized(self, client: HttpCatalogClient) -> None:
        assert client.is_initialized() is True

    def test_stats(self, client: HttpCatalogClient) -> None:
        s = client.stats()
        assert s["doc_count"] == 7

    def test_doc_count(self, client: HttpCatalogClient) -> None:
        assert client.doc_count() == 7

    def test_register_returns_tumbler(self, client: HttpCatalogClient) -> None:
        from nexus.catalog.catalog import Tumbler
        # Positional owner+title as in Catalog.register signature
        t = client.register("1.1", "My Paper", content_type="paper")
        assert isinstance(t, Tumbler)
        assert str(t) == "1.1.1"

    def test_register_no_bib_fields(self, client: HttpCatalogClient) -> None:
        """register() must NOT accept bib_year/bib_authors — CatalogEntry has none."""
        import inspect
        from nexus.catalog.http_catalog_client import HttpCatalogClient as HCC
        sig = inspect.signature(HCC.register)
        assert "bib_year" not in sig.parameters
        assert "bib_authors" not in sig.parameters

    def test_resolve_returns_entry(self, client: HttpCatalogClient) -> None:
        entry = client.resolve("1.1.1")
        assert entry is not None
        assert entry.title == "Test Doc"

    def test_resolve_404_returns_none(self, monkeypatch: pytest.MonkeyPatch, fake_server: str) -> None:
        """resolve() must return None (not raise) for 404."""
        import httpx
        with HttpCatalogClient(base_url=fake_server, _token="test_tok") as c:
            # Patch _get to simulate a 404
            def _fake_get(path, **params):
                resp = httpx.Response(404, json={"error": "not found"})
                raise httpx.HTTPStatusError("not found", request=None, response=resp)
            c._get = _fake_get
            result = c.resolve("9.9.9")
            assert result is None

    def test_find_returns_list(self, client: HttpCatalogClient) -> None:
        results = client.find("test query")
        assert len(results) >= 1
        assert results[0].title == "Test Doc"

    def test_all_documents(self, client: HttpCatalogClient) -> None:
        docs = client.all_documents()
        assert len(docs) == 2
        assert docs[1].title == "Second"

    def test_link_returns_bool(self, client: HttpCatalogClient) -> None:
        # Canonical Catalog.link() takes positional created_by and returns bool.
        # Migrated from old client-specific sig (created_by was kw-only with default).
        result = client.link("1.1.1", "1.1.2", "cites", "test-suite")
        assert isinstance(result, bool)

    def test_link_returns_true_when_created(self, client: HttpCatalogClient) -> None:
        FakeCatalogHandler.link_created = True
        try:
            assert client.link("1.1.1", "1.1.2", "cites", "test-suite") is True
        finally:
            FakeCatalogHandler.link_created = True

    def test_link_returns_false_when_merged(self, client: HttpCatalogClient) -> None:
        # njrcn.3: created=False (ON CONFLICT merged an existing link) → link() returns
        # False, mirroring canonical (True=new, False=merged). This is the branch that
        # changed meaning (was result['ok'], now result['created']).
        FakeCatalogHandler.link_created = False
        try:
            assert client.link("1.1.1", "1.1.2", "cites", "test-suite") is False
        finally:
            FakeCatalogHandler.link_created = True

    def test_link_returns_false_on_response_without_created(
        self, client: HttpCatalogClient
    ) -> None:
        # Version-skew lock: a service that omits 'created' (old JAR) → bool(None) → False.
        FakeCatalogHandler.link_created = None  # omit the key
        try:
            assert client.link("1.1.1", "1.1.2", "cites", "test-suite") is False
        finally:
            FakeCatalogHandler.link_created = True

    # ── RDR-168 P3 wire-semantics regressions (substantive-critic Criticals) ──────

    def test_all_documents_content_type_does_not_loop(
        self, client: HttpCatalogClient
    ) -> None:
        """all_documents(content_type=X, limit=0) issues ONE /list, never loops.

        The service's content_type branch ignores limit/offset and returns every row.
        A pagination loop would re-fetch the full (>=page) set forever. Regression guard
        for the infinite-loop Critical: assert a single /list request and all rows back.
        """
        FakeCatalogHandler.reset_log()
        FakeCatalogHandler.list_content_type_count = 1500  # >= the 1000 page size
        docs = client.all_documents(content_type="code")  # limit defaults to 0 (unbounded)
        assert len(docs) == 1500
        assert FakeCatalogHandler.get_ops.count("/list") == 1

    def test_link_if_absent_skips_when_link_present(
        self, client: HttpCatalogClient
    ) -> None:
        """Existing link → skip (return False), NO /link write (no overwrite).

        Canonical link_if_absent is INSERT-OR-SKIP; the service POST /link is an UPSERT
        that would overwrite created_by/spans/meta. The pre-flight must short-circuit.
        """
        FakeCatalogHandler.reset_log()
        result = client.link_if_absent("1.1.1", "1.1.2", "cites", "indexer")
        assert result is False
        assert "/link" not in FakeCatalogHandler.post_ops

    def test_link_if_absent_writes_when_absent_and_serializes_params(
        self, client: HttpCatalogClient
    ) -> None:
        """Absent link → write, with every caller param serialized onto the payload."""
        FakeCatalogHandler.reset_log()
        result = client.link_if_absent(
            FakeCatalogHandler.link_absent_from, "1.1.2", "cites", "indexer",
            from_span="chash:aa", to_span="chash:bb", allow_dangling=True,
        )
        assert result is True
        assert "/link" in FakeCatalogHandler.post_ops
        body = FakeCatalogHandler.last_link_body
        assert body["created_by"] == "indexer"
        assert body["from_span"] == "chash:aa"
        assert body["to_span"] == "chash:bb"
        assert body["allow_dangling"] is True

    def test_bulk_unlink_dry_run_returns_real_count_without_deleting(
        self, client: HttpCatalogClient
    ) -> None:
        """dry_run=True returns the would-delete count via link_query, no /unlink POST."""
        FakeCatalogHandler.reset_log()
        n = client.bulk_unlink(link_type="cites", dry_run=True)
        assert n == 1  # the fake /link_query reports one matching link
        assert "/unlink" not in FakeCatalogHandler.post_ops

    def test_bulk_unlink_requires_a_filter(self, client: HttpCatalogClient) -> None:
        """Canonical parity: no filter and not dry_run → ValueError (guard against mass delete)."""
        with pytest.raises(ValueError, match="at least one filter"):
            client.bulk_unlink()

    def test_links_from_uses_direction_out(self, client: HttpCatalogClient) -> None:
        # GET /links?tumbler=X&direction=out
        links = client.links_from("1.1.1")
        assert len(links) == 1
        # Return-type parity: typed CatalogLink (attribute access), like local Catalog.
        assert links[0].link_type == "cites"
        assert str(links[0].to_tumbler) == "1.1.2"

    def test_links_from_forwards_link_types_server_side(self, client: HttpCatalogClient) -> None:
        # njrcn.5: link_types is forwarded to the server-side IN filter (the fake mirrors
        # it), so a matching set returns the link and a non-matching set returns nothing —
        # no client-side over-fetch-then-filter.
        assert len(client.links_from("1.1.1", link_types=["cites", "relates"])) == 1
        assert client.links_from("1.1.1", link_types=["implements"]) == []

    def test_links_to_uses_direction_in(self, client: HttpCatalogClient) -> None:
        # GET /links?tumbler=X&direction=in
        links = client.links_to("1.1.2")
        assert len(links) == 1
        # Return-type parity: typed CatalogLink (attribute access), like local Catalog.
        assert links[0].link_type == "cites"
        assert str(links[0].from_tumbler) == "1.1.3"

    def test_link_query(self, client: HttpCatalogClient) -> None:
        links = client.link_query(link_type="cites")
        assert len(links) == 1
        assert links[0].link_type == "cites"  # typed CatalogLink, not dict

    def test_get_manifests_returns_typed_rows(self, client: HttpCatalogClient) -> None:
        # Return-type parity: batch get_manifests yields list[ManifestRow] per doc_id
        # (search_engine.py prefers this over the per-doc loop in service mode).
        by_doc = client.get_manifests(["1.1.1"])
        assert "1.1.1" in by_doc
        assert by_doc["1.1.1"][0].chash == CHASH_A
        assert by_doc["1.1.1"][0].position == 0

    def test_graph_post_traverse(self, client: HttpCatalogClient) -> None:
        # graph() must POST /traverse (not GET)
        result = client.graph("1.1.1")
        assert isinstance(result, dict)
        assert "nodes" in result
        assert "edges" in result

    def test_graph_many_post_traverse(self, client: HttpCatalogClient) -> None:
        result = client.graph_many(["1.1.1", "1.1.2"])
        assert isinstance(result, dict)
        assert "nodes" in result

    def test_delete_document_uses_post(self, client: HttpCatalogClient) -> None:
        # POST /delete with body {tumbler: ...} → {"deleted": 1}
        result = client.delete_document("1.1.1")
        assert result is True

    def test_write_manifest_uses_rows_key(self, client: HttpCatalogClient) -> None:
        # Must send 'rows' key not 'chunks'
        client.write_manifest("1.1.1", [{"position": 0, "chash": CHASH_A}])

    def test_get_manifest_returns_rows(self, client: HttpCatalogClient) -> None:
        # GET /manifest/get?doc_id=X → response key 'rows'
        rows = client.get_manifest("1.1.1")
        assert len(rows) == 1
        # Return-type parity: typed ManifestRow (attribute access), like local Catalog.
        assert rows[0].chash == CHASH_A
        assert rows[0].position == 0

    def test_get_chunk_chashes_from_manifest(self, client: HttpCatalogClient) -> None:
        # Pulls chashes from manifest rows (not a separate endpoint)
        chashes = client.get_chunk_chashes("1.1.1")
        assert CHASH_A in chashes

    def test_chashes_for_collection(self, client: HttpCatalogClient) -> None:
        chashes = client.chashes_for_collection("code__test__v1")
        assert CHASH_A in chashes

    def test_relation_counts_unwraps_counts_and_casts_int(
        self, client: HttpCatalogClient,
    ) -> None:
        # RDR-159 P-1a: POST /verify/relation-counts → {"counts": {rel: n}};
        # client unwraps the "counts" key and casts to int.
        counts = client.relation_counts(["nexus.memory", "nexus.plans"])
        assert counts == {"nexus.memory": 42, "nexus.plans": 42}

    def test_relation_counts_empty_short_circuits(
        self, client: HttpCatalogClient,
    ) -> None:
        assert client.relation_counts([]) == {}

    def test_manifest_backfill_returns_stamped_count(
        self, client: HttpCatalogClient,
    ) -> None:
        # RDR-159 P-1b: POST /manifest/backfill → {"stamped": n}
        assert client.manifest_backfill() == 7

    def test_manifest_orphans_returns_count_and_sample(
        self, client: HttpCatalogClient,
    ) -> None:
        # RDR-159 P-1b: GET /manifest/orphans?dim= → {dim, count, orphans}
        result = client.manifest_orphans(384, limit=100)
        assert result["dim"] == 384
        assert result["count"] == 2
        assert len(result["orphans"]) == 2
        assert result["orphans"][0]["doc_id"] == "1.1.1"

    def test_manifest_orphans_rejects_unsupported_dim(
        self, client: HttpCatalogClient,
    ) -> None:
        import pytest as _pytest
        with _pytest.raises(ValueError, match="dim must be one of"):
            client.manifest_orphans(512)

    def test_manifest_orphans_rejects_nonpositive_limit(
        self, client: HttpCatalogClient,
    ) -> None:
        import pytest as _pytest
        with _pytest.raises(ValueError, match="limit must be > 0"):
            client.manifest_orphans(384, limit=0)

    def test_docs_for_chashes_returns_dict_shape(self, client: HttpCatalogClient) -> None:
        # nexus-h8rf6.3: the wire response is {"tumblers": [tumbler_string, ...]}
        # — a flat list from SELECT DISTINCT doc_id WHERE chash IN (...), NOT a
        # per-chash map. The client reconstructs the dict shape (matching local
        # Catalog.docs_for_chashes) via a second get_manifests() round-trip that
        # intersects each candidate doc's manifest chashes against the request.
        # Pre-fix this returned the flat list directly, which crashed every
        # ``by_chash.items()`` consumer (build_staleness_cache et al.) with
        # AttributeError, silently degrading every service-mode index run to a
        # full re-chunk + re-embed.
        result = client.docs_for_chashes([CHASH_A])
        assert isinstance(result, dict)
        assert result == {CHASH_A: ["1.1.1"]}

    def test_docs_for_chashes_empty_input_returns_empty_dict(
        self, client: HttpCatalogClient,
    ) -> None:
        assert client.docs_for_chashes([]) == {}

    def test_unlink_returns_int_count(self, client: HttpCatalogClient) -> None:
        # nexus-h8rf6.3: pre-fix this returned a bool (deleted > 0), so
        # commands/catalog_cmds/links.py's "Removed {removed} link(s)" echoed
        # "Removed True link(s)" and mcp/catalog.py's {"removed": removed}
        # returned a bool instead of a count.
        removed = client.unlink("1.1.1", "1.1.2", "cites")
        assert removed == 1
        assert type(removed) is int

    def test_set_owner_head_hash_returns_int_count(self, client: HttpCatalogClient) -> None:
        # nexus-h8rf6.3: pre-fix this returned None, so indexer.py's
        # ``if rowcount == 0: _log.warning(...)`` (lost-write detector) could
        # never fire in service mode.
        updated = client.set_owner_head_hash("1.1", "deadbeef")
        assert updated == 1
        assert type(updated) is int

    def test_lookup_doc_id_by_collection_and_path_miss_returns_empty_string(
        self, client: HttpCatalogClient,
    ) -> None:
        # nexus-h8rf6.3: local Catalog's documented contract is "" (never
        # None) on no-match; align the service client to match.
        def _fake_get(path: str, **params: object) -> dict:
            return {"documents": []}
        client._get = _fake_get  # type: ignore[method-assign]
        result = client.lookup_doc_id_by_collection_and_path("code__x", "missing.py")
        assert result == ""

    def test_list_collections(self, client: HttpCatalogClient) -> None:
        colls = client.list_collections()
        assert len(colls) == 1

    def test_supersede_collection(self, client: HttpCatalogClient) -> None:
        # Canonical Catalog.supersede_collection() takes positional old_name, new_name.
        # Migrated from old client-specific sig (new_name was keyword-only superseded_by).
        # Returns None (canonical), not int.
        result = client.supersede_collection("old__coll", "new__coll")
        assert result is None

    def test_rename_collection(self, client: HttpCatalogClient) -> None:
        # Sends {old_name, new_name} (canonical form)
        FakeCatalogHandler.last_rename_body = {}
        n = client.rename_collection("old__coll", "new__coll")
        assert n == 3
        # nexus-gaou3: default rename omits cross_model (server 409s an existing target).
        assert "cross_model" not in FakeCatalogHandler.last_rename_body

    def test_rename_collection_cross_model_sets_flag(self, client: HttpCatalogClient) -> None:
        # nexus-gaou3: the deliberate cross-model repoint sends cross_model:true so the
        # server takes the RDR-162 COPY branch instead of 409ing the existing target.
        FakeCatalogHandler.last_rename_body = {}
        client.rename_collection("old__coll", "new__coll", cross_model=True)
        assert FakeCatalogHandler.last_rename_body.get("cross_model") is True

    def test_rename_collection_cascade_cross_model_in_body(self, client: HttpCatalogClient) -> None:
        FakeCatalogHandler.last_rename_body = {}
        client.rename_collection_cascade("old__coll", "new__coll", cross_model=True)
        assert FakeCatalogHandler.last_rename_body.get("cross_model") is True

    def test_rename_collection_plain_collision_raises(self, client: HttpCatalogClient) -> None:
        # nexus-gaou3: a plain rename onto an existing target gets a 409 from the
        # server; the client must surface it (not swallow it into a 0 count).
        import httpx

        FakeCatalogHandler.rename_conflicts = True
        try:
            with pytest.raises(httpx.HTTPStatusError):
                client.rename_collection("old__coll", "new__coll")
        finally:
            FakeCatalogHandler.rename_conflicts = False

    def test_rename_collection_cross_model_bypasses_collision(self, client: HttpCatalogClient) -> None:
        # nexus-gaou3: cross_model=True takes the RDR-162 repoint branch even when
        # the server would 409 a plain rename — no exception, repoint count returned.
        FakeCatalogHandler.rename_conflicts = True
        try:
            n = client.rename_collection("old__coll", "new__coll", cross_model=True)
            assert n == 3
        finally:
            FakeCatalogHandler.rename_conflicts = False

    def test_bulk_unlink_uses_unlink_route(self, client: HttpCatalogClient) -> None:
        # bulk_unlink POSTs to /unlink (the same handler as unlink)
        n = client.bulk_unlink(link_type="cites")
        assert n == 1  # fake server returns {"deleted": 1}

    def test_update_documents_collection_batch(self, client: HttpCatalogClient) -> None:
        # Canonical Catalog.update_documents_collection_batch() takes pairs: list[tuple[str,str]].
        # Migrated from old client-specific sig (tumblers list + collection string).
        n = client.update_documents_collection_batch(
            [("1.1.1", "new__coll"), ("1.1.2", "new__coll")]
        )
        assert n == 2

    def test_register_owner(self, client: HttpCatalogClient) -> None:
        from nexus.catalog.catalog import Tumbler
        # Uses POST /owners/upsert
        t = client.register_owner(name="acme")
        assert isinstance(t, Tumbler)

    def test_ensure_owner_for_repo(self, client: HttpCatalogClient) -> None:
        from nexus.catalog.catalog import Tumbler
        t = client.ensure_owner_for_repo(repo="/tmp/myrepo")
        assert isinstance(t, Tumbler)

    def test_set_owner_head_hash(self, client: HttpCatalogClient) -> None:
        # POST /owners/head_hash {tumbler_prefix, head_hash}
        client.set_owner_head_hash("1.1", "abc123def456")

    def test_resync_chunk_count_is_noop(self, client: HttpCatalogClient) -> None:
        # Must not raise
        client.resync_chunk_count_cache("1.1.1")


# ── Guarded methods ───────────────────────────────────────────────────────────

class TestGuardedMethods:
    def test_rebuild_raises(self, client: HttpCatalogClient) -> None:
        with pytest.raises(NotImplementedError, match="rebuild"):
            client.rebuild()

    def test_defrag_raises(self, client: HttpCatalogClient) -> None:
        with pytest.raises(NotImplementedError, match="defrag"):
            client.defrag()

    def test_compact_raises(self, client: HttpCatalogClient) -> None:
        with pytest.raises(NotImplementedError, match="compact"):
            client.compact()

    def test_sync_raises(self, client: HttpCatalogClient) -> None:
        with pytest.raises(NotImplementedError, match="sync"):
            client.sync()

    def test_pull_raises(self, client: HttpCatalogClient) -> None:
        with pytest.raises(NotImplementedError, match="pull"):
            client.pull()

    def test_rebuild_if_stale_noop(self, client: HttpCatalogClient) -> None:
        client.rebuild_if_stale()  # must NOT raise

    def test_catalog_path_is_none(self, client: HttpCatalogClient) -> None:
        assert client.catalog_path is None


# ── Factory seam tests ────────────────────────────────────────────────────────

class TestFactorySeam:
    def test_make_catalog_reader_service_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        from nexus.catalog.factory import make_catalog_reader

        reader = make_catalog_reader()
        assert isinstance(reader, HttpCatalogClient)
        reader.close()

    def test_make_catalog_writer_service_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        from nexus.catalog.factory import _ServiceCatalogWriter, make_catalog_writer

        writer = make_catalog_writer()
        assert isinstance(writer, _ServiceCatalogWriter)
        assert writer.routed is True
        writer.close()

    def test_service_catalog_writer_whitelist_blocks_read_ops(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        from nexus.catalog.factory import make_catalog_writer

        writer = make_catalog_writer()
        with pytest.raises(AttributeError, match="not a catalog write op"):
            _ = writer.resolve  # read op — must be blocked
        writer.close()

    def test_service_catalog_writer_whitelist_allows_write_ops(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        from nexus.catalog.factory import make_catalog_writer

        writer = make_catalog_writer()
        for op in CATALOG_WRITE_OPS:
            attr = getattr(writer, op, None)
            assert attr is not None, f"write op {op!r} missing from _ServiceCatalogWriter"
        writer.close()

    def test_is_interactive_write_pending_false_in_service_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_ServiceCatalogWriter.is_interactive_write_pending() returns False.

        Correct in service mode: the write-pending state is maintained server-side,
        not in the Python process.  The Python writer is a stateless RPC proxy.
        """
        monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        from nexus.catalog.factory import make_catalog_writer

        writer = make_catalog_writer()
        assert writer.is_interactive_write_pending() is False
        writer.close()

    def test_mcp_path_routes_to_http_catalog_client_in_service_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The MCP catalog server (mcp_infra.get_catalog_writer) routes to
        HttpCatalogClient when NX_STORAGE_BACKEND_CATALOG=service.

        This is the critical seam: mcp/catalog.py calls _get_catalog_writer() which
        calls mcp_infra.get_catalog_writer() which calls make_catalog_writer() from
        factory.py.  If any step in this chain bypasses the factory, the service
        routing would be silently skipped.  This test locks the full chain.
        """
        monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok-mcp-test")
        from nexus.catalog.factory import _ServiceCatalogWriter, make_catalog_writer
        from nexus.mcp_infra import get_catalog_writer

        writer = get_catalog_writer()
        assert isinstance(writer, _ServiceCatalogWriter), (
            f"MCP catalog path returned {type(writer)!r} instead of "
            f"_ServiceCatalogWriter; factory seam broken"
        )
        assert writer.routed is True
        writer.close()

    def test_no_production_bypass_of_factory(self) -> None:
        """No Python source file in src/ should bare-construct HttpCatalogClient
        outside of factory.py (seam audit).
        """
        import subprocess
        from pathlib import Path
        # Repo root derived from this file's location, not a hardcoded
        # absolute path — the latter breaks on CI runners (the dir does
        # not exist there). tests/catalog/test_http_catalog_client.py
        # → parents[2] is the repo root.
        repo_root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            ["grep", "-rn", "HttpCatalogClient(", "--include=*.py", "src/"],
            cwd=str(repo_root),
            capture_output=True, text=True,
        )
        hits = [
            line for line in result.stdout.splitlines()
            if "factory.py" not in line
            and "http_catalog_client.py" not in line
        ]
        assert hits == [], (
            "Production code constructs HttpCatalogClient directly, bypassing factory:\n"
            + "\n".join(hits)
        )


# ── resolve_span / resolve_chash unit tests (nexus-njrcn.4) ─────────────────

# 64-char hex chash for test fixtures (all must be valid [0-9a-f]{64})
_FULL_CHASH = "deadbeef" * 8               # 64 hex chars
_CHASH_32   = "deadbeef" * 4               # first 32 chars (server key)
_MISSING_CHASH = "feeded00" * 8            # 64-char hex for 404 path
_MISSING_32 = "feeded00" * 4              # first 32 chars
_GLOBAL_CHASH_FULL = "aabbccdd" * 8        # 64-char hex for global lookup
_GLOBAL_CHASH_32   = "aabbccdd" * 4        # first 32 chars
_MISS_GLOBAL_FULL  = "00000000" * 8        # 64-char hex — missing in server
_MISS_GLOBAL_32    = "00000000" * 4        # first 32 chars


class TestResolveSpan:
    """Unit tests for HttpCatalogClient.resolve_span (nexus-njrcn.4)."""

    def test_resolve_span_returns_chunk_text(self) -> None:
        """Happy path: correct dict shape with chunk_text and metadata."""
        server, base_url = start_fake_server()
        try:
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            result = client.resolve_span(
                f"chash:{_FULL_CHASH}",
                "knowledge__o__bge-768__v1",
            )
            assert result is not None
            assert result["chunk_text"] == "hello span world"
            assert result["metadata"] == {"lang": "en"}
            # chunk_hash carries the full 64-char hex (from parse_chash_span), not the 32-char server key
            assert result["chunk_hash"] == _FULL_CHASH
            assert "char_range" not in result
        finally:
            server.shutdown()

    def test_resolve_span_applies_char_range(self) -> None:
        """char_range slices chunk_text and is included in the output dict."""
        server, base_url = start_fake_server()
        try:
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            # generic chash (not deadbeef) to hit the "generic chunk text" branch
            generic_chash = "cafebabe" * 8
            result = client.resolve_span(
                f"chash:{generic_chash}:8-13",
                "knowledge__o__bge-768__v1",
            )
            assert result is not None
            # "generic chunk text"[8:13] == "chunk"
            assert result["chunk_text"] == "chunk"
            assert result["char_range"] == (8, 13)
        finally:
            server.shutdown()

    def test_resolve_span_non_chash_returns_none(self) -> None:
        """Non-chash span (e.g. line-range) returns None without HTTP call."""
        server, base_url = start_fake_server()
        try:
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            result = client.resolve_span("42-57", "knowledge__o__bge-768__v1")
            assert result is None
        finally:
            server.shutdown()

    def test_resolve_span_404_returns_none(self) -> None:
        """A 404 from the server maps to None (chunk not found)."""
        server, base_url = start_fake_server()
        try:
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            result = client.resolve_span(
                f"chash:{_MISSING_CHASH}",
                "knowledge__o__bge-768__v1",
            )
            assert result is None
        finally:
            server.shutdown()

    def test_resolve_span_malformed_chash_returns_none(self) -> None:
        """Malformed chash span returns None (ValueError caught gracefully)."""
        server, base_url = start_fake_server()
        try:
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            result = client.resolve_span("chash:not-a-hex", "knowledge__o__bge-768__v1")
            assert result is None
        finally:
            server.shutdown()

    def test_resolve_span_t3_ignored(self) -> None:
        """t3 kwarg is accepted (conformance) and silently ignored."""
        server, base_url = start_fake_server()
        try:
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            result = client.resolve_span(
                f"chash:{_FULL_CHASH}",
                "knowledge__o__bge-768__v1",
                t3=object(),  # arbitrary non-None value
            )
            assert result is not None
            assert result["chunk_text"] == "hello span world"
        finally:
            server.shutdown()


class TestResolveChash:
    """Unit tests for HttpCatalogClient.resolve_chash (nexus-njrcn.4)."""

    def test_resolve_chash_returns_full_dict(self) -> None:
        """Happy path: correct dict shape with all expected keys."""
        server, base_url = start_fake_server()
        try:
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            result = client.resolve_chash(f"chash:{_GLOBAL_CHASH_FULL}")
            assert result is not None
            # Canonical contract: chash/chunk_hash are the FULL 64-char parsed hex,
            # not the 32-char wire key the service stores (njrcn.4 review High).
            assert result["chash"] == _GLOBAL_CHASH_FULL
            assert result["chunk_hash"] == _GLOBAL_CHASH_FULL
            assert result["physical_collection"] == "knowledge__o__bge-768__v1"
            assert result["doc_id"] == "1.2.3"
            assert result["chunk_text"] == "resolved chunk body"
            assert result["metadata"] == {"source": "test"}
            assert "char_range" not in result
        finally:
            server.shutdown()

    def test_resolve_chash_applies_char_range(self) -> None:
        """char_range slices chunk_text and is included in output.

        The span form ``chash:<hex>:<start>-<end>`` passes start/end to the
        client which parses them via parse_chash_span; the client then sends
        only chash[:32] to the server and slices the returned text locally.
        Server returns "resolved chunk body"; slice [9:14] == "chunk".
        """
        server, base_url = start_fake_server()
        try:
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            result = client.resolve_chash(f"chash:{_GLOBAL_CHASH_FULL}:9-14")
            assert result is not None
            assert result["chunk_text"] == "chunk"
            assert result["char_range"] == (9, 14)
        finally:
            server.shutdown()

    def test_resolve_chash_404_returns_none(self) -> None:
        """A 404 from the server maps to None."""
        server, base_url = start_fake_server()
        try:
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            result = client.resolve_chash(f"chash:{_MISS_GLOBAL_FULL}")
            assert result is None
        finally:
            server.shutdown()

    def test_resolve_chash_prefer_collection_forwarded(self) -> None:
        """prefer_collection kwarg is forwarded as a query param."""
        server, base_url = start_fake_server()
        try:
            FakeCatalogHandler.reset_log()
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            result = client.resolve_chash(
                f"chash:{_GLOBAL_CHASH_FULL}",
                prefer_collection="knowledge__o__bge-768__v1",
            )
            assert result is not None
            # The server saw the resolve_chash GET
            assert "/resolve_chash" in FakeCatalogHandler.get_ops
        finally:
            server.shutdown()

    def test_resolve_chash_t3_and_chash_index_ignored(self) -> None:
        """t3 and chash_index positional args are accepted and silently ignored."""
        server, base_url = start_fake_server()
        try:
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            result = client.resolve_chash(
                f"chash:{_GLOBAL_CHASH_FULL}",
                object(),   # t3 — positional, must be accepted
                object(),   # chash_index — positional, must be accepted
            )
            assert result is not None
            assert result["chunk_text"] == "resolved chunk body"
        finally:
            server.shutdown()

    def test_resolve_chash_malformed_returns_none(self) -> None:
        """Malformed chash returns None (ValueError caught gracefully)."""
        server, base_url = start_fake_server()
        try:
            client = HttpCatalogClient(base_url=base_url, _token="tok")
            result = client.resolve_chash("chash:not-a-valid-hex")
            assert result is None
        finally:
            server.shutdown()


class TestByFilePathExactMatchGuard:
    """GH #1350 / nexus-h9f1w: by_file_path(owner, fp) must return None for a
    brand-new file even when the service /list ignores file_path under owner and
    returns the FULL owner list. Trusting docs[0] mis-attributed a new file's
    chunks to an unrelated doc, overwriting that doc's manifest (silent data
    corruption, fired twice in prod). The client MUST filter by exact file_path.
    """

    def _client_returning(self, fake_server: str, documents: list[dict]):
        c = HttpCatalogClient(base_url=fake_server, _token="test_tok")

        def _fake_get(path: str, **params: Any) -> dict:
            # Reproduce the buggy server: owner+file_path ignores file_path and
            # returns the entire owner list regardless of the file_path param.
            return {"documents": documents}

        c._get = _fake_get  # type: ignore[method-assign]
        return c

    def test_new_file_under_populated_owner_returns_none(self, fake_server: str) -> None:
        """The corruption trigger: querying a NEW path returns None, not docs[0]."""
        owner_list = [
            {"tumbler": "1.12.1", "title": "Beyond Similarity Search", "file_path": "existing/a.pdf"},
            {"tumbler": "1.12.2", "title": "Other", "file_path": "existing/b.pdf"},
        ]
        c = self._client_returning(fake_server, owner_list)
        assert c.by_file_path("1.12", "brand/new/paper.pdf") is None

    def test_existing_file_returns_its_own_entry_not_docs0(self, fake_server: str) -> None:
        """A real match is selected by exact file_path even when it is NOT docs[0]."""
        owner_list = [
            {"tumbler": "1.12.1", "title": "Beyond Similarity Search", "file_path": "existing/a.pdf"},
            {"tumbler": "1.12.2", "title": "Target", "file_path": "existing/b.pdf"},
        ]
        c = self._client_returning(fake_server, owner_list)
        entry = c.by_file_path("1.12", "existing/b.pdf")
        assert entry is not None
        assert str(entry.tumbler) == "1.12.2"

    def test_empty_owner_returns_none(self, fake_server: str) -> None:
        c = self._client_returning(fake_server, [])
        assert c.by_file_path("1.12", "any/path.pdf") is None
