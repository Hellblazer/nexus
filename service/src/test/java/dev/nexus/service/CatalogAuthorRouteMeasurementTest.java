// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogDocReads;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;

import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-uwggq.2.3 (RDR-241 Phase 2): how long does the author route take on the spike's fixture
 * ({@link CatalogRdr241RouteStatementSpikeTest.Fx}: one tenant, 50,000 live and 2,000 deleted documents), with no index
 * on author? The spike (nexus-uwggq.2.1) measured the statement at 153.7 ms for the broad term "author" at OFFSET 40,000.
 * This runs the ROUTE (real service, bound parameters, a pooled connection, JSON out) and the product's own statements
 * under {@code EXPLAIN (ANALYZE, BUFFERS)}: the page with {@code count(*) OVER ()}, the page alone and the count statement,
 * median of five after one warm-up, and asserts every answered count against the Java model of the fixture.
 *
 * <p>Opt-in, tag {@code measurement}: {@code scripts/mvnw-leased.sh --lab-run lab-m -B test -Dtest.excluded.groups=
 * -Dgroups=measurement -Dtest=CatalogAuthorRouteMeasurementTest -Dnexus.test.fork.timeout=0}. Output: {@code RDR241AUTHOR} lines.
 */
@Tag("measurement")
class CatalogAuthorRouteMeasurementTest extends Rdr241RouteTestBase {

    private static final String SVC_ROLE = "svc_rdr241_authoridx_test";
    private static final String SVC_PASS = "svc_rdr241_authoridx_test_pass";
    private static final int RUNS = 5;
    private static final Pattern EXEC_TIME = Pattern.compile("Execution Time: ([0-9.]+) ms");

    @Override String svcRole() { return SVC_ROLE; }
    @Override String svcPass() { return SVC_PASS; }
    @Override void bootstrapRole(Connection su) throws Exception {
        PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
    }

    private final List<String> report = new ArrayList<>();

    private void emit(String s) {
        System.out.println(s);
        report.add(s);
    }

    private void seedFixture() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(seed, TENANT, COLL);   // P3.1: a document names a registered collection, by id
            Long collId = dev.nexus.service.db.CollectionIds.require(seed, TENANT, COLL);
            OffsetDateTime deletedAt = OffsetDateTime.parse("2026-10-01T12:00:00+00:00");
            int total = CatalogRdr241RouteStatementSpikeTest.Fx.LIVE + CatalogRdr241RouteStatementSpikeTest.Fx.DELETED;
            for (int lo = 0; lo < total; lo += 1000) {
                var step = seed.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                    CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.AUTHOR, CATALOG_DOCUMENTS.CONTENT_TYPE,
                    CATALOG_DOCUMENTS.FILE_PATH, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.SOURCE_URI,
                    CATALOG_DOCUMENTS.METADATA, CATALOG_DOCUMENTS.DELETED_AT, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID);
                for (int i = lo; i < Math.min(total, lo + 1000); i++) {
                    step = step.values(TENANT, CatalogRdr241RouteStatementSpikeTest.Fx.tumbler(i), "Doc " + i,
                        CatalogRdr241RouteStatementSpikeTest.Fx.author(i), CatalogRdr241RouteStatementSpikeTest.Fx.contentType(i),
                        "/fx/" + i + ".md", COLL, "file:///fx/" + i + ".md", JSONB.valueOf("{}"),
                        CatalogRdr241RouteStatementSpikeTest.Fx.live(i) ? null : deletedAt, collId);
                }
                step.execute();
            }
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
        }
    }

    /** Median wall time of the route over {@code RUNS} calls after one warm-up, and the count the route answered. */
    private double routeMedianMs(String query, long expectCount) throws Exception {
        assertThat(ok(query)).containsEntry("count", (int) expectCount);   // warm-up, and the answer is the model's
        List<Double> times = new ArrayList<>();
        for (int r = 0; r < RUNS; r++) {
            long t0 = System.nanoTime();
            var body = ok(query);
            times.add((System.nanoTime() - t0) / 1e6);
            assertThat(body).containsEntry("count", (int) expectCount);
        }
        Collections.sort(times);
        return times.get(RUNS / 2);
    }

    /** Median Execution Time of one statement of the product's, inlined, as the spike measured; the last run's plan is reported. */
    private double statementMedianMs(String label, java.util.function.Function<DSLContext, org.jooq.Query> build) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var q = build.apply(ctx);
            PgContainerHelper.explainPlanWith(ctx, q, PgContainerHelper.ExplainOptions.ANALYZE_BUFFERS);   // warm-up
            List<Double> times = new ArrayList<>();
            String plan = null;
            for (int r = 0; r < RUNS; r++) {
                plan = PgContainerHelper.explainPlanWith(ctx, q, PgContainerHelper.ExplainOptions.ANALYZE_BUFFERS);
                Matcher m = EXEC_TIME.matcher(plan);
                assertThat(m.find()).isTrue();
                times.add(Double.parseDouble(m.group(1)));
            }
            Collections.sort(times);
            emit("RDR241AUTHOR plan of " + label + ":\n" + plan);
            return times.get(RUNS / 2);
        }
    }

    private void measureAll(String stage) throws Exception {
        record Case(String term, int offset) {}
        for (Case c : List.of(new Case("lamport", 0), new Case("author", 0), new Case("author", 40_000), new Case("0% author_f", 0))) {
            long expect = CatalogRdr241RouteStatementSpikeTest.Fx.count(i -> CatalogRdr241RouteStatementSpikeTest.Fx.authorContains(i, c.term));
            assertThat(expect).as("model count for " + c.term).isGreaterThan(0);
            String q = "/v1/catalog/docs/by-author?limit=1000&offset=" + c.offset + "&author=" + URLEncoder.encode(c.term, StandardCharsets.UTF_8);
            double route = routeMedianMs(q, expect);
            var where = CatalogDocReads.authorWhere(TENANT, c.term);
            String tag = stage + " author~'" + c.term + "' offset " + c.offset;
            double window = statementMedianMs(tag + " page with count(*) OVER ()", ctx -> CatalogDocReads.pageQuery(ctx, where, 1000, c.offset));
            double rowsOnly = statementMedianMs(tag + " page without the window", ctx -> CatalogDocReads.pageRowsQuery(ctx, where, 1000, c.offset));
            double count = statementMedianMs(tag + " count statement", ctx -> CatalogDocReads.countQuery(ctx, where));
            emit(String.format(Locale.ROOT, "RDR241AUTHOR %s | route median %.1f ms | page+window %.1f ms | page only %.1f ms | count %.1f ms | matches %d",
                tag, route, window, rowsOnly, count, expect));
        }
    }

    @Test
    void theAuthorRouteAndItsStatementsWithNoIndex() throws Exception {
        seedFixture();
        measureAll("NO INDEX");
        System.out.println("RDR241AUTHOR done, " + report.size() + " lines");
    }
}
