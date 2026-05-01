# SPDX-License-Identifier: AGPL-3.0-or-later

"""Regression tests for the RDR-101 Phase 1+2 hardening PR.

One test per finding so a future regression points at the exact
contract that was relaxed:

- H1: ``migrate_chash_index_rename_doc_id`` warns when the table has
  neither ``doc_id`` nor ``chunk_chroma_id`` instead of silently
  skipping.
- H2: migrations count assertion is ``>=`` not ``==``.
- H5: ``doctor --t3-doc-id-coverage`` does NOT fail by default when
  events.jsonl claims chunks T3 doesn't have; ``--strict-not-in-t3``
  opts back into the strict contract.
- H6: ``repair-orphan-chunks --assign`` deduplicates by chunk_id and
  warns on an unknown doc_id.
- Regression guard: ``Catalog.resolve_chash`` still surfaces the
  ``doc_id`` key in the returned ChunkRef (Phase 4 back-compat
  boundary; see Phase 0 nexus-o6aa.3 deliverable).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.catalog import events as ev
from nexus.catalog.catalog import Catalog
from nexus.catalog.event_log import EventLog
from nexus.commands.catalog import doctor_cmd, repair_orphan_chunks_cmd


@pytest.fixture()
def isolated_nexus(tmp_path: Path) -> Path:
    return tmp_path / "test-catalog"


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


# ── H1: chash_index unrecognized-schema warning ──────────────────────────


class TestChashIndexUnrecognizedSchemaWarn:
    def test_warns_when_table_has_neither_column(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        # caplog doesn't capture structlog reliably; intercept the
        # warning call directly.
        from nexus.db import migrations as _migrations

        captured: list[tuple[str, dict]] = []

        class _CapturingLogger:
            def warning(self, event: str, **kw):
                captured.append((event, kw))

            def info(self, *a, **kw):
                pass

            def debug(self, *a, **kw):
                pass

        monkeypatch.setattr(_migrations, "_log", _CapturingLogger())

        conn = sqlite3.connect(":memory:")
        # Create a chash_index table with neither doc_id nor chunk_chroma_id.
        conn.executescript("""
            CREATE TABLE chash_index (
                chash TEXT NOT NULL,
                physical_collection TEXT NOT NULL,
                some_other_column TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (chash, physical_collection)
            );
        """)
        conn.commit()

        _migrations.migrate_chash_index_rename_doc_id(conn)

        assert any(
            event == "chash_index_unrecognized_schema"
            for event, _ in captured
        ), (
            f"migrate_chash_index_rename_doc_id should log a warning when "
            f"the table has neither doc_id nor chunk_chroma_id; pre-fix "
            f"it returned silently. Captured: {captured}"
        )


# ── H2: migration count is >= ────────────────────────────────────────────


class TestMigrationCountNonShrinking:
    def test_count_assertion_does_not_fail_on_added_migration(self):
        # Smoke check that the assertion is >= not ==. Direct read of
        # the test code: this is a guard that future PRs adding
        # migrations don't get gated on bumping a sentinel.
        from pathlib import Path

        test_file = (
            Path(__file__).parent / "test_migrations.py"
        )
        text = test_file.read_text()
        assert "assert len(MIGRATIONS) >= 30" in text, (
            "test_migrations.py should use >= for the migrations count "
            "assertion (was == 30 pre-hardening, friction-magnet)"
        )


# ── H5: --strict-not-in-t3 default off ───────────────────────────────────


class TestT3DocIdCoverageNotInT3IsWarning:
    def _seed_log_and_chroma(
        self, isolated_nexus, chroma_client, *, t3_chunk_id: str,
        log_chunk_id: str,
    ):
        """Seed events.jsonl with a ChunkIndexed for log_chunk_id and
        T3 with t3_chunk_id. If they differ, the log claims a chunk T3
        doesn't have."""
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

        Catalog.init(isolated_nexus)
        log = EventLog(isolated_nexus)
        log.append_many([
            ev.Event(
                type=ev.TYPE_CHUNK_INDEXED, v=0,
                payload=ev.ChunkIndexedPayload(
                    chunk_id=log_chunk_id, chash="h",
                    doc_id="uuid7-A", coll_id="code__test",
                    position=0,
                ),
                ts="t",
            ),
        ])
        col = chroma_client.get_or_create_collection(
            "code__test", embedding_function=DefaultEmbeddingFunction(),
        )
        col.add(
            ids=[t3_chunk_id], documents=["x"],
            metadatas=[{"doc_id": "uuid7-A"}],
        )

    def test_default_does_not_fail_on_not_in_t3(
        self, isolated_nexus, runner, monkeypatch: pytest.MonkeyPatch,
    ):
        import chromadb

        client = chromadb.EphemeralClient()
        for c in list(client.list_collections()):
            try:
                client.delete_collection(c.name)
            except Exception:
                pass

        self._seed_log_and_chroma(
            isolated_nexus, client,
            t3_chunk_id="ch1", log_chunk_id="ch1-deleted-from-t3",
        )

        class _FakeT3:
            _client = client

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())

        # Default: not_in_t3 is a warning. With one ChunkIndexed event
        # whose chunk_id is missing from T3 and one T3 chunk that's NOT
        # in the log, the doctor should still PASS (because the chunk
        # T3 has carries the right doc_id, and the missing-from-t3
        # chunk is treated as a warning rather than a hard fail).
        result = runner.invoke(
            doctor_cmd, ["--t3-doc-id-coverage", "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)["t3_doc_id_coverage"]
        coll = payload["tables"]["code__test"]
        assert coll["not_in_t3_count"] == 1
        # PASS despite not_in_t3 — that's the new default.
        assert coll["pass"] is True
        assert payload["pass"] is True

    def test_strict_flag_makes_not_in_t3_a_failure(
        self, isolated_nexus, runner, monkeypatch: pytest.MonkeyPatch,
    ):
        import chromadb

        client = chromadb.EphemeralClient()
        for c in list(client.list_collections()):
            try:
                client.delete_collection(c.name)
            except Exception:
                pass

        self._seed_log_and_chroma(
            isolated_nexus, client,
            t3_chunk_id="ch1", log_chunk_id="ch1-deleted-from-t3",
        )

        class _FakeT3:
            _client = client

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())

        result = runner.invoke(
            doctor_cmd,
            ["--t3-doc-id-coverage", "--strict-not-in-t3", "--json"],
        )
        assert result.exit_code == 1
        payload = json.loads(result.output)["t3_doc_id_coverage"]
        assert payload["pass"] is False
        coll = payload["tables"]["code__test"]
        assert coll["not_in_t3_count"] == 1
        assert coll["pass"] is False


# ── H6: --assign deduplicates and warns on unknown doc_id ────────────────


class TestRepairAssignDedupAndUnknownDocId:
    def _seed(self, catalog_dir: Path, events: list[ev.Event]) -> None:
        catalog_dir.mkdir(parents=True, exist_ok=True)
        Catalog.init(catalog_dir)
        log = EventLog(catalog_dir)
        log.append_many(events)

    def test_duplicate_assign_chunk_id_keeps_last(self, isolated_nexus, runner):
        # Two --assign pairs with the same chunk_id; later wins.
        self._seed(isolated_nexus, [
            ev.Event(
                type=ev.TYPE_CHUNK_INDEXED, v=0,
                payload=ev.ChunkIndexedPayload(
                    chunk_id="orph1", chash="h", doc_id="",
                    coll_id="code__test", position=0,
                    synthesized_orphan=True,
                ),
                ts="t",
            ),
        ])
        result = runner.invoke(
            repair_orphan_chunks_cmd,
            [
                "--assign", "orph1:doc-first",
                "--assign", "orph1:doc-second",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["repairs_count"] == 1
        assert payload["skipped_count"] == 0

        # The corrective event in the log must have the LAST assigned
        # doc_id, not the first. Pre-fix the loop processed both
        # pairs; the first wrote `repairs[0]` and the second silently
        # disappeared because `orphan_by_chunk_id` had already been
        # consumed.
        log = EventLog(isolated_nexus)
        last_event = list(log.replay())[-1]
        assert last_event.payload.doc_id == "doc-second"

    def test_unknown_doc_id_logs_warning(
        self, isolated_nexus, runner, monkeypatch: pytest.MonkeyPatch,
    ):
        # Seed: one orphan + one DocumentRegistered with a known doc_id.
        # --assign for an unknown doc_id should still apply (operator
        # may know what they're doing) but log a warning.
        self._seed(isolated_nexus, [
            ev.Event(
                type=ev.TYPE_DOCUMENT_REGISTERED, v=0,
                payload=ev.DocumentRegisteredPayload(
                    doc_id="known-doc",
                    owner_id="1.1",
                    content_type="prose",
                    source_uri="file:///x.md",
                    coll_id="code__test",
                    tumbler="1.1.1",
                ),
                ts="t",
            ),
            ev.Event(
                type=ev.TYPE_CHUNK_INDEXED, v=0,
                payload=ev.ChunkIndexedPayload(
                    chunk_id="orph1", chash="h", doc_id="",
                    coll_id="code__test", position=0,
                    synthesized_orphan=True,
                ),
                ts="t",
            ),
        ])

        # Capture structlog warning calls via monkeypatch.
        from nexus.commands import catalog as _catcmd

        captured: list[tuple[str, dict]] = []

        class _Capture:
            def warning(self, event, **kw):
                captured.append((event, kw))

            def info(self, *a, **kw):
                pass

            def debug(self, *a, **kw):
                pass

            def error(self, *a, **kw):
                pass

        monkeypatch.setattr(_catcmd, "_log", _Capture())

        result = runner.invoke(
            repair_orphan_chunks_cmd,
            ["--assign", "orph1:typo-id-not-in-log"],
        )
        assert result.exit_code == 0, result.output

        warned = any(
            event == "repair_orphan_unknown_doc_id"
            for event, _ in captured
        )
        assert warned, (
            f"Expected repair_orphan_unknown_doc_id warning. Captured: {captured}"
        )


# ── Regression guard: ChunkRef back-compat 'doc_id' key ──────────────────


class TestResolveChashChunkRefHasDocIdKey:
    """RDR-101 Phase 0 nexus-o6aa.3 deliberately kept ``doc_id`` in
    Catalog.resolve_chash's returned dict for back-compat with Phase 4
    callers. This test guards against a "cleanup" PR that would
    inadvertently remove the back-compat shim before Phase 4 ships."""

    def test_chunk_ref_dict_has_doc_id_key(self):
        # Inspect the source for the literal ``"doc_id"`` key in
        # _build_ref. Direct test would need a full T3 + chash_index
        # setup; the source-level guard is sufficient as a regression
        # signal for "this back-compat boundary still exists."
        from nexus.catalog.catalog import Catalog as _Cat

        import inspect
        src = inspect.getsource(_Cat.resolve_chash)
        assert '"doc_id"' in src, (
            "Catalog.resolve_chash's returned ChunkRef dict must keep "
            "the 'doc_id' key for back-compat with Phase 4 callers per "
            "RDR-101 Phase 0 nexus-o6aa.3 deliverable. Phase 3 will "
            "introduce a parallel chunk_chroma_id key alongside, not "
            "replace doc_id."
        )
