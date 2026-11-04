# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-gie46 (RDR-147 Phase 1 Step 5): pin how catalog link deletion behaves TODAY.

RDR-147 withdrew the June plan to wire link removal into deletion, because the catalog already
cascades. These tests pin what the engine does now, against a REAL engine, so a later change
cannot alter it unnoticed:

1. a hard delete (``purge_trash`` past the grace window) removes the document's links in both
   directions (``catalog-032-links-tumbler-fk.xml``, ``ON DELETE CASCADE``);
2. a delete to trash keeps the links, and ``restore_document`` returns the document with them;
3. ``traverse`` from a live document never reaches a trashed endpoint, and a trashed document
   is no relay to what lies behind it (``graphBFS`` joins both endpoints to live documents);
4. ``links_from`` / ``links_to`` DO list a link whose other end is trashed. This is today's
   behaviour, pinned as-is, not a contract: whether they should filter it is RDR-147 Step 2's
   call (nexus-81183), and only if the linker ships. Changing it should be a decision, so this
   pin fails and gets rewritten with the decision.
5. ``find_by_title_exact`` never returns a trashed document.

Every pin carries its positive control (the thing it asserts is absent was present first), and
each was shown red with its assertion inverted; the cascade pin also with the two FKs dropped.
"""
from __future__ import annotations

import itertools
from contextlib import contextmanager

import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient
from tests._chunk_seed import _lit, _pg_state, _psql_superuser, ambient_tenant

# Not integration-marked: the substrate provisions itself, and CI's default selection must run
# these pins.

_LINK_TYPE = "relates"  # in the BFS default set, so the traverse pin is not vacuous
_CREATED_BY = "rdr147-pin"
_counter = itertools.count()


@pytest.fixture
def cat(t2_service_env):
    with HttpCatalogClient() as client:
        yield client


class _Graph:
    """A few live documents under one owner, with a unique title prefix per test."""

    def __init__(self, cat: HttpCatalogClient) -> None:
        self.cat = cat
        self.tag = f"rdr147-pin-{next(_counter)}"
        self.owner = cat.register_owner(
            f"{self.tag}-owner", "repo", repo_hash=f"{self.tag}-hash",
        )

    def doc(self, name: str) -> str:
        title = f"{self.tag}-{name}"
        t = self.cat.register(
            self.owner, title, content_type="knowledge",
            physical_collection="knowledge__rdr147pins",
        )
        return str(t)

    def title(self, name: str) -> str:
        return f"{self.tag}-{name}"

    def link(self, a: str, b: str) -> None:
        self.cat.link(a, b, _LINK_TYPE, _CREATED_BY)


def _pair(link) -> tuple[str, str, str]:
    return (str(link.from_tumbler), str(link.to_tumbler), link.link_type)


def _out(cat, t: str) -> set[tuple[str, str, str]]:
    return {_pair(lk) for lk in cat.links_from(t)}


def _in(cat, t: str) -> set[tuple[str, str, str]]:
    return {_pair(lk) for lk in cat.links_to(t)}


def _rows(tumbler: str) -> set[tuple[str, str, str]]:
    """The link rows touching *tumbler*, read from ``nexus.catalog_links`` itself (tenant-scoped).

    Pins 1 and 2 observe rows here rather than through ``links_from`` / ``links_to``: those readers
    are what pin 4 says may change (RDR-147 R8), and a filtering reader would hide a dangling row
    from pin 1 or hide a kept row from pin 2."""
    out = _psql_superuser(
        _pg_state(),
        "SELECT from_tumbler || '|' || to_tumbler || '|' || link_type FROM nexus.catalog_links "
        f"WHERE tenant_id = {_lit(ambient_tenant())} "
        f"AND (from_tumbler = {_lit(tumbler)} OR to_tumbler = {_lit(tumbler)})",
    )
    return {tuple(line.split("|")) for line in out.splitlines() if line}  # type: ignore[misc]


def _age_tombstone(tumbler: str, days: int = 3) -> None:
    """Backdate a tombstone past the 1 day grace window (the engine refuses ``older_than_days < 1``,
    so a fresh tombstone is never purgeable). Substrate SQL, as ``tests/_reapable_age.py`` does for
    chunks; raises when it aged nothing, so a purge after it is never silently a no-op."""
    out = _psql_superuser(
        _pg_state(),
        f"WITH u AS (UPDATE nexus.catalog_documents SET deleted_at = now() - interval '{int(days)} days' "
        f"WHERE tenant_id = {_lit(ambient_tenant())} AND tumbler = {_lit(tumbler)} "
        "AND deleted_at IS NOT NULL RETURNING 1) SELECT count(*) FROM u",
    )
    assert int(out) == 1, f"no tombstone to age for {tumbler}"


# Set while the link FKs are dropped; the module's autouse teardown restores them if the
# context manager's own restore did not run or failed, so a dropped FK never outlives its test.
_FK_RESTORE_PENDING: list[str | None] = [None]


def _restore_link_fks(tenant: str) -> None:
    """Delete this tenant's dangling link rows, then re-add each link FK that is missing, exactly
    as ``catalog-032-links-tumbler-fk.xml`` defines it (plain, validated, CASCADE / CASCADE).
    Idempotent: a constraint already present is left alone. PG and its DDL are per xdist worker
    (``ensure_engine`` is process-memoized), so no other worker sees the window."""
    t = _lit(tenant)
    adds = []
    for name, col in (("fk_catalog_links_from_document", "from_tumbler"),
                      ("fk_catalog_links_to_document", "to_tumbler")):
        adds.append(
            f"IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}') THEN "
            f"ALTER TABLE nexus.catalog_links ADD CONSTRAINT {name} "
            f"FOREIGN KEY (tenant_id, {col}) REFERENCES nexus.catalog_documents (tenant_id, tumbler) "
            "ON DELETE CASCADE ON UPDATE CASCADE; END IF;"
        )
    _psql_superuser(
        _pg_state(),
        f"DELETE FROM nexus.catalog_links l WHERE l.tenant_id = {t} AND (NOT EXISTS "
        "(SELECT 1 FROM nexus.catalog_documents d WHERE d.tenant_id = l.tenant_id AND d.tumbler = l.from_tumbler) "
        "OR NOT EXISTS (SELECT 1 FROM nexus.catalog_documents d "
        "WHERE d.tenant_id = l.tenant_id AND d.tumbler = l.to_tumbler)); "
        "DO $$ BEGIN " + " ".join(adds) + " END $$;",
    )


@pytest.fixture(autouse=True)
def _link_fk_backstop():
    yield
    tenant = _FK_RESTORE_PENDING[0]
    if tenant is not None:
        _restore_link_fks(tenant)
        _FK_RESTORE_PENDING[0] = None


@contextmanager
def _link_fks_dropped():
    """Vacuity control for the cascade pin: with the two link FKs gone, a hard delete must leave
    the links behind, proving the cascade (and nothing else) is what removes them. Restored on
    exit (this tenant's dangling rows deleted, the constraints re-added valid), and again by the
    module's autouse teardown if that restore did not complete."""
    tenant = ambient_tenant()
    _FK_RESTORE_PENDING[0] = tenant
    _psql_superuser(
        _pg_state(),
        "ALTER TABLE nexus.catalog_links DROP CONSTRAINT fk_catalog_links_from_document; "
        "ALTER TABLE nexus.catalog_links DROP CONSTRAINT fk_catalog_links_to_document;",
    )
    try:
        yield
    finally:
        _restore_link_fks(tenant)
        _FK_RESTORE_PENDING[0] = None


def _purge_one_aged(cat, tumbler: str) -> None:
    """Trash *tumbler*, age the tombstone, hard-delete it through the engine's purge sweep."""
    assert cat.delete_document(tumbler) is True
    _age_tombstone(tumbler)
    result = cat.purge_trash(older_than_days=1, dry_run=False)
    assert result["documents_purged"] >= 1, result
    # Tied to the victim, not just "something was purged": its row is physically gone.
    gone = _psql_superuser(
        _pg_state(),
        f"SELECT count(*) FROM nexus.catalog_documents WHERE tenant_id = {_lit(ambient_tenant())} "
        f"AND tumbler = {_lit(tumbler)}",
    )
    assert int(gone) == 0, f"purge_trash left {tumbler} in place"


# ── 1. hard delete cascades links, both directions ───────────────────────────


def _hard_delete_scenario(cat):
    g = _Graph(cat)
    live, victim = g.doc("live"), g.doc("victim")
    other = g.doc("other")
    g.link(victim, live)   # outbound from the purged document
    g.link(live, victim)   # inbound to the purged document
    g.link(live, other)    # unrelated, must survive
    # Positive control: both link rows exist before the purge.
    assert {(victim, live, _LINK_TYPE), (live, victim, _LINK_TYPE)} <= _rows(victim)
    _purge_one_aged(cat, victim)
    return live, victim, other


def test_hard_delete_removes_the_documents_links_in_both_directions(cat):
    live, victim, other = _hard_delete_scenario(cat)

    assert _rows(victim) == set(), "a link row of the purged document survived"
    # The cascade is scoped to the purged document: an unrelated link is untouched.
    assert (live, other, _LINK_TYPE) in _rows(live)


def test_the_cascade_is_the_fk_not_something_else(cat):
    """Vacuity control for the pin above: with the two FKs dropped the same purge leaves the links.
    Deliberately FK-specific: it would also go red if the cascade moved to an explicit delete in
    ``purge_trash`` (the design RDR-194 D2 rejected), which would be a decision to revisit here."""
    g = _Graph(cat)
    live, victim = g.doc("live"), g.doc("victim")
    g.link(victim, live)
    g.link(live, victim)
    with _link_fks_dropped():
        _purge_one_aged(cat, victim)
        assert {(victim, live, _LINK_TYPE), (live, victim, _LINK_TYPE)} <= _rows(victim), (
            "no FK, yet the links are gone: the pin is vacuous")


# ── 2. trash keeps links, restore returns them ───────────────────────────────


def test_trash_keeps_the_links_and_restore_brings_the_document_back_with_them(cat):
    g = _Graph(cat)
    live, victim = g.doc("live"), g.doc("victim")
    g.link(victim, live)
    g.link(live, victim)
    before_out = {(_pair(lk), lk.created_at) for lk in cat.links_from(victim)}
    before_in = {(_pair(lk), lk.created_at) for lk in cat.links_to(victim)}
    assert before_out and before_in, "positive control: the links exist before the trash"

    assert cat.delete_document(victim) is True
    assert {(victim, live, _LINK_TYPE), (live, victim, _LINK_TYPE)} <= _rows(victim), (
        "trash dropped a link row")

    assert cat.restore_document(victim) is True
    # Same rows, not re-created ones: created_at is part of the row's identity here.
    assert {(_pair(lk), lk.created_at) for lk in cat.links_from(victim)} == before_out
    assert {(_pair(lk), lk.created_at) for lk in cat.links_to(victim)} == before_in


# ── 3. traverse never reaches a trashed endpoint ─────────────────────────────


def test_traverse_does_not_reach_a_trashed_document_and_it_is_no_relay(cat):
    g = _Graph(cat)
    a, d, b = g.doc("a"), g.doc("d"), g.doc("b")
    g.link(a, d)
    g.link(d, b)

    def reach(seed: str, direction: str) -> tuple[set[str], set[tuple[str, str, str]]]:
        r = cat.graph_many([seed], depth=2, direction=direction, link_types=[_LINK_TYPE])
        return {str(n.tumbler) for n in r["nodes"]}, {_pair(e) for e in r["edges"]}

    # Outbound from A exercises the join's to-side filter; inbound from B its from-side filter
    # (edge D->B has D as its from endpoint), so dropping either half goes red.
    for seed, direction, relayed in ((a, "out", b), (b, "in", a)):
        nodes, edges = reach(seed, direction)  # positive control, before the trash
        assert {a, d, b} <= nodes, (direction, nodes)
        assert {(a, d, _LINK_TYPE), (d, b, _LINK_TYPE)} <= edges, (direction, edges)

    assert cat.delete_document(d) is True
    for seed, direction, relayed in ((a, "out", b), (b, "in", a)):
        nodes, edges = reach(seed, direction)
        assert d not in nodes, f"traverse ({direction}) reached a trashed document"
        assert relayed not in nodes, f"a trashed document acted as a relay ({direction})"
        assert all(d not in (f, t) for f, t, _ in edges), (direction, edges)
        assert seed in nodes


# ── 4. links_from / links_to still show a trashed endpoint (current behaviour) ──


def test_links_from_and_links_to_still_list_a_trashed_endpoint_as_of_today(cat):
    """TODAY'S BEHAVIOUR, pinned as it is, not a contract (RDR-147 R8). ``linksFrom`` / ``linksTo``
    read ``catalog_links`` alone, with no join to the documents, so a link whose other end is
    trashed is still returned. Whether they should filter it is decided in RDR-147 Step 2
    (nexus-81183), only if the linker ships; if that decision changes this, this test is rewritten
    with it, not worked around."""
    g = _Graph(cat)
    live, trashed = g.doc("live"), g.doc("trashed")
    g.link(live, trashed)
    g.link(trashed, live)
    assert (live, trashed, _LINK_TYPE) in _out(cat, live)  # positive control, before the trash
    assert (trashed, live, _LINK_TYPE) in _in(cat, live)

    assert cat.delete_document(trashed) is True
    assert (live, trashed, _LINK_TYPE) in _out(cat, live), "links_from now hides a trashed target"
    assert (trashed, live, _LINK_TYPE) in _in(cat, live), "links_to now hides a trashed source"


# ── 5. find_by_title_exact never returns a trashed document ──────────────────


def test_find_by_title_exact_never_returns_a_trashed_document(cat):
    g = _Graph(cat)
    t = g.doc("titled")
    title = g.title("titled")
    assert [str(e.tumbler) for e in cat.find_by_title_exact(title)] == [t]  # positive control

    assert cat.delete_document(t) is True
    assert cat.find_by_title_exact(title) == [], "a trashed document was returned by exact title"

    assert cat.restore_document(t) is True
    assert [str(e.tumbler) for e in cat.find_by_title_exact(title)] == [t]
