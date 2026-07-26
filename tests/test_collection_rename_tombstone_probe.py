# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-9n485 — rename_collection_data_plane's tombstone-vs-absent guards.

RDR-156 P3 gate finding nexus-70r3c.13: on the service (HttpVectorClient)
path, ``collection_exists()`` reads the tombstone-filtered
``collection_vector_stats`` view, so a collection whose every chunk belongs
to a trashed document reads exactly like a collection that never existed.
``rename_collection_data_plane``'s two pre-flight guards used that boolean
directly:

  - ``old`` absent-vs-tombstoned: raised a misleading "collection not
    found" for a tombstoned source (the real remedy is "restore the
    trashed documents first").
  - ``new`` absent-vs-tombstoned: a tombstoned target read as
    "not exists" (free to claim) — this rename path has no
    ``cross_model`` escape valve, so claiming it would silently land
    fresh/live data on top of dead rows sharing the same collection name.

These tests exercise the fix with a REAL ``HttpVectorClient`` (only the
network boundary — module-level ``_get``/``_post`` — is patched, matching
the pattern in ``tests/test_http_vector_client_stats.py``) so the guard is
proven against the actual three-state dispatch, not a mock standing in for
it. The existing MagicMock-based coverage in ``tests/test_collection_rename.py``
and ``tests/test_collection_rename_service_mode.py`` is untouched and must
keep passing unmodified — the isinstance(t3_db, HttpVectorClient) gate in
``nexus.db.collection_state.probe_collection_state`` is what makes that
possible (a bare MagicMock is never an HttpVectorClient instance, so those
suites keep exercising the pre-existing two-state collection_exists path).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import click
import pytest

from nexus.collection_rename import rename_collection_data_plane
from nexus.db.http_vector_client import HttpVectorClient
from nexus.db.storage_mode import StorageBackend

STATS_PATH = "/v1/vectors/stats"
COLLECTIONS_PATH = "/v1/vectors/collections"


@pytest.fixture(autouse=True)
def _pin_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "nexus.db.storage_mode.storage_backend_for",
        lambda store: StorageBackend.SERVICE,
    )


def _patch_get(monkeypatch, handler) -> list[str]:
    paths: list[str] = []

    def fake_get(path: str, *, tenant: str = "default") -> Any:
        paths.append(path)
        return handler(path)

    monkeypatch.setattr("nexus.db.http_vector_client._get", fake_get)
    return paths


def _stats_collections_handler(*, stats_names: set[str], raw_names: set[str]):
    def handler(path: str) -> Any:
        if path == STATS_PATH:
            return [
                {"name": n, "dim": 384, "count": 1, "last_write": "x"}
                for n in stats_names
            ]
        if path == COLLECTIONS_PATH:
            return [{"name": n} for n in raw_names]
        raise AssertionError(f"unexpected path {path}")

    return handler


class TestOldTombstonedVsAbsent:
    def test_old_truly_absent_reports_not_found(self, monkeypatch) -> None:
        _patch_get(
            monkeypatch,
            _stats_collections_handler(stats_names=set(), raw_names=set()),
        )
        client = HttpVectorClient()

        with pytest.raises(click.ClickException) as ei:
            rename_collection_data_plane(
                "code__old", "code__new", t3_db=client, catalog=MagicMock(),
            )
        assert "not found" in str(ei.value).lower()
        assert "tombstoned" not in str(ei.value).lower()

    def test_old_tombstoned_reports_actionable_message_not_bare_not_found(
        self, monkeypatch,
    ) -> None:
        # code__old has physical chunk rows (raw listing) but zero live
        # chunks (stats listing empty) -- every document under it is trashed.
        _patch_get(
            monkeypatch,
            _stats_collections_handler(stats_names=set(), raw_names={"code__old"}),
        )
        client = HttpVectorClient()

        with pytest.raises(click.ClickException) as ei:
            rename_collection_data_plane(
                "code__old", "code__new", t3_db=client, catalog=MagicMock(),
            )
        message = str(ei.value).lower()
        assert "tombstoned" in message or "trash" in message
        assert "restore" in message

    def test_old_present_proceeds_to_service_cascade(self, monkeypatch) -> None:
        _patch_get(
            monkeypatch,
            _stats_collections_handler(stats_names={"code__old"}, raw_names={"code__old"}),
        )
        client = HttpVectorClient()
        catalog = MagicMock()
        catalog.rename_collection_cascade = MagicMock(return_value={})

        rename_collection_data_plane(
            "code__old", "code__new", t3_db=client, catalog=catalog,
        )

        catalog.rename_collection_cascade.assert_called_once_with("code__old", "code__new")


class TestNewTombstonedVsAbsent:
    def test_new_tombstoned_is_not_free_to_claim(self, monkeypatch) -> None:
        """The bug this bead exists to fix: a tombstoned target used to read
        as "doesn't exist" (free), inviting a silent collision. It must now
        be refused exactly like a live collision."""
        _patch_get(
            monkeypatch,
            _stats_collections_handler(
                stats_names={"code__old"}, raw_names={"code__old", "code__new"},
            ),
        )
        client = HttpVectorClient()
        catalog = MagicMock()
        catalog.rename_collection_cascade = MagicMock(return_value={})

        with pytest.raises(click.ClickException) as ei:
            rename_collection_data_plane(
                "code__old", "code__new", t3_db=client, catalog=catalog,
            )
        assert "already exists" in str(ei.value).lower()
        catalog.rename_collection_cascade.assert_not_called()

    def test_new_truly_absent_is_free_to_claim(self, monkeypatch) -> None:
        _patch_get(
            monkeypatch,
            _stats_collections_handler(stats_names={"code__old"}, raw_names={"code__old"}),
        )
        client = HttpVectorClient()
        catalog = MagicMock()
        catalog.rename_collection_cascade = MagicMock(return_value={})

        rename_collection_data_plane(
            "code__old", "code__new", t3_db=client, catalog=catalog,
        )

        catalog.rename_collection_cascade.assert_called_once_with("code__old", "code__new")

    def test_new_live_present_is_not_free_to_claim(self, monkeypatch) -> None:
        _patch_get(
            monkeypatch,
            _stats_collections_handler(
                stats_names={"code__old", "code__new"}, raw_names={"code__old", "code__new"},
            ),
        )
        client = HttpVectorClient()
        catalog = MagicMock()
        catalog.rename_collection_cascade = MagicMock(return_value={})

        with pytest.raises(click.ClickException) as ei:
            rename_collection_data_plane(
                "code__old", "code__new", t3_db=client, catalog=catalog,
            )
        assert "already exists" in str(ei.value).lower()
        catalog.rename_collection_cascade.assert_not_called()
