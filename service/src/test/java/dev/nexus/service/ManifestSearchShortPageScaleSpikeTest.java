// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.PgVectorRepository;
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
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

/**
 * nexus-8ptg4 spike: what the short-page exact re-run ({@code exactOnUnderReturn} with {@code rerunWhenShort},
 * nexus-6pfgb.61) costs on a collection of 220,000 chunks, the scale RDR-236 assumption A8 names, and what the
 * candidate discriminator (a bounded manifest-side count of qualifying chunks) costs beside it. It asserts nothing and
 * runs only with {@code -Dnx.spike=true}; the numbers go on the bead.
 *
 * <p>Fixture. One document, {@value #N} chunks on a quarter circle, so the query (1, 0) ranks them in index order. The
 * manifest labels {@code section_type}: {@code few-x} on 30 chunks inside the first 100,000 ranks (a filter that
 * genuinely matches fewer rows than the page asks, which the walk finds whole); {@code uniform-x} on every hundredth
 * chunk (1 percent, spread, which fills the page); {@code far-x} on 5 early chunks and 2,195 beyond rank 205,000 (1
 * percent, far from the query, which the default 200,000-tuple scan cap cannot reach: A8's shape, a short non-empty
 * page that is starved); {@code only-far-x} on 2,200 chunks beyond rank 212,000 (the walk finds none: the empty-page
 * re-run of nexus-bq06h). Everything else is {@code body}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestSearchShortPageScaleSpikeTest {

    private static final String SVC_ROLE = "svc_short_page_scale";
    private static final String SVC_PASS = "svc_short_page_scale_pass";

    private static final String TENANT = "short-page-scale";
    private static final String COLLECTION = "docs__short-page-scale__minilm-l6-v2-384__v1";
    private static final String MODEL = "minilm-l6-v2-384";
    private static final String DOC = "sps-doc";
    private static final String QUERY = "spikeprobe";
    static final int N = 220_000;
    private static final int LIMIT = 10;
    private static final int WIDENED = 40;

    private static String chash(int i) {
        return Chash.ofText("sps-" + i).toHex();
    }

    private static String text(int g) {
        return "spikeprobe chunk " + g;
    }

    static String labelOf(int g) {
        if (g >= 2000 && g < 2005) return "far-x";
        if (g >= 205_000 && g < 207_195) return "far-x";
        if (g >= 212_000 && g < 214_200) return "only-far-x";
        if (g >= 1000 && g < 100_000 && (g - 1000) % 3000 == 0 && (g - 1000) / 3000 < 30) return "few-x";
        if (g % 100 == 50) return "uniform-x";
        return "body";
    }

    /** (cos, sin, 0, ...) by the chunk number in the text; the query is (1, 0). */
    private static final class QuarterCircle implements Embedder {
        @Override
        public List<float[]> embed(List<String> texts) {
            List<float[]> out = new ArrayList<>(texts.size());
            for (String t : texts) {
                double theta = 0.0;
                if (t.startsWith("spikeprobe chunk ")) {
                    int g = Integer.parseInt(t.substring("spikeprobe chunk ".length()));
                    theta = (Math.PI / 2.0) * g / (N - 1);
                }
                float[] v = new float[384];
                v[0] = (float) Math.cos(theta);
                v[1] = (float) Math.sin(theta);
                out.add(v);
            }
            return out;
        }
    }

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    PgVectorRepository vec;

    @BeforeAll
    void startAll() throws Exception {
        if (!"true".equals(System.getProperty("nx.spike"))) {
            return;
        }
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
        var embedder = new QuarterCircle();
        vec = new PgVectorRepository(new TenantScope(svcDs), embedder, embedder);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(seed, TENANT, COLLECTION);
            seed.insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
                .set(CATALOG_DOCUMENTS.TUMBLER, DOC)
                .set(CATALOG_DOCUMENTS.TITLE, "short-page-scale.md")
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, COLLECTION)
                .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///short-page-scale.md")
                .execute();
        }
        long t0 = System.nanoTime();
        for (int from = 0; from < N; from += 500) {
            List<String> ids = new ArrayList<>();
            List<String> texts = new ArrayList<>();
            List<Map<String, Object>> metas = new ArrayList<>();
            for (int g = from; g < Math.min(N, from + 500); g++) {
                ids.add(chash(g));
                texts.add(text(g));
                metas.add(new LinkedHashMap<>());
            }
            vec.upsertChunks(TENANT, COLLECTION, ids, texts, metas);
        }
        System.out.println("A8-SCALE seeded chunks in ms=" + (System.nanoTime() - t0) / 1_000_000);
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
            PgContainerHelper.vacuumTable(su, "nexus", "chunks");
            PgContainerHelper.vacuumTable(su, "nexus", "catalog_document_chunks");
            PgContainerHelper.analyzeTable(su, CHUNKS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENT_CHUNKS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
        }
        System.out.println("A8-SCALE seeded total ms=" + (System.nanoTime() - t0) / 1_000_000);
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

    private static Map<String, Object> where(String label) {
        Map<String, Object> w = new LinkedHashMap<>();
        w.put("section_type", label);
        return w;
    }

    /** Median and minimum milliseconds of {@code runs} searches; prints the row, as the bead records it. */
    private long[] timed(String what, Map<String, Object> where, int runs) {
        long[] ms = new long[runs];
        int rows = -1;
        long reruns0 = PgVectorRepository.shortPageRerunCount();
        long fallbacks0 = PgVectorRepository.exactFallbackCount();
        long skips0 = PgVectorRepository.shortPageSkipCount();
        for (int i = 0; i < runs; i++) {
            long start = System.nanoTime();
            rows = vec.search(TENANT, QUERY, List.of(COLLECTION), LIMIT, where).size();
            ms[i] = (System.nanoTime() - start) / 1_000_000;
        }
        long[] sorted = ms.clone();
        Arrays.sort(sorted);
        System.out.println("A8-SCALE " + what + ": rows=" + rows + " median_ms=" + sorted[runs / 2]
            + " min_ms=" + sorted[0] + " shortPageReruns/run=" + (PgVectorRepository.shortPageRerunCount() - reruns0) / (double) runs
            + " exactFallbacks/run=" + (PgVectorRepository.exactFallbackCount() - fallbacks0) / (double) runs
            + " shortPageSkips/run=" + (PgVectorRepository.shortPageSkipCount() - skips0) / (double) runs);
        return new long[] {sorted[runs / 2], rows};
    }

    /**
     * The candidate discriminator, as SQL driven from the manifest: the manifest occurrences of the collection whose
     * effective {@code section_type} is the label, joined to their chunk row for the fallback and the model, counted
     * up to the cap. The superuser connection stands in for the engine role (the tenant is in the predicate).
     */
    private long[] countCandidate(String label, int cap) throws Exception {
        String sql = """
            SELECT count(*) FROM (
              SELECT 1
                FROM nexus.catalog_document_chunks m
                JOIN nexus.catalog_documents d ON d.tenant_id = m.tenant_id AND d.tumbler = m.doc_id
                JOIN nexus.chunks c ON c.tenant_id = m.tenant_id AND c.collection = m.collection AND c.chash = m.chash
               CROSS JOIN LATERAL (SELECT jsonb_build_object('section_type',
                       CASE WHEN m.labels_written THEN to_jsonb(COALESCE(m.section_type, ''))
                            ELSE COALESCE(NULLIF(c.metadata -> 'section_type', 'null'::jsonb), '""'::jsonb) END) AS eff) e
               WHERE m.tenant_id = ? AND m.collection = ANY(?) AND d.deleted_at IS NULL
                 AND c.embedding_model = ? AND e.eff @> ?::jsonb
               LIMIT ?) t
            """;
        long[] ms = new long[3];
        long count = -1;
        try (Connection su = pg.createConnection("")) {
            for (int i = 0; i < ms.length; i++) {
                try (PreparedStatement ps = su.prepareStatement(sql)) {
                    ps.setString(1, TENANT);
                    ps.setArray(2, su.createArrayOf("text", new String[] {COLLECTION}));
                    ps.setString(3, MODEL);
                    ps.setString(4, "{\"section_type\":\"" + label + "\"}");
                    ps.setInt(5, cap);
                    long start = System.nanoTime();
                    try (ResultSet rs = ps.executeQuery()) {
                        rs.next();
                        count = rs.getLong(1);
                    }
                    ms[i] = (System.nanoTime() - start) / 1_000_000;
                }
            }
        }
        Arrays.sort(ms);
        System.out.println("A8-SCALE   count candidate (cap " + cap + ") label=" + label + ": count=" + count
            + " median_ms=" + ms[1] + " min_ms=" + ms[0]);
        return new long[] {ms[1], count};
    }

    @Test
    @EnabledIfSystemProperty(named = "nx.spike", matches = "true")
    void spike_measurements() throws Exception {
        System.out.println("A8-SCALE rows=" + N + " limit=" + LIMIT + " widened=" + WIDENED);
        timed("warmup unfiltered", null, 3);
        timed("unfiltered, default budget", null, 3);
        for (String label : List.of("body", "uniform-x", "few-x", "far-x", "only-far-x")) {
            System.out.println("A8-SCALE -- label " + label);
            timed(label + " HNSW route, default budget (with the short-page re-run)", where(label), 3);
            PgSession.overrideSearchExactMaxRowsForTests(10_000_000);
            timed(label + " exact route alone (router threshold raised)", where(label), 3);
            PgSession.resetSearchExactMaxRowsForTests();
            countCandidate(label, WIDENED + 1);
        }
    }
}
