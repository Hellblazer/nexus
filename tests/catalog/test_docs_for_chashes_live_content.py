# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-h8rf6.12: CONTENT (not just shape) regression for HttpCatalogClient
.docs_for_chashes against a live-shaped request.

Incident: nexus-h8rf6.3 fixed the RETURN shape (``list`` -> ``dict``) but the
2026-07-03 candidate-shakeout leg found ``nx index repo`` in service mode
STILL rebuilding an empty staleness cache immediately after writing 40 files
in the same run (``code: 0 docs, docs: 0 docs (0.0s)``) -- confirmed on a
second run that only 2 files had actually changed, yet all 40 were
re-processed.

Root cause (this bead): ``HttpCatalogClient.docs_for_chashes`` computes a
32-char-prefix map (``prefix_to_inputs``) for the SECOND round-trip's
intersection logic, but the FIRST round-trip -- the POST to
``/manifest/docs_for_chashes`` -- sent the RAW, un-normalized ``chashes``
argument. Real chunk metadata carries the FULL 64-char
``hashlib.sha256(...).hexdigest()`` (see code_indexer.py:396,
doc_indexer.py:1048/1136, prose_indexer.py:102/166), while
``CatalogRepository.docsForChashes`` (service/src/main/java/dev/nexus/
service/db/CatalogRepository.java:1130-1136) does an EXACT-match
``F_CHK_CHASH.in(chashes)`` against ``catalog_document_chunks.chash`` -- a
32-char RDR-108 D1 natural-id column with NO server-side normalization
(unlike local ``Catalog.docs_for_chashes``, which normalizes both the
stored column and the input via SQL ``substr(chash,1,32)`` --
catalog_writes.py:1145-1147). Sending 64-char values therefore matched
ZERO rows on every real call -- not an error, a legitimately empty result --
so ``by_doc_id`` in ``build_staleness_cache`` stayed empty for every
Phase-3-era chunk (no ``doc_id``/``source_path`` in metadata, chash-only
resolution).

Why the h8rf6.3 conformance test (test_docs_for_chashes_shape_conformance.py)
did NOT catch this: its fixtures used the chash literal ``"abc123"`` (6
chars) end to end -- ``"abc123"[:32] == "abc123"``, so client-side
truncation is a structural no-op for a fixture shorter than the truncation
boundary, and the shared ``FakeCatalogHandler`` fake server does not
inspect the POST body at all for ``/manifest/docs_for_chashes`` (always
returns a canned ``{"tumblers": ["1.1.1"]}``) -- so a wrong-length request
payload was invisible to every existing test. This module's fake server
DOES validate the request body with the same exact-match semantics as the
real Java SQL, and uses a REAL 64-char sha256 hexdigest as input, so it
fails pre-fix and passes post-fix.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.indexer_utils import build_staleness_cache

# A real 64-char sha256 hexdigest, matching exactly what
# code_indexer.py / doc_indexer.py / prose_indexer.py write into
# chunk metadata's ``chunk_text_hash`` field at index time.
_FULL_CHASH = hashlib.sha256(b"def process_payment(order):\n    return order.total\n").hexdigest()
assert len(_FULL_CHASH) == 64
# The RDR-108 D1 natural-id form actually stored server-side.
_STORED_PREFIX = _FULL_CHASH[:32]
_TUMBLER = "1.1.1"


class _ExactMatchCatalogHandler(BaseHTTPRequestHandler):
    """Minimal fake catalog server mirroring the REAL Java SQL semantics.

    Unlike ``tests/catalog/test_http_catalog_client.py::FakeCatalogHandler``
    (which returns a canned response regardless of the request body), this
    handler actually inspects the posted ``chashes`` list and only
    "matches" an exact string equal to ``_STORED_PREFIX`` -- exactly what
    ``CatalogRepository.docsForChashes``'s ``F_CHK_CHASH.in(chashes)`` does
    against the 32-char stored column. A request carrying the un-truncated
    64-char ``_FULL_CHASH`` will NOT match here, same as production.
    """

    last_docs_for_chashes_body: dict[str, Any] = {}

    def log_message(self, *args: Any) -> None:
        pass

    def _send_json(self, body: Any, code: int = 200) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        op = urlparse(self.path).path.removeprefix("/v1/catalog")
        body = self._read_body()

        if op == "/manifest/docs_for_chashes":
            _ExactMatchCatalogHandler.last_docs_for_chashes_body = body
            requested = body.get("chashes", [])
            # EXACT string match only — mirrors F_CHK_CHASH.in(chashes)
            # against the 32-char stored column. No substr/normalization.
            if _STORED_PREFIX in requested:
                self._send_json({"tumblers": [_TUMBLER]})
            else:
                self._send_json({"tumblers": []})
        elif op == "/manifest/get_many":
            doc_ids = body.get("doc_ids", [])
            manifests = {}
            if _TUMBLER in doc_ids:
                manifests[_TUMBLER] = [
                    {"position": 0, "chash": _STORED_PREFIX, "line_start": 1, "line_end": 3}
                ]
            self._send_json({"manifests": manifests})
        else:
            self._send_json({"error": f"unhandled op in test fake: {op}"}, 404)

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        self._send_json({"error": "unhandled GET in test fake"}, 404)


@pytest.fixture(scope="module")
def exact_match_server():
    server = HTTPServer(("127.0.0.1", 0), _ExactMatchCatalogHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.fixture
def http_client(exact_match_server: str):
    with HttpCatalogClient(
        base_url=exact_match_server, tenant="tenant_h8rf6_12", _token="test_tok",
    ) as c:
        yield c


class TestDocsForChashesSendsNormalizedRequest:
    """Proves the FIRST round-trip's wire payload, not just the return shape."""

    def test_full_64char_chash_resolves_against_exact_match_server(
        self, http_client: HttpCatalogClient,
    ) -> None:
        """The regression itself: passing a real 64-char chash must still
        resolve, because the client must normalize the OUTGOING request to
        the 32-char prefix before the server's exact-match filter sees it.

        Pre-fix (raw ``chashes`` sent as-is): the exact-match fake server
        never sees ``_STORED_PREFIX`` in the request, returns empty
        tumblers, and this assertion fails with ``{}``.
        """
        result = http_client.docs_for_chashes([_FULL_CHASH])
        assert result == {_FULL_CHASH: [_TUMBLER]}

    def test_request_payload_is_32char_normalized(
        self, http_client: HttpCatalogClient,
    ) -> None:
        """Direct wire-content assertion: what actually left the client."""
        http_client.docs_for_chashes([_FULL_CHASH])
        sent = _ExactMatchCatalogHandler.last_docs_for_chashes_body.get("chashes", [])
        assert sent == [_STORED_PREFIX]
        assert all(len(c) == 32 for c in sent)


class TestBuildStalenessCacheLiveContent:
    """LIVE-shaped content proof (not fixture shape): build_staleness_cache
    against a REAL HttpCatalogClient talking to a server that enforces
    exact-match semantics, fed a genuine 64-char sha256 chunk_text_hash —
    exactly the value real indexers write, unlike the 6-char "abc123"
    literal in the h8rf6.3 conformance fixtures that could not have
    exposed a truncation bug.
    """

    def test_nonzero_docs_after_index_like_write(
        self, http_client: HttpCatalogClient,
    ) -> None:
        col = MagicMock()
        col.get.return_value = {
            "ids": ["chunk-0"],
            "metadatas": [{
                "chunk_text_hash": _FULL_CHASH,
                "content_hash": "content-hash-a",
                "embedding_model": "voyage-code-3",
            }],
        }

        import nexus.catalog.factory as _factory_mod  # noqa: PLC0415 — mirrors indexer_utils.py's own deferred import of this seam
        with patch.object(_factory_mod, "make_catalog_reader", return_value=http_client):
            cache = build_staleness_cache(col)

        # Exact assertion, not an inequality: the "0 docs" bug returns
        # ``{}`` here; content is real (non-toy) proof this round-trips.
        assert cache.by_doc_id == {_TUMBLER: ("content-hash-a", "voyage-code-3")}
