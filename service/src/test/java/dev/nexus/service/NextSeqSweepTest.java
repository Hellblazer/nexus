// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import org.junit.jupiter.api.*;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-0ehwe item 5: a one-shot sweep that floors {@code next_seq} for EVERY
 * owner in a tenant to its own high-water mark, without waiting for the
 * owner's next registration to self-heal it.
 *
 * <p><strong>Why this exists on top of the already-shipped claim-time
 * self-heal</strong> ({@link NextSeqSelfHealingTest}). {@code claimNextSeq}
 * floors a drifted owner the moment it is next WRITTEN to — but an owner that
 * is never written to again stays drifted forever, invisibly, and the doctor
 * check ({@code _check_next_seq_drift} in {@code src/nexus/health.py}) can
 * only ever REPORT that, not fix it (no converge verb existed for it). This
 * sweep is that converge verb: it uses the exact same floor primitive
 * ({@code max(next_seq, high_water)}, monotonic, tombstone-inclusive) but
 * applies it to every owner in the tenant in one pass, and reports precisely
 * which owners were actually below their high-water mark so the blast radius
 * of a drift incident is KNOWN rather than guessed (nexus-pbawi's owner 1.12
 * was found only by an operator manually suspecting it).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class NextSeqSweepTest {

    private static final String TENANT = "nextseq-sweep-tenant";
    private static final String SVC_ROLE = "svc_nextseq_sweep";
    private static final String SVC_PASS = "svc_nextseq_sweep_pass";

    PostgreSQLContainer<?> pg;
    CatalogRepository repo;
    com.zaxxer.hikari.HikariDataSource svcDs;

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
        config.setMaximumPoolSize(4);
        config.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(config);
        repo = new CatalogRepository(new TenantScope(svcDs));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private String owner(String prefix, String name) {
        repo.upsertOwner(TENANT, Map.of(
            "tumbler_prefix", prefix, "name", name, "owner_type", "repo"));
        return prefix;
    }

    /** Drive next_seq BELOW its high-water mark, as runtime drift does. */
    private void driftNextSeq(String prefix, long value) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            try (var ps = su.prepareStatement(
                "UPDATE nexus.catalog_owners SET next_seq = ? WHERE tenant_id = ? AND tumbler_prefix = ?")) {
                ps.setLong(1, value);
                ps.setString(2, TENANT);
                ps.setString(3, prefix);
                assertThat(ps.executeUpdate()).isEqualTo(1);
            }
        }
    }

    @SuppressWarnings("unchecked")
    private List<Map<String, Object>> healedOwners(Map<String, Object> report) {
        return (List<Map<String, Object>>) report.get("owners");
    }

    @Test
    void driftedOwner_isFlooredAndReported() throws Exception {
        String p = owner("8001", "sweep-drifted");
        repo.registerDocument(TENANT, p, Map.of(
            "title", "a", "content_type", "code", "source_uri", "file:///sweep1/a.md"));
        driftNextSeq(p, 0);   // counter now BEHIND its own child (highest child = 1)

        Map<String, Object> report = repo.sweepNextSeqDrift(TENANT);

        var healed = healedOwners(report);
        var mine = healed.stream().filter(h -> p.equals(h.get("tumbler_prefix"))).findFirst();
        assertThat(mine).as("the drifted owner must appear in the healed list").isPresent();
        assertThat(((Number) mine.get().get("next_seq")).longValue()).isEqualTo(0L);
        assertThat(((Number) mine.get().get("high_water")).longValue()).isEqualTo(1L);
        assertThat(((Number) mine.get().get("floored_to")).longValue()).isEqualTo(1L);

        assertThat(((Number) repo.ownerByPrefix(TENANT, p).get("next_seq")).longValue())
            .as("the owner row itself must be floored, not just reported")
            .isEqualTo(1L);

        // A subsequent registration must now succeed with no drift-heal warning
        // (steady state restored).
        String next = repo.registerDocument(TENANT, p, Map.of(
            "title", "b", "content_type", "code", "source_uri", "file:///sweep1/b.md"));
        assertThat(next).isEqualTo("8001.2");
    }

    @Test
    void healthyOwner_isCheckedButNotHealed() throws Exception {
        // NON-VACUITY: the sweep must not perturb a normal, undrifted owner.
        String p = owner("8002", "sweep-healthy");
        repo.registerDocument(TENANT, p, Map.of(
            "title", "a", "content_type", "code", "source_uri", "file:///sweep2/a.md"));

        Map<String, Object> report = repo.sweepNextSeqDrift(TENANT);

        var healed = healedOwners(report);
        assertThat(healed.stream().anyMatch(h -> p.equals(h.get("tumbler_prefix"))))
            .as("a healthy owner must not be reported as healed")
            .isFalse();
        assertThat(((Number) report.get("checked")).intValue())
            .as("checked must count every owner, healthy or not")
            .isGreaterThanOrEqualTo(1);
        assertThat(((Number) repo.ownerByPrefix(TENANT, p).get("next_seq")).longValue())
            .as("a healthy owner's next_seq must be untouched")
            .isEqualTo(1L);
    }

    @Test
    void tombstonedChildren_countTowardTheFloor() throws Exception {
        String p = owner("8003", "sweep-tombstoned");
        String t1 = repo.registerDocument(TENANT, p, Map.of(
            "title", "a", "content_type", "code", "source_uri", "file:///sweep3/a.md"));
        assertThat(repo.deleteDocument(TENANT, t1)).isEqualTo(1);
        driftNextSeq(p, 0);

        Map<String, Object> report = repo.sweepNextSeqDrift(TENANT);
        var mine = healedOwners(report).stream()
            .filter(h -> p.equals(h.get("tumbler_prefix"))).findFirst();
        assertThat(mine)
            .as("a tombstoned child's tumbler must still count toward the high-water mark")
            .isPresent();
        assertThat(((Number) mine.get().get("high_water")).longValue()).isEqualTo(1L);
    }

    @Test
    void multipleOwners_onlyDriftedOnesAreReported() throws Exception {
        String healthy = owner("8004", "sweep-multi-healthy");
        repo.registerDocument(TENANT, healthy, Map.of(
            "title", "a", "content_type", "code", "source_uri", "file:///sweep4/a.md"));

        String drifted1 = owner("8005", "sweep-multi-drifted-1");
        repo.registerDocument(TENANT, drifted1, Map.of(
            "title", "a", "content_type", "code", "source_uri", "file:///sweep5/a.md"));
        driftNextSeq(drifted1, 0);

        String drifted2 = owner("8006", "sweep-multi-drifted-2");
        repo.registerDocument(TENANT, drifted2, Map.of(
            "title", "a", "content_type", "code", "source_uri", "file:///sweep6/a.md"));
        repo.registerDocument(TENANT, drifted2, Map.of(
            "title", "b", "content_type", "code", "source_uri", "file:///sweep6/b.md"));
        driftNextSeq(drifted2, 0);

        Map<String, Object> report = repo.sweepNextSeqDrift(TENANT);
        var healed = healedOwners(report);
        var healedPrefixes = healed.stream().map(h -> (String) h.get("tumbler_prefix")).toList();

        assertThat(healedPrefixes).contains(drifted1, drifted2);
        assertThat(healedPrefixes).doesNotContain(healthy);
    }

    @Test
    void sweepIsIdempotent_secondRunHealsNothing() throws Exception {
        String p = owner("8007", "sweep-idempotent");
        repo.registerDocument(TENANT, p, Map.of(
            "title", "a", "content_type", "code", "source_uri", "file:///sweep7/a.md"));
        driftNextSeq(p, 0);

        Map<String, Object> first = repo.sweepNextSeqDrift(TENANT);
        assertThat(healedOwners(first).stream().anyMatch(h -> p.equals(h.get("tumbler_prefix")))).isTrue();

        Map<String, Object> second = repo.sweepNextSeqDrift(TENANT);
        assertThat(healedOwners(second).stream().anyMatch(h -> p.equals(h.get("tumbler_prefix"))))
            .as("a floored owner must not be reported as healed again")
            .isFalse();
    }
}
