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

import static org.assertj.core.api.Assertions.assertThat;

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

    private static final String ADMIN_ROLE = "nexus_admin_vectors057rollback";
    private static final String ADMIN_PASS = "nexus_admin_vectors057rollback_pass";

    private static final List<String> TEXT_GATE_PROBES =
        List.of("text_gate_probe_384", "text_gate_probe_768", "text_gate_probe_1024");

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
            PostgreSQLContainer<?> rolledPg, PostgreSQLContainer<?> baselinePg, ChangesetRollbackSteps.PoolOpener pools,
            boolean definer, String owner) throws Exception {
        String posture = definer ? "SECURITY DEFINER" : "SECURITY INVOKER";
        var rolledBack = ChangesetRollbackSteps.rollBackAndCompare(rolledPg, baselinePg, pools, "vectors-057-1",
            all -> {
                var forward = probes(all);
                assertThat(forward.keySet().stream().map(k -> k.substring(0, k.indexOf('('))).toList())
                    .as("forward: the three probes, one signature each").containsExactlyInAnyOrderElementsOf(TEXT_GATE_PROBES);
                for (var f : forward.values()) {
                    assertThat(f.securityDefiner()).as("forward: %s is %s", f.identity(), posture).isEqualTo(definer);
                    assertThat(f.language()).as("forward: %s is plpgsql since vectors-057", f.identity()).isEqualTo("plpgsql");
                    assertThat(f.body()).as("forward: %s reads the switch (non-vacuity: the rollback has something to undo)",
                        f.identity()).contains("nexus.lexical_trgm");
                }
            });

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

    private static Map<String, PgCatalogProbes.FunctionDefinition> probes(
            Map<String, PgCatalogProbes.FunctionDefinition> all) {
        var out = new java.util.TreeMap<String, PgCatalogProbes.FunctionDefinition>();
        all.forEach((k, v) -> {
            if (TEXT_GATE_PROBES.stream().anyMatch(n -> k.startsWith(n + "("))) out.put(k, v);
        });
        return out;
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
