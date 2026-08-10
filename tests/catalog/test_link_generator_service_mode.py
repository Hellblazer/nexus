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
    _FILE_PATH_RE,
    _PROSE_PATH_RE,
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
    #: nexus-5i864: owners keyed by tumbler_prefix. ``resolve_path`` now
    #: performs the owner lookup + curator guard the local catalog always
    #: did, so the fake must serve ``/owners/show`` or every resolution
    #: returns None and the generators silently emit zero links.
    owners: dict[str, dict[str, Any]] = {}
    #: T0.2 (nx/chroma-residue-plan-2026-08-10): records every GET /list
    #: request's query params, so tests can pin the ROUND-TRIP count and
    #: prove a request was content_type-scoped rather than an unfiltered
    #: full-catalog fetch.
    list_requests: list[dict[str, str]] = []

    @classmethod
    def reset(cls) -> None:
        cls.documents = {}
        cls.links = []
        cls.list_requests = []
        # Default repo owner for the "1.1" prefix every fixture tumbler
        # sits under. ``repo_root`` is empty because every document in
        # this module carries an ABSOLUTE file_path, which resolve_path
        # returns before consulting repo_root; a test needing the
        # relative-path recombination must call set_owner() with one.
        cls.owners = {
            "1.1": {
                "tumbler_prefix": "1.1",
                "owner_type": "repo",
                "name": "fake-repo",
                "repo_root": "",
                "repo_hash": "",
                "head_hash": "",
            },
        }

    @classmethod
    def set_owner(cls, tumbler_prefix: str, **fields: Any) -> None:
        """Override/insert an owner row (e.g. a curator, or a repo_root)."""
        base = dict(cls.owners.get(tumbler_prefix) or {})
        base.setdefault("tumbler_prefix", tumbler_prefix)
        base.setdefault("owner_type", "repo")
        base.setdefault("repo_root", "")
        base.update(fields)
        cls.owners[tumbler_prefix] = base

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
            _State.list_requests.append(dict(params))
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
        elif op == "/owners/show":
            # nexus-5i864: mirrors CatalogHandler's owner show — 404 for an
            # unknown prefix, and a flat dict carrying tumbler_prefix (the
            # key HttpCatalogClient.get_owner_by_prefix requires before it
            # will treat the response as a hit).
            owner = _State.owners.get(params.get("tumbler_prefix", ""))
            if owner is None:
                self.send_response(404)
                self.end_headers()
                return
            self._send_json(owner)
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


# ── T0.2 (nx/chroma-residue-plan-2026-08-10): fetch-scope narrowing ─────────
#
# Before this change, all three of the generators below (citations excepted
# — see link_generator.py's ``_all_entries`` docstring) fetched the ENTIRE
# tenant catalog via ``_all_entries`` -> ``cat.all_documents()`` (unbounded,
# paginated 1000-at-a-time) and filtered by content_type client-side. A real
# ``nx index repo .`` run measured 37.0s of a 41.8s catalog-hook phase spent
# in linking that created ZERO links, against a ~19.8k-document catalog.
#
# The fix (``_entries_of_type`` -> ``all_documents(content_type=...)``)
# issues ONE server-side-filtered request per content type the generator
# actually matches on, instead of an O(catalog size) unfiltered scan.


def _old_generate_rdr_filepath_links(cat, *, writer=None, new_tumblers=None):
    """Reference copy of the PRE-T0.2 ``generate_rdr_filepath_links`` body
    (full unfiltered ``cat.all_documents()`` + client-side filter), kept
    ONLY so the equivalence tests below can prove the narrowed fetch
    produces a byte-identical link set. Not a production code path.
    """
    if new_tumblers is not None and len(new_tumblers) == 0:
        return 0
    entries = cat.all_documents()
    rdr_entries = [e for e in entries if e.content_type == "rdr" and e.file_path]
    code_entries = [e for e in entries if e.content_type == "code" and e.file_path]
    if new_tumblers is not None:
        new_set = {str(t) for t in new_tumblers}
        rdr_entries = [e for e in rdr_entries if str(e.tumbler) in new_set]
    path_to_code: dict[str, Tumbler] = {code.file_path: code.tumbler for code in code_entries}
    count = 0
    for rdr in rdr_entries:
        resolved = cat.resolve_path(rdr.tumbler)
        if resolved is None or not resolved.is_file():
            continue
        try:
            text = resolved.read_text(errors="replace")
        except OSError:
            continue
        seen_targets: set[str] = set()
        for match in _FILE_PATH_RE.finditer(text):
            fpath = match.group(0)
            if fpath in seen_targets:
                continue
            seen_targets.add(fpath)
            code_tumbler = path_to_code.get(fpath)
            if code_tumbler is None:
                continue
            try:
                created = (writer if writer is not None else cat).link_if_absent(
                    rdr.tumbler, code_tumbler, "implements", created_by="filepath_extractor",
                )
            except ValueError:
                continue
            if created:
                count += 1
    return count


def _old_generate_prose_filepath_links(cat, *, writer=None, new_tumblers=None):
    """Reference copy of the PRE-T0.2 ``generate_prose_filepath_links`` body."""
    if new_tumblers is not None and len(new_tumblers) == 0:
        return 0
    entries = cat.all_documents()
    prose_entries = [
        e for e in entries
        if e.content_type in ("prose", "markdown", "docs") and e.file_path
    ]
    code_entries = [e for e in entries if e.content_type == "code" and e.file_path]
    if new_tumblers is not None:
        new_set = {str(t) for t in new_tumblers}
        prose_entries = [e for e in prose_entries if str(e.tumbler) in new_set]
    path_to_code: dict[str, Tumbler] = {code.file_path: code.tumbler for code in code_entries}
    count = 0
    for prose in prose_entries:
        resolved = cat.resolve_path(prose.tumbler)
        if resolved is None or not resolved.is_file():
            continue
        try:
            text = resolved.read_text(errors="replace")
        except OSError:
            continue
        seen_targets: set[str] = set()
        for match in _PROSE_PATH_RE.finditer(text):
            fpath = match.group(0)
            if fpath in seen_targets:
                continue
            seen_targets.add(fpath)
            code_tumbler = path_to_code.get(fpath)
            if code_tumbler is None:
                continue
            try:
                created = (writer if writer is not None else cat).link_if_absent(
                    prose.tumbler, code_tumbler, "implements", created_by="filepath_extractor",
                )
            except ValueError:
                continue
            if created:
                count += 1
    return count


def _old_generate_pdf_corpus_links(cat, *, writer=None, new_tumblers=None):
    """Reference copy of the PRE-T0.2 ``generate_pdf_corpus_links`` body."""
    if new_tumblers is not None and len(new_tumblers) == 0:
        return 0
    entries = cat.all_documents()
    pdf_entries = [
        e for e in entries
        if e.content_type in ("pdf", "paper") and e.head_hash
    ]
    by_hash: dict[str, list] = {}
    for e in pdf_entries:
        by_hash.setdefault(e.head_hash, []).append(e)
    new_set = {str(t) for t in new_tumblers} if new_tumblers is not None else None
    count = 0
    for group in by_hash.values():
        if len(group) < 2:
            continue
        anchor = min(group, key=lambda e: str(e.tumbler))
        for member in group:
            if member.tumbler == anchor.tumbler:
                continue
            if new_set is not None and str(member.tumbler) not in new_set:
                continue
            try:
                created = (writer if writer is not None else cat).link_if_absent(
                    member.tumbler, anchor.tumbler, "same-as", created_by="content_hash_dedup",
                )
            except ValueError:
                continue
            if created:
                count += 1
    return count


class TestLinkGeneratorFetchScope:
    """Round-trip pin: each filepath/pdf generator must issue exactly one
    content_type-filtered ``/list`` request per content type it matches
    on, and NEVER an unfiltered full-catalog fetch.

    FAILS against pre-T0.2 code: ``_all_entries`` -> ``cat.all_documents()``
    sends a ``/list`` request with NO ``content_type`` param (server-side
    unbounded catalog scan) before filtering client-side, so
    ``list_requests`` would contain an entry with an empty content_type
    and the request COUNT would not be pinned to the generator's own
    content-type set.
    """

    def test_rdr_linker_fetches_only_its_content_types(self, reader, writer, tmp_path) -> None:
        rdr_path = tmp_path / "rdr.md"
        rdr_path.write_text("See `src/nexus/foo.py`.\n")
        _State.add_document(
            "1.1.1", title="foo.py", content_type="code", file_path="src/nexus/foo.py",
        )
        _State.add_document(
            "1.1.2", title="RDR", content_type="rdr", file_path=str(rdr_path),
        )
        # Noise: documents of unrelated content types that a full-catalog
        # scan would also have fetched. Must not add extra round trips.
        for i in range(25):
            _State.add_document(f"9.9.{i}", title=f"noise{i}", content_type="knowledge")

        generate_rdr_filepath_links(reader, writer=writer)

        requested = [r.get("content_type", "") for r in _State.list_requests]
        assert all(requested), f"unfiltered /list request(s) issued: {requested}"
        assert set(requested) == {"rdr", "code"}
        assert len(_State.list_requests) == 2

    def test_prose_linker_fetches_only_its_content_types(self, reader, writer, tmp_path) -> None:
        prose_path = tmp_path / "guide.md"
        prose_path.write_text("See ``src/nexus/foo.py``.\n")
        _State.add_document(
            "1.1.1", title="foo.py", content_type="code", file_path="src/nexus/foo.py",
        )
        _State.add_document(
            "1.1.2", title="Guide", content_type="prose", file_path=str(prose_path),
        )

        generate_prose_filepath_links(reader, writer=writer)

        requested = [r.get("content_type", "") for r in _State.list_requests]
        assert all(requested), f"unfiltered /list request(s) issued: {requested}"
        assert set(requested) == {"prose", "markdown", "docs", "code"}
        assert len(_State.list_requests) == 4

    def test_pdf_linker_fetches_only_its_content_types(self, reader, writer) -> None:
        _State.add_document(
            "1.1.1", title="P1", content_type="paper",
            head_hash="h1", physical_collection="knowledge__a",
        )
        _State.add_document(
            "1.1.2", title="P2", content_type="paper",
            head_hash="h1", physical_collection="knowledge__b",
        )

        generate_pdf_corpus_links(reader, writer=writer)

        requested = [r.get("content_type", "") for r in _State.list_requests]
        assert all(requested), f"unfiltered /list request(s) issued: {requested}"
        assert set(requested) == {"pdf", "paper"}
        assert len(_State.list_requests) == 2

    def test_citation_linker_unchanged_still_full_scans(self, reader, writer) -> None:
        """generate_citation_links is deliberately UNCHANGED (bib IDs can
        live on any content type — see link_generator.py). Pins that the
        narrowing did not touch it: exactly one unfiltered request.
        """
        _State.add_document(
            "1.1.1", title="A", content_type="paper",
            metadata={"bib_semantic_scholar_id": "a", "references": ["b"]},
        )
        _State.add_document(
            "1.1.2", title="B", content_type="paper",
            metadata={"bib_semantic_scholar_id": "b"},
        )

        generate_citation_links(reader, writer=writer)

        requested = [r.get("content_type", "") for r in _State.list_requests]
        assert requested == [""]


class TestLinkGeneratorFetchScopeEquivalence:
    """Load-bearing: the content_type-scoped fetch must produce a
    byte-identical link set to the old full-scan-then-filter approach.
    A performance win that silently changes WHICH links get created is a
    correctness regression, not an optimization — this is what proves it
    isn't one.
    """

    def _snapshot_and_clear(self) -> list[tuple[str, str, str, str]]:
        snap = sorted(
            (l["from_tumbler"], l["to_tumbler"], l["link_type"], l["created_by"])
            for l in _State.links
        )
        _State.links.clear()
        return snap

    def test_rdr_linker_equivalent(self, reader, writer, tmp_path) -> None:
        rdr1 = tmp_path / "rdr1.md"
        rdr1.write_text("See `src/nexus/a.py` and `src/nexus/missing.py`.\n")
        rdr2 = tmp_path / "rdr2.md"
        rdr2.write_text("Touches `tests/b_test.py`.\n")
        _State.add_document("1.1.1", title="a.py", content_type="code", file_path="src/nexus/a.py")
        _State.add_document("1.1.2", title="b_test.py", content_type="code", file_path="tests/b_test.py")
        _State.add_document("1.1.3", title="RDR1", content_type="rdr", file_path=str(rdr1))
        _State.add_document("1.1.4", title="RDR2", content_type="rdr", file_path=str(rdr2))
        # Noise: a non-code, non-rdr doc reusing both a real code file_path
        # and the RDR's own file_path — must not be pulled in as a source
        # or target by either implementation.
        _State.add_document("2.2.1", title="lookalike", content_type="knowledge", file_path="src/nexus/a.py")
        _State.add_document("2.2.2", title="lookalike2", content_type="prose", file_path=str(rdr1))

        old_snap = self._run(_old_generate_rdr_filepath_links, reader, writer)
        new_snap = self._run(generate_rdr_filepath_links, reader, writer)
        assert old_snap  # non-vacuous: real links were actually produced
        assert new_snap == old_snap

    def test_prose_linker_equivalent(self, reader, writer, tmp_path) -> None:
        p1 = tmp_path / "runbook.md"
        p1.write_text("See ``src/nexus/foo.py``.\n")
        p2 = tmp_path / "guide.md"
        p2.write_text("See ``conexus/skills/bar.md``.\n")
        _State.add_document("1.1.1", title="foo.py", content_type="code", file_path="src/nexus/foo.py")
        _State.add_document("1.1.2", title="bar.md", content_type="code", file_path="conexus/skills/bar.md")
        _State.add_document("1.1.3", title="Runbook", content_type="prose", file_path=str(p1))
        _State.add_document("1.1.4", title="Guide", content_type="markdown", file_path=str(p2))
        # Noise: unrelated content type must never contribute a source-side match.
        _State.add_document("2.2.1", title="noise", content_type="knowledge", file_path=str(p1))

        old_snap = self._run(_old_generate_prose_filepath_links, reader, writer)
        new_snap = self._run(generate_prose_filepath_links, reader, writer)
        assert old_snap
        assert new_snap == old_snap

    def test_pdf_linker_equivalent(self, reader, writer) -> None:
        _State.add_document("1.1.1", title="P1", content_type="pdf", head_hash="h1", physical_collection="knowledge__a")
        _State.add_document("1.1.2", title="P2", content_type="paper", head_hash="h1", physical_collection="knowledge__b")
        _State.add_document("1.1.3", title="P3", content_type="paper", head_hash="h1", physical_collection="knowledge__c")
        _State.add_document("1.1.4", title="Unique", content_type="pdf", head_hash="h2", physical_collection="knowledge__d")
        # Noise: same head_hash, wrong content_type — must not join the group.
        _State.add_document("2.2.1", title="noise", content_type="knowledge", head_hash="h1")

        old_snap = self._run(_old_generate_pdf_corpus_links, reader, writer)
        new_snap = self._run(generate_pdf_corpus_links, reader, writer)
        assert old_snap
        assert new_snap == old_snap

    def test_prose_linker_docs_source_type_equivalent(self, reader, writer, tmp_path) -> None:
        """Coverage-gap fix: the general prose equivalence test above only
        exercises "prose" and "markdown" as SOURCE content types, never
        "docs" -- despite "docs" being part of
        ``generate_prose_filepath_links``'s documented source-type set.
        Proves the narrowed fetch is equivalent for that source type too,
        not just the two exercised elsewhere."""
        d1 = tmp_path / "runbook.md"
        d1.write_text("See ``src/nexus/foo.py``.\n")
        _State.add_document("1.1.1", title="foo.py", content_type="code", file_path="src/nexus/foo.py")
        _State.add_document("1.1.2", title="Runbook", content_type="docs", file_path=str(d1))

        old_snap = self._run(_old_generate_prose_filepath_links, reader, writer)
        new_snap = self._run(generate_prose_filepath_links, reader, writer)
        assert old_snap  # non-vacuous: real links were actually produced
        assert new_snap == old_snap

    def test_rdr_linker_incremental_equivalent(self, reader, writer, tmp_path) -> None:
        """Same proof, scoped by new_tumblers (the production call shape
        from indexer.py)."""
        rdr1 = tmp_path / "rdr1.md"
        rdr1.write_text("See `src/nexus/a.py`.\n")
        rdr2 = tmp_path / "rdr2.md"
        rdr2.write_text("See `src/nexus/a.py` too.\n")
        _State.add_document("1.1.1", title="a.py", content_type="code", file_path="src/nexus/a.py")
        rdr1_t = Tumbler.parse("1.1.2")
        rdr2_t = Tumbler.parse("1.1.3")
        _State.add_document(str(rdr1_t), title="RDR1", content_type="rdr", file_path=str(rdr1))
        _State.add_document(str(rdr2_t), title="RDR2", content_type="rdr", file_path=str(rdr2))

        old_snap = self._run(_old_generate_rdr_filepath_links, reader, writer, new_tumblers=[rdr1_t])
        new_snap = self._run(generate_rdr_filepath_links, reader, writer, new_tumblers=[rdr1_t])
        assert old_snap
        assert new_snap == old_snap

    def _run(self, fn, reader, writer, **kwargs) -> list[tuple[str, str, str, str]]:
        fn(reader, writer=writer, **kwargs)
        return self._snapshot_and_clear()


# ── T0.2 follow-up: skip a generator's fetches when no new doc could ever ───
# match its source content_type ──────────────────────────────────────────────
#
# The T0.2 narrowing above still pays for a generator's full content-type
# fetch set on EVERY incremental run with a non-empty new_tumblers, even when
# none of the new documents are of that generator's own source type (e.g. an
# `nx index repo .` run whose new_tumblers are all "code" gains nothing from
# fetching "rdr" -- an RDR can never newly link to code it didn't just gain a
# path to). The existing `len(new_tumblers) == 0` short-circuit only covers
# the "nothing new at all" case, not "something new, but never our source
# type" -- which the indexer.py measurement showed is the COMMON case (37.0s
# of an 41.8s catalog-hook phase spent producing 0 links).
#
# `new_content_types` is opt-in and additive: passing it lets a generator
# prove -- without any fetch -- that its source-type filter can only ever
# produce an empty result, and skip straight to `return 0`. Omitting it
# (every pre-existing call site) preserves the exact old fetch-and-filter
# behavior.


class TestLinkGeneratorSeedSkip:
    """Proves the fetch-skip property directly against the /list request
    spy -- zero REQUESTS, not just zero links. A generator that still
    fetches and then produces 0 links is the pre-existing (correct but
    slow) behavior, not this optimization; these tests fail against that
    behavior and pass only once the pre-fetch short-circuit fires.
    """

    def test_rdr_linker_zero_requests_when_no_rdr_seed(self, reader, writer, tmp_path) -> None:
        rdr_path = tmp_path / "rdr.md"
        rdr_path.write_text("See `src/nexus/foo.py`.\n")
        _State.add_document("1.1.1", title="foo.py", content_type="code", file_path="src/nexus/foo.py")
        _State.add_document("1.1.2", title="RDR", content_type="rdr", file_path=str(rdr_path))
        # This run's new_tumblers name only the "code" doc -- the rdr
        # linker's source type ("rdr") is not among new_content_types.
        code_t = Tumbler.parse("1.1.1")

        count = generate_rdr_filepath_links(
            reader, writer=writer,
            new_tumblers=[code_t], new_content_types={"code"},
        )

        assert count == 0
        assert _State.list_requests == []

    def test_prose_linker_zero_requests_when_no_prose_seed(self, reader, writer) -> None:
        _State.add_document("1.1.1", title="foo.py", content_type="code", file_path="src/nexus/foo.py")
        code_t = Tumbler.parse("1.1.1")

        count = generate_prose_filepath_links(
            reader, writer=writer,
            new_tumblers=[code_t], new_content_types={"code"},
        )

        assert count == 0
        assert _State.list_requests == []

    def test_pdf_linker_zero_requests_when_no_pdf_seed(self, reader, writer) -> None:
        _State.add_document("1.1.1", title="RDR", content_type="rdr", file_path="/x/rdr.md")
        rdr_t = Tumbler.parse("1.1.1")

        count = generate_pdf_corpus_links(
            reader, writer=writer,
            new_tumblers=[rdr_t], new_content_types={"rdr"},
        )

        assert count == 0
        assert _State.list_requests == []

    def test_rdr_linker_still_fetches_and_links_when_rdr_seed_present(
        self, reader, writer, tmp_path,
    ) -> None:
        """The skip must not become unconditional: a qualifying seed still
        runs the generator's fetches and produces its links."""
        rdr_path = tmp_path / "rdr.md"
        rdr_path.write_text("See `src/nexus/foo.py`.\n")
        _State.add_document("1.1.1", title="foo.py", content_type="code", file_path="src/nexus/foo.py")
        _State.add_document("1.1.2", title="RDR", content_type="rdr", file_path=str(rdr_path))
        rdr_t = Tumbler.parse("1.1.2")

        count = generate_rdr_filepath_links(
            reader, writer=writer,
            new_tumblers=[rdr_t], new_content_types={"rdr"},
        )

        assert count == 1
        assert set(r.get("content_type", "") for r in _State.list_requests) == {"rdr", "code"}

    def test_prose_linker_still_fetches_and_links_when_docs_seed_present(
        self, reader, writer, tmp_path,
    ) -> None:
        """"docs" is a legitimate (if currently unproduced by indexer.py's
        _catalog_hook) source type -- naming it in new_content_types must
        still trigger the fetch + match, proving it wasn't dropped from the
        source-type set by this change."""
        doc_path = tmp_path / "runbook.md"
        doc_path.write_text("See ``src/nexus/foo.py``.\n")
        _State.add_document("1.1.1", title="foo.py", content_type="code", file_path="src/nexus/foo.py")
        _State.add_document("1.1.2", title="Runbook", content_type="docs", file_path=str(doc_path))
        docs_t = Tumbler.parse("1.1.2")

        count = generate_prose_filepath_links(
            reader, writer=writer,
            new_tumblers=[docs_t], new_content_types={"docs"},
        )

        assert count == 1

    def test_pdf_linker_still_fetches_and_links_when_pdf_seed_present(self, reader, writer) -> None:
        _State.add_document(
            "1.1.1", title="P1", content_type="paper",
            head_hash="h1", physical_collection="knowledge__a",
        )
        _State.add_document(
            "1.1.2", title="P2", content_type="paper",
            head_hash="h1", physical_collection="knowledge__b",
        )
        member_t = Tumbler.parse("1.1.2")

        count = generate_pdf_corpus_links(
            reader, writer=writer,
            new_tumblers=[member_t], new_content_types={"paper"},
        )

        assert count == 1
        assert set(r.get("content_type", "") for r in _State.list_requests) == {"pdf", "paper"}

    def test_skip_is_opt_in_omitting_new_content_types_preserves_old_behavior(
        self, reader, writer, tmp_path,
    ) -> None:
        """Backward compatibility: every pre-existing call site (including
        every other test in this file, and the CLI full-scan path) does not
        pass new_content_types at all. That must produce the EXACT
        pre-existing fetch-and-filter behavior, not the new skip -- even
        when new_tumblers alone names only a non-qualifying type."""
        _State.add_document("1.1.1", title="foo.py", content_type="code", file_path="src/nexus/foo.py")
        code_t = Tumbler.parse("1.1.1")

        count = generate_rdr_filepath_links(reader, writer=writer, new_tumblers=[code_t])

        assert count == 0
        # Still fetches -- omitting new_content_types must not activate the skip.
        assert set(r.get("content_type", "") for r in _State.list_requests) == {"rdr", "code"}
