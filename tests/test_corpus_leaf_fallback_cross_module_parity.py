# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-module parity for the RDR-103 Phase 5 leaf-fallback collection-name
derivation (nexus-nb3yg/c8hl7/gtl01 review round: code-review-expert S1 /
substantive-critic Q3 residual).

Before this file, the ``docs__<corpus>__<model>__v1`` formula was
hand-duplicated at five call sites (three in ``doc_indexer.py``, two in
``commands/index.py``); the reviewer noted the *existing* tests pinned
``commands/index.py``'s copy against a hardcoded string literal rather than
against ``doc_indexer.py``'s copy — a ``doc_indexer`` drift would not have
failed them. Both call sites now import the SAME shared helper,
``nexus.corpus.docs_leaf_fallback_collection_name`` — this file proves that
wiring by DRIVING each real call site (not reading its source) and asserting
its output equals a direct call to the shared helper, for a corpus name that
exercises the ``_`` -> ``-`` rewrite.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from nexus.corpus import docs_leaf_fallback_collection_name

_UNDERSCORE_CORPUS = "my_underscore_corpus"
_FAKE_MODEL = "bge-base-en-v15-768"


def _dummy_embed(docs, model):
    return ([[0.0]] * len(docs), model)


class _RecordingCol:
    """T3 collection stub: records the name it was constructed under via
    the enclosing ``_RecordingT3`` and always reports no existing chunks so
    the freshness check falls through to the chunk_fn call."""

    def get(self, where=None, include=None, limit=None, ids=None, **kw):
        return {"metadatas": []}


class _RecordingT3:
    def __init__(self) -> None:
        self.requested_names: list[str] = []
        self._col = _RecordingCol()

    def get_or_create_collection(self, name: str) -> _RecordingCol:
        self.requested_names.append(name)
        return self._col

    def collection_exists(self, name: str) -> bool:
        # nexus-o5x2c: _index_document now resolves its write client
        # BEFORE the leaf-fallback derivation and passes
        # db.collection_exists as the grandfather probe — this stub
        # reports nothing pre-existing, matching a genuinely fresh
        # recording double (the probe is exercised directly in
        # tests/test_rdr_109_phase2_dispatch.py; this file only needs
        # the attribute to exist so real production wiring doesn't
        # AttributeError on a test double that's missing a method every
        # real T3 client has).
        return False


def test_direct_helper_rewrites_underscores_to_hyphens() -> None:
    """Baseline: the shared helper itself, for the corpus shape this file
    is built around."""
    with patch("nexus.corpus.effective_embedding_model_for_writes", return_value=_FAKE_MODEL):
        name = docs_leaf_fallback_collection_name(_UNDERSCORE_CORPUS)
    assert name == f"docs__my-underscore-corpus__{_FAKE_MODEL}__v1"


def test_doc_indexer_index_document_leaf_fallback_matches_shared_helper(tmp_path) -> None:
    """Drives the real ``_index_document`` leaf-fallback branch
    (``doc_indexer.py`` — the ``collection_name is None`` site shared by
    ``index_markdown``'s caller) and proves the collection name it actually
    requests from T3 equals a direct call to the shared helper for the same
    corpus — real production wiring, not a duplicated literal."""
    from nexus.doc_indexer import _index_document

    doc_path = tmp_path / "doc.md"
    doc_path.write_text("content")

    t3 = _RecordingT3()
    chunk_fn = MagicMock(return_value=[])  # short-circuits at `if not prepared: return 0`
    # before doc_id/catalog registration is ever reached.

    with patch("nexus.corpus.effective_embedding_model_for_writes", return_value=_FAKE_MODEL):
        expected = docs_leaf_fallback_collection_name(_UNDERSCORE_CORPUS)
        result = _index_document(
            doc_path, _UNDERSCORE_CORPUS, chunk_fn, t3,
            collection_name=None, embed_fn=_dummy_embed, force=False,
        )

    assert result == 0
    assert t3.requested_names == [expected]


def test_index_run_refused_message_corpus_fallback_matches_shared_helper() -> None:
    """Drives the real ``commands.index._index_run_refused_message``
    corpus-only fallback (the nexus-nb3yg fix site) and proves the derived
    name it compares against equals a direct call to the shared helper for
    the same underscore-bearing corpus."""
    from nexus.commands.index import _index_run_refused_message

    class _FakeRefused:
        doc_id = "1.2.3"
        referenced = 4
        present = 1
        missing = 3

    class _FakeEntry:
        def __init__(self, physical_collection: str) -> None:
            self.physical_collection = physical_collection

    class _FakeReader:
        def __init__(self, entry) -> None:
            self._entry = entry

        def resolve(self, doc_id: str):
            return self._entry

    with patch("nexus.corpus.effective_embedding_model_for_writes", return_value=_FAKE_MODEL):
        expected = docs_leaf_fallback_collection_name(_UNDERSCORE_CORPUS)
        with patch(
            "nexus.catalog.factory.make_catalog_reader",
            return_value=_FakeReader(_FakeEntry(expected)),
        ):
            msg = _index_run_refused_message(
                _FakeRefused(), target_collection="", corpus=_UNDERSCORE_CORPUS,
            )

    assert "collection check: confirmed" in msg
    assert expected in msg
