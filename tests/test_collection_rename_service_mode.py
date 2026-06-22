# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-164 P3 (nexus-77vve) — service-mode branch of ``rename_collection_data_plane``.

In service mode the entire collection re-home is ONE atomic
``CatalogRepository.renameCollection`` on the Java service. The client must fold
the SQLite-era fan-out (T2 cascade + separate Chroma rename + catalog cascade)
into a single ``rename_collection_cascade`` call, map the per-table counts back,
and NOT issue a separate local T3 rename (the pgvector chunks moved inside the
same transaction). A service failure is atomic, so it raises (not fail-open).

Local-mode coverage lives in ``test_collection_rename.py`` (sqlite-pinned).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import click
import pytest

from nexus.collection_rename import (
    remap_collection_references,
    rename_collection_data_plane,
)
from nexus.db.storage_mode import StorageBackend


@pytest.fixture(autouse=True)
def _pin_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "nexus.db.storage_mode.storage_backend_for",
        lambda store: StorageBackend.SERVICE,
    )


def _fake_t3(*, old_exists: bool = True, new_exists: bool = False) -> MagicMock:
    t3 = MagicMock()
    t3.collection_exists = MagicMock(
        side_effect=lambda name: old_exists if name == "code__old"
        else new_exists if name == "code__new" else False
    )
    t3.rename_collection = MagicMock()
    return t3


_SERVER_COUNTS = {
    "catalog_collections_inserted": 1,
    "chunks_384": 4,
    "chunks_768": 0,
    "chunks_1024": 0,
    "chash_index": 3,
    "topic_assignments": 2,
    "topics": 1,
    "taxonomy_meta": 1,
    "taxonomy_centroids_384": 1,
    "document_aspects": 2,
    "document_highlights": 1,
    "aspect_extraction_queue": 2,
    "catalog_documents": 1,
    "relevance_log": 2,
    "search_telemetry": 2,
    "hook_failures": 1,
    "catalog_collections_deleted": 1,
}


def test_service_mode_uses_single_endpoint_and_maps_counts() -> None:
    t3 = _fake_t3()
    client = MagicMock()
    client.rename_collection_cascade = MagicMock(return_value=dict(_SERVER_COUNTS))

    counts = rename_collection_data_plane(
        "code__old", "code__new", t3_db=t3, catalog=client
    )

    # ONE atomic call to the consolidated endpoint with the canonical args.
    client.rename_collection_cascade.assert_called_once_with("code__old", "code__new")
    # No separate local T3 rename — the service re-homed the pgvector chunks.
    t3.rename_collection.assert_not_called()

    # Server per-table counts mapped onto the data-plane's count keys.
    assert counts["tax_topics"] == 1
    assert counts["tax_assignments"] == 2
    assert counts["tax_meta"] == 1
    assert counts["chash"] == 3
    assert counts["aspects"] == 2
    assert counts["aspect_queue"] == 2
    assert counts["highlights"] == 1
    assert counts["tax_centroids"] == 1  # 384:1 + 768:0 + 1024:0
    assert counts["relevance_log"] == 2
    assert counts["search_telemetry"] == 2
    assert counts["hook_failures"] == 1
    assert counts["catalog_docs"] == 1


def test_service_mode_failure_raises_clickexception_atomic() -> None:
    t3 = _fake_t3()
    client = MagicMock()
    client.rename_collection_cascade = MagicMock(
        side_effect=RuntimeError("simulated service outage")
    )

    with pytest.raises(click.ClickException) as ei:
        rename_collection_data_plane(
            "code__old", "code__new", t3_db=t3, catalog=client
        )

    assert "unchanged" in str(ei.value)
    assert "simulated service outage" in str(ei.value)
    # Atomic: no local T3 rename attempted.
    t3.rename_collection.assert_not_called()


def test_service_mode_still_guards_unknown_old() -> None:
    t3 = _fake_t3(old_exists=False)
    client = MagicMock()
    client.rename_collection_cascade = MagicMock()

    with pytest.raises(click.ClickException) as ei:
        rename_collection_data_plane(
            "code__old", "code__new", t3_db=t3, catalog=client
        )
    assert "not found" in str(ei.value).lower()
    client.rename_collection_cascade.assert_not_called()


def test_service_mode_still_guards_target_collision() -> None:
    t3 = _fake_t3(new_exists=True)
    client = MagicMock()
    client.rename_collection_cascade = MagicMock()

    with pytest.raises(click.ClickException) as ei:
        rename_collection_data_plane(
            "code__old", "code__new", t3_db=t3, catalog=client
        )
    assert "already exists" in str(ei.value).lower()
    client.rename_collection_cascade.assert_not_called()


# ── remap_collection_references (RDR-162 cross-model repoint, nexus-gaou3) ──────

def _patch_t2_cascade(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the T2 atomic cascade so remap_collection_references reaches the
    catalog leg without a real T2 database."""
    monkeypatch.setattr(
        "nexus.mcp_infra.t2_index_write",
        lambda fn: {},
    )


def test_remap_service_mode_passes_cross_model_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # nexus-gaou3: in service mode the catalog repoint MUST signal cross_model=True
    # so the Java endpoint takes the RDR-162 COPY branch instead of 409ing the
    # (legitimately pre-existing) target.
    _patch_t2_cascade(monkeypatch)
    catalog = MagicMock()
    catalog.rename_collection = MagicMock(return_value=7)

    counts = remap_collection_references("code__old", "code__new", catalog=catalog)

    catalog.rename_collection.assert_called_once_with(
        "code__old", "code__new", cross_model=True
    )
    assert counts["catalog_docs"] == 7


def test_remap_sqlite_mode_omits_cross_model_kwarg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # nexus-gaou3: the local catalog writer's rename_collection has no cross_model
    # parameter, so it must NOT be threaded in sqlite mode. (Overrides the autouse
    # service pin.)
    monkeypatch.setattr(
        "nexus.db.storage_mode.storage_backend_for",
        lambda store: StorageBackend.SQLITE,
    )
    _patch_t2_cascade(monkeypatch)
    catalog = MagicMock()
    catalog.rename_collection = MagicMock(return_value=4)

    counts = remap_collection_references("code__old", "code__new", catalog=catalog)

    catalog.rename_collection.assert_called_once_with("code__old", "code__new")
    assert counts["catalog_docs"] == 4
