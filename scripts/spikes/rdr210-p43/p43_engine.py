# SPDX-License-Identifier: AGPL-3.0-or-later
"""Raw HTTP client for the SCRATCH engine the P43 harness provisions.

This talks to the engine's own routes, the same ones
``tests/e2e/dual-embedder-gate.sh`` drives with curl: catalog owner / collection
/ document registration, the combined write ``/v1/catalog/manifest/write_many``
(chunks and owner land together, a chunk is searchable only with a manifest
row), ``/v1/vectors/search`` and ``/v1/vectors/count``.

It never touches a service it did not provision: the lease it reads is the one
in ``P43_SCRATCH_CONFIG_DIR`` (set by ``run.sh``), and it refuses a non-loopback
endpoint. It never reads, prints or sends a credential other than the scratch
engine's own bearer token. Voyage-bound requests are charged to a
:class:`p43_budget.TokenBudget` BEFORE they are sent.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import p43_budget as budget
from p43_corpus import Chunk

USAGE_HEADER = "X-Nexus-Usage-Tokens"
RETRY_STATUSES = frozenset({429, 502, 503, 504})
MAX_TRIES = 5

#: Write-request chunk caps per family, the real client's
#: (``http_vector_client._ONNX_LOCAL_UPSERT_CHUNK_CAP`` = 16, ``_CCE_UPSERT_CHUNK_CAP`` = 64,
#: ``_CODE_UPSERT_CHUNK_CAP`` = 300): bge is memory-bound, CCE is slow server-side.
BGE_CAP = 16
CCE_CAP = 64
CODE_CAP = 300

BGE_MODEL = "bge-base-en-v15-768"
CONTEXT_MODEL = "voyage-context-3"
CODE_MODEL = "voyage-code-3"


def is_voyage_model(model: str) -> bool:
    return model.startswith("voyage-")


def write_cap(model: str) -> int:
    if model == CONTEXT_MODEL:
        return CCE_CAP
    if model == CODE_MODEL:
        return CODE_CAP
    return BGE_CAP


def model_of_collection(name: str) -> str:
    """The model token of a conformant four-segment collection name."""
    parts = name.split("__")
    if len(parts) != 4:
        raise ValueError(f"not a four-segment collection name: {name!r}")
    return parts[2]


def collection_name(content_type: str, owner_id: str, model: str) -> str:
    return f"{content_type}__{owner_id}__{model}__v1"


class EngineError(RuntimeError):
    def __init__(self, status: int, path: str, body: str):
        super().__init__(f"{path} -> HTTP {status}: {body[:500]}")
        self.status = status
        self.path = path
        self.body = body


@dataclass
class Response:
    status: int
    body: Any
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def usage_tokens(self) -> int:
        try:
            return int(self.headers.get(USAGE_HEADER.lower(), "0"))
        except ValueError:
            return 0


def read_lease(config_dir: Path) -> tuple[str, str]:
    """``(base_url, bearer_token)`` from the scratch config dir's lease file."""
    matches = sorted(config_dir.glob("storage_service_addr.*"))
    if not matches:
        raise RuntimeError(f"no engine lease (storage_service_addr.*) in {config_dir}")
    doc = json.loads(matches[0].read_text(encoding="utf-8"))
    ep = doc.get("endpoint", doc)
    host = ep.get("host", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError(f"refusing a non-loopback engine endpoint ({host!r}): the harness runs against a scratch engine only")
    return f"http://127.0.0.1:{int(ep['port'])}", str(ep["token"])


class Engine:
    def __init__(self, base_url: str, token: str, tokens: budget.TokenBudget,
                 *, sleep=time.sleep):
        self.base = base_url
        self._token = token
        self.tokens = tokens
        self._sleep = sleep
        self.requests = 0

    @classmethod
    def from_env(cls, tokens: budget.TokenBudget) -> "Engine":
        if os.environ.get("P43_FENCED") != "1":
            raise RuntimeError("P43_FENCED=1 not set: run through run.sh, which provisions the scratch engine")
        cfg = os.environ.get("P43_SCRATCH_CONFIG_DIR")
        if not cfg:
            raise RuntimeError("P43_SCRATCH_CONFIG_DIR not set")
        url, token = read_lease(Path(cfg))
        return cls(url, token, tokens)

    # ── transport ────────────────────────────────────────────────────────────
    def request(self, method: str, path: str, body: dict | None = None, *,
                timeout: float = 120.0) -> Response:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self._token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        last: EngineError | None = None
        for attempt in range(1, MAX_TRIES + 1):
            req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
            self.requests += 1
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - loopback scratch engine
                    raw = resp.read().decode("utf-8")
                    hdrs = {k.lower(): v for k, v in resp.headers.items()}
                    out = Response(resp.status, json.loads(raw) if raw else None, hdrs)
                    self.tokens.note_reported(out.usage_tokens)
                    return out
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace")
                last = EngineError(exc.code, path, text)
                if exc.code in RETRY_STATUSES and attempt < MAX_TRIES:
                    ra = exc.headers.get("Retry-After", "")
                    wait = min(int(ra), 60) if ra.isdigit() else 3 * attempt
                    self._sleep(wait)
                    continue
                raise last from None
            except urllib.error.URLError as exc:
                raise EngineError(0, path, f"connection failed: {exc.reason}") from None
        assert last is not None
        raise last

    def get(self, path: str, **kw) -> Response:
        return self.request("GET", path, None, **kw)

    def post(self, path: str, body: dict, **kw) -> Response:
        return self.request("POST", path, body, **kw)

    # ── facts about the engine ──────────────────────────────────────────────
    def version(self) -> dict:
        return self.get("/version").body

    def count(self, collection: str) -> int:
        r = self.get("/v1/vectors/count?collection=" + urllib.parse.quote(collection))
        return int(r.body.get("count", 0))

    _rerank_voyage: bool | None = None

    def reranker_is_voyage(self) -> bool:
        """True when the engine's reranker is a Voyage model (``rerank-*``), so a
        rerank request costs Voyage tokens. The engine picks the reranker by
        PROFILE: a bge profile reranks locally even when keyed."""
        if self._rerank_voyage is None:
            self._rerank_voyage = str(self.version().get("reranker", "")).startswith("rerank-")
        return self._rerank_voyage

    # ── registration and writes ──────────────────────────────────────────────
    def upsert_owner(self, prefix: str, name: str, owner_type: str = "p43") -> None:
        r = self.post("/v1/catalog/owners/upsert",
                      {"tumbler_prefix": prefix, "name": name, "owner_type": owner_type})
        if r.body.get("ok") is not True:
            raise EngineError(r.status, "/v1/catalog/owners/upsert", json.dumps(r.body))

    def upsert_collection(self, name: str, content_type: str, owner_id: str, model: str) -> None:
        r = self.post("/v1/catalog/collections/upsert", {
            "name": name, "content_type": content_type,
            "owner_id": owner_id, "embedding_model": model,
        })
        if r.body.get("ok") is not True:
            raise EngineError(r.status, "/v1/catalog/collections/upsert", json.dumps(r.body))

    def register_doc(self, owner_prefix: str, title: str, content_type: str, file_path: str) -> str:
        r = self.post("/v1/catalog/doc/register", {
            "owner_prefix": owner_prefix, "title": title,
            "content_type": content_type, "file_path": file_path,
        })
        tumbler = r.body.get("tumbler")
        if not isinstance(tumbler, str) or not tumbler.startswith(owner_prefix + "."):
            raise EngineError(r.status, "/v1/catalog/doc/register", json.dumps(r.body))
        return tumbler

    def write_doc(self, collection: str, model: str, doc_id: str, chunks: list[Chunk],
                  *, label: str) -> int:
        """Write one document's chunks to *collection* through the combined write.

        Batched at the family's write cap: the first batch REPLACES the document's
        manifest (``write_many``), later batches append rows by position
        (``/manifest/append``). Every batch to a Voyage collection is charged to
        the token budget before it is sent. Returns the chunks written (engine
        ``chunks_written`` summed)."""
        cap = write_cap(model)
        voyage = is_voyage_model(model)
        written = 0
        for start in range(0, len(chunks), cap):
            batch = chunks[start:start + cap]
            if voyage:
                self.tokens.charge(budget.chars_to_tokens(sum(len(c.text) for c in batch)),
                                   f"embed:{label}")
            rows = [{"position": c.position, "chash": c.chash} for c in batch]
            seen: set[str] = set()
            payload = []
            for c in batch:
                if c.chash not in seen:
                    seen.add(c.chash)
                    payload.append({"chash": c.chash, "text": c.text, "metadata": {"source": "p43"}})
            if start == 0:
                body = {"collection": collection, "docs": [{"doc_id": doc_id, "rows": rows}],
                        "chunks": payload}
                resp = self.post("/v1/catalog/manifest/write_many", body, timeout=600)
            else:
                body = {"doc_id": doc_id, "collection": collection, "rows": rows, "chunks": payload}
                resp = self.post("/v1/catalog/manifest/append", body, timeout=600)
            if "chunks_written" not in (resp.body or {}):
                raise EngineError(resp.status, "write", "response carries no chunks_written: " + json.dumps(resp.body))
            written += int(resp.body["chunks_written"])
        return written

    # ── search ───────────────────────────────────────────────────────────────
    def search(self, query: str, collections: list[str], n_results: int, *,
               rerank: bool = False, thresholds_off: bool = True) -> tuple[list[dict], dict]:
        """``POST /v1/vectors/search``. Returns ``(rows, envelope)``; *envelope* is
        the rerank envelope without its rows (``rerank_degraded``, ``rerank_model``,
        ...) or ``{}`` for a plain search. No distance threshold is ever sent: the
        route applies none (the client applies them)."""
        voyage_cols = [c for c in collections if is_voyage_model(model_of_collection(c))]
        if voyage_cols:
            self.tokens.charge(budget.chars_to_tokens(len(query)) * len(voyage_cols) or 1,
                               "query-embed")
        body: dict = {"query": query, "collections": collections, "n_results": n_results}
        if rerank:
            if self.reranker_is_voyage():
                # Upper bound before the send: every candidate row at the chunk cap.
                self.tokens.charge(n_results * budget.chars_to_tokens(len(query) + 1500), "rerank")
            body["rerank"] = True
        r = self.post("/v1/vectors/search", body)
        if rerank:
            if not isinstance(r.body, dict) or "results" not in r.body:
                raise EngineError(r.status, "/v1/vectors/search",
                                  "rerank asked but the engine answered a bare array (it predates the fused rerank stage)")
            env = {k: v for k, v in r.body.items() if k != "results"}
            return r.body["results"], env
        if not isinstance(r.body, list):
            raise EngineError(r.status, "/v1/vectors/search", "expected a JSON array")
        return r.body, {}
