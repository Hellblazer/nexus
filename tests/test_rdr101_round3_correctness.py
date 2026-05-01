# SPDX-License-Identifier: AGPL-3.0-or-later

"""RDR-101 Phase 3 round-3 review remediation: correctness fixes.

Covers the load-bearing items from the round-3 review of PRs #430/#431
that block the irreversibility cutover (NEXUS_EVENT_SOURCED default
flip):

1. ``link`` / ``link_if_absent`` / ``unlink`` / ``bulk_unlink`` event-source
   the LinkCreated / LinkDeleted events when ``NEXUS_EVENT_SOURCED=1``.
   Pre-fix these mutators stayed on the legacy direct-write path and
   the event log silently dropped every link mutation under the gate.
2. ``Catalog._ensure_consistent`` rebuilds from ``events.jsonl`` when the
   gate is on. Pre-fix it always read legacy JSONL, so a cross-process
   write that landed only in the event log was invisible to subsequent
   ``Catalog()`` instances.
3. ``nx catalog doctor --replay-equality`` reads ``events.jsonl`` when
   present. Pre-fix it always called ``synthesize_from_jsonl`` so once
   the gate was on the verb measured the wrong source of truth.
4. Projector ``_v1_unsupported`` raises (covered in
   ``test_catalog_projector.py::TestUnknownDispatch::test_v1_known_type_raises``).
5. ``make_event`` defaults ``v=0`` (covered in
   ``test_catalog_events.py::TestVersioning::test_default_version_is_0``).
6. Legacy ``update()`` ``INSERT OR REPLACE`` includes ``alias_of`` so an
   alias survives a subsequent update().
7. Single ``Projector`` instance cached at ``Catalog.__init__``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nexus.catalog import events as ev
from nexus.catalog.catalog import Catalog
from nexus.catalog.catalog_db import CatalogDB
from nexus.catalog.event_log import EventLog
from nexus.catalog.projector import Projector


# ── Link mutators event-sourced ──────────────────────────────────────────


class TestLinkEventSourced:
    """``link`` writes LinkCreated to events.jsonl under the gate."""

    def test_link_emits_event_and_projects(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        a = cat.register(owner, "a.md", content_type="prose")
        b = cat.register(owner, "b.md", content_type="prose")

        created = cat.link(a, b, "cites", "agent-1")
        assert created is True

        log = EventLog(d)
        link_events = [
            e for e in log.replay()
            if e.type == ev.TYPE_LINK_CREATED
        ]
        assert len(link_events) == 1
        p = link_events[0].payload
        assert p.from_doc == str(a)
        assert p.to_doc == str(b)
        assert p.link_type == "cites"
        assert p.creator == "agent-1"

        rows = cat._db.execute(
            "SELECT count(*) FROM links WHERE from_tumbler=? AND to_tumbler=?",
            (str(a), str(b)),
        ).fetchone()
        assert rows[0] == 1

    def test_link_merge_overwrites_via_insert_or_replace(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        # Two link() calls on the same composite key emit two
        # LinkCreated events. Replay through the projector's
        # INSERT OR REPLACE must converge on the SECOND payload's
        # merged metadata. INSERT OR IGNORE would have silently
        # dropped the second event and the merged co_discovered_by
        # list would never reach SQLite.
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        a = cat.register(owner, "a.md", content_type="prose")
        b = cat.register(owner, "b.md", content_type="prose")
        cat.link(a, b, "cites", "agent-1")
        merged = cat.link(a, b, "cites", "agent-2")
        assert merged is False

        # Replay events.jsonl into a fresh DB; the merged metadata
        # must include both creators in co_discovered_by.
        log = EventLog(d)
        proj_db = CatalogDB(tmp_path / "projected.db")
        try:
            Projector(proj_db).apply_all(log.replay())
        finally:
            proj_db.close()

        with sqlite3.connect(str(tmp_path / "projected.db")) as conn:
            row = conn.execute(
                "SELECT metadata FROM links WHERE from_tumbler=? "
                "AND to_tumbler=? AND link_type=?",
                (str(a), str(b), "cites"),
            ).fetchone()
        import json
        meta = json.loads(row[0])
        assert "agent-2" in meta.get("co_discovered_by", [])

    def test_unlink_emits_event_and_deletes_row(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        a = cat.register(owner, "a.md", content_type="prose")
        b = cat.register(owner, "b.md", content_type="prose")
        cat.link(a, b, "cites", "agent-1")
        n = cat.unlink(a, b, "cites")
        assert n == 1

        log = EventLog(d)
        types = [e.type for e in log.replay()]
        assert types.count(ev.TYPE_LINK_DELETED) == 1

        rows = cat._db.execute(
            "SELECT count(*) FROM links WHERE from_tumbler=? AND to_tumbler=?",
            (str(a), str(b)),
        ).fetchone()
        assert rows[0] == 0

    def test_link_if_absent_emits_event(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        a = cat.register(owner, "a.md", content_type="prose")
        b = cat.register(owner, "b.md", content_type="prose")
        created = cat.link_if_absent(a, b, "cites", "agent-1")
        assert created is True

        log = EventLog(d)
        link_events = [
            e for e in log.replay() if e.type == ev.TYPE_LINK_CREATED
        ]
        assert len(link_events) == 1

        # Second call on the same key returns False and emits NO event.
        skipped = cat.link_if_absent(a, b, "cites", "agent-2")
        assert skipped is False
        link_events = [
            e for e in EventLog(d).replay()
            if e.type == ev.TYPE_LINK_CREATED
        ]
        assert len(link_events) == 1

    def test_bulk_unlink_emits_events(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        a = cat.register(owner, "a.md", content_type="prose")
        b = cat.register(owner, "b.md", content_type="prose")
        c = cat.register(owner, "c.md", content_type="prose")
        cat.link(a, b, "cites", "agent-1")
        cat.link(a, c, "cites", "agent-1")
        n = cat.bulk_unlink(from_t=str(a), link_type="cites")
        assert n == 2

        log = EventLog(d)
        types = [e.type for e in log.replay()]
        assert types.count(ev.TYPE_LINK_DELETED) == 2

    def test_full_replay_includes_links(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        a = cat.register(owner, "a.md", content_type="prose")
        b = cat.register(owner, "b.md", content_type="prose")
        cat.link(a, b, "cites", "agent-1")
        cat._db.close()

        log = EventLog(d)
        proj_db = CatalogDB(tmp_path / "projected.db")
        try:
            Projector(proj_db).apply_all(log.replay())
        finally:
            proj_db.close()

        with sqlite3.connect(str(tmp_path / "projected.db")) as conn:
            row = conn.execute(
                "SELECT count(*) FROM links WHERE from_tumbler=? AND to_tumbler=?",
                (str(a), str(b)),
            ).fetchone()
        assert row[0] == 1


# ── _ensure_consistent rebuilds from events.jsonl ────────────────────────


class TestEnsureConsistentEventSourced:
    def test_second_catalog_sees_events_jsonl_writes(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        # Cross-process scenario: process A writes via event-sourced;
        # process B opens the catalog and must see the same state.
        # Pre-fix B would rebuild from JSONL only and miss any
        # divergence.
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat_a = Catalog(d, d / ".catalog.db")
        owner = cat_a.register_owner("nexus", "repo", repo_hash="abab")
        a = cat_a.register(owner, "a.md", content_type="prose")
        cat_a._db.close()

        cat_b = Catalog(d, tmp_path / "process_b.db")
        try:
            row = cat_b._db.execute(
                "SELECT title FROM documents WHERE tumbler = ?",
                (str(a),),
            ).fetchone()
            assert row == ("a.md",)
        finally:
            cat_b._db.close()

    def test_rebuild_clears_existing_rows_before_replay(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        # Round-4 review (reviewer E): the previous test opened cat_b
        # against a fresh SQLite, so the DELETE FROM was a no-op and
        # the contract "rebuild clears stale rows before replay" was
        # untested. This test pre-populates a SQLite with a row that
        # is NOT in events.jsonl, then opens a Catalog against it, and
        # asserts the stale row is gone after _ensure_consistent
        # rebuilds from events.jsonl.
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        # Step 1: write a real event-sourced state with one document.
        cat_a = Catalog(d, d / ".catalog.db")
        owner = cat_a.register_owner("nexus", "repo", repo_hash="abab")
        real = cat_a.register(owner, "real.md", content_type="prose")
        cat_a._db.close()

        # Step 2: build a stale SQLite with a phantom row that
        # events.jsonl does NOT contain. This stands in for cross-
        # process drift (or a corrupt cache).
        stale_db_path = tmp_path / "stale.db"
        stale_db = CatalogDB(stale_db_path)
        try:
            # Same owner so the FK structure is intact.
            stale_db.execute(
                "INSERT OR REPLACE INTO owners "
                "(tumbler_prefix, name, owner_type, repo_hash, "
                "description, repo_root) VALUES (?, ?, ?, ?, ?, ?)",
                ("1.1", "nexus", "repo", "abab", "", ""),
            )
            stale_db.execute(
                "INSERT INTO documents (tumbler, title, content_type) "
                "VALUES (?, ?, ?)",
                ("1.1.999", "phantom.md", "prose"),
            )
            stale_db.commit()
        finally:
            stale_db.close()

        # Step 3: open Catalog against the stale SQLite. _ensure_consistent
        # must DELETE the phantom row and replay events.jsonl.
        cat_b = Catalog(d, stale_db_path)
        try:
            phantom = cat_b._db.execute(
                "SELECT count(*) FROM documents WHERE tumbler = ?",
                ("1.1.999",),
            ).fetchone()
            assert phantom[0] == 0, (
                "stale phantom row survived the event-sourced rebuild — "
                "the DELETE-and-replay contract is broken"
            )
            real_row = cat_b._db.execute(
                "SELECT title FROM documents WHERE tumbler = ?",
                (str(real),),
            ).fetchone()
            assert real_row == ("real.md",)
        finally:
            cat_b._db.close()

    def test_bootstrap_guardrail_refuses_when_event_log_sparse(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        # Round-4 review (reviewer B EC-1): operator flips
        # NEXUS_EVENT_SOURCED=1 on a catalog that has 10 documents in
        # documents.jsonl but only 1 event in events.jsonl (the first
        # post-flip write). The event-sourced rebuild would DELETE all
        # 10 legacy rows and replay only the 1 event, silently wiping
        # the catalog. The guardrail must detect this and fall through
        # to the legacy rebuild.
        monkeypatch.delenv("NEXUS_EVENT_SOURCED", raising=False)
        monkeypatch.delenv("NEXUS_EVENT_LOG_SHADOW", raising=False)
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        for i in range(10):
            cat.register(owner, f"doc-{i}.md", content_type="prose",
                         file_path=f"doc-{i}.md")
        cat._db.close()

        # Now manually write a single event into events.jsonl (as if
        # one event-sourced write happened after the gate was flipped).
        events_path = d / "events.jsonl"
        events_path.write_text(
            '{"type":"DocumentRegistered","v":0,"payload":{'
            '"doc_id":"1.1.99","owner_id":"1.1","content_type":"prose",'
            '"source_uri":"","coll_id":"","title":"new.md","tumbler":"1.1.99",'
            '"author":"","year":0,"file_path":"new.md","corpus":"",'
            '"physical_collection":"","chunk_count":0,"head_hash":"",'
            '"indexed_at":"","alias_of":"","meta":{},"source_mtime":0.0,'
            '"indexed_at_doc":""},"ts":"2026-05-01T00:00:00+00:00"}\n'
        )

        # Now open with the gate ON. The guardrail should refuse the
        # event-sourced rebuild and fall through to legacy.
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        cat2 = Catalog(d, tmp_path / "process_b.db")
        try:
            doc_count = cat2._db.execute(
                "SELECT count(*) FROM documents"
            ).fetchone()[0]
            # The legacy 10 documents must survive — bootstrap guardrail
            # refused the event-sourced rebuild that would have wiped
            # to 1 row.
            assert doc_count >= 10, (
                f"bootstrap guardrail failed: legacy rebuild produced "
                f"{doc_count} rows but documents.jsonl has 10"
            )
        finally:
            cat2._db.close()

    def test_atomicity_apply_all_failure_rolls_back_deletes(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        # Round-4 review (reviewer C): _ensure_consistent's
        # DELETE+replay must be atomic. If apply_all raises (e.g. a
        # malformed event triggers NotImplementedError via a v: 1
        # path), the DELETEs must roll back, leaving SQLite in its
        # prior state.
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "catalog"
        d.mkdir()
        cat_a = Catalog(d, d / ".catalog.db")
        owner = cat_a.register_owner("nexus", "repo", repo_hash="abab")
        # Register enough documents that the bootstrap guardrail
        # (RDR-101 Phase 3 follow-up B floor at 1) unambiguously
        # passes — leaving the v:1 raise as the failure path the
        # test is exercising. With one document, a poisoned v:1
        # DocumentDeleted decrements event_doc_count to 0 < 1, the
        # guardrail fires before the rebuild attempt, and the
        # atomicity invariant has nothing to assert against.
        a = cat_a.register(owner, "a.md", content_type="prose")
        cat_a.register(owner, "b.md", content_type="prose")
        cat_a.register(owner, "c.md", content_type="prose")
        cat_a._db.close()

        # Append a v: 1 event after the legitimate v: 0 events. The
        # projector will raise NotImplementedError when it dispatches
        # the v: 1 line.
        events_path = d / "events.jsonl"
        events_path.open("a").write(
            '{"type":"DocumentDeleted","v":1,"payload":{'
            '"doc_id":"1.1.99","reason":"poisoned"},'
            '"ts":"2026-05-01T00:00:00+00:00"}\n'
        )

        # Pre-populate a separate SQLite with a sentinel row that
        # MUST survive a failed rebuild (proves the DELETEs rolled
        # back).
        stale_path = tmp_path / "stale.db"
        stale = CatalogDB(stale_path)
        try:
            stale.execute(
                "INSERT OR REPLACE INTO owners "
                "(tumbler_prefix, name, owner_type, repo_hash, "
                "description, repo_root) VALUES (?, ?, ?, ?, ?, ?)",
                ("1.1", "nexus", "repo", "abab", "", ""),
            )
            stale.execute(
                "INSERT INTO documents (tumbler, title, content_type) "
                "VALUES (?, ?, ?)",
                ("1.1.42", "sentinel.md", "prose"),
            )
            stale.commit()
        finally:
            stale.close()

        # Open Catalog against the stale SQLite. _ensure_consistent
        # tries the event-sourced rebuild, hits the v: 1 raise, and
        # rolls back the DELETEs. The sentinel row must survive.
        cat_b = Catalog(d, stale_path)
        try:
            assert cat_b.degraded is True, (
                "Catalog should be marked degraded after a failed rebuild"
            )
            sentinel = cat_b._db.execute(
                "SELECT count(*) FROM documents WHERE tumbler = ?",
                ("1.1.42",),
            ).fetchone()
            assert sentinel[0] == 1, (
                "DELETE was not rolled back — atomicity is broken; "
                "the sentinel row was wiped by the failed rebuild"
            )
        finally:
            cat_b._db.close()


# ── Doctor --replay-equality reads events.jsonl ──────────────────────────


class TestDoctorReplayEqualityEventLog:
    """``nx catalog doctor --replay-equality`` reads ``events.jsonl`` when
    it exists and has content — pre-fix it always called
    ``synthesize_from_jsonl``, so once ``NEXUS_EVENT_SOURCED=1`` was on by
    default the verb measured the wrong source of truth."""

    def test_doctor_uses_events_jsonl_when_present(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        # Round-4 review (reviewer E): the previous test was
        # discrimination-free — synthesize_from_jsonl and
        # EventLog.replay produced the same projection for the
        # scenario, so a regression that always called the synthesizer
        # would still pass. This test writes events.jsonl content that
        # the synthesizer CANNOT reproduce (a link with no entry in
        # links.jsonl), then projects through the doctor and asserts
        # the link IS present in the projected DB. Only the
        # events.jsonl path can produce that output.
        from nexus.commands.catalog import _run_replay_equality

        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        d = tmp_path / "test-catalog"
        Catalog.init(d)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(d))
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        a = cat.register(owner, "a.md", content_type="prose", file_path="a.md")
        b = cat.register(owner, "b.md", content_type="prose", file_path="b.md")
        cat.link(a, b, "cites", "agent-1")
        cat._db.close()

        # Sanity: links.jsonl must contain the link (event-sourced
        # path still writes legacy JSONL for back-compat).
        links_text = (d / "links.jsonl").read_text()
        assert "cites" in links_text

        # Now strip the link from links.jsonl so the synthesizer
        # branch CANNOT reproduce it. Only the events.jsonl branch
        # carries the LinkCreated event.
        (d / "links.jsonl").write_text("")

        report = _run_replay_equality()
        assert report["event_source"] == "events.jsonl", report
        # The projected DB has the link because we replayed
        # events.jsonl. The live DB also has it (it was committed
        # there at link() time). So replay-equality holds for links.
        # If the doctor had instead called synthesize_from_jsonl
        # against the empty links.jsonl, the projected DB would have
        # NO link rows, and replay-equality would FAIL with
        # only_in_live=[the link]. We check for pass=True as the
        # discriminating assertion.
        assert report["pass"] is True, (
            "Doctor must replay events.jsonl, not synthesize from the "
            "(now-empty) links.jsonl. Report:\n" + str(report)
        )

    def test_doctor_falls_back_to_synthesizer_when_no_events(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        from nexus.commands.catalog import _run_replay_equality

        # PR ζ flipped NEXUS_EVENT_SOURCED default to ON; the doctor
        # synthesizer fallback is the legacy path, so pin explicitly.
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
        monkeypatch.delenv("NEXUS_EVENT_LOG_SHADOW", raising=False)
        d = tmp_path / "test-catalog"
        Catalog.init(d)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(d))
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        cat.register(owner, "a.md", content_type="prose", file_path="a.md")
        cat._db.close()

        # events.jsonl shouldn't exist yet (no shadow, no event-sourced).
        events_path = d / "events.jsonl"
        assert (
            not events_path.exists() or events_path.stat().st_size == 0
        )
        report = _run_replay_equality()
        assert report["pass"] is True, report
        assert report["event_source"] == "synthesized"


# ── Legacy update() carries alias_of through INSERT OR REPLACE ───────────


class TestLegacyUpdateAliasOfColumn:
    """The legacy ``update()`` ``INSERT OR REPLACE`` column list
    includes ``alias_of`` and the round-4 fix threads
    ``rec_dict["alias_of"]`` so a caller passing ``alias_of`` in
    ``**fields`` actually lands.

    Round-4 review (reviewer E) flagged the original test as a non-
    test (alias_of="" matches the column default — removing the
    column would have produced the same value). These replacements
    pre-set alias_of via set_alias() and verify it survives an
    update(), and explicitly pass alias_of via **fields and verify
    the value lands (the round-4 rec_dict["alias_of"] threading
    fix)."""

    def test_alias_of_survives_legacy_update_through_alias(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        # PR ζ flipped default to ES; this is a legacy-path test.
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
        monkeypatch.delenv("NEXUS_EVENT_LOG_SHADOW", raising=False)
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        canonical = cat.register(owner, "canonical.md", content_type="prose")
        alias = cat.register(owner, "alias.md", content_type="prose")
        cat.set_alias(alias, canonical)

        # update() FOLLOWS the alias by default — it ends up updating
        # canonical's row, not alias's. The alias row's alias_of must
        # not change. Pins resolve-follows-alias semantics.
        cat.update(alias, chunk_count=99)
        alias_row = cat._db.execute(
            "SELECT alias_of, chunk_count FROM documents WHERE tumbler = ?",
            (str(alias),),
        ).fetchone()
        assert alias_row == (str(canonical), 0)
        canon_row = cat._db.execute(
            "SELECT alias_of, chunk_count FROM documents WHERE tumbler = ?",
            (str(canonical),),
        ).fetchone()
        assert canon_row == ("", 99)

    def test_explicit_alias_of_in_fields_lands(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        # Round-4 review (reviewer D): caller passes ``alias_of``
        # explicitly. Pre-fix both event payload and legacy SQL
        # VALUES read ``entry.alias_of``, silently dropping the
        # caller-supplied value. Round-4 fix threads
        # ``rec_dict["alias_of"]``.
        #
        # PR ζ flipped default to ES; this is a legacy-path test.
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
        monkeypatch.delenv("NEXUS_EVENT_LOG_SHADOW", raising=False)
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        a = cat.register(owner, "a.md", content_type="prose")
        b = cat.register(owner, "b.md", content_type="prose")
        cat.update(a, alias_of=str(b))
        row = cat._db.execute(
            "SELECT alias_of FROM documents WHERE tumbler = ?",
            (str(a),),
        ).fetchone()
        assert row[0] == str(b), (
            "update(t, alias_of='X') silently dropped the value — "
            "rec_dict['alias_of'] is not threaded through"
        )


# ── Cached projector ─────────────────────────────────────────────────────


class TestProjectorCached:
    def test_catalog_caches_projector_at_init(
        self, tmp_path,
    ):
        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        # Single instance, accessible via attribute.
        proj1 = cat._projector
        proj2 = cat._projector
        assert proj1 is proj2
        assert isinstance(proj1, Projector)
