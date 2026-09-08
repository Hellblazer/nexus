// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Bead nexus-h8rf6.2 (pure-cache semantics) + RDR-204 Phase 1 bead nexus-ft04v.7
 * ({@link CollectionRegistry#requireRegistered} fail-loud contract).
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
 * <p>This class now proves two things: the {@link CollectionRegistry#requireRegistered}
 * contract (throws {@link UnregisteredCollectionException} and writes nothing when the
 * pair is absent; succeeds and marks the cache when the row exists — driven through
 * {@link ChashRepository#renameCollection}, the surviving production caller after RDR-187
 * retired the upsert write path), and the pure in-process cache semantics
 * (isKnown/markKnown/evict/evictTenant) unaffected by this bead.
 *
 * <p>Hermetic: Testcontainers pgvector/pgvector:pg17, {@code nexus_svc} role (full DML via
 * {@code grants-nexus-svc.xml}), PER_CLASS. {@link CollectionRegistry#clearForTests()} runs
 * after each test so the process-static cache never leaks state between test methods —
 * each method also uses a distinct collection name as defense-in-depth.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionRegistryTest {

    private static final String TENANT = "cr-contention-tenant";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    ChashRepository repo;

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
            ctx.insertInto(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS,
                            dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.TENANT_ID,
                            dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.NAME)
               .values(TENANT, collection)
               .onConflict(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.TENANT_ID,
                           dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.NAME)
               .doNothing()
               .execute();
            return null;
        });
    }

    private boolean rowExists(String collection) {
        return tenantScope.withTenant(TENANT, ctx -> ctx.fetchExists(
            ctx.selectOne()
               .from(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS)
               .where(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)
                   .and(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.NAME.eq(collection)))));
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
        CollectionRegistry.markKnown(TENANT, collection);
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
        CollectionRegistry.markKnown("t1", "c1");
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
        CollectionRegistry.markKnown("t1", "c1");
        CollectionRegistry.markKnown("t1", "c2");
        CollectionRegistry.markKnown("t2", "c1");

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
        CollectionRegistry.markKnown("t1", "c1");
        CollectionRegistry.markKnown("t1", "c2");
        CollectionRegistry.markKnown("t2", "c1");

        CollectionRegistry.evictTenant("t1");

        assertThat(CollectionRegistry.isKnown("t1", "c1")).isFalse();
        assertThat(CollectionRegistry.isKnown("t1", "c2")).isFalse();
        // A different tenant's entries are untouched, including one sharing
        // the SAME collection name ("t1x" starting with "t1" must not
        // collide with "t1" — the '|' separator is part of the prefix test).
        assertThat(CollectionRegistry.isKnown("t2", "c1")).isTrue();
        CollectionRegistry.markKnown("t1x", "c9");
        CollectionRegistry.evictTenant("t1");
        assertThat(CollectionRegistry.isKnown("t1x", "c9"))
                .as("evictTenant(\"t1\") must not evict tenant \"t1x\" via a bare prefix match")
                .isTrue();
        // Evicting a tenant with no cached entries is a harmless no-op.
        CollectionRegistry.evictTenant("never-known-tenant");
    }
}
