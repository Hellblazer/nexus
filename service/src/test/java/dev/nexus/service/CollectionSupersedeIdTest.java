// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.CollectionTombstones;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.UnregisteredCollectionException;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.SQLException;
import java.util.Map;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.1 stage 2b (nexus-p2m1x.17), residual R7 (nexus-p2m1x.46): the writers that retire a registry row.
 *
 * <p>{@code superseded_by_id = collection_id} reads "retired, successor gone". Only the stamp move and the successor
 * delete create it. {@code supersedeCollection} resolves the successor's id in its own statement and refuses an
 * unregistered target with a typed exception; the delete of a successor repairs, in its own transaction, every row
 * that still names it (by id, or by name before the stamp) and that stays.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionSupersedeIdTest {

    private static final String T = "ss-id";
    private static final String SVC_ROLE = "svc_ss_id";
    private static final String SVC_PASS = "svc_ss_id_pass";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    CatalogRepository repo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        repo = new CatalogRepository(new TenantScope(svcDs));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private <R> R su(Function<DSLContext, R> work) {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private static String name(String stem) {
        return "knowledge__" + stem + "__minilm-l6-v2-384__v1";
    }

    private void register(String... names) {
        su(ctx -> {
            for (String n : names) PgContainerHelper.insertCollection(ctx, T, n);
            return null;
        });
    }

    /** A registered collection that holds content: one document homed in it. */
    private void registerWithDocument(String collection) {
        register(collection);
        repo.upsertDocument(T, Map.of("tumbler", "doc-" + collection, "title", "holds content", "content_type", "paper",
            "corpus", "knowledge", "physical_collection", collection, "chunk_count", 0));
    }

    private Long id(String name) {
        return su(ctx -> ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID));
    }

    private Long supersededId(String name) {
        return su(ctx -> ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID));
    }

    private String supersededBy(String name) {
        return su(ctx -> ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_BY));
    }

    private boolean registered(String name) {
        return id(name) != null;
    }

    // ── supersedeCollection ───────────────────────────────────────────────────

    @Test
    void supersede_setsTheSuccessorsId_inTheSameStatement_andKeepsTheCas() {
        String a = name("s1-a"), b = name("s1-b"), c = name("s1-c");
        register(a, b, c);
        assertThat(repo.supersedeCollection(T, a, b, "")).isEqualTo(1);
        assertThat(supersededId(a)).isEqualTo(id(b));
        assertThat(repo.supersedeCollection(T, a, b, "")).as("the same target again is idempotent").isEqualTo(1);
        assertThat(repo.supersedeCollection(T, a, c, "")).as("a different target is refused by the CAS").isZero();
        assertThat(supersededBy(a)).isEqualTo(b);
        assertThat(supersededId(a)).isEqualTo(id(b));
    }

    @Test
    void supersede_toAnUnregisteredTarget_isRefusedWithATypedException_andWritesNothing() {
        String a = name("s2-a");
        register(a);
        assertThatThrownBy(() -> repo.supersedeCollection(T, a, name("s2-nowhere"), ""))
            .isInstanceOf(UnregisteredCollectionException.class);
        assertThat(supersededBy(a)).isEmpty();
        assertThat(supersededId(a)).isNull();
    }

    // ── the successor delete ──────────────────────────────────────────────────

    @Test
    void deletingASuccessor_repairsTheTombstoneThatHoldsContent_andRemovesTheEmptyOne() {
        String a = name("d1-a"), b = name("d1-b"), c = name("d1-c");
        registerWithDocument(a);
        register(b, c);
        repo.supersedeCollection(T, a, b, "");
        repo.supersedeCollection(T, c, b, "");
        Long idA = id(a);

        Map<String, Integer> counts = repo.deleteCollection(T, b);

        assertThat(counts.get("catalog_collections")).as("the successor's own row").isEqualTo(1);
        assertThat(registered(b)).isFalse();
        assertThat(registered(c)).as("the empty tombstone goes with it (nexus-s80u9)").isFalse();
        assertThat(registered(a)).as("the tombstone that holds content stays").isTrue();
        assertThat(supersededId(a)).as("R7: it now reads retired, no successor: its own id").isEqualTo(idA);
        assertThat(supersededBy(a)).as("the name column is untouched").isEqualTo(b);
    }

    @Test
    void deletingTheEndOfAChainOfEmptyTombstones_removesAllOfThem() {
        String x1 = name("d2-1"), x2 = name("d2-2"), x3 = name("d2-3");
        register(x1, x2, x3);
        repo.supersedeCollection(T, x1, x2, "");
        repo.supersedeCollection(T, x2, x3, "");
        repo.deleteCollection(T, x3);
        assertThat(registered(x1)).isFalse();
        assertThat(registered(x2)).isFalse();
        assertThat(registered(x3)).isFalse();
    }

    @Test
    void deletingASuccessorUnderKeepQuarantine_keepsTheEmptyTombstoneAndRepairsIt() {
        String a = name("d3-a"), b = name("d3-b");
        register(a, b);
        repo.supersedeCollection(T, a, b, "");
        repo.deleteCollection(T, b, true);
        assertThat(registered(a)).as("kept: the name comes back").isTrue();
        assertThat(supersededId(a)).as("but it must not name the id that went").isEqualTo(id(a));
    }

    @Test
    void deletingASuccessor_repairsAPreStampTombstoneByName() {
        String a = name("d4-a"), b = name("d4-b");
        registerWithDocument(a);
        register(b);
        su(ctx -> {
            P31CheckReplay.lift(ctx, P31CheckReplay.COLLECTIONS_SUPERSEDED);
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, b)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(a)).execute();
            P31CheckReplay.restore(ctx, P31CheckReplay.COLLECTIONS_SUPERSEDED);
            return null;
        });
        assertThat(supersededId(a)).as("precondition: unstamped tombstone naming b").isNull();
        repo.deleteCollection(T, b);
        assertThat(supersededId(a)).isEqualTo(id(a));
    }

    @Test
    void theGhostSweep_deletingAGhostSuccessor_repairsTheTombstoneThatNamesIt() {
        // its own tenant: the sweep walks every registry row of the tenant it is given
        final String sweepTenant = "ss-id-sweep";
        String a = name("d5-a"), b = name("d5-b");
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, sweepTenant, a);
            PgContainerHelper.insertCollection(ctx, sweepTenant, b);
            return null;
        });
        repo.upsertDocument(sweepTenant, Map.of("tumbler", "doc-d5", "title", "holds content", "content_type", "paper",
            "corpus", "knowledge", "physical_collection", a, "chunk_count", 0));
        repo.supersedeCollection(sweepTenant, a, b, "");
        repo.sweepGhostsAndMarkDormant(sweepTenant);
        Long idA = su(ctx -> ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(sweepTenant)).and(CATALOG_COLLECTIONS.NAME.eq(a))
            .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID));
        Long supersededA = su(ctx -> ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(sweepTenant)).and(CATALOG_COLLECTIONS.NAME.eq(a))
            .fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID));
        boolean bGone = su(ctx -> ctx.fetchCount(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID.eq(sweepTenant)
            .and(CATALOG_COLLECTIONS.NAME.eq(b)))) == 0;
        assertThat(bGone).as("b holds nothing: a ghost, deleted by the sweep").isTrue();
        assertThat(supersededA).as("the tombstone that holds content now reads retired, no successor").isEqualTo(idA);
    }

    // ── the superseded_by invariant: a row of the named successor exists => its id, else the self id ─────────

    private void assertInvariant(String when) {
        int violations = su(ctx -> CollectionTombstones.invariantViolations(ctx, T));
        assertThat(violations)
            .as("%s: catalog_collections rows that break the superseded_by invariant", when).isZero();
    }

    /** An owner of its own per name: two live collections on one tuple are refused by a named upsert and an import (RDR-229 P3.8). */
    private Map<String, Object> reg(String name) {
        return Map.of("name", name, "content_type", "knowledge", "owner_id", "ss-" + name, "embedding_model", "minilm-l6-v2-384",
            "model_version", "v1");
    }

    @Test
    void theParityCheck_countsAnUnstampedTombstone_andAStaleSelfId() {
        String a = name("p1-a"), b = name("p1-b");
        register(a, b);
        su(ctx -> {
            P31CheckReplay.lift(ctx, P31CheckReplay.COLLECTIONS_SUPERSEDED);
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, b)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(a)).execute();
            return null;
        });
        int unstamped = su(ctx -> CollectionTombstones.invariantViolations(ctx, T));
        assertThat(unstamped).as("unstamped tombstone").isEqualTo(1);
        su(ctx -> {
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, CATALOG_COLLECTIONS.COLLECTION_ID)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(a)).execute();
            return null;
        });
        int stale = su(ctx -> CollectionTombstones.invariantViolations(ctx, T));
        assertThat(stale).as("self id while the successor row exists").isEqualTo(1);
        su(ctx -> {
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, id(b))
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(a)).execute();
            P31CheckReplay.restore(ctx, P31CheckReplay.COLLECTIONS_SUPERSEDED);
            return null;
        });
        assertInvariant("repaired by hand");
    }

    @Test
    void aPlainSuccessorDelete_keepsTheSelfId_untilTheNameIsRegisteredAgain() {
        String a = name("p2-a"), b = name("p2-b");
        registerWithDocument(a);
        register(b);
        repo.supersedeCollection(T, a, b, "");
        repo.deleteCollection(T, b);
        assertThat(supersededId(a)).isEqualTo(id(a));
        assertInvariant("after a plain successor delete");
    }

    @Test
    void aKeepQuarantineReindex_repointsTheTombstoneAtTheReRegisteredRow() {
        String a = name("p3-a"), b = name("p3-b");
        register(a, b);
        repo.supersedeCollection(T, a, b, "");
        Long oldB = id(b);
        repo.deleteCollection(T, b, true);
        assertThat(supersededId(a)).as("(i) the delete gives the survivor its self id").isEqualTo(id(a));
        repo.upsertCollection(T, reg(b));
        assertThat(id(b)).as("a new registry row, a new id").isNotEqualTo(oldB);
        assertThat(supersededId(a)).as("(ii) the registration re-points it").isEqualTo(id(b));
        assertInvariant("after the reindex");
    }

    @Test
    void importingTheNameAgain_repointsTheTombstones_includingOneInAnEarlierChunkOfTheBatch() {
        String a = name("p4-a"), b = name("p4-b");
        registerWithDocument(a);
        register(b);
        repo.supersedeCollection(T, a, b, "");
        repo.deleteCollection(T, b);
        assertThat(supersededId(a)).isEqualTo(id(a));
        repo.importCollectionsBatch(T, java.util.List.of(reg(b)));
        assertThat(supersededId(a)).isEqualTo(id(b));
        String c = name("p4-c"), d = name("p4-d");
        registerWithDocument(c);
        register(d);
        repo.supersedeCollection(T, c, d, "");
        repo.deleteCollection(T, d);
        repo.importCollection(T, reg(d));
        assertThat(supersededId(c)).isEqualTo(id(d));
        assertInvariant("after the imports");
    }

    @Test
    void aRenameOntoANameATombstoneNames_repointsTheTombstoneAtTheNewRow() {
        String t = name("p5-t"), gone = name("p5-gone"), x = name("p5-x");
        registerWithDocument(t);
        register(gone, x);
        repo.supersedeCollection(T, t, gone, "");
        repo.deleteCollection(T, gone);
        assertThat(supersededId(t)).isEqualTo(id(t));
        repo.renameCollection(T, x, gone);
        assertThat(supersededId(t)).as("the rename's new row is the name t names").isEqualTo(id(gone));
        assertInvariant("after the rename");
    }
}
