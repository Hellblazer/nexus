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
    """Point the indexer's T2 write paths at a tmp_path-scoped DB so worker
    and test share the same SQLite file.

    RDR-128 P1 (kg8sj): the enqueue hook now routes through
    ``t2_index_write`` (daemon-or-direct), so isolate that too — here it
    writes directly to the tmp DB (daemon routing is covered by
    ``tests/test_rdr128_p1_index_write_routing.py``). ``t2_ctx`` stays
    patched for the tests that open the queue directly.
    """
    import nexus.mcp_infra as infra
    db_path = tmp_path / "worker_t2.db"
    monkeypatch.setattr(infra, "t2_ctx", lambda: T2Database(db_path))

    def _direct_index_write(write_fn):  # noqa: ANN001
        # RDR-128 P3: t2_index_write returns write_fn's result so the
        # worker's routed poll can consume claim_batch's rows. Mirror that.
        with T2Database(db_path) as db:
            return write_fn(db)

    monkeypatch.setattr(infra, "t2_index_write", _direct_index_write)
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

    def test_queuerow_roundtrips_through_daemon_wire_shape(self) -> None:
        """RDR-128 P3: routed through the daemon, ``claim_batch`` rows arrive
        as plain dicts (``t2_daemon._t2_decode`` returns a dataclass's fields
        as a dict, not a reconstructed object). The worker's poll relies on
        ``QueueRow(**wire_dict)`` to restore object attribute access. Pin that
        round-trip so a QueueRow field change can't silently break the routed
        worker (the direct-fallback path returns objects and would mask it)."""
        import dataclasses

        from nexus.db.t2.aspect_extraction_queue import QueueRow

        original = QueueRow(
            collection="knowledge__delos",
            source_path="/p1.pdf",
            content_hash="abc123",
            content="",
            retry_count=2,
            doc_id="1.2.3",
        )
        # The daemon wire protocol hands the client the dataclass's fields
        # as a plain dict; reconstruct exactly as the worker poll does.
        wire_dict = dataclasses.asdict(original)
        assert not isinstance(wire_dict, QueueRow)
        restored = QueueRow(**wire_dict)
        assert restored == original
        assert restored.collection == "knowledge__delos"
        assert restored.retry_count == 2
        assert restored.doc_id == "1.2.3"

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

    def test_worker_null_confidence_record_dropped_without_retry(
        self, _isolate_t2: Path,
    ) -> None:
        """nexus-17wf: a null-fields record (the failure shape that
        the extractor's internal 3-retry budget produces) carries
        ``confidence=None``. The DocumentAspects upsert must DROP
        it (no row written) so downstream consumers don't treat
        a failed extraction as authoritative; the worker still
        marks the queue row done (no worker-level retry, since
        retrying would re-attempt 3 more times for no benefit).

        Pre-fix: the null record was persisted, polluting 16.6%
        of the table per 2026-05-08 prod probe. Reverting the
        confidence-floor check in DocumentAspects.upsert lands
        the row again and this test fails on the ``rec is None``
        assertion below.
        """
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
        # The null-confidence record must NOT be persisted (nexus-17wf).
        with T2Database(_isolate_t2) as db:
            rec = db.document_aspects.get("knowledge__delos", "/p1.pdf")
        assert rec is None, (
            "nexus-17wf: confidence=None record must be dropped at "
            "upsert (was committed pre-fix, polluting downstream "
            "consumers); reverting the floor check fails this assert"
        )

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
        self, _isolate_t2: Path, monkeypatch,
    ) -> None:
        """The hook lazy-starts the worker on the first supported
        enqueue. Subsequent enqueues do not re-start."""
        # Exercises the hook's auto-spawn path specifically, so opt back into
        # autostart (conftest disables it suite-wide to drop the per-test
        # worker-stop teardown tax — nexus test-suite trim).
        monkeypatch.setenv("NX_ASPECT_WORKER_AUTOSTART", "1")
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


class TestGap2SourcePathNormalization:
    """RDR-145 Phase 1 (P1.2, nexus-syga3): forward-only ``source_path``
    canonicalization in ``aspect_extraction_enqueue_hook``.

    The hook resolves a file-backed ``source_path`` against the catalog
    BEFORE writing the queue row. On a hit whose stored ``file_path`` is a
    cleaner relative form it canonicalizes; on a miss it leaves the path
    AS-IS and emits a loud ``aspect_source_path_uncanonical`` warning (the
    tripwire — it NEVER synthesizes a path, which would re-introduce the
    ``nexus-3e4s`` CWD-anchoring class). Note-backed rows (``source_path``
    is a 32-hex chash, not a filesystem path) are skipped entirely so the
    probe never false-warns on a correct chash.

    Forward-only: existing ``document_aspects`` rows are never touched (that
    is the one-time migration cleanup, RDR-145 Phase 2 / ``nexus-nx9nx``).
    """

    @staticmethod
    def _seed_catalog(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        collection: str,
        file_path: str,
        title: str,
    ) -> None:
        """Build a real local catalog at a tmp dir, register one document,
        and inject it into the hook via ``_resolve_catalog_reader``.

        Injection (not env discovery) keeps the test hermetic and immune to
        the ambient storage backend: ``make_catalog_reader()`` returns an HTTP
        client to the live service whenever the shell is in service mode, so
        env-seeding a tmp catalog would be silently ignored. The injected
        object is a real :class:`Catalog`, so ``lookup_doc_id_by_collection_and_path``
        and ``by_doc_id`` exercise production code, not a mock."""
        import hashlib

        import nexus.aspect_worker as mod
        from nexus.catalog import Catalog

        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        (cat_dir / "owners.jsonl").touch()
        (cat_dir / "documents.jsonl").touch()
        (cat_dir / "links.jsonl").touch()
        repo_root = str(tmp_path / "repo")
        (tmp_path / "repo").mkdir()
        repo_hash = hashlib.sha256(repo_root.encode()).hexdigest()[:8]
        cat = Catalog(cat_dir, cat_dir / ".catalog.db")
        owner = cat.register_owner(
            "seed-repo", "repo", repo_hash=repo_hash, repo_root=repo_root,
        )
        cat.register(
            owner, title, content_type="paper",
            file_path=file_path, physical_collection=collection,
        )
        monkeypatch.setattr(mod, "_resolve_catalog_reader", lambda: cat)

    def test_resolving_absolute_path_normalizes_to_canonical_relative(
        self, _isolate_t2: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A file-backed absolute ``source_path`` that resolves to a catalog
        entry whose stored ``file_path`` is the relative canonical form is
        normalized to that relative form before the queue row is written.

        CONTRIVANCE NOTE: this registers the doc with ``title == abs_path`` so
        the catalog probe hits on its ``title = ?`` leg. The REAL Bucket-B
        contamination population does NOT have a title equal to its absolute
        path, so in production those rows MISS the probe and land in
        ``test_unresolved_path_left_as_is_and_warns`` (the warn branch is the
        realistic representative). This test exists to exercise the normalize
        branch through real Catalog/T2/SQL — it is a branch-coverage test, not
        evidence that normalization fires on real ingest."""
        import nexus.aspect_worker as mod
        from nexus.aspect_worker import aspect_extraction_enqueue_hook

        monkeypatch.setattr(mod, "ensure_worker_started", lambda: None)
        abs_path = "/Users/somebody/git/nexus-clone/papers/foo.md"
        self._seed_catalog(
            tmp_path, monkeypatch,
            collection="knowledge__delos",
            file_path="papers/foo.md",  # catalog canonical (relative)
            title=abs_path,             # contrived: resolver hits on title probe
        )
        aspect_extraction_enqueue_hook(
            source_path=abs_path, collection="knowledge__delos", content="x",
        )
        with T2Database(_isolate_t2) as db:
            rows = db.aspect_queue.list_pending()
        assert len(rows) == 1
        assert rows[0].source_path == "papers/foo.md"

    def test_unresolved_path_left_as_is_and_warns(
        self, _isolate_t2: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A file-backed ``source_path`` that does NOT resolve is left
        unchanged and a loud ``aspect_source_path_uncanonical`` warning is
        emitted (never silently rewritten to a guessed path)."""
        from structlog.testing import capture_logs

        import nexus.aspect_worker as mod
        from nexus.aspect_worker import aspect_extraction_enqueue_hook

        monkeypatch.setattr(mod, "ensure_worker_started", lambda: None)
        self._seed_catalog(
            tmp_path, monkeypatch,
            collection="knowledge__delos",
            file_path="papers/known.md",
            title="papers/known.md",
        )
        stray = "/Users/nobody/git/nexus-ghost/papers/stray.md"
        with capture_logs() as cap:
            aspect_extraction_enqueue_hook(
                source_path=stray, collection="knowledge__delos", content="x",
            )
        with T2Database(_isolate_t2) as db:
            rows = db.aspect_queue.list_pending()
        assert len(rows) == 1
        assert rows[0].source_path == stray  # unchanged — never guessed
        assert any(
            e.get("event") == "aspect_source_path_uncanonical"
            and e.get("source_path") == stray
            and e.get("collection") == "knowledge__delos"
            for e in cap
        )

    def test_note_backed_chash_source_path_untouched_no_warn(
        self, _isolate_t2: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A note-backed row whose ``source_path`` is a 32-hex chash is left
        unchanged with NO uncanonical warning — chashes are not filesystem
        paths (RDR-172 owns note identity via ``doc_id``)."""
        from structlog.testing import capture_logs

        import nexus.aspect_worker as mod
        from nexus.aspect_worker import aspect_extraction_enqueue_hook

        monkeypatch.setattr(mod, "ensure_worker_started", lambda: None)
        self._seed_catalog(
            tmp_path, monkeypatch,
            collection="knowledge__knowledge",
            file_path="",  # note-backed: no file_path
            title="A session note",
        )
        chash = "a" * 32
        with capture_logs() as cap:
            aspect_extraction_enqueue_hook(
                source_path=chash, collection="knowledge__knowledge", content="x",
            )
        with T2Database(_isolate_t2) as db:
            rows = db.aspect_queue.list_pending()
        assert len(rows) == 1
        assert rows[0].source_path == chash
        assert not any(
            e.get("event") == "aspect_source_path_uncanonical" for e in cap
        )

    def test_catalog_reader_unavailable_is_silent_noop(
        self, _isolate_t2: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the catalog reader cannot be built (raises), the hook degrades
        to a no-op: source_path unchanged, NO uncanonical warning (absence of
        a catalog is not an uncanonical path)."""
        from structlog.testing import capture_logs

        import nexus.aspect_worker as mod
        from nexus.aspect_worker import aspect_extraction_enqueue_hook

        monkeypatch.setattr(mod, "ensure_worker_started", lambda: None)

        def _boom():
            raise RuntimeError("catalog unavailable")

        monkeypatch.setattr(mod, "_resolve_catalog_reader", _boom)
        path = "/Users/x/git/repo/papers/foo.md"
        with capture_logs() as cap:
            aspect_extraction_enqueue_hook(
                source_path=path, collection="knowledge__delos", content="x",
            )
        with T2Database(_isolate_t2) as db:
            rows = db.aspect_queue.list_pending()
        assert len(rows) == 1
        assert rows[0].source_path == path
        assert not any(
            e.get("event") == "aspect_source_path_uncanonical" for e in cap
        )

    def test_resolved_but_canonical_is_absolute_left_as_is(
        self, _isolate_t2: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A path that resolves but whose catalog file_path is itself absolute
        is NOT normalized (we only canonicalize toward a relative form) — and
        no uncanonical warning fires (it did resolve)."""
        from structlog.testing import capture_logs

        import nexus.aspect_worker as mod
        from nexus.aspect_worker import aspect_extraction_enqueue_hook

        monkeypatch.setattr(mod, "ensure_worker_started", lambda: None)
        # Canonical file_path is absolute (but inside repo_root so it clears
        # the register-time cross-project guard); the stray probe resolves via
        # the title. canonical is absolute and DIFFERS -> the isabs leg blocks
        # normalization and the path is left as-is.
        abs_canonical = str(tmp_path / "repo" / "papers" / "canon.md")
        stray = "/Users/x/git/clone/papers/stray.md"
        self._seed_catalog(
            tmp_path, monkeypatch,
            collection="knowledge__delos",
            file_path=abs_canonical, title=stray,
        )
        with capture_logs() as cap:
            aspect_extraction_enqueue_hook(
                source_path=stray, collection="knowledge__delos", content="x",
            )
        with T2Database(_isolate_t2) as db:
            rows = db.aspect_queue.list_pending()
        assert len(rows) == 1
        assert rows[0].source_path == stray  # absolute canonical -> not normalized
        assert not any(
            e.get("event") == "aspect_source_path_uncanonical" for e in cap
        )


class TestEnqueueFailureTripwire:
    """RDR-172 P2.1 (nexus-hlkvj): loudness tripwire on enqueue failure.

    ``aspect_extraction_enqueue_hook`` keeps the enqueue best-effort (never
    block ingest, RDR-089 P0.1), but that internal swallow also hides the
    failure from ``hook_registry``'s ``hook_failures`` recorder — the silent-
    total-failure class (RF-7, the nexus-ov0sw bug). The hook therefore
    persists its OWN ``hook_failures`` row inside the except block so CI /
    --fullstack can assert zero enqueue failures across an ingest E2E. The
    persist is itself best-effort: a telemetry-write failure never blocks
    ingest.
    """

    def test_enqueue_failure_records_hook_failures_row(
        self, _isolate_t2: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """NON-VACUITY (RF-7): a forced enqueue failure WRITES a structured
        hook_failures row keyed ``hook_name='aspect_extraction_enqueue_hook'``
        — the tripwire is proven to increment, not just assert-zero on green."""
        import nexus.aspect_worker as mod
        from nexus.aspect_worker import aspect_extraction_enqueue_hook
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        monkeypatch.setattr(mod, "ensure_worker_started", lambda: None)
        monkeypatch.setattr(mod, "_resolve_catalog_reader", lambda: None)

        def _boom(self, *a, **k):
            raise RuntimeError("enqueue boom")

        monkeypatch.setattr(AspectExtractionQueue, "enqueue", _boom)

        aspect_extraction_enqueue_hook(
            source_path="/p1.pdf", collection="knowledge__delos", content="x",
        )
        with T2Database(_isolate_t2) as db:
            rows = db.telemetry.conn.execute(
                "SELECT doc_id, collection, hook_name, error, chain "
                "FROM hook_failures"
            ).fetchall()
        assert len(rows) == 1
        doc_id, coll, hook_name, error, chain = rows[0]
        assert doc_id == "/p1.pdf"
        assert coll == "knowledge__delos"
        assert hook_name == "aspect_extraction_enqueue_hook"
        assert "enqueue boom" in error
        assert chain == "document"

    def test_successful_enqueue_writes_no_hook_failures(
        self, _isolate_t2: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ASSERT-ZERO baseline (the CI invariant): a normal enqueue writes a
        queue row and ZERO hook_failures rows."""
        import nexus.aspect_worker as mod
        from nexus.aspect_worker import aspect_extraction_enqueue_hook

        monkeypatch.setattr(mod, "ensure_worker_started", lambda: None)
        monkeypatch.setattr(mod, "_resolve_catalog_reader", lambda: None)

        aspect_extraction_enqueue_hook(
            source_path="/p1.pdf", collection="knowledge__delos", content="x",
        )
        with T2Database(_isolate_t2) as db:
            pending = db.aspect_queue.list_pending()
            failures = db.telemetry.conn.execute(
                "SELECT count(*) FROM hook_failures "
                "WHERE hook_name = 'aspect_extraction_enqueue_hook'"
            ).fetchone()[0]
        assert len(pending) == 1
        assert failures == 0

    def test_tripwire_persist_failure_never_blocks_ingest(
        self, _isolate_t2: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Best-effort: if BOTH the enqueue AND the tripwire persist raise, the
        hook still returns without propagating (ingest is never blocked)."""
        import nexus.aspect_worker as mod
        from nexus.aspect_worker import aspect_extraction_enqueue_hook
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue
        from nexus.db.t2.telemetry import Telemetry

        monkeypatch.setattr(mod, "ensure_worker_started", lambda: None)
        monkeypatch.setattr(mod, "_resolve_catalog_reader", lambda: None)

        def _boom(self, *a, **k):
            raise RuntimeError("enqueue boom")

        def _boom_telemetry(self, *a, **k):
            raise RuntimeError("telemetry down")

        monkeypatch.setattr(AspectExtractionQueue, "enqueue", _boom)
        monkeypatch.setattr(Telemetry, "record_hook_failure", _boom_telemetry)

        # Must not raise.
        aspect_extraction_enqueue_hook(
            source_path="/p1.pdf", collection="knowledge__delos", content="x",
        )


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

        def fake_batch(items, *_args, **_kwargs):
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
                for c, sp, _content, *_ in items
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

        def fake_batch(items, *_args, **_kwargs):
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

    def test_worker_falls_through_to_per_row_on_heterogeneous_batch(
        self, _isolate_t2: Path,
    ) -> None:
        """When a claimed batch crosses ExtractorConfig boundaries
        (e.g. knowledge__ + rdr__ rows in the same claim), the worker
        falls through to per-row processing rather than letting
        extract_aspects_batch raise on heterogeneous configs.

        Pre-fix repro: this test would fail because ``_extract_aspects_batch``
        was invoked once with mixed configs and raised
        ``ValueError: extract_aspects_batch requires all items to share
        a single ExtractorConfig``, marking every row in the batch
        as failed.
        """
        from nexus.aspect_extractor import AspectRecord
        from nexus.aspect_worker import AspectExtractionWorker

        # 3 knowledge__ rows (scholarly-paper config) + 2 rdr__ rows
        # (rdr-frontmatter config) — claimed together when batch_size=5.
        with T2Database(_isolate_t2) as db:
            for i in range(3):
                db.aspect_queue.enqueue(
                    "knowledge__delos",
                    f"/papers/p{i}.pdf",
                    content=f"paper {i}",
                )
            for i in range(2):
                db.aspect_queue.enqueue(
                    "rdr__test-aaaaaaaa",
                    f"/rdrs/r{i}.md",
                    content=f"---\ntitle: r{i}\n---\nbody",
                )

        batch_calls: list[int] = []
        single_calls: list[str] = []

        def fake_batch(items, *_args, **_kwargs):
            batch_calls.append(len(items))
            raise AssertionError(
                "batch path must NOT fire on heterogeneous configs"
            )

        def fake_single(content, source_path, collection, **_kw):
            single_calls.append(source_path)
            return AspectRecord(
                collection=collection, source_path=source_path,
                problem_formulation=f"P-{source_path}",
                proposed_method="M",
                experimental_datasets=["d"],
                experimental_baselines=["b"],
                experimental_results="R",
                extras={}, confidence=0.9,
                extracted_at="2026-05-06T00:00:00+00:00",
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

        # Batch never fired — heterogeneity detected first.
        assert batch_calls == []
        # All 5 rows went through the single-row path.
        assert sorted(single_calls) == sorted([
            "/papers/p0.pdf", "/papers/p1.pdf", "/papers/p2.pdf",
            "/rdrs/r0.md", "/rdrs/r1.md",
        ])


# ── Bounded backoff-retry ladder (RDR-163 P1, nexus-ztpt6) ──────────────────


class TestRetryClassification:
    """`_is_retryable` reuses BOTH retry.py transient predicates."""

    def test_db_locked_is_retryable(self) -> None:
        import sqlite3

        from nexus.aspect_worker import _is_retryable
        assert _is_retryable(sqlite3.OperationalError("database is locked"))

    def test_transport_error_is_retryable(self) -> None:
        import httpx

        from nexus.aspect_worker import _is_retryable
        assert _is_retryable(httpx.ConnectError("connection refused"))

    def test_http_503_message_is_retryable(self) -> None:
        from nexus.aspect_worker import _is_retryable
        assert _is_retryable(Exception("upstream returned 503 service unavailable"))

    def test_value_error_is_not_retryable(self) -> None:
        from nexus.aspect_worker import _is_retryable
        assert not _is_retryable(ValueError("malformed record"))

    def test_type_error_is_not_retryable(self) -> None:
        from nexus.aspect_worker import _is_retryable
        assert not _is_retryable(TypeError("programming bug"))

    def test_plain_exception_is_not_retryable(self) -> None:
        from nexus.aspect_worker import _is_retryable
        assert not _is_retryable(Exception("nothing transient here"))


class TestBackoffInterval:
    """`_backoff_interval_seconds` doubles per attempt with deterministic jitter."""

    def test_no_jitter_doubles_per_attempt(self) -> None:
        from nexus.aspect_worker import _RETRY_BASE_SECONDS, _backoff_interval_seconds

        mid = lambda: 0.5  # jitter factor exactly 1.0  # noqa: E731
        assert _backoff_interval_seconds(0, rng=mid) == int(_RETRY_BASE_SECONDS)
        assert _backoff_interval_seconds(1, rng=mid) == int(_RETRY_BASE_SECONDS * 2)
        assert _backoff_interval_seconds(3, rng=mid) == int(_RETRY_BASE_SECONDS * 8)

    def test_jitter_bounds_are_plus_minus_fraction(self) -> None:
        from nexus.aspect_worker import (
            _RETRY_BASE_SECONDS,
            _RETRY_JITTER_FRACTION,
            _backoff_interval_seconds,
        )

        base = _RETRY_BASE_SECONDS * 4  # retry_count=2
        lo = _backoff_interval_seconds(2, rng=lambda: 0.0)  # factor 1-frac
        hi = _backoff_interval_seconds(2, rng=lambda: 1.0)  # factor 1+frac
        assert lo == int(base * (1.0 - _RETRY_JITTER_FRACTION))
        assert hi == int(base * (1.0 + _RETRY_JITTER_FRACTION))


class _FakeQueue:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def mark_failed(self, collection, source_path, error) -> None:  # noqa: ANN001
        self.calls.append(("failed", collection, source_path, error))

    def mark_retry(self, collection, source_path, interval_seconds=0) -> None:  # noqa: ANN001
        self.calls.append(("retry", collection, source_path, interval_seconds))


class _FakeDb:
    def __init__(self) -> None:
        self.aspect_queue = _FakeQueue()


class TestRetryLadderRouting:
    """`_mark_retry_or_fail_routed` decision: retry vs terminal."""

    def _worker_and_db(self, monkeypatch):  # noqa: ANN001
        import nexus.mcp_infra as infra
        from nexus.aspect_worker import AspectExtractionWorker

        fake_db = _FakeDb()
        monkeypatch.setattr(infra, "t2_index_write", lambda fn: fn(fake_db))
        return AspectExtractionWorker(poll_interval=10.0), fake_db

    def _row(self, retry_count: int):
        import types
        return types.SimpleNamespace(
            collection="knowledge__delos", source_path="/p.pdf", retry_count=retry_count,
        )

    def test_retryable_under_cap_marks_retry_with_backoff(self, monkeypatch) -> None:  # noqa: ANN001
        import sqlite3
        worker, db = self._worker_and_db(monkeypatch)
        worker._mark_retry_or_fail_routed(self._row(0), sqlite3.OperationalError("database is locked"))
        assert len(db.aspect_queue.calls) == 1
        kind, _coll, _sp, interval = db.aspect_queue.calls[0]
        assert kind == "retry"
        assert interval > 0  # backed off (retry_count=0 -> base*1, jittered)

    def test_non_retryable_marks_failed_immediately(self, monkeypatch) -> None:  # noqa: ANN001
        worker, db = self._worker_and_db(monkeypatch)
        worker._mark_retry_or_fail_routed(self._row(0), ValueError("malformed"))
        assert len(db.aspect_queue.calls) == 1
        assert db.aspect_queue.calls[0][0] == "failed"

    def test_retryable_at_cap_marks_failed(self, monkeypatch) -> None:  # noqa: ANN001
        import sqlite3
        from nexus.aspect_worker import _RETRY_MAX_ATTEMPTS
        worker, db = self._worker_and_db(monkeypatch)
        worker._mark_retry_or_fail_routed(
            self._row(_RETRY_MAX_ATTEMPTS), sqlite3.OperationalError("database is locked"),
        )
        assert db.aspect_queue.calls[0][0] == "failed"


class TestRetryLadderIntegration:
    """End-to-end: a retryable failure cycles the ladder to terminal at the cap."""

    def test_retryable_failure_climbs_to_terminal_at_cap(self, _isolate_t2: Path) -> None:
        import sqlite3

        from nexus.aspect_worker import _RETRY_MAX_ATTEMPTS, AspectExtractionWorker

        with T2Database(_isolate_t2) as db:
            db.aspect_queue.enqueue("knowledge__delos", "/retry.pdf")

        def fake_extract(content, source_path, collection, **_kw):  # noqa: ANN001, ANN202
            raise sqlite3.OperationalError("database is locked")

        # SQLite stub ignores the backoff interval (no next_retry_at column), so
        # the row is immediately re-claimable and the worker burns the whole
        # retry budget quickly before going terminal — proving the ladder ran
        # (old behaviour was a single-shot terminal fail at retry_count==1).
        with patch("nexus.aspect_worker._extract_aspects", fake_extract):
            worker = AspectExtractionWorker(poll_interval=0.02)
            worker.start()
            try:
                _wait_until(
                    lambda: _row_status(_isolate_t2, "/retry.pdf") == "failed",
                    timeout=10.0,
                )
            finally:
                worker.stop(timeout=5.0)

        with T2Database(_isolate_t2) as db:
            row = db.aspect_queue.conn.execute(
                "SELECT status, retry_count FROM aspect_extraction_queue WHERE source_path = ?",
                ("/retry.pdf",),
            ).fetchone()
        assert row[0] == "failed"
        # Exact boundary (single worker => deterministic): the row is retried at
        # claim-time counts 0..cap-1 (cap mark_retry calls, each +1), then the
        # claim-time count == cap trips terminal mark_failed (+1 more). Final
        # retry_count is therefore cap+1 — proving the full ladder ran, not a
        # single-shot fail (which would terminate at retry_count == 1).
        assert row[1] == _RETRY_MAX_ATTEMPTS + 1


# ── nexus-w8lg1: the queue row carries the CATALOG doc_id ────────────────────


class TestEnqueueHookDocIdWiring:
    """nexus-w8lg1 (6.3.0 live shakeout finding #1): the enqueue hook must
    persist the catalog tumbler it was handed as the row's ``doc_id`` —
    verbatim, no substitution anywhere between fire_document and the
    queue write. The live bug shipped the T3 chunk hash instead, which
    the engine's composite FK rejected (typed 409) on every CLI store
    put. SQLite has no FK, so this pins the WIRING (the class of bug
    that actually occurred) on every CI run; the FK itself is exercised
    by the --fullstack rehearsal."""

    def test_enqueue_persists_catalog_doc_id_verbatim(self, _isolate_t2):
        from nexus.aspect_worker import aspect_extraction_enqueue_hook

        chunk_id = "bf715bbd" + "0" * 24  # the WRONG identity (T3 chunk hash)
        aspect_extraction_enqueue_hook(
            source_path=chunk_id,
            collection="knowledge__delos",
            content="note body",
            doc_id="1.7.42",
        )

        with T2Database(_isolate_t2) as db:
            row = db.aspect_queue.claim_next()
        assert row is not None
        assert row.doc_id == "1.7.42"
        assert row.source_path == chunk_id

    def test_enqueue_empty_doc_id_stays_empty(self, _isolate_t2):
        """catalog absent -> doc_id stays "" (persists NULL service-side,
        which the nullable FK accepts)."""
        from nexus.aspect_worker import aspect_extraction_enqueue_hook

        aspect_extraction_enqueue_hook(
            source_path="c" * 32,
            collection="knowledge__delos",
            content="body",
        )
        with T2Database(_isolate_t2) as db:
            row = db.aspect_queue.claim_next()
        assert row is not None
        assert row.doc_id == ""
