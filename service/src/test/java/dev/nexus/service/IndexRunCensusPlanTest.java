// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.IndexRunCensusReads;
import dev.nexus.service.db.TenantScope;
import org.jooq.Query;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd: the stale and failed statements of the index-run census read the partial index
 * {@code idx_catalog_documents_index_state} (catalog-020-2, {@code (tenant_id, index_state) WHERE index_state <> 'complete'})
 * and STOP at their limit. A tenant of 6,000 documents of which 300 are non-complete is seeded and analysed (the planner
 * chooses the index on its own at that selectivity; nothing is switched off), and the EXPLAIN ANALYZE of each statement
 * must show the index scan and an actual row count of at most {@code limit}: the scan stopped at the limit, it did not read
 * the 300 and cut them afterwards.
 */
class IndexRunCensusPlanTest extends IndexRunCensusTestBase {

    private static final int LIMIT = 11;
    /** The rows the scan of the documents table produced: an index scan on the partial index, or the heap scan its bitmap feeds. */
    private static final Pattern SCAN_ROWS = Pattern.compile(
        "(?:Index Scan using idx_catalog_documents_index_state|Bitmap Heap Scan on nexus\\.catalog_documents)[^\\n]*actual time=[0-9.]+\\.\\.[0-9.]+ rows=(\\d+) loops=1");

    private void seed() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            var batch = new ArrayList<Query>();
            OffsetDateTime old = OffsetDateTime.now(ZoneOffset.UTC).minusHours(30);
            for (int i = 0; i < 6_000; i++) {
                String state = i < 100 ? "indexing" : i < 300 ? "failed" : i % 3 == 0 ? "complete" : null;
                var q = dsl.insertInto(CATALOG_DOCUMENTS)
                    .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT).set(CATALOG_DOCUMENTS.TUMBLER, "8.1." + (i + 1))
                    .set(CATALOG_DOCUMENTS.TITLE, "d").set(CATALOG_DOCUMENTS.CONTENT_TYPE, "code")
                    .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///plan/" + i).set(CATALOG_DOCUMENTS.INDEX_STATE, state);
                batch.add("indexing".equals(state) ? q.set(CATALOG_DOCUMENTS.INDEX_STARTED_AT, old) : q);
            }
            for (int i = 0; i < batch.size(); i += 1000) dsl.batch(batch.subList(i, Math.min(i + 1000, batch.size()))).execute();
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
        }
    }

    private String explain(java.util.function.Function<org.jooq.DSLContext, Query> statement) {
        return new TenantScope(svcDs).withTenant(TENANT, ctx -> PgContainerHelper.explainPlanWith(
            ctx, statement.apply(ctx), PgContainerHelper.ExplainOptions.ANALYZE_VERBOSE_COSTS_OFF));
    }

    private static int indexScanRows(String plan) {
        assertThat(plan).contains("idx_catalog_documents_index_state");
        var m = SCAN_ROWS.matcher(plan);
        assertThat(m.find()).as("a scan of the documents table in:\n" + plan).isTrue();
        return Integer.parseInt(m.group(1));
    }

    @Test
    void theStaleStatementReadsThePartialIndexAndStopsAtItsLimit() throws Exception {
        seed();
        String plan = explain(ctx -> IndexRunCensusReads.staleQuery(ctx, TENANT, OffsetDateTime.now(ZoneOffset.UTC).minusHours(6), LIMIT));
        System.out.println("PLAN stale (limit " + LIMIT + "):\n" + plan);
        assertThat(plan).contains("idx_catalog_documents_index_state").contains("Limit");
        assertThat(indexScanRows(plan)).as("rows the scan of the documents produced, of 100 stale documents").isLessThanOrEqualTo(LIMIT);
        // the VERBOSE plan names the table schema-qualified, so this is the form a real seq scan prints (the control test below shows it)
        assertThat(plan).doesNotContain("Seq Scan on nexus.catalog_documents").doesNotContain("Sort");
    }

    @Test
    void theFailedStatementReadsThePartialIndexAndStopsAtItsLimit() throws Exception {
        seed();
        String plan = explain(ctx -> IndexRunCensusReads.failedQuery(ctx, TENANT, LIMIT));
        System.out.println("PLAN failed (limit " + LIMIT + "):\n" + plan);
        assertThat(plan).contains("idx_catalog_documents_index_state").contains("Limit");
        assertThat(indexScanRows(plan)).as("rows the scan of the documents produced, of 200 failed documents").isLessThanOrEqualTo(LIMIT);
        assertThat(plan).doesNotContain("Seq Scan on nexus.catalog_documents");
    }

    /** Control: the string the two tests above refuse is the one a real sequential scan of the documents prints in this plan form. */
    @Test
    void aStatementTheIndexCannotServePrintsTheSeqScanFormTheTestsRefuse() throws Exception {
        seed();
        String plan = explain(ctx -> ctx.select(CATALOG_DOCUMENTS.TUMBLER).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.INDEX_STATE.isNull())).limit(LIMIT));
        assertThat(plan).contains("Seq Scan on nexus.catalog_documents");
    }

    /**
     * Why the stale and failed samples keep the planner's order: an ORDER BY tumbler under the LIMIT cannot use the partial
     * index's order (equal {@code (tenant_id, index_state)} keys sit in physical order), so the scan reads EVERY row of the
     * state and sorts. The plan shows it: the scan produces all 100 stale rows and a Sort sits above it.
     */
    @Test
    void orderingTheSampleByTumblerWouldReadEveryStaleRowAndSortThem() throws Exception {
        seed();
        String plan = explain(ctx -> ctx.select(CATALOG_DOCUMENTS.TUMBLER).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.DELETED_AT.isNull())
                .and(CATALOG_DOCUMENTS.INDEX_STATE.eq("indexing")).and(CATALOG_DOCUMENTS.INDEX_STATE.ne("complete")))
            .orderBy(CATALOG_DOCUMENTS.TUMBLER).limit(LIMIT));
        System.out.println("PLAN stale ordered by tumbler (limit " + LIMIT + "):\n" + plan);
        assertThat(plan).contains("Sort");
        assertThat(indexScanRows(plan)).as("the ordered statement reads every stale row").isEqualTo(100);
    }

    // ── the chunk read of failed_hidden ─────────────────────────────────────

    private static final String C384 = "knowledge__census-384__minilm-l6-v2-384__v1";

    private int leavesPlanned(String collection, String model) {
        String plan = new TenantScope(svcDs).withTenant(TENANT, ctx -> PgContainerHelper.explainPlanWith(ctx,
            IndexRunCensusReads.storedByHashQuery(ctx, TENANT, collection, model, List.of("h1", "h2")),
            new PgContainerHelper.ExplainOptions(false, false, false, false)));
        var m = Pattern.compile("chunks_m[0-9a-f]+_t_[0-9a-f]+").matcher(plan);
        var seen = new java.util.TreeSet<String>();
        while (m.find()) seen.add(m.group());
        assertThat(seen).as("chunk leaves planned for " + collection + ":\n" + plan).hasSize(1);
        return seen.iterator().next().hashCode();
    }

    /**
     * The chunk read names the collection and its model as constants, so the planner prunes {@code nexus.chunks} to the
     * collection's leaf when it plans: the plan mentions exactly one chunk leaf, and another leaf for the collection of the
     * other model. (Without the model predicate every model's leaf is planned.)
     */
    @Test
    void theChunkReadIsPrunedToTheCollectionsLeaf_atPlanTime() throws Exception {
        collection(TENANT, C1);
        collection(TENANT, C384);
        chunk(TENANT, C1, "{\"content_hash\":\"h1\"}");
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).insertInto(CHUNKS)
                .set(CHUNKS.TENANT_ID, TENANT).set(CHUNKS.COLLECTION, C384).set(CHUNKS.CHASH, nextChash())
                .set(CHUNKS.EMBEDDING_MODEL, PgContainerHelper.collectionModel(DSL.using(su, SQLDialect.POSTGRES), TENANT, C384))
                .set(CHUNKS.CHUNK_TEXT, "t").set(CHUNKS.METADATA, org.jooq.JSONB.valueOf("{}"))
                .set(CHUNKS.EMBEDDING_384, VectorSeeds.vec(384, 0.1f)).execute();
        }
        String model384;
        try (Connection su = pg.createConnection("")) {
            model384 = PgContainerHelper.collectionModel(DSL.using(su, SQLDialect.POSTGRES), TENANT, C384);
        }
        assertThat(leavesPlanned(C1, MODEL)).as("two models, two leaves").isNotEqualTo(leavesPlanned(C384, model384));
    }

    /** Control: without the limit the same statement reads all of its set, so the bound above is the limit's doing. */
    @Test
    void withoutALimitTheSameStatementReadsAllHundredStaleRows() throws Exception {
        seed();
        String plan = explain(ctx -> IndexRunCensusReads.staleQuery(ctx, TENANT, OffsetDateTime.now(ZoneOffset.UTC).minusHours(6), 10_000));
        assertThat(indexScanRows(plan)).isEqualTo(100);
    }
}
