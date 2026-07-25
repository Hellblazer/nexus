# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Every service-backed raw-handle guard must raise AttributeError, never else.

nexus-xj744. ``db/t2/_raw_handle_guard.py`` states the contract in its own
docstring: the guard properties raise ``AttributeError`` and *never*
``RuntimeError``, because "a RuntimeError would propagate through ``hasattr``
and break the guard contract".

That is not stylistic. ``hasattr()`` swallows only ``AttributeError``; anything
else propagates. The whole sanctioned service-mode idiom is::

    if not has_raw_access(store):
        ...take the service branch...

A guard raising ``RuntimeError`` turns that safe probe into a crash in service
mode — the mechanism that exists to make the check safe becomes the thing that
breaks it, and only in the deployment where it matters.

``HttpCatalogClient._db`` was violating this (found by the nexus-at2ff sweep,
2026-07-25). It was latent — no live ``hasattr(cat, "_db")`` caller existed —
but a contract nobody enforces is a contract each new store re-litigates. This
file enforces it across the whole family so the next service-backed store
cannot reintroduce it.
"""
from __future__ import annotations

import pytest

from nexus.db.storage_mode import has_raw_access


def _service_stores() -> list[tuple[str, object]]:
    """Instantiate every service-backed store WITHOUT touching the network.

    ``__new__`` deliberately: these classes resolve an endpoint in ``__init__``,
    which would need a live service. The guard properties under test are pure
    class-level descriptors and need no instance state.
    """
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    from nexus.db.t2.http_aspect_queue import HttpAspectQueue
    from nexus.db.t2.http_chash_index import HttpChashIndex
    from nexus.db.t2.http_document_aspects_store import HttpDocumentAspectsStore
    from nexus.db.t2.http_document_highlights_store import HttpDocumentHighlightsStore
    from nexus.db.t2.http_memory_store import HttpMemoryStore
    from nexus.db.t2.http_plan_library import HttpPlanLibrary
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
    from nexus.db.t2.http_telemetry_store import HttpTelemetryStore

    classes = [
        HttpAspectQueue, HttpChashIndex, HttpDocumentAspectsStore,
        HttpDocumentHighlightsStore, HttpMemoryStore, HttpPlanLibrary,
        HttpTaxonomyStore, HttpTelemetryStore, HttpCatalogClient,
    ]
    return [(c.__name__, c.__new__(c)) for c in classes]


#: Raw-handle attributes the guards cover. ``_db`` is the catalog client's
#: equivalent of the T2 stores' ``conn``.
_GUARDED_ATTRS = ("conn", "_lock", "_db")


def test_the_store_roster_is_not_empty() -> None:
    """Non-vacuity: every assertion below iterates the roster, so an import
    rename that silently empties it would make this file pass by doing
    nothing."""
    stores = _service_stores()
    assert len(stores) >= 9, [n for n, _ in stores]


@pytest.mark.parametrize("name,store", _service_stores(), ids=lambda v: v if isinstance(v, str) else "")
def test_guard_raises_attributeerror_not_runtimeerror(name: str, store: object) -> None:
    """The contract itself, per store and per guarded attribute."""
    for attr in _GUARDED_ATTRS:
        if attr not in dir(type(store)):
            continue  # this store does not define that guard; nothing to check
        with pytest.raises(AttributeError) as exc:
            getattr(store, attr)
        # RuntimeError is the specific violation found in the wild; assert the
        # exact type rather than just "an exception", or a RuntimeError
        # subclass of AttributeError-lookalike would slip through.
        assert not isinstance(exc.value, RuntimeError), (
            f"{name}.{attr} raised a RuntimeError. hasattr() does not swallow "
            f"it, so has_raw_access({name}) CRASHES in service mode instead of "
            f"returning False (nexus-xj744)."
        )


@pytest.mark.parametrize("name,store", _service_stores(), ids=lambda v: v if isinstance(v, str) else "")
def test_hasattr_returns_false_rather_than_raising(name: str, store: object) -> None:
    """The consequence that actually matters to callers.

    This is the assertion that would have caught HttpCatalogClient._db: the
    guard's *type* is an implementation detail, but `hasattr` returning False
    is the behaviour every service-mode branch depends on.
    """
    for attr in _GUARDED_ATTRS:
        if attr not in dir(type(store)):
            continue
        assert hasattr(store, attr) is False, (
            f"hasattr({name}, {attr!r}) did not return False — it either "
            f"raised (guard contract violated) or the attribute really exists."
        )


@pytest.mark.parametrize("name,store", _service_stores(), ids=lambda v: v if isinstance(v, str) else "")
def test_has_raw_access_is_false_for_every_service_store(name: str, store: object) -> None:
    """The sanctioned probe must work on all of them."""
    assert has_raw_access(store) is False, (
        f"has_raw_access({name}) is not False — service-mode branches keyed on "
        f"it would take the raw-SQLite path against a service-backed store."
    )
