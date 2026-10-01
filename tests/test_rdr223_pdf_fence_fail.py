# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 (nexus-z0o2p.11 / .15 / .35): a failed run marks its fence and does nothing else.

``_fence_fail`` used to stamp the document ``failed`` and then rebuild its manifest from any chunks
the run stored (``_heal_failed_document``, nexus-0ntxj). That rebuild existed for the paths that wrote
chunks BEFORE their owner rows. Every writer now sends a chunk with its owner row in one request, so
a failed run has no ownerless chunk to give an owner, and the rebuild could only do harm: it found
chunks by content hash, so it could graft ANOTHER document's identical chunk onto the failed one, and
on a failed RE-index it replaced the manifest with a fragment found by the OLD content hash. It is
retired (nexus-z0o2p.35, M3), together with the ``heal`` parameter and its ``True`` default.
"""
from __future__ import annotations

import ast
import inspect
import pathlib
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def cat():
    c = MagicMock()
    with patch("nexus.catalog.factory.make_catalog_writer", return_value=c):
        yield c


def test_fence_fail_marks_the_fence_and_rebuilds_nothing(cat) -> None:
    from nexus.doc_indexer import _fence_fail

    with patch("nexus.catalog.manifest_heal.heal_manifest_gaps") as healer:
        _fence_fail("1.1.1", "boom")

    cat.fail_index_run.assert_called_once_with("1.1.1", "boom")
    healer.assert_not_called()


def test_the_failed_document_heal_and_its_parameter_are_gone() -> None:
    import nexus.doc_indexer as di

    assert not hasattr(di, "_heal_failed_document")
    assert list(inspect.signature(di._fence_fail).parameters) == ["doc_id", "error"]


def test_no_fence_fail_call_site_passes_a_heal_argument() -> None:
    """Every ``_fence_fail`` call in ``src/nexus`` is two positional arguments: a leftover
    ``heal=...`` would be a TypeError on the failure path, which must never raise."""
    root = pathlib.Path(inspect.getfile(inspect.getmodule(_fence_fail_ref()))).parent
    seen = 0
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_fence_fail":
                seen += 1
                assert not n.keywords, f"{path.name}:{n.lineno} _fence_fail with keyword arguments"
    assert seen >= 15, f"non-vacuity: expected to find the _fence_fail call sites, found {seen}"


def _fence_fail_ref():
    from nexus import doc_indexer

    return doc_indexer


def test_a_progress_callback_that_raises_after_the_stamp_does_not_fail_the_write() -> None:
    """The completion stamp rides ``finish()``, so the final ``(total, total)`` progress call comes
    AFTER the document is stamped complete. An exception from a progress callback there must not
    reach the caller's failure handling, which would mark the stamped document ``failed``.
    (Hooks cannot do this: ``HookRegistry`` contains a hook's ``Exception``; the progress callback
    is the one caller-supplied function that runs after the stamp with nothing containing it.)"""
    import hashlib

    from nexus.doc_indexer import _write_chunks_with_owner_rows

    class _Cat:
        def __init__(self) -> None:
            self.failed: list[str] = []

        def begin_index_run(self, *a, **k):
            return {"prior_chashes": [], "prior_count": 0}

        def write_manifest_many(self, docs, complete=None, *, sweep=False, chunks=None, collection, **kw):
            doc = docs[0][0]
            return {"chunks_written": len(chunks or []), "failed_doc_ids": [], "complete_refused": [],
                    "dropped_chashes": {doc: []}, "dropped_count": {doc: 0}}

        def fail_index_run(self, doc_id, error):
            self.failed.append(doc_id)

        def close(self) -> None:
            pass

    text = "progress chunk"
    chash = hashlib.sha256(text.encode()).hexdigest()
    cat = _Cat()

    def _bad_progress(done: int, total: int) -> None:
        raise RuntimeError("progress bar exploded")

    with patch("nexus.catalog.factory.make_catalog_writer", return_value=cat):
        result = _write_chunks_with_owner_rows(
            "docs__pfail__bge-base-en-v15-768__v1", "1.7.1", "h" * 64, [chash], [text],
            [{"chunk_text_hash": chash}], on_progress=_bad_progress)

    assert result.batches == 1
    assert cat.failed == [], "the fence was not marked failed"
