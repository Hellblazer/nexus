# SPDX-License-Identifier: AGPL-3.0-or-later
"""Semantic parity for the pipeline fake engine (RDR-186 .16).

Ports the retired ``test_pipeline_buffer.py`` scenarios to run through the
REAL ``HttpPipelineDB`` against ``FakePipelineEngine`` — keeping the fake
honest to the server contract the stage tests now stand on (the
authoritative server pins are the Java ``PipelineHandlerTest``). SQLite-
specific scenarios (WAL, per-thread connections, schema idempotency) died
with the substrate.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from nexus.db.http_pipeline_client import PipelineConflictRunning, PipelineRunFenced
from tests.pipeline_fake_engine import FakePipelineEngine, make_fake_engine_db

_T0 = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)


class _Clock:
    """Fixed, manually-advanced clock (house rule: no wall-clock sleeps)."""

    def __init__(self) -> None:
        self.now = _T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture()
def clock() -> _Clock:
    return _Clock()


@pytest.fixture()
def rig(clock: _Clock):
    return make_fake_engine_db(clock=clock)


@pytest.fixture()
def db(rig):
    return rig[0]


@pytest.fixture()
def engine(rig) -> FakePipelineEngine:
    return rig[1]


class TestPipeline:
    @pytest.mark.parametrize("setup,expected", [
        ("new", "created"),
        ("running_stale", "resuming"),
        ("failed", "resuming"),
        # nexus-edjmu: a document-identity create resets a leftover
        # completed row and answers "created" (never "skip"); the legacy
        # hash-only create still answers "skip", pinned in
        # TestPerRowIdentity below.
        ("completed", "created"),
    ])
    def test_create_pipeline(self, db, clock, setup, expected):
        if setup == "new":
            assert db.create_pipeline("h1", "/a.pdf", "docs__test") == expected
            return
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        if setup == "running_stale":
            clock.advance(minutes=6)
        elif setup == "failed":
            db.mark_failed("h1", "boom")
        elif setup == "completed":
            db.mark_completed("h1")
        assert db.create_pipeline("h1", "/a.pdf", "docs__test") == expected

    def test_create_pipeline_running_recent_raises_conflict(self, db, clock):
        """nexus-lcmbp: a retry against a 'running' row with a FRESH
        heartbeat must be a loud PipelineConflictRunning, never the silent
        200 'skip' that let a stranded-row retry exit rc=0 with 0 chunks."""
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        with pytest.raises(PipelineConflictRunning) as exc_info:
            db.create_pipeline("h1", "/a.pdf", "docs__test")
        err = exc_info.value
        assert err.content_hash == "h1"
        assert err.stale_threshold_seconds == 300
        assert err.heartbeat_age_seconds >= 0
        assert "resume window" in err.remedy
        # The refused attempt must not touch the row.
        state = db.get_pipeline_state("h1")
        assert state["status"] == "running"

    def test_stale_threshold_agrees_across_client_and_wire(self, db, clock):
        """nexus-lcmbp fix-list #6: the fake engine's staleness rule, the
        client's own STALE_THRESHOLD constant, and the literal
        stale_threshold_seconds parsed off a REAL 409 wire body must all
        agree at 300s — the value pinned server-side (Java) by
        PipelineRepository.STALE_THRESHOLD / PipelineHandlerTest. A drift
        here would silently desync the client's orphan-scan staleness
        judgment from the server's create() staleness judgment."""
        from nexus.db.http_pipeline_client import STALE_THRESHOLD

        assert int(STALE_THRESHOLD.total_seconds()) == 300

        db.create_pipeline("h1", "/a.pdf", "docs__test")
        with pytest.raises(PipelineConflictRunning) as exc_info:
            db.create_pipeline("h1", "/a.pdf", "docs__test")

        assert exc_info.value.stale_threshold_seconds == int(STALE_THRESHOLD.total_seconds())
        assert exc_info.value.stale_threshold_seconds == 300

    def test_get_pipeline_state(self, db):
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        state = db.get_pipeline_state("h1")
        assert state["status"] == "running" and state["pdf_path"] == "/a.pdf"
        assert state["collection"] == "docs__test" and state["pages_extracted"] == 0

    def test_get_pipeline_state_missing(self, db):
        assert db.get_pipeline_state("nonexistent") is None

    @pytest.mark.parametrize("kwargs,check_field,check_val,untouched_field,untouched_val", [
        ({"pages_extracted": 5, "chunks_created": 20}, "pages_extracted", 5, None, None),
        ({"chunks_uploaded": 10}, "chunks_uploaded", 10, "pages_extracted", 0),
    ])
    def test_update_progress(self, db, kwargs, check_field, check_val, untouched_field, untouched_val):
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        db.update_progress("h1", **kwargs)
        state = db.get_pipeline_state("h1")
        assert state[check_field] == check_val
        if untouched_field:
            assert state[untouched_field] == untouched_val


class TestPages:
    @pytest.fixture(autouse=True)
    def _run(self, db):
        # nexus-edjmu: pages belong to a run; the FK refuses a parentless
        # write (the engine answers 400 "no pipeline row for ...").
        db.create_pipeline("h1", "/a.pdf", "docs__test")

    def test_write_and_read(self, db):
        db.write_page("h1", 0, "first page")
        db.write_page("h1", 1, "second page")
        pages = db.read_pages("h1")
        assert len(pages) == 2 and pages[0]["page_text"] == "first page"

    def test_write_with_metadata(self, db):
        meta = {"font": "Times", "tables": 2}
        db.write_page("h1", 0, "text", metadata=meta)
        assert json.loads(db.read_pages("h1")[0]["metadata_json"]) == meta

    def test_read_empty(self, db):
        assert db.read_pages("nonexistent") == []

    def test_write_idempotent(self, db):
        """Page writes REPLACE (latest text wins) — even within one
        buffered batch."""
        db.write_page("h1", 0, "first")
        db.write_page("h1", 0, "updated")
        pages = db.read_pages("h1")
        assert len(pages) == 1 and pages[0]["page_text"] == "updated"

    def test_read_pages_from_offset(self, db):
        for i in range(4):
            db.write_page("h1", i, f"p{i}")
        rows = db.read_pages_from("h1", 2)
        assert [r["page_index"] for r in rows] == [2, 3]


class TestChunks:
    @pytest.fixture(autouse=True)
    def _run(self, db):
        # nexus-edjmu: chunks belong to a run; the FK refuses a parentless
        # write (the engine answers 400 "no pipeline row for ...").
        db.create_pipeline("h1", "/a.pdf", "docs__test")

    def test_write_and_read(self, db):
        db.write_chunk("h1", 0, "chunk text", "cid-0")
        db.write_chunk("h1", 1, "chunk text 2", "cid-1")
        chunks = db.read_ready_chunks("h1")
        assert len(chunks) == 2 and chunks[0]["chunk_text"] == "chunk text"

    def test_write_with_metadata(self, db):
        meta = {"source_page": 3}
        db.write_chunk("h1", 0, "text", "cid-0", metadata=meta)
        assert json.loads(db.read_ready_chunks("h1")[0]["metadata_json"]) == meta

    @pytest.mark.parametrize("total,mark_indices,expected_ready", [
        (2, [0], 1), (5, [0, 1, 2], 2),
    ])
    def test_mark_uploaded(self, db, total, mark_indices, expected_ready):
        for i in range(total):
            db.write_chunk("h1", i, f"text{i}", f"cid-{i}")
        db.mark_uploaded("h1", mark_indices)
        assert len(db.read_ready_chunks("h1")) == expected_ready

    def test_write_idempotent_preserves_original(self, db):
        """Chunk writes IGNORE on conflict (first write wins) — an
        existing row is never overwritten (idempotent resume)."""
        db.write_chunk("h1", 0, "original", "cid-0")
        db.write_chunk("h1", 0, "should-be-ignored", "cid-0-new")
        chunks = db.read_ready_chunks("h1")
        assert len(chunks) == 1 and chunks[0]["chunk_text"] == "original"

    def test_write_ignore_preserves_embedding(self, db):
        db.write_chunk("h1", 0, "text", "cid-0", embedding=b"\x00\x01\x02")
        db.flush("h1")
        db.write_chunk("h1", 0, "text", "cid-0")  # resume replay, no embedding
        rows = db.read_ready_chunks("h1")
        assert rows[0]["embedding"] == b"\x00\x01\x02"

    def test_counts_blank_hash_is_zero_not_global(self, db, engine):
        """PipelineHandler.handleCounts contract: blank/absent content_hash
        yields embedded_chunks=0, never a cross-pipeline sum (.16 critic
        Significant #3 — the drift class the parity suite must pin)."""
        db.write_chunk("h1", 0, "t", "cid-0", embedding=b"\x01")
        db.flush("h1")
        assert engine.counts({})["embedded_chunks"] == 0
        assert engine.counts({"content_hash": ""})["embedded_chunks"] == 0
        assert engine.counts({"pipeline_id": db.pipeline_id_for("h1")})["embedded_chunks"] == 1
        assert db.count_embedded_chunks("h1") == 1
        # nexus-edjmu: a bare hash names legacy rows only; this run is a
        # document row, so an old client's bare-hash count sees nothing.
        assert engine.counts({"content_hash": "h1"})["embedded_chunks"] == 0

    def test_uploadable_requires_embedding_sentinel_counts(self, db):
        """Uploadable = embedding present and not uploaded; the b'' service
        sentinel COUNTS as present (nexus-9n1u3) — only SQL-NULL means
        not-embedded."""
        db.write_chunk("h1", 0, "no-embed", "cid-0", embedding=None)
        db.write_chunk("h1", 1, "sentinel", "cid-1", embedding=b"")
        db.write_chunk("h1", 2, "vector", "cid-2", embedding=b"\x01")
        rows = db.read_uploadable_chunks("h1")
        assert [r["chunk_index"] for r in rows] == [1, 2]
        assert db.count_embedded_chunks("h1") == 2


class TestCleanup:
    def test_delete_pipeline_data(self, db):
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        db.write_page("h1", 0, "page text")
        db.write_chunk("h1", 0, "chunk text", "cid-0")
        db.delete_pipeline_data("h1")
        assert db.get_pipeline_state("h1") is None
        assert db.read_pages("h1") == [] and db.read_ready_chunks("h1") == []

    def test_delete_nonexistent(self, db):
        db.delete_pipeline_data("ghost")

    def test_delete_pipeline_data_for_collection(self, db):
        db.create_pipeline("h_keep", "/k.pdf", "docs__keep")
        db.write_page("h_keep", 0, "keep")
        db.create_pipeline("h_drop", "/d.pdf", "knowledge__delos")
        db.write_page("h_drop", 0, "drop")
        db.flush_all()

        assert db.delete_pipeline_data_for_collection("knowledge__delos") == 1
        assert db.get_pipeline_state("h_drop") is None
        assert db.read_pages("h_drop") == []
        assert db.get_pipeline_state("h_keep") is not None
        assert len(db.read_pages("h_keep")) == 1

    def test_delete_pipeline_data_for_collection_no_rows(self, db):
        assert db.delete_pipeline_data_for_collection("docs__ghost") == 0

    def test_heartbeat_updated(self, db, clock):
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        t0 = db.get_pipeline_state("h1")["updated_at"]
        clock.advance(seconds=1)
        db.update_progress("h1", pages_extracted=1)
        assert db.get_pipeline_state("h1")["updated_at"] > t0

    def test_bad_field_raises(self, db):
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        with pytest.raises(Exception, match="[Uu]nknown progress fields"):
            db.update_progress("h1", nonexistent_field=1)

    def test_clear_orphan_wal_preserves_pipeline_row(self, db):
        """nexus-2fyb C-int-2: WAL clear drops pages+chunks but keeps the
        failed pipeline row's audit trail."""
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        db.write_page("h1", 0, "p")
        db.write_chunk("h1", 0, "c", "cid-0")
        db.mark_failed("h1", "math pdf without MinerU")
        db.clear_orphan_wal("h1")
        assert db.read_pages("h1") == [] and db.read_ready_chunks("h1") == []
        state = db.get_pipeline_state("h1")
        assert state["status"] == "failed" and state["error"] == "math pdf without MinerU"


class TestScanOrphanedPipelines:
    @pytest.mark.parametrize("setup,expected_orphan", [
        ("missing_pdf", True),
        ("stale_running", True),
        ("recent_running", False),
        ("completed_old", False),
        ("failed_missing_pdf", True),
    ])
    def test_orphan_detection(self, db, clock, setup, expected_orphan):
        pdf = "/nonexistent/gone.pdf" if "missing" in setup else __file__
        db.create_pipeline("h1", pdf, "docs__test")
        if "stale" in setup:
            clock.advance(minutes=6)
        elif "completed" in setup:
            db.mark_completed("h1")
            clock.advance(minutes=60)
        elif "failed" in setup:
            db.mark_failed("h1", "crash")
        orphans = db.scan_orphaned_pipelines()
        assert ("h1" in orphans) == expected_orphan

    def test_delete_cleans_all_tables(self, db):
        db.create_pipeline("h1", "/nonexistent/gone.pdf", "docs__test")
        db.write_page("h1", 0, "page text")
        db.write_chunk("h1", 0, "chunk text", "cid-0")
        orphans = db.scan_orphaned_pipelines(delete=True)
        assert "h1" in orphans
        assert db.get_pipeline_state("h1") is None and db.read_pages("h1") == []

    def test_empty_database(self, db):
        assert db.scan_orphaned_pipelines() == []


class TestPerRowIdentity:
    """nexus-edjmu (pipeline-002-per-row-identity.xml): the Java
    ``PipelineHandlerTest`` scenarios for one-row-per-document keying,
    mirrored against the fake so the stage suites stand on the same
    contract."""

    def test_two_documents_sharing_a_hash_get_their_own_rows_and_wals(self, db, engine):
        other, _ = make_fake_engine_db()
        other._client = db._client
        assert db.create_pipeline("h1", "/a.pdf", "docs__test") == "created"
        assert other.create_pipeline("h1", "/b.pdf", "docs__test") == "created"
        assert db.pipeline_id_for("h1") != other.pipeline_id_for("h1")
        db.write_page("h1", 0, "a's page")
        db.write_chunk("h1", 0, "a's chunk", "cid-a", embedding=b"\x01")
        db.mark_uploaded("h1", [0])
        other.write_chunk("h1", 0, "b's chunk", "cid-b", embedding=b"\x01")
        # B's WAL is B's own: nothing of A's leaks into its reads or counts.
        assert other.read_pages("h1") == []
        assert [r["chunk_id"] for r in other.read_uploadable_chunks("h1")] == ["cid-b"]
        assert other.count_embedded_chunks("h1") == 1
        assert db.count_embedded_chunks("h1") == 1
        assert db.read_uploadable_chunks("h1") == []  # A's one chunk is uploaded

    def test_one_rows_cleanup_leaves_the_siblings_wal(self, db, engine):
        other, _ = make_fake_engine_db()
        other._client = db._client
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        other.create_pipeline("h1", "/b.pdf", "docs__test")
        db.write_page("h1", 0, "a")
        other.write_page("h1", 0, "b")
        other.flush("h1")
        db.clear_orphan_wal("h1")
        assert other.read_pages("h1") != [], "A's clear_wal wiped B's pages"
        assert db.delete_pipeline_data("h1") is True
        assert other.read_pages("h1") != [], "A's delete wiped B's pages"
        assert other.get_pipeline_state("h1") is not None

    def test_force_delete_names_the_document(self, db, engine):
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        db.mark_completed("h1")
        other, _ = make_fake_engine_db()
        other._client = db._client
        other.create_pipeline("h1", "/b.pdf", "docs__test")
        fresh, _ = make_fake_engine_db()
        fresh._client = db._client
        assert fresh.delete_pipeline_data("h1", collection="docs__test", pdf_path="/b.pdf") is True
        assert [r["pdf_path"] for r in engine.rows_for("h1")] == ["/a.pdf"]
        # A narrowing that matches nothing is a miss, never a widen.
        assert fresh.delete_pipeline_data("h1", collection="docs__test", pdf_path="/zzz.pdf") is False
        assert len(engine.rows_for("h1")) == 1

    def test_same_instance_refuses_a_second_document_while_the_first_is_live(self, db):
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        with pytest.raises(PipelineConflictRunning, match="this HttpPipelineDB instance"):
            db.create_pipeline("h1", "/b.pdf", "docs__test")
        # ...but a re-create of the SAME document is the engine's call.
        with pytest.raises(PipelineConflictRunning, match="resume window"):
            db.create_pipeline("h1", "/a.pdf", "docs__test")
        db.mark_failed("h1", "boom")
        assert db.create_pipeline("h1", "/a.pdf", "docs__test") == "resuming"

    def test_legacy_create_keeps_one_row_per_hash(self, db, engine):
        """A client older than pipeline-002 sends no identity: one row per
        hash, completed -> skip, running -> 409, exactly pipeline-001."""
        legacy = {"content_hash": "h1", "pdf_path": "/a.pdf", "collection": "docs__test"}
        assert engine.create(legacy)["status"] == "created"
        assert engine.row_for("h1")["keyed_by"] == "content_hash"
        # A second path under the legacy algorithm is the SAME row: 409.
        from tests.pipeline_fake_engine import _ConflictRunning
        with pytest.raises(_ConflictRunning):
            engine.create({**legacy, "pdf_path": "/b.pdf"})
        engine.complete({"content_hash": "h1"})
        assert engine.create({**legacy, "pdf_path": "/b.pdf"})["status"] == "skip"
        assert len(engine.rows_for("h1")) == 1

    def test_hash_only_calls_land_on_the_legacy_row(self, db, engine):
        """A legacy client's run is never hijacked by a document row a newer
        client inserts for the same bytes mid-run."""
        legacy = {"content_hash": "h1", "pdf_path": "/old.pdf", "collection": "docs__test"}
        legacy_id = engine.create(legacy)["pipeline_id"]
        assert db.create_pipeline("h1", "/new.pdf", "docs__test") == "created"
        new_id = db.pipeline_id_for("h1")
        assert new_id != legacy_id
        engine.progress({"content_hash": "h1", "fields": {"pages_extracted": 7}})
        engine.complete({"content_hash": "h1"})
        assert engine.pipelines[legacy_id]["pages_extracted"] == 7
        assert engine.pipelines[legacy_id]["status"] == "completed"
        assert engine.pipelines[new_id]["pages_extracted"] == 0
        assert engine.pipelines[new_id]["status"] == "running"
        assert db.get_pipeline_state("h1")["pipeline_id"] == new_id

    def test_legacy_create_beside_a_strangers_document_row_gets_its_own_row(self, db, engine):
        """A bare hash names legacy rows only: an old client never adopts
        another document's row, and its hash-only delete never reaches one."""
        assert db.create_pipeline("h1", "/b.pdf", "docs__test") == "created"
        doc_id = db.pipeline_id_for("h1")
        db.mark_failed("h1", "crash")
        legacy = {"content_hash": "h1", "pdf_path": "/a.pdf", "collection": "docs__test"}
        created = engine.create(legacy)
        assert created["status"] == "created"
        assert created["pipeline_id"] != doc_id
        engine.write_pages({"content_hash": "h1", "pages": [{"page_index": 0, "page_text": "a"}]})
        engine.complete({"content_hash": "h1"})
        assert engine.pipelines[created["pipeline_id"]]["status"] == "completed"
        assert engine.pipelines[doc_id]["status"] == "failed"
        assert engine.read_pages({"pipeline_id": doc_id})["pages"] == []
        assert engine.delete({"content_hash": "h1"}) == {"deleted": True}
        assert engine.delete({"content_hash": "h1"}) == {"deleted": False}
        assert doc_id in engine.pipelines

    def test_legacy_create_adopts_its_own_documents_row(self, db, engine):
        assert db.create_pipeline("h1", "/a.pdf", "docs__test") == "created"
        doc_id = db.pipeline_id_for("h1")
        db.mark_failed("h1", "crash")
        legacy = {"content_hash": "h1", "pdf_path": "/a.pdf", "collection": "docs__test"}
        resumed = engine.create(legacy)
        assert resumed == {"status": "resuming", "pipeline_id": doc_id, "run_epoch": 1}
        assert engine.pipelines[doc_id]["keyed_by"] == "content_hash"
        engine.progress({"content_hash": "h1", "fields": {"pages_extracted": 3}})
        assert engine.pipelines[doc_id]["pages_extracted"] == 3

    def test_explicit_narrowing_beats_a_held_id_on_a_reused_instance(self, db, engine):
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        held = db.pipeline_id_for("h1")
        db.mark_completed("h1")
        other, _ = make_fake_engine_db()
        other._client = db._client
        other.create_pipeline("h1", "/b.pdf", "docs__test")
        assert db.delete_pipeline_data("h1", collection="docs__test", pdf_path="/b.pdf") is True
        assert [r["pipeline_id"] for r in engine.rows_for("h1")] == [held]

    def test_scan_deletes_the_listed_row_only(self, db, engine, clock):
        db.create_pipeline("h1", "/nonexistent/a.pdf", "docs__test")
        other, _ = make_fake_engine_db(clock=clock)
        other._client = db._client
        other.create_pipeline("h1", __file__, "docs__test")
        assert db.scan_orphaned_pipelines(delete=True) == ["h1"]
        assert [r["pdf_path"] for r in engine.rows_for("h1")] == [__file__]

    def test_delete_collection_keeps_the_sibling_in_another_collection(self, db, engine):
        db.create_pipeline("h1", "/a.pdf", "docs__gone")
        db.write_page("h1", 0, "a")
        other, _ = make_fake_engine_db()
        other._client = db._client
        other.create_pipeline("h1", "/a.pdf", "docs__keep")
        other.write_page("h1", 0, "k")
        other.flush("h1")
        assert db.delete_pipeline_data_for_collection("docs__gone") == 1
        assert [r["collection"] for r in engine.rows_for("h1")] == ["docs__keep"]
        assert other.read_pages("h1") != []

    def test_create_without_pipeline_id_in_the_answer_is_loud(self, db, engine, monkeypatch):
        """An engine older than pipeline-002 answers {status} only; the
        client refuses rather than running hash-addressed."""
        original = engine.create
        monkeypatch.setattr(engine, "create", lambda body: {"status": original(body)["status"]})
        with pytest.raises(RuntimeError, match="pipeline_id"):
            db.create_pipeline("h1", "/a.pdf", "docs__test")


class TestRunEpoch:
    """nexus-8vu8p: a run's ownership generation fences a taken-over run's
    writes. Mirrors PipelineHandlerTest.runEpoch_* against the fake."""

    def test_epoch_starts_at_zero_and_bumps_on_every_takeover(self, db, engine, clock):
        assert db.create_pipeline("h1", "/a.pdf", "docs__test") == "created"
        assert db.run_epoch_for("h1") == 0
        clock.advance(minutes=6)
        other, _ = make_fake_engine_db(clock=clock)
        other._client = db._client
        assert other.create_pipeline("h1", "/a.pdf", "docs__test") == "resuming"
        assert other.run_epoch_for("h1") == 1
        other.mark_failed("h1", "x")
        third, _ = make_fake_engine_db(clock=clock)
        third._client = db._client
        assert third.create_pipeline("h1", "/a.pdf", "docs__test") == "resuming"
        assert third.run_epoch_for("h1") == 2
        third.mark_completed("h1")
        fourth, _ = make_fake_engine_db(clock=clock)
        fourth._client = db._client
        assert fourth.create_pipeline("h1", "/a.pdf", "docs__test") == "created"
        assert fourth.run_epoch_for("h1") == 3, "a leftover reset bumps, never returns to 0"

    def test_every_write_of_the_stale_run_is_fenced_and_writes_nothing(self, db, engine, clock):
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        pid = db.pipeline_id_for("h1")
        clock.advance(minutes=6)
        owner, _ = make_fake_engine_db(clock=clock)
        owner._client = db._client
        assert owner.create_pipeline("h1", "/a.pdf", "docs__test") == "resuming"
        owner.write_page("h1", 0, "owner")
        owner.write_chunk("h1", 0, "o0", "cid-o0", embedding=b"")
        owner.update_progress("h1", pages_extracted=1)
        owner.flush("h1")

        with pytest.raises(PipelineRunFenced) as exc:
            db.write_page("h1", 1, "stale"); db.flush("h1")
        assert (exc.value.pipeline_id, exc.value.run_epoch, exc.value.current_epoch) == (pid, 0, 1)
        for call in (
            # Progress coalesces behind the pending page batch; the flush is the write.
            lambda: (db.update_progress("h1", pages_extracted=9), db.flush("h1")),
            lambda: db.store_extraction_metadata("h1", {}),
            lambda: db.mark_uploaded("h1", [0]),
            lambda: db.mark_completed("h1"),
            lambda: db.mark_failed("h1", "stale"),
            lambda: db.clear_orphan_wal("h1"),
            lambda: db.delete_pipeline_data("h1"),
        ):
            with pytest.raises(PipelineRunFenced):
                call()
        row = engine.pipelines[pid]
        assert (row["status"], row["pages_extracted"], row["run_epoch"]) == ("resuming", 1, 1)
        assert [r["page_text"] for r in owner.read_pages("h1")] == ["owner"]
        assert len(owner.read_uploadable_chunks("h1")) == 1
        # Reads are never fenced.
        assert db.get_pipeline_state("h1")["run_epoch"] == 1
        # A write without an epoch (a client older than pipeline-003) is unfenced.
        assert engine.progress({"pipeline_id": pid, "fields": {"pages_extracted": 2}}) == {"updated": True}
        assert row["pages_extracted"] == 2

    def test_create_answer_without_run_epoch_is_loud(self, db, engine, monkeypatch):
        original = engine.create
        monkeypatch.setattr(engine, "create", lambda body: {k: v for k, v in original(body).items() if k != "run_epoch"})
        with pytest.raises(RuntimeError, match="run_epoch"):
            db.create_pipeline("h1", "/a.pdf", "docs__test")
