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

CATALOG SUBSTRATE (nexus-i711w Stage 2). An earlier sweep recorded this file
as a WHOLE-FILE DELETE because its four module-level imports all die, so it
could not COLLECT after the deletion. That was OVERTURNED by subject: 3 of
its 14 tests are about ``alias_of`` threading through ``update()``, which
``HttpCatalogClient`` implements, and deleting the file would have silently
dropped them. Measured disposition: 2 PORT, 1 PORT-BLOCKED, 1 GAP, 10 DIE.

  - PORT (2 of the 3 attempted): two of ``TestLegacyUpdateAliasOfColumn``.
    Kept in this file, so all four dying imports moved INSIDE the bodies that
    still need them and the file still collects. The third,
    ``test_alias_of_survives_legacy_update_through_alias``, is PORT-BLOCKED:
    the service ``update`` does not follow aliases, so the port inverted the
    assertion (measured, not predicted). It stays pinned and annotated.
  - GAP (1): ``test_link_merge_overwrites_via_insert_or_replace`` — see its
    own annotation.
  - DIE (10): the link event-emission tests, the ``_ensure_consistent``
    DELETE-and-replay cohort (already pinned at CLASS level before this
    change — left exactly as it was), and the cached-``Projector`` test.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest

from tests._catalog_fixture_ops import ActiveCatalog, unroutable_write_target


@pytest.fixture()
def active_catalog() -> ActiveCatalog:
    """Seed and read through whichever catalog is live (nexus-i711w Stage 2)."""
    return ActiveCatalog()


def _slug() -> str:
    """A per-test discriminator for owner names and file paths."""
    return uuid.uuid4().hex[:8]


def _local_catalog(tmp_path) -> tuple[Any, Path]:
    """A real LOCAL SQLite ``Catalog`` rooted in *tmp_path*, plus its dir.

    ONLY for the DIE and GAP cohorts; callers also carry
    ``local_catalog_backend``.
    """
    from nexus.catalog.catalog import Catalog

    d = tmp_path / "catalog"
    d.mkdir()
    return Catalog(d, d / ".catalog.db"), d


def _alias_of(cat: Any, tumbler: Any) -> str:
    """The ``alias_of`` of *tumbler*'s OWN row, not of what it points at.

    ``resolve`` FOLLOWS the alias by default on both substrates, so
    ``resolve(alias).alias_of`` hands back the canonical row's (empty)
    ``alias_of`` and an assertion built on it is structurally unable to
    observe the write it is about. Scanning ``all_documents`` for the exact
    tumbler is the substrate-neutral form of the old
    ``SELECT alias_of FROM documents WHERE tumbler = ?``.
    """
    rows = [d for d in cat.all_documents() if str(d.tumbler) == str(tumbler)]
    assert len(rows) == 1, (
        f"expected exactly one row for {tumbler}, got {len(rows)}"
    )
    return rows[0].alias_of


# ── Link mutators event-sourced ──────────────────────────────────────────


@pytest.mark.usefixtures("local_catalog_backend")
class TestLinkEventSourced:
    """``link`` writes LinkCreated to events.jsonl under the gate.

    nexus-i711w Stage 2: DIE (5 of 6). Every test here asserts either the
    ``events.jsonl`` line count for a link mutation or a projection replayed
    from it — both local-only. The sixth
    (``test_link_merge_overwrites_via_insert_or_replace``) is a GAP; see its
    own annotation. Pinned at class level; the dying imports live in the
    bodies.
    """

    def test_link_emits_event_and_projects(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        from nexus.catalog import events as ev
        from nexus.catalog.event_log import EventLog

        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        cat, d = _local_catalog(tmp_path)
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
        """GAP nexus-i711w.1 item 5 — PINNED, not ported, not deleted.

        The link-merge UPSERT contract: a second ``link()`` on the same
        composite key must MERGE (adding the new creator to
        ``co_discovered_by``), not be dropped. ``POST /link`` is a confirmed
        UPSERT service-side, but the only existing service-side test scripts
        a ``FakeCatalogHandler`` rather than exercising a real engine, so the
        contract has no live assertion. Converting this test would move it
        onto a substrate where nothing asserts the behaviour it is about;
        deleting it loses the contract's only written record. It stays
        pinned and is the SPECIFICATION SOURCE for i711w.1.
        """
        from nexus.catalog.catalog_db import CatalogDB
        from nexus.catalog.event_log import EventLog
        from nexus.catalog.projector import Projector

        # Two link() calls on the same composite key emit two
        # LinkCreated events. Replay through the projector's
        # INSERT OR REPLACE must converge on the SECOND payload's
        # merged metadata. INSERT OR IGNORE would have silently
        # dropped the second event and the merged co_discovered_by
        # list would never reach SQLite.
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        cat, d = _local_catalog(tmp_path)
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
        from nexus.catalog import events as ev
        from nexus.catalog.event_log import EventLog

        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        cat, d = _local_catalog(tmp_path)
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
        from nexus.catalog import events as ev
        from nexus.catalog.event_log import EventLog

        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        cat, d = _local_catalog(tmp_path)
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
        from nexus.catalog import events as ev
        from nexus.catalog.event_log import EventLog

        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        cat, d = _local_catalog(tmp_path)
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
        from nexus.catalog.catalog_db import CatalogDB
        from nexus.catalog.event_log import EventLog
        from nexus.catalog.projector import Projector

        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        cat, d = _local_catalog(tmp_path)
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


@pytest.mark.usefixtures("local_catalog_backend")
class TestEnsureConsistentEventSourced:
    """The local catalog's DELETE-and-replay projection rebuild.

    nexus-aqbrk: PINNED. These open a ``Catalog`` against a deliberately
    STALE .catalog.db and assert ``_ensure_consistent`` deletes the phantom
    rows and replays ``events.jsonl``. That machinery is the LOCAL catalog's
    event-sourcing: a SQLite projection rebuilt from a JSONL log. The service
    catalog is Postgres with no event log and no projection to rebuild, so
    there is nothing on that side to test — and the writes died on the frozen-
    migration-source invariant besides ("attempt to write a readonly
    database").
    """
    def test_second_catalog_sees_events_jsonl_writes(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        # Cross-process scenario: process A writes via event-sourced;
        # process B opens the catalog and must see the same state.
        # Pre-fix B would rebuild from JSONL only and miss any
        # divergence.
        from nexus.catalog.catalog import Catalog

        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        cat_a, d = _local_catalog(tmp_path)
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
        from nexus.catalog.catalog import Catalog
        from nexus.catalog.catalog_db import CatalogDB

        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        # Step 1: write a real event-sourced state with one document.
        cat_a, d = _local_catalog(tmp_path)
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
        from nexus.catalog.catalog import Catalog

        monkeypatch.delenv("NEXUS_EVENT_SOURCED", raising=False)
        monkeypatch.delenv("NEXUS_EVENT_LOG_SHADOW", raising=False)
        cat, d = _local_catalog(tmp_path)
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
        from nexus.catalog.catalog import Catalog
        from nexus.catalog.catalog_db import CatalogDB

        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        cat_a, d = _local_catalog(tmp_path)
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


# TestDoctorReplayEqualityEventLog removed (nexus-i711w Stage 2 sub-stage
# C-store): doctor --replay-equality read events.jsonl and diffed the
# rebuilt projection against .catalog.db. Both artifacts are local-only and
# the check was deleted with them.


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
    fix).

    nexus-i711w Stage 2: 2 of the 3 RELOCATED onto the live substrate — the
    subject is ``alias_of`` threading through ``update()``, and ``update`` /
    the document read-back exist on both substrates, which is why the earlier
    "whole-file delete" reading of this file was wrong. The third
    (``test_alias_of_survives_legacy_update_through_alias``) is PORT-BLOCKED
    on a measured substrate divergence; see its own annotation."""

    @pytest.mark.usefixtures("local_catalog_backend")
    def test_alias_of_survives_legacy_update_through_alias(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        """nexus-i711w Stage 2: PORT-BLOCKED, pinned so it keeps passing.

        This one WAS attempted, and the attempt is what measured it. Its
        subject is resolve-follows-alias semantics INSIDE ``update()``: an
        update addressed to an alias must land on the CANONICAL row and
        leave the alias row's own fields alone. On the service arm the
        assertion inverts — measured as
        ``assert ('1.1.1', 99) == ('1.1.1', 0)``: the alias row itself took
        the ``chunk_count=99``.

        Settled from source, not guessed. ``HttpCatalogClient.update`` posts
        ``/update`` keyed on the tumbler verbatim
        (http_catalog_client.py:890) and ``CatalogRepository.updateDocument``
        UPDATEs ``WHERE tumbler = ?`` with no alias hop
        (CatalogRepository.java:485-501). The client's ``resolve`` also
        ignores its own ``follow_alias`` parameter (line 877-888) and
        ``resolve_alias`` degenerates to returning the input tumbler
        (line 1144-1148). So alias FOLLOWING does not exist service-side at
        all: converting this test would not port it, it would assert the
        opposite behaviour.

        Filed as a substrate divergence. The test stays here, unconverted,
        as its only written record.
        """
        from nexus.catalog.catalog import Catalog

        # PR ζ flipped default to ES; this is a legacy-path test.
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
        monkeypatch.delenv("NEXUS_EVENT_LOG_SHADOW", raising=False)
        cat, _ = _local_catalog(tmp_path)
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

    def test_explicit_alias_of_in_fields_lands(self, active_catalog):
        """A caller-supplied ``alias_of`` in ``**fields`` actually lands.

        nexus-i711w Stage 2: RELOCATED onto the live substrate. ``update``
        IS on ``CATALOG_WRITE_OPS``, so a plain ``ActiveCatalog`` routes it.

        Round-4 review (reviewer D): caller passes ``alias_of``
        explicitly. Pre-fix both event payload and legacy SQL
        VALUES read ``entry.alias_of``, silently dropping the
        caller-supplied value. Round-4 fix threads
        ``rec_dict["alias_of"]``.
        """
        cat = active_catalog
        slug = _slug()
        owner = cat.register_owner(f"alias-explicit-{slug}", "repo", repo_hash=slug)
        a = cat.register(
            owner, "a.md", content_type="prose", file_path=f"{slug}/a.md",
        )
        b = cat.register(
            owner, "b.md", content_type="prose", file_path=f"{slug}/b.md",
        )
        cat.update(a, alias_of=str(b))
        assert _alias_of(cat, a) == str(b), (
            "update(t, alias_of='X') silently dropped the value — "
            "rec_dict['alias_of'] is not threaded through"
        )

    def test_explicit_alias_of_in_es_fields_lands(self, active_catalog):
        """The same scenario, entered through the ES write path.

        nexus-i711w Stage 2: RELOCATED onto the live substrate. The gate
        distinction the test was built around (``NEXUS_EVENT_SOURCED=1`` vs
        ``0``) is local-only, and so is the replay half, so on the live
        substrate this collapses to the same statement as
        ``test_explicit_alias_of_in_fields_lands``. Kept rather than dropped
        because it is the ES-path entry the round-C follow-up added
        deliberately, and because a future service-side ``update`` that
        threads fields differently per code path would still be caught
        twice rather than once.

        RDR-101 Phase 3 follow-up C (nexus-o6aa.9.8): the legacy-path
        coverage of the round-4 ``rec_dict['alias_of']`` threading fix did
        not have an ES-mode counterpart. Without this test, a regression in
        the ES write path that silently drops caller-supplied ``alias_of``
        would not be caught.
        """
        cat = active_catalog
        slug = _slug()
        owner = cat.register_owner(f"alias-es-{slug}", "repo", repo_hash=slug)
        a = cat.register(
            owner, "a.md", content_type="prose", file_path=f"{slug}/a.md",
        )
        b = cat.register(
            owner, "b.md", content_type="prose", file_path=f"{slug}/b.md",
        )
        cat.update(a, alias_of=str(b))
        assert _alias_of(cat, a) == str(b), (
            "update(t, alias_of='X') silently dropped the value — "
            "rec_dict['alias_of'] is not threaded through to the write path"
        )


# ── Cached projector ─────────────────────────────────────────────────────


@pytest.mark.usefixtures("local_catalog_backend")
class TestProjectorCached:
    """nexus-i711w Stage 2: DIE. Asserts ``Catalog.__init__`` caches one
    ``Projector`` — both classes die together."""

    def test_catalog_caches_projector_at_init(
        self, tmp_path,
    ):
        from nexus.catalog.projector import Projector

        cat, _ = _local_catalog(tmp_path)
        # Single instance, accessible via attribute.
        proj1 = cat._projector
        proj2 = cat._projector
        assert proj1 is proj2
        assert isinstance(proj1, Projector)
