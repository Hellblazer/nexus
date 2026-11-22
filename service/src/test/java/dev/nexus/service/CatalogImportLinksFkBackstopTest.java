// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assertions.assertThrows;

/**
 * nexus-ndwzk (1): the raw SQLSTATE 23503 backstop inside the import-links
 * INSERT — the catch that exists for a document hard-deleted between
 * {@code requireImportLinkEndpointsExist}'s SELECT and the multi-row INSERT.
 * These tests do NOT reproduce that race (there is no seam to interleave a
 * DELETE between the two statements deterministically); they drive the
 * INSERT half with the precheck BYPASSED, which puts the database in exactly
 * the state the race would leave it in — an endpoint the precheck never saw
 * missing — so the REAL {@code fk_catalog_links_to_document} /
 * {@code fk_catalog_links_from_document} violation fires and is proven to map
 * to the same {@code DanglingEndpointException} shape (400 dangling_endpoint
 * at the wire), never a 409/500, with the chunk rolled back whole.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogImportLinksFkBackstopTest {
    private static final String SVC_ROLE = "svc_links_backstop_test";
    private static final String SVC_PASS = "svc_links_backstop_test_pass";
    private static final String TENANT = "links-backstop-tenant";
    private static final String COLLECTION = "knowledge__links-backstop__minilm-l6-v2-384__v1";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    CatalogRepository catalogRepo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        catalogRepo = new CatalogRepository(tenantScope);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private void seedDoc(String tumbler) {
        catalogRepo.upsertDocument(TENANT, Map.of(
            "tumbler", tumbler, "title", tumbler, "content_type", "paper",
            "corpus", "knowledge", "physical_collection", COLLECTION));
    }

    private static Map<String, Object> link(String from, String to) {
        return Map.of("from_tumbler", from, "to_tumbler", to, "link_type", "cites", "created_by", "test");
    }

    @Test
    void backstop_missingToTumbler_mapsTheRealFkViolationToDanglingEndpoint() {
        seedDoc("bs1-src");
        var ex = assertThrows(CatalogRepository.DanglingEndpointException.class, () ->
            tenantScope.withTenant(TENANT, ctx -> {
                catalogRepo.insertImportLinkChunkUnchecked(ctx, TENANT, List.of(link("bs1-src", "bs1-gone")));
                return null;
            }));
        assertThat(ex.missing()).containsExactly("to_tumbler");
        assertThat(ex.getMessage()).contains("endpoint removed between precheck and insert");
        assertThat(catalogRepo.linksFrom(TENANT, "bs1-src", (List<String>) null)).isEmpty();
    }

    @Test
    void backstop_missingFromTumbler_namesFromTumbler() {
        seedDoc("bs2-dst");
        var ex = assertThrows(CatalogRepository.DanglingEndpointException.class, () ->
            tenantScope.withTenant(TENANT, ctx -> {
                catalogRepo.insertImportLinkChunkUnchecked(ctx, TENANT, List.of(link("bs2-gone", "bs2-dst")));
                return null;
            }));
        assertThat(ex.missing()).containsExactly("from_tumbler");
    }

    @Test
    void backstop_wholeChunkRollsBack_notPartial() {
        // One good row and one dangling row in the same chunk: the multi-row
        // INSERT is atomic, so the good row must not survive either.
        seedDoc("bs3-a");
        seedDoc("bs3-b");
        assertThrows(CatalogRepository.DanglingEndpointException.class, () ->
            tenantScope.withTenant(TENANT, ctx -> {
                catalogRepo.insertImportLinkChunkUnchecked(ctx, TENANT,
                    List.of(link("bs3-a", "bs3-b"), link("bs3-a", "bs3-gone")));
                return null;
            }));
        assertThat(catalogRepo.linksFrom(TENANT, "bs3-a", (List<String>) null)).isEmpty();
    }

    @Test
    void checkedPath_stillSucceedsWhenEveryEndpointExists() {
        seedDoc("bs4-a");
        seedDoc("bs4-b");
        assertThat(catalogRepo.importLinksBatch(TENANT, List.of(link("bs4-a", "bs4-b")))).isEqualTo(1);
        assertThat(catalogRepo.linksFrom(TENANT, "bs4-a", (List<String>) null)).hasSize(1);
    }
}
