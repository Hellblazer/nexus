# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-167 Phase 1 (nexus-dngtz.1.2): the correlation id reaches the three indexer thread pools.

A bare ``ThreadPoolExecutor.submit`` does not carry a ``ContextVar`` into the worker thread, so the id
the CLI binds is lost before ``nx index repo`` makes its engine calls from the "nx-index" pool, the
"nx-flush" pool and the PDF pipeline's three-stage pool. Each submit must run its callable inside a
fresh ``contextvars.copy_context()`` (one Context cannot be entered by two threads at once).

Every behaviour test records the id INSIDE the worker thread (and the thread's name, so a worker that
never ran cannot pass), then asserts on the record after the pool is done. The source pin is the
backstop: it names each file, fails on a bare ``.submit(``, and fails when a file has no submit.
"""
from __future__ import annotations

import ast
import threading
from pathlib import Path
from typing import Any

import pytest

from nexus import correlation
from nexus.chunk_batcher import ChunkBatcher
from nexus.db.inmemory_pipeline import make_in_memory_pipeline_db
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

    def extractor(*args: Any, **kwargs: Any) -> None:
        rec.record()
        raise RuntimeError("stop after recording")

    def stage(*args: Any, **kwargs: Any) -> int:
        rec.record()
        return 0

    monkeypatch.setattr("nexus.pipeline_stages._extractor_loop_bound_to_cancel", extractor)
    monkeypatch.setattr("nexus.pipeline_stages.chunker_loop", stage)
    monkeypatch.setattr("nexus.pipeline_stages.uploader_loop", stage)

    db, _engine = make_in_memory_pipeline_db()
    with pytest.raises(RuntimeError, match="stop after recording"):
        pipeline_index_pdf(
            Path("/corr/doc.pdf"), "corrhash1", "docs__test", throwaway_t3(),
            db=db, embed_fn=lambda texts, model: [], corpus="test", dry_run=True,
        )

    # The pool shuts down with wait=True on a stage failure, so all three stages have run.
    assert len(rec.seen) == 3, rec.seen
    assert all(name.startswith("ThreadPoolExecutor") for name, _ in rec.seen), rec.seen
    assert {cid for _, cid in rec.seen} == {bound_id}


def _is_copy_context_run(node: ast.expr, ctx_names: set[str]) -> bool:
    """``copy_context().run`` / ``contextvars.copy_context().run`` / ``<name bound from copy_context()>.run``."""
    if not (isinstance(node, ast.Attribute) and node.attr == "run"):
        return False
    base = node.value
    if isinstance(base, ast.Name):
        return base.id in ctx_names
    if isinstance(base, ast.Call):
        func = base.func
        return (isinstance(func, ast.Name) and func.id == "copy_context") or (
            isinstance(func, ast.Attribute) and func.attr == "copy_context"
        )
    return False


def _bare_submits(path: Path) -> tuple[int, list[int]]:
    """(count of ``.submit(`` calls, line numbers of those whose first argument is not a copied Context's run)."""
    tree = ast.parse(path.read_text())
    ctx_names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and (
                (isinstance(node.value.func, ast.Name) and node.value.func.id == "copy_context")
                or (isinstance(node.value.func, ast.Attribute) and node.value.func.attr == "copy_context")
            )
        ):
            ctx_names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    total = 0
    bare: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "submit":
            total += 1
            if not node.args or not _is_copy_context_run(node.args[0], ctx_names):
                bare.append(node.lineno)
    return total, bare


@pytest.mark.parametrize(
    ("module", "expected_submits"),
    [
        ("indexer_utils.py", 1),
        ("chunk_batcher.py", 1),
        ("pipeline_stages.py", 3),
    ],
)
def test_every_pool_submit_carries_a_copied_context(module: str, expected_submits: int) -> None:
    total, bare = _bare_submits(_SRC / module)
    assert total >= 1, f"{module}: no .submit( call found; the pin would pass vacuously"
    assert total == expected_submits, f"{module}: {total} submits, expected {expected_submits}; update the pin on purpose"
    assert bare == [], f"{module}: submit at line(s) {bare} does not pass copy_context().run as its first argument"


def test_the_pin_rejects_a_bare_submit(tmp_path: Path) -> None:
    src = tmp_path / "m.py"
    src.write_text(
        "import contextvars\n"
        "def f(pool, fn):\n"
        "    pool.submit(fn, 1)\n"
        "    pool.submit(contextvars.copy_context().run, fn, 1)\n"
        "    ctx = contextvars.copy_context()\n"
        "    pool.submit(ctx.run, fn, 2)\n"
    )
    assert _bare_submits(src) == (3, [3])
