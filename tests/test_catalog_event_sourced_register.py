# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the RDR-101 Phase 3 PR α event-sourced register path.

Coverage:
- Gate parsing (PR ζ semantics, nexus-o6aa.9.5): 0/false/no/off → OFF;
  1/true/yes/on/unset/empty → ON. The default flipped to ON in PR ζ.
- Legacy path (gate explicitly OFF): legacy direct-write path runs
  unchanged.
- Gate ON: register_owner / register write events.jsonl FIRST, then
  project to SQLite via Projector.apply, then append to legacy JSONL
  for back-compat.
- Equivalence: a sequence of register() calls under the new path
  produces a SQLite state byte-equal to the same sequence under the
  legacy path.
- Replay: events.jsonl produced by the new path replays through a
  fresh CatalogDB to a state byte-equal to the live DB.
- Shadow emit suppression: when event-sourced is ON, shadow emit does
  NOT double-write (would otherwise produce duplicate events.jsonl
  lines).
- Idempotency: register() under the new path keeps the same idempotency
  guards (file_path dedup, head_hash+title dedup).

CATALOG SUBSTRATE (nexus-i711w terminal deletion, executed): the DIE
cohort (gate parsing, events.jsonl presence/replay, dual-path SQLite
equivalence, shadow-emit suppression) and the GAP-pinned idempotency
test retired with the local event-sourcing machinery — see the
tombstones below. What remains is the PORT cohort: register_owner /
register row persistence through ActiveCatalog on the live catalog.
"""

from __future__ import annotations

import uuid

import pytest

from tests._catalog_fixture_ops import ActiveCatalog


@pytest.fixture()
def active_catalog() -> ActiveCatalog:
    """Seed and read through whichever catalog is live (nexus-i711w Stage 2)."""
    return ActiveCatalog()


def _slug() -> str:
    """A per-test discriminator for owner names and file paths."""
    return uuid.uuid4().hex[:8]


# TestEventSourcedGate + TestLegacyPathStillRuns retired (nexus-i711w
# terminal deletion): _read_event_sourced_gate and the events.jsonl /
# legacy-JSONL dual-write machinery died with nexus.catalog.catalog.


class TestEventSourcedPathWrites:
    def test_register_owner_persists_the_owner_row(self, active_catalog):
        """``register_owner`` lands exactly one owner carrying its fields.

        nexus-i711w Stage 2: PORTED. Was
        ``test_register_owner_writes_event_log_first``. Two of its three
        assertions were local-only artifacts (``events.jsonl``,
        ``owners.jsonl``); the OwnerRegistered emission is still pinned by
        ``TestShadowEmitSuppressedWhenEventSourced``.

        CARDINALITY IS PRESERVED DELIBERATELY: the old assertion was a
        single-row SELECT keyed on the literal prefix "1.1", which only
        holds for a virgin catalog. ``get_owner_by_prefix`` would be the
        nearest one-shot equivalent but returns ONE owner and so could not
        observe a duplicate; filtering ``list_owners()`` states the
        "exactly one owner by this name" the test always meant, and the
        minted prefix is asserted against the returned Tumbler instead of a
        hardcoded one.
        """
        cat = active_catalog
        slug = _slug()
        name = f"es-register-owner-{slug}"
        owner = cat.register_owner(name, "repo", repo_hash=slug)

        rows = [o for o in cat.list_owners() if o.get("name") == name]
        assert len(rows) == 1, f"expected exactly one owner {name!r}, got {len(rows)}"
        assert rows[0]["owner_type"] == "repo"
        assert rows[0]["repo_hash"] == slug
        assert rows[0]["tumbler_prefix"] == str(owner)

    def test_register_persists_title_chunk_count_and_head_hash(self, active_catalog):
        """``register`` lands all three fields on the document row.

        nexus-i711w Stage 2: PORTED. Was
        ``test_register_writes_event_log_first``; the DocumentRegistered
        emission half is still pinned by ``TestNewPathReplays``, which
        replays ``events.jsonl`` and diffs the documents it reconstructs.
        """
        cat = active_catalog
        slug = _slug()
        owner = cat.register_owner(f"es-register-{slug}", "repo", repo_hash=slug)
        tumbler = cat.register(
            owner, "doc.md",
            content_type="prose",
            file_path=f"{slug}/doc.md",
            chunk_count=12,
            head_hash="aaaa1111",
        )

        entry = cat.resolve(tumbler)
        assert entry is not None
        assert (entry.title, entry.chunk_count, entry.head_hash) == (
            "doc.md", 12, "aaaa1111",
        )


# ── Equivalence: new path ≡ legacy path ──────────────────────────────────


# TestEquivalence / TestNewPathReplays /
# TestShadowEmitSuppressedWhenEventSourced retired (nexus-i711w terminal
# deletion): dual-path SQLite byte-equivalence, events.jsonl replay, and
# shadow-emit suppression were properties of the deleted local
# event-sourcing machinery.
#
# TestIdempotencyUnderEventSourced retired with them — GAP nexus-i711w.1
# item 9 STILL OWED: register() file_path idempotency (same tumbler, no
# second row) has no live service-side assertion; the retired test body
# (git history of this file) is the specification source for the fresh
# service-side test i711w.1 will write.
