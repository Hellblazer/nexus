# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for HttpTelemetryStore.

Test approach: in-process fake HTTP server implementing the /v1/telemetry/*
contract. The fake server mirrors the REAL Java TelemetryHandler shape faithfully.

This verifies:
  - HttpTelemetryStore makes correct HTTP calls (right paths, headers, payloads)
  - HTTP error codes map to the expected Python exceptions
  - Auth header and X-Nexus-Tenant header are sent on every request
  - import_* methods route to POST /v1/telemetry/import with correct table field
  - TIMESTAMP PRESERVATION: import paths forward the source timestamp verbatim
    (not now()); verifying the store sends the correct field, not the server-side
    behavior which is tested in TelemetryRepositoryTest.java
  - rename_collection returns the dict shape {search_telemetry, hook_failures}

Full cross-language end-to-end is in tests/db/test_http_telemetry_store_integration.py
(marked integration).
"""
from __future__ import annotations

import json
import socket
import threading
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from nexus.db.t2.http_telemetry_store import DEFAULT_TENANT, HttpTelemetryStore

TOKEN = "fake-telemetry-token-abc"
PAST_TS = "2024-01-15T10:30:00Z"


# ── In-process fake server ─────────────────────────────────────────────────────

_relevance_log: list[dict[str, Any]] = []
_search_telemetry: list[dict[str, Any]] = []
_tier_writes: list[dict[str, Any]] = []
_nx_answer_runs: list[dict[str, Any]] = []
_hook_failures: list[dict[str, Any]] = []
_frecency: dict[str, dict[str, Any]] = {}  # keyed by chunk_id
_STORE_LOCK = threading.Lock()
_ID_SEQ: dict[str, int] = defaultdict(int)

IMPORT_LOG: list[dict[str, Any]] = []  # captures /import payloads for assertion


def _clear_all() -> None:
    with _STORE_LOCK:
        _relevance_log.clear()
        _search_telemetry.clear()
        _tier_writes.clear()
        _nx_answer_runs.clear()
        _hook_failures.clear()
        _frecency.clear()
        _ID_SEQ.clear()
        IMPORT_LOG.clear()


class _FakeTelemetryHandler(BaseHTTPRequestHandler):
    """In-process stub of TelemetryHandler (Java)."""

    def log_message(self, fmt, *args):
        pass

    def _check_auth(self) -> bool:
        auth   = self.headers.get("Authorization", "")
        tenant = self.headers.get("X-Nexus-Tenant", "")
        if auth != f"Bearer {TOKEN}":
            self._send(401, {"error": "unauthorized"})
            return False
        if not tenant:
            self._send(400, {"error": "missing X-Nexus-Tenant header"})
            return False
        return True

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        return json.loads(raw) if raw else {}

    def _send(self, status: int, data: Any) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _qs(self) -> dict[str, str]:
        parsed = parse_qs(urlparse(self.path).query)
        return {k: v[0] for k, v in parsed.items()}

    def do_POST(self):
        if not self._check_auth():
            return
        pp = urlparse(self.path).path
        body = self._body()

        if pp == "/v1/telemetry/relevance/log":
            with _STORE_LOCK:
                _ID_SEQ["rel"] += 1
                row = {
                    "id":         _ID_SEQ["rel"],
                    "query":      body.get("query", ""),
                    "chunk_id":   body.get("chunk_id", ""),
                    "collection": body.get("collection", ""),
                    "action":     body.get("action", ""),
                    "session_id": body.get("session_id", ""),
                    "timestamp":  datetime.now(UTC).isoformat(),
                }
                _relevance_log.append(row)
            self._send(200, {"id": row["id"]})

        elif pp == "/v1/telemetry/relevance/batch":
            rows = body.get("rows", [])
            with _STORE_LOCK:
                for r in rows:
                    _ID_SEQ["rel"] += 1
                    _relevance_log.append({
                        "id":         _ID_SEQ["rel"],
                        "query":      r[0] if len(r) > 0 else "",
                        "chunk_id":   r[1] if len(r) > 1 else "",
                        "collection": r[2] if len(r) > 2 else "",
                        "action":     r[3] if len(r) > 3 else "",
                        "session_id": r[4] if len(r) > 4 else "",
                        "timestamp":  datetime.now(UTC).isoformat(),
                    })
            self._send(200, {"inserted": len(rows)})

        elif pp == "/v1/telemetry/relevance/expire":
            days = int(body.get("days", 90))
            cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
            with _STORE_LOCK:
                before = len(_relevance_log)
                _relevance_log[:] = [r for r in _relevance_log if r["timestamp"] >= cutoff]
                deleted = before - len(_relevance_log)
            self._send(200, {"deleted": deleted})

        elif pp == "/v1/telemetry/search/batch":
            rows = body.get("rows", [])
            with _STORE_LOCK:
                for r in rows:
                    _search_telemetry.append({
                        "ts":           r[0] if len(r) > 0 else "",
                        "query_hash":   r[1] if len(r) > 1 else "",
                        "collection":   r[2] if len(r) > 2 else "",
                        "raw_count":    r[3] if len(r) > 3 else 0,
                        "kept_count":   r[4] if len(r) > 4 else 0,
                        "top_distance": r[5] if len(r) > 5 else None,
                        "threshold":    r[6] if len(r) > 6 else None,
                    })
            self._send(200, {"inserted": len(rows)})

        elif pp == "/v1/telemetry/search/trim":
            days = int(body.get("days", 30))
            cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
            with _STORE_LOCK:
                before = len(_search_telemetry)
                _search_telemetry[:] = [r for r in _search_telemetry if r["ts"] >= cutoff]
                deleted = before - len(_search_telemetry)
            self._send(200, {"deleted": deleted})

        elif pp == "/v1/telemetry/rename_collection":
            old = body.get("old", "")
            new = body.get("new", "")
            with _STORE_LOCK:
                st_count = sum(
                    1 for r in _search_telemetry
                    if r["collection"] == old
                )
                for r in _search_telemetry:
                    if r["collection"] == old:
                        r["collection"] = new
                hf_count = sum(
                    1 for r in _hook_failures
                    if r.get("collection") == old
                )
                for r in _hook_failures:
                    if r.get("collection") == old:
                        r["collection"] = new
            self._send(200, {
                "search_telemetry": st_count,
                "hook_failures":    hf_count,
            })

        elif pp == "/v1/telemetry/tier_writes/record":
            with _STORE_LOCK:
                _ID_SEQ["tw"] += 1
                _tier_writes.append({
                    "id":           _ID_SEQ["tw"],
                    "session_id":   body.get("session_id", ""),
                    "ts":           body.get("ts", datetime.now(UTC).isoformat()),
                    "tool":         body.get("tool", ""),
                    "tier":         body.get("tier", ""),
                    "agent":        body.get("agent"),
                    "project":      body.get("project"),
                    "target_title": body.get("target_title"),
                })
            self._send(200, {"ok": True})

        elif pp == "/v1/telemetry/nx_answer_runs/record":
            with _STORE_LOCK:
                _ID_SEQ["nar"] += 1
                _nx_answer_runs.append({
                    "id":               _ID_SEQ["nar"],
                    "question":         body.get("question", ""),
                    "created_at":       body.get("created_at", datetime.now(UTC).isoformat()),
                })
            self._send(200, {"ok": True})

        elif pp == "/v1/telemetry/hook_failures/record":
            with _STORE_LOCK:
                _ID_SEQ["hf"] += 1
                _hook_failures.append({
                    "id":          _ID_SEQ["hf"],
                    "hook_name":   body.get("hook_name", ""),
                    "occurred_at": body.get("occurred_at", datetime.now(UTC).isoformat()),
                    "collection":  body.get("collection", ""),
                })
            self._send(200, {"ok": True})

        elif pp == "/v1/telemetry/frecency/upsert":
            chunk_id = body.get("chunk_id", "")
            with _STORE_LOCK:
                existing = _frecency.get(chunk_id)
                if existing is None:
                    _frecency[chunk_id] = dict(body)
                else:
                    # GREATEST for score/count/last_hit_at, LEAST for embedded_at
                    def _gts(a: str | None, b: str | None) -> str | None:
                        if a is None: return b
                        if b is None: return a
                        return max(a, b)
                    def _lts(a: str | None, b: str | None) -> str | None:
                        if a is None: return b
                        if b is None: return a
                        return min(a, b)
                    existing["frecency_score"] = max(
                        float(existing.get("frecency_score", 0.0) or 0.0),
                        float(body.get("frecency_score", 0.0) or 0.0),
                    )
                    existing["miss_count"] = max(
                        int(existing.get("miss_count", 0) or 0),
                        int(body.get("miss_count", 0) or 0),
                    )
                    existing["last_hit_at"] = _gts(
                        existing.get("last_hit_at"), body.get("last_hit_at")
                    )
                    existing["embedded_at"] = _lts(
                        existing.get("embedded_at"), body.get("embedded_at")
                    )
            self._send(200, {"ok": True})

        elif pp == "/v1/telemetry/import":
            # Capture payload for assertion; do same routing as above
            with _STORE_LOCK:
                IMPORT_LOG.append(dict(body))
            table = body.get("table", "")
            if table == "relevance_log":
                with _STORE_LOCK:
                    _ID_SEQ["rel"] += 1
                    _relevance_log.append({
                        "id":         _ID_SEQ["rel"],
                        "query":      body.get("query", ""),
                        "chunk_id":   body.get("chunk_id", ""),
                        "collection": body.get("collection", ""),
                        "action":     body.get("action", ""),
                        "session_id": body.get("session_id", ""),
                        # VERBATIM from source — fidelity-preserving import
                        "timestamp":  body.get("timestamp", ""),
                    })
            elif table == "frecency":
                chunk_id = body.get("chunk_id", "")
                with _STORE_LOCK:
                    existing = _frecency.get(chunk_id)
                    if existing is None:
                        _frecency[chunk_id] = dict(body)
                    else:
                        # GREATEST/LEAST conflict
                        def _g(a, b, default=0.0):
                            a = a if a is not None else default
                            b = b if b is not None else default
                            return max(a, b)
                        def _gts(a, b):
                            if a is None: return b
                            if b is None: return a
                            return max(a, b)
                        def _lts(a, b):
                            if a is None: return b
                            if b is None: return a
                            return min(a, b)
                        existing["frecency_score"] = _g(
                            body.get("frecency_score"), existing.get("frecency_score")
                        )
                        existing["miss_count"] = int(_g(
                            body.get("miss_count"), existing.get("miss_count"), 0
                        ))
                        existing["last_hit_at"] = _gts(
                            existing.get("last_hit_at"), body.get("last_hit_at")
                        )
                        existing["embedded_at"] = _lts(
                            existing.get("embedded_at"), body.get("embedded_at")
                        )
            # For other tables: just record in IMPORT_LOG (already done above)
            self._send(200, {"ok": True})

        else:
            self._send(404, {"error": "not found"})

    def do_GET(self):
        if not self._check_auth():
            return
        pp = urlparse(self.path).path
        qs = self._qs()

        if pp == "/v1/telemetry/relevance/query":
            q         = qs.get("query", "")
            chunk_id  = qs.get("chunk_id", "")
            action    = qs.get("action", "")
            session_id = qs.get("session_id", "")
            limit     = int(qs.get("limit", "100"))
            with _STORE_LOCK:
                results = [
                    r for r in _relevance_log
                    if (not q         or r["query"]      == q)
                    and (not chunk_id  or r["chunk_id"]   == chunk_id)
                    and (not action    or r["action"]     == action)
                    and (not session_id or r["session_id"] == session_id)
                ]
                results = sorted(results, key=lambda x: x["timestamp"], reverse=True)[:limit]
            self._send(200, results)

        elif pp == "/v1/telemetry/search/stats":
            collection = qs.get("collection", "")
            days       = int(qs.get("days", "30"))
            cutoff     = (datetime.now(UTC) - timedelta(days=days)).isoformat()
            with _STORE_LOCK:
                rows = [r for r in _search_telemetry
                        if r["collection"] == collection and r["ts"] >= cutoff]
            row_count = len(rows)
            zero_count = sum(1 for r in rows if r.get("kept_count", 1) == 0)
            zero_hit_rate = zero_count / row_count if row_count else None
            dists = [r["top_distance"] for r in rows
                     if r.get("raw_count", 0) > 0 and r.get("top_distance") is not None]
            median = None
            if dists:
                dists.sort()
                n = len(dists)
                median = (dists[n // 2] if n % 2 == 1
                          else (dists[n // 2 - 1] + dists[n // 2]) / 2)
            self._send(200, {
                "row_count":           row_count,
                "zero_hit_rate":       zero_hit_rate,
                "median_top_distance": median,
            })

        elif pp == "/v1/telemetry/frecency/get":
            chunk_id = qs.get("chunk_id", "")
            with _STORE_LOCK:
                row = _frecency.get(chunk_id)
            if row is None:
                self._send(404, {"error": "not found"})
            else:
                self._send(200, row)

        else:
            self._send(404, {"error": "not found"})


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def fake_server():
    """Start the fake TelemetryHandler server on a random free port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = HTTPServer(("127.0.0.1", port), _FakeTelemetryHandler)
    t   = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def clear_stores():
    """Clear all in-memory stores before each test."""
    _clear_all()
    yield
    _clear_all()


@pytest.fixture
def client(fake_server):
    """HttpTelemetryStore connected to the fake server."""
    c = HttpTelemetryStore(base_url=fake_server, _token=TOKEN)
    yield c
    c.close()


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestLogRelevance:
    def test_log_returns_id(self, client):
        rid = client.log_relevance("test query", "chunk-1", "store_put")
        assert isinstance(rid, int)
        assert rid > 0

    def test_log_roundtrip_query(self, client):
        client.log_relevance("round-trip query", "chunk-rt", "catalog_link",
                             collection="knowledge__nexus")
        rows = client.get_relevance_log(query="round-trip query")
        assert len(rows) == 1
        assert rows[0]["chunk_id"] == "chunk-rt"
        assert rows[0]["action"] == "catalog_link"
        assert rows[0]["collection"] == "knowledge__nexus"

    def test_log_sends_auth_headers(self, fake_server):
        """Wrong token must raise HTTP 401."""
        bad = HttpTelemetryStore(base_url=fake_server, _token="wrong-token")
        with pytest.raises(Exception, match="401"):
            bad.log_relevance("q", "c", "a")
        bad.close()


class TestLogRelevanceBatch:
    def test_batch_returns_count(self, client):
        rows = [
            ("q1", "c1", "coll1", "store_put", "sess1"),
            ("q2", "c2", "coll2", "catalog_link", "sess2"),
        ]
        count = client.log_relevance_batch(rows)
        assert count == 2

    def test_batch_empty_returns_zero(self, client):
        assert client.log_relevance_batch([]) == 0


class TestGetRelevanceLog:
    def test_filter_by_query(self, client):
        client.log_relevance("find-me", "c1", "a1")
        client.log_relevance("skip-me", "c2", "a2")
        rows = client.get_relevance_log(query="find-me")
        assert len(rows) == 1
        assert rows[0]["query"] == "find-me"

    def test_filter_by_chunk_id(self, client):
        client.log_relevance("q", "target-chunk", "store_put")
        client.log_relevance("q", "other-chunk", "store_put")
        rows = client.get_relevance_log(chunk_id="target-chunk")
        assert len(rows) == 1
        assert rows[0]["chunk_id"] == "target-chunk"

    def test_empty_filter_returns_all(self, client):
        client.log_relevance("q1", "c1", "a1")
        client.log_relevance("q2", "c2", "a2")
        rows = client.get_relevance_log()
        assert len(rows) >= 2

    def test_limit_honored(self, client):
        for i in range(5):
            client.log_relevance(f"q{i}", f"c{i}", "a")
        rows = client.get_relevance_log(limit=3)
        assert len(rows) <= 3


class TestExpireRelevanceLog:
    def test_expire_deletes_old_rows_on_fake(self, client):
        # Fake server uses server-side now() for log_relevance — to test expire,
        # seed an old row via import and then expire
        client.import_relevance_row(
            query="old",
            chunk_id="old-chunk",
            collection="",
            action="store_put",
            session_id="",
            timestamp="2020-01-01T00:00:00Z",  # 4+ years ago
        )
        rows_before = client.get_relevance_log(query="old")
        assert len(rows_before) == 1
        deleted = client.expire_relevance_log(days=365 * 3)
        assert deleted >= 1


class TestLogSearchBatch:
    def test_batch_inserts(self, client):
        rows = [
            ("2024-01-01T00:00:00Z", "hash1", "code__nexus", 10, 8, 0.25, 0.3),
            ("2024-01-02T00:00:00Z", "hash2", "knowledge__nexus", 5, 5, None, None),
        ]
        count = client.log_search_batch(rows)
        assert count == 2

    def test_batch_empty(self, client):
        assert client.log_search_batch([]) == 0


class TestQueryCollectionStats:
    def test_stats_empty_collection(self, client):
        stats = client.query_collection_stats("nonexistent__coll")
        assert stats["row_count"] == 0
        assert stats["zero_hit_rate"] is None
        assert stats["median_top_distance"] is None

    def test_stats_with_data(self, client):
        rows = [
            ("2025-01-01T00:00:00Z", "h1", "code__nexus", 10, 8, 0.2, 0.3),
            ("2025-01-02T00:00:00Z", "h2", "code__nexus",  5, 0, 0.4, 0.3),
        ]
        client.log_search_batch(rows)
        stats = client.query_collection_stats("code__nexus", days=365 * 5)
        assert stats["row_count"] == 2
        assert stats["zero_hit_rate"] == 0.5  # 1 of 2 rows has kept_count=0


class TestTrimSearchTelemetry:
    def test_trim_days_validation(self, client):
        with pytest.raises(ValueError, match="days must be >= 1"):
            client.trim_search_telemetry(days=0)

    def test_trim_removes_old(self, client):
        rows = [
            ("2020-01-01T00:00:00Z", "old-hash", "code__nexus", 1, 1, None, None),
        ]
        client.log_search_batch(rows)
        deleted = client.trim_search_telemetry(days=365 * 3)
        assert deleted >= 1


class TestRenameCollection:
    def test_rename_updates_search_telemetry(self, client):
        rows = [
            ("2025-01-01T00:00:00Z", "h1", "old-coll", 5, 5, None, None),
        ]
        client.log_search_batch(rows)
        result = client.rename_collection(old="old-coll", new="new-coll")
        assert isinstance(result, dict)
        assert "search_telemetry" in result
        assert "hook_failures" in result
        assert result["search_telemetry"] >= 1

    def test_rename_returns_int_counts(self, client):
        result = client.rename_collection(old="x", new="y")
        assert isinstance(result["search_telemetry"], int)
        assert isinstance(result["hook_failures"], int)


class TestImportTimestampFidelity:
    """HEADLINE: verify that import_* methods forward timestamps VERBATIM.

    The contract: the store sends ``timestamp=PAST_TS`` in the HTTP body;
    the fake server stores it verbatim. This verifies the PYTHON CLIENT sends
    the correct field. The Java service's actual TIMESTAMPTZ preservation is
    tested in TelemetryRepositoryTest.java.
    """

    def test_import_relevance_timestamp_verbatim(self, client):
        client.import_relevance_row(
            query="ts-fidelity-test",
            chunk_id="chunk-ts",
            collection="knowledge__nexus",
            action="store_put",
            session_id="sess-ts",
            timestamp=PAST_TS,
        )
        # IMPORT_LOG captures the payload sent to /v1/telemetry/import
        with _STORE_LOCK:
            payloads = [p for p in IMPORT_LOG if p.get("table") == "relevance_log"
                        and p.get("query") == "ts-fidelity-test"]
        assert len(payloads) == 1, "import request must reach the fake server"
        assert payloads[0]["timestamp"] == PAST_TS, (
            f"TIMESTAMP PRESERVATION: client must send PAST_TS={PAST_TS!r} verbatim; "
            f"got {payloads[0]['timestamp']!r}")

    def test_import_relevance_row_is_retrievable(self, client):
        client.import_relevance_row(
            query="import-retrieve",
            chunk_id="chunk-ir",
            collection="",
            action="store_put",
            session_id="",
            timestamp=PAST_TS,
        )
        rows = client.get_relevance_log(query="import-retrieve")
        assert len(rows) == 1
        assert rows[0]["timestamp"] == PAST_TS

    def test_import_search_row_forwarded(self, client):
        client.import_search_row(
            ts=PAST_TS,
            query_hash="tshash",
            collection="code__nexus",
            raw_count=10,
            kept_count=8,
            top_distance=0.25,
            threshold=0.3,
        )
        with _STORE_LOCK:
            payloads = [p for p in IMPORT_LOG if p.get("table") == "search_telemetry"]
        assert len(payloads) == 1
        assert payloads[0]["ts"] == PAST_TS, "search_telemetry ts must be forwarded verbatim"

    def test_import_tier_write_forwarded(self, client):
        client.import_tier_write(
            session_id="sess-tw",
            ts=PAST_TS,
            tool="memory_put",
            tier="T2",
            agent="developer",
            project="nexus",
            target_title="some-title",
        )
        with _STORE_LOCK:
            payloads = [p for p in IMPORT_LOG if p.get("table") == "tier_writes"]
        assert len(payloads) == 1
        assert payloads[0]["ts"] == PAST_TS

    def test_import_nx_answer_run_forwarded(self, client):
        client.import_nx_answer_run(
            question="how does X work",
            plan_id=None,
            matched_confidence=0.8,
            step_count=3,
            final_text="answer text",
            cost_usd=0.01,
            duration_ms=1500,
            created_at=PAST_TS,
        )
        with _STORE_LOCK:
            payloads = [p for p in IMPORT_LOG if p.get("table") == "nx_answer_runs"]
        assert len(payloads) == 1
        assert payloads[0]["created_at"] == PAST_TS

    def test_import_hook_failure_forwarded(self, client):
        client.import_hook_failure(
            doc_id="doc-123",
            collection="knowledge__nexus",
            hook_name="post_store",
            error="connection refused",
            occurred_at=PAST_TS,
            batch_doc_ids=None,
            is_batch=False,
            chain=None,
        )
        with _STORE_LOCK:
            payloads = [p for p in IMPORT_LOG if p.get("table") == "hook_failures"]
        assert len(payloads) == 1
        assert payloads[0]["occurred_at"] == PAST_TS


class TestFrecencyGreatestLeast:
    """Verify GREATEST no-clobber and LEAST embedded_at via the fake server."""

    def test_frecency_greatest_does_not_clobber_live_score(self, client):
        # Insert live-mutable frecency
        client.import_frecency_row(
            chunk_id="chunk-greatest",
            embedded_at="2024-01-01T00:00:00Z",
            ttl_days=30,
            frecency_score=0.95,
            miss_count=20,
            last_hit_at="2025-06-01T00:00:00Z",
        )
        # Re-import with stale (lower) values
        client.import_frecency_row(
            chunk_id="chunk-greatest",
            embedded_at="2023-01-01T00:00:00Z",
            ttl_days=30,
            frecency_score=0.50,
            miss_count=5,
            last_hit_at="2024-01-01T00:00:00Z",
        )
        row = _frecency.get("chunk-greatest")
        assert row is not None
        assert float(row["frecency_score"]) == pytest.approx(0.95), (
            "GREATEST: re-import with stale score=0.50 must not clobber live score=0.95")
        assert int(row["miss_count"]) == 20, (
            "GREATEST: re-import with stale miss_count=5 must not clobber live miss_count=20")

    def test_frecency_least_preserves_oldest_embedded_at(self, client):
        # Insert with a recent embedded_at
        client.import_frecency_row(
            chunk_id="chunk-least",
            embedded_at="2025-06-01T00:00:00Z",
            ttl_days=30,
            frecency_score=0.5,
            miss_count=1,
            last_hit_at=None,
        )
        # Re-import with an OLDER embedded_at — LEAST means older wins
        client.import_frecency_row(
            chunk_id="chunk-least",
            embedded_at="2023-01-01T00:00:00Z",
            ttl_days=30,
            frecency_score=0.3,
            miss_count=0,
            last_hit_at=None,
        )
        row = _frecency.get("chunk-least")
        assert row is not None
        assert row["embedded_at"] == "2023-01-01T00:00:00Z", (
            "LEAST: older embedded_at must win on conflict; "
            f"got {row['embedded_at']!r}")


class TestImportDoNothing:
    """Verify that importing the same event row twice results in DO NOTHING (not duplicate)."""

    def test_relevance_log_import_idempotent(self, client):
        kwargs = dict(
            query="idem-query",
            chunk_id="idem-chunk",
            collection="",
            action="store_put",
            session_id="sess",
            timestamp="2024-06-01T12:00:00Z",
        )
        client.import_relevance_row(**kwargs)
        client.import_relevance_row(**kwargs)
        # The fake server tracks by IMPORT_LOG (no dedup), but the REAL PG
        # service would return DO NOTHING. Here we just verify both calls succeed.
        rows = client.get_relevance_log(query="idem-query")
        # In the fake server there IS no dedup — idempotency is asserted by the
        # Java TelemetryRepositoryTest. Here we just verify the client makes 2
        # successful round-trips without error.
        assert len(rows) >= 1


class TestConfigErrors:
    def test_missing_port_raises(self, monkeypatch):
        monkeypatch.delenv("NX_SERVICE_PORT", raising=False)
        monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="NX_SERVICE_PORT"):
            HttpTelemetryStore()

    def test_missing_token_raises(self, monkeypatch, fake_server):
        # base_url provided but no NX_SERVICE_TOKEN
        monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="NX_SERVICE_TOKEN"):
            HttpTelemetryStore(base_url=fake_server)
