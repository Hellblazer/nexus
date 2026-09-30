# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pbwx: a code/docs/rdr collection's owner_id is its documents' owner segment.

The client registers a collection under the owner segment it parses out of the
collection NAME, a slug for a legacy repo (``code__arcaneum-2ad2825c__...``).
The documents in it live under a tumbler and that is the authoritative owner:
the engine writes the hyphenated owner segment (``owner_segment_for_tumbler``,
``1-15``) once a document lands in the collection. ``get_collection_owner_root``
and ``collections_by_owner`` key on that segment, so they find the collection
only after the repair. Real engine substrate: the rule lives in the engine.
"""
from __future__ import annotations

import pytest

from nexus import corpus
from nexus.catalog.collection_name import owner_segment_for_tumbler
from nexus.catalog.factory import make_catalog_reader, make_catalog_writer

_CODE_MODEL = "bge-base-en-v15-768"
_CTX_MODEL = "bge-base-en-v15-768"


@pytest.fixture(autouse=True)
def _clear_registration_cache():
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()
    yield
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()


@pytest.mark.integration
def test_slug_named_repo_collection_takes_its_documents_owner_and_is_found_by_owner(
    t2_service_env, tmp_path,
):
    reader = make_catalog_reader()
    writer = make_catalog_writer(priority="interactive")
    repo_root = tmp_path / "arcaneum"
    repo_root.mkdir()
    owner = writer.register_owner(
        "6pbwx-arcaneum", "repo", repo_hash="6pbwxarcaneumcafe0", repo_root=repo_root,
    )
    segment = owner_segment_for_tumbler(str(owner))
    assert segment and segment.replace("-", "").isdigit(), "guard: the production shape, hyphenated"

    name = f"code__arcaneum-6pbwx2ad__{_CODE_MODEL}__v1"
    slug = "arcaneum-6pbwx2ad"
    writer.register_collection(name, content_type="code", owner_id=slug, embedding_model=_CODE_MODEL)
    assert reader.get_collection(name)["owner_id"] == slug, "guard: registered with the slug"
    assert reader.get_collection_owner_root(name) == (slug, ""), "guard: the slug matches no owner row"

    writer.register(
        owner=owner, title="main.py", content_type="code", file_path="main.py",
        physical_collection=name,
    )

    assert reader.get_collection(name)["owner_id"] == segment
    got_owner, got_root = reader.get_collection_owner_root(name)
    assert got_owner == segment
    assert got_root == str(repo_root), "collectionOwnerRoot joins the segment back to the owner row"
    assert name in [c["name"] for c in reader.collections_by_owner(segment)]

    # The re-registration a cold chunk write sends carries the slug again; it changes nothing.
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()
    writer.register_collection(name, content_type="code", owner_id=slug, embedding_model=_CODE_MODEL)
    assert reader.get_collection(name)["owner_id"] == segment


@pytest.mark.integration
def test_knowledge_collection_owner_is_not_rewritten_to_its_documents_tumbler(t2_service_env):
    reader = make_catalog_reader()
    writer = make_catalog_writer(priority="interactive")
    curator = writer.register_owner("knowledge", "curator")
    subject = "6pbwx-distributed-systems"
    name = f"knowledge__{subject}__{_CTX_MODEL}__v1"
    writer.register_collection(name, content_type="knowledge", owner_id=subject, embedding_model=_CTX_MODEL)
    writer.register(
        owner=curator, title="raft note", content_type="knowledge", physical_collection=name,
        source_uri=f"chroma://{name}/raft-note",
    )
    assert owner_segment_for_tumbler(str(curator)), "guard: the document has an owner segment to derive"
    assert reader.get_collection(name)["owner_id"] == subject
