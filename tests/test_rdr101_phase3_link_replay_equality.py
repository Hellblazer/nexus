# SPDX-License-Identifier: AGPL-3.0-or-later

"""RDR-101 Phase 3 PR γ — link/unlink merge semantics deep-clean.

Round 3 (PR #432/#433) made ``link``/``link_if_absent``/``unlink``/
``bulk_unlink`` event-source their LinkCreated/LinkDeleted events under
``NEXUS_EVENT_SOURCED=1`` and confirmed the basic replay path. This file
hardens the contract against the multi-mutation sequences that the
irreversibility cutover depends on:

1. Long mutation sequences (create → merge×3 → unlink → recreate → merge
   → bulk_unlink → recreate) replay equal to direct mutation.
2. ``bulk_unlink`` interleaved with ``rename_collection`` (a write that
   re-emits ``DocumentRegistered`` for every renamed document) replays
   equal — links carry tumbler strings, so a collection rename must NOT
   touch link rows on either path.
3. The links table's autoincrement ``id`` PK is reassigned on every
   ``INSERT OR REPLACE``. The doctor's snapshot already excludes ``id``
   by name (this file's own ``_snapshot_table`` helper mirrors what the
   doctor's deleted ``_run_replay_equality`` used to exclude); this verifies
   that a merge sequence which churns the ``id`` does not break replay-
   equality.
4. Under the gate, link tombstones never double-emit. The legacy path
   shadow-emits ``LinkDeleted`` after the SQLite commit, but the event-
   sourced path emits + projects per-row inside the loop and skips the
   trailing shadow-emit block. A regression that re-enabled the shadow
   block would put a duplicate ``LinkDeleted`` in events.jsonl.

Each test is paired with a "with-teeth" comment naming the production
invariant it would catch if regressed (per the round-3+4 review
finding that several tests passed for the wrong reason).

CATALOG SUBSTRATE (nexus-i711w Stage 2). Measured disposition: 3 PORT,
5 DIE.

  - DIE (5): every test whose assertion IS ``_assert_replay_equal`` —
    a live ``.catalog.db`` diffed against a projection rebuilt from
    ``events.jsonl`` — plus the two tombstone double-emit counts. Both
    artifacts are local-only; the service catalog is Postgres with no event
    log and no projection to rebuild. They retire with
    ``nexus/catalog/{catalog,catalog_db,event_log,projector,events}.py`` and
    each carries ``local_catalog_backend`` so the pin is EXPLICIT.
  - PORT (3): the LINK invariants that are not about replay at all —
    ``unlink`` -> recreate must not inherit the old ``co_discovered_by``,
    and ``rename_collection`` must not touch link rows in either order.
    ``HttpCatalogClient`` implements ``link`` / ``unlink`` / ``bulk_unlink``
    / ``links_from`` / ``rename_collection``, so those three seed and read
    through :class:`tests._catalog_fixture_ops.ActiveCatalog`. Their
    trailing ``_assert_replay_equal`` call is dropped (it is the DIE half);
    the pre/post-rename link-snapshot assertion — which the round-4 review
    added precisely because replay-equality alone could not catch a
    symmetric mutation — is what carries over, now expressed through
    ``links_from`` instead of a raw ``PRAGMA``-driven table snapshot.

All five dying modules are imported INSIDE the bodies that still need them,
never at module scope, so this file still COLLECTS after the deletion.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest

from tests._catalog_fixture_ops import ActiveCatalog


# ─── helpers ──────────────────────────────────────────────────────────────


@pytest.fixture()
def active_catalog() -> ActiveCatalog:
    """Seed and read through whichever catalog is live (nexus-i711w Stage 2)."""
    return ActiveCatalog()


def _slug() -> str:
    """A per-test discriminator for owner names / paths / collections."""
    return uuid.uuid4().hex[:8]


def _local_catalog(tmp_path) -> tuple[Any, Path]:
    """A real LOCAL SQLite ``Catalog`` rooted in *tmp_path*, plus its dir.

    ONLY for the DIE cohort; callers also carry ``local_catalog_backend``.
    """
    from nexus.catalog.catalog import Catalog

    cat_dir = tmp_path / "catalog"
    cat_dir.mkdir()
    return Catalog(cat_dir, cat_dir / ".catalog.db"), cat_dir


def _link_snapshot(cat: Any, tumblers: list[Any]) -> list[tuple]:
    """Order-independent snapshot of every OUTBOUND link of *tumblers*.

    The substrate-neutral replacement for ``_snapshot_table(conn, "links",
    exclude=("id",))`` in the three ported tests: it carries every field
    ``CatalogLink`` exposes (endpoints, type, spans, creator, stamp, and the
    merged metadata) and therefore keeps the pre/post-rename equality
    assertion as strong as the raw-table form — including the
    ``co_discovered_by`` list, whose loss on rename is one of the failures
    the assertion exists to see. ``id`` is absent by construction rather
    than excluded by name, so the doctor's ``LINKS_EXCLUDE`` coupling that
    ``TestLinkIdReassignmentSafe`` pins is NOT silently re-created here.
    """
    rows: list[tuple] = []
    for t in tumblers:
        for lnk in cat.links_from(t):
            rows.append((
                str(lnk.from_tumbler), str(lnk.to_tumbler), lnk.link_type,
                lnk.from_span, lnk.to_span, lnk.created_by, lnk.created_at,
                json.dumps(lnk.meta or {}, sort_keys=True),
            ))
    return sorted(rows)


# Mirror the doctor's links-snapshot exclusion (commands/catalog.py:
# LINKS_EXCLUDE = ["id"]). Keep these in sync — if the doctor's contract
# ever stops excluding id, this constant must follow.
_LINKS_EXCLUDE = ("id",)


def _snapshot_table(
    conn: sqlite3.Connection,
    table: str,
    *,
    exclude: tuple[str, ...] = (),
) -> list[tuple]:
    """Order-independent snapshot matching the doctor's verb."""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    cols = [c for c in cols if c not in set(exclude)]
    if not cols:
        return []
    sort_cols = ", ".join(cols)
    return list(
        conn.execute(f"SELECT {sort_cols} FROM {table} ORDER BY {sort_cols}")
    )


def _project_events_to(events_iter, db_path: Path) -> int:
    """Replay ``events_iter`` into a fresh CatalogDB at ``db_path``.

    DIE-cohort helper (nexus-i711w Stage 2) — retires with the projector.
    """
    from nexus.catalog.catalog_db import CatalogDB
    from nexus.catalog.projector import Projector

    proj_db = CatalogDB(db_path)
    try:
        return Projector(proj_db).apply_all(events_iter)
    finally:
        proj_db.close()


def _assert_replay_equal(cat_dir: Path, live_db: Path, projected_path: Path) -> None:
    """Diff live SQLite vs replayed SQLite; exclude ``links.id``.

    Surfaces a projector failure as an early ``AssertionError`` rather
    than letting ``_project_events_to``'s ``finally`` close a partial
    projection that the snapshot diff would silently compare against
    live state. Counts are sanity-checked: zero events applied for a
    test that mutated the catalog is a regression.

    DIE-cohort helper (nexus-i711w Stage 2): a live ``.catalog.db`` diffed
    against a projection rebuilt from ``events.jsonl``; neither artifact has
    a service-mode counterpart, so the import stays inside the body and the
    helper retires with its callers.
    """
    from nexus.catalog.event_log import EventLog

    applied = _project_events_to(EventLog(cat_dir).replay(), projected_path)
    assert applied > 0, (
        "projector applied 0 events — replay-equality cannot be "
        "established against an empty projection. Either the test "
        "did not mutate the catalog, or the event log is empty."
    )
    with sqlite3.connect(f"file:{live_db}?mode=ro", uri=True) as live:
        live_links = _snapshot_table(live, "links", exclude=_LINKS_EXCLUDE)
        live_docs = _snapshot_table(live, "documents")
        live_owners = _snapshot_table(live, "owners")
    with sqlite3.connect(str(projected_path)) as proj:
        proj_links = _snapshot_table(proj, "links", exclude=_LINKS_EXCLUDE)
        proj_docs = _snapshot_table(proj, "documents")
        proj_owners = _snapshot_table(proj, "owners")
    assert live_owners == proj_owners, (
        f"owners diverged: live={live_owners}, projected={proj_owners}"
    )
    assert live_docs == proj_docs, (
        f"documents diverged: live={live_docs}, projected={proj_docs}"
    )
    assert live_links == proj_links, (
        f"links diverged: live={live_links}, projected={proj_links}"
    )


# ─── A. Long mutation sequence ────────────────────────────────────────────


class TestLongLinkMutationSequence:
    """create → merge×3 → unlink → recreate → merge → bulk_unlink → recreate."""

    @pytest.mark.usefixtures("local_catalog_backend")
    def test_full_sequence_replays_equal(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        # WITH TEETH (structural): catches a regression where DELETE
        # events stop landing in SQLite (live keeps the row, replay
        # drops it) or where the writer fails to emit a LinkCreated
        # for the recreate-after-unlink path (live recreates, replay
        # has nothing to recreate).
        # NOT teeth for INSERT OR IGNORE — the writer's synchronous
        # ``_projector.apply()`` would make live and replay agree
        # symmetrically. Coverage for that regression lives in
        # ``test_link_merge_overwrites_via_insert_or_replace`` (round-3)
        # and ``TestLinkIdReassignmentSafe`` below.
        # nexus-i711w Stage 2: DIE — the assertion IS ``_assert_replay_equal``.
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        cat, cat_dir = _local_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        a = cat.register(owner, "a.md", content_type="prose")
        b = cat.register(owner, "b.md", content_type="prose")

        # Phase 1: create + 3 merges.
        assert cat.link(a, b, "cites", "agent-1") is True
        assert cat.link(a, b, "cites", "agent-2") is False
        assert cat.link(a, b, "cites", "agent-3") is False
        assert cat.link(a, b, "cites", "agent-4") is False

        # Phase 2: unlink (drops the row), then recreate.
        assert cat.unlink(a, b, "cites") == 1
        assert cat.link(a, b, "cites", "agent-5") is True
        assert cat.link(a, b, "cites", "agent-6") is False

        # Phase 3: bulk_unlink, then recreate one more time.
        assert cat.bulk_unlink(from_t=str(a), to_t=str(b), link_type="cites") == 1
        assert cat.link(a, b, "cites", "agent-7") is True
        cat._db.close()

        _assert_replay_equal(
            cat_dir, cat_dir / ".catalog.db", tmp_path / "projected.db",
        )

    def test_merge_after_unlink_does_not_carry_old_co_discovered_by(
        self, active_catalog,
    ):
        """unlink -> recreate is a FRESH link, not a resurrected one.

        nexus-i711w Stage 2: PORTED. This was always the test in this file
        whose subject was NOT replay — the round-3 review added it precisely
        because a tombstone-instead-of-DELETE regression would keep
        replay-equality passing while breaking the semantics. That makes it a
        genuine link invariant, and one both substrates can express.

        WITH TEETH: if a future change made unlink leave a tombstone
        row instead of DELETEing, the recreated link would inherit
        the old ``co_discovered_by``.

        Read through ``links_from`` rather than the projected snapshot: the
        link's identity here is (from, to, type), and reading the LIVE
        catalog is what the invariant is actually about.
        """
        cat = active_catalog
        slug = _slug()
        owner = cat.register_owner(f"link-fresh-{slug}", "repo", repo_hash=slug)
        a = cat.register(
            owner, "a.md", content_type="prose", file_path=f"{slug}/a.md",
        )
        b = cat.register(
            owner, "b.md", content_type="prose", file_path=f"{slug}/b.md",
        )

        cat.link(a, b, "cites", "old-1")
        cat.link(a, b, "cites", "old-2")  # merge — adds old-2 to co
        assert cat.unlink(a, b, "cites") == 1
        cat.link(a, b, "cites", "fresh")  # fresh creator

        rows = [
            lnk for lnk in cat.links_from(a, "cites")
            if str(lnk.to_tumbler) == str(b)
        ]
        assert len(rows) == 1, (
            f"expected exactly one a->b cites link after unlink -> recreate, "
            f"got {len(rows)}"
        )
        assert rows[0].created_by == "fresh"
        # Fresh creation has no co_discovered_by yet (single creator).
        # The previous old-1/old-2 list must NOT survive the unlink.
        co = (rows[0].meta or {}).get("co_discovered_by", [])
        assert "old-1" not in co
        assert "old-2" not in co


# ─── B. bulk_unlink interleaved with rename_collection ────────────────────


class TestBulkUnlinkRenameInterleaving:
    """rename_collection re-emits DocumentRegistered; links carry tumblers,
    not collection names, so the link snapshot is unaffected by the rename.
    Verify replay-equality holds for an interleaved sequence.
    """

    def test_rename_then_bulk_unlink_leaves_links_untouched(
        self, active_catalog,
    ):
        """``rename_collection`` must not touch link rows; ``bulk_unlink``
        still keys on tumblers afterwards.

        nexus-i711w Stage 2: PORTED, and renamed to say what it now asserts.
        The round-4 review added the pre/post-rename snapshot equality here
        precisely because replay-equality alone could not catch a symmetric
        live+projection mutation — that assertion is the part that is about
        the LINK invariant rather than the event log, and it survives.

        WITH TEETH: if rename_collection ever cascaded into the links
        table (e.g. rewrote ``from_tumbler`` to encode the new
        collection), the pre/post snapshots diverge.
        """
        cat = active_catalog
        slug = _slug()
        old_coll, new_coll = f"old_coll_{slug}", f"new_coll_{slug}"
        owner = cat.register_owner(f"link-rename-{slug}", "repo", repo_hash=slug)
        # Service precondition, measured not assumed: POST /collections/rename
        # 404s unless the collection has a catalog_collections row, whereas the
        # local verb renames by scanning documents.physical_collection.
        cat.register_collection(old_coll, content_type="prose")
        a = cat.register(
            owner, "a.md", content_type="prose", file_path=f"{slug}/a.md",
            physical_collection=old_coll,
        )
        b = cat.register(
            owner, "b.md", content_type="prose", file_path=f"{slug}/b.md",
            physical_collection=old_coll,
        )
        c = cat.register(
            owner, "c.md", content_type="prose", file_path=f"{slug}/c.md",
            physical_collection=old_coll,
        )
        cat.link(a, b, "cites", "agent-1")
        cat.link(a, c, "cites", "agent-1")
        cat.link(b, c, "cites", "agent-2")

        # Snapshot links BEFORE the rename to assert rename does not
        # touch link rows.
        links_before = _link_snapshot(cat, [a, b, c])
        assert len(links_before) == 3, (
            f"seed did not land: expected 3 links, snapshot has "
            f"{len(links_before)}"
        )
        renamed = cat.rename_collection(old_coll, new_coll)
        assert renamed == 3
        links_after = _link_snapshot(cat, [a, b, c])
        assert links_before == links_after, (
            "rename_collection mutated link rows — "
            "tumbler-keyed link identity must be invariant under "
            "collection rename"
        )

        # bulk_unlink AFTER the rename — by from_tumbler still works
        # because tumblers are independent of collection.
        n = cat.bulk_unlink(from_t=str(a), link_type="cites")
        assert n == 2  # a→b, a→c
        assert _link_snapshot(cat, [a]) == []

        # Recreate one of them — should be a fresh link in new state.
        assert cat.link(a, b, "cites", "agent-3") is True

    def test_bulk_unlink_then_rename_does_not_resurrect_links(
        self, active_catalog,
    ):
        """Same invariant in the opposite order: a rename after a
        ``bulk_unlink`` must not resurrect the deleted rows.

        nexus-i711w Stage 2: PORTED, and renamed to say what it now asserts.

        WITH TEETH: catches a regression where rename_collection writes
        to the links table would clobber the deletes that already landed.
        """
        cat = active_catalog
        slug = _slug()
        old_coll, new_coll = f"old_coll_{slug}", f"new_coll_{slug}"
        owner = cat.register_owner(f"link-unlink-{slug}", "repo", repo_hash=slug)
        # Service precondition — see the sibling test above.
        cat.register_collection(old_coll, content_type="prose")
        a = cat.register(
            owner, "a.md", content_type="prose", file_path=f"{slug}/a.md",
            physical_collection=old_coll,
        )
        b = cat.register(
            owner, "b.md", content_type="prose", file_path=f"{slug}/b.md",
            physical_collection=old_coll,
        )
        assert cat.link(a, b, "cites", "agent-1") is True
        assert cat.link(a, b, "cites", "agent-2") is False  # merge

        n = cat.bulk_unlink(from_t=str(a), link_type="cites")
        assert n == 1

        # Snapshot AFTER bulk_unlink (no links) and confirm rename does
        # not resurrect the deleted row.
        links_pre_rename = _link_snapshot(cat, [a, b])
        assert links_pre_rename == [], (
            f"bulk_unlink left links behind: {links_pre_rename}"
        )
        renamed = cat.rename_collection(old_coll, new_coll)
        assert renamed == 2
        links_post_rename = _link_snapshot(cat, [a, b])
        assert links_pre_rename == links_post_rename, (
            "rename_collection resurrected or mutated link rows; "
            "tumbler-keyed link identity must be invariant under "
            "collection rename"
        )


# ─── C. id reassignment-on-REPLACE doesn't break replay-equality ──────────


class TestLinkIdReassignmentSafe:
    """The projector's INSERT OR REPLACE deletes-and-re-inserts on the
    composite-key UNIQUE INDEX, which assigns a new ``id`` integer.
    The doctor excludes ``id`` from its snapshot
    (``commands/catalog.py::LINKS_EXCLUDE``); a merge churn must therefore
    leave the doctor's diff at zero.
    """

    @pytest.mark.usefixtures("local_catalog_backend")
    def test_id_churn_under_merge_is_invisible_to_replay_equality(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        """nexus-i711w Stage 2: DIE. Its subject is the SQLite ``links.id``
        autoincrement PK and the doctor's ``LINKS_EXCLUDE`` snapshot
        contract — neither exists service-side."""
        # WITH TEETH: if anything in the doctor stops excluding id by
        # name (or excludes by position and a column gets reordered),
        # this test fails because the live row's id (e.g. 4 after
        # 3 merges) will not match the replay's id (1, since replay
        # has only one event landing in a fresh DB).
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        cat, cat_dir = _local_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        a = cat.register(owner, "a.md", content_type="prose")
        b = cat.register(owner, "b.md", content_type="prose")

        cat.link(a, b, "cites", "agent-1")
        live_id_1 = cat._db.execute(
            "SELECT id FROM links WHERE from_tumbler=? AND to_tumbler=?",
            (str(a), str(b)),
        ).fetchone()[0]
        cat.link(a, b, "cites", "agent-2")  # merge — REPLACE bumps id
        cat.link(a, b, "cites", "agent-3")  # merge — REPLACE bumps id
        live_id_3 = cat._db.execute(
            "SELECT id FROM links WHERE from_tumbler=? AND to_tumbler=?",
            (str(a), str(b)),
        ).fetchone()[0]
        # Sanity: the id MUST have changed across the merge sequence
        # (else this test is not exercising the invariant it claims to).
        assert live_id_3 != live_id_1, (
            "id did not change across merge — projector path may have "
            "been bypassed; re-check NEXUS_EVENT_SOURCED gate"
        )
        cat._db.close()

        _assert_replay_equal(
            cat_dir, cat_dir / ".catalog.db", tmp_path / "projected.db",
        )


# ─── D. Tombstone shadow-emit gated under event-sourced ───────────────────


@pytest.mark.usefixtures("local_catalog_backend")
class TestLinkTombstoneNoDoubleEmit:
    """nexus-i711w Stage 2: DIE. Both tests count ``LinkDeleted`` lines in
    ``events.jsonl`` under the two shadow-emit gates; there is no service-mode
    observable for either the log or the gates.

    Both ``unlink`` and ``bulk_unlink`` write the LinkDeleted event
    inline (event-sourced path) AND have a trailing shadow-emit block
    for the legacy path. Defense-in-depth: TWO gates prevent double-
    emit under ES — an outer ``not self._event_sourced_enabled`` at
    the call site, and an inner ``if self._event_sourced_enabled:
    return`` in ``_emit_shadow_event``. The contract: under any
    combination of NEXUS_EVENT_SOURCED and NEXUS_EVENT_LOG_SHADOW,
    exactly one LinkDeleted lands per removed row.

    Coverage scope: removing the OUTER call-site gate alone is caught
    by the inner ``_emit_shadow_event`` early-return (no duplicate).
    Removing the INNER gate alone is caught by the outer call-site
    skip (no duplicate). Removing BOTH simultaneously is what these
    tests catch — the failure mode is "duplicate LinkDeleted per
    row," easily observed by counting events. To exercise the gates
    independently, the inner-gate test would need to monkeypatch
    ``_emit_shadow_event`` to bypass its own gate; the value of that
    extra coverage is low (the gates are 4 lines apart in the same
    file, both removed-together is the realistic regression).
    """

    def test_unlink_emits_exactly_one_tombstone(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        # WITH TEETH: set BOTH gates so the realistic transition-window
        # state (operator turning ES on while leaving SHADOW=1) is
        # exercised. If BOTH defense-in-depth gates are removed, this
        # assertion fails (count becomes 2). Single-gate removal is
        # caught by the surviving gate — see class docstring.
        from nexus.catalog import events as ev
        from nexus.catalog.event_log import EventLog

        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        monkeypatch.setenv("NEXUS_EVENT_LOG_SHADOW", "1")
        cat, cat_dir = _local_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        a = cat.register(owner, "a.md", content_type="prose")
        b = cat.register(owner, "b.md", content_type="prose")
        cat.link(a, b, "cites", "agent-1")
        cat.unlink(a, b, "cites")

        log = EventLog(cat_dir)
        deletes = [e for e in log.replay() if e.type == ev.TYPE_LINK_DELETED]
        assert len(deletes) == 1, (
            f"expected 1 LinkDeleted under ES+SHADOW, found {len(deletes)} "
            "— outer ``not self._event_sourced_enabled`` gate may have "
            "been removed"
        )

    def test_bulk_unlink_emits_one_tombstone_per_row(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        # WITH TEETH: same invariant on bulk_unlink. Under ES+SHADOW
        # we expect exactly 3 events (one per row). Doubling implies
        # BOTH defense-in-depth gates were removed simultaneously
        # (single-gate removal is caught by the surviving gate; see
        # class docstring).
        from nexus.catalog import events as ev
        from nexus.catalog.event_log import EventLog

        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        monkeypatch.setenv("NEXUS_EVENT_LOG_SHADOW", "1")
        cat, cat_dir = _local_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        a = cat.register(owner, "a.md", content_type="prose")
        b = cat.register(owner, "b.md", content_type="prose")
        c = cat.register(owner, "c.md", content_type="prose")
        d = cat.register(owner, "d.md", content_type="prose")
        cat.link(a, b, "cites", "agent-1")
        cat.link(a, c, "cites", "agent-1")
        cat.link(a, d, "cites", "agent-1")

        n = cat.bulk_unlink(from_t=str(a), link_type="cites")
        assert n == 3

        log = EventLog(cat_dir)
        deletes = [e for e in log.replay() if e.type == ev.TYPE_LINK_DELETED]
        assert len(deletes) == 3, (
            f"expected 3 LinkDeleted (one per row) under ES+SHADOW, "
            f"found {len(deletes)}"
        )


# ─── E. Cross-mutation event ordering preserves replay equality ───────────


class TestEventOrderingPreservesReplayEquality:
    """Mixed mutation sequences (link + register + rename + unlink) all
    appended to one events.jsonl must replay deterministically. The
    flock serializes mutations on the writer side, so events are
    appended in a single linearization — replay-equality is the binding
    contract that a future writer (e.g. transactional batch) preserves
    that linearization.
    """

    @pytest.mark.usefixtures("local_catalog_backend")
    def test_mixed_event_stream_replays_equal(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        """nexus-i711w Stage 2: DIE — the assertion IS
        ``_assert_replay_equal``."""
        # WITH TEETH: any future change that splits the writer into
        # parallel paths without preserving append order would diverge
        # the replay.
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
        cat, cat_dir = _local_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        a = cat.register(
            owner, "a.md", content_type="prose", physical_collection="c1",
        )
        b = cat.register(
            owner, "b.md", content_type="prose", physical_collection="c1",
        )
        cat.link(a, b, "cites", "agent-1")
        c = cat.register(
            owner, "c.md", content_type="prose", physical_collection="c1",
        )
        cat.link(b, c, "cites", "agent-2")
        cat.link(a, b, "cites", "agent-3")  # merge into existing
        cat.rename_collection("c1", "c2")
        cat.unlink(a, b, "cites")
        cat.link(c, a, "cites", "agent-4")
        cat._db.close()

        _assert_replay_equal(
            cat_dir, cat_dir / ".catalog.db", tmp_path / "projected.db",
        )
