// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

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
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.Map;
import java.util.function.Consumer;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;

/**
 * One changeset's rollback, checked against a database that never applied it: migrate {@code rolledPg} fully, roll it
 * back through the named changeset (with everything after it), migrate {@code baselinePg} to exactly the changeset
 * before it, and require every function in schema nexus to match in arguments, result, body, definition, volatility,
 * language, SECURITY DEFINER, SET clauses, owner and ACL. Shared by {@link Vectors057RollbackIntegrationTest} and
 * {@link Vectors058RollbackIntegrationTest}; the full round trip in {@link SchemaRollbackRoundTripIntegrationTest}
 * drops schema nexus and so cannot see whether a function body was put back.
 */
public final class ChangesetRollbackSteps {

    private static final String MASTER_CHANGELOG_RELATIVE = "db/changelog/db.changelog-master.xml";

    private ChangesetRollbackSteps() {
    }

    /** Opens a pool as the migrating role of one posture on a given container. */
    public interface PoolOpener {
        HikariDataSource open(PostgreSQLContainer<?> pg, String poolName);
    }

    /**
     * Runs the comparison and returns the rolled-back database's functions, so the caller can name the posture it
     * expects (the comparison alone would pass with both sides wrong the same way). {@code forwardCheck} sees the fully
     * migrated functions before the rollback: the non-vacuity check that the rollback has something to undo.
     */
    public static Map<String, PgCatalogProbes.FunctionDefinition> rollBackAndCompare(
            PostgreSQLContainer<?> rolledPg, PostgreSQLContainer<?> baselinePg, PoolOpener pools, String changesetId,
            Consumer<Map<String, PgCatalogProbes.FunctionDefinition>> forwardCheck) throws Exception {
        int precedingRows;
        Map<String, PgCatalogProbes.FunctionDefinition> rolledBack;
        try (HikariDataSource ds = pools.open(rolledPg, "nexus-rollback-" + changesetId)) {
            SchemaMigrator.migrate(ds);
            try (Connection c = ds.getConnection()) {
                forwardCheck.accept(PgCatalogProbes.functionDefinitionsIn(dsl(c), "nexus"));
                int depth = rollbackDepthThrough(c, changesetId);
                assertThat(depth).as("%s must have executed, or the rollback below is a no-op", changesetId)
                    .isGreaterThan(0);
                precedingRows = changelogRowCount(c) - depth;
                assertThatCode(() -> rollback(ds, depth)).doesNotThrowAnyException();
            }
            try (Connection c = ds.getConnection()) {
                assertThat(changelogRowCount(c)).as("the rollback stops at %s's predecessor", changesetId)
                    .isEqualTo(precedingRows);
                rolledBack = PgCatalogProbes.functionDefinitionsIn(dsl(c), "nexus");
            }
        }

        Map<String, PgCatalogProbes.FunctionDefinition> baseline;
        try (HikariDataSource ds = pools.open(baselinePg, "nexus-baseline-before-" + changesetId)) {
            applyForward(ds, precedingRows);
            try (Connection c = ds.getConnection()) {
                assertThat(changelogRowCount(c))
                    .as("the baseline applied exactly the rows that precede %s", changesetId).isEqualTo(precedingRows);
                baseline = PgCatalogProbes.functionDefinitionsIn(dsl(c), "nexus");
            }
        }

        assertThat(rolledBack.keySet()).as("the rollback restored exactly the functions that existed before %s", changesetId)
            .containsExactlyElementsOf(baseline.keySet());
        for (var entry : baseline.entrySet()) {
            var want = entry.getValue();
            var got = rolledBack.get(entry.getKey());
            String id = entry.getKey();
            assertThat(got.arguments()).as("%s: argument list", id).isEqualTo(want.arguments());
            assertThat(got.result()).as("%s: return type", id).isEqualTo(want.result());
            assertThat(got.body()).as("%s: body", id).isEqualTo(want.body());
            assertThat(got.definition()).as("%s: pg_get_functiondef", id).isEqualTo(want.definition());
            assertThat(got.volatility()).as("%s: volatility", id).isEqualTo(want.volatility());
            assertThat(got.language()).as("%s: language", id).isEqualTo(want.language());
            assertThat(got.securityDefiner()).as("%s: SECURITY DEFINER", id).isEqualTo(want.securityDefiner());
            assertThat(got.config()).as("%s: SET clauses", id).isEqualTo(want.config());
            assertThat(got.owner()).as("%s: owner", id).isEqualTo(want.owner());
            assertThat(got.acl()).as("%s: ACL", id).isEqualTo(want.acl());
        }
        return rolledBack;
    }

    private static DSLContext dsl(Connection c) {
        return DSL.using(c, SQLDialect.POSTGRES);
    }

    /** How many trailing (by execution order) DATABASECHANGELOG rows reach {@code changesetId}. */
    private static int rollbackDepthThrough(Connection c, String changesetId) {
        Field<Integer> orderExecuted = DSL.field(DSL.name("orderexecuted"), Integer.class);
        Field<String> id = DSL.field(DSL.name("id"), String.class);
        var targetOrder = dsl(c).select(orderExecuted).from(DSL.table(DSL.name("databasechangelog")))
            .where(id.eq(changesetId));
        return dsl(c).selectCount()
            .from(DSL.table(DSL.name("databasechangelog")))
            .where(orderExecuted.ge(targetOrder))
            .fetchOne(0, int.class);
    }

    private static int changelogRowCount(Connection c) {
        return dsl(c).selectCount().from(DSL.table(DSL.name("databasechangelog"))).fetchOne(0, int.class);
    }

    /** Rolls back the last {@code rows} DATABASECHANGELOG rows by execution order. */
    private static void rollback(HikariDataSource ds, int rows) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(
                    MASTER_CHANGELOG_RELATIVE,
                    new ClassLoaderResourceAccessor(),
                    database)) {
                liquibase.rollback(rows, new Contexts(), new LabelExpression());
            }
        }
    }

    /** Applies exactly the next {@code changesToApply} pending changesets in master order and stops. */
    private static void applyForward(HikariDataSource ds, int changesToApply) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(
                    MASTER_CHANGELOG_RELATIVE,
                    new ClassLoaderResourceAccessor(),
                    database)) {
                liquibase.update(changesToApply, new Contexts(), new LabelExpression());
            }
        }
    }
}
