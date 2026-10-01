// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Query;
import org.jooq.ResultQuery;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.COLLECTION_VECTOR_STATS;
import static dev.nexus.service.jooq.nexus.Tables.HYBRID_SEARCH_384;
import static dev.nexus.service.jooq.nexus.Tables.PLAIN_SEARCH_384;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TOPIC_SCOPED_384;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_BY_CHASH_384;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_HNSW_FIRST_384;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_384;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-192 Phase 2 gate finding (bead nexus-wbfpw.37, T2 nexus/critique-rdr-192-phase2 row 11):
 * plan evidence for the live(c) read paths vectors-019 and vectors-023 moved, beyond the two the
 * earlier work measured (plain search and {@code text_gated_search_hnsw_first}, in
 * {@code PlainSearchTextGatedSearchExplainTest} and {@code ChunkLiveOwnersMsz9iScaleIntegrationTest}).
 * Covered here: topic-scoped search, the SQL hybrid function, the hybrid dispatch's gate probe,
 * the by-chash rank, and the {@code collection_vector_stats} inventory view.
 *
 * <p><b>Role.</b> Every plan is taken as the production service role {@code nexus_svc}
 * (NOSUPERUSER NOBYPASSRLS, asserted below), inside {@link TenantScope#withTenant}, so the
 * row-level-security filter is part of the plan rather than bypassed by a superuser. The plans
 * are taken at the planner's own choice (no {@code enable_*} switch), because the question is
 * what the planner does with the inlined predicate, and each function is measured as a plain
 * {@code SELECT .. FROM fn(..)} so the plan is the one the repository's own call produces.
 *
 * <p><b>Fixture.</b> {@value #NUM_CHUNKS} 384-dim chunks (override {@code -Dnx.rdr192Explain.chunks}),
 * 90% manifested across {@value #NUM_DOCS} documents of which every third is tombstoned, 10%
 * manifest-less; so 63% of the chunks are live(c) and 37% are hidden by one of the two reasons live(c)
 * hides a chunk. Chunk text carries a rare token (selective gate, 1%) and a common one (dense gate,
 * 70%). One topic is assigned to the first {@value #TOPIC_CHUNKS} chunks.
 *
 * <p><b>What is pinned and why.</b> The one regression these queries share is the liveness
 * predicate stopping inlining: {@code nexus.chunk_live_owners} is a set-returning SQL function that
 * the planner folds into an indexed per-row probe of {@code catalog_document_chunks} and
 * {@code catalog_documents} (a SubPlan; PG 17 does not pull the EXISTS up into a semi-join, see T2
 * nexus/rdr-192-reapable-plans-and-decisions-2026-10-01) only while it stays a plain inlinable
 * {@code LANGUAGE sql} function (vectors-018's header; SECURITY DEFINER, a {@code SET} clause
 * or a volatile marking each break it). If it stopped inlining, every chunk read would call it as an
 * opaque function per row, a cost no result-set test sees. Each plan below asserts the function name is
 * absent and the probe's target table is present. The stats view carries one further pin: a single
 * shared probe per chunk. The remaining plan facts (index choices, row estimates, costs) are
 * written to {@code target/rdr192-explain-evidence.txt} for the record, not pinned: they move with
 * fixture size and PG minor version, and a pin on them would fail for reasons that are not regressions.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr192LiveCExplainEvidenceIntegrationTest {

    private static final String TENANT = "rdr192-explain";
    private static final String COLL = "knowledge__rdr192-explain__minilm-l6-v2-384__v1";
    private static final int DIM = 384;
    static final int NUM_CHUNKS = Integer.getInteger("nx.rdr192Explain.chunks", 24_000);
    static final int NUM_DOCS = 600;
    static final int TOPIC_CHUNKS = 2_000;
    static final int TIMED_RUNS = 5;
    private static final String TOPIC_LABEL = "rdr192-explain-topic";
    // Tokens share no trigram with the filler text ("rdr192 explain fixture chunk N alpha bravo"): a
    // token that did (the first cut used "rdr192rare") puts its trigrams in every row, the GIN trigram
    // index then cannot narrow the candidate set, and the probe's selective-gate plan is a sequential
    // scan that says nothing about the indexed case.
    private static final String RARE_TOKEN = "qzvkwx";
    private static final String COMMON_TOKEN = "jmpthy";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    final List<String> chashHex = new ArrayList<>();
    final List<float[]> vectors = new ArrayList<>();
    final Map<String, String> evidence = new LinkedHashMap<>();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        seed();
    }

    @AfterAll
    void stopAll() throws IOException {
        writeEvidence();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── fixture ─────────────────────────────────────────────────────────────

    private void seed() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, COLL);
        }
        Random rnd = new Random(20261001037L);
        var repo = new PgVectorRepository(tenantScope, (Embedder) null, (Embedder) null);
        int batch = 1000;
        for (int start = 0; start < NUM_CHUNKS; start += batch) {
            int end = Math.min(start + batch, NUM_CHUNKS);
            List<String> ids = new ArrayList<>();
            List<String> texts = new ArrayList<>();
            List<float[]> vecs = new ArrayList<>();
            List<Map<String, Object>> metas = new ArrayList<>();
            for (int i = start; i < end; i++) {
                String id = Chash.ofText("rdr192-explain-chunk-" + i).toHex();
                chashHex.add(id);
                float[] v = unitVector(rnd);
                vectors.add(v);
                ids.add(id);
                vecs.add(v);
                String text = "rdr192 explain fixture chunk " + i + " alpha bravo";
                if (i % 100 == 0) text = RARE_TOKEN + " " + text;          // 1%: selective gate
                if (i % 10 < 7) text = COMMON_TOKEN + " " + text;          // 70%: dense gate
                texts.add(text);
                metas.add(Map.of());
            }
            repo.upsertChunksWithVectors(TENANT, COLL, ids, texts, vecs, metas);
        }

        int manifested = NUM_CHUNKS * 9 / 10;
        int perDoc = Math.max(1, manifested / NUM_DOCS);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            List<Query> docs = new ArrayList<>();
            for (int d = 0; d < NUM_DOCS; d++) {
                docs.add(ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID,
                        CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.CONTENT_TYPE,
                        CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
                    .values(TENANT, doc(d), "Doc " + d, "prose", COLL));
            }
            ctx.batch(docs).execute();
            su.commit();
            for (int start = 0; start < manifested; start += 5000) {
                int end = Math.min(start + 5000, manifested);
                List<Query> rows = new ArrayList<>();
                for (int i = start; i < end; i++) {
                    int d = Math.min(NUM_DOCS - 1, i / perDoc);
                    rows.add(ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
                            CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION,
                            CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION)
                        .values(TENANT, doc(d), i - d * perDoc, HexFormat.of().parseHex(chashHex.get(i)), COLL));
                }
                ctx.batch(rows).execute();
                su.commit();
            }
            // Tombstone every third document (scattered), the msz9i methodology.
            for (int d = 0; d < NUM_DOCS; d += 3) {
                ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.DELETED_AT, OffsetDateTime.now())
                   .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.TUMBLER.eq(doc(d))))
                   .execute();
            }
            su.commit();

            long topicId = 9_100_000_001L;
            ctx.insertInto(TOPICS, TOPICS.ID, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION,
                           TOPICS.DOC_COUNT, TOPICS.CREATED_AT, TOPICS.REVIEW_STATUS)
               .values(topicId, TENANT, TOPIC_LABEL, COLL, TOPIC_CHUNKS, OffsetDateTime.now(), "pending")
               .execute();
            for (int start = 0; start < TOPIC_CHUNKS; start += 1000) {
                List<Query> rows = new ArrayList<>();
                for (int i = start; i < Math.min(start + 1000, TOPIC_CHUNKS); i++) {
                    rows.add(ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID,
                            TOPIC_ASSIGNMENTS.DOC_ID, TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY,
                            TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, TOPIC_ASSIGNMENTS.ASSIGNED_AT)
                        .values(TENANT, HexFormat.of().parseHex(chashHex.get(i)), topicId, "projection", COLL,
                                OffsetDateTime.now()));
                }
                ctx.batch(rows).execute();
                su.commit();
            }
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.analyzeTable(su, CHUNKS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENT_CHUNKS);
            PgContainerHelper.analyzeTable(su, TOPIC_ASSIGNMENTS);
            PgContainerHelper.analyzeTable(su, TOPICS);
        }
    }

    private static String doc(int d) {
        return String.format("rdr192-explain-doc-%04d", d);
    }

    private static float[] unitVector(Random rnd) {
        float[] v = new float[DIM];
        double norm = 0;
        for (int i = 0; i < DIM; i++) {
            v[i] = (float) rnd.nextGaussian();
            norm += v[i] * v[i];
        }
        float inv = (float) (1.0 / Math.sqrt(norm));
        for (int i = 0; i < DIM; i++) v[i] *= inv;
        return v;
    }

    // ── the role the plans are taken as ─────────────────────────────────────

    /**
     * Non-vacuity for every plan below: it was taken as {@code nexus_svc}, which is neither a
     * superuser nor BYPASSRLS. A plan taken as the container superuser would carry no RLS qual
     * and prove nothing about production.
     */
    @Test
    void plansAreTakenAsTheProductionRoleUnderRls() {
        Table<?> roles = DSL.table(DSL.name("pg_catalog", "pg_roles"));
        Field<String> name = DSL.field(DSL.name("rolname"), String.class);
        Field<Boolean> superuser = DSL.field(DSL.name("rolsuper"), Boolean.class);
        Field<Boolean> bypass = DSL.field(DSL.name("rolbypassrls"), Boolean.class);
        tenantScope.withTenant(TENANT, ctx -> {
            var row = ctx.select(name, superuser, bypass).from(roles).where(name.eq(DSL.currentUser())).fetchOne();
            assertThat(row).as("current role must be visible in pg_roles").isNotNull();
            assertThat(row.value1()).isEqualTo(PgContainerHelper.SVC_USERNAME);
            assertThat(row.value2()).as("nexus_svc must not be a superuser").isFalse();
            assertThat(row.value3()).as("nexus_svc must not bypass RLS").isFalse();
            return null;
        });
        String plan = explain("rls_probe", ctx -> ctx.selectCount().from(CHUNKS)
            .where(CHUNKS.COLLECTION.eq(COLL)));
        assertThat(plan).as("the plan must carry the row-level-security qual").contains("tenant_id");
    }

    // ── topic-scoped ────────────────────────────────────────────────────────

    @Test
    void topicScopedSearch_inlinesLiveC() {
        Table<?> fn = SEARCH_TOPIC_SCOPED_384.call(queryVec(), TOPIC_LABEL, COLL, 10);
        String plan = explain("search_topic_scoped_384", ctx -> ctx.select(fn.field("id")).from(fn));
        assertInlinedLiveC(plan, "search_topic_scoped_384");
    }

    // ── hybrid ──────────────────────────────────────────────────────────────

    @Test
    void hybridSearchFunction_inlinesLiveC() {
        Table<?> fn = HYBRID_SEARCH_384.call(queryVec(), COMMON_TOKEN, new String[] {COLL}, null, 10);
        String plan = explain("hybrid_search_384", ctx -> ctx.select(fn.field("id")).from(fn));
        assertInlinedLiveC(plan, "hybrid_search_384");
    }

    @Test
    void gateProbe_selective_inlinesLiveC() {
        Table<?> fn = TEXT_GATE_PROBE_384.call(RARE_TOKEN, new String[] {COLL}, null, null,
            PgVectorRepository.SELECTIVE_GATE_MAX + 1);
        String plan = explain("text_gate_probe_384 (selective gate)", ctx -> ctx.selectFrom(fn));
        assertInlinedLiveC(plan, "text_gate_probe_384");
    }

    @Test
    void gateProbe_dense_inlinesLiveC() {
        Table<?> fn = TEXT_GATE_PROBE_384.call(COMMON_TOKEN, new String[] {COLL}, null, null,
            PgVectorRepository.SELECTIVE_GATE_MAX + 1);
        String plan = explain("text_gate_probe_384 (dense gate)", ctx -> ctx.selectFrom(fn));
        assertInlinedLiveC(plan, "text_gate_probe_384");
    }

    /**
     * Evidence only (nothing here asserts a plan shape): the selective gate's plan with sequential scans
     * penalised, to record whether the GIN text indexes offer an indexed alternative and what it costs
     * and returns. The planner's own choice above is a sequential scan for this gate.
     */
    @Test
    void gateProbe_selective_withSeqscanOff_recordsTheIndexedAlternative() {
        Table<?> fn = TEXT_GATE_PROBE_384.call(RARE_TOKEN, new String[] {COLL}, null, null,
            PgVectorRepository.SELECTIVE_GATE_MAX + 1);
        String plan = explainWith("text_gate_probe_384 (selective gate, enable_seqscan=off)",
            List.of("enable_seqscan"), ctx -> ctx.selectFrom(fn));
        assertInlinedLiveC(plan, "text_gate_probe_384");
    }

    @Test
    void byChashRank_inlinesLiveC() {
        List<String> some = new ArrayList<>();
        for (int i = 0; i < NUM_CHUNKS; i += 100) some.add(chashHex.get(i));
        byte[][] chashes = some.stream().map(h -> HexFormat.of().parseHex(h)).toArray(byte[][]::new);
        Table<?> fn = TEXT_GATED_SEARCH_BY_CHASH_384.call(queryVec(), chashes, new String[] {COLL}, null, null, 10);
        String plan = explain("text_gated_search_by_chash_384", ctx -> ctx.select(fn.field("id")).from(fn));
        assertInlinedLiveC(plan, "text_gated_search_by_chash_384");
    }

    @Test
    void hnswFirstRank_inlinesLiveC() {
        Table<?> fn = TEXT_GATED_SEARCH_HNSW_FIRST_384.call(queryVec(), COMMON_TOKEN, new String[] {COLL},
            null, null, 10);
        String plan = explain("text_gated_search_hnsw_first_384", ctx -> ctx.select(fn.field("id")).from(fn));
        assertInlinedLiveC(plan, "text_gated_search_hnsw_first_384");
    }

    @Test
    void plainSearch_inlinesLiveC_reference() {
        Table<?> fn = PLAIN_SEARCH_384.call(queryVec(), new String[] {COLL}, null, null, 10);
        String plan = explain("plain_search_384 (reference)", ctx -> ctx.select(fn.field("id")).from(fn));
        assertInlinedLiveC(plan, "plain_search_384");
    }

    // ── collection_vector_stats ─────────────────────────────────────────────

    @Test
    void collectionVectorStats_oneCollection_inlinesLiveC() {
        String plan = explain("collection_vector_stats (one collection)", ctx -> ctx
            .select(COLLECTION_VECTOR_STATS.COLLECTION, COLLECTION_VECTOR_STATS.CHUNK_COUNT,
                    COLLECTION_VECTOR_STATS.STORED_COUNT)
            .from(COLLECTION_VECTOR_STATS)
            .where(COLLECTION_VECTOR_STATS.COLLECTION.eq(COLL)));
        assertInlinedLiveC(plan, "collection_vector_stats");
        assertOneSharedLiveProbe(plan, "collection_vector_stats");
    }

    @Test
    void collectionVectorStats_wholeTenant_inlinesLiveC() {
        String plan = explain("collection_vector_stats (whole tenant)", ctx -> ctx
            .select(COLLECTION_VECTOR_STATS.COLLECTION, COLLECTION_VECTOR_STATS.CHUNK_COUNT,
                    COLLECTION_VECTOR_STATS.STORED_COUNT)
            .from(COLLECTION_VECTOR_STATS));
        assertInlinedLiveC(plan, "collection_vector_stats");
        assertOneSharedLiveProbe(plan, "collection_vector_stats");
    }

    /** The view must also report what the fixture constructed, so the plans above were taken
     *  over a population that has both kinds of hidden chunk. */
    @Test
    void fixtureHasLiveAndHiddenChunks() {
        tenantScope.withTenant(TENANT, ctx -> {
            var row = ctx.select(COLLECTION_VECTOR_STATS.CHUNK_COUNT, COLLECTION_VECTOR_STATS.STORED_COUNT)
                .from(COLLECTION_VECTOR_STATS)
                .where(COLLECTION_VECTOR_STATS.COLLECTION.eq(COLL)).fetchOne();
            assertThat(row.value2()).isEqualTo((long) NUM_CHUNKS);
            long live = row.value1();
            assertThat(live).as("live(c) hides the manifest-less 10% and the tombstoned third of the rest")
                .isBetween((long) (NUM_CHUNKS * 0.55), (long) (NUM_CHUNKS * 0.70));
            return null;
        });
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    /**
     * The liveness predicate must have inlined: no call to the function by name (a per-row
     * Function Scan or SubPlan over {@code chunk_live_owners} would show it), and the semi-join
     * target its body reads, {@code catalog_document_chunks}, must be in the plan.
     */
    private static void assertInlinedLiveC(String plan, String what) {
        assertThat(plan)
            .as("%s: chunk_live_owners must inline into a probe of catalog_document_chunks; its name "
                + "in the plan means it stayed an opaque per-row call. Plan was:%n%s", what, plan)
            .doesNotContain("chunk_live_owners")
            .contains("catalog_document_chunks");
        assertThat(plan)
            .as("%s: no Function Scan over the search function itself either. Plan was:%n%s", what, plan)
            .doesNotContain("Function Scan");
    }

    /**
     * The view reads {@code chunk_count} and {@code last_write} through the same live(c) test, and the
     * planner evaluates it ONCE per chunk and feeds both aggregates (one SubPlan). The view is O(chunks)
     * by construction, so a second probe per chunk would double a cost that every {@code list_collections}
     * pays; this fails if a change makes the two aggregates probe separately.
     */
    private static void assertOneSharedLiveProbe(String plan, String what) {
        assertThat(plan)
            .as("%s: live(c) must be one per-chunk probe shared by both aggregates. Plan was:%n%s", what, plan)
            .contains("SubPlan 1")
            .doesNotContain("SubPlan 2");
    }

    private Vector queryVec() {
        return Vector.of(vectors.get(7));
    }

    /**
     * EXPLAIN as nexus_svc inside the tenant scope, at the planner's own choice, recorded for the
     * evidence file together with the statement's measured wall-clock (one warm-up run, then the
     * median of {@value #TIMED_RUNS}) and the row count it returned. Timing is evidence only and is
     * never asserted: it moves with the box.
     */
    private String explain(String label, Function<DSLContext, ? extends ResultQuery<?>> queryBuilder) {
        return explainWith(label, List.of(), queryBuilder);
    }

    private String explainWith(String label, List<String> offGucs,
                               Function<DSLContext, ? extends ResultQuery<?>> queryBuilder) {
        return tenantScope.withTenant(TENANT, ctx -> {
            // The same serving GUCs the repository sets before an ordered vector fetch, so the plan is
            // the one production gets and not the one a default session gets.
            PgSession.setHnswEfSearch(ctx, 10);
            for (String guc : offGucs) PgSession.setLocal(ctx, guc, "off");
            ResultQuery<?> q = queryBuilder.apply(ctx);
            String plan = ctx.explain(q).plan();
            int rows = q.fetch().size();                       // warm-up
            long[] ms = new long[TIMED_RUNS];
            for (int i = 0; i < TIMED_RUNS; i++) {
                long t0 = System.nanoTime();
                q.fetch();
                ms[i] = (System.nanoTime() - t0) / 1_000_000;
            }
            java.util.Arrays.sort(ms);
            synchronized (evidence) {
                evidence.put(label, "rows=" + rows + "  p50=" + ms[TIMED_RUNS / 2] + " ms  (runs "
                    + java.util.Arrays.toString(ms) + ")\n" + plan);
            }
            return plan;
        });
    }

    private void writeEvidence() throws IOException {
        Path out = Paths.get(System.getProperty("user.dir"), "target", "rdr192-explain-evidence.txt");
        Files.createDirectories(out.getParent());
        StringBuilder sb = new StringBuilder();
        sb.append("RDR-192 live(c) plan evidence (nexus-wbfpw.37), role nexus_svc, ")
          .append(NUM_CHUNKS).append(" chunks, ").append(NUM_DOCS).append(" documents\n");
        synchronized (evidence) {
            evidence.forEach((label, plan) ->
                sb.append("\n=== ").append(label).append(" ===\n").append(plan).append('\n'));
        }
        Files.writeString(out, sb.toString());
    }
}
