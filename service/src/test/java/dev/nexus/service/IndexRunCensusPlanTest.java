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
import java.util.regex.Pattern;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
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
        assertThat(plan).doesNotContain("Seq Scan on catalog_documents").doesNotContain("Sort");
    }

    @Test
    void theFailedStatementReadsThePartialIndexAndStopsAtItsLimit() throws Exception {
        seed();
        String plan = explain(ctx -> IndexRunCensusReads.failedQuery(ctx, TENANT, LIMIT));
        System.out.println("PLAN failed (limit " + LIMIT + "):\n" + plan);
        assertThat(plan).contains("idx_catalog_documents_index_state").contains("Limit");
        assertThat(indexScanRows(plan)).as("rows the scan of the documents produced, of 200 failed documents").isLessThanOrEqualTo(LIMIT);
        assertThat(plan).doesNotContain("Seq Scan on catalog_documents");
    }

    /** Control: without the limit the same statement reads all of its set, so the bound above is the limit's doing. */
    @Test
    void withoutALimitTheSameStatementReadsAllHundredStaleRows() throws Exception {
        seed();
        String plan = explain(ctx -> IndexRunCensusReads.staleQuery(ctx, TENANT, OffsetDateTime.now(ZoneOffset.UTC).minusHours(6), 10_000));
        assertThat(indexScanRows(plan)).isEqualTo(100);
    }
}
