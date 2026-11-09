// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

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
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;

/**
 * vectors-057 rollback vs vectors-031-2's probes (nexus-seo4k), split out of
 * {@link SchemaRollbackRoundTripIntegrationTest}, which sits at its file-size ceiling.
 *
 * <p>vectors-057-1 redefines the three text-gate probes as one plpgsql function holding two statements, keeping the
 * posture vectors-031-2 chose. Its rollback re-reads that posture and restores vectors-031-2's body for it. The full
 * round trip in {@link SchemaRollbackRoundTripIntegrationTest} drops schema nexus, so it cannot see whether the
 * rollback put those bodies back; this class rolls back through vectors-057-1 ALONE (and the runAlways grants after
 * it) and compares every function in schema nexus with a second database migrated only up to the changeset before it,
 * the oracle of that class's vectors-055 test: a database that never saw vectors-057, not the rolled-back one
 * compared with itself.
 *
 * <p>This class holds the definer posture (two-role walk, an owner that is not nexus_svc, where vectors-029's guard
 * holds and vectors-031-2 made the probes SECURITY DEFINER plpgsql with a pinned search_path). The invoker posture
 * (migrated as nexus_svc itself) runs in {@code TextGateProbeSingleRoleGuardIntegrationTest}, the one class that
 * creates nexus_svc as the owner on dedicated containers, through {@link #assertRollbackRestoresVectors031Probes}:
 * {@code TestRoleUniquenessScanTest} allows a role created by two classes only when each declares it exclusive on the
 * shared cluster, which nexus_svc, held by the template migration, cannot be.
 */
public class Vectors057RollbackIntegrationTest {

    private static final String MASTER_CHANGELOG_RELATIVE = "db/changelog/db.changelog-master.xml";

    private static final String ADMIN_ROLE = "nexus_admin_vectors057rollback";
    private static final String ADMIN_PASS = "nexus_admin_vectors057rollback_pass";

    private static final List<String> TEXT_GATE_PROBES =
        List.of("text_gate_probe_384", "text_gate_probe_768", "text_gate_probe_1024");

    /** Opens a pool as the migrating role of one posture on a given container. */
    public interface PoolOpener {
        HikariDataSource open(PostgreSQLContainer<?> pg, String poolName);
    }

    @Test
    void vectors057Rollback_restoresTheDefinerProbes_asVectors031Left_ownerAndGrantsKept() throws Exception {
        PostgreSQLContainer<?> rolledPg = null;
        PostgreSQLContainer<?> baselinePg = null;
        try {
            rolledPg = PgContainerHelper.startEmptyDatabase();
            baselinePg = PgContainerHelper.startEmptyDatabase();
            try (Connection su = rolledPg.createConnection("")) {
                dbaBootstrap(su);
            }
            try (Connection su = baselinePg.createConnection("")) {
                dbaBootstrap(su);
            }
            assertRollbackRestoresVectors031Probes(rolledPg, baselinePg,
                Vectors057RollbackIntegrationTest::newAdminPool, true, ADMIN_ROLE);
        } finally {
            try {
                if (baselinePg != null) baselinePg.stop();
            } finally {
                if (rolledPg != null) rolledPg.stop();
            }
        }
    }

    /**
     * Migrates {@code rolledPg} and {@code baselinePg} (both already provisioned for the posture) as the role
     * {@code pools} opens, rolls {@code rolledPg} back through vectors-057-1, walks {@code baselinePg} to the
     * changeset before it, and compares every function in schema nexus; then names the probes' posture
     * ({@code definer}: SECURITY DEFINER plpgsql with the search_path pin; otherwise SECURITY INVOKER LANGUAGE sql
     * with no SET clause) and owner, so the comparison cannot pass with both sides wrong the same way.
     */
    public static void assertRollbackRestoresVectors031Probes(
            PostgreSQLContainer<?> rolledPg, PostgreSQLContainer<?> baselinePg, PoolOpener pools,
            boolean definer, String owner) throws Exception {
        String posture = definer ? "SECURITY DEFINER" : "SECURITY INVOKER";
        int precedingRows;
        Map<String, PgCatalogProbes.FunctionDefinition> rolledBack;
        try (HikariDataSource ds = pools.open(rolledPg, "nexus-rollback-vectors057")) {
            SchemaMigrator.migrate(ds);
            try (Connection c = ds.getConnection()) {
                var forward = probeDefinitions(c);
                assertThat(forward.keySet().stream().map(k -> k.substring(0, k.indexOf('('))).toList())
                    .as("forward: the three probes, one signature each").containsExactlyInAnyOrderElementsOf(TEXT_GATE_PROBES);
                for (var f : forward.values()) {
                    assertThat(f.securityDefiner()).as("forward: %s is %s", f.identity(), posture).isEqualTo(definer);
                    assertThat(f.language()).as("forward: %s is plpgsql since vectors-057", f.identity()).isEqualTo("plpgsql");
                    assertThat(f.body()).as("forward: %s reads the switch (non-vacuity: the rollback has something to undo)",
                        f.identity()).contains("nexus.lexical_trgm");
                }
                int depth = rollbackDepthThrough(c, "vectors-057-1");
                assertThat(depth).as("vectors-057-1 must have executed, or the rollback below is a no-op").isGreaterThan(0);
                precedingRows = changelogRowCount(c) - depth;
                assertThatCode(() -> rollbackEverything(ds, depth)).doesNotThrowAnyException();
            }
            try (Connection c = ds.getConnection()) {
                assertThat(changelogRowCount(c)).as("the rollback stops at vectors-057-1's predecessor")
                    .isEqualTo(precedingRows);
                rolledBack = PgCatalogProbes.functionDefinitionsIn(dsl(c), "nexus");
            }
        }

        Map<String, PgCatalogProbes.FunctionDefinition> baseline;
        try (HikariDataSource ds = pools.open(baselinePg, "nexus-baseline-before-vectors057")) {
            reapplyForward(ds, precedingRows);
            try (Connection c = ds.getConnection()) {
                assertThat(changelogRowCount(c))
                    .as("the baseline applied exactly the rows that precede vectors-057-1").isEqualTo(precedingRows);
                baseline = PgCatalogProbes.functionDefinitionsIn(dsl(c), "nexus");
            }
        }

        assertThat(rolledBack.keySet()).as("the rollback restored exactly the functions that existed before vectors-057")
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

        // The comparison above would pass vacuously if both sides were wrong the same way; name the probes' posture.
        var probes = rolledBack.values().stream()
            .filter(f -> TEXT_GATE_PROBES.stream().anyMatch(n -> f.identity().startsWith(n + "("))).toList();
        assertThat(probes).as("the three probes survive the rollback, one signature each").hasSize(3);
        for (var f : probes) {
            assertThat(f.body()).as("rolled back: %s no longer reads the switch", f.identity())
                .doesNotContain("lexical_trgm");
            assertThat(f.securityDefiner()).as("rolled back: %s keeps %s", f.identity(), posture).isEqualTo(definer);
            assertThat(f.owner()).as("rolled back: %s keeps its owner", f.identity()).isEqualTo(owner);
            assertThat(f.acl()).as("rolled back: %s keeps nexus_svc's EXECUTE grant", f.identity())
                .contains("nexus_svc=X/");
            if (definer) {
                assertThat(f.language()).as("rolled back: %s stays plpgsql (vectors-031-2's definer body)", f.identity())
                    .isEqualTo("plpgsql");
                assertThat(f.config()).as("rolled back: %s keeps its search_path pin and plan_cache_mode", f.identity())
                    .contains("search_path=pg_catalog, pg_temp").contains("plan_cache_mode=force_custom_plan");
            } else {
                assertThat(f.language()).as("rolled back: %s is LANGUAGE sql again (inlinable)", f.identity())
                    .isEqualTo("sql");
                assertThat(f.config()).as("rolled back: %s carries no SET clause", f.identity()).isNull();
            }
        }
    }

    private static Map<String, PgCatalogProbes.FunctionDefinition> probeDefinitions(Connection c) {
        var all = PgCatalogProbes.functionDefinitionsIn(dsl(c), "nexus");
        var out = new java.util.TreeMap<String, PgCatalogProbes.FunctionDefinition>();
        all.forEach((k, v) -> {
            if (TEXT_GATE_PROBES.stream().anyMatch(n -> k.startsWith(n + "("))) out.put(k, v);
        });
        return out;
    }

    // ── Liquibase drive (the subset of SchemaRollbackRoundTripIntegrationTest's this class uses) ──────────────

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
    private static void rollbackEverything(HikariDataSource ds, int rows) throws Exception {
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
    private static void reapplyForward(HikariDataSource ds, int changesToApply) throws Exception {
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

    /** The DBA-side provisioning of an empty database on the shared cluster, as SchemaRollbackRoundTripIntegrationTest. */
    private static void dbaBootstrap(Connection su) throws Exception {
        PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
    }

    private static HikariDataSource newAdminPool(PostgreSQLContainer<?> pg, String poolName) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(ADMIN_ROLE);
        cfg.setPassword(ADMIN_PASS);
        cfg.setMaximumPoolSize(2);
        cfg.setPoolName(poolName);
        return new HikariDataSource(cfg);
    }
}
