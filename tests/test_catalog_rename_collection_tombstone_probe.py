# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-9n485 — ``nx catalog rename-collection``'s tombstone-vs-absent guards.

Mirrors ``tests/test_collection_rename_tombstone_probe.py`` but for the
``rename_collection_cmd`` CLI verb (``src/nexus/commands/catalog_cmds/
collections.py``), which repeats the same two ``t3_db.collection_exists``
guards against ``old``/``new`` before calling ``rename_collection_data_plane``.
A REAL ``HttpVectorClient`` (network boundary patched, matching
``tests/test_http_vector_client_stats.py``) proves the guard against the
actual three-state probe dispatch. ``tests/test_catalog_rename_collection.py``
(T3Database-backed) is untouched and must keep passing unmodified.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.http_vector_client import HttpVectorClient
from tests._catalog_fixture_ops import ActiveCatalog

STATS_PATH = "/v1/vectors/stats"
COLLECTIONS_PATH = "/v1/vectors/collections"


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def catalog():
    """Seed and read through whichever catalog is live (nexus-i711w).

    Was a directly-constructed local ``Catalog`` injected through the
    ``_get_catalog`` patch seam; that seam and the local class died with the
    local catalog. The verb now resolves the env-routed service-only
    factories against the engine's per-test tenant, and the tests seed
    through the same factories — the identical port shape recorded in
    tests/test_catalog_rename_collection.py's module docstring.
    """
    return ActiveCatalog()


def _patch_get(monkeypatch, *, stats_names: set[str], raw_names: set[str]) -> None:
    def fake_get(path: str, *, tenant: str = "default") -> Any:
        if path == STATS_PATH:
            return [
                {"name": n, "dim": 384, "count": 1, "last_write": "x"}
                for n in stats_names
            ]
        if path == COLLECTIONS_PATH:
            return [{"name": n} for n in raw_names]
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr("nexus.db.http_vector_client._get", fake_get)


def test_rename_rejects_tombstoned_old_with_actionable_message(
    monkeypatch, catalog, runner,
) -> None:
    catalog.register_collection("knowledge__delos")
    # knowledge__delos has physical chunk rows (raw) but zero live chunks
    # (stats) -- every document under it is trashed.
    _patch_get(monkeypatch, stats_names=set(), raw_names={"knowledge__delos"})
    t3_db = HttpVectorClient()

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main,
            ["catalog", "rename-collection",
             "knowledge__delos", "knowledge__1-1__voyage-context-3__v1"],
        )
    assert result.exit_code != 0
    message = result.output.lower()
    assert "not registered" not in message  # it IS registered -- don't misreport
    assert "tombstoned" in message or "trash" in message
    assert "restore" in message


def test_rename_rejects_tombstoned_new_as_not_free_to_claim(
    monkeypatch, catalog, runner,
) -> None:
    """The collision bug this bead exists to fix: a tombstoned target used to
    read as "doesn't exist in T3" (free), inviting a rename onto dead rows."""
    catalog.register_collection("knowledge__delos")
    target = "knowledge__1-1__voyage-context-3__v1"
    # old is live; new has physical rows but zero live chunks (tombstoned).
    _patch_get(
        monkeypatch,
        stats_names={"knowledge__delos"},
        raw_names={"knowledge__delos", target},
    )
    t3_db = HttpVectorClient()
    t3_db.rename_collection = MagicMock()  # local-mode leg; must not be reached

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main,
            ["catalog", "rename-collection", "knowledge__delos", target],
        )
    assert result.exit_code != 0
    assert "already exists" in result.output.lower()
    t3_db.rename_collection.assert_not_called()
