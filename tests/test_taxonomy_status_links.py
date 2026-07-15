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

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from nexus.db.t2 import T2Database


def _seed(db_path: Path) -> None:
    with T2Database(db_path) as db:
        ids = []
        for label in ("t1", "t2", "t3"):
            cur = db.taxonomy.conn.execute(
                "INSERT INTO topics (label, collection, doc_count, created_at) "
                "VALUES (?, 'docs__alpha', 1, '2026-01-01T00:00:00Z')",
                (label,),
            )
            ids.append(cur.lastrowid)
        db.taxonomy.conn.commit()
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


def test_status_raw_access_link_count_unchanged(tmp_path: Path) -> None:
    from nexus.commands.taxonomy_cmd import taxonomy

    db_path = tmp_path / "memory.db"
    _seed(db_path)

    runner = CliRunner()
    with patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path):
        result = runner.invoke(taxonomy, ["status"])

    assert result.exit_code == 0, result.output
    assert "2 topic links" in result.output
