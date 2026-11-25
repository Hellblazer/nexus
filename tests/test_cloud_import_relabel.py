# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx backup restore`` prints the collections the restore relabelled (RDR-229 P3.11, nexus-p2m1x.31): from the
engine's report, from the restore marker when the session is lost after the commit, and nothing when there are none.

Split from ``tests/test_cloud_import.py`` to keep that file under the test-file size cap; the fixtures and helpers
(``backup_file``, the autouse ``_home``, ``_point_at``, ``_flat``, ``_forget_the_session_after_commit``) are that
module's, imported here rather than copied.
"""

from __future__ import annotations

from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from nexus.backup.tenant_file import index_file
from nexus.cli import main
from tests._fake_import_engine import FakeImportEngine
from tests.test_cloud_import import (  # noqa: F401  (backup_file and _home are fixtures, _home autouse)
    _flat,
    _forget_the_session_after_commit,
    _home,
    _point_at,
    backup_file,
)


def test_the_verb_prints_each_collection_the_restore_relabelled(
    cloud_mode: None, monkeypatch: pytest.MonkeyPatch, backup_file: Path,
) -> None:
    report = {
        "tables": [{"table": "memory", "staged": 1, "inserted": 1, "seeded_skipped": 0, "dropped": 0}],
        "soft_references": [], "skipped_tables": {}, "skipped_columns": [],
        "collections_relabelled": [{"label": "c4812", "target_label": "c9001"}, {"label": "c7", "target_label": "c9002"}],
    }
    with FakeImportEngine(report=report) as engine:
        _point_at(monkeypatch, engine)
        result = CliRunner().invoke(main, ["backup", "restore", str(backup_file)])
    assert result.exit_code == 0, result.output
    out = _flat(result.output)
    assert "Collection relabelled: c4812 -> c9001" in out
    assert "Collection relabelled: c7 -> c9002" in out


def test_the_verb_prints_nothing_about_relabels_when_the_engine_reports_none(
    cloud_mode: None, monkeypatch: pytest.MonkeyPatch, backup_file: Path,
) -> None:
    with FakeImportEngine() as engine:
        _point_at(monkeypatch, engine)
        result = CliRunner().invoke(main, ["backup", "restore", str(backup_file)])
    assert result.exit_code == 0, result.output
    assert "relabelled" not in result.output


def test_the_relabels_are_printed_from_the_marker_when_the_session_is_lost_after_commit(
    cloud_mode: None, monkeypatch: pytest.MonkeyPatch, backup_file: Path,
) -> None:
    sha = index_file(backup_file).header_sha256
    with FakeImportEngine() as engine:
        engine.marker = {
            "header_sha256": sha, "session_id": "s", "committed_at": "T", "row_counts": {"memory": 18},
            "collections_relabelled_count": 3,
            "collections_relabelled": [{"label": "c4812", "target_label": "c9001"}, {"label": "c7", "target_label": "c9002"}],
        }
        _forget_the_session_after_commit(engine)
        _point_at(monkeypatch, engine)
        result = CliRunner().invoke(main, ["backup", "restore", str(backup_file)])
    assert result.exit_code == 0, result.output
    out = _flat(result.output)
    assert "its restore marker records that this file's load committed" in out
    assert "Collection relabelled: c4812 -> c9001" in out
    assert "Collection relabelled: c7 -> c9002" in out
    assert "... and 1 more collections relabelled" in out


def test_an_entry_with_no_target_label_is_not_printed() -> None:
    from nexus.commands.backup_cmd import _echo_relabelled

    @click.command()
    def show() -> None:
        _echo_relabelled([{"label": "c1"}, {"label": "c2", "target_label": "c3"}])

    out = CliRunner().invoke(show).output
    assert "c2 -> c3" in out and "None" not in out


def test_the_relabels_are_read_from_the_marker_when_the_engines_report_is_gone() -> None:
    from nexus.commands.backup_cmd import _echo_relabelled

    runner = CliRunner()

    @click.command()
    def show() -> None:
        _echo_relabelled([{"label": "c1", "target_label": "c50"}], 1003)

    result = runner.invoke(show)
    assert "Collection relabelled: c1 -> c50" in result.output
    assert "... and 1002 more collections relabelled" in result.output
