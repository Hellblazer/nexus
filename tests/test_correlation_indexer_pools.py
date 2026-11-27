# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-167 Phase 1 (nexus-dngtz.1.2): the correlation id reaches the engine-bound indexer thread pools.

A bare ``ThreadPoolExecutor.submit`` does not carry a ``ContextVar`` into the worker thread, so the id
the CLI binds is lost before ``nx index repo`` makes its engine calls from the "nx-index" pool, the
"nx-flush" pool, the PDF pipeline's three-stage pool and the "aspect-enqueue" pool. Each submit must
run its callable inside a fresh ``contextvars.copy_context()`` (one Context cannot be entered by two
threads at once).

Every behaviour test records the id INSIDE the worker thread (and the thread's name, so a worker that
never ran cannot pass), then asserts on the record after the pool is done. The source pin is the
backstop: it names each file, fails on a bare ``.submit(``, and fails when a file has no submit.
"""
from __future__ import annotations

import ast
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from nexus import correlation
from nexus.chunk_batcher import ChunkBatcher
from nexus.db.inmemory_pipeline import make_in_memory_pipeline_db
from nexus.db.t2.http_aspect_queue import HttpAspectQueue
from nexus.db.t2.http_tuple_store import HttpTupleStore
from nexus.indexer_utils import run_file_loop
from nexus.pipeline_stages import pipeline_index_pdf
from tests._owner_write_double import throwaway_t3

_SRC = Path(__file__).resolve().parent.parent / "src" / "nexus"


@pytest.fixture
def bound_id():
    """A bound correlation id for the test; reset by token afterwards."""
    cid = correlation.new_correlation_id()
    token = correlation.bind(cid)
    try:
        yield cid
    finally:
        correlation.reset(token)


class _Recorder:
    """Records ``(thread name, correlation id)`` from inside the worker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.seen: list[tuple[str, str | None]] = []

    def record(self, overlap: threading.Barrier | None = None) -> None:
        if overlap is not None:
            # Hold three workers inside the pool at once: one Context shared by two of them raises
            # RuntimeError at entry, so a submit that reuses a Context cannot pass.
            overlap.wait(timeout=10)
        with self._lock:
            self.seen.append((threading.current_thread().name, correlation.current_correlation_id()))

    def ids_on(self, prefix: str) -> list[str | None]:
        return [cid for name, cid in self.seen if name.startswith(prefix)]


def test_nx_index_pool_workers_see_the_bound_id(bound_id: str, tmp_path: Path) -> None:
    rec = _Recorder()
    overlap = threading.Barrier(3)

    def index_one(file: Path, score: float, timers: object) -> int:
        rec.record(overlap)
        return 1

    files = [(1.0, tmp_path / f"f{i}.md") for i in range(6)]
    run_file_loop(files, index_one, concurrency=3, on_file=None, on_stage_timers=None)

    ids = rec.ids_on("nx-index")
    assert len(ids) == len(files), rec.seen
    assert set(ids) == {bound_id}


def test_nx_flush_pool_workers_see_the_bound_id(bound_id: str) -> None:
    rec = _Recorder()
    overlap = threading.Barrier(3)

    def flush(collection: str, ids: list[str], docs: list[str], metas: list[dict], file_contexts: list | None = None) -> None:
        rec.record(overlap)

    batcher = ChunkBatcher(flush=flush, max_chunks=5, flush_concurrency=3)
    for i in range(6):
        ids = [f"f{i}-c{j}" for j in range(5)]
        batcher.add(f"f{i}.py", "code__x", ids, [f"t{x}" for x in ids], [{"i": j} for j in range(5)])
    batcher.drain()

    ids_seen = rec.ids_on("nx-flush")
    assert len(ids_seen) == 6, rec.seen
    assert set(ids_seen) == {bound_id}


def test_pdf_pipeline_stage_workers_see_the_bound_id(bound_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    overlap = threading.Barrier(3)

    def extractor(*args: Any, **kwargs: Any) -> None:
        rec.record(overlap)
        raise RuntimeError("stop after recording")

    def stage(*args: Any, **kwargs: Any) -> int:
        rec.record(overlap)
        return 0

    monkeypatch.setattr("nexus.pipeline_stages._extractor_loop_bound_to_cancel", extractor)
    monkeypatch.setattr("nexus.pipeline_stages.chunker_loop", stage)
    monkeypatch.setattr("nexus.pipeline_stages.uploader_loop", stage)

    db, _engine = make_in_memory_pipeline_db()
    with pytest.raises(RuntimeError, match="stop after recording"):
        pipeline_index_pdf(
            Path("/corr/doc.pdf"), "corrhash1", "docs__test", throwaway_t3(),
            db=db, embed_fn=lambda texts, model: ([], ""), corpus="test", dry_run=True,
        )

    # The three stages are held at the barrier together: one Context shared by the three submits
    # raises at entry. The pool shuts down with wait=True on a stage failure, so all three have run.
    assert len(rec.seen) == 3, rec.seen
    assert {cid for _, cid in rec.seen} == {bound_id}


def test_aspect_enqueue_pool_workers_see_the_bound_id(bound_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_SERVICE_TOKEN", "test-token")
    monkeypatch.setattr("nexus.corpus.ensure_collection_registered", lambda *a, **k: None)
    queue = HttpAspectQueue(base_url="https://mock.test")
    rec = _Recorder()
    overlap = threading.Barrier(2)

    def out_replace(*args: Any, **kwargs: Any) -> tuple[str, str]:
        rec.record(overlap)
        return ("t", "created")

    rows = [{"collection": "code__x", "source_path": f"f{i}.py", "doc_id": f"1.1.{i}"} for i in range(4)]
    with patch.object(HttpTupleStore, "out_replace", side_effect=out_replace):
        assert queue.enqueue_many(rows) == 4

    ids = rec.ids_on("aspect-enqueue")
    assert len(ids) == len(rows), rec.seen
    assert set(ids) == {bound_id}


def _is_copy_context_call(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and (
        (isinstance(node.func, ast.Name) and node.func.id == "copy_context")
        or (isinstance(node.func, ast.Attribute) and node.func.attr == "copy_context")
    )


def _pool_calls(path: Path) -> tuple[int, int, list[int]]:
    """(``.submit(`` count, ``.map(`` count, line numbers of calls that do not carry a copied Context).

    A ``.submit(`` is clean when its first argument is ``copy_context().run`` or ``<name>.run`` for a
    name bound from ``copy_context()``, and each such name is used by one submit only (two submits on
    one name share a Context). A ``.map(`` is always flagged: it cannot take a Context per item, so a
    pool in these files submits per item instead.
    """
    tree = ast.parse(path.read_text())
    ctx_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_copy_context_call(node.value):
            ctx_names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    submits = maps = 0
    bad: list[int] = []
    uses: dict[str, int] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr == "map":
            maps += 1
            bad.append(node.lineno)
        elif node.func.attr == "submit":
            submits += 1
            first = node.args[0] if node.args else None
            if not (isinstance(first, ast.Attribute) and first.attr == "run"):
                bad.append(node.lineno)
            elif _is_copy_context_call(first.value):
                pass
            elif isinstance(first.value, ast.Name) and first.value.id in ctx_names:
                uses[first.value.id] = uses.get(first.value.id, 0) + 1
                if uses[first.value.id] > 1:
                    bad.append(node.lineno)
            else:
                bad.append(node.lineno)
    return submits, maps, sorted(bad)


@pytest.mark.parametrize(
    ("module", "expected_submits"),
    [
        ("indexer_utils.py", 1),
        ("chunk_batcher.py", 1),
        ("pipeline_stages.py", 3),
        ("db/t2/http_aspect_queue.py", 1),
    ],
)
def test_every_pool_submit_carries_a_copied_context(module: str, expected_submits: int) -> None:
    submits, maps, bad = _pool_calls(_SRC / module)
    assert submits >= 1, f"{module}: no .submit( call found; the pin would pass vacuously"
    assert submits == expected_submits, f"{module}: {submits} submits, expected {expected_submits}; update the pin on purpose"
    assert maps == 0, f"{module}: a pool .map( call carries no per-item Context; submit per item with copy_context().run"
    assert bad == [], f"{module}: line(s) {bad} do not pass a fresh copy_context().run as the submit's first argument"


def test_the_pin_rejects_a_bare_submit_a_map_and_a_shared_context(tmp_path: Path) -> None:
    src = tmp_path / "m.py"
    src.write_text(
        "import contextvars\n"
        "def f(pool, fn):\n"
        "    pool.submit(fn, 1)\n"  # line 3: bare
        "    pool.submit(contextvars.copy_context().run, fn, 1)\n"  # line 4: fine
        "    ctx = contextvars.copy_context()\n"
        "    pool.submit(ctx.run, fn, 2)\n"  # line 6: first use of ctx, fine
        "    pool.submit(ctx.run, fn, 3)\n"  # line 7: second use, shared
        "    list(pool.map(fn, [1]))\n"  # line 8: map
    )
    assert _pool_calls(src) == (4, 1, [3, 7, 8])
