// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.SchemaMigrator;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17), fix round A, critique B3: the single-role posture. The migrating role IS {@code nexus_svc}
 * (a supported BYO posture, RDR-246), so vectors-065 skips {@code catalog_collections_owner_read} and the migrating role's
 * reads of the FORCE row-secured registry see no row (the tenant GUC is unset). vectors-068-19's VALIDATE scans every row
 * whatever the policies say, so with its guard reading zero rows it ran on any pre-P3.1 tombstone, raised 23514 and rolled
 * the changeset back: the engine did not start, every boot.
 *
 * <p>The fix is that the VALIDATE never fails the changeset ({@code check_violation} is trapped and the CHECK stays NOT
 * VALID for the stamp's {@code on_complete}). The precondition is blind in that posture too (it counts no tombstone); the
 * changeset says deliberately that it treats "cannot see" as clean, because D2's repair move and every stamp are blocked
 * there by the row-security refusal in MovePreflight, so no UPDATE can meet the CHECK.
 */
class Rdr229P31SingleRoleSupersededByCheckTest {

    private static final String SVC = PgContainerHelper.SVC_USERNAME;
    private static final String SVC_PASSWORD = PgContainerHelper.SVC_PASSWORD;
    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    private static final String FIRST = "vectors-068-1";
    private static final String CHECK_CHANGESET = "vectors-068-19";
    private static final String CHECK = "catalog_collections_superseded_by_id_check";
    private static final String TENANT = "p31-single-role";

    @Test
    void aRenamedCollectionsTombstoneDoesNotWedgeTheBoot_andTheCheckEndsNotValid() throws Exception {
        // a container of its own: nexus_svc IS the migrating role here, and the shared cluster holds that role already (it cannot be
        // declared exclusive and dropped), the same reason TextGateProbeSingleRoleGuardIntegrationTest takes startDedicated
        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, SVC, SVC_PASSWORD);   // nexus_svc IS the migrating role
            }
            var cfg = new HikariConfig();
            cfg.setJdbcUrl(pg.getJdbcUrl());
            cfg.setUsername(SVC);
            cfg.setPassword(SVC_PASSWORD);
            cfg.setMaximumPoolSize(2);
            try (HikariDataSource svcPool = new HikariDataSource(cfg)) {
                migrateUpTo(svcPool, FIRST);
                try (Connection su = pg.createConnection("")) {
                    // as the superuser, which row security does not bind: a store from before 0.1.200 with one renamed collection
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    PgContainerHelper.insertCollection(ctx, TENANT, "live-one");
                    PgContainerHelper.insertCollection(ctx, TENANT, "tomb-renamed");
                    ctx.update(DSL.table(DSL.name("nexus", "catalog_collections")))
                        .set(DSL.field(DSL.name("superseded_by"), String.class), "successor-gone")
                        .set(DSL.field(DSL.name("model_version"), String.class), "v1")
                        .where(DSL.field(DSL.name("name"), String.class).eq("tomb-renamed")).execute();
                    assertThat(ctx.fetchCount(DSL.table(DSL.name("nexus", "catalog_collections")),
                        DSL.field(DSL.name("superseded_by"), String.class).ne(""))).as("non-vacuity: one tombstone seeded").isEqualTo(1);
                }
                try (Connection c = svcPool.getConnection()) {
                    assertThat(DSL.using(c, SQLDialect.POSTGRES).fetchCount(DSL.table(DSL.name("nexus", "catalog_collections"))))
                        .as("non-vacuity: the migrating role reads no registry row (no owner-read policy in this posture)").isZero();
                }

                SchemaMigrator.migrate(svcPool);   // before the fix: 23514 from the VALIDATE, the engine does not start

                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    assertThat(PgCatalogProbes.constraintCount(ctx, CHECK)).as("the CHECK landed").isEqualTo(1);
                    assertThat(PgCatalogProbes.constraintValidated(ctx, CHECK)).as("and stays NOT VALID for the stamp's on_complete").isFalse();
                    assertThat(ctx.fetchExists(DSL.selectOne().from(DSL.table(DSL.name("public", "databasechangelog")))
                        .where(DSL.field(DSL.name("id"), String.class).eq(CHECK_CHANGESET)))).as("the changeset ran").isTrue();
                }
            }
        } finally {
            pg.stop();
        }
    }

    private static void migrateUpTo(HikariDataSource ds, String changesetId) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                List<liquibase.changelog.ChangeSet> unrun = liquibase.listUnrunChangeSets(new Contexts(), new LabelExpression());
                int idx = -1;
                for (int i = 0; i < unrun.size(); i++) {
                    if (changesetId.equals(unrun.get(i).getId())) {
                        idx = i;
                        break;
                    }
                }
                assertThat(idx).as("%s must be pending in the HEAD changelog", changesetId).isGreaterThanOrEqualTo(0);
                liquibase.update(idx, new Contexts(), new LabelExpression());
            }
        }
    }
}
