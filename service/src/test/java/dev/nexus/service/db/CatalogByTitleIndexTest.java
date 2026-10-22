// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.PgCatalogProbes;
import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-gacz0 (review round): the exact-title lookup {@link CatalogRepository#documentsByTitle}
 * is an index lookup, not a tenant-wide scan. {@code catalog-045-1} adds a partial
 * {@code (tenant_id, md5(title)) WHERE deleted_at IS NULL} index; the query states
 * {@code md5(title) = md5(?)} beside the exact equality so the planner can use it.
 *
 * <p>The proof is the PLAN of the product's own statement ({@link CatalogRepository#documentsByTitleQuery}),
 * explained as the RLS-subject service role under the tenant GUC, on a table large enough and analyzed so
 * the planner has a real choice. Mutation-checked: with the md5 condition removed from the statement the
 * plan does not reach the title index (it walks the primary key for the ORDER BY, or scans the table).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogByTitleIndexTest {

    private static final String SVC_ROLE = "svc_cat_bt_idx_test";
    private static final String SVC_PASS = "svc_cat_bt_idx_test_pass";
    private static final String TENANT = "cat-by-title-idx-tenant";
    private static final String INDEX = "idx_catalog_documents_title_md5_live";
    private static final String TITLE = "Index Probe Title";
    private static final int FILLER_ROWS = 20_000;

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    TenantScope tenantScope;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        try (Connection su = pg.createConnection("")) {
            seed(su);
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(2);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /**
     * {@code FILLER_ROWS} live documents of distinct titles, three documents carrying {@link #TITLE}
     * (two with chunks, one ghost), then statistics: the planner must see a table where a sequential
     * scan costs more than a lookup, as it does on a real tenant.
     */
    private static void seed(Connection su) throws Exception {
        DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
        var series = DSL.generateSeries(1, FILLER_ROWS).as("s", "i");
        Field<String> i = DSL.cast(series.field("i", Integer.class), SQLDataType.VARCHAR);
        seed.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.CONTENT_TYPE, CATALOG_DOCUMENTS.CHUNK_COUNT)
            .select(seed.select(DSL.val(TENANT, CATALOG_DOCUMENTS.TENANT_ID),
                    DSL.concat(DSL.inline("5.1."), i),
                    DSL.concat(DSL.inline("Filler Title "), i), DSL.inline("knowledge"), DSL.inline(3))
                .from(series))
            .execute();
        for (int n = 1; n <= 3; n++) {
            seed.insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
                .set(CATALOG_DOCUMENTS.TUMBLER, "6." + n + ".1")
                .set(CATALOG_DOCUMENTS.TITLE, TITLE)
                .set(CATALOG_DOCUMENTS.CONTENT_TYPE, "knowledge")
                .set(CATALOG_DOCUMENTS.CHUNK_COUNT, n == 3 ? 0 : 4)
                .execute();
        }
        PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
    }

    @Test
    void theIndexIsPartialOnTenantAndMd5OfTitle_andNotUnique() throws Exception {
        try (Connection su = pg.createConnection("")) {
            String def = PgCatalogProbes.indexDef(DSL.using(su, SQLDialect.POSTGRES), "nexus", INDEX);
            assertThat(def).isNotNull();
            assertThat(def).contains("tenant_id", "md5(title)", "deleted_at IS NULL");
            assertThat(def).doesNotContain("UNIQUE");
        }
    }

    private String planOf(String owner, String contentType, Integer chunkCount) {
        return tenantScope.withTenant(TENANT, ctx -> PgContainerHelper.explainPlanWith(ctx,
            CatalogRepository.documentsByTitleQuery(ctx, TITLE, owner, contentType, chunkCount),
            PgContainerHelper.ExplainOptions.ANALYZE_VERBOSE_COSTS_OFF));
    }

    @Test
    void theExactTitleStatementReachesTheTitleIndex_withAndWithoutTheOtherFilters() {
        for (String plan : new String[] {
                planOf(null, null, null),
                planOf("6.1", null, null),
                planOf("6", "knowledge", 0)}) {
            assertThat(plan).as("plan:%n%s", plan)
                .contains(INDEX)
                .doesNotContain("Seq Scan on nexus.catalog_documents");
        }
    }

    @Test
    void theStatementStillAnswersCorrectly_onTheLargeTable() {
        // Non-vacuity of the plan test: it is the same statement, and it finds exactly the three.
        var rows = tenantScope.withTenant(TENANT, ctx ->
            CatalogRepository.documentsByTitleQuery(ctx, TITLE, null, null, null).fetch());
        assertThat(rows).hasSize(3);
        var ghosts = tenantScope.withTenant(TENANT, ctx ->
            CatalogRepository.documentsByTitleQuery(ctx, TITLE, null, null, 0).fetch());
        assertThat(ghosts).hasSize(1);
    }
}
