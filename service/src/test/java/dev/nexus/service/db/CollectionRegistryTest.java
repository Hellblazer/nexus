// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.EmbedderRouter;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Bead nexus-h8rf6.2 (pure-cache semantics) + RDR-204 Phase 1 bead nexus-ft04v.7
 * ({@link CollectionRegistry#requireRegistered} fail-loud contract) + RDR-204
 * Phase 2 bead nexus-ft04v.14 (the cache holds the {@link CollectionRow}, not
 * presence — {@link #require_cachesFullRow_andHitAvoidsCatalogRead()} through
 * {@link #supersedeCollection_doesNotLeaveAStaleRowInTheCache()} below).
 *
 * <p><strong>Retired by nexus-ft04v.7:</strong> the lock-contention tests this class used
 * to carry (a held, uncommitted lock on a {@code catalog_collections} row, racing {@code
 * ensureCollectionRegistered}'s {@code INSERT ... ON CONFLICT DO NOTHING} on another
 * thread) tested a mechanism that no longer exists. {@code ChashRepository
 * .ensureCollectionRegistered} — like the other six stub-insert paths this bead retires —
 * no longer writes a row at all; it calls {@link CollectionRegistry#requireRegistered},
 * a read-only existence check that never contends for a row lock, cached or not. The
 * contention story survives only for the FOUR write paths that carry real, client-supplied
 * attributes ({@code CatalogRepository.upsertCollection} and siblings), which this bead does
 * not touch and this class does not exercise.
 *
 * <p>This class now proves: the {@link CollectionRegistry#requireRegistered} contract
 * (throws {@link UnregisteredCollectionException} and writes nothing when the pair is
 * absent; succeeds and marks the cache when the row exists — driven through {@link
 * ChashRepository#renameCollection}, the surviving production caller after RDR-187
 * retired the upsert write path); the pure in-process cache semantics
 * (isKnown/markKnown/evict/evictTenant) unaffected by nexus-ft04v.7; and, as of
 * nexus-ft04v.14, the ROW-cache contract — {@link CollectionRegistry#require} returns
 * every attribute and a hit never re-queries, and all four invalidation points (DELETE,
 * canonical RENAME, {@code embedding_profile} write, and supersede's deliberate
 * non-invalidation) leave no stale row readable.
 *
 * <p>Hermetic: Testcontainers pgvector/pgvector:pg17, {@code nexus_svc} role (full DML via
 * {@code grants-nexus-svc.xml}), PER_CLASS. {@link CollectionRegistry#clearForTests()} runs
 * after each test so the process-static cache never leaks state between test methods —
 * each method also uses a distinct collection name as defense-in-depth.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionRegistryTest {

    private static final String TENANT = "cr-contention-tenant";

    /** A stand-in for {@code Bge768Embedder} that needs no model file (mirrors EmbedderRouterEmbeddingProfileSeedTest). */
    private static final class FakeBge implements Embedder {
        @Override public List<float[]> embed(List<String> texts) {
            return texts.stream().map(t -> new float[768]).toList();
        }
        @Override public String modelToken() {
            return "bge-base-en-v15-768";
        }
    }

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    ChashRepository repo;
    CatalogRepository catalogRepo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername("nexus_svc");
        cfg.setPassword("nexus_svc_pass");
        // Pool sized 2: one connection for the blocker, one for the repo call under test —
        // proves the mechanism without needing a big pool or wall-clock racing.
        cfg.setMaximumPoolSize(2);
        cfg.setConnectionTimeout(15000);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        tenantScope = new TenantScope(svcDs);
        repo = new ChashRepository(tenantScope);
        catalogRepo = new CatalogRepository(tenantScope);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg    != null) pg.stop();
    }

    @AfterEach
    void clearCache() {
        CollectionRegistry.clearForTests();
    }

    // -------------------------------------------------------------------------
    // Helper: seed a real catalog_collections row via the generated jOOQ DSL
    // (never raw SQL) — the "already registered" fixture for the tests below.
    // -------------------------------------------------------------------------

    private void seedRegistered(String collection) {
        tenantScope.withTenant(TENANT, ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT, collection);
            return null;
        });
    }

    /**
     * Seed a {@code catalog_collections} row with EXPLICIT, non-default attributes
     * (nexus-ft04v.14) — unlike {@link #seedRegistered}, which never sets {@code
     * dimension} and so cannot distinguish "the column was read" from "the column
     * is always NULL". {@code embeddingModel} must be a real {@code
     * nexus.embedding_models} row (FK) — callers pass {@code "voyage-code-3"} or
     * {@code "bge-base-en-v15-768"}, both seeded by {@code
     * catalog-036-embedding-profile.xml}.
     */
    private void seedRegisteredWithAttributes(String collection, String contentType, String ownerId,
                                               String embeddingModel, int dimension, String lifecycleState) {
        tenantScope.withTenant(TENANT, ctx -> {
            ctx.insertInto(CATALOG_COLLECTIONS,
                    CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                    CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                    CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.DIMENSION,
                    CATALOG_COLLECTIONS.LIFECYCLE_STATE)
                .values(TENANT, collection, contentType, ownerId, embeddingModel, dimension, lifecycleState)
                .onConflictDoNothing()
                .execute();
            return null;
        });
    }

    private boolean rowExists(String collection) {
        return tenantScope.withTenant(TENANT, ctx -> ctx.fetchExists(
            ctx.selectOne()
               .from(CATALOG_COLLECTIONS)
               .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)
                   .and(CATALOG_COLLECTIONS.NAME.eq(collection)))));
    }

    // -------------------------------------------------------------------------
    // RDR-204 Phase 1 (bead nexus-ft04v.7): CollectionRegistry.requireRegistered's
    // fail-loud contract, driven through ChashRepository.renameCollection — the
    // surviving in-txn ensureCollectionRegistered caller after RDR-187 retired the
    // upsert write path (PgVectorRepository's ingest registration mirrors the same
    // seam).
    // -------------------------------------------------------------------------

    @Test
    void requireRegistered_throwsAndWritesNoRow_whenCollectionNeverRegistered() {
        String collection = "code__cr-miss__voyage-code-3__v1";
        assertThat(CollectionRegistry.isKnown(TENANT, collection)).isFalse();
        assertThat(rowExists(collection)).isFalse();

        assertThatThrownBy(
                () -> repo.renameCollection(TENANT, "cr-miss-src", collection))
            .isInstanceOf(UnregisteredCollectionException.class)
            .hasMessageContaining(collection)
            .hasMessageContaining("POST /v1/catalog/collections/upsert");

        assertThat(CollectionRegistry.isKnown(TENANT, collection)).isFalse();
        assertThat(rowExists(collection))
            .as("a write against an unregistered collection must create NO catalog_collections row")
            .isFalse();
    }

    @Test
    void requireRegistered_succeedsAndMarksKnown_whenCollectionAlreadyRegistered() {
        String collection = "code__cr-hit__voyage-code-3__v1";
        seedRegistered(collection);
        assertThat(CollectionRegistry.isKnown(TENANT, collection)).isFalse();

        assertThatCode(() -> repo.renameCollection(TENANT, "cr-hit-src", collection))
            .doesNotThrowAnyException();

        assertThat(CollectionRegistry.isKnown(TENANT, collection))
            .as("requireRegistered must mark the pair known after confirming the row exists")
            .isTrue();
    }

    @Test
    void requireRegistered_skipsDbCheck_whenAlreadyCached() {
        // Cache the pair WITHOUT a real row — proves requireRegistered trusts the
        // cache and never re-checks the database once a pair is known-registered.
        String collection = "code__cr-cached__voyage-code-3__v1";
        CollectionRegistry.markKnown(TENANT, collection,
            new CollectionRow("code", "cr-cached", "voyage-code-3", 1024, "live"));
        assertThat(rowExists(collection)).isFalse();

        assertThatCode(() -> repo.renameCollection(TENANT, "cr-cached-src", collection))
            .as("a cached (tenant, collection) pair must skip the DB existence check entirely")
            .doesNotThrowAnyException();
    }

    // -------------------------------------------------------------------------
    // Test 3: pure cache semantics (no DB) — fast sanity on isKnown/markKnown.
    // -------------------------------------------------------------------------

    @Test
    void isKnown_falseByDefault_trueAfterMarkKnown_scopedPerTenantAndCollection() {
        assertThat(CollectionRegistry.isKnown("t1", "c1")).isFalse();
        CollectionRegistry.markKnown("t1", "c1", new CollectionRow("code", "o", "voyage-code-3", 1024, "live"));
        assertThat(CollectionRegistry.isKnown("t1", "c1")).isTrue();
        // Distinct tenant, same collection name — must NOT be known.
        assertThat(CollectionRegistry.isKnown("t2", "c1")).isFalse();
        // Same tenant, distinct collection — must NOT be known.
        assertThat(CollectionRegistry.isKnown("t1", "c2")).isFalse();
    }

    @Test
    void evict_forgetsOnlyTheEvictedPair() {
        // nexus-h8rf6 wave review: deleteCollection / renameCollection remove the
        // catalog_collections row post-commit; a stale cache entry would make
        // later writers silently skip re-registration for a reused name.
        CollectionRow row = new CollectionRow("code", "o", "voyage-code-3", 1024, "live");
        CollectionRegistry.markKnown("t1", "c1", row);
        CollectionRegistry.markKnown("t1", "c2", row);
        CollectionRegistry.markKnown("t2", "c1", row);

        CollectionRegistry.evict("t1", "c1");

        assertThat(CollectionRegistry.isKnown("t1", "c1")).isFalse();
        // Sibling collection and other tenant untouched.
        assertThat(CollectionRegistry.isKnown("t1", "c2")).isTrue();
        assertThat(CollectionRegistry.isKnown("t2", "c1")).isTrue();
        // Evicting an unknown pair is a harmless no-op.
        CollectionRegistry.evict("t1", "never-known");
    }

    @Test
    void evictTenant_forgetsOnlyThatTenantsPairs() {
        // RDR-204 bead nexus-ft04v.6: an embedding_profile write for a tenant
        // evicts every CollectionRegistry entry for that tenant only.
        CollectionRow row = new CollectionRow("code", "o", "voyage-code-3", 1024, "live");
        CollectionRegistry.markKnown("t1", "c1", row);
        CollectionRegistry.markKnown("t1", "c2", row);
        CollectionRegistry.markKnown("t2", "c1", row);

        CollectionRegistry.evictTenant("t1");

        assertThat(CollectionRegistry.isKnown("t1", "c1")).isFalse();
        assertThat(CollectionRegistry.isKnown("t1", "c2")).isFalse();
        // A different tenant's entries are untouched, including one sharing
        // the SAME collection name ("t1x" starting with "t1" must not
        // collide with "t1" — the '|' separator is part of the prefix test).
        assertThat(CollectionRegistry.isKnown("t2", "c1")).isTrue();
        CollectionRegistry.markKnown("t1x", "c9", row);
        CollectionRegistry.evictTenant("t1");
        assertThat(CollectionRegistry.isKnown("t1x", "c9"))
                .as("evictTenant(\"t1\") must not evict tenant \"t1x\" via a bare prefix match")
                .isTrue();
        // Evicting a tenant with no cached entries is a harmless no-op.
        CollectionRegistry.evictTenant("never-known-tenant");
    }

    // -------------------------------------------------------------------------
    // RDR-204 Phase 2 (bead nexus-ft04v.14): the cache holds the ROW, not presence.
    // -------------------------------------------------------------------------

    @Test
    void require_cachesFullRow_andHitAvoidsCatalogRead() {
        String collection = "code__cr-row-cache__voyage-code-3__v1";
        seedRegisteredWithAttributes(collection, "code", "cr-row-cache", "voyage-code-3", 1024, "live");

        CollectionRow row = tenantScope.withTenant(TENANT,
            ctx -> CollectionRegistry.require(ctx, TENANT, collection));

        assertThat(row.contentType()).isEqualTo("code");
        assertThat(row.ownerId()).isEqualTo("cr-row-cache");
        assertThat(row.embeddingModel()).isEqualTo("voyage-code-3");
        assertThat(row.dimension()).isEqualTo(1024);
        assertThat(row.lifecycleState()).isEqualTo("live");

        // A second require() call with a null DSLContext proves the hit is served
        // from the cache without ever touching the database: on a cache MISS this
        // would NPE inside the SELECT; on a HIT the row is returned before ctx is
        // ever dereferenced.
        CollectionRow cachedRow = CollectionRegistry.require(null, TENANT, collection);
        assertThat(cachedRow).isEqualTo(row);
    }

    @Test
    void require_throwsWithNoRowCached_whenCollectionNeverRegistered() {
        String collection = "code__cr-row-miss__voyage-code-3__v1";
        assertThatThrownBy(() -> tenantScope.withTenant(TENANT,
                ctx -> CollectionRegistry.require(ctx, TENANT, collection)))
            .isInstanceOf(UnregisteredCollectionException.class);
        assertThat(CollectionRegistry.cached(TENANT, collection)).isEmpty();
    }

    @Test
    void deleteCollection_evictsCachedRow() {
        String collection = "code__cr-del-evict__voyage-code-3__v1";
        seedRegisteredWithAttributes(collection, "code", "cr-del-evict", "voyage-code-3", 1024, "live");
        CollectionRegistry.markKnown(TENANT, collection,
            new CollectionRow("code", "cr-del-evict", "voyage-code-3", 1024, "live"));
        assertThat(CollectionRegistry.cached(TENANT, collection)).isPresent();

        catalogRepo.deleteCollection(TENANT, collection);

        assertThat(CollectionRegistry.cached(TENANT, collection))
            .as("delete must evict the cached row so a reused name re-verifies against the database")
            .isEmpty();
    }

    @Test
    void renameCollection_canonicalBranch_evictsOldName_andCachesNewNameRow() {
        String oldName = "code__cr-ren-old__voyage-code-3__v1";
        String newName = "code__cr-ren-new__voyage-code-3__v1";
        seedRegisteredWithAttributes(oldName, "code", "cr-ren-old", "voyage-code-3", 1024, "live");
        CollectionRegistry.markKnown(TENANT, oldName,
            new CollectionRow("code", "cr-ren-old", "voyage-code-3", 1024, "live"));

        catalogRepo.renameCollection(TENANT, oldName, newName);

        assertThat(CollectionRegistry.cached(TENANT, oldName))
            .as("the canonical rename branch retires oldName as a tombstone; a stale cache entry "
                + "would make a LATER reuse of oldName silently skip re-registration")
            .isEmpty();
        assertThat(CollectionRegistry.cached(TENANT, newName))
            .as("markKnown must cache newName's row, copied from oldName's metadata by the "
                + "rename's own INSERT-SELECT")
            .hasValueSatisfying(row -> {
                assertThat(row.contentType()).isEqualTo("code");
                assertThat(row.ownerId()).isEqualTo("cr-ren-old");
                assertThat(row.embeddingModel()).isEqualTo("voyage-code-3");
                assertThat(row.dimension()).isEqualTo(1024);
                assertThat(row.lifecycleState()).isEqualTo("live");
            });
    }

    @Test
    void profileWrite_evictsCollectionRegistry_forcingSubsequentReadToReVerify() {
        // Non-vacuous per Sam's decision (T2 204-research-17, mirrored from
        // EmbedderRouterEmbeddingProfileSeedTest): asserting the whole cache is
        // empty afterward would be vacuous if it started empty. Pre-populate a
        // KNOWN entry, then observe the flip from present to absent — proving the
        // eviction ITSELF fired, not merely that a re-read happens to agree (a
        // profile write touches nexus.embedding_profile, never catalog_collections,
        // so a value-based assertion here would pass whether or not evictTenant ran).
        String tenant = TENANT + "-profile";
        String collection = "code__" + tenant + "__bge-base-en-v15-768__v1";
        CollectionRegistry.markKnown(tenant, collection,
            new CollectionRow("code", tenant, "bge-base-en-v15-768", 768, "live"));
        assertThat(CollectionRegistry.cached(tenant, collection)).isPresent();

        EmbedderRouter router = new EmbedderRouter(new FakeBge(), "document");
        router.seedEmbeddingProfile(tenantScope, tenant);

        assertThat(CollectionRegistry.cached(tenant, collection))
            .as("a profile write for this tenant must evict every cached CollectionRow, forcing "
                + "the next read to re-verify against the database rather than trusting stale "
                + "in-process state")
            .isEmpty();
    }

    @Test
    void supersedeCollection_doesNotLeaveAStaleRowInTheCache() {
        String name = "code__cr-supersede__voyage-code-3__v1";
        String target = "code__cr-supersede-target__voyage-code-3__v2";
        seedRegisteredWithAttributes(name, "code", "cr-supersede", "voyage-code-3", 1024, "live");
        CollectionRow cachedBefore = tenantScope.withTenant(TENANT,
            ctx -> CollectionRegistry.require(ctx, TENANT, name));

        catalogRepo.supersedeCollection(TENANT, name, target, "");

        // supersedeCollection touches ONLY superseded_by/superseded_at — none of the
        // five attributes CollectionRow caches — so the row must read identically
        // whether served from the (deliberately unevicted) cache or freshly
        // re-fetched: neither path may disagree with the other, and neither may
        // disagree with what was true before the supersede.
        CollectionRow cachedAfter = CollectionRegistry.cached(TENANT, name).orElseThrow();
        assertThat(cachedAfter).isEqualTo(cachedBefore);

        CollectionRegistry.evict(TENANT, name);
        CollectionRow freshAfter = tenantScope.withTenant(TENANT,
            ctx -> CollectionRegistry.require(ctx, TENANT, name));
        assertThat(freshAfter)
            .as("a fresh re-read after supersede must agree with the never-invalidated cached "
                + "value -- supersede must never leave a caller reading a stale/mismatched row, "
                + "cached or freshly fetched")
            .isEqualTo(cachedBefore);
    }
}
