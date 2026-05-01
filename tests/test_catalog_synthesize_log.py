# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the RDR-101 Phase 2 ``nx catalog synthesize-log`` verb.

Coverage:
- Verb fails loudly when catalog is not initialized.
- ``--dry-run`` reports counts without writing.
- Default behavior refuses to overwrite a non-empty events.jsonl.
- ``--force`` truncates the existing log before writing.
- ``--json`` emits a structured report.
- The written events have UUID7 doc_ids; the original tumbler is
  preserved in payload.tumbler for back-compat.
- Re-synthesizing the same catalog (with --force) produces the same
  event counts but different doc_ids (UUID7 is fresh per run).
- The persisted log replays through the projector against a fresh
  CatalogDB and reproduces the live tumbler-keyed SQLite state (the
  projector reads payload.tumbler, not payload.doc_id, for the v: 0
  schema).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.catalog import events as ev
from nexus.catalog.catalog import Catalog
from nexus.catalog.catalog_db import CatalogDB
from nexus.catalog.event_log import EventLog
from nexus.catalog.projector import Projector
from nexus.commands.catalog import synthesize_log_cmd


@pytest.fixture()
def isolated_nexus(tmp_path: Path) -> Path:
    """Catalog dir set up by the autouse ``_isolate_catalog`` fixture in
    tests/conftest.py via NEXUS_CATALOG_PATH."""
    return tmp_path / "test-catalog"


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _build_initialized_catalog(catalog_dir: Path) -> Catalog:
    Catalog.init(catalog_dir)
    cat = Catalog(catalog_dir, catalog_dir / ".catalog.db")
    owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
    cat.register(owner, "doc-A.md", content_type="prose", file_path="doc-A.md")
    cat.register(owner, "doc-B.md", content_type="prose", file_path="doc-B.md")
    return cat


# ── Usage / fail-loud ────────────────────────────────────────────────────


class TestUsage:
    def test_missing_catalog_is_clean_error(self, isolated_nexus, runner):
        result = runner.invoke(synthesize_log_cmd, [])
        assert result.exit_code != 0
        assert "not initialized" in result.output.lower()


# ── Dry run ──────────────────────────────────────────────────────────────


class TestDryRun:
    def test_dry_run_reports_counts_no_write(self, isolated_nexus, runner):
        cat = _build_initialized_catalog(isolated_nexus)
        cat._db.close()

        result = runner.invoke(synthesize_log_cmd, ["--dry-run"])
        assert result.exit_code == 0, result.output
        assert "dry-run" in result.output.lower()
        # Owner + 2 documents → 3 events at minimum.
        assert "OwnerRegistered" in result.output
        assert "DocumentRegistered" in result.output

        # events.jsonl untouched.
        log = EventLog(isolated_nexus)
        assert log.path.read_text() == ""

    def test_dry_run_json_report(self, isolated_nexus, runner):
        cat = _build_initialized_catalog(isolated_nexus)
        cat._db.close()

        result = runner.invoke(synthesize_log_cmd, ["--dry-run", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["dry_run"] is True
        assert payload["wrote"] is False
        assert payload["events_total"] >= 3
        assert "OwnerRegistered" in payload["events_by_type"]
        assert "DocumentRegistered" in payload["events_by_type"]


# ── Default: refuse to overwrite, write to fresh log ─────────────────────


class TestWriteAndOverwrite:
    def test_writes_to_fresh_log(self, isolated_nexus, runner):
        cat = _build_initialized_catalog(isolated_nexus)
        cat._db.close()

        result = runner.invoke(synthesize_log_cmd, [])
        assert result.exit_code == 0, result.output
        assert "Wrote events.jsonl" in result.output

        log = EventLog(isolated_nexus)
        events = list(log.replay())
        assert len(events) >= 3
        types = {e.type for e in events}
        assert ev.TYPE_OWNER_REGISTERED in types
        assert ev.TYPE_DOCUMENT_REGISTERED in types

    def test_refuses_to_overwrite_non_empty_log(self, isolated_nexus, runner):
        cat = _build_initialized_catalog(isolated_nexus)
        cat._db.close()

        # First run populates events.jsonl.
        first = runner.invoke(synthesize_log_cmd, [])
        assert first.exit_code == 0
        # Second run without --force must refuse.
        second = runner.invoke(synthesize_log_cmd, [])
        assert second.exit_code != 0
        assert "non-empty" in second.output.lower()

    def test_force_truncates_and_rewrites(self, isolated_nexus, runner):
        cat = _build_initialized_catalog(isolated_nexus)
        cat._db.close()

        runner.invoke(synthesize_log_cmd, [])
        log = EventLog(isolated_nexus)
        first_events = list(log.replay())
        first_doc_ids = {
            e.payload.doc_id for e in first_events
            if e.type == ev.TYPE_DOCUMENT_REGISTERED
        }

        result = runner.invoke(synthesize_log_cmd, ["--force"])
        assert result.exit_code == 0
        second_events = list(log.replay())
        # Same event count.
        assert len(second_events) == len(first_events)
        second_doc_ids = {
            e.payload.doc_id for e in second_events
            if e.type == ev.TYPE_DOCUMENT_REGISTERED
        }
        # Fresh UUID7s on every synthesize-log run — the canonical
        # doc_ids are different even though the tumblers are the same.
        assert first_doc_ids.isdisjoint(second_doc_ids)


# ── UUID7 minting + tumbler preservation ─────────────────────────────────


class TestUUID7Minting:
    def test_doc_ids_are_uuid7(self, isolated_nexus, runner):
        cat = _build_initialized_catalog(isolated_nexus)
        cat._db.close()

        runner.invoke(synthesize_log_cmd, [])
        log = EventLog(isolated_nexus)
        for e in log.replay():
            if e.type != ev.TYPE_DOCUMENT_REGISTERED:
                continue
            # doc_id must parse as a UUID7.
            u = uuid.UUID(e.payload.doc_id)
            assert u.version == 7, (
                f"DocumentRegistered.doc_id should be UUID7, got "
                f"version {u.version}: {e.payload.doc_id}"
            )

    def test_tumbler_preserved_alongside_uuid7(self, isolated_nexus, runner):
        cat = _build_initialized_catalog(isolated_nexus)
        cat._db.close()

        runner.invoke(synthesize_log_cmd, [])
        log = EventLog(isolated_nexus)
        doc_events = [
            e for e in log.replay() if e.type == ev.TYPE_DOCUMENT_REGISTERED
        ]
        assert len(doc_events) == 2
        for e in doc_events:
            assert e.payload.tumbler != "", (
                "Phase 2 synthesis must preserve the original tumbler "
                "in payload.tumbler so the v: 0 projector keeps writing "
                "to the existing tumbler-keyed SQLite schema."
            )
            # The two should differ in shape: doc_id is UUID7, tumbler is "1.X.Y".
            assert e.payload.doc_id != e.payload.tumbler


# ── Round-trip: synthesized log replays to live SQLite ───────────────────


class TestReplayEquality:
    """The synthesized log + projector must reproduce the live SQLite,
    even though the events carry UUID7 doc_ids the live schema doesn't
    know about. The v: 0 projector reads ``payload.tumbler`` for the
    SQLite write."""

    def test_synthesized_log_replays_to_match(self, isolated_nexus, runner, tmp_path):
        cat = _build_initialized_catalog(isolated_nexus)
        cat._db.close()

        result = runner.invoke(synthesize_log_cmd, [])
        assert result.exit_code == 0

        # Replay events.jsonl into a fresh CatalogDB.
        log = EventLog(isolated_nexus)
        projected_path = tmp_path / "projected.db"
        proj_db = CatalogDB(projected_path)
        try:
            Projector(proj_db).apply_all(log.replay())
        finally:
            proj_db.close()

        live_conn = sqlite3.connect(str(isolated_nexus / ".catalog.db"))
        proj_conn = sqlite3.connect(str(projected_path))
        try:
            for table in ("owners", "documents"):
                cur = live_conn.execute(f"PRAGMA table_info({table})")
                cols = ", ".join(r[1] for r in cur.fetchall())
                live_rows = sorted(live_conn.execute(
                    f"SELECT {cols} FROM {table} ORDER BY {cols}"
                ).fetchall())
                proj_rows = sorted(proj_conn.execute(
                    f"SELECT {cols} FROM {table} ORDER BY {cols}"
                ).fetchall())
                assert live_rows == proj_rows, (
                    f"{table}: synthesized log replay diverged from live SQLite\n"
                    f"  live:      {live_rows}\n"
                    f"  projected: {proj_rows}"
                )
        finally:
            live_conn.close()
            proj_conn.close()


# ── Synthesizer-level tumbler stability across re-emit ───────────────────


class TestTumblerStableAcrossEmits:
    """A single synthesizer invocation must give every appearance of the
    same tumbler the same UUID7 doc_id (the mapping pass guarantees this).
    Otherwise an alias graph would have alias_doc_id != the
    DocumentRegistered's doc_id for the alias row."""

    def test_alias_uses_same_doc_id_as_registered(self, isolated_nexus, runner):
        Catalog.init(isolated_nexus)
        cat = Catalog(isolated_nexus, isolated_nexus / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="ababab")
        canonical = cat.register(owner, "canon.md", content_type="prose")
        alias = cat.register(owner, "alias.md", content_type="prose")
        cat.set_alias(alias, canonical)
        cat._db.close()

        result = runner.invoke(synthesize_log_cmd, [])
        assert result.exit_code == 0

        log = EventLog(isolated_nexus)
        events = list(log.replay())

        # Find the DocumentRegistered for the alias and the matching
        # DocumentAliased event; their doc_id pair must be consistent.
        registered_by_tumbler = {
            e.payload.tumbler: e.payload.doc_id
            for e in events if e.type == ev.TYPE_DOCUMENT_REGISTERED
        }
        aliased_events = [
            e for e in events if e.type == ev.TYPE_DOCUMENT_ALIASED
        ]
        assert len(aliased_events) == 1
        a = aliased_events[0]
        # alias_doc_id and canonical_doc_id must be the UUID7s the
        # corresponding DocumentRegistered events carried.
        assert a.payload.alias_doc_id == registered_by_tumbler[str(alias)]
        assert a.payload.canonical_doc_id == registered_by_tumbler[str(canonical)]
