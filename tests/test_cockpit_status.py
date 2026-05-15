# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nx cockpit status`` (RDR-111 consumer-side landing)."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from click.testing import CliRunner


def _seed_tuples_db(path: Path, rows: list[tuple[str, str, float]]) -> None:
    """Write a tuples.db with just enough schema for status-cmd to query."""
    from nexus.tuplespace.store import apply_tuples_schema

    conn = sqlite3.connect(str(path))
    apply_tuples_schema(conn)
    for subspace, content, created_at in rows:
        conn.execute(
            "INSERT INTO tuples (id, subspace, template_name, content, "
            "dimensions_json, embed_text, created_at) "
            "VALUES (?, ?, ?, ?, '{}', '', ?)",
            (
                f"id-{content}-{created_at}",
                subspace,
                subspace,
                content,
                created_at,
            ),
        )
    conn.commit()
    conn.close()


def test_cockpit_status_renders_per_subspace_summary(tmp_path: Path) -> None:
    from nexus.commands.cockpit import cockpit_group

    db = tmp_path / "tuples.db"
    now = time.time()
    _seed_tuples_db(
        db,
        [
            ("hook_events/tool_call_intent", "a", now - 30),
            ("hook_events/tool_call_intent", "b", now - 60),
            ("hook_events/assistant_turn_ended", "c", now - 7200),  # 2h ago
            ("tasks/myproj", "d", now - 10),
        ],
    )

    runner = CliRunner()
    result = runner.invoke(cockpit_group, ["status", "--db", str(db), "--window", "1h"])
    assert result.exit_code == 0, result.output
    assert "total tuples     4" in result.output
    assert "hook_events/tool_call_intent" in result.output
    assert "hook_events/assistant_turn_ended" in result.output
    assert "tasks/myproj" in result.output
    # Recent within 1h: 3 tuples (the 2h-ago one is outside the window).
    # The output is column-formatted so we can't string-match the exact row
    # easily; just assert the table contains expected subspaces.


def test_cockpit_status_missing_db_exits_nonzero(tmp_path: Path) -> None:
    from nexus.commands.cockpit import cockpit_group

    runner = CliRunner()
    result = runner.invoke(
        cockpit_group, ["status", "--db", str(tmp_path / "nope.db")]
    )
    assert result.exit_code != 0
    assert "tuples.db not found" in result.output
