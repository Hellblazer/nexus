// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CyclicBarrier;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-wtzzq (GH #1522): N first-time owner registrations released in
 * the same instant must each keep their own name and get a distinct prefix.
 *
 * <p>Before the per-tenant advisory lock in {@link CatalogRepository#upsertOwner},
 * two allocators computed the same max + 1; the second INSERT landed ON
 * CONFLICT (tenant_id, tumbler_prefix) DO UPDATE on the first's row and
 * renamed it, so one repo's name vanished ("server did not return prefix" on
 * the client). Eight threads on one barrier reproduce that shape with high
 * probability; with the lock the outcome is exact: eight owners, eight names,
 * eight distinct prefixes.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class OwnerRegistrationRaceTest {
    private static final String TENANT = "owner-race-tenant";
    private static final String SVC_ROLE = "svc_owner_race_test";
    private static final String SVC_PASS = "svc_owner_race_test_pass";
    private static final int N = 8;

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
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(N);
        config.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(config);
        repo = new CatalogRepository(new TenantScope(svcDs));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test
    void concurrentFirstTimeRegistrations_eachKeepTheirName_andGetDistinctPrefixes() throws Exception {
        CyclicBarrier gate = new CyclicBarrier(N);
        ExecutorService pool = Executors.newFixedThreadPool(N);
        try {
            List<Future<?>> futures = new ArrayList<>();
            for (int i = 0; i < N; i++) {
                final String name = "race-repo-" + i;
                futures.add(pool.submit(() -> {
                    gate.await(30, TimeUnit.SECONDS);
                    repo.upsertOwner(TENANT, Map.of(
                        "name", name, "owner_type", "repo",
                        "repo_hash", "hash-" + name, "repo_root", "/tmp/" + name));
                    return null;
                }));
            }
            for (Future<?> f : futures) {
                f.get(60, TimeUnit.SECONDS);
            }
        } finally {
            pool.shutdownNow();
        }

        var owners = repo.listOwners(TENANT);
        List<String> names = owners.stream().map(o -> (String) o.get("name")).sorted().toList();
        List<String> prefixes = owners.stream().map(o -> (String) o.get("tumbler_prefix")).toList();
        assertThat(names)
            .as("every registration keeps its own name; a lost name is the GH #1522 overwrite")
            .hasSize(N)
            .containsExactlyElementsOf(
                java.util.stream.IntStream.range(0, N).mapToObj(i -> "race-repo-" + i).sorted().toList());
        assertThat(prefixes).as("one distinct prefix per owner").doesNotHaveDuplicates().hasSize(N);
        assertThat(prefixes).allMatch(p -> p.matches("1\\.\\d+"));
    }
}
