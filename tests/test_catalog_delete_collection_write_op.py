# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-8tnz2 fix-round (code-review-expert Important finding): a
companion test for ``_SERVICE_ONLY_WRITE_OPS`` gaining ``delete_collection``
(``src/nexus/catalog/factory.py``), mirroring the established per-entry
precedent every other member of that frozenset already carries
(``tests/test_catalog_purge_trash.py``'s ``purge_trash`` pair,
``tests/test_gc_audit_record_client.py:50-60``'s ``record_gc_audit`` pair).

Grepped the whole tree for "_ServiceCatalogWriter" + "delete_collection"
together before this file: zero hits -- the only prior coverage was
incidental, via ``tests/test_reconcile_stale_drop_orphan_collections.py``'s
CliRunner-level tests, which happened to prove writer-not-reader routing
only because that file's ``_Cat`` fake (playing the ``cat`` param) has no
``delete_collection`` method at all. This file is the dedicated,
factory-level unit test the codebase's own convention requires.
"""
from __future__ import annotations

import pytest

from nexus.catalog.factory import _SERVICE_ONLY_WRITE_OPS, _ServiceCatalogWriter


def test_delete_collection_is_a_whitelisted_service_write_op() -> None:
    """``delete_collection`` reaches the engine's cascaded
    ``/collections/delete`` route through the write-only proxy; a read-side
    handle forwards it ungated too (``_SharedServiceCatalogHandle`` has no
    whitelist at all — see the frozenset's own comment), but every mutation
    in this codebase is expected to route through the writer deliberately,
    and this whitelist is what makes that routing possible without an
    AttributeError."""
    assert "delete_collection" in _SERVICE_ONLY_WRITE_OPS

    class _Backend:
        def delete_collection(self, name):
            return {"chunks": 3, "catalog_documents": 0}

    assert _ServiceCatalogWriter(_Backend()).delete_collection("code__orphan") == {
        "chunks": 3, "catalog_documents": 0,
    }
    with pytest.raises(AttributeError):
        _ServiceCatalogWriter(_Backend()).some_unwhitelisted_name
