# SPDX-License-Identifier: AGPL-3.0-or-later
"""CLI wiring for ``nx catalog export`` / ``nx catalog import``
(nexus-xn3fr, GH #1419.9).

Mirrors ``tests/test_catalog_purge_trash.py``'s fake-based style: the
core module (``nexus.catalog.recovery_bundle``) has its own fake-server
suite in ``tests/catalog/test_recovery_bundle.py``; these tests exercise
only the CLI seam — argument handling, the summary rendering, and the
contract that a partial import REPORTS each note that did not verify and
runs to completion (fail-loud means visible, not aborted), while the exit
status is non-zero (RDR-223 P2.8, nexus-z0o2p.18).
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


def test_catalog_import_prints_summary_and_exits_nonzero_when_a_note_failed(
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
    # Partial failure is REPORTED per note, never an abort of the rest (the import ran to
    # completion: the counts are all there), but the exit status says a note did not verify
    # (RDR-223 P2.8, nexus-z0o2p.18; it was 0 before).
    assert result.exit_code == 1, result.output
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


def _invoke_import(tmp_path, monkeypatch, summary):
    bundle = tmp_path / "recovery.jsonl"
    bundle.write_text(
        json.dumps({"format": "nexus-recovery-bundle", "format_version": 1}) + "\n"
    )
    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: object())
    monkeypatch.setattr("nexus.commands.catalog._get_catalog_writer", lambda: object())
    monkeypatch.setattr(
        "nexus.catalog.recovery_bundle.import_bundle", lambda r, w, t, p: summary
    )
    monkeypatch.setattr("nexus.db.make_t3", lambda: object())
    return CliRunner().invoke(main, ["catalog", "import", str(bundle)])


def test_catalog_import_exits_zero_when_every_note_verified(tmp_path, monkeypatch):
    result = _invoke_import(tmp_path, monkeypatch, ImportSummary(docs_imported=3))
    assert result.exit_code == 0, result.output
    assert "docs imported: 3" in result.output


def test_catalog_import_exits_nonzero_for_an_uncertain_note_and_names_it(tmp_path, monkeypatch):
    summary = ImportSummary(
        docs_imported=2, docs_uncertain=1,
        doc_unverified=[{"title": "maybe-note", "error": "may have landed", "stamp_refused": False}],
    )
    result = _invoke_import(tmp_path, monkeypatch, summary)
    assert result.exit_code == 1, result.output
    assert "DOCS NOT CONFIRMED (may have landed): 1" in result.output
    assert "'maybe-note': may have landed" in result.output


def test_catalog_import_exits_nonzero_for_a_note_the_engine_would_not_stamp(tmp_path, monkeypatch):
    summary = ImportSummary(
        docs_imported=2, docs_stamp_refused=1,
        doc_unverified=[{"title": "unstamped", "error": "stamp refused", "stamp_refused": True}],
    )
    result = _invoke_import(tmp_path, monkeypatch, summary)
    assert result.exit_code == 1, result.output
    assert "NOT STAMPED COMPLETE" in result.output
    assert "'unstamped': stamp refused" in result.output


def test_unresolvable_links_alone_do_not_change_the_exit_status(tmp_path, monkeypatch):
    summary = ImportSummary(
        docs_imported=1,
        unresolvable_links=[
            {"from_source_uri": "a", "to_source_uri": "b", "link_type": "cites", "missing": "to"}
        ],
    )
    result = _invoke_import(tmp_path, monkeypatch, summary)
    assert result.exit_code == 0, result.output
