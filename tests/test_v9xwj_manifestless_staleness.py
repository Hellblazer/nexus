# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-v9xwj: what the mark of a lost manifest-less document changes for the staleness check.

The heal marks such a document by failing its fence. The docs say an index run that walks its
file re-indexes a manifest-less document anyway, and that the mark only changes what the fence
says, EXCEPT for a document with a pre-RDR-108-Phase-3 chunk that still carries a ``doc_id``
metadata key: that key puts the document in the staleness cache, and then the mark (``never_fresh``)
is what makes the run re-index it. These tests pin both halves, storage-free.
"""
from __future__ import annotations

from types import SimpleNamespace

from nexus.indexer_utils import apply_catalog_content_hashes, build_staleness_cache, check_staleness

DOC = "1.1.7"
H = "h" * 64
MODEL = "m"


class _Col:
    name = "docs__t__m__v1"

    def __init__(self, metadata: dict) -> None:
        self._metadata = metadata

    def get_all_metadata(self) -> dict:
        return {"ids": ["c1"], "metadatas": [self._metadata]}


def _current(col: _Col, monkeypatch, *, fenced: bool = True, marked: bool = False) -> bool:
    """The full-run check for DOC at hash H: cache sweep, then the complete-fence overlay (a
    failed fence is not 'complete', so it lands in ``never_fresh`` instead)."""
    # A manifest-less document is in no manifest, so the catalog maps no chunk hash to it.
    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_reader",
        lambda *a, **k: SimpleNamespace(docs_for_chashes=lambda chashes: {}),
        raising=False,
    )
    cache = build_staleness_cache(col)
    if fenced and not marked:
        apply_catalog_content_hashes(cache, {DOC: H})
    if marked:
        cache.never_fresh = frozenset({DOC})
    return check_staleness(None, "f.md", H, MODEL, doc_id=DOC, cache=cache)


def test_a_manifestless_document_with_only_doc_id_free_chunks_re_indexes_whatever_its_fence(monkeypatch) -> None:
    """The premise of the docs: no ``doc_id`` on the chunk and no manifest row, so the document
    is not in the cache and reads stale, with a 'complete' fence or a marked one."""
    chunk = {"embedding_model": MODEL, "chunk_text_hash": "c1"}
    assert _current(_Col(chunk), monkeypatch, fenced=True) is False
    assert _current(_Col(chunk), monkeypatch, marked=True) is False


def test_a_legacy_chunk_carrying_doc_id_makes_a_complete_fence_read_current_and_the_mark_undoes_it(
    monkeypatch,
) -> None:
    """The exception the docs state: a chunk that still carries ``doc_id`` puts a manifest-less
    document in the cache, and a 'complete' fence at the file's hash then reads current. The mark
    fails the fence, which puts the document in ``never_fresh``: stale, so the run re-indexes it."""
    chunk = {"doc_id": DOC, "embedding_model": MODEL, "chunk_text_hash": "c1"}
    assert _current(_Col(chunk), monkeypatch, fenced=True) is True
    assert _current(_Col(chunk), monkeypatch, marked=True) is False
