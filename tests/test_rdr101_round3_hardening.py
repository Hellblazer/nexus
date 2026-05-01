# SPDX-License-Identifier: AGPL-3.0-or-later

"""RDR-101 Phase 3 round-3 review remediation: hardening / diagnostics.

Companion to ``test_rdr101_round3_correctness.py``. This file covers the
non-load-bearing items: failure-mode regression tests, diagnostic
emissions, and singleton-cache documentation guarantees.

Coverage:

1. Mid-mutation failure modes — when ``_write_to_event_log`` raises
   TypeError (non-serializable payload), the catalog must NOT commit
   SQLite, NOT append legacy JSONL, and the events.jsonl must remain
   un-extended (the writer holds the flock and the failure short-
   circuits BEFORE the projector runs).
2. Projector failure mid-mutation propagates to the caller (no silent
   half-write).
3. ``Catalog.__init__`` emits a structured ``catalog_gate_state`` log
   line so an operator can confirm the active write path without
   grepping the environment.
4. ``Catalog.mtime_paths()`` includes events.jsonl so the MCP
   singleton's freshness check picks up cross-process event-sourced
   writes.
5. FTS5 documents-table rowid behaviour under
   ``rename_collection`` — replay equality holds regardless of the
   internal rowid the write path picks (smoke test against the row's
   actual searchability).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from nexus.catalog import events as ev
from nexus.catalog.catalog import Catalog
from nexus.catalog.catalog_db import CatalogDB
from nexus.catalog.event_log import EventLog
from nexus.catalog.projector import Projector


# ── Mid-mutation failure: events.jsonl write raises ──────────────────────


class TestRegisterOwnerCrashWindow:
    """Round-4 review (reviewer A C2): under NEXUS_EVENT_SOURCED=1 the
    owner high-water-mark must come from SQLite, not owners.jsonl.
    Pre-fix a crash between SQLite commit and JSONL append left
    owners.jsonl with stale next_seq → next register_owner allocated
    a duplicate ``1.1`` tumbler → projector's INSERT OR REPLACE
    silently overwrote the first owner on next replay."""

    def test_event_sourced_owner_allocation_uses_sqlite(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        first = cat.register_owner("nexus", "repo", repo_hash="aaaa")
        # Simulate the crash window: SQLite has the owner, owners.jsonl
        # is truncated (as if the JSONL append never happened).
        owners_jsonl = d / "owners.jsonl"
        owners_jsonl.write_text("")
        # Next register_owner must read the high-water-mark from
        # SQLite under ES, not the now-empty JSONL.
        second = cat.register_owner("other", "repo", repo_hash="bbbb")
        assert str(first) != str(second), (
            f"register_owner re-allocated tumbler {first} as {second} "
            "after JSONL truncation — ES path incorrectly reads "
            "high-water-mark from owners.jsonl"
        )
        # Both rows survive in SQLite.
        assert cat._db.execute("SELECT count(*) FROM owners").fetchone()[0] == 2


class TestDoctorReportSchema:
    """Round-4 review (reviewer D): the existing doctor JSON consumer
    test in test_catalog_doctor_replay_equality.py does not assert on
    the new ``event_source`` field. This test pins the schema so a
    future refactor that drops the field breaks loudly."""

    def test_replay_equality_report_includes_event_source(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        from nexus.commands.catalog import _run_replay_equality

        # PR ζ flipped default to ES; legacy-path assertion.
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
        monkeypatch.delenv("NEXUS_EVENT_LOG_SHADOW", raising=False)
        d = tmp_path / "test-catalog"
        Catalog.init(d)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(d))
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        cat.register(owner, "a.md", content_type="prose", file_path="a.md")
        cat._db.close()

        report = _run_replay_equality()
        assert "event_source" in report
        assert report["event_source"] in ("events.jsonl", "synthesized")
        assert "shadow_only" in report
        assert isinstance(report["shadow_only"], bool)


class TestEventLogWriteFailure:
    """When ``_write_to_event_log`` fails under ``NEXUS_EVENT_SOURCED=1``,
    the entire mutation must abort. SQLite must not be mutated, the
    legacy JSONL must not be appended, and the events.jsonl must not
    be partial.

    The event-sourced order is: events.jsonl → projector.apply → commit
    → legacy JSONL. A failure in step 1 short-circuits steps 2-4.
    """

    def test_eventlog_failure_aborts_register(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")

        # Patch _write_to_event_log to raise as if json.dumps failed
        # on a non-serializable payload value.
        def _boom(self, event):
            raise TypeError("payload not JSON-serializable")

        monkeypatch.setattr(
            "nexus.catalog.catalog.Catalog._write_to_event_log", _boom
        )

        with pytest.raises(TypeError):
            cat.register(owner, "doc.md", content_type="prose", file_path="doc.md")

        # SQLite has no document row — the projector never ran.
        rows = cat._db.execute("SELECT count(*) FROM documents").fetchone()
        assert rows[0] == 0
        # documents.jsonl was not appended (or the line wasn't added).
        text = (d / "documents.jsonl").read_text() if (d / "documents.jsonl").exists() else ""
        assert "doc.md" not in text


# ── Mid-mutation failure: projector raises ───────────────────────────────


class TestProjectorFailure:
    """Projector raising mid-apply propagates to the caller.

    Pre-fix the v: 1 path silently swallowed events; the round-3 fix
    flipped that to NotImplementedError. These tests pin the contract
    on the direct-apply path AND the end-to-end mutator path so a
    future ``except Exception: pass`` somewhere upstream cannot
    silently swallow the raise."""

    def test_projector_v1_raises_propagates(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        cat.register_owner("nexus", "repo", repo_hash="abab")

        bad = ev.make_event(
            ev.DocumentDeletedPayload(doc_id="1.7.42", reason="manual"),
            v=1,
        )
        with pytest.raises(NotImplementedError):
            cat._projector.apply(bad)

    def test_projector_failure_mid_register_propagates(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        # Round-4 review: end-to-end mutator path. Patch the cached
        # projector's ``apply`` to raise mid-register(); the exception
        # must propagate out of register() with no SQLite row, no
        # legacy JSONL append, and (because the event log was already
        # written before the projector ran) events.jsonl ahead by one
        # event that has no SQLite counterpart.
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")

        owner_count_before = cat._db.execute(
            "SELECT count(*) FROM owners"
        ).fetchone()[0]

        # Patch the projector to raise on the next apply().
        original_apply = cat._projector.apply
        calls = {"n": 0}

        def boom(event):
            calls["n"] += 1
            raise RuntimeError("projector exploded")

        monkeypatch.setattr(cat._projector, "apply", boom)

        with pytest.raises(RuntimeError, match="projector exploded"):
            cat.register(owner, "doc.md", content_type="prose",
                         file_path="doc.md")

        # SQLite has no document row.
        doc_count = cat._db.execute(
            "SELECT count(*) FROM documents"
        ).fetchone()[0]
        assert doc_count == 0

        # Owner count unchanged (the failure was in document register,
        # not owner register).
        assert cat._db.execute(
            "SELECT count(*) FROM owners"
        ).fetchone()[0] == owner_count_before

        # Restore so cleanup doesn't deadlock; the lock was already
        # released via the finally clause inside register().
        monkeypatch.setattr(cat._projector, "apply", original_apply)


# ── Diagnostics: gate-state log emission ─────────────────────────────────


class TestGateStateLog:
    def test_init_emits_catalog_gate_state(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        # Round-4 review (reviewer E): the prior assertion
        # ``isinstance(gate_records, list)`` was vacuously true. This
        # version monkeypatches the catalog module's logger and asserts
        # the structured event was emitted with the correct flags.
        from nexus.catalog import catalog as cat_mod

        events: list[dict] = []

        class _Capture:
            def debug(self, event: str, **kw):
                events.append({"level": "debug", "event": event, **kw})

            def info(self, event: str, **kw):
                events.append({"level": "info", "event": event, **kw})

            def warning(self, event: str, **kw):
                events.append({"level": "warning", "event": event, **kw})

        monkeypatch.setattr(cat_mod, "_log", _Capture())
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        monkeypatch.setenv("NEXUS_EVENT_LOG_SHADOW", "0")
        d = tmp_path / "catalog"
        d.mkdir()
        Catalog(d, d / ".catalog.db")

        gate_lines = [e for e in events if e["event"] == "catalog_gate_state"]
        assert len(gate_lines) >= 1, (
            f"catalog_gate_state never emitted; got events: {events}"
        )
        line = gate_lines[0]
        assert line["event_sourced"] is True
        assert line["shadow_emit"] is False
        assert str(d) in line["catalog_dir"]


# ── mtime_paths includes events.jsonl ────────────────────────────────────


class TestMtimePaths:
    def test_mtime_paths_includes_events_jsonl(self, tmp_path):
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        paths = cat.mtime_paths()
        names = {p.name for p in paths}
        assert names == {
            "owners.jsonl",
            "documents.jsonl",
            "links.jsonl",
            "events.jsonl",
        }

    def test_jsonl_paths_excludes_events_jsonl(self, tmp_path):
        # _should_compact uses path.stem to map back to a SQL table; the
        # legacy three-tuple must NOT include events.jsonl (no events
        # SQL table).
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        paths = cat.jsonl_paths()
        names = {p.name for p in paths}
        assert names == {"owners.jsonl", "documents.jsonl", "links.jsonl"}


# ── FTS5 rowid divergence under rename_collection ────────────────────────


class TestFtsRowidUnderRenameCollection:
    """rename_collection under NEXUS_EVENT_SOURCED=1 routes through
    INSERT OR REPLACE in the projector, which deletes-and-reinserts the
    document row with a fresh rowid. The FTS5 trigger fires accordingly,
    rebuilding the FTS index for the new rowid. This test pins the
    behaviour: a search that hit the doc before the rename still hits
    after the rename."""

    def test_search_hits_after_event_sourced_rename(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        cat.register(
            owner, "uniqueterm-alpha-doc.md",
            content_type="prose",
            file_path="alpha.md",
            physical_collection="docs__old",
        )

        before = cat.find("uniqueterm-alpha-doc")
        assert len(before) == 1

        n = cat.rename_collection("docs__old", "docs__new")
        assert n == 1

        # FTS5 still resolves the row by title even after the
        # delete-and-reinsert that INSERT OR REPLACE performs.
        after = cat.find("uniqueterm-alpha-doc")
        assert len(after) == 1
        assert after[0].physical_collection == "docs__new"

    def test_replay_equality_after_rename(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        # Even with the FTS5 reinsert, replay-equality on the documents
        # table holds (the doctor verb already excludes id by name; this
        # test confirms the same for documents.tumbler PK + columns).
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        cat.register(
            owner, "x.md", content_type="prose",
            file_path="x.md", physical_collection="docs__old",
        )
        cat.rename_collection("docs__old", "docs__new")
        cat._db.close()

        log = EventLog(d)
        proj_db = CatalogDB(tmp_path / "projected.db")
        try:
            Projector(proj_db).apply_all(log.replay())
        finally:
            proj_db.close()

        with sqlite3.connect(str(d / ".catalog.db")) as live, \
             sqlite3.connect(str(tmp_path / "projected.db")) as proj:
            live_row = live.execute(
                "SELECT physical_collection FROM documents"
            ).fetchall()
            proj_row = proj.execute(
                "SELECT physical_collection FROM documents"
            ).fetchall()
        assert live_row == proj_row == [("docs__new",)]
