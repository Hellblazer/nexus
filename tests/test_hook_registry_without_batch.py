# SPDX-License-Identifier: AGPL-3.0-or-later
"""HookRegistry.without_batch (nexus-wbfpw.40): a copy that leaves one batch
hook out, keeps every other registration and its classification, and does
not change the source registry."""
from __future__ import annotations

from nexus.hook_registry import HookRegistry


def _plain(doc_ids, collection, contents, embeddings, metadatas):
    pass


def _with_doc_id(doc_ids, collection, contents, embeddings, metadatas, *, catalog_doc_id=""):
    pass


def _document(source_path, collection, content, *, doc_id=""):
    pass


def _single(doc_id, collection, content):
    pass


def test_without_batch_drops_one_hook_and_keeps_the_rest() -> None:
    src = HookRegistry()
    src.register_single(_single)
    src.register_batch(_plain)
    src.register_batch(_with_doc_id)
    src.register_document(_document)

    copy = src.without_batch(_with_doc_id)

    assert copy._batch == [_plain]
    assert id(_with_doc_id) not in copy._batch_with_catalog_doc_id
    assert copy._single == [_single] and copy._document == [_document]
    assert copy._document_with_doc_id == src._document_with_doc_id
    # The source registry is untouched.
    assert src._batch == [_plain, _with_doc_id]
    assert id(_with_doc_id) in src._batch_with_catalog_doc_id


def test_without_batch_keeps_a_remaining_hooks_classification() -> None:
    src = HookRegistry()
    src.register_batch(_with_doc_id)
    src.register_batch(_plain)
    copy = src.without_batch(_plain)
    assert copy._batch == [_with_doc_id]
    assert id(_with_doc_id) in copy._batch_with_catalog_doc_id
