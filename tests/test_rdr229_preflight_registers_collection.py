# SPDX-License-Identifier: AGPL-3.0-or-later
"""The doc_indexer pre-flight registers the document's collection before the document (RDR-229 P3.1, nexus-p2m1x.17).

Since P3.1 the engine refuses ``/v1/catalog/doc/register`` and ``/doc/resolve_or_register`` for a collection that has
no registry row (422 ``unregistered_collection``). ``index_markdown`` registers the document in its pre-flight, ahead
of ``_index_document``, whose own registration (``_register_before_read``) runs later, so the first markdown file
into a new collection was refused and the run failed as ``CatalogIdentityMissingError``. The pre-flight registers the
collection first, through the registrar the catalog writer registers with.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.t2_tenant


def test_the_first_document_of_a_new_collection_registers_the_collection_first(tmp_path: Path) -> None:
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    from nexus.doc_indexer import _register_or_lookup_doc_id

    collection = "docs__p31preflight__bge-base-en-v15-768__v1"
    note = tmp_path / "first.md"
    note.write_text("# First\n\nbody\n")

    with HttpCatalogClient() as cat:
        assert cat.get_collection(collection) is None, "the seed must start with no registry row"

    doc_id = _register_or_lookup_doc_id(note, "p31preflight", content_type="markdown", physical_collection=collection)

    assert doc_id, "the pre-flight registered no document: the engine refused it under an unregistered collection"
    with HttpCatalogClient() as cat:
        assert cat.get_collection(collection) is not None


def test_a_refused_registration_leaves_a_collection_that_already_has_its_row_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registration is best effort: the client's own checks can refuse it (a profile that disagrees with the
    engine's, a retired name, a voyage intent with no key) for a collection whose row already exists, and the document
    still registers, since the engine only needs the row. A real refusal is raised again by the chunk write's own
    registration, with its own message."""
    from nexus.catalog.write_outcome import PreSendArgumentError
    from tests._catalog_fixture_ops import register_seed_collections

    import nexus.doc_indexer as doc_indexer

    collection = "docs__p31preflightrow__bge-base-en-v15-768__v1"
    register_seed_collections(collection)

    def refuse(*_a: object, **_k: object) -> None:
        raise PreSendArgumentError("the client refused before it sent anything")

    monkeypatch.setattr(doc_indexer, "ensure_collection_registered", refuse)
    note = tmp_path / "second.md"
    note.write_text("# Second\n\nbody\n")

    assert doc_indexer._register_or_lookup_doc_id(
        note, "p31preflight", content_type="markdown", physical_collection=collection,
    )
