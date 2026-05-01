# SPDX-License-Identifier: AGPL-3.0-or-later

"""Regression tests for the RDR-101 meta-review remediation.

After the first round of correctness + hardening PRs (#427, #428), six
parallel reviewers found 4 genuine correctness bugs and several
quality issues in the FIX surface itself. One test per finding so
future regressions point at the exact contract that was relaxed.

- M1: ``Catalog._emit_shadow_event`` swallows TypeError / OSError so a
  bad payload does not abort the catalog mutation that triggered it.
- M2: ``synthesize-log --force`` ``preserve_doc_ids`` map uses
  last-occurrence-wins (not first-wins) so a tumbler that was
  resurrected (tombstone → re-register) preserves the LIVE doc_id.
- M3: ``Projector._v0_document_renamed`` uses ``payload.tumbler or
  payload.doc_id`` for the WHERE clause (was: ``payload.doc_id``).
  Mirrors the M-projector fix in PR #427 to ``_v0_document_deleted``.
- M4: ``Catalog.rename_collection`` shadow events preserve
  ``alias_of`` from the renamed row (was: hardcoded ``""``).
- M5: ``Projector._v0_document_aliased`` rejects self-alias.
- M8: ``doctor --strict-not-in-t3`` without ``--t3-doc-id-coverage``
  is a UsageError, not a silent no-op.
- M8b: JSON payload includes ``strict_not_in_t3: bool`` for auditability.
- M9: ``repair-orphan-chunks --assign`` preserves the orphan's
  ``position`` / ``content_hash`` / ``embedded_at`` on the corrective
  event (was: hardcoded ``position=0``).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.catalog import events as ev
from nexus.catalog.catalog import Catalog
from nexus.catalog.catalog_db import CatalogDB
from nexus.catalog.event_log import EventLog
from nexus.catalog.projector import Projector
from nexus.commands.catalog import (
    doctor_cmd,
    repair_orphan_chunks_cmd,
    synthesize_log_cmd,
)


# ── M1: shadow emit failure does not abort catalog mutation ───────────────


class TestShadowEmitFailureNonFatal:
    """A bad payload (datetime in meta) used to silently coerce via
    default=str (silent corruption); after PR #427 it raised TypeError
    which propagated out of the catalog mutator and left SQLite +
    legacy JSONL committed but events.jsonl write failed. This fix
    catches broadly so the catalog mutation still completes.
    """

    def test_typeerror_in_emit_does_not_abort_mutation(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        # PR ζ (nexus-o6aa.9.5) flipped NEXUS_EVENT_SOURCED default to
        # ON; shadow emit is a no-op under ES, so pin to legacy to
        # exercise the shadow-failure-non-fatal invariant.
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
        monkeypatch.setenv("NEXUS_EVENT_LOG_SHADOW", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")

        # Force _emit_shadow_event to raise via a poisoned to_dict.
        # We do this by replacing make_event's output with an event
        # whose to_dict raises TypeError unconditionally.
        captured: list = []
        original_emit = cat._emit_shadow_event

        def _poisoned_emit(event):
            class _Bad:
                type = event.type
                def to_dict(self_inner):
                    raise TypeError("simulated non-JSON-native payload")
            captured.append(event.type)
            return original_emit(_Bad())

        # First test: register_owner with poisoned emit. Mutation
        # should still succeed; events.jsonl should be empty (the
        # write failed before the file was opened).
        monkeypatch.setattr(cat, "_emit_shadow_event", _poisoned_emit)
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        assert owner is not None  # mutation succeeded

        # SQLite committed.
        rows = cat._db.execute(
            "SELECT name FROM owners WHERE tumbler_prefix = ?", (str(owner),),
        ).fetchall()
        assert rows == [("nexus",)]

        # The poisoned emit was called; its inner TypeError did NOT
        # propagate out of register_owner.
        assert "OwnerRegistered" in captured


# ── M2: --force preserve_doc_ids last-occurrence wins ─────────────────────


class TestForcePreserveLastOccurrence:
    """A tumbler tombstoned then re-registered should preserve the
    LIVE (last) doc_id on --force, not the dead (first) one.
    """

    def test_resurrection_preserves_last_doc_id(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        # Hand-craft a prior events.jsonl with a tumbler that was
        # registered with doc-A, deleted, then re-registered with doc-B.
        # The live document is doc-B; that's what --force must preserve.
        cat_dir = tmp_path / "test-catalog"
        Catalog.init(cat_dir)
        log = EventLog(cat_dir)
        log.append_many([
            ev.make_event(
                ev.DocumentRegisteredPayload(
                    doc_id="doc-A-dead",
                    owner_id="1.1",
                    content_type="prose",
                    source_uri="file:///x.md",
                    coll_id="docs__test",
                    title="x.md",
                    tumbler="1.1.1",
                ),
                v=0,
            ),
            ev.make_event(
                ev.DocumentDeletedPayload(
                    doc_id="doc-A-dead",
                    reason="resurrection-test",
                    tumbler="1.1.1",
                ),
                v=0,
            ),
            ev.make_event(
                ev.DocumentRegisteredPayload(
                    doc_id="doc-B-live",
                    owner_id="1.1",
                    content_type="prose",
                    source_uri="file:///x.md",
                    coll_id="docs__test",
                    title="x.md",
                    tumbler="1.1.1",
                ),
                v=0,
            ),
        ])

        # Add a documents.jsonl row so the synthesizer's
        # _build_tumbler_to_doc_id has a tumbler to mint for. Its
        # mint will be overridden by preserve_map (which we want to
        # carry doc-B-live, not doc-A-dead).
        cat = Catalog(cat_dir, cat_dir / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        # The register call uses owner=1.1 internally; verify by reading
        # the tumbler. We need a tumbler matching the prior log's "1.1.1".
        # To be robust, just register one document and find its tumbler.
        new_doc = cat.register(
            owner, "x.md", content_type="prose", file_path="x.md",
        )
        cat._db.close()
        # preserve_map's behaviour is what matters; the tumbler in the
        # prior log might not match the test's freshly-registered doc.
        # Inject a synthetic prior log row matching the new doc's tumbler.
        log = EventLog(cat_dir)
        # Truncate + re-seed the prior log with the resurrection
        # sequence keyed to the actual tumbler.
        log.truncate()
        log.append_many([
            ev.make_event(
                ev.DocumentRegisteredPayload(
                    doc_id="doc-A-dead",
                    owner_id=str(owner),
                    content_type="prose",
                    source_uri="file:///x.md",
                    coll_id="docs__test",
                    title="x.md",
                    tumbler=str(new_doc),
                ),
                v=0,
            ),
            ev.make_event(
                ev.DocumentRegisteredPayload(
                    doc_id="doc-B-live",
                    owner_id=str(owner),
                    content_type="prose",
                    source_uri="file:///x.md",
                    coll_id="docs__test",
                    title="x.md",
                    tumbler=str(new_doc),
                ),
                v=0,
            ),
        ])

        runner = CliRunner()
        result = runner.invoke(synthesize_log_cmd, ["--force", "--json"])
        assert result.exit_code == 0, result.output

        # Replay: the new events.jsonl's DocumentRegistered for
        # tumbler new_doc must carry doc_id == doc-B-live (the LAST
        # occurrence in the prior log), not doc-A-dead.
        replayed = list(log.replay())
        registered_for_new_doc = [
            e for e in replayed
            if e.type == ev.TYPE_DOCUMENT_REGISTERED
            and e.payload.tumbler == str(new_doc)
        ]
        assert len(registered_for_new_doc) == 1
        assert registered_for_new_doc[0].payload.doc_id == "doc-B-live"


# ── M3: _v0_document_renamed uses tumbler fallback ────────────────────────


class TestV0DocumentRenamedUsesTumbler:
    def test_rename_with_uuid7_doc_id_falls_back_to_tumbler(self, tmp_path):
        db = CatalogDB(tmp_path / ".catalog.db")
        db.execute(
            "INSERT INTO documents "
            "(tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, "
            "indexed_at, metadata, source_mtime, source_uri) "
            "VALUES ('1.7.42', 'doc', '', 0, '', '', '', '', 0, '', '', "
            "'{}', 0, 'file:///old.py')"
        )
        db.commit()

        proj = Projector(db)
        proj.apply(ev.Event(
            type=ev.TYPE_DOCUMENT_RENAMED, v=0,
            payload=ev.DocumentRenamedPayload(
                doc_id="019de2fc-2b2a-7bc4-9d84-6d0c17d2357e",
                new_source_uri="file:///new.py",
                tumbler="1.7.42",
            ),
            ts="t",
        ))
        db.commit()

        row = db.execute(
            "SELECT source_uri FROM documents WHERE tumbler = ?",
            ("1.7.42",),
        ).fetchone()
        assert row is not None
        assert row[0] == "file:///new.py", (
            "Pre-fix the projector used WHERE tumbler=<UUID7> and the "
            "rename silently no-oped. Post-fix tumbler-or-doc_id falls "
            "back to the tumbler field."
        )
        db.close()

    def test_rename_falls_back_to_doc_id_when_tumbler_empty(self, tmp_path):
        db = CatalogDB(tmp_path / ".catalog.db")
        db.execute(
            "INSERT INTO documents "
            "(tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, "
            "indexed_at, metadata, source_mtime, source_uri) "
            "VALUES ('1.7.42', 'doc', '', 0, '', '', '', '', 0, '', '', "
            "'{}', 0, 'file:///old.py')"
        )
        db.commit()

        proj = Projector(db)
        proj.apply(ev.Event(
            type=ev.TYPE_DOCUMENT_RENAMED, v=0,
            payload=ev.DocumentRenamedPayload(
                doc_id="1.7.42",
                new_source_uri="file:///new.py",
            ),
            ts="t",
        ))
        db.commit()

        row = db.execute(
            "SELECT source_uri FROM documents WHERE tumbler = ?",
            ("1.7.42",),
        ).fetchone()
        assert row is not None
        assert row[0] == "file:///new.py"
        db.close()


# ── M4: rename_collection preserves alias_of ──────────────────────────────


class TestRenameCollectionPreservesAliasOf:
    def test_aliased_doc_keeps_alias_of_after_rename(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("NEXUS_EVENT_LOG_SHADOW", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        canonical = cat.register(
            owner, "canonical.md", content_type="prose",
            file_path="canonical.md",
            physical_collection="docs__old",
        )
        alias = cat.register(
            owner, "alias.md", content_type="prose",
            file_path="alias.md",
            physical_collection="docs__old",
        )
        cat.set_alias(alias, canonical)

        # Confirm alias_of is set in the live SQLite before rename.
        before = cat._db.execute(
            "SELECT alias_of FROM documents WHERE tumbler = ?",
            (str(alias),),
        ).fetchone()
        assert before[0] == str(canonical)

        cat.rename_collection("docs__old", "docs__new")

        # Replay events.jsonl into a fresh CatalogDB. The alias row's
        # alias_of must survive the rename; pre-fix the rename's emit
        # hardcoded alias_of="" and replay broke the alias graph.
        log = EventLog(d)
        proj_db = CatalogDB(tmp_path / "projected.db")
        try:
            Projector(proj_db).apply_all(log.replay())
        finally:
            proj_db.close()

        with sqlite3.connect(str(tmp_path / "projected.db")) as pc:
            row = pc.execute(
                "SELECT alias_of, physical_collection FROM documents "
                "WHERE tumbler = ?",
                (str(alias),),
            ).fetchone()
        assert row is not None
        assert row[0] == str(canonical), (
            f"alias_of lost after rename_collection replay: {row[0]!r}"
        )
        assert row[1] == "docs__new"


# ── M5: _v0_document_aliased rejects self-alias ───────────────────────────


class TestV0DocumentAliasedRejectsSelfAlias:
    def test_self_alias_is_silent_noop(self, tmp_path):
        db = CatalogDB(tmp_path / ".catalog.db")
        db.execute(
            "INSERT INTO documents "
            "(tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, "
            "indexed_at, metadata, source_mtime, source_uri) "
            "VALUES ('1.1.1', 'doc', '', 0, '', '', '', '', 0, '', '', "
            "'{}', 0, '')"
        )
        db.commit()

        proj = Projector(db)
        proj.apply(ev.Event(
            type=ev.TYPE_DOCUMENT_ALIASED, v=0,
            payload=ev.DocumentAliasedPayload(
                alias_doc_id="1.1.1",
                canonical_doc_id="1.1.1",  # self-alias — bad event
            ),
            ts="t",
        ))
        db.commit()

        row = db.execute(
            "SELECT alias_of FROM documents WHERE tumbler = ?",
            ("1.1.1",),
        ).fetchone()
        # Pre-fix the projector would write alias_of="1.1.1" (self),
        # creating a 1-cycle in the alias graph.
        assert row[0] == "", (
            f"Self-alias should be rejected; got alias_of={row[0]!r}"
        )
        db.close()


# ── M8: --strict-not-in-t3 requires --t3-doc-id-coverage ──────────────────


class TestStrictNotInT3RequiresCoverageFlag:
    @pytest.fixture
    def runner(self):
        return CliRunner()

    def test_strict_without_coverage_is_usage_error(self, tmp_path, runner):
        from nexus.catalog.catalog import Catalog as _Cat
        cat_dir = tmp_path / "test-catalog"
        _Cat.init(cat_dir)

        result = runner.invoke(
            doctor_cmd,
            ["--replay-equality", "--strict-not-in-t3"],
        )
        # Pre-fix this was a silent no-op; the strict flag was simply
        # ignored when --t3-doc-id-coverage wasn't passed. Post-fix it
        # raises a UsageError so an operator can see the dependency.
        assert result.exit_code != 0
        assert "strict-not-in-t3 requires" in result.output.lower() or (
            "strict-not-in-t3 requires" in (result.stderr or "").lower()
        )


# ── M8b: JSON payload includes strict_not_in_t3 ───────────────────────────


class TestStrictModeAuditableInJson:
    """The per-collection ``pass`` reflects strict-vs-default mode, but
    a JSON consumer reading historical reports cannot tell which mode
    produced the result without the explicit field."""

    def test_strict_flag_surfaced_in_json_payload(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        import chromadb

        client = chromadb.EphemeralClient()
        for c in list(client.list_collections()):
            try:
                client.delete_collection(c.name)
            except Exception:
                pass

        cat_dir = tmp_path / "test-catalog"
        Catalog.init(cat_dir)
        log = EventLog(cat_dir)
        log.append([
            ev.Event(
                type=ev.TYPE_CHUNK_INDEXED, v=0,
                payload=ev.ChunkIndexedPayload(
                    chunk_id="c1", chash="h", doc_id="uuid-A",
                    coll_id="code__test", position=0,
                ),
                ts="t",
            ),
        ][0])
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
        col = client.get_or_create_collection(
            "code__test", embedding_function=DefaultEmbeddingFunction(),
        )
        col.add(
            ids=["c1"], documents=["x"],
            metadatas=[{"doc_id": "uuid-A"}],
        )

        class _FakeT3:
            _client = client

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())

        runner = CliRunner()
        # Default mode.
        r1 = runner.invoke(doctor_cmd, ["--t3-doc-id-coverage", "--json"])
        p1 = json.loads(r1.output)["t3_doc_id_coverage"]
        assert p1["strict_not_in_t3"] is False

        # Strict mode.
        r2 = runner.invoke(
            doctor_cmd,
            ["--t3-doc-id-coverage", "--strict-not-in-t3", "--json"],
        )
        p2 = json.loads(r2.output)["t3_doc_id_coverage"]
        assert p2["strict_not_in_t3"] is True


# ── M9: repair --assign preserves position / content_hash ─────────────────


class TestRepairAssignPreservesPosition:
    def test_position_carried_to_corrective_event(self, tmp_path):
        cat_dir = tmp_path / "test-catalog"
        Catalog.init(cat_dir)
        log = EventLog(cat_dir)
        log.append(ev.Event(
            type=ev.TYPE_CHUNK_INDEXED, v=0,
            payload=ev.ChunkIndexedPayload(
                chunk_id="orph1", chash="hash1", doc_id="",
                coll_id="code__test", position=42,
                content_hash="contenthash1",
                embedded_at="2026-04-30T12:00:00Z",
                synthesized_orphan=True,
            ),
            ts="t",
        ))

        runner = CliRunner()
        result = runner.invoke(
            repair_orphan_chunks_cmd,
            ["--assign", "orph1:doc-uuid-A", "--json"],
        )
        assert result.exit_code == 0, result.output

        events = list(log.replay())
        corrective = events[-1]
        assert corrective.type == ev.TYPE_CHUNK_INDEXED
        assert corrective.payload.chunk_id == "orph1"
        assert corrective.payload.synthesized_orphan is False
        assert corrective.payload.doc_id == "doc-uuid-A"
        # Pre-fix position was hardcoded to 0 — the orphan's position
        # was lost on repair. Post-fix it carries through.
        assert corrective.payload.position == 42
        assert corrective.payload.content_hash == "contenthash1"
        assert corrective.payload.embedded_at == "2026-04-30T12:00:00Z"
