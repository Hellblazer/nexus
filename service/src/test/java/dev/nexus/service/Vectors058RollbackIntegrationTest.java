// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Map;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * vectors-058 rollback vs vectors-057's gate (nexus-bbtc2). vectors-058-1 gives the three text-gate probes a third
 * statement for {@code nexus.lexical_trgm = 'substring'}, keeping the posture vectors-031-2 chose; vectors-058-2 makes
 * the ten LANGUAGE sql row-gating functions read the same GUC. Rolling back through vectors-058-1 (vectors-058-2 and the
 * runAlways grants after it included) must leave every function in schema nexus as a database migrated only to
 * vectors-057 has it ({@link ChangesetRollbackSteps}).
 *
 * <p>This class holds the definer posture (two-role walk, an owner that is not nexus_svc). The invoker posture runs in
 * {@code TextGateProbeSingleRoleGuardIntegrationTest} through {@link #assertRollbackRestoresVectors057Gate}, for the
 * reason {@link Vectors057RollbackIntegrationTest} gives.
 */
public class Vectors058RollbackIntegrationTest {

    private static final String ADMIN_ROLE = "nexus_admin_vectors058rollback";
    private static final String ADMIN_PASS = "nexus_admin_vectors058rollback_pass";

    static final List<String> PROBES = List.of("text_gate_probe_384", "text_gate_probe_768", "text_gate_probe_1024");

    /** The ten LANGUAGE sql functions vectors-058-2 changes. */
    static final List<String> ROW_GATES = Stream.concat(
            Stream.of("text_gated_search_hnsw_first", "text_gated_search_allowed", "manifest_search")
                .flatMap(b -> Stream.of(b + "_384", b + "_768", b + "_1024")),
            Stream.of("manifest_qualifying_hits"))
        .toList();

    @Test
    void vectors058Rollback_restoresVectors057sGate_inTheDefinerPosture() throws Exception {
        PostgreSQLContainer<?> rolledPg = null;
        PostgreSQLContainer<?> baselinePg = null;
        try {
            rolledPg = PgContainerHelper.startEmptyDatabase();
            baselinePg = PgContainerHelper.startEmptyDatabase();
            try (Connection su = rolledPg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            try (Connection su = baselinePg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            assertRollbackRestoresVectors057Gate(rolledPg, baselinePg,
                Vectors058RollbackIntegrationTest::newAdminPool, true, ADMIN_ROLE);
        } finally {
            try {
                if (baselinePg != null) baselinePg.stop();
            } finally {
                if (rolledPg != null) rolledPg.stop();
            }
        }
    }

    /**
     * The comparison of {@link ChangesetRollbackSteps#rollBackAndCompare} through vectors-058-1, then the named shape on
     * each side, so the comparison cannot pass with both sides wrong the same way: forward, the probes hold the
     * {@code 'substring'} branch and the ten row gates read the GUC; rolled back, the probes keep vectors-057's two
     * statements and posture and the ten row gates no longer read it and are still inlinable.
     */
    public static void assertRollbackRestoresVectors057Gate(
            PostgreSQLContainer<?> rolledPg, PostgreSQLContainer<?> baselinePg, ChangesetRollbackSteps.PoolOpener pools,
            boolean definer, String owner) throws Exception {
        String posture = definer ? "SECURITY DEFINER" : "SECURITY INVOKER";
        var rolledBack = ChangesetRollbackSteps.rollBackAndCompare(rolledPg, baselinePg, pools, "vectors-058-1",
            all -> {
                var probes = named(all, PROBES);
                assertThat(probes).as("forward: the three probes, one signature each").hasSize(3);
                for (var f : probes) {
                    assertThat(f.body()).as("forward: %s holds the substring branch (non-vacuity)", f.identity())
                        .contains("'substring'").contains("~~*");
                    assertThat(f.securityDefiner()).as("forward: %s is %s", f.identity(), posture).isEqualTo(definer);
                }
                var gates = named(all, ROW_GATES);
                assertThat(gates).as("forward: the ten row gates, one signature each").hasSize(10);
                for (var f : gates) {
                    assertThat(f.body()).as("forward: %s reads the switch (non-vacuity)", f.identity())
                        .contains("nexus.lexical_trgm").contains("~~*");
                    assertThat(f.language()).as("forward: %s stays LANGUAGE sql", f.identity()).isEqualTo("sql");
                    assertThat(f.config()).as("forward: %s carries no SET clause (inlinable)", f.identity()).isNull();
                }
            });

        var probes = named(rolledBack, PROBES);
        assertThat(probes).as("the three probes survive the rollback, one signature each").hasSize(3);
        for (var f : probes) {
            assertThat(f.body()).as("rolled back: %s is vectors-057's two statements", f.identity())
                .contains("nexus.lexical_trgm").doesNotContain("'substring'").doesNotContain("~~*");
            assertThat(f.language()).as("rolled back: %s stays plpgsql", f.identity()).isEqualTo("plpgsql");
            assertThat(f.securityDefiner()).as("rolled back: %s keeps %s", f.identity(), posture).isEqualTo(definer);
            assertThat(f.owner()).as("rolled back: %s keeps its owner", f.identity()).isEqualTo(owner);
            assertThat(f.acl()).as("rolled back: %s keeps nexus_svc's EXECUTE grant", f.identity()).contains("nexus_svc=X/");
        }
        var gates = named(rolledBack, ROW_GATES);
        assertThat(gates).as("the ten row gates survive the rollback, one signature each").hasSize(10);
        for (var f : gates) {
            assertThat(f.body()).as("rolled back: %s no longer reads the switch", f.identity())
                .doesNotContain("lexical_trgm").contains("OPERATOR(nexus.<%)");
            assertThat(f.language()).as("rolled back: %s is LANGUAGE sql", f.identity()).isEqualTo("sql");
            assertThat(f.securityDefiner()).as("rolled back: %s is SECURITY INVOKER", f.identity()).isFalse();
            assertThat(f.config()).as("rolled back: %s carries no SET clause", f.identity()).isNull();
            assertThat(f.owner()).as("rolled back: %s keeps its owner", f.identity()).isEqualTo(owner);
        }
    }

    private static List<PgCatalogProbes.FunctionDefinition> named(
            Map<String, PgCatalogProbes.FunctionDefinition> all, List<String> names) {
        return all.values().stream()
            .filter(f -> names.stream().anyMatch(n -> f.identity().startsWith(n + "("))).toList();
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
