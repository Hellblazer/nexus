# SPDX-License-Identifier: AGPL-3.0-or-later
"""CLI wiring for ``nx catalog export`` / ``nx catalog import``
(nexus-xn3fr, GH #1419.9).

Mirrors ``tests/test_catalog_purge_trash.py``'s fake-based style: the
core module (``nexus.catalog.recovery_bundle``) has its own fake-server
suite in ``tests/catalog/test_recovery_bundle.py``; these tests exercise
only the CLI seam — argument handling, the summary rendering, and the
locked contract that a partial import REPORTS failures and exits 0
(fail-loud means visible, not aborted).
"""
from __future__ import annotations

import json

from click.testing import CliRunner

from nexus.catalog.recovery_bundle import ExportSummary, ImportSummary
from nexus.cli import main


def test_catalog_export_writes_jsonl_bundle_file(tmp_path, monkeypatch):
    out = tmp_path / "recovery.jsonl"

    def _fake_export(reader, t3, path):
        path.write_text(
            json.dumps({"format": "nexus-recovery-bundle", "format_version": 1}) + "\n"
        )
        return ExportSummary(docs_exported=3, links_exported=7, ghosts_skipped=1)

    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: object())
    monkeypatch.setattr("nexus.catalog.recovery_bundle.export_bundle", _fake_export)
    monkeypatch.setattr("nexus.db.make_t3", lambda: object())

    result = CliRunner().invoke(main, ["catalog", "export", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert "knowledge docs: 3" in result.output
    assert "links: 7" in result.output
    assert "ghosts skipped" in result.output


def test_catalog_import_prints_summary_and_exits_zero_on_partial_success(
    tmp_path, monkeypatch
):
    bundle = tmp_path / "recovery.jsonl"
    bundle.write_text(
        json.dumps({"format": "nexus-recovery-bundle", "format_version": 1}) + "\n"
    )

    summary = ImportSummary(
        docs_imported=2,
        docs_failed=1,
        links_created=4,
        links_merged=1,
        unresolvable_links=[
            {
                "from_source_uri": "chroma://k/a",
                "to_source_uri": "file:///gone.py",
                "link_type": "cites",
                "missing": "to",
            }
        ],
        doc_failures=[{"title": "broken-note", "error": "simulated"}],
    )

    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: object())
    monkeypatch.setattr("nexus.commands.catalog._get_catalog_writer", lambda: object())
    monkeypatch.setattr(
        "nexus.catalog.recovery_bundle.import_bundle", lambda r, w, t, p: summary
    )
    monkeypatch.setattr("nexus.db.make_t3", lambda: object())

    result = CliRunner().invoke(main, ["catalog", "import", str(bundle)])
    # Locked contract: partial failure is REPORTED, not an abort — exit 0.
    assert result.exit_code == 0, result.output
    assert "docs imported: 2" in result.output
    assert "DOC FAILURES: 1" in result.output
    assert "broken-note" in result.output
    assert "UNRESOLVABLE LINKS: 1" in result.output
    assert "file:///gone.py" in result.output
    assert "missing: to" in result.output


def test_catalog_import_requires_existing_file(tmp_path):
    result = CliRunner().invoke(
        main, ["catalog", "import", str(tmp_path / "nope.jsonl")]
    )
    assert result.exit_code != 0
