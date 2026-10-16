# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-gjxkb: the streaming PDF uploader must not finish its writer while chunks are still to come.

Production (``nx dt index``, conexus 7.81.0, the Voyager PDF): the uploader read the buffer, the
chunker's final pass then wrote its last chunks and set ``chunking_done``, and only then did the
uploader check the event. It saw "chunker done" next to an empty read, finished the writer (whose
last request carries the sweep, against a manifest missing the tail), and the next poll handed the
late chunks to the finished writer: ``ValueError: MultiBatchDocumentWriter: already finished``.

Each test drives the real ``uploader_loop`` over the in-memory pipeline engine and reproduces one
interleaving deterministically by wrapping the buffer's read, the way the stage threads interleave
in production. ``RecordingWriter`` refuses work after ``finish()`` exactly as the real writer does.
"""
from __future__ import annotations

import struct
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.db.http_pipeline_client import HttpPipelineDB
from nexus.hook_registry import HookRegistry
from nexus.pipeline_stages import uploader_loop
from tests._bounded_polling import bound_the_polling
from tests._owner_write_double import install_streaming_writer
from tests.pipeline_fake_engine import make_fake_engine_db

H = "h1"


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.pipeline_stages._POLL_INTERVAL", 0.01)
    bound_the_polling(monkeypatch)


@pytest.fixture()
def writer(monkeypatch: pytest.MonkeyPatch):
    return install_streaming_writer(monkeypatch)


@pytest.fixture()
def db() -> HttpPipelineDB:
    store, _engine = make_fake_engine_db()
    store.create_pipeline(H, "/a.pdf", "docs__test")
    store.update_progress(H, total_pages=1, pages_extracted=1)
    return store


def _write(db: HttpPipelineDB, indices: range) -> None:
    for i in indices:
        db.write_chunk(H, i, f"chunk {i} text", f"{H}_{i}",
                       metadata={"page": 1, "content_hash": H},
                       embedding=struct.pack("4f", float(i), 0.1, 0.2, 0.3))


def _positions(writer: Any) -> list[list[int]]:
    (w,) = writer.instances
    return [[r["position"] for r in rows] for rows, _ in w.batches]


def _run(db: HttpPipelineDB, chunking_done: threading.Event) -> None:
    # No post-store hooks: they are not what these tests are about, and the default set reaches
    # for a live service.
    uploader_loop(H, db, MagicMock(), "docs__test", threading.Event(), chunking_done,
                  catalog_doc_id="1.1.1", hooks=HookRegistry())


def test_chunks_the_chunker_writes_between_the_read_and_the_done_check_reach_the_writer(
    db, writer, monkeypatch,
) -> None:
    """The production interleaving. Read 2 returns nothing new; before the uploader looks at the
    event, the chunker's final pass writes chunks 3 and 4, records the final count and signals."""
    _write(db, range(3))
    db.update_progress(H, chunks_created=3)       # the chunker's provisional count
    done = threading.Event()
    real_read, calls = db.read_uploadable_chunks, {"n": 0}

    def read(content_hash: str, limit: int = 0) -> list[dict]:
        rows = real_read(content_hash, limit=limit)
        calls["n"] += 1
        if calls["n"] == 2:
            _write(db, range(3, 5))
            db.update_progress(H, chunks_created=5, chunks_embedded=5)
            done.set()
        return rows

    monkeypatch.setattr(db, "read_uploadable_chunks", read)
    _run(db, done)

    (w,) = writer.instances
    assert w.finished
    assert [p for batch in _positions(writer) for p in batch] == [0, 1, 2, 3, 4], \
        "the writer finished before the chunker's last chunks reached it"
    state = db.get_pipeline_state(H)
    assert state["chunks_uploaded"] == 5 and state["status"] == "completed"


def test_a_row_not_yet_visible_in_the_middle_is_waited_for_not_skipped(db, writer, monkeypatch) -> None:
    """HttpPipelineDB documents a cross-thread flush race: a read can miss rows another thread's
    POST has not landed yet while later rows are visible. The uploader takes only the contiguous
    run from the next position; skipping past the hole would leave position 2 out of the manifest
    for good (the ``> last_added`` filter never reads it again) and the run would never end."""
    _write(db, range(5))
    db.update_progress(H, chunks_created=5, chunks_embedded=5)
    done = threading.Event()
    done.set()
    real_read, calls = db.read_uploadable_chunks, {"n": 0}

    def read(content_hash: str, limit: int = 0) -> list[dict]:
        rows = real_read(content_hash, limit=limit)
        calls["n"] += 1
        return [r for r in rows if r["chunk_index"] != 2] if calls["n"] == 1 else rows

    monkeypatch.setattr(db, "read_uploadable_chunks", read)
    _run(db, done)

    assert _positions(writer) == [[0, 1], [2, 3, 4]]
    assert writer.instances[0].finished
    assert db.get_pipeline_state(H)["status"] == "completed"


def test_the_writer_is_not_finished_while_the_buffer_holds_fewer_rows_than_the_chunker_made(
    db, writer, monkeypatch,
) -> None:
    """The landing decision rests on the durable row count, not on which threads might still be
    flushing: chunks 3 and 4 land in the engine only after the uploader has seen the chunker done
    and an empty read."""
    _write(db, range(3))
    db.update_progress(H, chunks_created=5, chunks_embedded=5)
    done = threading.Event()
    done.set()
    real_read, calls = db.read_uploadable_chunks, {"n": 0}

    def read(content_hash: str, limit: int = 0) -> list[dict]:
        calls["n"] += 1
        if calls["n"] == 3:
            _write(db, range(3, 5))
        return real_read(content_hash, limit=limit)

    monkeypatch.setattr(db, "read_uploadable_chunks", read)
    _run(db, done)

    assert [p for batch in _positions(writer) for p in batch] == [0, 1, 2, 3, 4]
    assert writer.instances[0].finished


def test_a_first_chunk_not_yet_visible_is_not_mistaken_for_a_partial_resume(db, writer, monkeypatch) -> None:
    """With no writer yet, a first readable row above 0 is a partial resume only when an earlier
    process flagged the head uploaded. When nothing is flagged, chunk 0 is not visible yet: wait."""
    _write(db, range(4))
    db.update_progress(H, chunks_created=4, chunks_embedded=4)
    done = threading.Event()
    done.set()
    real_read, calls = db.read_uploadable_chunks, {"n": 0}

    def read(content_hash: str, limit: int = 0) -> list[dict]:
        rows = real_read(content_hash, limit=limit)
        calls["n"] += 1
        return [r for r in rows if r["chunk_index"] != 0] if calls["n"] == 1 else rows

    monkeypatch.setattr(db, "read_uploadable_chunks", read)
    _run(db, done)

    assert _positions(writer) == [[0, 1, 2, 3]]
