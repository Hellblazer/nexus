// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.VectorSeeds;
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
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.MANIFEST_QUALIFYING_HITS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-zk7rw: the plan of {@code nexus.manifest_qualifying_hits}, the bounded count that decides whether a short page is
 * re-run exact (nexus-8ptg4), is pinned on a table nobody has analyzed and on one that has been. The count is only worth
 * its cost while it reads the manifest by its keys; a plan that scanned the collection's chunks or the catalog
 * sequentially would cost more than the re-run it exists to avoid, and no result changes, so only the plan can show it.
 *
 * <p>One fixture, two states, so the pin holds before the first ANALYZE (a collection freshly indexed, where the
 * planner has only default estimates) and after it.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestQualifyingHitsPlanTest {

    private static final String SVC_ROLE = "svc_qual_plan";
    private static final String SVC_PASS = "svc_qual_plan_pass";
    private static final String TENANT = "qual-plan";
    private static final String COLLECTION = "docs__qual-plan__minilm-l6-v2-384__v1";
    private static final String MODEL = "minilm-l6-v2-384";
    private static final String DOC = "qp-doc";
    private static final int N = 40_000;

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;

    @BeforeAll
    void start() throws Exception {
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
        cfg.setMaximumPoolSize(2);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(seed, TENANT, COLLECTION);
            seed.insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
                .set(CATALOG_DOCUMENTS.TUMBLER, DOC)
                .set(CATALOG_DOCUMENTS.TITLE, "qual-plan.md")
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, COLLECTION)
                .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///qual-plan.md")
                .execute();
            var series = DSL.generateSeries(1, N).as("s", "i");
            Field<Integer> i = series.field("i", Integer.class);
            Field<String> iText = DSL.cast(i, SQLDataType.VARCHAR);
            Field<byte[]> chash = DSL.function("decode", SQLDataType.BLOB,
                DSL.concat(DSL.md5(DSL.concat(DSL.inline("qp-a-"), iText)), DSL.md5(DSL.concat(DSL.inline("qp-b-"), iText))),
                DSL.inline("hex"));
            seed.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.EMBEDDING_MODEL, CHUNKS.CHASH,
                    CHUNKS.CHUNK_TEXT, VectorSeeds.chunkEmbedding(384))
                .select(seed.select(DSL.val(TENANT, CHUNKS.TENANT_ID), DSL.val(COLLECTION, CHUNKS.COLLECTION),
                        DSL.val(MODEL, CHUNKS.EMBEDDING_MODEL), chash, DSL.concat(DSL.inline("qp chunk "), iText),
                        DSL.val(VectorSeeds.vecXY(384, 1f, 0f), VectorSeeds.chunkEmbedding(384)))
                    .from(series))
                .execute();
            seed.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                    CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION,
                    CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE,
                    CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN)
                .select(seed.select(DSL.val(TENANT, CATALOG_DOCUMENT_CHUNKS.TENANT_ID),
                        DSL.val(DOC, CATALOG_DOCUMENT_CHUNKS.DOC_ID), i, chash,
                        DSL.val(COLLECTION, CATALOG_DOCUMENT_CHUNKS.COLLECTION),
                        DSL.val(MODEL, CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL),
                        DSL.when(i.mod(2000).eq(0), DSL.inline("rare-x")).otherwise(DSL.inline("body")),
                        DSL.inline(true))
                    .from(series))
                .execute();
        }
    }

    @AfterAll
    void stop() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private String plan(String label, int cap) {
        return plan(label, cap, false);
    }

    /** {@code noIndexScans}: the plan under the exact re-run's setting (index scans off), the control that the pin can fail. */
    private String plan(String label, int cap, boolean noIndexScans) {
        ManifestPushdown.Plan plan = ManifestPushdown.of(ManifestLabelStage.split(Map.<String, Object>of("section_type", label)).labels());
        assertThat(plan).isNotNull();
        return tenantScope.withTenant(TENANT, ctx -> {
            if (noIndexScans) {
                PgSession.disableIndexScanForExactFallback(ctx);
            }
            return PgContainerHelper.explainPlanInlined(ctx,
            ctx.selectFrom(MANIFEST_QUALIFYING_HITS.call(new String[] {COLLECTION}, null, null, plan.containment(),
                plan.jsonPath(), cap, MODEL, TENANT, null)));
        });
    }

    /**
     * What the count's plan must be, in either table state: it ends in the LIMIT; the manifest ({@code
     * catalog_document_chunks}) and the chunk rows are read through an index, never sequentially; and nothing in it must
     * read every qualifying occurrence before it can return the first (a Sort or an Aggregate, which is what a DISTINCT
     * or GROUP BY inside the function would add). The documents table is left free: one row here, it can be hashed or
     * scanned as the planner likes.
     *
     * <p>The two states differ and both are the intended shape. Unanalyzed, the planner has only default estimates and
     * drives a nested loop from the manifest's {@code (tenant_id, collection)} index, probing the chunk row by its
     * primary key. Analyzed, it merges the two by chash, both sides index scans, which streams as well.
     */
    private void assertStreamingIndexPlan(String state, String plan) {
        assertThat(plan).as("%s: the cap is a LIMIT at the top of the plan\n%s", state, plan).startsWith("Limit");
        assertThat(plan).as("%s: the manifest is read through an index\n%s", state, plan)
            .containsPattern("Index (Only )?Scan using idx_catalog_chunks_\\w+ on catalog_document_chunks");
        assertThat(plan).as("%s: the chunk row is read through an index\n%s", state, plan)
            .containsPattern("Index (Only )?Scan using chunks_\\w+ on chunks_\\w+");
        assertThat(plan).as("%s: no sequential scan of the manifest\n%s", state, plan)
            .doesNotContain("Seq Scan on catalog_document_chunks");
        assertThat(plan).as("%s: no sequential scan of the chunk rows\n%s", state, plan)
            .doesNotContainPattern("Seq Scan on chunks_");
        assertThat(plan).as("%s: nothing that must read every occurrence before returning one\n%s", state, plan)
            .doesNotContain("Sort").doesNotContain("Aggregate");
    }

    @Test
    void theCountPlanIsAStreamingIndexPlan_onAnUnanalyzedTableAndAnAnalyzedOne() throws Exception {
        // 11 is a page of 10 plus one; 22 is the bound the distinct count reads for it.
        for (String label : new String[] {"rare-x", "body"}) {
            for (int cap : new int[] {11, 22}) {
                assertStreamingIndexPlan("unanalyzed, " + label + ", cap " + cap, plan(label, cap));
            }
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.analyzeTable(su, CHUNKS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENT_CHUNKS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
        }
        for (String label : new String[] {"rare-x", "body"}) {
            for (int cap : new int[] {11, 22}) {
                assertStreamingIndexPlan("analyzed, " + label + ", cap " + cap, plan(label, cap));
            }
        }
        // Control: the pin is not inert. The same statement planned with index scans off (a plan that regressed to
        // bitmap or sequential access) fails it.
        assertThatThrownBy(() -> assertStreamingIndexPlan("index scans off", plan("rare-x", 11, true)))
            .isInstanceOf(AssertionError.class).hasMessageContaining("index scans off");
    }
}
