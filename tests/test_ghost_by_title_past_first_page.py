# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ghost-by-title reconcile in ``catalog_store_hook_tracked`` must find a
ghost wherever it ranks in the full-text matches (nexus-3bafq sibling,
critic finding relayed by nexus-bf).

``_find_ghost_by_title`` filtered ``reader.find(title)``, which returns the
engine's default 50 rows. On a real tenant a same-titled ghost past row 50
went unmatched and ``register()`` minted a duplicate document.
"""
from __future__ import annotations

import pytest

from nexus.catalog.factory import make_catalog_reader, make_catalog_writer

pytestmark = pytest.mark.integration

_COLLECTION = "knowledge__ghostpage__bge-base-en-v15-768__v1"
_TITLE = "ghostpage target"


def test_ghost_ranked_past_the_first_page_is_reconciled(t2_service_env):
    from nexus.catalog.store_hook import catalog_store_hook_tracked

    reader = make_catalog_reader()
    writer = make_catalog_writer()
    assert reader is not None
    owner = writer.register_owner("knowledge", "curator")
    # 70 matching decoys registered first: the page comes back in tumbler
    # string order, so the ghost's later tumbler (1.x.71) sorts past row 50.
    for i in range(70):
        writer.register(
            owner=owner, title=f"{_TITLE} decoy {i}",
            content_type="knowledge", physical_collection=_COLLECTION,
        )
    ghost = str(writer.register(
        owner=owner, title=_TITLE, content_type="knowledge",
        physical_collection=_COLLECTION,
    ))
    page = [str(e.tumbler) for e in reader.find(_TITLE, content_type="knowledge")]
    assert ghost not in page, "control: the ghost must rank past the default page"

    tumbler, created = catalog_store_hook_tracked(
        title=_TITLE, doc_id="a" * 64, collection_name=_COLLECTION,
    )

    assert created is False, "the ghost must be reconciled, not duplicated"
    assert tumbler == ghost
    same_title = [e for e in reader.find_all(_TITLE, content_type="knowledge") if e.title == _TITLE]
    assert len(same_title) == 1, same_title

    # nexus-gacz0, folded in here so it costs no extra tenant: the engine's exact-title
    # route answers what the full-text paging above had to filter. A client of our own
    # (the shared reader hides its transport) asks the route directly, so this is the
    # route and not the fallback, then through find_by_title_exact with and without the
    # owner scope; an unrelated owner finds nothing.
    from nexus.catalog.http_catalog_client import HttpCatalogClient  # noqa: PLC0415 — test-local import

    cat = HttpCatalogClient(tenant=t2_service_env)
    try:
        raw = cat._get("/by_title", title=_TITLE, content_type="knowledge")
        assert [d["tumbler"] for d in raw["documents"]] == [ghost] and raw["count"] == 1
        assert [str(e.tumbler) for e in cat.find_by_title_exact(_TITLE, content_type="knowledge")] == [ghost]
        assert [str(e.tumbler) for e in cat.find_by_title_exact(_TITLE, owner=owner)] == [ghost]
        assert cat.find_by_title_exact(_TITLE, owner="9999") == []
        assert getattr(cat, "_by_title_backoff", None) is None, "the route must not have fallen back"
    finally:
        cat.close()
