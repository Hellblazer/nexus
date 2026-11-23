// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.datamove.Gate3;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
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
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.8 (nexus-p2m1x.28) on P3.1 stage 2c: {@link CollectionTombstones#supersede(DSLContext, String, String, String, long)},
 * the overload a rename retires its source through. A rename draws the new row's id first, supersedes the source with it, then
 * inserts the new row (the new row cannot be inserted live while the source holds its tuple), so at the supersede the successor
 * has no registry row for the other overload's subselect to read. The overload takes the id from the caller, keeps the CAS, and
 * skips the registry lookup.
 *
 * <p>As the engine's service role (NOBYPASSRLS) in the tenant's scope, the way a rename runs it.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionTombstonesSupersedeToIdTest {

    private static final String T = "ss-to-id";
    private static final String SVC_ROLE = "svc_ss_to_id";
    private static final String SVC_PASS = "svc_ss_to_id_pass";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(2);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        scope = new TenantScope(svcDs);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private <R> R su(Function<DSLContext, R> work) {
        try (Connection c = pg.createConnection("")) {
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private static String name(String stem) {
        return "knowledge__" + stem + "__minilm-l6-v2-384__v1";
    }

    private void register(String name) {
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, T, name);
            return null;
        });
    }

    private int supersedeToId(String from, String to, long toId) {
        Integer n = scope.withTenant(T, ctx -> CollectionTombstones.supersede(ctx, T, from, to, toId, Gate3.never()));
        return n;
    }

    private String supersededBy(String name) {
        return su(ctx -> ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_BY));
    }

    private Long supersededId(String name) {
        return su(ctx -> ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID));
    }

    @Test
    void supersedeToAnId_writesTheNameAndTheCallersId_forASuccessorWithNoRegistryRowYet() {
        String x = name("id-x"), y = name("id-y");
        register(x);

        int n = supersedeToId(x, y, 910_001L);

        assertThat(n).isEqualTo(1);
        assertThat(supersededBy(x)).isEqualTo(y);
        assertThat(supersededId(x)).as("the id is the caller's, not a registry lookup's").isEqualTo(910_001L);
    }

    @Test
    void theOtherOverload_stillRefusesAnUnregisteredTarget() {
        String x = name("ref-x"), y = name("ref-y");
        register(x);

        assertThatThrownBy(() -> scope.withTenant(T, ctx ->
                CollectionTombstones.supersede(ctx, T, x, y, DSL.currentOffsetDateTime(), Gate3.never())))
            .isInstanceOf(UnregisteredCollectionException.class);

        assertThat(supersededBy(x)).as("nothing was written").isEmpty();
    }

    @Test
    void supersedeToAnId_keepsTheCas_theSameTargetIsIdempotentAndAnotherTargetMatchesNoRow() {
        String x = name("cas-x"), y = name("cas-y"), z = name("cas-z");
        register(x);
        assertThat(supersedeToId(x, y, 910_002L)).isEqualTo(1);

        assertThat(supersedeToId(x, y, 910_002L)).as("the same target again").isEqualTo(1);
        assertThat(supersedeToId(x, z, 910_003L))
            .as("a different target is the CAS's zero rows, as for the other overload").isZero();

        assertThat(supersededBy(x)).isEqualTo(y);
        assertThat(supersededId(x)).isEqualTo(910_002L);
    }

    /**
     * tamsin's condition on the overload (RDR-229 P3.8 on P3.1, 2026-11-21): a rename whose retire CAS matches no row of
     * an X that exists (X was retired for a third collection while the rename ran) refuses, naming it, instead of a silent
     * zero that would leave Y inserted with nothing pointing at it. Nothing is renamed: X stays retired for Z, and no Y row
     * exists.
     */
    @Test
    void aRenameWhoseRetireMatchesNoRowOfAnExistingSourceRefusesAndRegistersNothing() {
        String x = name("lost-x"), y = name("lost-y"), z = name("lost-z");
        register(x);
        register(z);
        Integer retiredForZ = scope.withTenant(T, ctx -> CollectionTombstones.supersede(ctx, T, x, z, DSL.currentOffsetDateTime(), Gate3.never()));
        assertThat(retiredForZ).as("guard: X is already retired for Z").isEqualTo(1);

        assertThatThrownBy(() -> scope.withTenant(T, ctx -> {
            CollectionRename.retireAndRegister(ctx, T, x, y, CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                new java.util.HashMap<>(), new String[4], Gate3.never());
            return null;
        }))
            .isInstanceOf(CatalogRepository.CollectionMergeRefused.class)
            .hasMessageContaining(x)
            .hasMessageContaining("retired for another collection");

        assertThat(supersededBy(x)).as("X stays retired for Z").isEqualTo(z);
        Integer yRows = su(ctx -> ctx.fetchCount(CATALOG_COLLECTIONS,
                CATALOG_COLLECTIONS.TENANT_ID.eq(T).and(CATALOG_COLLECTIONS.NAME.eq(y))));
        assertThat(yRows).as("no row for Y was registered").isZero();
    }
}
