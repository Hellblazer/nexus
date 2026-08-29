# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-sz89e: a ghost document is cleaned up by the production delete paths.

Every production caller of ``store_delete_catalog_cleanup`` and
``reap_catalog_manifest_for_chashes`` (MCP ``store_delete``, ``nx store
delete``, ``HttpVectorClient.expire``) passes a real ``expected_collection``.
The nexus-c53hy / nexus-h7nax collection-mismatch guard then compares it to
``entry.physical_collection`` and, on inequality, does nothing — which is
right for a document that lives in another collection, and wrong for a
GHOST: a ghost's ``physical_collection`` is blank (the documented live
population; 228 were tombstoned by ``reconcile-stale`` on 2026-08-28), so it
"mismatches" every target and the nexus-d9fwj retraction fix below it was
never reached from production.

The rule this pins: a blank ``physical_collection`` has no collection to
mismatch. The guard still refuses a document that names a DIFFERENT
collection (the c53hy protection, asserted here too so the fix cannot widen
it).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tests._catalog_fixture_ops import ActiveCatalog, count_documents, seed_manifest_chunks

_TARGET = "knowledge__sz89e__voyage-context-3__v1"
_OTHER = "knowledge__elsewhere__voyage-context-3__v1"


@pytest.fixture(autouse=True)
def _point_catalog_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "catalog"))


def _chash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _register(cat: ActiveCatalog, *, title: str, chash: str, physical_collection: str | None) -> str:
    """A knowledge doc resolvable by chash; ``physical_collection=None`` registers a GHOST."""
    owner = cat.register_owner("sz89e", "curator")
    kwargs = {"meta": {"doc_id": chash}}
    if physical_collection is not None:
        kwargs["physical_collection"] = physical_collection
    tumbler = cat.register(owner, title, content_type="knowledge", **kwargs)
    seed_manifest_chunks(_TARGET, [chash])
    cat.append_manifest_chunks(str(tumbler), [{"chash": chash, "position": 0}], collection=_TARGET)
    cat.resync_chunk_count_cache(str(tumbler))
    return str(tumbler)


def test_store_delete_cleanup_reaches_a_ghost_when_the_caller_names_a_real_collection() -> None:
    """The production shape: expected_collection is the collection the caller
    is deleting from. Before the fix the mismatch guard short-circuited on the
    ghost's blank collection and the catalog row survived."""
    from nexus.catalog.store_hook import store_delete_catalog_cleanup

    cat = ActiveCatalog()
    chash = _chash("sz89e-ghost")
    _register(cat, title="sz89e Ghost", chash=chash, physical_collection=None)
    assert count_documents() == 1

    deleted_tumbler, error = store_delete_catalog_cleanup(chash, expected_collection=_TARGET)

    assert error == "", error
    assert deleted_tumbler, "the ghost's tumbler must be reported as deleted"
    assert count_documents() == 0, "the ghost's catalog row must be tombstoned"


def test_reap_reaches_a_ghost_when_the_caller_names_a_real_collection() -> None:
    from nexus.catalog.store_hook import reap_catalog_manifest_for_chashes

    cat = ActiveCatalog()
    chash = _chash("sz89e-ghost-reap")
    _register(cat, title="sz89e Ghost Reap", chash=chash, physical_collection=None)
    assert count_documents() == 1

    reap_catalog_manifest_for_chashes([chash], expected_collection=_TARGET)

    assert count_documents() == 0, "the ghost's catalog row must be tombstoned by the reap"


def test_a_document_in_another_collection_is_still_protected() -> None:
    """c53hy stays intact: a NON-blank collection that differs from the
    caller's target is never touched by either path."""
    from nexus.catalog.store_hook import (
        reap_catalog_manifest_for_chashes,
        store_delete_catalog_cleanup,
    )

    cat = ActiveCatalog()
    chash = _chash("sz89e-other-owner")
    _register(cat, title="sz89e Other Owner", chash=chash, physical_collection=_OTHER)
    assert count_documents() == 1

    deleted_tumbler, error = store_delete_catalog_cleanup(chash, expected_collection=_TARGET)
    assert (deleted_tumbler, error) == ("", "")
    reap_catalog_manifest_for_chashes([chash], expected_collection=_TARGET)
    assert count_documents() == 1, "a document that names another collection must survive both paths"
