# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6lfdi: PG-world (service-mode) coverage for the catalog auto-link
generators in ``src/nexus/catalog/link_generator.py``.

``link_generator``'s four ``generate_*`` functions are typed against a local
``Catalog`` in their docstrings/history but only structurally require the
caller-facing subset (``all_documents``, ``resolve_path``, ``link_if_absent``)
— they have ZERO service-mode integration coverage prior to this file (bead
finding). This is exactly the dict-vs-typed / wire-shape class of bug this
repo's ``umvh2``-class recurrence guard chases, so this suite drives the real
client over a fake transport rather than mocking ``Catalog`` methods.

Wiring mirrors production (``src/nexus/indexer.py`` ``_catalog_hook``,
lines ~934-948, and ``src/nexus/commands/enrich.py`` ``run_bib_enrichment``):
reads flow through a real ``HttpCatalogClient`` (the ``reader``/``cat`` arg),
writes flow through a SEPARATE ``HttpCatalogClient`` instance wrapped in
``_ServiceCatalogWriter`` (the ``writer`` arg) — never the same object,
matching the reader/writer split ``factory.make_catalog_reader`` /
``make_catalog_writer`` enforce in service mode.

Distinct from ``tests/catalog/test_http_catalog_client.py``'s
``FakeCatalogHandler``: that fixture is stateless/canned (one fixed document
shape per route, shared across ~90 test classes exercising the full client
surface). The link generators need a small STATEFUL subset (a documents table
+ a links table) so multi-document citation/filepath/pdf-hash scenarios and
idempotency (``link_if_absent`` re-run == 0 new links) can be exercised
faithfully without perturbing that shared fixture. This file therefore runs
its own local fake server — additive, does not touch
``tests/catalog/test_http_catalog_client.py`` or
``tests/catalog/test_shape_parity_tripwire.py`` (owned by a sibling bead).

Route shapes (GET ``/list``, ``/show``, ``/link_query``; POST ``/link``) are
wire-faithful to ``CatalogHandler.java``'s switch cases per the route table
in ``http_catalog_client.py``'s module docstring.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from nexus.catalog.factory import _ServiceCatalogWriter
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.link_generator import (
    generate_citation_links,
    generate_pdf_corpus_links,
    generate_prose_filepath_links,
    generate_rdr_filepath_links,
)
from nexus.catalog.tumbler import Tumbler

# ── stateful fake server ──────────────────────────────────────────────────────


class _State:
    """Server-side fixture state (documents + links tables), reset per test."""

    documents: dict[str, dict[str, Any]] = {}
    links: list[dict[str, Any]] = []

    @classmethod
    def reset(cls) -> None:
        cls.documents = {}
        cls.links = []

    @classmethod
    def add_document(cls, tumbler: str, **fields: Any) -> str:
        base: dict[str, Any] = {
            "tumbler": tumbler,
            "title": tumbler,
            "content_type": "",
            "file_path": "",
            "source_uri": "",
            "chunk_count": 0,
            "head_hash": "",
            "metadata": {},
            "source_mtime": 0.0,
            "bib_year": 0,
            "bib_authors": "",
            "bib_venue": "",
            "bib_citation_count": 0,
        }
        base.update(fields)
        cls.documents[tumbler] = base
        return tumbler

    @classmethod
    def links_matching(
        cls, from_t: str, to_t: str, link_type: str,
    ) -> list[dict[str, Any]]:
        out = []
        for lnk in cls.links:
            if from_t and lnk["from_tumbler"] != from_t:
                continue
            if to_t and lnk["to_tumbler"] != to_t:
                continue
            if link_type and lnk["link_type"] != link_type:
                continue
            out.append(lnk)
        return out


class FakeLinkGenHandler(BaseHTTPRequestHandler):
    """Minimal stateful fake — only the routes ``link_generator.py`` touches
    via ``HttpCatalogClient``: GET ``/list``, ``/show``, ``/link_query``;
    POST ``/link``. Wire shapes mirror ``CatalogHandler.java``'s switch cases
    (see the route table in ``http_catalog_client.py``'s module docstring).
    """

    def log_message(self, *args: Any) -> None:
        pass  # suppress test noise

    def _send_json(self, body: Any, code: int = 200) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _query_params(self) -> dict[str, str]:
        qs = urlparse(self.path).query
        return {k: v[0] for k, v in parse_qs(qs).items()} if qs else {}

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length:
            return json.loads(self.rfile.read(length))
        return {}

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler contract
        path = urlparse(self.path).path
        op = path.removeprefix("/v1/catalog")
        params = self._query_params()

        if op == "/list":
            docs = list(_State.documents.values())
            content_type = params.get("content_type")
            if content_type:
                # Mirrors CatalogHandler: the content_type branch ignores
                # limit/offset and returns ALL matching rows in one response
                # (HttpCatalogClient.all_documents relies on this for its
                # single-request content_type-filtered path).
                matched = [d for d in docs if d.get("content_type") == content_type]
                self._send_json({"documents": matched, "count": len(matched)})
                return
            # Unfiltered: HttpCatalogClient.all_documents(limit=0) paginates
            # with limit=1000/offset stepping until a short page is seen.
            limit = int(params.get("limit", 0)) or (len(docs) or 1)
            offset = int(params.get("offset", 0))
            page = docs[offset:offset + limit]
            self._send_json({"documents": page, "count": len(page)})
        elif op == "/show":
            tumbler = params.get("tumbler", "")
            doc = _State.documents.get(tumbler)
            if doc is None:
                self.send_response(404)
                self.end_headers()
                return
            self._send_json(doc)
        elif op == "/link_query":
            matches = _State.links_matching(
                params.get("from_tumbler", ""),
                params.get("to_tumbler", ""),
                params.get("link_type", ""),
            )
            self._send_json({"links": matches, "count": len(matches)})
        else:
            self._send_json({"error": f"unknown GET op: {op}"}, 404)

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler contract
        path = urlparse(self.path).path
        op = path.removeprefix("/v1/catalog")
        body = self._read_body()

        if op == "/link":
            from_t = body.get("from_tumbler", "")
            to_t = body.get("to_tumbler", "")
            link_type = body.get("link_type", "")
            existing = _State.links_matching(from_t, to_t, link_type)
            if existing:
                # Real service semantics: POST /link is an UPSERT
                # (ON CONFLICT DO UPDATE) — merges fields, created=False.
                existing[0].update({
                    "created_by": body.get("created_by", ""),
                    "from_span": body.get("from_span", ""),
                    "to_span": body.get("to_span", ""),
                })
                self._send_json({"ok": True, "created": False})
            else:
                _State.links.append({
                    "from_tumbler": from_t,
                    "to_tumbler": to_t,
                    "link_type": link_type,
                    "created_by": body.get("created_by", ""),
                    "from_span": body.get("from_span", ""),
                    "to_span": body.get("to_span", ""),
                })
                self._send_json({"ok": True, "created": True})
        else:
            self._send_json({"ok": True})


def _start_server() -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), FakeLinkGenHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.05)  # brief wait for the thread to reach serve_forever
    return server, f"http://127.0.0.1:{port}"


@pytest.fixture
def fake_server():
    _State.reset()
    server, url = _start_server()
    yield url
    server.shutdown()


@pytest.fixture
def reader(fake_server: str):
    with HttpCatalogClient(base_url=fake_server, tenant="tenant_abc", _token="test_tok") as c:
        yield c


@pytest.fixture
def writer(fake_server: str):
    # nexus-6lfdi: mirrors production wiring — a SEPARATE HttpCatalogClient
    # instance from the reader, wrapped in _ServiceCatalogWriter, exactly as
    # indexer._catalog_hook and commands/enrich.py wire make_catalog_reader()
    # + make_catalog_writer() as two distinct client objects pointed at the
    # same service.
    client = HttpCatalogClient(base_url=fake_server, tenant="tenant_abc", _token="test_tok")
    w = _ServiceCatalogWriter(client)
    yield w
    w.close()


# ── generate_citation_links ──────────────────────────────────────────────────


class TestCitationLinksServiceMode:
    def test_citation_from_ss_id(self, reader, writer) -> None:
        _State.add_document(
            "1.1.1", title="Paper A", content_type="paper",
            metadata={"bib_semantic_scholar_id": "ssA", "references": ["ssB"]},
        )
        _State.add_document(
            "1.1.2", title="Paper B", content_type="paper",
            metadata={"bib_semantic_scholar_id": "ssB"},
        )
        count = generate_citation_links(reader, writer=writer)
        assert count == 1
        assert len(_State.links) == 1
        lnk = _State.links[0]
        assert lnk["from_tumbler"] == "1.1.1"
        assert lnk["to_tumbler"] == "1.1.2"
        assert lnk["link_type"] == "cites"
        assert lnk["created_by"] == "bib_enricher"

    def test_no_self_citation(self, reader, writer) -> None:
        _State.add_document(
            "1.1.1", title="Paper A", content_type="paper",
            metadata={"bib_semantic_scholar_id": "ssA", "references": ["ssA"]},
        )
        count = generate_citation_links(reader, writer=writer)
        assert count == 0
        assert _State.links == []

    def test_no_link_when_target_missing(self, reader, writer) -> None:
        _State.add_document(
            "1.1.1", title="Paper A", content_type="paper",
            metadata={"bib_semantic_scholar_id": "ssA", "references": ["ssC"]},
        )
        count = generate_citation_links(reader, writer=writer)
        assert count == 0

    def test_no_duplicate_citations_on_rerun(self, reader, writer) -> None:
        _State.add_document(
            "1.1.1", title="Paper A", content_type="paper",
            metadata={"bib_semantic_scholar_id": "ssA", "references": ["ssB"]},
        )
        _State.add_document(
            "1.1.2", title="Paper B", content_type="paper",
            metadata={"bib_semantic_scholar_id": "ssB"},
        )
        first = generate_citation_links(reader, writer=writer)
        second = generate_citation_links(reader, writer=writer)
        assert first == 1
        assert second == 0  # link_if_absent's pre-flight /link_query hit
        assert len(_State.links) == 1  # no duplicate row created server-side

    def test_openalex_id_space_also_matches(self, reader, writer) -> None:
        """nexus-57mk: bib_openalex_id is indexed alongside
        bib_semantic_scholar_id in the same id_to_tumbler map."""
        _State.add_document(
            "1.1.1", title="Paper A", content_type="paper",
            metadata={"bib_openalex_id": "W1", "references": ["W2"]},
        )
        _State.add_document(
            "1.1.2", title="Paper B", content_type="paper",
            metadata={"bib_openalex_id": "W2"},
        )
        count = generate_citation_links(reader, writer=writer)
        assert count == 1


# ── generate_rdr_filepath_links ───────────────────────────────────────────────


class TestRdrFilepathLinksServiceMode:
    def test_backtick_path_creates_link(self, reader, writer, tmp_path) -> None:
        rdr_path = tmp_path / "rdr.md"
        rdr_path.write_text("We modified `src/nexus/catalog/catalog.py` to fix the bug.")
        _State.add_document(
            "1.1.1", title="catalog.py", content_type="code",
            file_path="src/nexus/catalog/catalog.py",
        )
        _State.add_document(
            "1.1.2", title="Fix Catalog Bug", content_type="rdr",
            file_path=str(rdr_path),
        )
        count = generate_rdr_filepath_links(reader, writer=writer)
        assert count == 1
        lnk = _State.links[0]
        assert lnk["from_tumbler"] == "1.1.2"
        assert lnk["to_tumbler"] == "1.1.1"
        assert lnk["link_type"] == "implements"
        assert lnk["created_by"] == "filepath_extractor"

    def test_no_link_for_unindexed_path(self, reader, writer, tmp_path) -> None:
        rdr_path = tmp_path / "rdr.md"
        rdr_path.write_text("See `src/nexus/missing_file.py` for details.")
        _State.add_document(
            "1.1.1", title="Dangling Ref", content_type="rdr",
            file_path=str(rdr_path),
        )
        count = generate_rdr_filepath_links(reader, writer=writer)
        assert count == 0

    def test_rdr_without_file_on_disk_skipped(self, reader, writer) -> None:
        _State.add_document(
            "1.1.1", title="ghost.py", content_type="code",
            file_path="src/ghost.py",
        )
        _State.add_document(
            "1.1.2", title="Ghost RDR", content_type="rdr",
            file_path="/nonexistent/path/rdr.md",
        )
        count = generate_rdr_filepath_links(reader, writer=writer)
        assert count == 0

    def test_no_duplicate_on_rerun(self, reader, writer, tmp_path) -> None:
        rdr_path = tmp_path / "rdr.md"
        rdr_path.write_text("Edit `src/catalog.py`.")
        _State.add_document(
            "1.1.1", title="catalog.py", content_type="code",
            file_path="src/catalog.py",
        )
        _State.add_document(
            "1.1.2", title="Catalog Work", content_type="rdr",
            file_path=str(rdr_path),
        )
        generate_rdr_filepath_links(reader, writer=writer)
        second = generate_rdr_filepath_links(reader, writer=writer)
        assert second == 0
        assert len(_State.links) == 1

    def test_incremental_new_tumblers_scopes_scan(self, reader, writer, tmp_path) -> None:
        a_rdr = tmp_path / "a.md"
        a_rdr.write_text("See `src/nexus/a.py`.")
        b_rdr = tmp_path / "b.md"
        b_rdr.write_text("See `src/nexus/b.py`.")
        _State.add_document("1.1.1", title="a.py", content_type="code", file_path="src/nexus/a.py")
        _State.add_document("1.1.2", title="b.py", content_type="code", file_path="src/nexus/b.py")
        _State.add_document("1.1.3", title="RDR A", content_type="rdr", file_path=str(a_rdr))
        _State.add_document("1.1.4", title="RDR B", content_type="rdr", file_path=str(b_rdr))

        count = generate_rdr_filepath_links(
            reader, writer=writer, new_tumblers=[Tumbler.parse("1.1.3")],
        )
        assert count == 1
        assert len(_State.links) == 1
        assert _State.links[0]["from_tumbler"] == "1.1.3"


# ── generate_prose_filepath_links ────────────────────────────────────────────


class TestProseFilepathLinksServiceMode:
    def test_prose_doc_links_to_code(self, reader, writer, tmp_path) -> None:
        prose_path = tmp_path / "runbook.md"
        prose_path.write_text("See ``src/nexus/foo.py`` for the impl.\n")
        _State.add_document(
            "1.1.1", title="Runbook", content_type="prose",
            file_path=str(prose_path),
        )
        _State.add_document(
            "1.1.2", title="foo.py", content_type="code",
            file_path="src/nexus/foo.py",
        )
        count = generate_prose_filepath_links(reader, writer=writer)
        assert count == 1
        lnk = _State.links[0]
        assert lnk["from_tumbler"] == "1.1.1"
        assert lnk["to_tumbler"] == "1.1.2"
        assert lnk["link_type"] == "implements"

    def test_non_source_root_dir_links(self, reader, writer, tmp_path) -> None:
        """nexus-sob9 widening contract: docs/ -> conexus/ (no src/ anchor)
        must link via the wider prose regex."""
        prose_path = tmp_path / "guide.md"
        prose_path.write_text("See ``conexus/skills/foo.md`` for usage.\n")
        _State.add_document(
            "1.1.1", title="Guide", content_type="prose",
            file_path=str(prose_path),
        )
        _State.add_document(
            "1.1.2", title="foo.md", content_type="code",
            file_path="conexus/skills/foo.md",
        )
        count = generate_prose_filepath_links(reader, writer=writer)
        assert count == 1

    def test_bare_filename_does_not_match(self, reader, writer, tmp_path) -> None:
        prose_path = tmp_path / "loose.md"
        prose_path.write_text("Run ``foo.py`` to start.\n")
        _State.add_document(
            "1.1.1", title="Loose", content_type="prose",
            file_path=str(prose_path),
        )
        _State.add_document(
            "1.1.2", title="foo.py", content_type="code", file_path="foo.py",
        )
        count = generate_prose_filepath_links(reader, writer=writer)
        assert count == 0


# ── generate_pdf_corpus_links ────────────────────────────────────────────────


class TestPdfCorpusLinksServiceMode:
    def test_two_pdfs_with_same_hash_get_linked(self, reader, writer) -> None:
        _State.add_document(
            "1.1.1", title="Paper1", content_type="paper",
            head_hash="abc123", physical_collection="knowledge__delos",
        )
        _State.add_document(
            "1.1.2", title="Paper2", content_type="paper",
            head_hash="abc123", physical_collection="knowledge__art-papers",
        )
        count = generate_pdf_corpus_links(reader, writer=writer)
        assert count == 1
        lnk = _State.links[0]
        # anchor = lexicographically-first tumbler ("1.1.1" < "1.1.2")
        assert lnk["from_tumbler"] == "1.1.2"
        assert lnk["to_tumbler"] == "1.1.1"
        assert lnk["link_type"] == "same-as"
        assert lnk["created_by"] == "content_hash_dedup"

    def test_no_link_when_hash_unique(self, reader, writer) -> None:
        _State.add_document(
            "1.1.1", title="Unique", content_type="paper",
            head_hash="unique-hash", physical_collection="knowledge__delos",
        )
        count = generate_pdf_corpus_links(reader, writer=writer)
        assert count == 0

    def test_idempotent(self, reader, writer) -> None:
        _State.add_document(
            "1.1.1", title="P1", content_type="paper",
            head_hash="h1", physical_collection="knowledge__delos",
        )
        _State.add_document(
            "1.1.2", title="P2", content_type="paper",
            head_hash="h1", physical_collection="knowledge__art-papers",
        )
        first = generate_pdf_corpus_links(reader, writer=writer)
        second = generate_pdf_corpus_links(reader, writer=writer)
        assert first == 1
        assert second == 0
        assert len(_State.links) == 1

    def test_pdfs_without_head_hash_skipped(self, reader, writer) -> None:
        _State.add_document(
            "1.1.1", title="NoHash1", content_type="paper",
            head_hash="", physical_collection="knowledge__delos",
        )
        _State.add_document(
            "1.1.2", title="NoHash2", content_type="paper",
            head_hash="", physical_collection="knowledge__art-papers",
        )
        count = generate_pdf_corpus_links(reader, writer=writer)
        assert count == 0
