// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.*;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-0ehwe: tumbler allocation must SELF-HEAL past a drifted next_seq.
 *
 * <p>THE WEDGE. registerDocument claimed next_seq and inserted; the INSERT's
 * only ON CONFLICT arbiter is (tenant_id, source_uri), but the only unique key
 * on catalog_documents is (tenant_id, tumbler) — so a tumbler collision had no
 * arm and escaped as a bare 409. The next_seq increment shared the failing
 * transaction and rolled back WITH it, so the allocator never advanced: one
 * drifted owner was a PERMANENT, TOTAL outage for that owner that retry could
 * never clear (nexus-pbawi fixed one owner by hand).
 *
 * <p>The existing coverage only exercised the CUTOVER path
 * (test_next_seq_reconciled_no_tumbler_collision_post_cutover), which is
 * exactly why RUNTIME drift slipped through. These tests drift the counter at
 * RUNTIME.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class NextSeqSelfHealingTest {

    private static final String TENANT = "nextseq-tenant";
    private static final String SVC_ROLE = "svc_nextseq";
    private static final String SVC_PASS = "svc_nextseq_pass";

    PostgreSQLContainer<?> pg;
    CatalogRepository repo;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN "
                + "CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "'; END IF; END $$");
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN "
                + "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; END IF; END $$");
        }
        try (Connection su = pg.createConnection("")) {
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(su)))
                .update(new Contexts());
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute("ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
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

    @Test
    void drifted_owner_still_registers_instead_of_409ing() throws Exception {
        String p = owner("7001", "drifted");
        String first = repo.registerDocument(TENANT, p, Map.of(
            "title", "a", "content_type", "code", "source_uri", "file:///nextseq/a.md"));
        assertThat(first).isEqualTo("7001.1");

        driftNextSeq(p, 0);   // counter now BEHIND its own child

        // Pre-fix this raised a bare 409 and rolled the counter back, forever.
        String second = repo.registerDocument(TENANT, p, Map.of(
            "title", "b", "content_type", "code", "source_uri", "file:///nextseq/b.md"));
        assertThat(second)
            .as("a drifted owner must self-heal, not wedge permanently")
            .isEqualTo("7001.2");
    }

    @Test
    void tombstoned_children_still_consume_their_tumbler() throws Exception {
        String p = owner("7002", "tombstoned");
        String t1 = repo.registerDocument(TENANT, p, Map.of(
            "title", "a", "content_type", "code", "source_uri", "file:///nextseq2/a.md"));
        assertThat(repo.deleteDocument(TENANT, t1)).isEqualTo(1);

        driftNextSeq(p, 0);

        // The (tenant_id, tumbler) PK does NOT exclude tombstones — unlike the
        // partial source_uri index — so 7002.1 is still TAKEN. An allocator
        // that ignored tombstones would hand it out again and collide.
        String next = repo.registerDocument(TENANT, p, Map.of(
            "title", "b", "content_type", "code", "source_uri", "file:///nextseq2/b.md"));
        assertThat(next)
            .as("a tombstoned child's tumbler must not be re-issued")
            .isEqualTo("7002.2");
    }

    @Test
    void batch_register_also_heals() throws Exception {
        String p = owner("7003", "batch");
        repo.registerDocument(TENANT, p, Map.of(
            "title", "a", "content_type", "code", "source_uri", "file:///nextseq3/a.md"));
        driftNextSeq(p, 0);

        var out = repo.registerDocumentMany(TENANT, p, List.of(
            Map.of("title", "b", "content_type", "code", "source_uri", "file:///nextseq3/b.md"),
            Map.of("title", "c", "content_type", "code", "source_uri", "file:///nextseq3/c.md")));

        assertThat(out).as("the batch claim site must floor too, or the whole batch 409s")
            .containsExactly("7003.2", "7003.3");
    }

    @Test
    void healthy_owner_is_unaffected() throws Exception {
        // NON-VACUITY: the floor must not perturb a normal allocation.
        String p = owner("7004", "healthy");
        assertThat(repo.registerDocument(TENANT, p, Map.of(
            "title", "a", "content_type", "code", "source_uri", "file:///nextseq4/a.md"))).isEqualTo("7004.1");
        assertThat(repo.registerDocument(TENANT, p, Map.of(
            "title", "b", "content_type", "code", "source_uri", "file:///nextseq4/b.md"))).isEqualTo("7004.2");
    }

    @Test
    void next_seq_is_readable_so_drift_can_be_audited() throws Exception {
        // nexus-0ehwe item 3: next_seq was on NO read path, so the only way to
        // tell a drifted owner from a healthy one was to attempt a real
        // registration and see whether it 409'd — a mutation as a diagnostic.
        String p = owner("7005", "observable");
        repo.registerDocument(TENANT, p, Map.of(
            "title", "a", "content_type", "code", "source_uri", "file:///nextseq5/a.md"));

        Map<String, Object> row = repo.ownerByPrefix(TENANT, p);
        assertThat(row).containsKey("next_seq");
        assertThat(((Number) row.get("next_seq")).longValue()).isEqualTo(1L);

        driftNextSeq(p, 0);
        assertThat(((Number) repo.ownerByPrefix(TENANT, p).get("next_seq")).longValue())
            .as("drift must be VISIBLE without attempting a write")
            .isEqualTo(0L);
    }
}
