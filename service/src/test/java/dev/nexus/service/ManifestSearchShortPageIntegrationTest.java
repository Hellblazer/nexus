// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.PgVectorRepository.ArmErrorKind;
import dev.nexus.service.vectors.PgVectorRepository.FanoutSettings;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.condition.EnabledIfSystemProperty;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-6pfgb.61 (RDR-236 Phase 3 gate, critique Significant 1; assumption A8): the A8 spike and the pin for its
 * remedy. Above the cardinality router's threshold (20,000 rows) a positive label filter runs as an EXISTS on the
 * manifest INSIDE the HNSW scan ({@code manifest_search_<dim>}). The iterative scan stops at {@code
 * hnsw.max_scan_tuples} (or its memory budget), so a rare label whose chunks rank beyond the cap yields a SHORT page.
 * An EMPTY page was always repaired by an exact re-run (nexus-bq06h); a short, non-empty one was not.
 *
 * <p>Fixture. One document, 21,000 chunks (above the 20,000 router threshold, so the router sends the statement down
 * the HNSW route with no override) laid on a quarter circle so the query (1, 0) ranks them in index order. The
 * manifest says {@code section_type body} everywhere except three chunks, {@code appendix-x} at ranks 5, 10,000 and
 * 20,900 (0-based). The scan cap is set to its engine minimum, 1,000 tuples, through the budget test seam, so the
 * walk visits the nearest ~1,000 rows: it finds the rank-5 chunk and not the other two.
 *
 * <p>What this class pins: that page IS short without the re-run (the spike's finding, recorded in the bead), that
 * the re-run makes it complete, that a scan the cap did not truncate pays no re-run, that a filter which genuinely
 * matches fewer rows than asked pays none either (nexus-8ptg4: the bounded manifest count shows the page complete) and
 * returns the same rows, that a starved page with fewer qualifying chunks than the statement asks for still re-runs, that
 * a fan-out arm whose re-run is cancelled is marked timed out, and that nothing re-runs for a statement with no
 * manifest predicate.
 *
 * <p>{@code spike_measurements} prints the timings the bead records (the walk, the exact re-run, the common label, the
 * chunk-row filter). It asserts nothing, so it runs only with {@code -Dnx.spike=true}; the pins do the asserting. The
 * EXPLAIN ANALYZE comparison of the EXISTS beside its rewrites is recorded on the bead and in vectors-045's comment.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestSearchShortPageIntegrationTest {

    private static final String SVC_ROLE = "svc_short_page";
    private static final String SVC_PASS = "svc_short_page_pass";

    private static final String TENANT = "short-page";
    private static final String COLLECTION = "docs__short-page__minilm-l6-v2-384__v1";
    private static final String MODEL = "minilm-l6-v2-384";
    private static final String DOC = "sp-doc";
    private static final String QUERY = "spikeprobe";
    private static final int N = 21_000;
    private static final List<Integer> RARE = List.of(5, 10_000, 20_900);

    private static String chash(int i) {
        return Chash.ofText("sp-" + i).toHex();
    }

    private static String text(int g) {
        return "spikeprobe chunk " + g;
    }

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    PgVectorRepository vec;

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
        var tenantScope = new TenantScope(svcDs);

        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        embedder.register(QUERY, 1.0f, 0.0f);
        for (int g = 0; g < N; g++) {
            double theta = (Math.PI / 2.0) * g / (N - 1);
            embedder.register(text(g), (float) Math.cos(theta), (float) Math.sin(theta));
        }
        vec = new PgVectorRepository(tenantScope, embedder, embedder);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(seed, TENANT, COLLECTION);
            seed.insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
                .set(CATALOG_DOCUMENTS.TUMBLER, DOC)
                .set(CATALOG_DOCUMENTS.TITLE, "short-page.md")
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, COLLECTION)
                .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///short-page.md")
                .execute();
        }
        // The chunk at index g sits at angle (pi/2) * g / (N - 1), so the query (1, 0) ranks them in index order. The
        // three rare chunks also carry a chunk-row marker: a predicate on it is the in-index filter the manifest EXISTS
        // replaced, which the spike times against it and which no manifest re-run covers.
        for (int from = 0; from < N; from += 500) {
            List<String> ids = new ArrayList<>();
            List<String> texts = new ArrayList<>();
            List<Map<String, Object>> metas = new ArrayList<>();
            for (int g = from; g < Math.min(N, from + 500); g++) {
                ids.add(chash(g));
                texts.add(text(g));
                Map<String, Object> m = new LinkedHashMap<>();
                if (RARE.contains(g)) m.put("lang", "rare");
                metas.add(m);
            }
            vec.upsertChunks(TENANT, COLLECTION, ids, texts, metas);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            for (int from = 0; from < N; from += 500) {
                var step = seed.insertInto(CATALOG_DOCUMENT_CHUNKS,
                    CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                    CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                    CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL,
                    CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE, CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN);
                for (int g = from; g < Math.min(N, from + 500); g++) {
                    step = step.values(TENANT, DOC, g, HexFormat.of().parseHex(chash(g)), COLLECTION, MODEL,
                        RARE.contains(g) ? "appendix-x" : "body", true);
                }
                step.execute();
            }
            PgContainerHelper.analyzeTable(su, CHUNKS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENT_CHUNKS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
        }
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @AfterEach
    void restore() {
        PgSession.resetScanBudgetForTests();
        PgSession.resetSearchExactMaxRowsForTests();
    }

    private static Map<String, Object> rare() {
        Map<String, Object> w = new LinkedHashMap<>();
        w.put("section_type", "appendix-x");
        return w;
    }

    private static List<String> rareChashes() {
        return RARE.stream().map(ManifestSearchShortPageIntegrationTest::chash).collect(Collectors.toList());
    }

    private static List<String> ids(List<Map<String, Object>> rows) {
        return rows.stream().map(r -> (String) r.get("id")).collect(Collectors.toList());
    }

    /** Five timed searches; prints the rows of the last and the median and minimum milliseconds, for the bead. */
    private List<Map<String, Object>> timed(String label, Map<String, Object> where) {
        long[] ms = new long[5];
        List<Map<String, Object>> rows = null;
        for (int i = 0; i < ms.length; i++) {
            long start = System.nanoTime();
            rows = vec.search(TENANT, QUERY, List.of(COLLECTION), 10, where);
            ms[i] = (System.nanoTime() - start) / 1_000_000;
        }
        java.util.Arrays.sort(ms);
        System.out.println("A8-SPIKE " + label + ": rows=" + rows.size() + " median_ms=" + ms[2] + " min_ms=" + ms[0]
            + " exactFallbacks=" + PgVectorRepository.exactFallbackCount() + " shortPageReruns="
            + PgVectorRepository.shortPageRerunCount());
        return rows;
    }

    @Test
    @EnabledIfSystemProperty(named = "nx.spike", matches = "true")
    void spike_measurements() {
        timed("warmup unfiltered", null);
        timed("unfiltered n=10, default budget", null);
        timed("rare label, default budget (scan exhausts the index)", rare());
        PgSession.overrideScanBudgetForTests(1000, 100);
        timed("rare label, scan cap 1000 tuples", rare());
        PgSession.resetScanBudgetForTests();
        PgSession.overrideSearchExactMaxRowsForTests(1_000_000);
        timed("rare label, exact route (router threshold raised)", rare());
        PgSession.resetSearchExactMaxRowsForTests();
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("section_type", "body");
        timed("common label (body), default budget", body);
        Map<String, Object> lang = new LinkedHashMap<>();
        lang.put("lang", "rare");
        timed("chunk-row predicate lang=rare (the in-index filter before the EXISTS), default budget", lang);
        PgSession.overrideScanBudgetForTests(1000, 100);
        timed("chunk-row predicate lang=rare, scan cap 1000 tuples", lang);
    }

    private static long reruns() {
        return PgVectorRepository.shortPageRerunCount();
    }

    private static long skips() {
        return PgVectorRepository.shortPageSkipCount();
    }

    @Test
    void aRareLabelPastTheScanCap_isRepairedByOneExactRerun() {
        PgSession.overrideScanBudgetForTests(1000, 100);
        long before = reruns();
        long skipsBefore = skips();

        var rows = vec.search(TENANT, QUERY, List.of(COLLECTION), 10, rare());

        // Without the re-run this page held ONE row, the rank-5 chunk (spike_measurements, rows=1). The filter admits
        // THREE chunks, fewer than the 40 the statement asks for, and the page is still starved: the discriminator
        // compares the manifest count with the page (1 row, 3 qualifying), not with the widened n.
        assertThat(ids(rows)).containsExactlyElementsOf(rareChashes());
        assertThat(reruns() - before).isEqualTo(1);
        assertThat(skips() - skipsBefore).isZero();
    }

    @Test
    void aFilterThatGenuinelyMatchesFewRows_isNotRerun_andReturnsTheSameRows() {
        // nexus-8ptg4. The default budget walks all 21,000 tuples, so the three rows are all there are; the page is
        // short because the filter admits three chunks, and the manifest count (3, against a page of 3) shows it
        // complete. Before the discriminator this page paid one O(collection) exact re-run for nothing.
        long before = reruns();
        long skipsBefore = skips();

        var rows = vec.search(TENANT, QUERY, List.of(COLLECTION), 10, rare());

        assertThat(ids(rows)).containsExactlyElementsOf(rareChashes());
        assertThat(reruns() - before).isZero();
        assertThat(skips() - skipsBefore).isEqualTo(1);
    }

    @Test
    void aPageTheScanFills_isNotRerun() {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("section_type", "body");
        long before = reruns();
        long skipsBefore = skips();

        assertThat(vec.search(TENANT, QUERY, List.of(COLLECTION), 10, body)).hasSize(10);
        assertThat(vec.search(TENANT, QUERY, List.of(COLLECTION), 10, null)).hasSize(10);

        assertThat(reruns() - before).isZero();
        assertThat(skips() - skipsBefore).as("a full page is not even asked").isZero();
    }

    @Test
    void aPredicateOnTheChunkRow_isOutsideTheRerun() {
        // No manifest predicate rides this statement: the re-run is scoped to manifest_search, as the bead says.
        PgSession.overrideScanBudgetForTests(1000, 100);
        Map<String, Object> lang = new LinkedHashMap<>();
        lang.put("lang", "rare");
        long before = reruns();

        vec.search(TENANT, QUERY, List.of(COLLECTION), 10, lang);

        assertThat(reruns() - before).isZero();
    }

    @Test
    void theHybridDenseBranch_andThePerCollectionArm_repairTheSameShortPage() {
        PgSession.overrideScanBudgetForTests(1000, 100);
        long before = reruns();

        // selectiveGateMax 1: the text gate admits 21,000 rows, so the dense (HNSW-first) branch runs.
        var hybrid = vec.hybridSearchWithTokens(TENANT, QUERY, List.of(COLLECTION), 10, rare(), false, 1).value();
        assertThat(ids(hybrid)).containsExactlyInAnyOrderElementsOf(rareChashes());
        assertThat(reruns() - before).isEqualTo(1);

        var perCollection = vec.searchPerCollection(TENANT, QUERY, List.of(COLLECTION), 10, 10, null, rare(), false).rows();
        assertThat(ids(perCollection)).containsExactlyElementsOf(rareChashes());
        assertThat(reruns() - before).isEqualTo(2);
    }

    @Test
    void theHybridDenseBranch_andThePerCollectionArm_skipTheRerunForAGenuinelyShortPage() {
        // The default budget walks every tuple: the three rows are all the filter admits.
        long before = reruns();
        long skipsBefore = skips();

        var hybrid = vec.hybridSearchWithTokens(TENANT, QUERY, List.of(COLLECTION), 10, rare(), false, 1).value();
        assertThat(ids(hybrid)).containsExactlyInAnyOrderElementsOf(rareChashes());
        var perCollection = vec.searchPerCollection(TENANT, QUERY, List.of(COLLECTION), 10, 10, null, rare(), false).rows();
        assertThat(ids(perCollection)).containsExactlyElementsOf(rareChashes());

        assertThat(reruns() - before).isZero();
        assertThat(skips() - skipsBefore).isEqualTo(2);
    }

    /** Median milliseconds of {@code runs} timed calls. */
    private static long medianMs(int runs, Runnable r) {
        long[] ms = new long[runs];
        for (int i = 0; i < runs; i++) {
            long start = System.nanoTime();
            r.run();
            ms[i] = (System.nanoTime() - start) / 1_000_000;
        }
        java.util.Arrays.sort(ms);
        return ms[runs / 2];
    }

    @Test
    void aFanOutArmWhoseRerunRunsIntoItsBound_isMarkedTimedOut_notServedAShortPage() {
        // nexus-8ptg4. A starved arm (the scan cap reaches 1 of 3 rows) re-runs exact under the arm's own statement
        // bound. The bound is calibrated against this box: a fraction of what the exact route alone costs, so the walk
        // and the manifest count fit inside it and the re-run does not. Before the fix the cancelled re-run was
        // swallowed and the arm returned its one-row page as if the answer were complete.
        PgSession.overrideSearchExactMaxRowsForTests(1_000_000);
        long exactMs = medianMs(3, () -> vec.search(TENANT, QUERY, List.of(COLLECTION), 10, rare()));
        PgSession.resetSearchExactMaxRowsForTests();
        PgSession.overrideScanBudgetForTests(1000, 100);
        int boundMs = (int) Math.max(60, exactMs * 2 / 5);
        long deadlinesBefore = PgVectorRepository.shortPageRerunDeadlineCount();

        var result = vec.searchPerCollection(TENANT, QUERY, List.of(COLLECTION), 10, 10, null, rare(), false,
            new FanoutSettings(1, 60_000, boundMs));

        assertThat(PgVectorRepository.shortPageRerunDeadlineCount() - deadlinesBefore)
            .as("one arm deadline was met at the re-run, counted apart from a count's (exact route alone %d ms, "
                + "bound %d ms)", exactMs, boundMs)
            .isEqualTo(1);
        assertThat(result.perCollection()).hasSize(1);
        assertThat(result.perCollection().get(0).errorKind()).isEqualTo(ArmErrorKind.STATEMENT_TIMEOUT);
        assertThat(result.rows()).as("no short page is served as the arm's answer").isEmpty();
    }
}
