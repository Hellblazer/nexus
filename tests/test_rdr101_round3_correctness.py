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

CATALOG SUBSTRATE (nexus-i711w terminal deletion, executed): the DIE
cohort (link event emission, _ensure_consistent replay, cached
Projector) and the PORT-BLOCKED alias-following pin retired with the
local catalog — see the tombstones below, which preserve the GAP and
substrate-divergence records. What remains is the PORT cohort: the two
explicit-alias_of update() tests on the live catalog.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests._catalog_fixture_ops import ActiveCatalog


@pytest.fixture()
def active_catalog() -> ActiveCatalog:
    """Seed and read through whichever catalog is live (nexus-i711w Stage 2)."""
    return ActiveCatalog()


def _slug() -> str:
    """A per-test discriminator for owner names and file paths."""
    return uuid.uuid4().hex[:8]


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


# ── Link mutators event-sourced: RETIRED (nexus-i711w terminal deletion) ──
# TestLinkEventSourced (6 tests: LinkCreated/LinkDeleted event emission,
# replay, projection) and TestEnsureConsistentEventSourced (4 tests:
# events.jsonl rebuild, bootstrap guardrail, apply_all atomicity) died
# with the local event-sourcing machinery. GAP note preserved from the
# retired test_link_merge_overwrites_via_insert_or_replace: link() merge
# overwrite semantics (INSERT OR REPLACE on duplicate link) have no
# service-side assertion yet.


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

    # test_alias_of_survives_legacy_update_through_alias retired
    # (nexus-i711w terminal deletion). SUBSTRATE-DIVERGENCE RECORD kept
    # from its docstring: the local update() FOLLOWED aliases (an update
    # addressed to an alias landed on the canonical row); the service
    # update posts /update keyed on the tumbler verbatim with no alias
    # hop, HttpCatalogClient.resolve ignores follow_alias, and
    # resolve_alias degenerates to identity — alias following does not
    # exist service-side. Measured, not guessed (the port attempt
    # inverted the assertion). The divergence is now behaviour-of-record.

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


# TestProjectorCached retired (nexus-i711w terminal deletion): the cached
# Projector was an implementation detail of the deleted local Catalog.
