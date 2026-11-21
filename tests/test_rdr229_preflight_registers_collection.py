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


def test_a_refusal_the_client_raises_before_sending_is_the_runs_error_not_a_registration_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The collection's registration refuses on the client's own checks (a profile that disagrees with the engine's, a
    retired name, a voyage intent with no key, a bad argument): nothing reached the engine, and the refusal is what the
    run reports, not a bare 'catalog registration failed' after the engine's 422 for the unregistered collection."""
    from nexus.catalog.write_outcome import PreSendArgumentError
    import nexus.doc_indexer as doc_indexer

    def refuse(*_a: object, **_k: object) -> None:
        raise PreSendArgumentError("the client refused before it sent anything")

    monkeypatch.setattr(doc_indexer, "ensure_collection_registered", refuse)
    note = tmp_path / "second.md"
    note.write_text("# Second\n\nbody\n")

    with pytest.raises(PreSendArgumentError):
        doc_indexer._register_or_lookup_doc_id(
            note, "p31preflight", content_type="markdown",
            physical_collection="docs__p31preflight__bge-base-en-v15-768__v1",
        )
