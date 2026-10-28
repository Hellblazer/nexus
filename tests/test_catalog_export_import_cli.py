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
import urllib.error

from click.testing import CliRunner

from nexus.catalog.recovery_bundle import ExportSummary, ImportSummary
from nexus.cli import main
from nexus.db.http_vector_client import VectorServiceError


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
    assert "note keys not recovered" not in result.output


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
                "from_source_uri": "nx-note://k/a",
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


def test_catalog_export_names_the_notes_whose_keys_could_not_be_recovered(tmp_path, monkeypatch):
    """nexus-6pfgb.14: a legacy note on a shared chunk exports with empty keys, and the command says so."""
    def _fake_export(reader, t3, path):
        path.write_text(json.dumps({"format": "nexus-recovery-bundle", "format_version": 1}) + "\n")
        return ExportSummary(docs_exported=3, note_keys_unrecovered=2,
                             note_keys_unrecovered_notes=[("knowledge__x", "legacy-a"),
                                                          ("knowledge__y", ""),
                                                          ("knowledge__z", "bad\x1b[2Jtitle")])

    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: object())
    monkeypatch.setattr("nexus.catalog.recovery_bundle.export_bundle", _fake_export)
    monkeypatch.setattr("nexus.db.make_t3", lambda: object())

    result = CliRunner().invoke(main, ["catalog", "export", str(tmp_path / "r.jsonl")])
    assert result.exit_code == 0, result.output
    assert "note keys not recovered: 2" in result.output
    assert '    knowledge__x: "legacy-a"\n' in result.output
    assert "    knowledge__y: (untitled)\n" in result.output
    assert "\x1b" not in result.output and '"bad\\u001b[2Jtitle"' in result.output, "a title is escaped"
    assert "re-put it with its tags, category and TTL" in result.output


def test_catalog_export_caps_the_unrecovered_titles_it_prints(tmp_path, monkeypatch):
    notes = [("knowledge__x", f"legacy-{i}") for i in range(23)]

    def _fake_export(reader, t3, path):
        path.write_text(json.dumps({"format": "nexus-recovery-bundle", "format_version": 1}) + "\n")
        return ExportSummary(note_keys_unrecovered=23, note_keys_unrecovered_notes=notes)

    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: object())
    monkeypatch.setattr("nexus.catalog.recovery_bundle.export_bundle", _fake_export)
    monkeypatch.setattr("nexus.db.make_t3", lambda: object())

    result = CliRunner().invoke(main, ["catalog", "export", str(tmp_path / "r.jsonl")])
    assert '"legacy-19"\n' in result.output and "legacy-20" not in result.output
    assert "... and 3 more" in result.output


def test_catalog_export_names_the_split_notes_left_out_for_a_missing_piece(tmp_path, monkeypatch):
    """nexus-6pfgb.36: a note with a piece gone from the store is left out, and the command names it."""
    notes = [("knowledge__y", "bad\x1b[2Jtitle")] + [("knowledge__x", f"split-{i}") for i in range(21)]

    def _fake_export(reader, t3, path):
        path.write_text(json.dumps({"format": "nexus-recovery-bundle", "format_version": 1}) + "\n")
        return ExportSummary(docs_exported=3, notes_incomplete=22, notes_incomplete_notes=notes)

    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: object())
    monkeypatch.setattr("nexus.catalog.recovery_bundle.export_bundle", _fake_export)
    monkeypatch.setattr("nexus.db.make_t3", lambda: object())

    result = CliRunner().invoke(main, ["catalog", "export", str(tmp_path / "r.jsonl")])
    assert result.exit_code == 1, "nexus-6pfgb.37: a bundle missing notes fails the export"
    assert "22 note(s) are not in the bundle" in result.output
    assert (tmp_path / "r.jsonl").exists(), "the bundle is still written for inspection"
    assert "WARNING: 22 note(s) written as several pieces have a piece missing" in result.output
    assert '    knowledge__x: "split-18"\n' in result.output and "split-19" not in result.output
    assert "... and 2 more" in result.output
    assert "\x1b" not in result.output and '"bad\\u001b[2Jtitle"' in result.output, "a title is escaped"


def test_catalog_export_stops_with_no_bundle_when_the_store_cannot_be_read(tmp_path, monkeypatch):
    """nexus-6pfgb.36: a store read failure is an error with a non-zero exit, never a short bundle."""
    def _fake_export(reader, t3, path):
        raise VectorServiceError("503 from store-get")

    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: object())
    monkeypatch.setattr("nexus.catalog.recovery_bundle.export_bundle", _fake_export)
    monkeypatch.setattr("nexus.db.make_t3", lambda: object())

    result = CliRunner().invoke(main, ["catalog", "export", str(tmp_path / "r.jsonl")])
    assert result.exit_code == 1, result.output
    assert "No bundle was written" in result.output


def test_catalog_export_reports_an_unreachable_local_store_without_a_traceback(tmp_path, monkeypatch):
    """A local endpoint raises its transport error unwrapped; the export reports it the same way."""
    def _fake_export(reader, t3, path):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: object())
    monkeypatch.setattr("nexus.catalog.recovery_bundle.export_bundle", _fake_export)
    monkeypatch.setattr("nexus.db.make_t3", lambda: object())

    result = CliRunner().invoke(main, ["catalog", "export", str(tmp_path / "r.jsonl")])
    assert result.exit_code == 1, result.output
    assert "connection refused" in result.output and "No bundle was written" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_catalog_export_exits_zero_when_only_ghosts_were_skipped(tmp_path, monkeypatch):
    """nexus-6pfgb.37: a ghost has nothing left to save, so it does not fail the export."""
    def _fake_export(reader, t3, path):
        path.write_text(json.dumps({"format": "nexus-recovery-bundle", "format_version": 1}) + "\n")
        return ExportSummary(docs_exported=3, ghosts_skipped=4)

    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: object())
    monkeypatch.setattr("nexus.catalog.recovery_bundle.export_bundle", _fake_export)
    monkeypatch.setattr("nexus.db.make_t3", lambda: object())

    result = CliRunner().invoke(main, ["catalog", "export", str(tmp_path / "r.jsonl")])
    assert result.exit_code == 0, result.output
    assert "ghosts skipped (rows with no live content): 4" in result.output
