# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-l0ai3: the rename summary shows the aspects tasks the engine rewrote.

The engine's rename counts include ``aspects_tasks`` (the unconsumed task
bodies it points at the new collection, RDR-235) and, until that table is
dropped, ``aspect_extraction_queue``. The summary printed neither.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.collection_rename import (
    rename_collection_data_plane,
    rename_summary_parts,
)

pytestmark = pytest.mark.usefixtures("cloud_mode")

_COUNTS = {
    "tax_topics": 0, "tax_assignments": 0, "tax_meta": 0,
    "chash": 0, "catalog_docs": 0,
}


class TestAspectsTasksSummary:
    def test_data_plane_maps_aspects_tasks(self):
        client = MagicMock()
        client.rename_collection_cascade = MagicMock(return_value={
            "aspects_tasks": 4, "aspect_extraction_queue": 2,
        })
        t3 = MagicMock()
        t3.collection_exists = MagicMock(side_effect=lambda n: n == "docs__old")
        counts = rename_collection_data_plane(
            "docs__old", "docs__new", t3_db=t3, catalog=client,
        )
        assert counts["aspect_tasks"] == 4
        assert counts["aspect_queue"] == 2

    def test_data_plane_zero_when_the_engine_omits_the_key(self):
        client = MagicMock()
        client.rename_collection_cascade = MagicMock(return_value={})
        t3 = MagicMock()
        t3.collection_exists = MagicMock(side_effect=lambda n: n == "docs__old")
        counts = rename_collection_data_plane(
            "docs__old", "docs__new", t3_db=t3, catalog=client,
        )
        assert counts["aspect_tasks"] == 0

    def test_summary_parts_name_tasks_and_queue(self):
        parts = rename_summary_parts({**_COUNTS, "aspect_tasks": 4, "aspect_queue": 2})
        assert "4 aspect tasks" in parts
        assert "2 aspect queue rows" in parts

    def test_summary_parts_omit_zero_and_tolerate_missing_keys(self):
        assert rename_summary_parts({**_COUNTS}) == []
        assert rename_summary_parts({}) == []

    def test_nx_collection_rename_prints_the_tasks(self):
        from nexus.commands.collection import rename_cmd

        with patch(
            "nexus.commands.collection.rename_collection_data_plane",
            return_value={**_COUNTS, "aspect_tasks": 4},
        ):
            result = CliRunner().invoke(rename_cmd, ["docs__old", "docs__new"])
        assert result.exit_code == 0, result.output
        assert "4 aspect tasks" in result.output

    def test_nx_catalog_rename_collection_prints_the_tasks(self):
        from nexus.commands.catalog_cmds.collections import rename_collection_cmd

        cat = MagicMock()
        cat.get_collection.return_value = {
            "embedding_model": "", "lifecycle_state": "live", "superseded_by": "",
        }
        writer = MagicMock()
        writer.supersede_collection.return_value = 1
        t3 = MagicMock()
        with patch("nexus.commands.catalog._get_catalog", return_value=cat), patch(
            "nexus.commands.catalog._get_catalog_writer", return_value=writer,
        ), patch("nexus.db.make_t3", return_value=t3), patch(
            "nexus.db.collection_state.probe_collection_state",
        ) as probe, patch(
            "nexus.commands.collection.rename_collection_data_plane",
            return_value={**_COUNTS, "aspect_tasks": 4, "aspect_queue": 1},
        ), patch("nexus.corpus.ensure_collection_registered"):
            from nexus.db.collection_state import CollectionState

            probe.side_effect = lambda _t3, name: (
                CollectionState.PRESENT if name == "docs__old" else CollectionState.ABSENT
            )
            result = CliRunner().invoke(
                rename_collection_cmd,
                ["docs__old", "docs__new", "--allow-legacy", "--yes"],
            )
        assert result.exit_code == 0, result.output
        assert "4 aspect tasks" in result.output
        assert "1 aspect queue rows" in result.output
