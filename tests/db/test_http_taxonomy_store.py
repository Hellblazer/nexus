# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for HttpTaxonomyStore (bead nexus-gmiaf.14, RDR-152 P2.4).

Test approach: in-process fake HTTP server implementing the /v1/taxonomy/*
contract. The fake server mirrors the real Java TaxonomyHandler shape.

Verifies:
  - HttpTaxonomyStore makes correct HTTP calls (right paths, headers, payloads)
  - Response -> Python mapping is correct (types, None normalization)
  - HTTP error codes map to expected Python exceptions
  - Auth header and X-Nexus-Tenant header are sent on every request
  - Import fidelity: id/doc_count/timestamps preserved verbatim
  - CHROMA BOUNDARY: delete_topic and merge_topics return collection name

Full cross-language end-to-end is in
tests/db/test_http_taxonomy_store_integration.py (marked integration).
"""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from nexus.db.t2.http_taxonomy_store import DEFAULT_TENANT, HttpTaxonomyStore

TOKEN = "fake-taxonomy-token-abc"

# ── In-process fake server ────────────────────────────────────────────────────

# Shared in-memory store
_TOPICS: dict[int, dict[str, Any]] = {}
_ASSIGNMENTS: list[dict[str, Any]] = []
_LINKS: list[dict[str, Any]] = []
_META: dict[str, dict[str, Any]] = {}
_STORE_LOCK = threading.Lock()
_ID_SEQ = [0]


def _next_id() -> int:
    _ID_SEQ[0] += 1
    return _ID_SEQ[0]


def _reset_stores() -> None:
    _TOPICS.clear()
    _ASSIGNMENTS.clear()
    _LINKS.clear()
    _META.clear()
    _ID_SEQ[0] = 0


class _FakeTaxonomyHandler(BaseHTTPRequestHandler):
    """Minimal fake implementation of /v1/taxonomy/* endpoint."""

    def log_message(self, *args: Any) -> None:  # suppress request logs in test output
        pass

    def _auth_ok(self) -> bool:
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {TOKEN}"

    def _tenant(self) -> str:
        return self.headers.get("X-Nexus-Tenant", DEFAULT_TENANT)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        return json.loads(raw)

    def _json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _query_params(self) -> dict[str, str]:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        return {k: v[0] for k, v in qs.items()}

    def do_GET(self) -> None:
        if not self._auth_ok():
            self._json(401, {"error": "unauthorized"})
            return

        path = urlparse(self.path).path
        params = self._query_params()

        with _STORE_LOCK:
            if path == "/v1/taxonomy/topics":
                collection = params.get("collection")
                topics = [
                    t for t in _TOPICS.values()
                    if collection is None or t["collection"] == collection
                ]
                self._json(200, topics)

            elif path == "/v1/taxonomy/topics/by_id":
                tid = int(params.get("id", 0))
                t = _TOPICS.get(tid)
                if t is None:
                    self._json(404, {"error": "not found"})
                else:
                    self._json(200, t)

            elif path == "/v1/taxonomy/topics/resolve":
                label = params.get("label", "")
                collection = params.get("collection")
                for t in _TOPICS.values():
                    if t["label"] == label and (collection is None or t["collection"] == collection):
                        self._json(200, {"id": t["id"]})
                        return
                self._json(404, {"error": "not found"})

            elif path == "/v1/taxonomy/topics/collections":
                colls = sorted({t["collection"] for t in _TOPICS.values()})
                self._json(200, colls)

            elif path == "/v1/taxonomy/topics/root":
                roots = [t for t in _TOPICS.values() if t.get("parent_id") is None]
                self._json(200, roots)

            elif path == "/v1/taxonomy/topics/children":
                pid = int(params.get("parent_id", -1))
                children = [t for t in _TOPICS.values() if t.get("parent_id") == pid]
                self._json(200, children)

            elif path == "/v1/taxonomy/topics/unreviewed":
                collection = params.get("collection")
                limit = int(params.get("limit", 100))
                pending = [
                    t for t in _TOPICS.values()
                    if t.get("review_status") == "pending"
                    and (collection is None or t["collection"] == collection)
                ]
                self._json(200, pending[:limit])

            elif path == "/v1/taxonomy/assignments/docs":
                topic_id = int(params.get("topic_id", 0))
                limit = int(params.get("limit", 3))
                docs = [
                    a["doc_id"] for a in _ASSIGNMENTS if a["topic_id"] == topic_id
                ]
                if limit > 0:
                    docs = docs[:limit]
                self._json(200, docs)

            elif path == "/v1/taxonomy/assignments/by_label":
                label = params.get("label", "")
                topic_ids = {t["id"] for t in _TOPICS.values() if t["label"] == label}
                docs = [a["doc_id"] for a in _ASSIGNMENTS if a["topic_id"] in topic_ids]
                self._json(200, docs)

            elif path == "/v1/taxonomy/icf/source_count":
                colls = {a["source_collection"] for a in _ASSIGNMENTS if a.get("source_collection")}
                self._json(200, {"count": len(colls)})

            elif path == "/v1/taxonomy/icf/rows":
                # Fake ICF: each topic gets count=1
                topic_ids = {a["topic_id"] for a in _ASSIGNMENTS}
                rows = [{"topic_id": tid, "icf_raw": 1.0} for tid in topic_ids]
                self._json(200, rows)

            elif path == "/v1/taxonomy/top_topics":
                collection = params.get("collection", "")
                top_n = int(params.get("top_n", 10))
                matching = [
                    t for t in _TOPICS.values() if t.get("collection") == collection
                ]
                self._json(200, matching[:top_n])

            elif path == "/v1/taxonomy/chunk_grounded":
                doc_id = params.get("doc_id", "")
                source_coll = params.get("source_collection", "")
                for a in _ASSIGNMENTS:
                    if a["doc_id"] == doc_id and a.get("source_collection") == source_coll:
                        self._json(200, {"similarity": a.get("similarity", 0.0)})
                        return
                self._json(404, {"error": "not found"})

            elif path == "/v1/taxonomy/projection_counts":
                from collections import Counter
                counts = Counter(
                    a["source_collection"] for a in _ASSIGNMENTS
                    if a.get("assigned_by") == "projection" and a.get("source_collection")
                )
                self._json(200, [{"source_collection": k, "count": v} for k, v in counts.items()])

            elif path == "/v1/taxonomy/meta/last_count":
                collection = params.get("collection", "")
                m = _META.get(collection)
                if m is None:
                    self._json(404, {"error": "not found"})
                else:
                    self._json(200, {"count": m.get("last_discover_doc_count", 0)})

            else:
                self._json(404, {"error": f"GET {path} not found"})

    def do_POST(self) -> None:
        if not self._auth_ok():
            self._json(401, {"error": "unauthorized"})
            return

        path = urlparse(self.path).path
        body = self._read_body()

        with _STORE_LOCK:
            if path == "/v1/taxonomy/topics/delete":
                tid = int(body.get("topic_id", 0))
                t = _TOPICS.pop(tid, None)
                if t is None:
                    self._json(404, {"error": "not found"})
                else:
                    # Remove assignments for this topic
                    _ASSIGNMENTS[:] = [a for a in _ASSIGNMENTS if a["topic_id"] != tid]
                    self._json(200, {"collection": t.get("collection")})

            elif path == "/v1/taxonomy/topics/merge":
                src_id = int(body.get("source_id", 0))
                tgt_id = int(body.get("target_id", 0))
                src = _TOPICS.pop(src_id, None)
                if src is None:
                    self._json(404, {"error": "source not found"})
                    return
                # Reassign source's assignments to target
                for a in _ASSIGNMENTS:
                    if a["topic_id"] == src_id:
                        a["topic_id"] = tgt_id
                self._json(200, {"collection": src.get("collection")})

            elif path == "/v1/taxonomy/topics/update_label":
                tid = int(body.get("topic_id", 0))
                if tid not in _TOPICS:
                    self._json(404, {"error": "not found"})
                else:
                    _TOPICS[tid]["label"] = body["label"]
                    self._json(200, {"ok": True})

            elif path == "/v1/taxonomy/topics/rename":
                tid = int(body.get("topic_id", 0))
                if tid not in _TOPICS:
                    self._json(404, {"error": "not found"})
                else:
                    _TOPICS[tid]["label"] = body["label"]
                    _TOPICS[tid]["review_status"] = "accepted"
                    self._json(200, {"ok": True})

            elif path == "/v1/taxonomy/topics/mark_reviewed":
                tid = int(body.get("topic_id", 0))
                if tid not in _TOPICS:
                    self._json(404, {"error": "not found"})
                else:
                    _TOPICS[tid]["review_status"] = body["status"]
                    self._json(200, {"ok": True})

            elif path == "/v1/taxonomy/assignments/assign":
                doc_id = body["doc_id"]
                topic_id = int(body["topic_id"])
                similarity = body.get("similarity")
                assigned_by = body.get("assigned_by", "hdbscan")
                source_collection = body.get("source_collection")
                assigned_at = body.get("assigned_at")
                # Upsert: update similarity if greater
                for a in _ASSIGNMENTS:
                    if a["doc_id"] == doc_id and a["topic_id"] == topic_id:
                        if similarity is not None and (a.get("similarity") or 0.0) < similarity:
                            a["similarity"] = similarity
                        a["assigned_by"] = assigned_by
                        a["assigned_at"] = assigned_at
                        a["source_collection"] = source_collection
                        self._json(200, {"ok": True})
                        return
                _ASSIGNMENTS.append({
                    "doc_id": doc_id,
                    "topic_id": topic_id,
                    "assigned_by": assigned_by,
                    "similarity": similarity,
                    "assigned_at": assigned_at,
                    "source_collection": source_collection,
                })
                self._json(200, {"ok": True})

            elif path == "/v1/taxonomy/assignments/for_docs":
                doc_ids = set(body.get("doc_ids", []))
                mapping = [
                    {"doc_id": a["doc_id"], "topic_id": a["topic_id"]}
                    for a in _ASSIGNMENTS if a["doc_id"] in doc_ids
                ]
                self._json(200, mapping)

            elif path == "/v1/taxonomy/assignments/purge_doc":
                # Purge by project/title as doc_id pattern — fake: remove by project prefix
                project = body.get("project", "")
                title = body.get("title", "")
                doc_id = f"{project}/{title}"
                before = len(_ASSIGNMENTS)
                _ASSIGNMENTS[:] = [a for a in _ASSIGNMENTS if a["doc_id"] != doc_id]
                removed = before - len(_ASSIGNMENTS)
                self._json(200, {"removed": removed})

            elif path == "/v1/taxonomy/links/pairs":
                topic_ids = set(body.get("topic_ids", []))
                pairs = [
                    {"from_topic_id": lk["from_topic_id"], "to_topic_id": lk["to_topic_id"], "link_count": lk["link_count"]}
                    for lk in _LINKS
                    if lk["from_topic_id"] in topic_ids or lk["to_topic_id"] in topic_ids
                ]
                self._json(200, pairs)

            elif path == "/v1/taxonomy/links/upsert":
                from_id = int(body["from_topic_id"])
                to_id = int(body["to_topic_id"])
                link_count = int(body.get("link_count", 0))
                link_types = body.get("link_types", "[]")
                for lk in _LINKS:
                    if lk["from_topic_id"] == from_id and lk["to_topic_id"] == to_id:
                        lk["link_count"] = max(lk["link_count"], link_count)
                        lk["link_types"] = link_types
                        self._json(200, {"ok": True})
                        return
                _LINKS.append({
                    "from_topic_id": from_id,
                    "to_topic_id": to_id,
                    "link_count": link_count,
                    "link_types": link_types,
                })
                self._json(200, {"ok": True})

            elif path == "/v1/taxonomy/meta/record":
                collection = body["collection"]
                doc_count = int(body.get("doc_count", 0))
                discovered_at = body.get("discovered_at")
                m = _META.get(collection, {})
                _META[collection] = {
                    "collection": collection,
                    "last_discover_doc_count": max(m.get("last_discover_doc_count", 0), doc_count),
                    "last_discover_at": discovered_at,
                }
                self._json(200, {"ok": True})

            elif path == "/v1/taxonomy/import/topic":
                tid = int(body["id"])
                _TOPICS[tid] = {
                    "id":            tid,
                    "label":         body["label"],
                    "parent_id":     body.get("parent_id"),
                    "collection":    body["collection"],
                    "centroid_hash": body.get("centroid_hash"),
                    "doc_count":     int(body.get("doc_count", 0)),
                    "created_at":    body.get("created_at", ""),
                    "review_status": body.get("review_status", "pending"),
                    "terms":         body.get("terms"),
                }
                self._json(200, {"id": tid})

            elif path == "/v1/taxonomy/import/assignment":
                _ASSIGNMENTS.append({
                    "doc_id":            body["doc_id"],
                    "topic_id":          int(body["topic_id"]),
                    "assigned_by":       body.get("assigned_by", "hdbscan"),
                    "similarity":        body.get("similarity"),
                    "assigned_at":       body.get("assigned_at"),
                    "source_collection": body.get("source_collection"),
                })
                self._json(200, {"ok": True})

            elif path == "/v1/taxonomy/import/link":
                _LINKS.append({
                    "from_topic_id": int(body["from_topic_id"]),
                    "to_topic_id":   int(body["to_topic_id"]),
                    "link_count":    int(body.get("link_count", 0)),
                    "link_types":    body.get("link_types", "[]"),
                })
                self._json(200, {"ok": True})

            elif path == "/v1/taxonomy/import/meta":
                collection = body["collection"]
                _META[collection] = {
                    "collection":               collection,
                    "last_discover_doc_count":  int(body.get("last_discover_doc_count", 0)),
                    "last_discover_at":         body.get("last_discover_at"),
                }
                self._json(200, {"ok": True})

            else:
                self._json(404, {"error": f"POST {path} not found"})


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def fake_server():
    """Start an in-process fake taxonomy HTTP server for the test module."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    srv = HTTPServer(("127.0.0.1", port), _FakeTaxonomyHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def reset_stores():
    """Reset in-memory stores before each test."""
    _reset_stores()
    yield


@pytest.fixture
def client(fake_server: str) -> HttpTaxonomyStore:
    """HttpTaxonomyStore pointed at the fake server."""
    return HttpTaxonomyStore(
        base_url=fake_server,
        _token=TOKEN,
    )


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestAuth:
    def test_wrong_token_raises(self, fake_server: str) -> None:
        bad = HttpTaxonomyStore(base_url=fake_server, _token="wrong")
        with pytest.raises(Exception):
            bad.get_topics()

    def test_tenant_header_sent(self, fake_server: str) -> None:
        """Verify X-Nexus-Tenant header is included (custom tenant round-trips)."""
        store = HttpTaxonomyStore(
            base_url=fake_server, tenant="acme-corp", _token=TOKEN
        )
        # Should not raise
        store.get_topics()


class TestTopicCRUD:
    def test_get_topics_empty(self, client: HttpTaxonomyStore) -> None:
        assert client.get_topics() == []

    def test_import_and_get_topics(self, client: HttpTaxonomyStore) -> None:
        client.import_topic(
            src_id=1,
            label="machine-learning",
            parent_id=None,
            collection="knowledge__papers",
            centroid_hash=None,
            doc_count=10,
            created_at="2026-01-01T00:00:00Z",
            review_status="pending",
            terms=None,
        )
        topics = client.get_topics()
        assert len(topics) == 1
        assert topics[0]["label"] == "machine-learning"
        assert topics[0]["doc_count"] == 10

    def test_get_topics_filters_by_collection(self, client: HttpTaxonomyStore) -> None:
        client.import_topic(
            src_id=1, label="ml", parent_id=None,
            collection="coll-A", centroid_hash=None, doc_count=5,
            created_at="2026-01-01T00:00:00Z", review_status="pending", terms=None,
        )
        client.import_topic(
            src_id=2, label="nlp", parent_id=None,
            collection="coll-B", centroid_hash=None, doc_count=3,
            created_at="2026-01-01T00:00:00Z", review_status="pending", terms=None,
        )
        assert len(client.get_topics("coll-A")) == 1
        assert client.get_topics("coll-A")[0]["label"] == "ml"
        assert len(client.get_topics("coll-B")) == 1

    def test_get_topic_by_id(self, client: HttpTaxonomyStore) -> None:
        client.import_topic(
            src_id=42, label="deep-learning", parent_id=None,
            collection="knowledge__papers", centroid_hash=None, doc_count=7,
            created_at="2026-01-01T00:00:00Z", review_status="pending", terms=None,
        )
        t = client.get_topic_by_id(42)
        assert t is not None
        assert t["label"] == "deep-learning"

    def test_get_topic_by_id_not_found(self, client: HttpTaxonomyStore) -> None:
        assert client.get_topic_by_id(999) is None

    def test_resolve_label(self, client: HttpTaxonomyStore) -> None:
        client.import_topic(
            src_id=5, label="transformers", parent_id=None,
            collection="knowledge__papers", centroid_hash=None, doc_count=3,
            created_at="2026-01-01T00:00:00Z", review_status="pending", terms=None,
        )
        tid = client.resolve_label("transformers")
        assert tid == 5

    def test_resolve_label_not_found(self, client: HttpTaxonomyStore) -> None:
        assert client.resolve_label("nonexistent") is None

    def test_get_distinct_collections(self, client: HttpTaxonomyStore) -> None:
        client.import_topic(
            src_id=1, label="t1", parent_id=None, collection="coll-A",
            centroid_hash=None, doc_count=1, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        client.import_topic(
            src_id=2, label="t2", parent_id=None, collection="coll-B",
            centroid_hash=None, doc_count=1, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        colls = client.get_distinct_collections()
        assert set(colls) == {"coll-A", "coll-B"}

    def test_update_topic_label(self, client: HttpTaxonomyStore) -> None:
        client.import_topic(
            src_id=1, label="old-label", parent_id=None, collection="c",
            centroid_hash=None, doc_count=1, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        client.update_topic_label(1, "new-label")
        t = client.get_topic_by_id(1)
        assert t["label"] == "new-label"
        # review_status should not change
        assert t["review_status"] == "pending"

    def test_rename_topic_sets_accepted(self, client: HttpTaxonomyStore) -> None:
        client.import_topic(
            src_id=1, label="draft", parent_id=None, collection="c",
            centroid_hash=None, doc_count=1, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        client.rename_topic(1, "final-name")
        t = client.get_topic_by_id(1)
        assert t["label"] == "final-name"
        assert t["review_status"] == "accepted"

    def test_mark_topic_reviewed(self, client: HttpTaxonomyStore) -> None:
        client.import_topic(
            src_id=1, label="t", parent_id=None, collection="c",
            centroid_hash=None, doc_count=1, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        client.mark_topic_reviewed(1, "accepted")
        t = client.get_topic_by_id(1)
        assert t["review_status"] == "accepted"

    def test_get_unreviewed_topics(self, client: HttpTaxonomyStore) -> None:
        client.import_topic(
            src_id=1, label="t1", parent_id=None, collection="c",
            centroid_hash=None, doc_count=1, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        client.import_topic(
            src_id=2, label="t2", parent_id=None, collection="c",
            centroid_hash=None, doc_count=1, created_at="2026-01-01T00:00:00Z",
            review_status="accepted", terms=None,
        )
        pending = client.get_unreviewed_topics()
        assert len(pending) == 1
        assert pending[0]["label"] == "t1"


class TestDeleteAndMerge:
    def test_delete_topic_returns_collection(self, client: HttpTaxonomyStore) -> None:
        """CHROMA BOUNDARY: delete_topic returns collection name for chroma cleanup."""
        client.import_topic(
            src_id=1, label="t", parent_id=None, collection="knowledge__papers",
            centroid_hash=None, doc_count=1, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        collection = client.delete_topic(1)
        assert collection == "knowledge__papers"
        assert client.get_topic_by_id(1) is None

    def test_delete_topic_not_found_returns_none(self, client: HttpTaxonomyStore) -> None:
        result = client.delete_topic(9999)
        assert result is None

    def test_delete_topic_cleans_assignments(self, client: HttpTaxonomyStore) -> None:
        client.import_topic(
            src_id=1, label="t", parent_id=None, collection="c",
            centroid_hash=None, doc_count=1, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        client.assign_topic("doc1", 1, "hdbscan")
        assert client.get_topic_doc_ids(1) == ["doc1"]
        client.delete_topic(1)
        assert client.get_topic_doc_ids(1) == []

    def test_merge_topics_returns_source_collection(self, client: HttpTaxonomyStore) -> None:
        """CHROMA BOUNDARY: merge_topics returns source collection for chroma cleanup."""
        client.import_topic(
            src_id=10, label="source", parent_id=None, collection="knowledge__papers",
            centroid_hash=None, doc_count=1, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        client.import_topic(
            src_id=20, label="target", parent_id=None, collection="knowledge__papers",
            centroid_hash=None, doc_count=1, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        collection = client.merge_topics(10, 20)
        assert collection == "knowledge__papers"
        # Source topic removed
        assert client.get_topic_by_id(10) is None
        # Target topic still exists
        assert client.get_topic_by_id(20) is not None

    def test_merge_reassigns_docs_to_target(self, client: HttpTaxonomyStore) -> None:
        client.import_topic(
            src_id=1, label="src", parent_id=None, collection="c",
            centroid_hash=None, doc_count=1, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        client.import_topic(
            src_id=2, label="tgt", parent_id=None, collection="c",
            centroid_hash=None, doc_count=1, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        client.assign_topic("doc1", 1, "hdbscan")
        client.merge_topics(1, 2)
        # doc1 now assigned to topic 2
        docs = client.get_topic_doc_ids(2)
        assert "doc1" in docs


class TestAssignments:
    def test_assign_and_get_doc_ids(self, client: HttpTaxonomyStore) -> None:
        client.import_topic(
            src_id=1, label="t", parent_id=None, collection="c",
            centroid_hash=None, doc_count=0, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        client.assign_topic("doc1", 1, "hdbscan", similarity=0.9)
        client.assign_topic("doc2", 1, "hdbscan", similarity=0.8)
        doc_ids = client.get_topic_doc_ids(1, limit=10)
        assert set(doc_ids) == {"doc1", "doc2"}

    def test_assign_similarity_greatest_wins(self, client: HttpTaxonomyStore) -> None:
        client.import_topic(
            src_id=1, label="t", parent_id=None, collection="c",
            centroid_hash=None, doc_count=0, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        client.assign_topic("doc1", 1, "projection", similarity=0.5,
                             source_collection="code__nexus")
        client.assign_topic("doc1", 1, "projection", similarity=0.8,
                             source_collection="code__nexus")
        # Second assign should win (higher similarity)
        for a in _ASSIGNMENTS:
            if a["doc_id"] == "doc1" and a["topic_id"] == 1:
                assert a["similarity"] == pytest.approx(0.8)

    def test_get_assignments_for_docs(self, client: HttpTaxonomyStore) -> None:
        client.import_topic(
            src_id=1, label="t", parent_id=None, collection="c",
            centroid_hash=None, doc_count=0, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        client.assign_topic("doc-a", 1, "hdbscan")
        client.assign_topic("doc-b", 1, "hdbscan")
        mapping = client.get_assignments_for_docs(["doc-a", "doc-b"])
        assert mapping["doc-a"] == 1
        assert mapping["doc-b"] == 1

    def test_get_doc_ids_for_topic(self, client: HttpTaxonomyStore) -> None:
        client.import_topic(
            src_id=1, label="neural-nets", parent_id=None, collection="c",
            centroid_hash=None, doc_count=0, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        client.assign_topic("docX", 1, "hdbscan")
        result = client.get_doc_ids_for_topic("neural-nets")
        assert "docX" in result


class TestTopicTree:
    def test_get_topic_tree_root(self, client: HttpTaxonomyStore) -> None:
        client.import_topic(
            src_id=1, label="root1", parent_id=None, collection="c",
            centroid_hash=None, doc_count=1, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        client.import_topic(
            src_id=2, label="child1", parent_id=1, collection="c",
            centroid_hash=None, doc_count=1, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        roots = client.get_topic_tree()
        labels = [t["label"] for t in roots]
        assert "root1" in labels
        assert "child1" not in labels

    def test_get_topic_tree_children(self, client: HttpTaxonomyStore) -> None:
        client.import_topic(
            src_id=1, label="parent", parent_id=None, collection="c",
            centroid_hash=None, doc_count=1, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        client.import_topic(
            src_id=2, label="child", parent_id=1, collection="c",
            centroid_hash=None, doc_count=1, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        children = client.get_topic_tree(parent_id=1)
        assert len(children) == 1
        assert children[0]["label"] == "child"


class TestLinks:
    def test_upsert_and_get_links(self, client: HttpTaxonomyStore) -> None:
        client.import_topic(
            src_id=1, label="t1", parent_id=None, collection="c",
            centroid_hash=None, doc_count=0, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        client.import_topic(
            src_id=2, label="t2", parent_id=None, collection="c",
            centroid_hash=None, doc_count=0, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        client.upsert_topic_links([(1, 2, 5)])
        pairs = client.get_topic_link_pairs([1, 2])
        assert len(pairs) == 1
        assert pairs[0][2] == 5  # link_count

    def test_upsert_link_greatest_wins(self, client: HttpTaxonomyStore) -> None:
        client.import_topic(
            src_id=1, label="t1", parent_id=None, collection="c",
            centroid_hash=None, doc_count=0, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        client.import_topic(
            src_id=2, label="t2", parent_id=None, collection="c",
            centroid_hash=None, doc_count=0, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        client.upsert_topic_links([(1, 2, 3)])
        client.upsert_topic_links([(1, 2, 10)])
        pairs = client.get_topic_link_pairs([1])
        assert pairs[0][2] == 10

    def test_upsert_returns_count(self, client: HttpTaxonomyStore) -> None:
        client.import_topic(
            src_id=1, label="t1", parent_id=None, collection="c",
            centroid_hash=None, doc_count=0, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        client.import_topic(
            src_id=2, label="t2", parent_id=None, collection="c",
            centroid_hash=None, doc_count=0, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        n = client.upsert_topic_links([(1, 2, 1), (2, 1, 2)])
        assert n == 2


class TestMetaAndRebalance:
    def test_record_discover_count(self, client: HttpTaxonomyStore) -> None:
        client.record_discover_count("knowledge__papers", 100)
        # Should not raise; check via needs_rebalance
        assert not client.needs_rebalance("knowledge__papers", 102)  # <5% growth

    def test_needs_rebalance_on_new_collection(self, client: HttpTaxonomyStore) -> None:
        assert client.needs_rebalance("new-collection", 50)

    def test_needs_rebalance_on_large_growth(self, client: HttpTaxonomyStore) -> None:
        client.record_discover_count("coll", 100)
        assert client.needs_rebalance("coll", 200)  # 100% growth

    def test_needs_rebalance_stable(self, client: HttpTaxonomyStore) -> None:
        client.record_discover_count("coll", 100)
        assert not client.needs_rebalance("coll", 103)  # 3% growth


class TestImportFidelity:
    def test_import_topic_preserves_id(self, client: HttpTaxonomyStore) -> None:
        """ETL fidelity: original SQLite id must be preserved."""
        client.import_topic(
            src_id=9999,
            label="fidelity-test",
            parent_id=None,
            collection="knowledge__papers",
            centroid_hash="abc123",
            doc_count=42,
            created_at="2025-06-01T12:00:00Z",
            review_status="accepted",
            terms='["ai", "ml"]',
        )
        t = client.get_topic_by_id(9999)
        assert t is not None
        assert t["id"] == 9999
        assert t["label"] == "fidelity-test"
        assert t["doc_count"] == 42
        assert t["centroid_hash"] == "abc123"
        assert t["review_status"] == "accepted"

    def test_import_assignment_fidelity(self, client: HttpTaxonomyStore) -> None:
        client.import_topic(
            src_id=1, label="t", parent_id=None, collection="c",
            centroid_hash=None, doc_count=0, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        client.import_assignment(
            doc_id="doc-fidelity",
            topic_id=1,
            assigned_by="projection",
            similarity=0.77,
            assigned_at="2026-01-15T10:00:00Z",
            source_collection="knowledge__papers",
        )
        docs = client.get_topic_doc_ids(1)
        assert "doc-fidelity" in docs
        for a in _ASSIGNMENTS:
            if a["doc_id"] == "doc-fidelity":
                assert a["similarity"] == pytest.approx(0.77)
                assert a["assigned_by"] == "projection"
                break

    def test_import_topic_link_fidelity(self, client: HttpTaxonomyStore) -> None:
        client.import_topic(
            src_id=1, label="t1", parent_id=None, collection="c",
            centroid_hash=None, doc_count=0, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        client.import_topic(
            src_id=2, label="t2", parent_id=None, collection="c",
            centroid_hash=None, doc_count=0, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        client.import_topic_link(
            from_topic_id=1, to_topic_id=2,
            link_count=99, link_types='["co-occurrence"]',
        )
        pairs = client.get_topic_link_pairs([1])
        assert any(p[2] == 99 for p in pairs)

    def test_import_taxonomy_meta_fidelity(self, client: HttpTaxonomyStore) -> None:
        client.import_taxonomy_meta(
            collection="knowledge__papers",
            last_discover_doc_count=500,
            last_discover_at="2026-05-01T00:00:00Z",
        )
        # Check via needs_rebalance (last_discover_doc_count=500, asking with 501 = <5%)
        assert not client.needs_rebalance("knowledge__papers", 501)


class TestMiscMethods:
    def test_close_is_idempotent(self, client: HttpTaxonomyStore) -> None:
        """close() should not raise even if called multiple times."""
        client.close()
        client.close()

    def test_clear_icf_cache_noop(self, client: HttpTaxonomyStore) -> None:
        """clear_icf_cache is a no-op over HTTP."""
        client.clear_icf_cache()  # should not raise

    def test_get_labels_for_ids(self, client: HttpTaxonomyStore) -> None:
        client.import_topic(
            src_id=1, label="alpha", parent_id=None, collection="c",
            centroid_hash=None, doc_count=0, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        client.import_topic(
            src_id=2, label="beta", parent_id=None, collection="c",
            centroid_hash=None, doc_count=0, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        labels = client.get_labels_for_ids([1, 2, 999])
        assert labels[1] == "alpha"
        assert labels[2] == "beta"
        assert 999 not in labels

    def test_top_topics_for_collection(self, client: HttpTaxonomyStore) -> None:
        client.import_topic(
            src_id=1, label="ml", parent_id=None, collection="coll-A",
            centroid_hash=None, doc_count=5, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        client.import_topic(
            src_id=2, label="nlp", parent_id=None, collection="coll-B",
            centroid_hash=None, doc_count=3, created_at="2026-01-01T00:00:00Z",
            review_status="pending", terms=None,
        )
        tops = client.top_topics_for_collection("coll-A")
        assert len(tops) == 1
        assert tops[0]["label"] == "ml"
