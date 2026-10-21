// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.sql.Connection;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-s80u9 (7.77.0 shakeout N5): {@code nx collection rename A B} followed by {@code nx collection delete B}
 * left a ghost registry row for A.
 *
 * <p>A rename retires A as a tombstone ({@code superseded_by = B}); the tombstone is the rename's audit trail and
 * the target of a rename-back (nexus-cecqy). Once B is deleted the tombstone names nothing and holds nothing, so it
 * is debris, and the ghost sweep (and {@code nx doctor}'s "Ghost collections" row) counted it. Deleting B now
 * removes the retired rows that name it and hold no content. A retired row that still holds content (a plain
 * supersede moves nothing) is the data's only home and stays.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogDeleteRetiredPredecessorTest {

    private static final String SVC_ROLE = "svc_del_retired";
    private static final String SVC_PASS = "svc_del_retired_pass";

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
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        repo = new CatalogRepository(new TenantScope(svcDs));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test
    void renameThenDeleteTheSuccessor_leavesNoRowForTheOldName_andNoGhost() throws Exception {
        String tenant = "del-retired-a";
        String a = "knowledge__drp-a__minilm-l6-v2-384__v1";
        String b = "knowledge__drp-a__minilm-l6-v2-384__v2";
        seed(tenant, a, true);

        repo.renameCollection(tenant, a, b);
        assertThat(rowNames(tenant)).as("the rename retires A as a tombstone").contains(a, b);

        repo.deleteCollection(tenant, b);

        assertThat(rowNames(tenant)).as("neither name is left in the registry").doesNotContain(a, b);
        assertThat(repo.sweepGhostsAndMarkDormant(tenant, true).ghostNames())
            .as("nx doctor's Ghost collections row reads zero").isEmpty();
    }

    @Test
    void aChainOfRenames_isRemovedWhole_whenTheLastSuccessorIsDeleted() throws Exception {
        String tenant = "del-retired-chain";
        String a = "knowledge__drp-c__minilm-l6-v2-384__v1";
        String b = "knowledge__drp-c__minilm-l6-v2-384__v2";
        String c = "knowledge__drp-c__minilm-l6-v2-384__v3";
        seed(tenant, a, true);

        repo.renameCollection(tenant, a, b);
        repo.renameCollection(tenant, b, c);
        assertThat(rowNames(tenant)).contains(a, b, c);

        repo.deleteCollection(tenant, c);

        // B named C and A named B: once C goes, B is debris, and once B goes, so is A.
        assertThat(rowNames(tenant)).doesNotContain(a, b, c);
    }

    @Test
    void aRetiredRowThatStillHoldsContent_isKept_whenItsSuccessorIsDeleted() throws Exception {
        String tenant = "del-retired-keep";
        String a = "knowledge__drp-k__minilm-l6-v2-384__v1";
        String b = "knowledge__drp-k__minilm-l6-v2-384__v2";
        seed(tenant, a, true);
        seed(tenant, b, false);
        // A plain supersede moves nothing: A keeps its chunks and now names B as its successor.
        repo.supersedeCollection(tenant, a, b, null);

        repo.deleteCollection(tenant, b);

        assertThat(rowNames(tenant)).as("A is the only home of its chunk, so its row stays").contains(a);
    }

    @Test
    void anotherTenantsRetiredRowNamingTheSameSuccessorName_isUntouched() throws Exception {
        String t1 = "del-retired-t1";
        String t2 = "del-retired-t2";
        String a = "knowledge__drp-t__minilm-l6-v2-384__v1";
        String b = "knowledge__drp-t__minilm-l6-v2-384__v2";
        seed(t1, a, true);
        seed(t2, a, true);
        repo.renameCollection(t1, a, b);
        repo.renameCollection(t2, a, b);

        repo.deleteCollection(t1, b);

        assertThat(rowNames(t1)).doesNotContain(a, b);
        assertThat(rowNames(t2)).as("tenant 2 still holds B, and A's tombstone names it").contains(a, b);
    }

    // ── fixture ──────────────────────────────────────────────────────────────

    private java.util.List<String> rowNames(String tenant) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            return ctx.select(CATALOG_COLLECTIONS.NAME).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).fetch(CATALOG_COLLECTIONS.NAME);
        }
    }

    /** Register {@code name} under {@code tenant}, optionally with one chunk in it. Superuser; bypasses RLS. */
    private void seed(String tenant, String name, boolean withChunk) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, name);
            if (withChunk) {
                float[] v = new float[384];
                java.util.Arrays.fill(v, 0.1f);
                String label = ("ab" + Math.abs((tenant + name).hashCode()) + "0".repeat(32)).substring(0, 32);
                PgContainerHelper.insertChunk384(ctx, tenant, name,
                    label.replaceAll("[^0-9a-f]", "a").getBytes(StandardCharsets.US_ASCII), Vector.of(v));
            }
        }
    }
}
