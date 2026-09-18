# SPDX-License-Identifier: AGPL-3.0-or-later
"""In-memory fake of the engine's ``/v1/pipeline`` surface (RDR-186 .16).

Test infrastructure for the streaming-PDF suites after the ``pipeline.db``
SQLite buffer retired: stage tests exercise the REAL ``HttpPipelineDB``
client (buffering, read-your-writes flushing, tri-state embedding wire
mapping) against this fake, which mirrors the Java ``PipelineHandler`` +
``PipelineRepository`` semantics — create created/resuming/skip with the
5-minute staleness rule, page REPLACE upserts, chunk IGNORE inserts,
uploadable = embedding-present (the ``b""`` service sentinel counts) and
not yet uploaded. The authoritative server contract is pinned by the Java
``PipelineHandlerTest``; ``tests/db/test_pipeline_fake_engine_parity.py``
keeps this fake honest against the same scenarios.

nexus-edjmu (pipeline-002-per-row-identity.xml): a row is one RUN of one
document, identity ``pipeline_id``, unique on (content_hash, collection,
pdf_path); pages and chunks are keyed on (pipeline_id, idx) and carry no
content_hash. Every route after ``/create`` names a row by ``pipeline_id``
or by ``content_hash`` narrowed by ``collection``/``pdf_path``; a hash-only
caller lands on the hash's ``keyed_by='content_hash'`` row only, never a
document row (see ``PipelineRepository.resolve``). ``/create`` without ``identity`` runs the
pipeline-001 one-row-per-hash algorithm verbatim (what a client older than
this engine sends); ``identity="document"`` keys on the document and resets
a leftover completed row instead of skipping it.

``clock`` is injectable for staleness tests (fixed clocks per house rule).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Callable

import httpx

from nexus.db.http_pipeline_client import STALE_THRESHOLD, HttpPipelineDB

_PROGRESS_FIELDS = {
    "total_pages", "pages_extracted", "chunks_created", "chunks_embedded", "chunks_uploaded",
}


class _StaleRun(Exception):
    """Fake-engine twin of ``PipelineStaleRunException`` (nexus-8vu8p)."""

    REMEDY = (
        "this run was taken over by a newer resume of the same document; stop "
        "without marking the row failed or clearing its WAL (the new owner "
        "holds both) and re-run the document if the new owner does not finish"
    )

    def __init__(self, pipeline_id: int, content_hash: str, run_epoch: int, current_epoch: int) -> None:
        self.pipeline_id = pipeline_id
        self.content_hash = content_hash
        self.run_epoch = run_epoch
        self.current_epoch = current_epoch
        super().__init__(
            f"pipeline_id={pipeline_id} (content_hash={content_hash}) is at run_epoch "
            f"{current_epoch}, this write carried {run_epoch} — {self.REMEDY}"
        )


class _ConflictRunning(Exception):
    """Fake-engine twin of ``PipelineConflictException`` (nexus-lcmbp).

    Carries the exact fields the Java ``HttpUtil.sendTypedDbError`` 409
    body puts on the wire, so :meth:`FakePipelineEngine.handler` can mirror
    the real engine's response shape.
    """

    def __init__(self, content_hash: str, started_at: str, heartbeat_age_seconds: int, stale_threshold_seconds: int) -> None:
        self.content_hash = content_hash
        self.started_at = started_at
        self.heartbeat_age_seconds = heartbeat_age_seconds
        self.stale_threshold_seconds = stale_threshold_seconds
        # nexus-lcmbp fix-list #5: this tail must stay textually identical to
        # the "remedy" field FakePipelineEngine.handler puts on the wire below
        # (mirrors PipelineConflictException.java / HttpUtil.java staying in
        # sync on the real engine side).
        super().__init__(
            f"pipeline for content_hash={content_hash} is already running "
            f"(last heartbeat {heartbeat_age_seconds}s ago; resumable once "
            f"the heartbeat exceeds {stale_threshold_seconds}s) — "
            "wait for the resume window (retry after the heartbeat exceeds "
            "the stale threshold) or inspect the pipeline row via "
            "GET /v1/pipeline/state (engine route; requires service auth)"
        )


class FakePipelineEngine:
    """Dict-backed twin of the three ``nexus.pdf_*`` tables.

    ``pipelines`` is keyed by ``pipeline_id``; ``pages``/``chunks`` by
    ``(pipeline_id, idx)``.
    """

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or (lambda: datetime.now(UTC))
        self.pipelines: dict[int, dict[str, Any]] = {}
        self.pages: dict[tuple[int, int], dict[str, Any]] = {}
        self.chunks: dict[tuple[int, int], dict[str, Any]] = {}
        self._next_id = 1

    # ── test conveniences ───────────────────────────────────────────────────

    def hashes(self) -> set[str]:
        """Every content_hash with at least one row."""
        return {r["content_hash"] for r in self.pipelines.values()}

    def rows_for(self, content_hash: str) -> list[dict[str, Any]]:
        """Every row for *content_hash*, oldest first."""
        return [r for _, r in sorted(self.pipelines.items()) if r["content_hash"] == content_hash]

    def row_for(self, content_hash: str) -> dict[str, Any]:
        """The one row for *content_hash*; asserts exactly one exists."""
        rows = self.rows_for(content_hash)
        assert len(rows) == 1, f"expected one row for {content_hash}, found {len(rows)}"
        return rows[0]

    def wal_hashes(self) -> set[str]:
        """content_hashes owning at least one page or chunk row."""
        owners = {pid for pid, _ in self.pages} | {pid for pid, _ in self.chunks}
        return {self.pipelines[pid]["content_hash"] for pid in owners if pid in self.pipelines}

    # ── resolution (PipelineRepository.resolve's twin) ──────────────────────

    def resolve(self, ref: dict) -> int | None:
        """The one row *ref* names, or ``None``: ``pipeline_id`` when given,
        else ``content_hash`` narrowed by any ``collection``/``pdf_path``
        (newest), else the bare hash's legacy (``keyed_by='content_hash'``)
        row only, never a document row."""
        pid = ref.get("pipeline_id")
        if pid not in (None, ""):
            pid = int(pid)
            return pid if pid in self.pipelines else None
        h = ref.get("content_hash")
        if not h:
            raise ValueError("'pipeline_id' or 'content_hash' is required")
        collection = ref.get("collection") or ""
        pdf_path = ref.get("pdf_path") or ""
        narrowed = bool(collection or pdf_path)
        candidates = [
            r for r in self.pipelines.values()
            if r["content_hash"] == h
            and (not collection or r["collection"] == collection)
            and (not pdf_path or r["pdf_path"] == pdf_path)
            # A bare hash names legacy rows only (an old client's own).
            and (narrowed or r["keyed_by"] == "content_hash")
        ]
        if not candidates:
            return None
        best = max(candidates, key=lambda r: (r["started_at"], r["pipeline_id"]))
        return best["pipeline_id"]

    def _lock_run(self, ref: dict) -> int | None:
        """PipelineRepository.lockRun's twin: the row, with the caller's
        ``run_epoch`` (when given) compared against the row's; a mismatch is
        a 409 stale_run and nothing is written."""
        pid = self.resolve(ref)
        if pid is None:
            return None
        epoch = ref.get("run_epoch")
        if epoch not in (None, ""):
            row = self.pipelines[pid]
            if int(epoch) != row["run_epoch"]:
                raise _StaleRun(pid, row["content_hash"], int(epoch), row["run_epoch"])
        return pid

    def _require_run(self, ref: dict) -> int:
        pid = self._lock_run(ref)
        if pid is None:
            raise ValueError(f"no pipeline row for {ref}")
        return pid

    # ── endpoint semantics ──────────────────────────────────────────────────

    def create(self, body: dict) -> dict:
        h = body["content_hash"]
        identity = body.get("identity")
        if identity in (None, "content_hash"):
            document = False
        elif identity == "document":
            document = True
        else:
            raise ValueError("'identity' must be \"document\" or \"content_hash\" when present")
        now_dt = self.clock()
        now = now_dt.isoformat()
        ref = (
            {"content_hash": h, "collection": body["collection"], "pdf_path": body["pdf_path"]}
            if document else {"content_hash": h}
        )
        pid = self.resolve(ref)
        if pid is None and not document:
            # No legacy row: the SAME document's document row, if any.
            pid = self.resolve({"content_hash": h, "collection": body["collection"], "pdf_path": body["pdf_path"]})
        if pid is None:
            pid = self._next_id
            self._next_id += 1
            self.pipelines[pid] = {
                "pipeline_id": pid,
                "content_hash": h, "pdf_path": body["pdf_path"],
                "collection": body["collection"],
                "keyed_by": "document" if document else "content_hash",
                "run_epoch": 0,
                "total_pages": None,
                "pages_extracted": 0, "chunks_created": None,
                "chunks_embedded": None, "chunks_uploaded": 0,
                "status": "running", "error": "", "extraction_meta": "",
                "started_at": now, "updated_at": now,
            }
            return {"status": "created", "pipeline_id": pid, "run_epoch": 0}
        row = self.pipelines[pid]
        if row["status"] == "completed":
            if not document:
                return {"status": "skip", "pipeline_id": pid, "run_epoch": row["run_epoch"]}
            # A leftover of a client that died between mark_completed and
            # delete: wipe its WAL, reset it, answer "created".
            self._delete_wal(pid)
            # A takeover: the epoch is BUMPED, never reset (a delayed write
            # from the completed run still holds the old one).
            row.update(
                status="running", keyed_by="document", total_pages=None,
                pages_extracted=0, chunks_created=None, chunks_embedded=None,
                chunks_uploaded=0, error="", extraction_meta="",
                run_epoch=row["run_epoch"] + 1,
                started_at=now, updated_at=now,
            )
            return {"status": "created", "pipeline_id": pid, "run_epoch": row["run_epoch"]}
        keyed_by = "document" if document else "content_hash"
        if row["status"] == "failed":
            row.update(status="resuming", keyed_by=keyed_by, run_epoch=row["run_epoch"] + 1, updated_at=now)
            return {"status": "resuming", "pipeline_id": pid, "run_epoch": row["run_epoch"]}
        age = now_dt - datetime.fromisoformat(row["updated_at"])
        stale = age > STALE_THRESHOLD
        if stale:
            row.update(status="resuming", keyed_by=keyed_by, run_epoch=row["run_epoch"] + 1, updated_at=now)
            return {"status": "resuming", "pipeline_id": pid, "run_epoch": row["run_epoch"]}
        # running with a fresh heartbeat — nexus-lcmbp: LOUD conflict, never
        # a silent "skip" (mirrors PipelineRepository.create's Java twin).
        raise _ConflictRunning(
            h, row["started_at"],
            int(age.total_seconds()), int(STALE_THRESHOLD.total_seconds()),
        )

    def state(self, params: dict) -> dict:
        pid = self.resolve(params)
        row = self.pipelines.get(pid) if pid is not None else None
        return {"pipeline": dict(row) if row else None}

    def write_pages(self, body: dict) -> dict:
        pid = self._require_run(body)
        now = self.clock().isoformat()
        for p in body["pages"]:
            self.pages[(pid, int(p["page_index"]))] = {
                "pipeline_id": pid, "page_index": int(p["page_index"]),
                "page_text": p["page_text"],
                "metadata_json": p.get("metadata_json", "{}"),
                "created_at": now,
            }
        return {"written": len(body["pages"])}

    def read_pages(self, params: dict) -> dict:
        pid = self.resolve(params)
        if pid is None:
            return {"pages": []}
        start = int(params.get("start", 0))
        rows = sorted(
            (dict(r) for (rp, idx), r in self.pages.items() if rp == pid and idx >= start),
            key=lambda r: r["page_index"],
        )
        return {"pages": rows}

    def write_chunks(self, body: dict) -> dict:
        pid = self._require_run(body)
        now = self.clock().isoformat()
        inserted = 0
        for c in body["chunks"]:
            key = (pid, int(c["chunk_index"]))
            if key in self.chunks:
                continue  # INSERT OR IGNORE / ON CONFLICT DO NOTHING
            self.chunks[key] = {
                "pipeline_id": pid, "chunk_index": int(c["chunk_index"]),
                "chunk_text": c["chunk_text"], "chunk_id": c["chunk_id"],
                "metadata_json": c.get("metadata_json", "{}"),
                "embedding": c.get("embedding"),  # wire form: None | "" | base64
                "uploaded": 0, "created_at": now,
            }
            inserted += 1
        return {"inserted": inserted}

    def read_chunks(self, params: dict) -> dict:
        pid = self.resolve(params)
        if pid is None:
            return {"chunks": []}
        uploadable = params.get("uploadable") in ("1", 1, True)
        limit = int(params.get("limit", 0))
        rows = sorted(
            (dict(r) for (rp, _), r in self.chunks.items() if rp == pid and r["uploaded"] == 0),
            key=lambda r: r["chunk_index"],
        )
        if uploadable:
            rows = [r for r in rows if r["embedding"] is not None]
        if limit > 0:
            rows = rows[:limit]
        return {"chunks": rows}

    def progress(self, body: dict) -> dict:
        fields = body["fields"]
        bad = set(fields) - _PROGRESS_FIELDS
        if bad:
            raise ValueError(f"Unknown progress fields: {bad}")
        pid = self._lock_run(body)
        row = self.pipelines.get(pid) if pid is not None else None
        if row is not None:
            row.update(fields)
            row["updated_at"] = self.clock().isoformat()
        return {"updated": True}

    def extraction_meta(self, body: dict) -> dict:
        pid = self._lock_run(body)
        row = self.pipelines.get(pid) if pid is not None else None
        if row is not None:
            row["extraction_meta"] = body["metadata_json"]
            row["updated_at"] = self.clock().isoformat()
        return {"updated": True}

    def complete(self, body: dict) -> dict:
        return self._set_status(body, "completed")

    def fail(self, body: dict) -> dict:
        return self._set_status(body, "failed", error=body.get("error", ""))

    def _set_status(self, ref: dict, status: str, *, error: str | None = None) -> dict:
        pid = self._lock_run(ref)  # one lock per logical write, as the engine
        row = self.pipelines.get(pid) if pid is not None else None
        if row is not None:
            row["status"] = status
            if error is not None:
                row["error"] = error
            row["updated_at"] = self.clock().isoformat()
        return {"updated": True}

    def mark_uploaded(self, body: dict) -> dict:
        pid = self._lock_run(body)
        if pid is None:
            return {"updated": 0}
        n = 0
        for idx in body["chunk_indices"]:
            row = self.chunks.get((pid, int(idx)))
            if row is not None:
                row["uploaded"] = 1
                n += 1
        return {"updated": n}

    def counts(self, params: dict) -> dict:
        # Mirrors PipelineHandler.handleCounts exactly: a call naming no row
        # at all yields embedded_chunks=0 (NOT a global sum) — the count is
        # per-pipeline-only by contract (.16 critic Significant #3).
        if not params.get("pipeline_id") and not params.get("content_hash"):
            embedded = 0
        else:
            pid = self.resolve(params)
            embedded = 0 if pid is None else sum(
                1 for (rp, _), r in self.chunks.items()
                if rp == pid and r["embedding"] is not None
            )
        return {"embedded_chunks": embedded, "pipelines": len(self.pipelines)}

    def clear_wal(self, body: dict) -> dict:
        pid = self._lock_run(body)  # lock and compare BEFORE the WAL wipe
        if pid is None:
            return {"cleared": True}
        self._delete_wal(pid)
        # nexus-33q80: zero chunks_uploaded/pages_extracted on the pipeline
        # row in the SAME call as the wipe, mirroring
        # PipelineRepository.clearOrphanWal's single transaction.
        row = self.pipelines[pid]
        row["chunks_uploaded"] = 0
        row["pages_extracted"] = 0
        row["updated_at"] = self.clock().isoformat()
        return {"cleared": True}

    def _delete_wal(self, pid: int) -> None:
        self.pages = {k: v for k, v in self.pages.items() if k[0] != pid}
        self.chunks = {k: v for k, v in self.chunks.items() if k[0] != pid}

    def delete(self, body: dict) -> dict:
        pid = self._lock_run(body)
        if pid is None:
            return {"deleted": False}
        self._delete_wal(pid)  # the FK cascade
        self.pipelines.pop(pid, None)
        return {"deleted": True}

    def delete_collection(self, body: dict) -> dict:
        collection = body["collection"]
        pids = [pid for pid, r in self.pipelines.items() if r["collection"] == collection]
        for pid in pids:
            self.delete({"pipeline_id": pid})
        return {"deleted": len(pids)}

    def list_pipelines(self, params: dict) -> dict:
        return {"pipelines": [dict(r) for _, r in sorted(self.pipelines.items())]}

    # ── httpx transport ─────────────────────────────────────────────────────

    _ROUTES = {
        ("POST", "/v1/pipeline/create"): "create",
        ("GET", "/v1/pipeline/state"): "state",
        ("POST", "/v1/pipeline/pages"): "write_pages",
        ("GET", "/v1/pipeline/pages"): "read_pages",
        ("POST", "/v1/pipeline/chunks"): "write_chunks",
        ("GET", "/v1/pipeline/chunks"): "read_chunks",
        ("POST", "/v1/pipeline/progress"): "progress",
        ("POST", "/v1/pipeline/extraction_meta"): "extraction_meta",
        ("POST", "/v1/pipeline/complete"): "complete",
        ("POST", "/v1/pipeline/fail"): "fail",
        ("POST", "/v1/pipeline/mark_uploaded"): "mark_uploaded",
        ("GET", "/v1/pipeline/counts"): "counts",
        ("POST", "/v1/pipeline/clear_wal"): "clear_wal",
        ("POST", "/v1/pipeline/delete"): "delete",
        ("POST", "/v1/pipeline/delete_collection"): "delete_collection",
        ("GET", "/v1/pipeline/list"): "list_pipelines",
    }

    def handler(self, request: httpx.Request) -> httpx.Response:
        method_name = self._ROUTES.get((request.method, request.url.path))
        if method_name is None:
            return httpx.Response(404, json={"error": f"no route {request.url.path}"})
        payload = (
            json.loads(request.content) if request.method == "POST"
            else dict(request.url.params)
        )
        try:
            return httpx.Response(200, json=getattr(self, method_name)(payload))
        except ValueError as exc:
            return httpx.Response(400, json={"error": str(exc)})
        except _StaleRun as exc:
            return httpx.Response(409, json={
                "error": str(exc),
                "status": "stale_run",
                "pipeline_id": exc.pipeline_id,
                "content_hash": exc.content_hash,
                "run_epoch": exc.run_epoch,
                "current_epoch": exc.current_epoch,
                "remedy": _StaleRun.REMEDY,
            })
        except _ConflictRunning as exc:
            return httpx.Response(409, json={
                "error": str(exc),
                "status": "conflict_running",
                "content_hash": exc.content_hash,
                "started_at": exc.started_at,
                "heartbeat_age_seconds": exc.heartbeat_age_seconds,
                "stale_threshold_seconds": exc.stale_threshold_seconds,
                "remedy": "wait for the resume window (retry after the "
                          "heartbeat exceeds the stale threshold) or inspect "
                          "the pipeline row via GET /v1/pipeline/state "
                          "(engine route; requires service auth)",
            })


def make_fake_engine_db(
    clock: Callable[[], datetime] | None = None,
) -> tuple[HttpPipelineDB, FakePipelineEngine]:
    """A real ``HttpPipelineDB`` wired to a fresh :class:`FakePipelineEngine`."""
    engine = FakePipelineEngine(clock=clock)
    db = HttpPipelineDB(base_url="http://fake-engine", _token="fake-token")
    db._client = httpx.Client(transport=httpx.MockTransport(engine.handler))
    db._clock = engine.clock  # one clock on both sides (deterministic staleness)
    return db, engine
