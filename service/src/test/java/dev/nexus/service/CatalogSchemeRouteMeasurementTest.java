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
 * nexus-uwggq.1.5 (RDR-241 Phase 2): how long does the by-scheme route take on a 50,000-live-row tenant (plus 2,000 deleted)
 * with no index? Seven in twenty rows are not files: https (some in capitals, some with a leading space), nx-note,
 * x-devonthink-item and obsidian, the rest file. The route (real service, bound parameters, JSON out) and the product's own
 * statements run under {@code EXPLAIN (ANALYZE, BUFFERS)}: the page with {@code count(*) OVER ()}, the page alone and the count
 * statement, median of five after one warm-up, every answered count asserted against a Java model of the fixture (the model
 * strips and parses the scheme in Java, independent of the SQL).
 *
 * <p>Opt-in, tag {@code measurement}: {@code scripts/mvnw-leased.sh --lab-run lab-m -B test -Dtest.excluded.groups=
 * -Dgroups=measurement -Dtest=CatalogSchemeRouteMeasurementTest -Dnexus.test.fork.timeout=0}. Output: {@code RDR241SCHEME} lines.
 */
@Tag("measurement")
class CatalogSchemeRouteMeasurementTest extends Rdr241RouteTestBase {

    private static final String SVC_ROLE = "svc_rdr241_schemeperf_test";
    private static final String SVC_PASS = "svc_rdr241_schemeperf_test_pass";
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

    private static final int LIVE = 50_000;
    private static final int DELETED = 2_000;

    /** The stored source_uri of row i, and (below) the scheme a urlparse reads in it. */
    private static String uri(int i) {
        int m = i % 20;
        if (m < 14) return "file:///fx/" + i + ".md";
        if (m < 17) {
            if (i % 7 == 0) return "HTTPS://example.org/" + i;
            if (i % 11 == 0) return " https://example.org/" + i;
            return "https://example.org/" + i;
        }
        if (m == 17) return "nx-note://n" + i;
        if (m == 18) return "x-devonthink-item://" + i;
        return "obsidian://vault/" + i;
    }

    private static String schemeOf(int i) {
        String u = uri(i).stripLeading();
        return u.substring(0, u.indexOf(':')).toLowerCase(Locale.ROOT);
    }

    private static long modelCount(String scheme) {
        long n = 0;
        for (int i = 0; i < LIVE; i++) if (schemeOf(i).equals(scheme)) n++;
        return n;
    }

    private void seedFixture() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            OffsetDateTime deletedAt = OffsetDateTime.parse("2026-10-01T12:00:00+00:00");
            int total = LIVE + DELETED;
            for (int lo = 0; lo < total; lo += 1000) {
                var step = seed.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                    CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.CONTENT_TYPE,
                    CATALOG_DOCUMENTS.FILE_PATH, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.SOURCE_URI,
                    CATALOG_DOCUMENTS.METADATA, CATALOG_DOCUMENTS.DELETED_AT);
                for (int i = lo; i < Math.min(total, lo + 1000); i++) {
                    step = step.values(TENANT, "7." + i + ".1", "Doc " + i, "prose", "/fx/" + i + ".md", COLL, uri(i),
                        JSONB.valueOf("{}"), i < LIVE ? null : deletedAt);
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
            emit("RDR241SCHEME plan of " + label + ":\n" + plan);
            return times.get(RUNS / 2);
        }
    }

    private void measureAll(String stage) throws Exception {
        record Case(String scheme, int offset) {}
        for (Case c : List.of(new Case("https", 0), new Case("https", 5000), new Case("file", 0), new Case("file", 30_000),
                new Case("nx-note", 0), new Case("obsidian", 0), new Case("zzz", 0))) {
            long expect = modelCount(c.scheme);
            if (!c.scheme.equals("zzz")) assertThat(expect).as("model count for " + c.scheme).isGreaterThan(0);
            String q = "/v1/catalog/docs/by-scheme?limit=1000&offset=" + c.offset + "&scheme=" + URLEncoder.encode(c.scheme, StandardCharsets.UTF_8);
            double route = routeMedianMs(q, expect);
            var where = CatalogDocReads.schemeWhere(TENANT, c.scheme);
            String tag = stage + " scheme " + c.scheme + " offset " + c.offset;
            double window = statementMedianMs(tag + " page with count(*) OVER ()", ctx -> CatalogDocReads.pageQuery(ctx, where, 1000, c.offset, dev.nexus.service.datamove.Gate3.never()));
            double rowsOnly = statementMedianMs(tag + " page without the window", ctx -> CatalogDocReads.pageRowsQuery(ctx, where, 1000, c.offset, dev.nexus.service.datamove.Gate3.never()));
            double count = statementMedianMs(tag + " count statement", ctx -> CatalogDocReads.countQuery(ctx, where));
            emit(String.format(Locale.ROOT, "RDR241SCHEME %s | route median %.1f ms | page+window %.1f ms | page only %.1f ms | count %.1f ms | matches %d",
                tag, route, window, rowsOnly, count, expect));
        }
    }

    @Test
    void theSchemeRouteAndItsStatementsWithNoIndex() throws Exception {
        seedFixture();
        measureAll("NO INDEX");
        System.out.println("RDR241SCHEME done, " + report.size() + " lines");
    }
}
