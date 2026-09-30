# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 (nexus-z0o2p.11 / .15): a failed PDF run does not heal.

``_fence_fail`` stamps the document ``failed`` and then rebuilds its manifest from any chunks the
run stored (``_heal_failed_document``, nexus-0ntxj). That rebuild exists for the paths that wrote
chunks BEFORE their owner rows. A PDF run's writer replaces the manifest with its first request and
every chunk it sends carries an owner row, so there is nothing to give an owner, and on a failed
RE-index the rebuild would replace the manifest with a fragment rebuilt from chunks found by the OLD
content hash. The PDF paths call ``_fence_fail(..., heal=False)``.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def cat():
    c = MagicMock()
    with patch("nexus.catalog.factory.make_catalog_writer", return_value=c):
        yield c


@pytest.mark.parametrize("heal,expected", [(True, 1), (False, 0)], ids=["default-heals", "heal-off"])
def test_fence_fail_heals_only_when_asked(cat, heal, expected) -> None:
    from nexus.doc_indexer import _fence_fail

    with patch("nexus.doc_indexer._heal_failed_document") as healer:
        _fence_fail("1.1.1", "boom", **({} if heal else {"heal": False}))

    cat.fail_index_run.assert_called_once_with("1.1.1", "boom")
    assert healer.call_count == expected


def test_the_default_still_heals_so_the_other_paths_are_unchanged(cat) -> None:
    from nexus.doc_indexer import _fence_fail

    with patch("nexus.doc_indexer._heal_failed_document") as healer:
        _fence_fail("1.1.1", "boom")
    healer.assert_called_once_with("1.1.1")


def test_a_failed_streaming_run_and_a_zero_chunk_run_do_not_heal() -> None:
    """The two ``_fence_fail`` calls in ``pipeline_index_pdf`` pass ``heal=False`` (the journey
    ``test_a_failed_second_request_leaves_the_freshly_minted_document_and_its_chunks`` shows the
    heal never runs against the real engine; this pins the argument at each site)."""
    import ast
    import pathlib

    import nexus.pipeline_stages as ps

    tree = ast.parse(pathlib.Path(ps.__file__).read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_fence_fail"]
    assert len(calls) == 2, "non-vacuity: the failure handler and the zero-chunk branch"
    for c in calls:
        heal = [k for k in c.keywords if k.arg == "heal"]
        assert heal and isinstance(heal[0].value, ast.Constant) and heal[0].value.value is False, \
            f"pipeline_stages.py:{c.lineno} _fence_fail without heal=False"


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
