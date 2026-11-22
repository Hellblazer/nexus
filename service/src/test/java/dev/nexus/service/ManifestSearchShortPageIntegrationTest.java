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
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
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
    /** nexus-6pfgb.41: the same grants as {@link #SVC_ROLE}, with the planner held to the HNSW index; see {@link #hnswDs}. */
    private static final String SVC_ROLE_HNSW = "svc_short_page_hnsw";

    private static final String TENANT = "short-page";
    private static final String COLLECTION = "docs__short-page__minilm-l6-v2-384__v1";
    private static final String MODEL = "minilm-l6-v2-384";
    private static final String DOC = "sp-doc";
    private static final String QUERY = "spikeprobe";
    /** nexus-zk7rw: a query whose text gate admits the rank-5 and rank-6 chunks only (see {@link #text}). */
    private static final String GATE_QUERY = "alphagate";
    private static final int N = 21_000;
    private static final List<Integer> RARE = List.of(5, 10_000, 20_900);
    /** nexus-zk7rw: a label on one chunk past the 1,000-tuple scan cap, so an empty page has something to repair. */
    private static final int TAIL = 20_950;
    /** nexus-zk7rw: a label whose one chunk is owned, with that label, by two documents (DOC and DOC_TWIN). */
    private static final int TWIN = 20_970;
    /** nexus-zk7rw: a label on a chunk the capped scan reaches (7, owned twice) and one it does not (20,960). */
    private static final List<Integer> MIXED = List.of(7, 20_960);
    /**
     * nexus-zk7rw review r1: a chunk with FOUR qualifying owners (DOC, and DOC_TWIN at three positions). A page of one
     * row gives a cap of 2 and a distinct read of 4 occurrences: the bound fills with one distinct chunk.
     */
    private static final int QUAD = 20_980;
    private static final String DOC_TWIN = "sp-doc-twin";

    private static String chash(int i) {
        return Chash.ofText("sp-" + i).toHex();
    }

    private static String text(int g) {
        // nexus-zk7rw: the rank-5 chunk (a rare one) and the rank-6 chunk (not) carry a word of their own, so a text gate
        // on it admits those two and nothing else, and selectiveGateMax 1 sends it down the dense branch.
        return "spikeprobe chunk " + g + (g == 5 || g == 6 ? " alphagate" : "");
    }

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    PgVectorRepository vec;
    /**
     * nexus-6pfgb.41: a service role of the same grants whose connections keep the planner on the HNSW index (the sequential
     * scan, bitmap scan, hash join and sort are costed out, as {@code PgVectorRepositoryRawSqlPlanShapeTest} does to
     * prove reachability). Route (b) has no cardinality router, so which plan it runs on 21,000 rows is the planner's
     * choice, and on the natural one it does not walk the capped index. This repository is the one on which a route (b)
     * page can be truncated by the scan cap, which is what the repair exists for.
     */
    HikariDataSource hnswDs;
    PgVectorRepository vecHnsw;

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
        embedder.register(GATE_QUERY, 1.0f, 0.0f);
        for (int g = 0; g < N; g++) {
            double theta = (Math.PI / 2.0) * g / (N - 1);
            embedder.register(text(g), (float) Math.cos(theta), (float) Math.sin(theta));
        }
        vec = new PgVectorRepository(tenantScope, embedder, embedder);
        // The role-level planner settings reach only connections opened after them, so they are set before the pool.
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE_HNSW, SVC_PASS);
            for (String guc : List.of("enable_seqscan", "enable_bitmapscan", "enable_sort", "enable_hashjoin")) {
                PgContainerHelper.setRolePlannerGuc(su, SVC_ROLE_HNSW, guc, "off");
            }
        }
        var hnswCfg = new HikariConfig();
        hnswCfg.setJdbcUrl(pg.getJdbcUrl());
        hnswCfg.setUsername(SVC_ROLE_HNSW);
        hnswCfg.setPassword(SVC_PASS);
        hnswCfg.setMaximumPoolSize(2);
        hnswCfg.setAutoCommit(true);
        hnswDs = new HikariDataSource(hnswCfg);
        vecHnsw = new PgVectorRepository(new TenantScope(hnswDs), embedder, embedder);

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
            // nexus-6pfgb.41: the aspect-scoped route (b) function joins document_aspects.
            seed.insertInto(DOCUMENT_ASPECTS)
                .set(DOCUMENT_ASPECTS.TENANT_ID, TENANT)
                .set(DOCUMENT_ASPECTS.COLLECTION, COLLECTION)
                .set(DOCUMENT_ASPECTS.SOURCE_PATH, "short-page.md")
                .set(DOCUMENT_ASPECTS.PROPOSED_METHOD, "gradient descent")
                .set(DOCUMENT_ASPECTS.CONFIDENCE, 0.9)
                .set(DOCUMENT_ASPECTS.EXTRACTED_AT, OffsetDateTime.parse("2026-03-01T00:00:00Z"))
                .set(DOCUMENT_ASPECTS.MODEL_VERSION, "sp-test-model")
                .set(DOCUMENT_ASPECTS.EXTRACTOR_NAME, "sp-test-extractor")
                .set(DOCUMENT_ASPECTS.DOC_ID, DOC)
                .set(DOCUMENT_ASPECTS.SOURCE_URI, "file:///short-page.md")
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
                        labelOf(g), true);
                }
                step.execute();
            }
            // nexus-zk7rw: a second document owning the chunks whose owners are counted. It labels them the way the
            // first one does, so a manifest count that counts owners sees two qualifying owners of one chunk.
            seed.insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
                .set(CATALOG_DOCUMENTS.TUMBLER, DOC_TWIN)
                .set(CATALOG_DOCUMENTS.TITLE, "short-page-twin.md")
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, COLLECTION)
                .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///short-page-twin.md")
                .execute();
            int position = 0;
            for (int g : List.of(7, TWIN, QUAD, QUAD, QUAD)) {
                seed.insertInto(CATALOG_DOCUMENT_CHUNKS,
                        CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                        CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                        CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL,
                        CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE, CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN)
                    .values(TENANT, DOC_TWIN, position++, HexFormat.of().parseHex(chash(g)), COLLECTION, MODEL,
                        labelOf(g), true)
                    .execute();
            }
            PgContainerHelper.analyzeTable(su, CHUNKS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENT_CHUNKS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
        }
    }

    /** The manifest label of chunk {@code g} in DOC (and, for the owned-twice chunks, in DOC_TWIN). */
    private static String labelOf(int g) {
        if (RARE.contains(g)) return "appendix-x";
        if (g == TAIL) return "tail-x";
        if (g == TWIN) return "twin-x";
        if (g == QUAD) return "quad-x";
        if (MIXED.contains(g)) return "mixed-x";
        return "body";
    }

    @AfterAll
    void stopAll() {
        if (hnswDs != null) hnswDs.close();
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

    /**
     * Timed searches after a warm-up; prints the rows of the last and the median and minimum milliseconds (to the
     * tenth), for the bead. nexus-qpt52 raised the sample from 5 to 31 and made the clock read fractional: the common
     * label is an 18 ms measurement and a plan-time cost of a millisecond or two is what it is looking for.
     */
    private List<Map<String, Object>> timed(String label, Map<String, Object> where) {
        return timedCall(label, () -> vec.search(TENANT, QUERY, List.of(COLLECTION), 10, where));
    }

    /** The same, over a route (b) family (nexus-qpt52: its object grew by six keys, paid per call in plan time). */
    private List<Map<String, Object>> timedB(String label, String family, Map<String, Object> where) {
        return timedCall(label, () -> routeB(vec, family, where));
    }

    private List<Map<String, Object>> timedCall(String label, java.util.function.Supplier<List<Map<String, Object>>> call) {
        for (int i = 0; i < 3; i++) {
            call.get();
        }
        long[] ns = new long[31];
        List<Map<String, Object>> rows = null;
        for (int i = 0; i < ns.length; i++) {
            long start = System.nanoTime();
            rows = call.get();
            ns[i] = System.nanoTime() - start;
        }
        java.util.Arrays.sort(ns);
        System.out.println("A8-SPIKE " + label + ": rows=" + rows.size()
            + String.format(java.util.Locale.ROOT, " median_ms=%.1f min_ms=%.1f", ns[ns.length / 2] / 1e6, ns[0] / 1e6)
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
        PgSession.resetScanBudgetForTests();
        // nexus-qpt52: route (b) on the same fixture. The common label is the plan-time probe: the object grew by six
        // keys, each folded away by manifest_names_key, and the engine inlines the body on every call.
        for (String family : List.of("metadata", "aspect", "graph")) {
            timedB("route (b) " + family + ": common label (body)", family, body);
        }
        Map<String, Object> notBody = new LinkedHashMap<>();
        notBody.put("section_type", Map.of("$ne", "appendix-x"));
        timedB("route (b) metadata: common label negated (section_type != appendix-x)", "metadata", notBody);
        Map<String, Object> year = new LinkedHashMap<>();
        year.put("bib_year", Map.of("$gte", 2000));
        timedB("route (b) metadata: bib_year >= 2000 (no document is enriched, so none qualifies)", "metadata", year);
    }

    private static long reruns() {
        return PgVectorRepository.shortPageRerunCount();
    }

    private static long skips() {
        return PgVectorRepository.shortPageSkipCount();
    }

    /** nexus-h7kw2: how many distinct counts read twice the cap because the cap's first read held repeats. */
    private static long widens() {
        return PgVectorRepository.qualifyingWidenCount();
    }

    @Test
    void aRareLabelPastTheScanCap_isRepairedByOneExactRerun() {
        PgSession.overrideScanBudgetForTests(1000, 100);
        long before = reruns();
        long skipsBefore = skips();
        long widensBefore = widens();

        var rows = vec.search(TENANT, QUERY, List.of(COLLECTION), 10, rare());

        // nexus-h7kw2: the page is one row, the cap 2, and the first read of 2 qualifying rows is two chunks with one
        // owner each: the count is decided at the cap and nothing is read twice as wide.
        assertThat(widens() - widensBefore).as("two distinct chunks in the cap's first read: no widening").isZero();
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
        long widensBefore = widens();

        var rows = vec.search(TENANT, QUERY, List.of(COLLECTION), 10, rare());

        assertThat(ids(rows)).containsExactlyElementsOf(rareChashes());
        assertThat(reruns() - before).isZero();
        assertThat(skips() - skipsBefore).isEqualTo(1);
        // nexus-h7kw2: a page of 3, a cap of 4, a first read of 3 (the function ran out): exact, no second read.
        assertThat(widens() - widensBefore).as("the function ran out under the cap: no widening").isZero();
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

    // ── nexus-zk7rw: the count's grain and the empty page ────────────────────────────────────────────────────────────

    private static List<String> distinctIds(List<Map<String, Object>> rows) {
        return ids(rows).stream().distinct().collect(Collectors.toList());
    }

    private static Map<String, Object> label(String name) {
        Map<String, Object> w = new LinkedHashMap<>();
        w.put("section_type", name);
        return w;
    }

    @Test
    void anEmptyPageWhoseManifestAdmitsNothing_isNotRerunExact() {
        // nexus-zk7rw item 5 (nexus-bq06h). No chunk carries the label, so the walk finds nothing and the exact re-run
        // of an empty page (3.6-4.0 s at 220,000 chunks) has nothing to find either: the manifest count is 0.
        long fallbacksBefore = PgVectorRepository.exactFallbackCount();
        long skipsBefore = skips();

        var rows = vec.search(TENANT, QUERY, List.of(COLLECTION), 10, label("no-such-label-x"));

        assertThat(rows).isEmpty();
        assertThat(PgVectorRepository.exactFallbackCount() - fallbacksBefore)
            .as("the empty-page exact re-run is not paid when the manifest admits no chunk").isZero();
        assertThat(skips() - skipsBefore).isEqualTo(1);
    }

    @Test
    void anEmptyPageWhoseManifestAdmitsAChunkPastTheScanCap_isStillRepairedExact() {
        // The guard for the item above: the capped walk (1,000 tuples) reaches none of the tail chunk, so the page is
        // empty, but the manifest admits it: the count is 1 and the exact re-run (nexus-bq06h) runs and finds it.
        PgSession.overrideScanBudgetForTests(1000, 100);
        long fallbacksBefore = PgVectorRepository.exactFallbackCount();
        long skipsBefore = skips();

        var rows = vec.search(TENANT, QUERY, List.of(COLLECTION), 10, label("tail-x"));

        assertThat(ids(rows)).containsExactly(chash(TAIL));
        assertThat(PgVectorRepository.exactFallbackCount() - fallbacksBefore).isEqualTo(1);
        assertThat(skips() - skipsBefore).isZero();
    }

    @Test
    void aShortPageWhoseOneChunkHasTwoQualifyingOwners_isNotRerun() {
        // nexus-zk7rw item 4. The default budget walks every tuple, so the one chunk is all the filter admits and the
        // page (1 row) is complete. The manifest holds TWO qualifying owners of it (DOC and DOC_TWIN): a count of
        // occurrences is 2 against a page of 1 and the O(collection) re-run is paid for nothing; a count of the
        // distinct (collection, chash) is 1 and it is skipped.
        long reruns = reruns();
        long skipsBefore = skips();
        long widensBefore = widens();

        var rows = vec.search(TENANT, QUERY, List.of(COLLECTION), 10, label("twin-x"));

        // The label stage hands back one row per qualifying owner of a chunk (ManifestLabelStage#expanded); the SQL
        // page it reads is one row per chunk, and that is the page the count is compared with.
        assertThat(distinctIds(rows)).containsExactly(chash(TWIN));
        assertThat(reruns() - reruns).isZero();
        assertThat(skips() - skipsBefore).isEqualTo(1);
        // nexus-h7kw2: the page is one row, the cap 2, and the first read of 2 is the two owners of the one chunk: fewer
        // distinct chunks than the cap, so the read is widened to 4 once, and the function runs out at 2.
        assertThat(widens() - widensBefore).as("two owners of one chunk fill the cap: widened once").isEqualTo(1);
    }

    @Test
    void aPageWhoseDistinctReadFillsItsBoundBelowTheCap_isRerun_notSkipped() {
        // nexus-zk7rw review r1 blocker 1. One chunk, four qualifying owners, a page of one row: the cap is 2 and the
        // distinct read asks for 4 occurrences and gets exactly 4 (the bound fills) over ONE distinct chunk, fewer than
        // the cap. The read cannot tell "there are no more" from "the bound stopped it", so it must answer the cap (an
        // over-count, a re-run), never the 1 distinct chunk it saw, which would skip a re-run that a larger set might
        // need. Here the chunk really is the only one, so the re-run finds nothing new and the rows are the same.
        long before = reruns();
        long skipsBefore = skips();
        long widensBefore = widens();

        var rows = vec.search(TENANT, QUERY, List.of(COLLECTION), 10, label("quad-x"));

        assertThat(distinctIds(rows)).containsExactly(chash(QUAD));
        assertThat(widens() - widensBefore).as("the cap's first read is two owners of one chunk: widened once")
            .isEqualTo(1);
        assertThat(reruns() - before).as("the bound filled, so the count reads as the cap and the page is re-run")
            .isEqualTo(1);
        assertThat(skips() - skipsBefore).isZero();
    }

    @Test
    void aStarvedPageWhoseReachedChunkHasTwoOwners_isStillRerun() {
        // The guard for the distinct count: chunk 7, which the capped walk reaches, has two owners, and chunk 20,960,
        // which it does not, has one. The page holds 1 row, the manifest admits 2 distinct chunks (3 owners), so the
        // re-run runs and returns both. A count that fell below the distinct chunks would skip it.
        PgSession.overrideScanBudgetForTests(1000, 100);
        long reruns = reruns();
        long skipsBefore = skips();

        var rows = vec.search(TENANT, QUERY, List.of(COLLECTION), 10, label("mixed-x"));

        assertThat(distinctIds(rows)).containsExactly(chash(7), chash(20_960));
        assertThat(reruns() - reruns).isEqualTo(1);
        assertThat(skips() - skipsBefore).isZero();
    }

    @Test
    void theHybridDenseBranch_countsOnlyTheChunksItsTextGateAdmits_soAGatedShortPageIsNotRerun() {
        // nexus-zk7rw item 3. The query text is also the text gate: "alphagate" admits the rank-5 chunk and the
        // rank-6 one, and not the other two chunks the label qualifies (10,000 and 20,900), so the page is ONE row and complete (the default
        // budget walks every tuple). The manifest count ignored the gate and said 3 qualifying chunks against a page of 1,
        // so the O(collection) exact re-run was paid and found nothing more; counted under the same gate it is 1.
        long before = reruns();
        long skipsBefore = skips();

        var rows = vec.hybridSearchWithTokens(TENANT, GATE_QUERY, List.of(COLLECTION), 10, rare(), false, 1).value();

        assertThat(distinctIds(rows)).containsExactly(chash(5));
        assertThat(reruns() - before).as("a page the text gate leaves short is not a starved one").isZero();
        assertThat(skips() - skipsBefore).isEqualTo(1);
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
        // and the manifest count usually fit inside it and the re-run does not. Before the fix the cancelled re-run
        // was swallowed and the arm returned its one-row page as if the answer were complete. On a loaded box the
        // count itself can meet the bound first (develop CI, run 41660277027: count cancelled at a 144 ms bound), so
        // the pin is one deadline met at the count OR the re-run; which one is pinned deterministically in
        // ShortPageRerunFailureTest. A bound met by the first walk alone counts neither and fails this pin, so it
        // cannot pass on an arm that never reached the short-page path.
        PgSession.overrideSearchExactMaxRowsForTests(1_000_000);
        long exactMs = medianMs(3, () -> vec.search(TENANT, QUERY, List.of(COLLECTION), 10, rare()));
        PgSession.resetSearchExactMaxRowsForTests();
        PgSession.overrideScanBudgetForTests(1000, 100);
        int boundMs = (int) Math.max(60, exactMs * 2 / 5);
        long deadlinesBefore = PgVectorRepository.shortPageRerunDeadlineCount()
            + PgVectorRepository.shortPageCountDeadlineCount();

        var result = vec.searchPerCollection(TENANT, QUERY, List.of(COLLECTION), 10, 10, null, rare(), false,
            new FanoutSettings(1, 60_000, boundMs));

        assertThat(PgVectorRepository.shortPageRerunDeadlineCount() + PgVectorRepository.shortPageCountDeadlineCount()
                   - deadlinesBefore)
            .as("one arm deadline was met on the short-page path, at the count or the re-run (exact route alone %d ms, "
                + "bound %d ms)", exactMs, boundMs)
            .isEqualTo(1);
        assertThat(result.perCollection()).hasSize(1);
        assertThat(result.perCollection().get(0).errorKind()).isEqualTo(ArmErrorKind.STATEMENT_TIMEOUT);
        assertThat(result.rows()).as("no short page is served as the arm's answer").isEmpty();
    }

    // ── nexus-6pfgb.41: the route (b) functions carry the same manifest predicate and the same repair ────────────────

    /** One route (b) search per family over the fixture collection, for the label filter {@code where}. */
    private List<Map<String, Object>> routeB(String family, Map<String, Object> where) {
        return routeB(vec, family, where);
    }

    private List<Map<String, Object>> routeB(PgVectorRepository vec, String family, Map<String, Object> where) {
        return switch (family) {
            case "metadata" -> vec.searchMetadataScoped(TENANT, QUERY, List.of(COLLECTION), null, null, null, null,
                null, where, 10);
            case "aspect" -> vec.searchAspectScopedWithTokens(TENANT, QUERY, List.of(COLLECTION), null, null, null,
                where, 10).value();
            case "graph" -> vec.searchGraphHop(TENANT, QUERY, List.of(DOC), List.of(COLLECTION), null, 1, "both",
                where, 10);
            default -> throw new IllegalArgumentException(family);
        };
    }

    private static List<String> chashes(List<Map<String, Object>> rows) {
        return rows.stream().map(r -> (String) r.get("chash")).collect(Collectors.toList());
    }

    @org.junit.jupiter.params.ParameterizedTest
    @org.junit.jupiter.params.provider.ValueSource(strings = {"metadata", "aspect", "graph"})
    void routeB_aRareLabelPastTheScanCap_isRepairedByOneExactRerun(String family) {
        PgSession.overrideScanBudgetForTests(1000, 100);
        long before = reruns();

        var rows = routeB(vecHnsw, family, rare());

        // Without the re-run this page holds the rank-5 chunk alone: the walk stops at the 1,000-tuple cap.
        assertThat(chashes(rows)).containsExactlyElementsOf(rareChashes());
        assertThat(reruns() - before).isEqualTo(1);
    }

    @org.junit.jupiter.params.ParameterizedTest
    @org.junit.jupiter.params.provider.ValueSource(strings = {"metadata", "graph"})
    void routeB_aFilterThatGenuinelyMatchesFewRows_isCountedNotRerun(String family) {
        // nexus-8ptg4 on route (b): the default budget walks every tuple, so the three rows are all the filter admits.
        // manifest_qualifying_hits counts three occurrences against a page of three and the O(collection) re-run is
        // skipped. Without the count passed to the re-run the same page paid the re-run (reruns +1, skips 0).
        long before = reruns();
        long skipsBefore = skips();

        var rows = routeB(vecHnsw, family, rare());

        assertThat(chashes(rows)).containsExactlyElementsOf(rareChashes());
        assertThat(reruns() - before).isZero();
        assertThat(skips() - skipsBefore).isEqualTo(1);
    }

    @Test
    void routeB_theAspectFamily_isNeverCounted_aShortPageAlwaysPaysTheRerun() {
        // nexus-6pfgb.41 review r2: search_aspect_scoped inner-joins document_aspects on (tenant_id, doc_id), which
        // nothing makes unique, so one manifest occurrence can give several rows and the per-occurrence count could
        // fall below the page and skip a needed re-run. The aspect family is passed no count: the same short page that
        // the other two families skip is re-run here (reruns +1, skips 0), with the same rows.
        long before = reruns();
        long skipsBefore = skips();

        var rows = routeB(vecHnsw, "aspect", rare());

        assertThat(chashes(rows)).containsExactlyElementsOf(rareChashes());
        assertThat(reruns() - before).isEqualTo(1);
        assertThat(skips() - skipsBefore).isZero();
    }

    @org.junit.jupiter.params.ParameterizedTest
    @org.junit.jupiter.params.provider.ValueSource(strings = {"metadata", "aspect", "graph"})
    void routeB_aPageTheScanFills_orAChunkRowFilter_isNotRerun(String family) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("section_type", "body");
        Map<String, Object> lang = new LinkedHashMap<>();
        lang.put("lang", "rare");
        PgSession.overrideScanBudgetForTests(1000, 100);
        long before = reruns();

        assertThat(routeB(family, body)).hasSize(10);
        assertThat(routeB(family, null)).hasSize(10);
        // A chunk-row predicate carries no manifest predicate: route (b) ran it once before and still does.
        routeB(family, lang);

        assertThat(reruns() - before).isZero();
    }
}
