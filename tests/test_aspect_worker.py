# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-089 follow-up (nexus-qeo8): async aspect-extraction worker.

The worker drains ``aspect_extraction_queue``, calls
``extract_aspects``, and writes results to ``document_aspects``.
The hook ``aspect_extraction_enqueue_hook`` is registered as a
``post_document_hook`` and writes one queue row per fired document
(microsecond-scale; ingest is unblocked).

Worker contract:

* Single worker per process (singleton, lazy-started).
* Daemon thread; dies with the process — durable queue absorbs
  process death.
* Polls every ``poll_interval`` seconds.
* On extract success → ``document_aspects`` upsert, queue
  ``mark_done`` (DELETE).
* On extract returning ``None`` (unsupported collection) → queue
  ``mark_done`` (drop silently).
* On extract returning a null-fields record (extractor's internal
  3-retry budget exhausted) → ``document_aspects`` upsert with
  null fields, queue ``mark_done``. The extractor already retried;
  the worker must NOT retry again.
* On uncaught exception in the worker body (T2 failure, programming
  bug) → queue ``mark_failed``. Triage via ``nx taxonomy status`` /
  manual sweep.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.db.t2 import T2Database


@pytest.fixture(autouse=True)
def _reset_worker():
    """Tear down the worker singleton between tests so each test sees
    a fresh module state.
    """
    from nexus.aspect_worker import reset_worker_for_tests
    reset_worker_for_tests()
    yield
    reset_worker_for_tests()


@pytest.fixture(autouse=True)
def _isolate_t2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point t2_ctx at a tmp_path-scoped DB so worker and test
    share the same SQLite file."""
    import nexus.mcp_infra as infra
    db_path = tmp_path / "worker_t2.db"
    monkeypatch.setattr(infra, "t2_ctx", lambda: T2Database(db_path))
    return db_path


# ── Worker drain happy path ──────────────────────────────────────────────────


class TestWorkerDrain:
    def test_worker_drains_pending_queue(self, _isolate_t2: Path) -> None:
        """A row enqueued before the worker starts is drained: the row
        is deleted from the queue and a document_aspects row exists."""
        from nexus.aspect_extractor import AspectRecord
        from nexus.aspect_worker import AspectExtractionWorker

        # Enqueue a row.
        with T2Database(_isolate_t2) as db:
            db.aspect_queue.enqueue("knowledge__delos", "/p1.pdf")

        def fake_extract(content, source_path, collection, **_kw):
            return AspectRecord(
                collection=collection,
                source_path=source_path,
                problem_formulation="P",
                proposed_method="M",
                experimental_datasets=["d1"],
                experimental_baselines=["b1"],
                experimental_results="R",
                extras={"venue": "V"},
                confidence=0.9,
                extracted_at="2026-04-26T00:00:00+00:00",
                model_version="claude-haiku-4-5-20251001",
                extractor_name="scholarly-paper-v1",
            )

        with patch("nexus.aspect_worker._extract_aspects", fake_extract):
            worker = AspectExtractionWorker(poll_interval=0.05)
            worker.start()
            try:
                _wait_until(
                    lambda: _queue_size(_isolate_t2) == 0, timeout=5.0,
                )
            finally:
                worker.stop(timeout=5.0)

        with T2Database(_isolate_t2) as db:
            rec = db.document_aspects.get("knowledge__delos", "/p1.pdf")
        assert rec is not None
        assert rec.problem_formulation == "P"
        assert rec.proposed_method == "M"

    def test_worker_unsupported_collection_drops_silently(
        self, _isolate_t2: Path,
    ) -> None:
        """If extract_aspects returns ``None`` (unsupported collection
        OR transient failure), the worker still mark_dones the queue
        row so it doesn't loop forever. No document_aspects row is
        written. Uses code__* — unsupported by design — to avoid
        coupling to whichever prefix is currently in the registry."""
        from nexus.aspect_worker import AspectExtractionWorker

        with T2Database(_isolate_t2) as db:
            db.aspect_queue.enqueue("code__nexus", "/p1.py")

        def fake_extract(content, source_path, collection, **_kw):
            return None  # unsupported collection

        with patch("nexus.aspect_worker._extract_aspects", fake_extract):
            worker = AspectExtractionWorker(poll_interval=0.05)
            worker.start()
            try:
                _wait_until(
                    lambda: _queue_size(_isolate_t2) == 0, timeout=5.0,
                )
            finally:
                worker.stop(timeout=5.0)

        with T2Database(_isolate_t2) as db:
            rec = db.document_aspects.get("code__nexus", "/p1.py")
        assert rec is None

    def test_worker_null_fields_record_persists_without_retry(
        self, _isolate_t2: Path,
    ) -> None:
        """If extract_aspects returned a null-fields record, the
        extractor's internal 3-retry budget is exhausted. The worker
        writes the null record + drops the queue row (no retry —
        retrying would re-attempt 3× more for no benefit)."""
        from nexus.aspect_extractor import AspectRecord
        from nexus.aspect_worker import AspectExtractionWorker

        with T2Database(_isolate_t2) as db:
            db.aspect_queue.enqueue("knowledge__delos", "/p1.pdf")

        call_count = [0]

        def fake_extract(content, source_path, collection, **_kw):
            call_count[0] += 1
            return AspectRecord(
                collection=collection,
                source_path=source_path,
                problem_formulation=None,
                proposed_method=None,
                experimental_datasets=[],
                experimental_baselines=[],
                experimental_results=None,
                extras={},
                confidence=None,
                extracted_at="2026-04-26T00:00:00+00:00",
                model_version="claude-haiku-4-5-20251001",
                extractor_name="scholarly-paper-v1",
            )

        with patch("nexus.aspect_worker._extract_aspects", fake_extract):
            worker = AspectExtractionWorker(poll_interval=0.05)
            worker.start()
            try:
                _wait_until(
                    lambda: _queue_size(_isolate_t2) == 0, timeout=5.0,
                )
            finally:
                worker.stop(timeout=5.0)

        # extract_aspects called exactly once — no worker-level retry.
        assert call_count[0] == 1
        with T2Database(_isolate_t2) as db:
            rec = db.document_aspects.get("knowledge__delos", "/p1.pdf")
        assert rec is not None
        assert rec.problem_formulation is None
        assert rec.extractor_name == "scholarly-paper-v1"

    def test_mcp_path_content_survives_the_queue(
        self, _isolate_t2: Path,
    ) -> None:
        """Critical-issue regression test (substantive critic finding):
        MCP ``store_put`` passes ``content=<full text>`` and
        ``source_path=<doc_id>``. The doc_id is a 16-char hex hash,
        not a filesystem path. The worker must therefore use the
        content the hook captured at enqueue time — re-reading
        source_path would attempt to open a non-existent file,
        produce a null-fields record, and silently lose the
        extraction.
        """
        from nexus.aspect_extractor import AspectRecord
        from nexus.aspect_worker import (
            AspectExtractionWorker,
            aspect_extraction_enqueue_hook,
        )

        # Simulate the MCP store_put boundary: source_path is a
        # 16-char content-hash doc_id, content is the full text.
        doc_id = "a3b9c2d1e4f5a6b7"
        full_text = "Paper introduces a new approach to BFT consensus."
        aspect_extraction_enqueue_hook(
            source_path=doc_id,
            collection="knowledge__delos",
            content=full_text,
        )

        # Capture what extract_aspects sees.
        seen: list[tuple[str, str]] = []

        def fake_extract(content, source_path, collection, **_kw):
            seen.append((content, source_path))
            return AspectRecord(
                collection=collection,
                source_path=source_path,
                problem_formulation="P",
                proposed_method="M",
                experimental_datasets=["d1"],
                experimental_baselines=["b1"],
                experimental_results="R",
                extras={"venue": "V"},
                confidence=0.9,
                extracted_at="2026-04-26T00:00:00+00:00",
                model_version="claude-haiku-4-5-20251001",
                extractor_name="scholarly-paper-v1",
            )

        with patch("nexus.aspect_worker._extract_aspects", fake_extract):
            worker = AspectExtractionWorker(poll_interval=0.05)
            worker.start()
            try:
                _wait_until(
                    lambda: _queue_size(_isolate_t2) == 0, timeout=5.0,
                )
            finally:
                worker.stop(timeout=5.0)

        # The worker MUST have called extract_aspects with the
        # captured content, NOT with content="" (which would force a
        # file-read fallback against a non-existent path).
        assert seen
        captured_content, captured_source = seen[0]
        assert captured_content == full_text, (
            "MCP-path content was not propagated through the queue: "
            "the worker received an empty string and would fall back "
            "to reading the doc_id as a filesystem path."
        )
        assert captured_source == doc_id

        # The aspect record landed in document_aspects with the doc_id
        # as source_path (the MCP boundary identifier).
        with T2Database(_isolate_t2) as db:
            rec = db.document_aspects.get("knowledge__delos", doc_id)
        assert rec is not None
        assert rec.problem_formulation == "P"

    def test_worker_uncaught_exception_marks_failed(
        self, _isolate_t2: Path,
    ) -> None:
        """A genuinely-broken state (T2 connection lost, programming
        bug) raises and is caught at the top of the worker loop. The
        row is marked ``failed`` for triage and the worker keeps
        running."""
        from nexus.aspect_worker import AspectExtractionWorker

        with T2Database(_isolate_t2) as db:
            db.aspect_queue.enqueue("knowledge__delos", "/p1.pdf")

        def fake_extract(content, source_path, collection, **_kw):
            raise RuntimeError("worker-level failure (not extractor)")

        with patch("nexus.aspect_worker._extract_aspects", fake_extract):
            worker = AspectExtractionWorker(poll_interval=0.05)
            worker.start()
            try:
                _wait_until(
                    lambda: _row_status(_isolate_t2, "/p1.pdf") == "failed",
                    timeout=5.0,
                )
            finally:
                worker.stop(timeout=5.0)

        with T2Database(_isolate_t2) as db:
            row = db.aspect_queue.conn.execute(
                "SELECT status, retry_count, last_error "
                "FROM aspect_extraction_queue WHERE source_path = ?",
                ("/p1.pdf",),
            ).fetchone()
        assert row[0] == "failed"
        assert row[1] >= 1
        assert "worker-level failure" in row[2]


# ── Worker lifecycle ─────────────────────────────────────────────────────────


class TestWorkerLifecycle:
    def test_worker_stop_is_clean(self, _isolate_t2: Path) -> None:
        """``stop()`` signals the worker and joins quickly; no zombie
        threads."""
        from nexus.aspect_worker import AspectExtractionWorker

        worker = AspectExtractionWorker(poll_interval=0.05)
        worker.start()
        assert worker.is_running()
        worker.stop(timeout=2.0)
        assert not worker.is_running()

    def test_double_start_is_idempotent(self, _isolate_t2: Path) -> None:
        """Calling ``start()`` twice does not spawn two threads."""
        from nexus.aspect_worker import AspectExtractionWorker

        worker = AspectExtractionWorker(poll_interval=0.05)
        worker.start()
        thread_1 = worker._thread
        worker.start()
        thread_2 = worker._thread
        try:
            assert thread_1 is thread_2
        finally:
            worker.stop(timeout=2.0)

    def test_ensure_worker_started_lazy_singleton(
        self, _isolate_t2: Path,
    ) -> None:
        """``ensure_worker_started`` returns the same singleton across
        calls and starts it on first call."""
        from nexus.aspect_worker import (
            ensure_worker_started,
            get_worker,
            stop_worker,
        )

        try:
            assert get_worker() is None
            ensure_worker_started()
            w1 = get_worker()
            assert w1 is not None
            assert w1.is_running()
            ensure_worker_started()  # idempotent
            assert get_worker() is w1
        finally:
            stop_worker(timeout=2.0)


# ── Enqueue hook ─────────────────────────────────────────────────────────────


class TestEnqueueHook:
    def test_hook_enqueues_supported_collection(
        self, _isolate_t2: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The registered hook writes a pending row for knowledge__*
        collections.

        Patches out ``ensure_worker_started`` so the worker doesn't
        race the test thread and drain the row before the assertion
        can see it. Worker-side behaviour is covered by the
        ``TestWorkerDrain`` suite above."""
        import nexus.aspect_worker as mod
        from nexus.aspect_worker import aspect_extraction_enqueue_hook

        monkeypatch.setattr(mod, "ensure_worker_started", lambda: None)

        aspect_extraction_enqueue_hook(
            source_path="/p1.pdf",
            collection="knowledge__delos",
            content="some text",
        )
        with T2Database(_isolate_t2) as db:
            rows = db.aspect_queue.list_pending()
        assert len(rows) == 1
        assert rows[0].source_path == "/p1.pdf"
        assert rows[0].collection == "knowledge__delos"

    def test_hook_skips_unsupported_collection(
        self, _isolate_t2: Path,
    ) -> None:
        """Hook is a no-op for collections that have no extractor
        config — no queue row, no worker started, no T3 read.

        ``code__*`` is unsupported by design: aspect extraction
        targets prose claims (problem_formulation, proposed_method,
        etc.), which don't map to source-code AST chunks. After #377,
        ``docs__*`` is no longer the canonical 'unsupported' example
        — it routes to scholarly-paper-v1 like ``knowledge__*``.
        """
        from nexus.aspect_worker import (
            aspect_extraction_enqueue_hook,
            get_worker,
        )

        aspect_extraction_enqueue_hook(
            source_path="/p1.py",
            collection="code__nexus",
            content="def foo(): pass",
        )
        with T2Database(_isolate_t2) as db:
            rows = db.aspect_queue.list_pending()
        assert rows == []
        assert get_worker() is None  # no lazy start either

    def test_hook_starts_worker_on_first_supported_enqueue(
        self, _isolate_t2: Path,
    ) -> None:
        """The hook lazy-starts the worker on the first supported
        enqueue. Subsequent enqueues do not re-start."""
        from nexus.aspect_worker import (
            aspect_extraction_enqueue_hook,
            get_worker,
            stop_worker,
        )

        try:
            assert get_worker() is None
            aspect_extraction_enqueue_hook(
                source_path="/p1.pdf",
                collection="knowledge__delos",
                content="x",
            )
            w1 = get_worker()
            assert w1 is not None
            assert w1.is_running()
            aspect_extraction_enqueue_hook(
                source_path="/p2.pdf",
                collection="knowledge__delos",
                content="y",
            )
            assert get_worker() is w1
        finally:
            stop_worker(timeout=2.0)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _wait_until(predicate, timeout: float = 5.0, poll: float = 0.05) -> None:
    """Poll ``predicate`` until True or raise."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(poll)
    raise AssertionError(f"predicate not satisfied within {timeout}s")


def _queue_size(db_path: Path) -> int:
    with T2Database(db_path) as db:
        return db.aspect_queue.conn.execute(
            "SELECT COUNT(*) FROM aspect_extraction_queue"
        ).fetchone()[0]


def _row_status(db_path: Path, source_path: str) -> str | None:
    with T2Database(db_path) as db:
        row = db.aspect_queue.conn.execute(
            "SELECT status FROM aspect_extraction_queue WHERE source_path = ?",
            (source_path,),
        ).fetchone()
    return row[0] if row else None


# ── Batch path (RDR-089 Phase D) ────────────────────────────────────────────


class TestBatchPath:
    """Worker drains in batches when queue depth allows it."""

    def test_worker_drains_batch_in_one_extract_call(
        self, _isolate_t2: Path,
    ) -> None:
        """Five enqueues, batch_size=5: the worker calls
        extract_aspects_batch ONCE for all five, not five times."""
        from nexus.aspect_extractor import AspectRecord
        from nexus.aspect_worker import AspectExtractionWorker

        # Enqueue directly to T2 (bypass the hook so no worker is
        # lazy-spawned before the patches are in place).
        with T2Database(_isolate_t2) as db:
            for i in range(5):
                db.aspect_queue.enqueue(
                    "knowledge__delos",
                    f"/papers/p{i}.pdf",
                    content=f"content {i}",
                )

        batch_calls: list[int] = []
        single_calls: list[int] = []

        def fake_batch(items):
            batch_calls.append(len(items))
            return [
                AspectRecord(
                    collection=c, source_path=sp,
                    problem_formulation=f"P-{sp}",
                    proposed_method="M",
                    experimental_datasets=["d"],
                    experimental_baselines=["b"],
                    experimental_results="R",
                    extras={}, confidence=0.9,
                    extracted_at="2026-04-26T00:00:00+00:00",
                    model_version="claude-haiku-4-5-20251001",
                    extractor_name="scholarly-paper-v1",
                )
                for c, sp, _content in items
            ]

        def fake_single(content, source_path, collection, **_kw):
            single_calls.append(source_path)
            raise AssertionError("single path should not fire on batch>=2")

        with patch("nexus.aspect_worker._extract_aspects_batch", fake_batch), \
             patch("nexus.aspect_worker._extract_aspects", fake_single):
            worker = AspectExtractionWorker(
                poll_interval=0.05, batch_size=5,
            )
            worker.start()
            try:
                _wait_until(
                    lambda: _queue_size(_isolate_t2) == 0, timeout=5.0,
                )
            finally:
                worker.stop(timeout=5.0)

        # Exactly one batch call covering all 5 papers.
        assert batch_calls == [5]
        assert single_calls == []

        # All five aspect rows landed.
        with T2Database(_isolate_t2) as db:
            count = db.document_aspects.conn.execute(
                "SELECT COUNT(*) FROM document_aspects"
            ).fetchone()[0]
        assert count == 5

    def test_worker_uses_single_path_when_only_one_row(
        self, _isolate_t2: Path,
    ) -> None:
        """One enqueue, batch_size=5: worker's single-row path fires
        rather than the batch path (no overhead amortisation
        benefit for one paper)."""
        from nexus.aspect_extractor import AspectRecord
        from nexus.aspect_worker import AspectExtractionWorker

        # Enqueue directly to T2 (bypass the hook).
        with T2Database(_isolate_t2) as db:
            db.aspect_queue.enqueue(
                "knowledge__delos", "/p1.pdf",
                content="content 1",
            )

        batch_calls: list[int] = []
        single_calls: list[str] = []

        def fake_batch(items):
            batch_calls.append(len(items))
            raise AssertionError("batch path should not fire for 1 row")

        def fake_single(content, source_path, collection, **_kw):
            single_calls.append(source_path)
            return AspectRecord(
                collection=collection, source_path=source_path,
                problem_formulation="P",
                proposed_method="M",
                experimental_datasets=["d"],
                experimental_baselines=["b"],
                experimental_results="R",
                extras={}, confidence=0.9,
                extracted_at="2026-04-26T00:00:00+00:00",
                model_version="claude-haiku-4-5-20251001",
                extractor_name="scholarly-paper-v1",
            )

        with patch("nexus.aspect_worker._extract_aspects_batch", fake_batch), \
             patch("nexus.aspect_worker._extract_aspects", fake_single):
            worker = AspectExtractionWorker(
                poll_interval=0.05, batch_size=5,
            )
            worker.start()
            try:
                _wait_until(
                    lambda: _queue_size(_isolate_t2) == 0, timeout=5.0,
                )
            finally:
                worker.stop(timeout=5.0)

        assert batch_calls == []
        assert single_calls == ["/p1.pdf"]
