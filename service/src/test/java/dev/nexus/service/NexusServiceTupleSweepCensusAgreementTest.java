// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TupleRepository;
import dev.nexus.service.tuples.TemplateRegistry;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-d94qm (shakeout F5): the census ({@code expired_unpurged}) and the purge share one predicate. One
 * scheduled tuple sweep pass, run as the production role (nexus_svc, FORCE RLS), leaves no row the census counts
 * as expired in any enrolled tenant, including lapsed directory leases and short-ttl board posts. So lingering
 * expired rows in the cloud mean no pass ran (the schedule, pinned by {@link NexusServiceTupleSweepScheduleTest}),
 * not a predicate gap.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class NexusServiceTupleSweepCensusAgreementTest {

    private static final Logger log = LoggerFactory.getLogger(NexusServiceTupleSweepCensusAgreementTest.class);
    private static final String TOKEN = "tuple-sweep-census-repro-token-1c4e";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    NexusService service;
    TupleRepository repo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection c = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(c);
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(6);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        TemplateRegistry registry = TemplateRegistry.loadAtBoot(null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L);
        service = new NexusService(0, TOKEN, svcDs, null, null, null, null, registry);
        repo = new TupleRepository(new TenantScope(svcDs), registry);
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) {
            try {
                service.stop();
            } catch (Exception ignored) {
                // never started
            }
        }
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static long expiredOf(List<TupleRepository.SubspaceCensus> cs) {
        return cs.stream().mapToLong(TupleRepository.SubspaceCensus::expiredUnpurged).sum();
    }

    private static String render(String tenant, List<TupleRepository.SubspaceCensus> cs) {
        StringBuilder sb = new StringBuilder();
        for (var c : cs) {
            sb.append(String.format("  tenant=%s subspace=%s total=%d consumed=%d expired_unpurged=%d%n",
                    tenant, c.subspace(), c.total(), c.consumed(), c.expiredUnpurged()));
        }
        return sb.toString();
    }

    @Test
    void oneSweepPassLeavesNoExpiredUnpurgedRowInAnyEnrolledTenant() throws Exception {
        String a = "tenant-a";
        String b = "tenant-b";

        // Tenant A: a directory lease that lapses (ttl 1), a heartbeated directory lease (ttl 300, re-sent),
        // board/ci posts: 5 short-ttl, 5 default.
        repo.out(a, "directory/nexus_4c7", Map.of("name", "nexus_4c7"), Map.of("session_id", "s-dead"),
                null, "n-dead", 1L);
        repo.out(a, "directory/nexus_live", Map.of("name", "nexus_live"), Map.of("session_id", "s-live"),
                null, "n-live", 300L);
        for (int i = 0; i < 5; i++) {
            repo.out(a, "board/ci/nexus-develop", Map.of("topic", "nexus-develop"), Map.of("from", "gh"),
                    "q" + i, "a-short-" + i, 1L);
            repo.out(a, "board/ci/nexus-develop", Map.of("topic", "nexus-develop"), Map.of("from", "gh"),
                    "d" + i, "a-long-" + i, null);
        }
        // Tenant B: 5 short-ttl board/ci posts.
        for (int i = 0; i < 5; i++) {
            repo.out(b, "board/ci/nexus-develop", Map.of("topic", "nexus-develop"), Map.of("from", "gh"),
                    "q" + i, "b-short-" + i, 1L);
        }
        Thread.sleep(2_500);
        // Heartbeat re-send of the live lease: same id, expires_at moves, created_at does not.
        repo.out(a, "directory/nexus_live", Map.of("name", "nexus_live"), Map.of("session_id", "s-live"),
                null, "n-live", 300L);

        var beforeA = repo.subspaceList(a, null);
        var beforeB = repo.subspaceList(b, null);
        log.info("census BEFORE sweep:\n{}{}", render(a, beforeA), render(b, beforeB));

        NexusService.TupleSweepRunResult r = service.runScheduledTupleSweep(OffsetDateTime.now(ZoneOffset.UTC));
        log.info("sweep: tenants_visited={} purged={} purge_examined={} cause={}",
                r.tenantsVisited(), r.purged(), r.purgeExamined(), r.incompleteCause());

        var afterA = repo.subspaceList(a, null);
        var afterB = repo.subspaceList(b, null);
        log.info("census AFTER sweep:\n{}{}", render(a, afterA), render(b, afterB));

        assertThat(expiredOf(beforeA) + expiredOf(beforeB)).as("seeded expired rows").isEqualTo(11);
        assertThat(r.tenantsVisited()).isEqualTo(2);
        assertThat(r.purgeExamined()).isEqualTo(11);
        assertThat(r.purged()).isEqualTo(11);
        assertThat(expiredOf(afterA)).as("tenant A expired_unpurged after sweep").isZero();
        assertThat(expiredOf(afterB)).as("tenant B expired_unpurged after sweep").isZero();
        // The heartbeated lease is live: one row, not purged.
        assertThat(afterA.stream().filter(c -> c.subspace().equals("directory/nexus_live")).findFirst())
                .hasValueSatisfying(c -> assertThat(c.total()).isEqualTo(1));
    }
}
