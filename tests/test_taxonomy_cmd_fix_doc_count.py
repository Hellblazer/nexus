# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx taxonomy audit --fix-doc-count`` (bead nexus-c0g6e, GH #1529).

The repeatable, on-demand twin of the engine's hygiene-007-1 boot walk:
dry-run by default (preview only), ``--yes`` applies. Both branches call
the SAME engine route (``HttpTaxonomyStore.recount_doc_count``) the doctor
row's remedy names — these tests fake that store, never a real engine, so
they stay fast and exercise only the CLI's own dry-run/apply/display-scope
logic.
"""
from __future__ import annotations

from click.testing import CliRunner

from nexus.commands.taxonomy_cmd import taxonomy


def _fake_db(recount_result: dict):
    calls: list[dict] = []

    class _Store:
        def recount_doc_count(self, *, dry_run: bool) -> dict:
            calls.append({"dry_run": dry_run})
            return recount_result

    class _Db:
        taxonomy = _Store()
        closed = False

        def close(self) -> None:
            self.closed = True

    db = _Db()
    db.calls = calls  # type: ignore[attr-defined]
    return db


def _run(monkeypatch, db, *args: str):
    monkeypatch.setattr(
        "nexus.commands.taxonomy_cmd._T2Database", lambda path, *, client=None: db,
    )
    runner = CliRunner()
    return runner.invoke(taxonomy, ["audit", "--collection", "knowledge__a", "--fix-doc-count", *args])


def test_dry_run_by_default_previews_without_applying(monkeypatch) -> None:
    db = _fake_db({
        "dry_run": True,
        "corrected": 1,
        "topics": [
            {"topic_id": 500, "label": "over-counted", "collection": "knowledge__a",
             "doc_count": 5, "actual_count": 2},
        ],
    })
    result = _run(monkeypatch, db)
    assert result.exit_code == 0, result.output
    assert db.calls == [{"dry_run": True}]
    assert "Would correct 1 topic(s)" in result.output
    assert "5 -> 2" in result.output
    assert "Dry run" in result.output
    assert db.closed is True


def test_yes_applies_and_reports_corrected(monkeypatch) -> None:
    db = _fake_db({
        "dry_run": False,
        "corrected": 1,
        "topics": [
            {"topic_id": 500, "label": "over-counted", "collection": "knowledge__a",
             "doc_count": 5, "actual_count": 2},
        ],
    })
    result = _run(monkeypatch, db, "--yes")
    assert result.exit_code == 0, result.output
    assert db.calls == [{"dry_run": False}]
    assert "Corrected 1 topic(s)" in result.output
    assert "Dry run" not in result.output


def test_display_scoped_to_collection_even_though_apply_is_tenant_wide(monkeypatch) -> None:
    """The engine route recounts the WHOLE tenant; --collection only filters
    what this command DISPLAYS. A drifted topic in a different collection
    must not appear in the --collection knowledge__a report, and the
    "elsewhere in this tenant" note must say so when the display set is
    empty but the tenant-wide result was not."""
    db = _fake_db({
        "dry_run": True,
        "corrected": 1,
        "topics": [
            {"topic_id": 999, "label": "other-collection", "collection": "knowledge__b",
             "doc_count": 9, "actual_count": 1},
        ],
    })
    result = _run(monkeypatch, db)
    assert result.exit_code == 0, result.output
    assert "No doc_count drift for collection 'knowledge__a'" in result.output
    assert "1 elsewhere in this tenant" in result.output
    assert "other-collection" not in result.output


def test_no_drift_anywhere_reports_cleanly(monkeypatch) -> None:
    db = _fake_db({"dry_run": True, "corrected": 0, "topics": []})
    result = _run(monkeypatch, db)
    assert result.exit_code == 0, result.output
    assert "No doc_count drift for collection 'knowledge__a'." in result.output
    assert "elsewhere" not in result.output


def test_db_closed_even_on_success(monkeypatch) -> None:
    db = _fake_db({"dry_run": True, "corrected": 0, "topics": []})
    _run(monkeypatch, db)
    assert db.closed is True
