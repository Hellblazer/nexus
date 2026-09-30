# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-7tys2: re-registering a collection must not replace its owner.

The client registers a collection before its first chunk write in every
process (``ensure_collection_registered``), sending the owner segment it
parses out of the collection NAME. The engine's ``/collections/upsert``
used to write that over an owner someone had registered on purpose, so a
slug-named collection (``code__arcaneum-2ad2825c__...``) ended up with the
slug as its ``catalog_collections.owner_id``. Real engine substrate: the
overwrite happens in the engine's ON CONFLICT arm, so no fake reproduces it.

The subject is that registration call, which every chunk write makes first.
This test calls it directly: since RDR-223 P3.1 a bare chunk write is not
available to drive it (the engine refuses an ownerless write from Phase 3 on),
and the chunk was never what the property is about.
"""
from __future__ import annotations


import pytest

from nexus import corpus
from nexus.catalog.factory import make_catalog_reader, make_catalog_writer

_MODEL = "bge-base-en-v15-768"
# The production shape: the hyphenated owner segment (owner_segment_for_tumbler), not the dotted tumbler.
OWNER_SEGMENT = "1-1"


@pytest.fixture(autouse=True)
def _clear_registration_cache():
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()
    yield
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()


@pytest.mark.integration
def test_reregistration_keeps_the_owner_the_collection_was_registered_with(t2_service_env):
    name = f"code__dbgslug-7tys2__{_MODEL}__v1"
    writer = make_catalog_writer(priority="interactive")
    reader = make_catalog_reader()
    writer.register_collection(
        name, content_type="code", owner_id=OWNER_SEGMENT, embedding_model=_MODEL,
    )
    assert reader.get_collection(name)["owner_id"] == OWNER_SEGMENT, "guard: registered with the owner segment"

    # A fresh process has an empty registration cache, so this call
    # re-registers the name with the owner segment parsed from it: the exact
    # request every chunk write makes before its first write.
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()
    corpus.ensure_collection_registered(name)
    assert name in corpus._REGISTERED_COLLECTIONS or any(
        n == name for _scope, n in corpus._REGISTERED_COLLECTIONS_SCOPED
    ), (
        "non-vacuity: the call went through the registration path, "
        "the one that sends the name's segment"
    )

    assert reader.get_collection(name)["owner_id"] == OWNER_SEGMENT
