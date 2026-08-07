# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Consolidated store-level self-heal + bypass-guard suite for the T2 HTTP
stores (RefreshableHttpStoreMixin adoption sweep: nexus-bikit.4 canonical
adopter, nexus-f2qvx.1/.2/.3 batches A/B/C).

Supersedes the 9 near-identical ``test_http_<store>_selfheal.py`` files
(memory, taxonomy, aspect_queue, centroid, chash_index, document_aspects,
document_highlights, plan_library, telemetry). ``tests/db/test_refreshable_client.py``
(nexus-bikit.2/.3) already proves the MIXIN self-heals over a synthetic
``/v1/echo`` endpoint; this file proves the same behaviour end-to-end
through each REAL ``Http*Store`` instance against a REAL (fake, in-process)
service — the thing an adopter can get wrong even with a correct mixin,
e.g. by leaving one inline ``self._client.*`` call site un-migrated.

Each store proves, parametrized over the identical contract:
  - write-path self-heal on rotated bearer token (401 -> retry -> succeed,
    exactly one retry — not a retry loop)
  - read-path self-heal on rotated bearer token
  - (where the store has a 404-as-sentinel contract) the sentinel survives
    adoption, even though the mixin's ``_get`` raises
    ``httpx.HTTPStatusError`` internally for ANY non-2xx
  - zero inline ``self._client.<verb>(...)`` call sites bypass the mixin's
    self-healing transport (scripted regression guard; "functional gates
    must be self-provisioning, not ambient machine state")

Store-specific behaviour that does NOT fit this template is kept as its
own explicitly named test class below the parametrized suite — nothing
from the original 9 files was dropped, only de-duplicated:
  - ``TestLazyCentroidChildPinPropagation`` (taxonomy-only: the lazy
    ``_centroid`` child's pin-state inheritance from its parent,
    nexus-gcx2r)
  - ``TestRenameLockAcceptedButIgnored`` / ``TestTimeoutPassthrough``
    (aspect_queue-only: constructor-parity ``rename_lock`` no-op + the
    store's own ``timeout`` kwarg threading through the mixin)
  - telemetry's chartered bypass-method exemption (``query_tier_writes_once``,
    nexus-ov13k) inside the generic bypass-guard test
"""
from __future__ import annotations

import json
import re
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from nexus.db.t2.http_aspect_queue import HttpAspectQueue
from nexus.db.t2.http_centroid_store import HttpCentroidStore
from nexus.db.t2.http_chash_index import HttpChashIndex
from nexus.db.t2.http_document_aspects_store import HttpDocumentAspectsStore
from nexus.db.t2.http_document_highlights_store import HttpDocumentHighlightsStore
from nexus.db.t2.http_memory_store import HttpMemoryStore
from nexus.db.t2.http_plan_library import HttpPlanLibrary
from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
from nexus.db.t2.http_telemetry_store import HttpTelemetryStore
from nexus.db.t2.records import AspectRecord, HighlightRecord

_SRC_T2_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "nexus" / "db" / "t2"
_BYPASS_PATTERN = re.compile(r"self\._client\.(get|post|put|delete|patch|request)\(")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── Shared rotatable-bearer fake-service plumbing ───────────────────────────


@dataclass
class _FakeState:
    """Per-store fake-service state: bearer token, request-count ledger
    (lets tests assert "retried exactly once" by counting round trips,
    INCLUDING the 401), and a free-form data container for whatever the
    store's endpoints persist."""

    initial_bearer: str
    valid_bearer: str = field(init=False)
    request_count: dict[str, int] = field(default_factory=dict)
    rows: Any = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    #: snapshot of ``extra``'s initial keys/values (e.g. id sequences,
    #: counters) — ``reset()`` restores to THIS, not to empty, since
    #: several handlers assume their counter keys always exist. Captured
    #: lazily on the FIRST ``reset()`` (module-level setup code that
    #: seeds ``extra`` runs at import time, before any test/reset), not in
    #: ``__post_init__`` — construction happens before that seeding.
    extra_defaults: dict[str, Any] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.valid_bearer = self.initial_bearer

    def reset(self) -> None:
        if self.extra_defaults is None:
            self.extra_defaults = dict(self.extra)
        self.valid_bearer = self.initial_bearer
        self.request_count.clear()
        if isinstance(self.rows, (dict, list)):
            self.rows.clear()
        self.extra.clear()
        self.extra.update(self.extra_defaults)


class _RotatableBearerHandlerBase(BaseHTTPRequestHandler):
    """Shared send/record/bearer-check plumbing. Subclasses implement
    ``do_POST``/``do_GET`` against their own ``state`` class attribute and
    endpoint paths — the request/response schema genuinely differs per
    store, so the per-store subclasses stay bespoke; only the boilerplate
    every original file duplicated verbatim is factored up here."""

    state: _FakeState

    def log_message(self, fmt, *args):  # noqa: A002 — matches BaseHTTPRequestHandler signature
        pass  # suppress test noise

    def _send(self, status: int, body: Any) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _record(self, method: str, path: str) -> None:
        key = f"{method} {path}"
        self.state.request_count[key] = self.state.request_count.get(key, 0) + 1

    def _check_bearer(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {self.state.valid_bearer}":
            self._send(401, {"error": "unauthorized"})
            return False
        return True


@contextmanager
def _running_fake_service(
    handler_cls: type[BaseHTTPRequestHandler],
    state: _FakeState,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[int]:
    state.reset()
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("NX_SERVICE_PORT", str(port))
    monkeypatch.setenv("NX_SERVICE_TOKEN", state.valid_bearer)
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)

    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()


def _rotate_bearer(state: _FakeState, monkeypatch: pytest.MonkeyPatch, new_bearer: str) -> None:
    state.valid_bearer = new_bearer
    monkeypatch.setenv("NX_SERVICE_TOKEN", new_bearer)


# ── Per-store fake services ──────────────────────────────────────────────
# Each store's endpoint paths and payload shapes are genuinely distinct —
# these small handler subclasses are the irreducible per-store delta.

# memory ----------------------------------------------------------------

_MEMORY_STATE = _FakeState(initial_bearer="selfheal-memory-initial-bearer")
_MEMORY_STATE.rows = {}
_MEMORY_STATE.extra["id_seq"] = 0


class _MemoryHandler(_RotatableBearerHandlerBase):
    state = _MEMORY_STATE

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        self._record("POST", path)
        if path != "/v1/memory/put":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        key = (body["project"], body["title"])
        entry = self.state.rows.get(key)
        if entry is None:
            self.state.extra["id_seq"] += 1
            entry = {
                "id": self.state.extra["id_seq"],
                "project": body["project"],
                "title": body["title"],
                "content": body["content"],
                "tags": body.get("tags", ""),
                "ttl": body.get("ttl"),
                "agent": body.get("agent"),
                "session": body.get("session"),
                "timestamp": "2026-07-12T00:00:00Z",
                "access_count": 0,
                "last_accessed": "",
            }
            self.state.rows[key] = entry
        else:
            entry["content"] = body["content"]
        self._send(200, {"id": entry["id"]})

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        self._record("GET", path)
        if path != "/v1/memory/get":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        entry = self.state.rows.get((params.get("project", ""), params.get("title", "")))
        if entry is None:
            self._send(404, {"error": "not found"})
            return
        self._send(200, dict(entry))


# taxonomy ----------------------------------------------------------------

_TAXONOMY_STATE = _FakeState(initial_bearer="selfheal-taxonomy-initial-bearer")
_TAXONOMY_STATE.rows = {}


class _TaxonomyHandler(_RotatableBearerHandlerBase):
    state = _TAXONOMY_STATE

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        self._record("POST", path)
        if path != "/v1/taxonomy/meta/record":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        self.state.rows[body["collection"]] = int(body["doc_count"])
        self._send(200, {})

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        self._record("GET", path)
        if path != "/v1/taxonomy/meta/last_count":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        collection = params.get("collection", "")
        if collection not in self.state.rows:
            self._send(404, {"error": "not found"})
            return
        self._send(200, {"count": self.state.rows[collection]})


# aspect_queue --------------------------------------------------------------

_ASPECT_QUEUE_STATE = _FakeState(initial_bearer="selfheal-aspect-queue-initial-bearer")
_ASPECT_QUEUE_STATE.rows = []
_ASPECT_QUEUE_STATE.extra["pending_count"] = 0


class _AspectQueueHandler(_RotatableBearerHandlerBase):
    state = _ASPECT_QUEUE_STATE

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        self._record("POST", path)
        if path != "/v1/aspects/queue/enqueue":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        length = int(self.headers.get("Content-Length", "0"))
        json.loads(self.rfile.read(length)) if length else {}
        self.state.extra["pending_count"] += 1
        self._send(200, {})

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        self._record("GET", path)
        if path != "/v1/aspects/queue/pending_count":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        self._send(200, {"count": self.state.extra["pending_count"]})


# centroid --------------------------------------------------------------

_CENTROID_STATE = _FakeState(initial_bearer="selfheal-centroid-initial-bearer")
_CENTROID_STATE.rows = {}


class _CentroidHandler(_RotatableBearerHandlerBase):
    state = _CENTROID_STATE

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        self._record("POST", path)
        if path != "/v1/taxonomy/centroids/upsert":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        for r in body.get("records", []):
            self.state.rows[(r["collection"], int(r["topic_id"]))] = r
        self._send(200, {"ok": True, "count": len(body.get("records", []))})

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        self._record("GET", path)
        if path != "/v1/taxonomy/centroids/count":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        collection = params.get("collection")
        n = sum(1 for (c, _t) in self.state.rows if collection is None or c == collection)
        self._send(200, {"count": n})


# chash_index -------------------------------------------------------------

_CHASH_STATE = _FakeState(initial_bearer="selfheal-chash-initial-bearer")
_CHASH_STATE.rows = {}


class _ChashHandler(_RotatableBearerHandlerBase):
    state = _CHASH_STATE

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        self._record("POST", path)
        if path != "/v1/chash/upsert":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        self.state.rows[(body["chash"], body["collection"])] = {
            "chash": body["chash"],
            "physical_collection": body["collection"],
            "created_at": "2026-06-01T00:00:00Z",
        }
        self._send(200, {"ok": True})

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        self._record("GET", path)
        if path != "/v1/chash/is_empty":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        self._send(200, {"empty": len(self.state.rows) == 0})


# document_aspects ----------------------------------------------------------

_DOCUMENT_ASPECTS_STATE = _FakeState(initial_bearer="selfheal-aspects-initial-bearer")
_DOCUMENT_ASPECTS_STATE.rows = {}


class _DocumentAspectsHandler(_RotatableBearerHandlerBase):
    state = _DOCUMENT_ASPECTS_STATE

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        self._record("POST", path)
        if path != "/v1/aspects/upsert":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        self.state.rows[(body["collection"], body["source_path"])] = body
        self._send(200, {"written": True})

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        self._record("GET", path)
        if path != "/v1/aspects/get":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        key = (params.get("collection", ""), params.get("source_path", ""))
        row = self.state.rows.get(key)
        if row is None:
            self._send(404, {"error": "not found"})
            return
        self._send(200, dict(row))


def _aspect_record(**kwargs: Any) -> AspectRecord:
    defaults: dict[str, Any] = {
        "collection": "test-coll",
        "source_path": "doc.pdf",
        "problem_formulation": "pf",
        "proposed_method": "pm",
        "experimental_datasets": ["ds1"],
        "experimental_baselines": ["bl1"],
        "experimental_results": "good",
        "extras": {"k": "v"},
        "confidence": 0.9,
        "extracted_at": "2026-07-12T00:00:00.000000Z",
        "model_version": "claude-haiku-v1",
        "extractor_name": "scholarly-paper-v1",
        "source_uri": "chroma://test-coll/doc.pdf",
        "doc_id": "1.2.3",
        "salient_sentences": ["sentence one"],
    }
    defaults.update(kwargs)
    return AspectRecord(**defaults)


# document_highlights ------------------------------------------------------

_DOCUMENT_HIGHLIGHTS_STATE = _FakeState(initial_bearer="selfheal-highlights-initial-bearer")
_DOCUMENT_HIGHLIGHTS_STATE.rows = {}


class _DocumentHighlightsHandler(_RotatableBearerHandlerBase):
    state = _DOCUMENT_HIGHLIGHTS_STATE

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        self._record("POST", path)
        if path != "/v1/aspects/highlights/upsert":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        self.state.rows[body["doc_id"]] = body
        self._send(200, {"written": True})

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        self._record("GET", path)
        if path != "/v1/aspects/highlights/get":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        doc_id = params.get("doc_id", "")
        row = self.state.rows.get(doc_id)
        if row is None:
            self._send(404, {"error": "not found"})
            return
        self._send(200, dict(row))


def _highlight_record(**kwargs: Any) -> HighlightRecord:
    defaults: dict[str, Any] = {
        "doc_id": "1.2.3",
        "source_uri": "x-devonthink://abc",
        "collection": "c",
        "highlights_md": "# Highlights",
        "mentions_md": "",
        "ingested_at": "2026-07-12T00:00:00Z",
    }
    defaults.update(kwargs)
    return HighlightRecord(**defaults)


# plan_library ------------------------------------------------------------

_PLAN_LIBRARY_STATE = _FakeState(initial_bearer="selfheal-plans-initial-bearer")
_PLAN_LIBRARY_STATE.rows = {}
_PLAN_LIBRARY_STATE.extra["id_seq"] = 0


class _PlanLibraryHandler(_RotatableBearerHandlerBase):
    """The read path (``plans/get``) is one of the EXACT pre-adoption
    bypass sites (``get_plan`` called ``self._client.get`` directly, with
    a hand-rolled 404-as-None check that never went through the mixin's
    raise-on-non-2xx contract)."""

    state = _PLAN_LIBRARY_STATE

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        self._record("POST", path)
        if path != "/v1/plans/save":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        self.state.extra["id_seq"] += 1
        row = {
            "id":         self.state.extra["id_seq"],
            "query":      body.get("query", ""),
            "plan_json":  body.get("plan_json", ""),
            "outcome":    body.get("outcome", "success"),
            "tags":       body.get("tags", ""),
            "project":    body.get("project", ""),
            "created_at": "2026-07-12T00:00:00Z",
        }
        self.state.rows[row["id"]] = row
        self._send(200, {"id": row["id"]})

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        self._record("GET", path)
        if path != "/v1/plans/get":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        plan_id = params.get("id")
        if plan_id is None:
            self._send(404, {"error": "not found"})
            return
        row = self.state.rows.get(int(plan_id))
        if row is None:
            self._send(404, {"error": "not found"})
            return
        self._send(200, dict(row))


# telemetry -----------------------------------------------------------------

_TELEMETRY_STATE = _FakeState(initial_bearer="selfheal-telemetry-initial-bearer")
_TELEMETRY_STATE.rows = []
_TELEMETRY_STATE.extra["id_seq"] = 0


class _TelemetryHandler(_RotatableBearerHandlerBase):
    """The read path (``relevance/query``) is the EXACT pre-adoption
    bypass site (``get_relevance_log`` called ``self._client.get``
    directly)."""

    state = _TELEMETRY_STATE

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        self._record("POST", path)
        if path != "/v1/telemetry/relevance/log":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        self.state.extra["id_seq"] += 1
        row = {
            "id":         self.state.extra["id_seq"],
            "query":      body.get("query", ""),
            "chunk_id":   body.get("chunk_id", ""),
            "collection": body.get("collection", ""),
            "action":     body.get("action", ""),
            "session_id": body.get("session_id", ""),
            "timestamp":  "2026-07-12T00:00:00Z",
        }
        self.state.rows.append(row)
        self._send(200, {"id": row["id"]})

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        self._record("GET", path)
        if path != "/v1/telemetry/relevance/query":
            self._send(404, {"error": "not found"})
            return
        if not self._check_bearer():
            return
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        query = params.get("query", "")
        matches = [r for r in self.state.rows if not query or r["query"] == query]
        self._send(200, matches)


# ── Contract-case registry ──────────────────────────────────────────────


@dataclass(frozen=True)
class SelfHealCase:
    """One store's binding of the shared self-heal contract onto its own
    fake service, store class, and endpoint semantics."""

    store_id: str
    source_file: str
    handler_cls: type[BaseHTTPRequestHandler]
    state: _FakeState
    make_store: Callable[[], Any]
    # write path
    do_write: Callable[[Any, str], Any]
    write_count_key: str
    assert_write_effect: Callable[[_FakeState, Any, Any], None]
    # read path
    seed_for_read: Callable[[Any], Any]
    do_read: Callable[[Any, Any], Any]
    read_count_key: str
    assert_read_result: Callable[[Any], None]
    # optional 404-as-sentinel contract
    do_missing: Callable[[Any], Any] | None = None
    missing_expected: Any = None
    # optional chartered bypass-method exemptions (telemetry only)
    chartered_bypass_methods: tuple[str, ...] = ()


# Per-store assertion callables. Named functions rather than inline lambdas
# so multi-part checks read as ordinary code (no generator-throw tricks).


def _assert_memory_write(state: _FakeState, r1: Any, r2: Any) -> None:
    assert isinstance(r1, int)
    assert r2 == r1, "row id changed across rotation (same title should update in place)"


def _assert_memory_read(result: Any) -> None:
    assert result is not None
    assert result["content"] == "read path content"


def _assert_taxonomy_write(state: _FakeState, r1: Any, r2: Any) -> None:
    assert state.rows.get("selfheal-coll") == 20, "last_count not updated to 20"


def _assert_taxonomy_read(result: Any) -> None:
    assert result is False, "expected no-growth => no rebalance"


def _assert_aspect_queue_write(state: _FakeState, r1: Any, r2: Any) -> None:
    assert state.extra["pending_count"] == 2, "expected 2 enqueued rows"


def _assert_aspect_queue_read(result: Any) -> None:
    assert result == 1, "expected pending_count == 1"


def _assert_centroid_write(state: _FakeState, r1: Any, r2: Any) -> None:
    assert len(state.rows) == 2, "expected 2 centroid rows"


def _assert_centroid_read(result: Any) -> None:
    assert result == 1, "expected count == 1"


def _assert_chash_write(state: _FakeState, r1: Any, r2: Any) -> None:
    assert len(state.rows) == 2, "expected 2 chash rows"


def _assert_chash_read(result: Any) -> None:
    assert result is True, "expected is_empty() == True (no seed write)"


def _assert_document_aspects_write(state: _FakeState, r1: Any, r2: Any) -> None:
    assert r1 is True, "first upsert did not return True"
    assert r2 is True, "second upsert did not return True"


def _assert_document_aspects_read(result: Any) -> None:
    assert result is not None
    assert result.source_path == "findme.pdf"


def _assert_document_highlights_write(state: _FakeState, r1: Any, r2: Any) -> None:
    assert r1 is True, "first upsert did not return True"
    assert r2 is True, "second upsert did not return True"


def _assert_document_highlights_read(result: Any) -> None:
    assert result is not None
    assert result.doc_id == "findme"


def _assert_plan_library_write(state: _FakeState, r1: Any, r2: Any) -> None:
    assert isinstance(r1, int)
    assert r2 != r1, "plan ids collided across writes"


def _assert_plan_library_read(result: Any) -> None:
    assert result is not None
    assert result["query"] == "findme-plan"


def _assert_telemetry_write(state: _FakeState, r1: Any, r2: Any) -> None:
    assert isinstance(r1, int)
    assert r2 != r1, "relevance ids collided across writes"


def _assert_telemetry_read(result: Any) -> None:
    assert len(result) == 1, "expected exactly 1 log row"
    assert result[0]["chunk_id"] == "chunk-r1"


CASES: list[SelfHealCase] = [
    SelfHealCase(
        store_id="memory",
        source_file="http_memory_store.py",
        handler_cls=_MemoryHandler,
        state=_MEMORY_STATE,
        make_store=HttpMemoryStore,
        do_write=lambda store, tag: store.put("selfheal-proj", "w1", f"content {tag}", ttl=30),
        write_count_key="POST /v1/memory/put",
        assert_write_effect=_assert_memory_write,
        seed_for_read=lambda store: store.put("selfheal-proj", "r1", "read path content", ttl=30),
        do_read=lambda store, ctx: store.get(project="selfheal-proj", title="r1"),
        read_count_key="GET /v1/memory/get",
        assert_read_result=_assert_memory_read,
        do_missing=lambda store: store.get(project="selfheal-proj", title="does-not-exist"),
        missing_expected=None,
    ),
    SelfHealCase(
        store_id="taxonomy",
        source_file="http_taxonomy_store.py",
        handler_cls=_TaxonomyHandler,
        state=_TAXONOMY_STATE,
        make_store=HttpTaxonomyStore,
        do_write=lambda store, tag: store.record_discover_count(
            "selfheal-coll", 10 if tag == "1" else 20
        ),
        write_count_key="POST /v1/taxonomy/meta/record",
        assert_write_effect=_assert_taxonomy_write,
        seed_for_read=lambda store: store.record_discover_count("selfheal-coll-r", 100),
        do_read=lambda store, ctx: store.needs_rebalance("selfheal-coll-r", 100),
        read_count_key="GET /v1/taxonomy/meta/last_count",
        assert_read_result=_assert_taxonomy_read,
        do_missing=lambda store: store.needs_rebalance("never-seen-collection", 5),
        missing_expected=True,
    ),
    SelfHealCase(
        store_id="aspect_queue",
        source_file="http_aspect_queue.py",
        handler_cls=_AspectQueueHandler,
        state=_ASPECT_QUEUE_STATE,
        make_store=HttpAspectQueue,
        do_write=lambda store, tag: store.enqueue("selfheal-coll", f"doc{tag}.pdf"),
        write_count_key="POST /v1/aspects/queue/enqueue",
        assert_write_effect=_assert_aspect_queue_write,
        seed_for_read=lambda store: store.enqueue("selfheal-coll-r", "doc.pdf"),
        do_read=lambda store, ctx: store.pending_count(),
        read_count_key="GET /v1/aspects/queue/pending_count",
        assert_read_result=_assert_aspect_queue_read,
    ),
    SelfHealCase(
        store_id="centroid",
        source_file="http_centroid_store.py",
        handler_cls=_CentroidHandler,
        state=_CENTROID_STATE,
        make_store=HttpCentroidStore,
        do_write=lambda store, tag: store.upsert(
            [
                {
                    "collection": "selfheal-c",
                    "topic_id": 1 if tag == "1" else 2,
                    "embedding": [1.0, 0.0] if tag == "1" else [0.0, 1.0],
                    "label": "a" if tag == "1" else "b",
                    "doc_count": 1,
                }
            ]
        ),
        write_count_key="POST /v1/taxonomy/centroids/upsert",
        assert_write_effect=_assert_centroid_write,
        seed_for_read=lambda store: store.upsert(
            [
                {
                    "collection": "selfheal-r",
                    "topic_id": 1,
                    "embedding": [1.0, 0.0],
                    "label": "a",
                    "doc_count": 1,
                }
            ]
        ),
        do_read=lambda store, ctx: store.count("selfheal-r"),
        read_count_key="GET /v1/taxonomy/centroids/count",
        assert_read_result=_assert_centroid_read,
    ),
    SelfHealCase(
        store_id="chash_index",
        source_file="http_chash_index.py",
        handler_cls=_ChashHandler,
        state=_CHASH_STATE,
        make_store=HttpChashIndex,
        do_write=lambda store, tag: store.upsert(chash=f"selfheal-c{tag}", collection="selfheal-col"),
        write_count_key="POST /v1/chash/upsert",
        assert_write_effect=_assert_chash_write,
        seed_for_read=lambda store: None,
        do_read=lambda store, ctx: store.is_empty(),
        read_count_key="GET /v1/chash/is_empty",
        assert_read_result=_assert_chash_read,
    ),
    SelfHealCase(
        store_id="document_aspects",
        source_file="http_document_aspects_store.py",
        handler_cls=_DocumentAspectsHandler,
        state=_DOCUMENT_ASPECTS_STATE,
        make_store=HttpDocumentAspectsStore,
        do_write=lambda store, tag: store.upsert(_aspect_record(source_path=f"selfheal-{tag}.pdf")),
        write_count_key="POST /v1/aspects/upsert",
        assert_write_effect=_assert_document_aspects_write,
        seed_for_read=lambda store: store.upsert(_aspect_record(collection="c", source_path="findme.pdf")),
        do_read=lambda store, ctx: store.get("c", "findme.pdf"),
        read_count_key="GET /v1/aspects/get",
        assert_read_result=_assert_document_aspects_read,
        do_missing=lambda store: store.get("never-seen-coll", "never-seen-path.pdf"),
        missing_expected=None,
    ),
    SelfHealCase(
        store_id="document_highlights",
        source_file="http_document_highlights_store.py",
        handler_cls=_DocumentHighlightsHandler,
        state=_DOCUMENT_HIGHLIGHTS_STATE,
        make_store=HttpDocumentHighlightsStore,
        do_write=lambda store, tag: store.upsert(_highlight_record(doc_id=f"selfheal-{tag}")),
        write_count_key="POST /v1/aspects/highlights/upsert",
        assert_write_effect=_assert_document_highlights_write,
        seed_for_read=lambda store: store.upsert(_highlight_record(doc_id="findme")),
        do_read=lambda store, ctx: store.get("findme"),
        read_count_key="GET /v1/aspects/highlights/get",
        assert_read_result=_assert_document_highlights_read,
        do_missing=lambda store: store.get("never-seen-doc-id"),
        missing_expected=None,
    ),
    SelfHealCase(
        store_id="plan_library",
        source_file="http_plan_library.py",
        handler_cls=_PlanLibraryHandler,
        state=_PLAN_LIBRARY_STATE,
        make_store=HttpPlanLibrary,
        do_write=lambda store, tag: store.save_plan(query=f"selfheal query {tag}", plan_json="{}"),
        write_count_key="POST /v1/plans/save",
        assert_write_effect=_assert_plan_library_write,
        seed_for_read=lambda store: store.save_plan(query="findme-plan", plan_json="{}"),
        do_read=lambda store, ctx: store.get_plan(ctx),
        read_count_key="GET /v1/plans/get",
        assert_read_result=_assert_plan_library_read,
        do_missing=lambda store: store.get_plan(999999),
        missing_expected=None,
    ),
    SelfHealCase(
        store_id="telemetry",
        source_file="http_telemetry_store.py",
        handler_cls=_TelemetryHandler,
        state=_TELEMETRY_STATE,
        make_store=HttpTelemetryStore,
        do_write=lambda store, tag: store.log_relevance(f"selfheal query {tag}", f"chunk-{tag}", "store_put"),
        write_count_key="POST /v1/telemetry/relevance/log",
        assert_write_effect=_assert_telemetry_write,
        seed_for_read=lambda store: store.log_relevance("findme-query", "chunk-r1", "store_put"),
        do_read=lambda store, ctx: store.get_relevance_log(query="findme-query"),
        read_count_key="GET /v1/telemetry/relevance/query",
        assert_read_result=_assert_telemetry_read,
        # nexus-ov13k: query_tier_writes_once is the session-end summary's
        # single-attempt read — the mixin's retry ladder (gateway backoff +
        # evidence-gated lease-wait) has a 20-50s worst case that must
        # never run at session close. Every OTHER call site must use
        # _get/_post.
        chartered_bypass_methods=("query_tier_writes_once",),
    ),
]

_CASES_WITH_MISSING = [c for c in CASES if c.do_missing is not None]


# ── Generic parametrized contract tests ─────────────────────────────────


@pytest.mark.parametrize("case", CASES, ids=[c.store_id for c in CASES])
def test_write_selfheals_on_rotated_bearer(case: SelfHealCase, monkeypatch: pytest.MonkeyPatch) -> None:
    """Write-path: the mutating operation must self-heal after a sibling
    process rotates the bearer token, not surface a 401 to the caller."""
    with _running_fake_service(case.handler_cls, case.state, monkeypatch):
        store = case.make_store()
        try:
            r1 = case.do_write(store, "1")
            assert case.state.request_count[case.write_count_key] == 1

            _rotate_bearer(case.state, monkeypatch, f"rotated-bearer-write-{case.store_id}")

            r2 = case.do_write(store, "2")
            # 1 (baseline) + 1 (401 on stale header) + 1 (retry, succeeds) == 3.
            assert case.state.request_count[case.write_count_key] == 3, (
                "expected exactly one failed attempt followed by one successful "
                "retry on the WRITE path — not a retry loop"
            )
            case.assert_write_effect(case.state, r1, r2)
        finally:
            store.close()


@pytest.mark.parametrize("case", CASES, ids=[c.store_id for c in CASES])
def test_read_selfheals_on_rotated_bearer(case: SelfHealCase, monkeypatch: pytest.MonkeyPatch) -> None:
    """Read-path: the query operation must self-heal too — a write-only
    fix would leave every read still vulnerable to the exact bug this
    mixin exists to close."""
    with _running_fake_service(case.handler_cls, case.state, monkeypatch):
        store = case.make_store()
        try:
            ctx = case.seed_for_read(store)
            assert case.read_count_key not in case.state.request_count

            baseline = case.do_read(store, ctx)
            case.assert_read_result(baseline)
            assert case.state.request_count[case.read_count_key] == 1

            _rotate_bearer(case.state, monkeypatch, f"rotated-bearer-read-{case.store_id}")

            result = case.do_read(store, ctx)
            case.assert_read_result(result)
            # 1 (baseline) + 1 (401 on stale header) + 1 (retry, succeeds) == 3.
            assert case.state.request_count[case.read_count_key] == 3, (
                "expected exactly one failed attempt followed by one successful "
                "retry on the READ path — not a retry loop"
            )
        finally:
            store.close()


@pytest.mark.parametrize("case", _CASES_WITH_MISSING, ids=[c.store_id for c in _CASES_WITH_MISSING])
def test_missing_returns_sentinel_not_an_exception(case: SelfHealCase, monkeypatch: pytest.MonkeyPatch) -> None:
    """404-as-sentinel contract must survive adoption: a genuine not-found
    is NOT retryable and NOT an exception to the caller, even though the
    mixin's ``_get`` raises ``httpx.HTTPStatusError`` internally for ANY
    non-2xx."""
    with _running_fake_service(case.handler_cls, case.state, monkeypatch):
        store = case.make_store()
        try:
            assert case.do_missing is not None
            result = case.do_missing(store)
            assert result is case.missing_expected
        finally:
            store.close()


@pytest.mark.parametrize("case", CASES, ids=[c.store_id for c in CASES])
def test_zero_inline_client_call_sites(case: SelfHealCase) -> None:
    """Scripted regression guard: a future edit must not reintroduce a
    direct ``self._client.<verb>(...)`` call that bypasses the mixin's
    self-healing ``_get``/``_post``/``_delete`` wrappers — exactly the
    class of gap this adoption sweep exists to close. Scripted per this
    project's "functional gates must be self-provisioning, never ambient
    machine state" convention (no reliance on a human remembering to grep
    before merging)."""
    source = (_SRC_T2_DIR / case.source_file).read_text()
    # Blank out chartered methods' bodies before scanning: an inline call
    # site is allowed ONLY inside an explicitly chartered method.
    scan = source
    for name in case.chartered_bypass_methods:
        start = scan.find(f"def {name}(")
        assert start != -1, f"chartered method {name} vanished — update the charter"
        nxt = scan.find("\n    def ", start + 1)
        scan = scan[:start] + scan[nxt if nxt != -1 else len(scan):]
    matches = _BYPASS_PATTERN.findall(scan)
    assert matches == [], (
        f"found {len(matches)} inline self._client.<verb>(...) call site(s) in "
        f"{case.source_file} that bypass RefreshableHttpStoreMixin's "
        f"self-healing transport: {matches}"
    )


# ── Store-specific deltas (do not fit the shared contract) ─────────────


class TestLazyCentroidChildPinPropagation:
    """nexus-gcx2r (decision-surface audit, 2026-07-12): the lazy
    ``_centroid`` property constructed ``HttpCentroidStore`` with EXPLICIT
    ``base_url=self._base_url, _token=self._token`` -- the mixin marks both
    halves pinned, and ``_invalidate_and_reresolve`` refuses to self-heal a
    fully pinned instance. Since that property is the ONLY production
    construction site of ``HttpCentroidStore``, the mixin's self-heal for
    centroid ops was dead code in production: any supervisor restart or
    token rotation turned every centroid-backed operation into a
    ``RuntimeError: cannot self-heal`` for the life of the instance.

    Contract fixed here: the child must inherit the PARENT's pin state --
    an unpinned (production) parent yields an unpinned, self-healing child;
    a deliberately pinned (test/fake-server) parent yields an equally
    pinned child, preserving the existing pin semantics.

    Taxonomy-only: no other store in this sweep has a lazy child-store
    property, so this class has no home in the generic contract above.
    """

    def test_unpinned_parent_yields_unpinned_selfhealing_child(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with _running_fake_service(_TaxonomyHandler, _TAXONOMY_STATE, monkeypatch):
            store = HttpTaxonomyStore()
            try:
                child = store._centroid
                assert child._base_url_pinned is False
                assert child._token_pinned is False

                # Prove the child ACTUALLY self-heals, not just that flags
                # are set: rotate the service token out from under it,
                # re-resolve, and confirm the child picked up the new
                # credential with no "cannot self-heal" RuntimeError.
                new_bearer = "selfheal-taxonomy-rotated-bearer"
                _rotate_bearer(_TAXONOMY_STATE, monkeypatch, new_bearer)
                child._invalidate_and_reresolve()
                assert child._token == new_bearer
            finally:
                store.close()

    def test_fully_pinned_parent_yields_fully_pinned_child(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A deliberately pinned parent (fake-server test construction) must
        still yield a pinned child -- the pin contract's refusal to silently
        repoint is preserved where it was actually intended."""
        with _running_fake_service(_TaxonomyHandler, _TAXONOMY_STATE, monkeypatch) as port:
            store = HttpTaxonomyStore(
                base_url=f"http://127.0.0.1:{port}", _token=_TAXONOMY_STATE.valid_bearer
            )
            try:
                child = store._centroid
                assert child._base_url_pinned is True
                assert child._token_pinned is True
                with pytest.raises(RuntimeError, match="cannot self-heal"):
                    child._invalidate_and_reresolve()
            finally:
                store.close()

    # The two MIXED combinations (substantive-critic, nexus-1ytp6/gcx2r fix
    # review round 1): the per-half propagation is exactly the novel logic
    # this fix introduces, so both mixed cases are pinned independently --
    # a regression to all-or-nothing propagation in either direction trips
    # one of these.

    def test_base_url_pinned_token_resolved_child_pins_url_half_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with _running_fake_service(_TaxonomyHandler, _TAXONOMY_STATE, monkeypatch) as port:
            store = HttpTaxonomyStore(base_url=f"http://127.0.0.1:{port}")
            try:
                child = store._centroid
                assert child._base_url_pinned is True
                assert child._token_pinned is False
            finally:
                store.close()

    def test_token_pinned_base_url_resolved_child_pins_token_half_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with _running_fake_service(_TaxonomyHandler, _TAXONOMY_STATE, monkeypatch):
            store = HttpTaxonomyStore(_token=_TAXONOMY_STATE.valid_bearer)
            try:
                child = store._centroid
                assert child._base_url_pinned is False
                assert child._token_pinned is True
            finally:
                store.close()


class TestRenameLockAcceptedButIgnored:
    """rename_lock stays a pure constructor-parity no-op post-adoption
    (verbatim behavior preserved from the pre-mixin constructor).
    Aspect_queue-only: no other store in this sweep has a rename_lock kwarg.
    """

    def test_rename_lock_accepted_but_ignored(self) -> None:
        lock = threading.RLock()
        store = HttpAspectQueue(base_url="http://test", _token="tok", rename_lock=lock)
        assert store.rename_lock is lock
        store.close()

    def test_rename_lock_defaults_to_a_new_rlock(self) -> None:
        store = HttpAspectQueue(base_url="http://test", _token="tok")
        assert isinstance(store.rename_lock, type(threading.RLock()))
        store.close()


class TestTimeoutPassthrough:
    """HttpAspectQueue's own public ``timeout`` kwarg must actually reach
    the underlying httpx.Client — this is the additive mixin change this
    batch required (RefreshableHttpStoreMixin.__init__ grew a matching
    optional ``timeout`` kwarg, defaulting to the same 30.0s every other
    adopter already got, so this store could thread ITS kwarg through
    instead of it being silently dropped). Aspect_queue-only: it is the
    one store in this sweep with its own public ``timeout`` constructor
    kwarg (pre-dating the mixin).
    """

    def test_default_timeout_is_30_seconds(self) -> None:
        store = HttpAspectQueue(base_url="http://test", _token="tok")
        assert store._client.timeout == httpx.Timeout(30.0)
        store.close()

    def test_explicit_timeout_reaches_the_underlying_client(self) -> None:
        store = HttpAspectQueue(base_url="http://test", _token="tok", timeout=5.0)
        assert store._client.timeout == httpx.Timeout(5.0)
        store.close()

    def test_explicit_timeout_differs_from_default(self) -> None:
        """Belt-and-suspenders: prove the two constructions are not
        coincidentally equal (i.e. this isn't testing a no-op)."""
        default_store = HttpAspectQueue(base_url="http://test", _token="tok")
        custom_store = HttpAspectQueue(base_url="http://test", _token="tok", timeout=90.0)
        assert default_store._client.timeout != custom_store._client.timeout
        default_store.close()
        custom_store.close()
