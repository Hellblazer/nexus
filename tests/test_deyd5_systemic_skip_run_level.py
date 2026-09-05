# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-deyd5 round 3 (coordinator directive, closing a round-2 code-review
HIGH finding): the systemic-skip verdict is a RUN-LEVEL judgment, evaluated
only after every ``run_file_loop`` category AND the ``ChunkBatcher``'s drain
have completed -- never a raise from inside a loop.

Round 2's shape (``nexus.errors.SystemicExtractionFailureError`` raised
directly from ``run_file_loop``, mid-``_run_index``) let a breach on ONE
category (whichever hit the floor first) fire BEFORE the batcher's drain or
the remaining categories ran. ``ChunkBatcher`` has no ``__del__``/atexit, so
already-staged-but-unflushed chunks were discarded, the RDR loop and
post-processing never ran, and the exception was uncaught between
``_run_index`` and the CLI -- an uncleaned traceback instead of the clean
``Error: ...`` UX every sibling stats-driven exit (``pdf_quality_gate_
failed``, ``chunk_flush_failed_files``) already gets. The trigger was not
contrived: PDF classification is extension-only, the default extractor never
runs OCR, and MinerU is opt-in -- so a routine scanned-PDF archive (a legal
archive, historical scans, forms) reliably breaches the floor on the PDF
category alone under default settings.

This file exercises ``_run_index`` with a REAL ``ChunkBatcher``-construction
branch (``db=MagicMock(spec=HttpVectorClient)``, mirroring tests/
test_4s1ww_chunk_flush_failure_reporting.py's precedent for exactly this
gate) so the drain call itself is observable -- proving the fix's core
claim: a category whose skip count would breach the floor does not stop the
batcher's drain from running, and no already-completed work is discarded.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

_DEFAULT_CONFIG = {
    "server": {"ignorePatterns": []},
    "indexing": {
        "code_extensions": [],
        "prose_extensions": [],
        "rdr_paths": ["docs/rdr"],
        "include_untracked": False,
    },
}


def _reg(override=None):
    base = {
        "collection": "code__repo",
        "code_collection": "code__repo__voyage-code-3__v1",
        "docs_collection": "docs__repo__voyage-context-3__v1",
    }
    m = MagicMock()
    m.get.return_value = {**base, **(override or {})}
    return m


@contextmanager
def _service_mode_patches(db, *, extra=None):
    # Mirrors tests/test_4s1ww_chunk_flush_failure_reporting.py's
    # _service_mode_patches exactly (kept local/duplicated rather than
    # cross-imported -- tests/*.py files are not import targets for each
    # other in this suite).
    patches = {
        "nexus.frecency.batch_frecency": {"return_value": {}},
        "nexus.ripgrep_cache.build_cache": {},
        "nexus.indexer._git_metadata": {"return_value": {}},
        "nexus.config.load_config": {"return_value": _DEFAULT_CONFIG},
        "nexus.config.get_credential": {"return_value": "fake-key"},
        "nexus.mcp_infra.get_t3": {"return_value": db},
        "nexus.db.make_t3": {"return_value": db},
        "nexus.indexer._index_code_file": {"return_value": 1},
        "nexus.indexer._index_prose_file": {"return_value": 0},
        "nexus.indexer._prune_misclassified": {},
        "nexus.indexer._prune_deleted_files": {},
        "nexus.indexer._migrate_legacy_collections": {"return_value": {}},
        "nexus.indexer.stamp_collection_version": {},
        "nexus.catalog.factory.make_catalog_reader": {"return_value": None},
        "nexus.catalog.factory.make_catalog_writer": {"return_value": None},
    }
    if extra:
        patches.update(extra)
    mocks, stack = {}, []
    for target, kw in patches.items():
        p = patch(target, **kw)
        m = p.start()
        stack.append(p)
        mocks[target.split(".")[-1]] = m
    try:
        yield mocks
    finally:
        for p in reversed(stack):
            p.stop()


class _DrainTrackingBatcher:
    """Stand-in ChunkBatcher recording whether ``.drain()`` was called --
    isolates "does _run_index's control flow still reach the drain step
    when a category's skip count breaches the systemic-skip floor" from
    "does ChunkBatcher's real flush/retry logic work"
    (tests/test_chunk_batcher.py's job)."""

    def __init__(self, *, flush, **_kw) -> None:
        self._flush = flush
        self.drain_called = False

    def add(self, *_a, **_kw):
        return False  # never staged -- file-level indexers are stubbed anyway

    def drain(self, on_progress=None) -> int:
        self.drain_called = True
        return 0

    @property
    def pending_summary(self) -> dict:
        return {"chunks": 0, "collections": 0, "in_flight": 0}

    @property
    def failed_files(self) -> dict:
        return {}

    @property
    def stats(self) -> dict:
        return {"flushes": 0.0, "flush_seconds": 0.0, "upload_seconds": 0.0}


def test_systemic_skip_breach_does_not_skip_the_drain_or_discard_completed_work(
    tmp_path, monkeypatch,
):
    """THE assertion the round-2 HIGH finding is about: a category whose
    skip count breaches the run-level floor must not stop the batcher's
    drain from running, and _run_index must return NORMALLY (no
    exception) with the successful code file's write still reflected in
    stats -- nothing already completed is discarded by the breach."""
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.errors import UnextractableContentError
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ok.py").write_text("x = 1\n")
    for i in range(25):
        (repo / f"scan{i}.pdf").write_bytes(b"%PDF-1.4 fake content")
    reg = _reg()

    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")
    monkeypatch.setenv("CHROMA_API_KEY", "fake")

    db = MagicMock(spec=HttpVectorClient)

    def _pdf_side_effect(file, *_a, **_kw):
        raise UnextractableContentError(f"{file.name}: no text extracted")

    tracker: dict[str, "_DrainTrackingBatcher"] = {}

    def _batcher_factory(*, flush, **kw):
        b = _DrainTrackingBatcher(flush=flush, **kw)
        tracker["batcher"] = b
        return b

    with _service_mode_patches(db, extra={
        "nexus.indexer._index_pdf_file": {"side_effect": _pdf_side_effect},
    }), patch("nexus.chunk_batcher.ChunkBatcher", _batcher_factory):
        stats = _run_index(repo, reg)

    # 25 of 26 attempted (96%) skipped -- above both the 20-file minimum
    # sample and the 50% ratio: a genuine breach, NOT total loss (the
    # code file succeeded), so there IS completed work to protect.
    assert stats["systemic_extraction_failure"] is True
    assert stats["skipped_unextractable_files"] == 25
    assert stats["files_attempted_total"] == 26

    # THE assertion: the drain step was reached despite the breach.
    assert tracker["batcher"].drain_called is True

    # THE successful code file's write is still reflected -- the PDF
    # category's breach did not retroactively lose the code category's
    # already-completed work.
    assert stats["files_changed_by_kind"]["code"] == 1


def test_systemic_skip_below_floor_does_not_flag_and_drain_still_runs(
    tmp_path, monkeypatch,
):
    """Control: a LOW skip ratio (the bead's own scenario, at a smaller
    scale) must not set the flag at all, and the drain still runs exactly
    as it does on any clean run."""
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.errors import UnextractableContentError
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(24):
        (repo / f"ok{i}.py").write_text("x = 1\n")
    (repo / "blank.pdf").write_bytes(b"%PDF-1.4 fake content")
    reg = _reg()

    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")
    monkeypatch.setenv("CHROMA_API_KEY", "fake")

    db = MagicMock(spec=HttpVectorClient)

    def _pdf_side_effect(file, *_a, **_kw):
        raise UnextractableContentError(f"{file.name}: no text extracted")

    tracker: dict[str, "_DrainTrackingBatcher"] = {}

    def _batcher_factory(*, flush, **kw):
        b = _DrainTrackingBatcher(flush=flush, **kw)
        tracker["batcher"] = b
        return b

    with _service_mode_patches(db, extra={
        "nexus.indexer._index_pdf_file": {"side_effect": _pdf_side_effect},
    }), patch("nexus.chunk_batcher.ChunkBatcher", _batcher_factory):
        stats = _run_index(repo, reg)

    # 1 of 25 attempted (4%) skipped -- well under the floor.
    assert stats["systemic_extraction_failure"] is False
    assert stats["skipped_unextractable_files"] == 1
    assert tracker["batcher"].drain_called is True
    assert stats["files_changed_by_kind"]["code"] == 24


# ── nexus-nukn3: the durable queue replaces the RECORD, not the VERDICT ─────


def test_skip_count_is_read_back_from_the_durable_store_not_the_in_memory_list(
    tmp_path, monkeypatch,
):
    """THE non-vacuity proof for nexus-nukn3's re-pointing: this test mocks
    HttpTelemetryStore to succeed and return a DIFFERENT total than the
    in-memory skip list's length. If ``_run_index`` were still using
    ``len(_skipped_files)`` directly (the pre-nukn3 behavior), this test
    would see 1, not the store's fabricated 7 -- proving the floor's input
    really is a QUERY against the durable queue, not a silent no-op wrapper
    around the same in-memory counter."""
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.errors import UnextractableContentError
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(24):
        (repo / f"ok{i}.py").write_text("x = 1\n")
    (repo / "blank.pdf").write_bytes(b"%PDF-1.4 fake content")
    reg = _reg()

    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")
    monkeypatch.setenv("CHROMA_API_KEY", "fake")

    db = MagicMock(spec=HttpVectorClient)

    def _pdf_side_effect(file, *_a, **_kw):
        raise UnextractableContentError(f"{file.name}: no text extracted")

    telemetry_store = MagicMock()
    telemetry_store.record_index_failures_batch.return_value = 1
    # The store's own read surface disagrees with the in-memory count on
    # purpose -- the only way this test can tell which one _run_index
    # actually used.
    telemetry_store.list_index_failures.return_value = {
        "rows": [], "total": 7, "oldest_occurred_at": "",
    }

    with _service_mode_patches(db, extra={
        "nexus.indexer._index_pdf_file": {"side_effect": _pdf_side_effect},
        "nexus.db.t2.http_telemetry_store.HttpTelemetryStore": {
            "return_value": telemetry_store,
        },
    }), patch("nexus.chunk_batcher.ChunkBatcher", _DrainTrackingBatcher):
        stats = _run_index(repo, reg)

    assert stats["skipped_unextractable_files"] == 7, (
        "must read the durable store's total, not len(_skipped_files) (== 1)"
    )

    # The batch write itself carries the right shape: one row, the real
    # file path, the UnextractableContentError class name, and a non-empty
    # run_id shared across the whole call.
    telemetry_store.record_index_failures_batch.assert_called_once()
    call = telemetry_store.record_index_failures_batch.call_args
    rows = call.args[0]
    assert len(rows) == 1
    file_path, error_class, error, occurred_at = rows[0]
    assert file_path.endswith("blank.pdf")
    assert error_class == "UnextractableContentError"
    assert "no text extracted" in error
    assert call.kwargs["run_id"]  # non-empty


def test_durable_write_failure_falls_back_to_the_in_memory_count(
    tmp_path, monkeypatch,
):
    """Advisory-write posture (matches every other telemetry call site,
    e.g. hook_registry._persist_hook_failure): a transport failure while
    recording the durable rows must not crash an otherwise-successful
    index run, and the floor's input degrades to the in-memory count
    rather than silently becoming zero or raising."""
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.errors import UnextractableContentError
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(24):
        (repo / f"ok{i}.py").write_text("x = 1\n")
    (repo / "blank.pdf").write_bytes(b"%PDF-1.4 fake content")
    reg = _reg()

    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")
    monkeypatch.setenv("CHROMA_API_KEY", "fake")

    db = MagicMock(spec=HttpVectorClient)

    def _pdf_side_effect(file, *_a, **_kw):
        raise UnextractableContentError(f"{file.name}: no text extracted")

    with _service_mode_patches(db, extra={
        "nexus.indexer._index_pdf_file": {"side_effect": _pdf_side_effect},
        "nexus.db.t2.http_telemetry_store.HttpTelemetryStore": {
            "side_effect": RuntimeError("service endpoint unresolvable"),
        },
    }), patch("nexus.chunk_batcher.ChunkBatcher", _DrainTrackingBatcher):
        stats = _run_index(repo, reg)  # must not raise

    assert stats["skipped_unextractable_files"] == 1
    assert stats["systemic_extraction_failure"] is False
