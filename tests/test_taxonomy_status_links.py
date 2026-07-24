"""Service-mode `nx taxonomy status` must report a real topic-link count.

nexus-ntkr5: the service-mode branch hardcoded ``link_count = 0``
("link count not exposed via public API") — stale since
``get_topic_link_pairs`` became public; after the first
``nx taxonomy links --refresh`` run the live store held 7,520 links while
status still displayed "0 topic links". The service-mode branch is
exercised here by forcing ``_has_raw_access`` False against a real SQLite
T2 — both stores implement the same public taxonomy contract.
"""

from __future__ import annotations

import itertools
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.db.storage_mode import has_raw_access
from nexus.db.t2 import T2Database

# Import src_ids start >= 1e9: import_topic preserves ids WITHOUT advancing
# the engine's topics sequence (see tests/test_context.py). Module-distinct
# base (1.3e9) — the topics PK is global across tenants, so per-module
# counters restarting at the same value collide within one engine session.
from tests.conftest import next_import_seed_id  # session-unique import ids (see conftest note)


def _seed(db_path: Path) -> None:
    with T2Database(db_path) as db:
        ids = []
        for label in ("t1", "t2", "t3"):
            if has_raw_access(db.taxonomy):
                cur = db.taxonomy.conn.execute(
                    "INSERT INTO topics (label, collection, doc_count, created_at) "
                    "VALUES (?, 'docs__alpha', 1, '2026-01-01T00:00:00Z')",
                    (label,),
                )
                db.taxonomy.conn.commit()
                ids.append(cur.lastrowid)
            else:
                ids.append(db.taxonomy.import_topic(
                    src_id=next_import_seed_id(),
                    label=label,
                    parent_id=None,
                    collection="docs__alpha",
                    centroid_hash=None,
                    doc_count=1,
                    created_at="2026-01-01T00:00:00Z",
                    review_status="pending",
                    terms=None,
                ))
        db.taxonomy.upsert_topic_links(
            [
                {"from_topic_id": ids[0], "to_topic_id": ids[1], "link_count": 1, "link_types": ["cites"]},
                {"from_topic_id": ids[1], "to_topic_id": ids[2], "link_count": 2, "link_types": ["relates"]},
            ]
        )


def test_status_counts_links_via_public_api_in_service_mode(tmp_path: Path) -> None:
    from nexus.commands.taxonomy_cmd import taxonomy

    db_path = tmp_path / "memory.db"
    _seed(db_path)

    runner = CliRunner()
    with (
        patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
        patch("nexus.commands.taxonomy_cmd._has_raw_access", return_value=False),
    ):
        result = runner.invoke(taxonomy, ["status"])

    assert result.exit_code == 0, result.output
    assert "2 topic links" in result.output


@pytest.mark.skipif(
    os.environ.get("NX_TEST_T2_SUBSTRATE") == "engine",
    reason="dies-roster: the raw-access status link-count branch (SQLite .conn aggregate) dies at the RDR-155 P4b flip",
)
def test_status_raw_access_link_count_unchanged(tmp_path: Path) -> None:
    from nexus.commands.taxonomy_cmd import taxonomy

    db_path = tmp_path / "memory.db"
    _seed(db_path)

    runner = CliRunner()
    with patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path):
        result = runner.invoke(taxonomy, ["status"])

    assert result.exit_code == 0, result.output
    assert "2 topic links" in result.output
