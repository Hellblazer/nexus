# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6lfdi: PG-world (service-mode) coverage for the bib-enrichment
enrich-then-write-back path (``nexus.bib_enricher`` /
``nexus.bib_enricher_openalex`` produce a bib dict; ``commands/enrich.py``'s
``_enrich_apply`` writes it back onto the catalog).

The external bib APIs (Semantic Scholar / OpenAlex) are irrelevant to this
file's purpose — ``_enrich_apply`` takes an already-resolved ``bib_meta``
dict as input, so the enrichment HTTP calls are never invoked here (no
mocking needed; the existing ``tests/test_bib_enricher*.py`` files already
cover ``enrich()`` / ``enrich_by_doi()`` / ``enrich_by_arxiv_id()`` in
isolation with ``Mock(spec=httpx.Response)``). This file drives the CATALOG
side of the write-back with a REAL ``HttpCatalogClient`` reader + a real
``_ServiceCatalogWriter``-wrapped ``HttpCatalogClient`` writer (mirrors
production wiring in ``commands/enrich.py``'s ``_catalog_enrich_hook``) over
a wire-faithful stateful fake server, per this repo's real-client-over-fake-
transport pattern (the ``umvh2``-class recurrence guard).

LIVE BUG FOUND while building this coverage (see
``TestMetaMergeSemanticsDrift`` below): ``HttpCatalogClient.update()`` passes
the ``meta=`` kwarg straight through to ``POST /update`` with NO client-side
merge, and ``CatalogRepository.updateDocument`` (Java) does a bare
``SET metadata = <new jsonb>`` — a full REPLACE, not a JSON merge. Local
``Catalog.update()`` (``catalog_writes.py`` lines ~447-451) explicitly reads
the current entry and merges the caller's ``meta`` dict into the existing
one before writing. Every ``writer.update(tumbler, meta={...})`` call site
in this codebase (``_enrich_apply`` here, plus ``indexer.py``,
``commands/dt.py``, ``commands/catalog_cmds/remediation.py``) is written
against the LOCAL merge contract; in service mode every one of them silently
drops any pre-existing metadata key absent from the new dict. This is a
wire-shape/semantics divergence signature-conformance tests cannot see
(``test_catalog_conformance.py``'s own docstring calls this class out
explicitly as a known blind spot). Pinned below as ``xfail(strict=True)``
per this repo's established RDR-168 divergence-tracking convention rather
than silently working around it.
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
from nexus.catalog.tumbler import Tumbler
from nexus.commands.enrich import _enrich_apply

# ── stateful fake server ──────────────────────────────────────────────────────


class _State:
    """Server-side fixture state (a documents table), reset per test.

    ``/update`` mirrors ``CatalogRepository.updateDocument``'s REAL
    semantics exactly (including the meta-REPLACE behavior described in the
    module docstring) so the drift test below is evidence, not a fixture
    artifact.
    """

    documents: dict[str, dict[str, Any]] = {}
    #: POST /update bodies received, in order — lets tests assert exactly
    #: what wire shape _enrich_apply produced.
    update_bodies: list[dict[str, Any]] = []

    @classmethod
    def reset(cls) -> None:
        cls.documents = {}
        cls.update_bodies = []

    @classmethod
    def add_document(cls, tumbler: str, **fields: Any) -> str:
        base: dict[str, Any] = {
            "tumbler": tumbler,
            "title": tumbler,
            "author": "",
            "year": 0,
            "content_type": "",
            "file_path": "",
            "source_uri": "",
            "corpus": "",
            "physical_collection": "",
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


# Java CatalogRepository.UPDATABLE_DOC_COLUMNS whitelist (CatalogRepository.java
# ~511-516) — mirrored here so the fake 400s exactly like the real service on
# an unrecognized top-level update key.
_UPDATABLE_DOC_COLUMNS = frozenset({
    "title", "author", "year", "content_type", "file_path", "corpus",
    "physical_collection", "chunk_count", "head_hash", "indexed_at",
    "meta", "metadata", "source_mtime", "alias_of", "source_uri",
    "bib_year", "bib_authors", "bib_venue", "bib_citation_count",
    "bib_semantic_scholar_id", "bib_openalex_id", "bib_doi", "bib_enriched_at",
})


class FakeBibCatalogHandler(BaseHTTPRequestHandler):
    """Minimal stateful fake — only the routes ``_enrich_apply`` touches:
    GET ``/list`` (list_by_collection), GET ``/resolve``
    (lookup_doc_id_by_collection_and_path), GET ``/search`` (find);
    POST ``/update``.
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
            collection = params.get("collection")
            if collection:
                docs = [d for d in docs if d.get("physical_collection") == collection]
            self._send_json({"documents": docs, "count": len(docs)})
        elif op == "/resolve":
            file_path = params.get("file_path", "")
            collection = params.get("collection", "")
            matches = [
                d for d in _State.documents.values()
                if d.get("file_path") == file_path
                and (not collection or d.get("physical_collection") == collection)
            ]
            self._send_json({"documents": matches})
        elif op == "/search":
            q = params.get("q", "")
            content_type = params.get("content_type")
            matches = [
                d for d in _State.documents.values()
                if d.get("title") == q
                and (not content_type or d.get("content_type") == content_type)
            ]
            self._send_json({"documents": matches, "count": len(matches)})
        else:
            self._send_json({"error": f"unknown GET op: {op}"}, 404)

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler contract
        path = urlparse(self.path).path
        op = path.removeprefix("/v1/catalog")
        body = self._read_body()

        if op == "/update":
            _State.update_bodies.append(dict(body))
            tumbler = body.get("tumbler", "")
            fields = {k: v for k, v in body.items() if k != "tumbler"}
            # Mirror CatalogRepository.updateDocument's whitelist: an unknown
            # top-level key 400s exactly like the real service.
            unknown = [k for k in fields if k not in _UPDATABLE_DOC_COLUMNS]
            if unknown:
                self._send_json(
                    {"error": f"updateDocument: column not updatable: {unknown[0]!r}"},
                    400,
                )
                return
            doc = _State.documents.get(tumbler)
            if doc is None:
                self._send_json({"updated": 0})
                return
            for key, value in fields.items():
                if key in ("meta", "metadata"):
                    # REAL semantics (CatalogRepository.java ~540-544):
                    # SET metadata = <new jsonb> — a REPLACE, not a merge.
                    doc["metadata"] = value
                else:
                    doc[key] = value
            self._send_json({"updated": 1})
        else:
            self._send_json({"ok": True})


def _start_server() -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), FakeBibCatalogHandler)
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
    # Mirrors production wiring (commands/enrich.py's _catalog_enrich_hook):
    # a SEPARATE HttpCatalogClient instance from the reader, wrapped in
    # _ServiceCatalogWriter.
    client = HttpCatalogClient(base_url=fake_server, tenant="tenant_abc", _token="test_tok")
    w = _ServiceCatalogWriter(client)
    yield w
    w.close()


_S2_BIB_META = {
    "year": 2024,
    "venue": "NeurIPS",
    "authors": "Alice, Bob",
    "citation_count": 42,
    "semantic_scholar_id": "ss123",
}

_OPENALEX_BIB_META = {
    "year": 2023,
    "venue": "ICML",
    "authors": "Carol",
    "citation_count": 7,
    "openalex_id": "W999",
    "doi": "10.1234/foo",
}


class TestEnrichApplyServiceModeSourcePathMatch:
    """nexus-tv22: source_paths is the authoritative match path (unambiguous
    per-document identity, unlike title which drifts across derive_title
    rewrites)."""

    def test_s2_backend_writes_author_year_and_meta(self, reader, writer) -> None:
        _State.add_document(
            "1.1.1", title="Paper One", physical_collection="knowledge__delos",
            file_path="paper1.pdf",
        )
        _enrich_apply(
            reader, writer, "knowledge__delos", "Paper One", ["paper1.pdf"],
            _S2_BIB_META, "s2", Tumbler,
        )
        assert len(_State.update_bodies) == 1
        body = _State.update_bodies[0]
        assert body["tumbler"] == "1.1.1"
        assert body["author"] == "Alice, Bob"
        assert body["year"] == 2024
        assert body["meta"]["venue"] == "NeurIPS"
        assert body["meta"]["citation_count"] == 42
        assert body["meta"]["bib_semantic_scholar_id"] == "ss123"
        # No 400 — every top-level wire key is on CatalogRepository's
        # UPDATABLE_DOC_COLUMNS whitelist.
        stored = _State.documents["1.1.1"]
        assert stored["author"] == "Alice, Bob"
        assert stored["year"] == 2024

    def test_openalex_backend_writes_bib_openalex_id_and_doi(self, reader, writer) -> None:
        _State.add_document(
            "1.1.1", title="Paper Two", physical_collection="knowledge__delos",
            file_path="paper2.pdf",
        )
        _enrich_apply(
            reader, writer, "knowledge__delos", "Paper Two", ["paper2.pdf"],
            _OPENALEX_BIB_META, "openalex", Tumbler,
        )
        body = _State.update_bodies[0]
        assert body["meta"]["bib_openalex_id"] == "W999"
        assert body["meta"]["bib_doi"] == "10.1234/foo"
        # S2 field must NOT appear for the openalex backend.
        assert "bib_semantic_scholar_id" not in body["meta"]

    def test_absolute_and_relative_path_variants_both_match(self, reader, writer) -> None:
        """The lookup tries (sp, sp.lstrip('/')) — an absolute source_path
        must still resolve against a catalog row storing the relative form."""
        _State.add_document(
            "1.1.1", title="Paper Three", physical_collection="knowledge__delos",
            file_path="paper3.pdf",
        )
        _enrich_apply(
            reader, writer, "knowledge__delos", "Paper Three", ["/paper3.pdf"],
            _S2_BIB_META, "s2", Tumbler,
        )
        assert len(_State.update_bodies) == 1
        assert _State.update_bodies[0]["tumbler"] == "1.1.1"

    def test_no_match_is_a_silent_noop(self, reader, writer) -> None:
        """No source_paths match, no collection title match, no FTS hit —
        _enrich_apply must not raise (best-effort catalog hook contract)."""
        _enrich_apply(
            reader, writer, "knowledge__delos", "Nonexistent Paper", ["missing.pdf"],
            _S2_BIB_META, "s2", Tumbler,
        )
        assert _State.update_bodies == []


class TestEnrichApplyServiceModeTitleFallback:
    def test_title_match_within_collection_when_no_source_paths(self, reader, writer) -> None:
        _State.add_document(
            "1.1.1", title="Fallback Paper", physical_collection="knowledge__delos",
            file_path="",
        )
        _enrich_apply(
            reader, writer, "knowledge__delos", "Fallback Paper", [],
            _S2_BIB_META, "s2", Tumbler,
        )
        assert len(_State.update_bodies) == 1
        assert _State.update_bodies[0]["tumbler"] == "1.1.1"

    def test_last_resort_fts_search_across_catalog(self, reader, writer) -> None:
        """No collection_name / no source_paths match: falls through to
        reader.find(title, content_type='paper')."""
        _State.add_document(
            "1.1.1", title="Global Paper", content_type="paper",
        )
        _enrich_apply(
            reader, writer, "", "Global Paper", [],
            _S2_BIB_META, "s2", Tumbler,
        )
        assert len(_State.update_bodies) == 1
        assert _State.update_bodies[0]["tumbler"] == "1.1.1"


class TestMetaMergeSemanticsDrift:
    """KNOWN DRIFT (nexus-6lfdi live finding) — see module docstring.

    ``HttpCatalogClient.update()`` (http_catalog_client.py) forwards the
    ``meta=`` kwarg to ``POST /update`` verbatim; ``CatalogRepository
    .updateDocument`` (CatalogRepository.java ~540-544) does
    ``SET metadata = <new jsonb>`` — a full REPLACE. Local
    ``Catalog.update()`` (``catalog_writes.py`` ~447-451) explicitly merges
    the caller's ``meta`` dict into the EXISTING entry's meta before
    writing. Every bib-enrichment write-back call
    (``_enrich_apply`` -> ``writer.update(tumbler, meta=meta_update)``)
    therefore silently drops any pre-existing metadata key not present in
    the new ``meta_update`` dict when running in service mode — data loss
    with no error, the exact silent class RDR-168 exists to catch, but
    orthogonal to signature conformance (both signatures match; this is a
    WIRE-SEMANTICS divergence).

    Filed as bead nexus-ke45f (fix belongs in HttpCatalogClient.update():
    read-merge-write for the meta/metadata kwarg, mirroring local
    semantics). This test pins the reproduction; flip to a plain
    (non-xfail) assertion when the fix lands and remove the xfail mark.
    """

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "KNOWN DRIFT (nexus-6lfdi / bead nexus-ke45f): HttpCatalogClient.update() "
            "does not merge the meta kwarg before POSTing (no read-before-write), and "
            "the service's updateDocument does a bare metadata REPLACE, not a JSON "
            "merge. Any writer.update(tumbler, meta={...}) in service mode — including "
            "every bib-enrichment write-back — clobbers pre-existing metadata keys. "
            "Local Catalog.update() merges; the client does not. XPASS here means the "
            "client gained read-merge-write semantics — remove this xfail."
        ),
    )
    def test_bib_write_back_preserves_preexisting_metadata_in_service_mode(
        self, reader, writer,
    ) -> None:
        _State.add_document(
            "1.1.1", title="Existing Meta Paper",
            physical_collection="knowledge__delos", file_path="existing.pdf",
            metadata={"content_hash": "abc123", "custom_field": "keep-me"},
        )
        _enrich_apply(
            reader, writer, "knowledge__delos", "Existing Meta Paper", ["existing.pdf"],
            _S2_BIB_META, "s2", Tumbler,
        )
        stored_meta = _State.documents["1.1.1"]["metadata"]
        # This is what the LOCAL (merge-semantics) contract guarantees and what
        # every writer.update(..., meta=...) call site in this codebase assumes.
        # In service mode it currently does NOT hold — the fields below are gone.
        assert stored_meta.get("content_hash") == "abc123"
        assert stored_meta.get("custom_field") == "keep-me"
        # The new bib fields are of course present (that part works correctly).
        assert stored_meta.get("venue") == "NeurIPS"
