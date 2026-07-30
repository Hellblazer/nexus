# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fresh service-side contract tests for bead nexus-i711w.1 — GAP items 10, 11, 12, 21.

Four contracts that previously had NO pinned service-side test, authored fresh
(no ported SQLite source) against a REAL HttpCatalogClient + Java engine + PG —
never a FakeCatalogHandler:

  item 10  duplicate-link dedup: linking the same (from, to, type) twice
           collapses to ONE row (engine: catalog_links_unique
           UNIQUE(tenant_id, from_tumbler, to_tumbler, link_type),
           catalog-001-baseline.xml; upsert via ON CONFLICT,
           CatalogRepository.upsertLink, CatalogRepository.java:1393-1421).
  item 11  nexus-7vuw owner name/type coexistence: owners with the same name
           but DIFFERENT owner_type coexist as separate rows with distinct
           tumbler prefixes (UNIQUE(name) was dropped in PR #494, replaced by
           the composite catalog_owners_unique_name_type
           UNIQUE(tenant_id, name, owner_type) — catalog-001-baseline.xml:47).
  item 12  search-still-hits-after-rename: after rename_collection(old, new),
           every catalog read that drives search attribution resolves under
           the NEW name and nothing dangles under the old one
           (CatalogRepository.renameCollectionTxn, CatalogRepository.java:2365-2446).
           Scoped to the CATALOG half of the contract; T3 vector renames are
           covered elsewhere.
  item 21  link-merge co_discovered_by against the REAL engine: the canonical
           local merge (catalog_links.py:224-268) preserves the original
           created_by and folds later creators into meta["co_discovered_by"].
           The engine's POST /link is ON CONFLICT DO UPDATE that OVERWRITES
           created_by and metadata with the EXCLUDED values instead
           (CatalogRepository.java:1404-1409) — the fold assertion is
           xfail(strict=True) with that evidence; the created/merged boolean
           half of the contract passes.

Fixture stack: same real-engine pattern as tests/db/test_http_catalog_integration.py
(hermetic PG + Liquibase-managed schema + shaded service JAR + HttpCatalogClient).
The module-scoped fixtures are IMPORTED from that module (the
test_http_shape_parity_integration.py precedent, same as the sibling
test_i711w_gap_xfails.py); pytest caches module-scoped fixtures per requesting
module, so this file still gets its own fresh PG + service instance and cannot
contaminate (or be contaminated by) any sibling module's data.
Marked @pytest.mark.integration — skips automatically when prerequisites are
absent. Public-API seeding only.

Run locally:
    cd service && mvn package -DskipTests
    uv run pytest tests/db/test_i711w_gap_contracts_fresh.py -m integration -q
"""
from __future__ import annotations

import hashlib

import pytest

# pytest resolves imported fixtures by name (same pattern as
# tests/db/test_http_shape_parity_integration.py). Each module gets its own
# module-scoped instances — fresh PG + fresh service for this file.
from tests.db.test_http_catalog_integration import (  # noqa: F401, PLC2701 — pytest resolves imported fixtures by name
    _ALL_PREREQS,
    _JAR,
    _JAVA,
    _PG_CTL,
    cat,
    pg_instance,
    service,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _ALL_PREREQS,
        reason=(
            "skipped: missing jar or PG binaries "
            f"(jar={_JAR.exists()}, pg={_PG_CTL.exists()}, java={_JAVA})"
        ),
    ),
]


def _ch(seed: str) -> str:
    """Full 64-lowercase-hex sha256 chash (RDR-180 manifest boundary width)."""
    return hashlib.sha256(seed.encode()).hexdigest()


@pytest.fixture(scope="module")
def owner(cat):
    """Base owner for document registration (explicit prefix, ETL-style)."""
    from nexus.catalog.tumbler import Tumbler
    t = cat.register_owner(
        name="i711w1-gap-owner",
        owner_type="curator",
        tumbler_prefix="1.1",
    )
    assert isinstance(t, Tumbler)
    return t


# ══════════════════════════════════════════════════════════════════════════════
# nexus-i711w.1 item 10 — duplicate-link dedup
# ══════════════════════════════════════════════════════════════════════════════


class TestItem10DuplicateLinkDedup:
    """Linking the same (from, to, type) twice collapses to ONE row.

    Engine: nexus.catalog_links carries catalog_links_unique
    UNIQUE(tenant_id, from_tumbler, to_tumbler, link_type)
    (service/src/main/resources/db/changelog/catalog-001-baseline.xml) and
    POST /v1/catalog/link is an ON CONFLICT upsert on exactly that key
    (CatalogRepository.upsertLink, CatalogRepository.java:1393-1421 —
    ``.onConflict(TENANT_ID, FROM_TUMBLER, TO_TUMBLER, LINK_TYPE)``).
    """

    # nexus-i711w.1 item 10
    def test_duplicate_link_collapses_to_one_row(self, cat, owner) -> None:
        a = cat.register(owner, "Dedup-From", content_type="paper",
                         source_uri="file:///i711w1/dedup/a.md")
        b = cat.register(owner, "Dedup-To", content_type="paper",
                         source_uri="file:///i711w1/dedup/b.md")

        first = cat.link(a, b, "cites", created_by="dedup-agent")
        assert first is True, "first link on a fresh edge must report created=True"

        second = cat.link(a, b, "cites", created_by="dedup-agent")
        assert second is False, (
            "second identical link must merge into the existing row "
            "(created=False, xmax!=0 RETURNING predicate — "
            "CatalogRepository.java:1417)"
        )

        # link_query on the exact key shows exactly ONE edge.
        rows = cat.link_query(from_t=str(a), to_t=str(b), link_type="cites")
        assert len(rows) == 1, (
            f"expected exactly 1 (from,to,type) row after duplicate link; "
            f"got {len(rows)}"
        )

        # links_from shows exactly ONE outgoing 'cites' edge from a.
        lf = cat.links_from(a, link_type="cites")
        edges_to_b = [lk for lk in lf if str(lk.to_tumbler) == str(b)]
        assert len(edges_to_b) == 1, (
            f"expected exactly 1 cites edge a->b in links_from; "
            f"got {len(edges_to_b)} of {len(lf)} total"
        )


# ══════════════════════════════════════════════════════════════════════════════
# nexus-i711w.1 item 11 — nexus-7vuw owner name/type coexistence
# ══════════════════════════════════════════════════════════════════════════════


class TestItem11OwnerNameTypeCoexistence:
    """Owners with the SAME name but DIFFERENT owner_type coexist.

    Contract from bead nexus-7vuw (closed via PR #494): the plain
    UNIQUE(name) constraint on owners was DROPPED and replaced with the
    composite UNIQUE(name, owner_type), precisely so a repo-type owner and a
    curator-type owner sharing a name are two separate rows with distinct
    tumbler prefixes.

    Engine: catalog_owners_unique_name_type UNIQUE (tenant_id, name,
    owner_type) — service/src/main/resources/db/changelog/
    catalog-001-baseline.xml:47; upsert allocation in
    CatalogRepository.upsertOwner (CatalogRepository.java:216-274): explicit
    prefix honoured, else repo_hash reuse, else fresh 1.{MAX+1}.
    """

    _NAME = "i711w1-samename"
    _REPO_HASH = _ch("i711w1-samename-repo-identity")[:16]

    @pytest.fixture(scope="class")
    def coexisting(self, cat):
        """Perform the same-name/different-type registrations ONCE for the
        class (sibling TestItem12/TestItem21 pattern) so neither test depends
        on the other having run first. Returns the repo-type owner's tumbler.

        Repo-type first, keyed by repo_hash so its prefix is deterministically
        retrievable via /owners/by_repo regardless of by_name row order.
        The curator-type register's return value is not captured: /owners/
        upsert echoes only {"ok":true} — CatalogHandler.java:903-908 — and the
        client's by_name fallback takes rows[0], which is ambiguous once two
        owners share a name; tests fetch each prefix through the
        type-discriminating public reads instead.
        """
        t_repo = cat.register_owner(
            name=self._NAME,
            owner_type="repo",
            repo_hash=self._REPO_HASH,
        )
        cat.register_owner(name=self._NAME, owner_type="curator")
        return t_repo

    # nexus-i711w.1 item 11
    def test_same_name_different_type_coexist(self, cat, coexisting) -> None:
        from nexus.catalog.tumbler import Tumbler

        t_repo = coexisting
        assert isinstance(t_repo, Tumbler)

        # Same name, DIFFERENT type: must NOT collide, must NOT have merged
        # into the repo-type row.
        t_cur = cat.curator_owner_tumbler_by_name(self._NAME)
        assert t_cur is not None, (
            "curator-type owner with a name already used by a repo-type owner "
            "must be registered (nexus-7vuw coexistence contract)"
        )
        assert str(t_cur) != str(t_repo), (
            "same-name owners of different types must be DISTINCT rows with "
            "distinct tumbler prefixes"
        )

        # by_name sees both rows.
        prefixes = {str(t) for t in cat.owner_tumblers_by_name(self._NAME)}
        assert prefixes == {str(t_repo), str(t_cur)}, (
            f"expected both same-name owners in by_name; got {prefixes}"
        )

        # Each row keeps its own owner_type.
        repo_row = cat.get_owner_by_prefix(str(t_repo))
        cur_row = cat.get_owner_by_prefix(str(t_cur))
        assert repo_row is not None and repo_row.get("owner_type") == "repo"
        assert cur_row is not None and cur_row.get("owner_type") == "curator"

    # nexus-i711w.1 item 11
    def test_same_name_same_type_reupsert_is_idempotent(
        self, cat, coexisting
    ) -> None:
        """Re-upserting the repo-type owner (same repo_hash) reuses its prefix
        and does NOT create a third same-name row (upsertOwner's repo_hash
        reuse leg, CatalogRepository.java:233-240)."""
        t_repo = cat.owner_for_repo(self._REPO_HASH)
        assert t_repo is not None, (
            "repo-type owner from the class-scoped coexistence fixture must exist"
        )
        assert str(t_repo) == str(coexisting)

        cat.register_owner(
            name=self._NAME,
            owner_type="repo",
            repo_hash=self._REPO_HASH,
        )

        assert str(cat.owner_for_repo(self._REPO_HASH)) == str(t_repo), (
            "re-upsert with the same repo_hash must keep the same prefix"
        )
        assert len(cat.owner_tumblers_by_name(self._NAME)) == 2, (
            "re-upsert must not mint a third same-name owner row"
        )


# ══════════════════════════════════════════════════════════════════════════════
# nexus-i711w.1 item 12 — search-still-hits-after-rename (catalog half)
# ══════════════════════════════════════════════════════════════════════════════


class TestItem12CatalogReadsAfterRename:
    """After rename_collection(old, new), every catalog read that drives
    search attribution resolves under the NEW name; nothing dangles under old.

    Engine: CatalogRepository.renameCollectionTxn
    (CatalogRepository.java:2365-2446) — canonical branch inserts the new
    registry row copying the tuple (2391), re-homes
    catalog_documents.physical_collection (2434) and the manifest's
    denormalized catalog_document_chunks.collection (2418, nexus-x6kdz — the
    combined-query join key), then deletes the old registry row (2442).

    EXPLICIT SCOPE NARROWING (stacked-review disclosure, 2026-07-29): this
    class pins the CATALOG-metadata half of the "search still hits after
    rename" contract ONLY — every read below is a catalog read (resolve /
    list_by_collection / lookup_doc_id_by_collection_and_path /
    get_collection / chashes_for_collection). NO real search()-after-rename
    test exists ANYWHERE in the repo: the engine-side vector re-home
    (chunks_384/768/1024, CatalogRepository.java:2411-2413) is asserted by
    nothing end-to-end, and tests/test_collection_rename_service_mode.py is
    MagicMock-only (it pins the client's request shape, not that a real
    semantic search over the renamed collection returns the doc's chunks).
    Follow-up: the real-search-after-rename half, recorded as a GAP
    candidate on nexus-i711w.1.
    """

    _OLD = "knowledge__i711w1-gapfresh__voyage-context-3__v1"
    _NEW = "knowledge__i711w1-gapfresh-renamed__voyage-context-3__v1"
    _PATH = "papers/i711w1_rename_probe.md"
    _CHASH = _ch("i711w1-rename-probe-chunk-0")

    @pytest.fixture(scope="class")
    def renamed(self, cat, owner):
        """Seed collection + doc + manifest under OLD via public API, then rename."""
        cat.register_collection(
            self._OLD,
            content_type="knowledge",
            owner_id="i711w1-gapfresh",
            embedding_model="voyage-context-3",
            model_version="v1",
        )
        t = cat.register(
            owner,
            "Rename Attribution Probe",
            content_type="knowledge",
            file_path=self._PATH,
            physical_collection=self._OLD,
            source_uri="file:///i711w1/rename_probe.md",
        )
        cat.write_manifest(str(t), [
            {"position": 0, "chash": self._CHASH, "line_start": 1, "line_end": 10},
        ])
        # Sanity: seeded reads resolve under OLD before the rename.
        assert cat.lookup_doc_id_by_collection_and_path(self._OLD, self._PATH) == str(t)
        assert self._CHASH in cat.chashes_for_collection(self._OLD)

        rehomed = cat.rename_collection(self._OLD, self._NEW)
        assert rehomed == 1, (
            f"rename_collection must report the 1 re-homed catalog_documents "
            f"row; got {rehomed}"
        )
        return t

    # nexus-i711w.1 item 12
    def test_resolve_carries_new_collection(self, cat, renamed) -> None:
        entry = cat.resolve(renamed)
        assert entry is not None
        assert entry.physical_collection == self._NEW, (
            "doc's physical_collection must be re-homed to the new name "
            "(CatalogRepository.java:2434)"
        )

    # nexus-i711w.1 item 12
    def test_by_collection_resolves_new_and_empties_old(self, cat, renamed) -> None:
        new_docs = cat.list_by_collection(self._NEW)
        assert str(renamed) in [str(d.tumbler) for d in new_docs], (
            "list_by_collection(new) must return the re-homed doc"
        )
        assert cat.list_by_collection(self._OLD) == [], (
            "list_by_collection(old) must be empty — nothing dangles under "
            "the old name"
        )

    # nexus-i711w.1 item 12
    def test_collection_path_lookup_resolves_new_and_empties_old(
        self, cat, renamed
    ) -> None:
        assert cat.lookup_doc_id_by_collection_and_path(
            self._NEW, self._PATH
        ) == str(renamed), (
            "(collection, path) attribution probe must resolve under the "
            "new name"
        )
        assert cat.lookup_doc_id_by_collection_and_path(
            self._OLD, self._PATH
        ) == "", (
            "(old collection, path) must no longer resolve"
        )

    # nexus-i711w.1 item 12
    def test_registry_row_moved(self, cat, renamed) -> None:
        assert cat.get_collection(self._NEW) is not None, (
            "new registry row must exist (CatalogRepository.java:2391)"
        )
        assert cat.get_collection(self._OLD) is None, (
            "old registry row must be deleted (CatalogRepository.java:2442)"
        )
        names = {c.get("name") for c in cat.list_collections()}
        assert self._NEW in names
        assert self._OLD not in names

    # nexus-i711w.1 item 12
    def test_manifest_chash_attribution_follows_rename(self, cat, renamed) -> None:
        """The manifest-side attribution read (the combined-query join key,
        nexus-x6kdz) follows the rename: chashes resolve under new, not old."""
        assert self._CHASH in cat.chashes_for_collection(self._NEW), (
            "manifest chash must be attributable under the NEW collection"
        )
        assert cat.chashes_for_collection(self._OLD) == set(), (
            "no manifest chash may dangle under the OLD collection"
        )


# ══════════════════════════════════════════════════════════════════════════════
# nexus-i711w.1 item 21 — link-merge co_discovered_by against a REAL engine
# ══════════════════════════════════════════════════════════════════════════════


class TestItem21LinkMergeCoDiscoveredBy:
    """POST /link is UPSERT (http_catalog_client.py:1379,1405 comments; engine
    CatalogRepository.upsertLink). Contract per the canonical local merge
    (src/nexus/catalog/catalog_links.py:224-268): a second link on the same
    (from, to, type) by a DIFFERENT creator merges — returns False, preserves
    the ORIGINAL created_by, and folds the new creator into
    meta["co_discovered_by"]. The MCP surface promises exactly this
    ("Duplicate links are merged with co_discovered_by tracking",
    src/nexus/mcp/catalog.py:373) in service mode too.
    """

    @pytest.fixture(scope="class")
    def merged_edge(self, cat, owner):
        c = cat.register(owner, "CoDisc-From", content_type="paper",
                         source_uri="file:///i711w1/codisc/from.md")
        d = cat.register(owner, "CoDisc-To", content_type="paper",
                         source_uri="file:///i711w1/codisc/to.md")
        first = cat.link(c, d, "cites", created_by="agent-x")
        second = cat.link(c, d, "cites", created_by="agent-y")
        return c, d, first, second

    # nexus-i711w.1 item 21
    def test_created_then_merged_flags(self, cat, merged_edge) -> None:
        """First link(A,B,type,created_by=X) returns True; second with
        created_by=Y returns False (merged). Engine: the ``(xmax = 0)``
        RETURNING predicate distinguishes fresh INSERT from DO UPDATE
        (CatalogRepository.java:1417-1419)."""
        _c, _d, first, second = merged_edge
        assert first is True
        assert second is False

    # nexus-i711w.1 item 21 — PRODUCT DIVERGENCE (engine vs canonical contract)
    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason=(
            "engine DIVERGES from the canonical co_discovered_by merge: "
            "CatalogRepository.upsertLink's ON CONFLICT DO UPDATE sets "
            "created_by = EXCLUDED.created_by and metadata = EXCLUDED.metadata "
            "(CatalogRepository.java:1404-1409, EX_LNK_CRTBY/EX_LNK_META at "
            "lines 177-178) — the second writer OVERWRITES the original "
            "creator and wipes the merged metadata instead of preserving "
            "created_by and folding the new creator into "
            "meta['co_discovered_by'] as the canonical local merge does "
            "(catalog_links.py:224-268; MCP contract mcp/catalog.py:373). "
            "No co_discovered_by handling exists anywhere in service/src. "
            "When the engine implements the fold this strict xfail flips to "
            "XPASS and forces removal of the marker."
        ),
    )
    def test_co_discovered_by_folds_second_creator(self, cat, merged_edge) -> None:
        c, d, _first, _second = merged_edge
        edges = [
            lk for lk in cat.links_from(c, link_type="cites")
            if str(lk.to_tumbler) == str(d)
        ]
        assert len(edges) == 1
        lk = edges[0]
        # Canonical contract: original creator preserved …
        assert lk.created_by == "agent-x", (
            f"merged link must keep the ORIGINAL creator; got {lk.created_by!r}"
        )
        # … and the second creator folded into co_discovered_by.
        assert "agent-y" in lk.meta.get("co_discovered_by", []), (
            f"second creator must be folded into meta['co_discovered_by']; "
            f"got meta={lk.meta!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Item 19 — alias_of POPULATION (Hal ruling 2026-07-30: pin the write path
# now; alias FOLLOWING stays nexus-ekaxn's, where resolve()/resolve_alias
# are documented no-ops service-side)
# ─────────────────────────────────────────────────────────────────────────────


class TestItem19AliasOfPopulation:
    """set_alias() writes alias_of on the engine and the entry surfaces it.

    # nexus-i711w.1 item 19
    The local projector populated ``alias_of`` from DocumentAliased events;
    that machinery dies with the src. The service write path is
    ``HttpCatalogClient.set_alias`` -> POST /update {tumbler, alias_of}
    (http_catalog_client.py:1142-1143), and the read side maps the column
    back at ``_to_entry`` (:194). This pins exactly that round trip —
    population, not following.
    """

    def test_set_alias_populates_alias_of_on_the_entry(self, cat, owner):
        src = cat.register(
            title="i711w1-item19-alias-src",
            owner=str(owner),
            physical_collection="code__i711w1-item19__v1",
            file_path="src/alias_src.py",
        )
        canonical = cat.register(
            title="i711w1-item19-canonical",
            owner=str(owner),
            physical_collection="code__i711w1-item19__v1",
            file_path="src/alias_dst.py",
        )
        # Pre-state guard (non-vacuity): fresh registration carries no alias.
        before = cat.resolve(str(src))
        assert before is not None and before.alias_of == "", (
            f"pre-state: expected empty alias_of, got {before.alias_of!r}"
        )

        cat.set_alias(src, canonical)

        after = cat.resolve(str(src))
        assert after is not None, "aliased entry must still resolve (population, not deletion)"
        assert after.alias_of == str(canonical), (
            f"alias_of must surface the canonical tumbler: "
            f"expected {canonical!s}, got {after.alias_of!r}"
        )
