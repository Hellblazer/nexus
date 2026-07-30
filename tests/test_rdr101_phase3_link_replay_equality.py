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

nexus-i711w terminal deletion: the DIE cohort (the 5 replay-equality /
tombstone-count tests) and its helpers retired WITH the local event log,
projector, and CatalogDB. The 3 ported link-invariant tests remain.
"""

from __future__ import annotations

import json
import uuid
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


# ─── A. Long mutation sequence ────────────────────────────────────────────


class TestLongLinkMutationSequence:
    """create → merge×3 → unlink → recreate → merge → bulk_unlink → recreate."""

    # nexus-i711w terminal deletion: test_full_sequence_replays_equal
    # retired — its assertion WAS the replay-equality diff.

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


# nexus-i711w terminal deletion: TestLinkIdReassignmentSafe (links.id
# churn vs the doctor LINKS_EXCLUDE snapshot), TestLinkTombstoneNoDoubleEmit
# (LinkDeleted counts in events.jsonl under the shadow-emit gates), and
# TestEventOrderingPreservesReplayEquality retired WITH the local event
# log/projector — none has a service-mode observable.
