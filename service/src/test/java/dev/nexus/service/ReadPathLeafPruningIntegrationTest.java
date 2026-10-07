// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.db.TaxonomyRepository;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.TaxonomyCentroidRepository;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.Table;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.Set;
import java.util.TreeSet;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Supplier;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.ASSIGN_FROM_CHASHES_1024;
import static dev.nexus.service.jooq.nexus.Tables.ASSIGN_FROM_CHASHES_384;
import static dev.nexus.service.jooq.nexus.Tables.ASSIGN_FROM_CHASHES_768;
import static dev.nexus.service.jooq.nexus.Tables.CROSS_PREVIEW_1024;
import static dev.nexus.service.jooq.nexus.Tables.CROSS_PREVIEW_384;
import static dev.nexus.service.jooq.nexus.Tables.CROSS_PREVIEW_768;
import static dev.nexus.service.jooq.nexus.Tables.PLAIN_SEARCH_1024;
import static dev.nexus.service.jooq.nexus.Tables.PLAIN_SEARCH_384;
import static dev.nexus.service.jooq.nexus.Tables.PLAIN_SEARCH_768;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_ASPECT_SCOPED_1024;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_ASPECT_SCOPED_384;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_ASPECT_SCOPED_768;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_GRAPH_HOP_1024;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_GRAPH_HOP_384;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_GRAPH_HOP_768;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_METADATA_SCOPED_1024;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_METADATA_SCOPED_384;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_METADATA_SCOPED_768;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TOPIC_SCOPED_1024;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TOPIC_SCOPED_384;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TOPIC_SCOPED_768;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_ANN_QUERY_1024;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_ANN_QUERY_384;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_ANN_QUERY_768;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_BY_CHASH_1024;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_BY_CHASH_384;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_BY_CHASH_768;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_HNSW_FIRST_1024;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_HNSW_FIRST_384;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_HNSW_FIRST_768;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_1024;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_384;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_768;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-225 Phase 2 step 2 (nexus-3wh8d.15): every search family reaches ONE (model, tenant) leaf of
 * {@code nexus.chunks} (and of {@code nexus.taxonomy_centroids} for the taxonomy families), chosen when the
 * statement is PLANNED, through the engine's own call path.
 *
 * <p>The plan is read from the server log. The nexus_svc role runs {@code auto_explain} with nested
 * statements and actual rows ({@code db.changelog-test-auto-explain.xml}), so a call through the repository, with
 * its bound parameters, the serving GUCs and {@code plan_cache_mode = force_custom_plan} in effect, logs the plan
 * of every statement it ran, including the body of a function that is never inlined (a plpgsql function, a
 * SECURITY DEFINER one). EXPLAIN of a literal-parameter copy is not that: Phase 1's F7 ran with literals, and
 * the gate round-2 hint asked for the bound path.
 *
 * <p>Two things are asserted for each call: the set of {@code chunks_m*_t_*} / {@code taxonomy_centroids_m*_t_*}
 * leaves the logged plans name is exactly the expected one (the same tenant's leaf of the other 1024-dimension
 * model, the other tenant's leaves and every leaf of the other dimensions are all absent), and no plan carries
 * {@code Subplans Removed}. The second is what separates planning-time pruning from pruning the executor does at
 * start-up from parameters: a start-up-pruned plan still lists every leaf at plan time and so still pays for them
 * in planning, which is what grows with the tenant count (TS2).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ReadPathLeafPruningIntegrationTest {

    static final String TA = "rp-tenant-a";
    static final String TB = "rp-tenant-b";

    static final String CTX_1 = "knowledge__rpa__voyage-context-3__v1";
    static final String CTX_2 = "docs__rpb__voyage-context-3__v1";
    static final String CODE_1 = "code__rpc__voyage-code-3__v1";
    static final String BGE_1 = "docs__rpd__bge-base-en-v15-768__v1";
    static final String MINI_1 = "knowledge__rpe__minilm-l6-v2-384__v1";

    static final String CTX = PartitionScratch.CONTEXT_3;
    static final String CODE = PartitionScratch.CODE_3;
    static final String BGE = PartitionScratch.BGE_768;
    static final String MINI = PartitionScratch.MINILM_384;

    static final String RARE = "rpraretoken";
    static final String COMMON = "rpcommontoken";
    static final String TOPIC = "rp-topic";
    static final int ROWS = 24;
    static final int RARE_ROWS = 3;

    static final String ADMIN_ROLE = "rp_admin";
    static final String ADMIN_PASS = "rp_admin_pass";

    private static final Pattern LEAF =
        Pattern.compile("\\b((?:chunks|taxonomy_centroids)_m[0-9a-f]{8}_t_[0-9a-f]{16})");

    PostgreSQLContainer<?> pg;
    HikariDataSource seedDs;
    HikariDataSource probeDs;
    TenantScope seedScope;
    TenantScope probeScope;

    PgVectorRepository vec384;
    PgVectorRepository vec768;
    PgVectorRepository vec1024;
    TaxonomyCentroidRepository centroids;
    TaxonomyRepository taxonomy;

    final Map<String, List<String>> chashes = new java.util.HashMap<>();   // collection -> chash hex
    final AtomicInteger sentinel = new AtomicInteger();

    /** Deterministic text -> unit vector, so a query always has a stable neighbourhood. */
    record HashEmbedder(int dim) implements Embedder {
        @Override public List<float[]> embed(List<String> texts) {
            List<float[]> out = new ArrayList<>();
            for (String t : texts) out.add(unit(new Random(t.hashCode()), dim));
            return out;
        }
        @Override public void close() {}
    }

    static float[] unit(Random rnd, int dim) {
        float[] v = new float[dim];
        double n = 0;
        for (int i = 0; i < dim; i++) { v[i] = (float) rnd.nextGaussian(); n += v[i] * v[i]; }
        n = Math.sqrt(n);
        for (int i = 0; i < dim; i++) v[i] /= (float) n;
        return v;
    }

    static String modelOf(String collection) {
        return collection.contains("voyage-context-3") ? CTX
            : collection.contains("voyage-code-3") ? CODE
            : collection.contains("bge-base") ? BGE : MINI;
    }

    static int dimOf(String collection) {
        return collection.contains("voyage") ? 1024 : collection.contains("bge-base") ? 768 : 384;
    }

    static String chunkLeaf(String collection, String tenant) {
        return PartitionScratch.expectedName("chunks", modelOf(collection), tenant);
    }

    static String centroidLeaf(String collection, String tenant) {
        return PartitionScratch.expectedName("taxonomy_centroids", modelOf(collection), tenant);
    }

    @BeforeAll
    void startAll() throws Exception {
        // A DEDICATED container migrated as a non-superuser owner, which is production's shape (the SECURITY
        // DEFINER probe runs as its owner, and a superuser owner would read past row-level security).
        pg = PgContainerHelper.startDedicated();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
        }
        var adminCfg = new HikariConfig();
        adminCfg.setJdbcUrl(pg.getJdbcUrl());
        adminCfg.setUsername(ADMIN_ROLE);
        adminCfg.setPassword(ADMIN_PASS);
        adminCfg.setMaximumPoolSize(2);
        try (var adminDs = new HikariDataSource(adminCfg)) {
            SchemaMigrator.migrate(adminDs);
            try (Connection c = adminDs.getConnection()) {
                PgContainerHelper.installTestObjects(c);
            }
        }
        seedDs = svcPool("rp-seed");
        seedScope = new TenantScope(seedDs);
        seed();
        // After the seed: from here nexus_svc's NEW sessions log every plan, nested ones included.
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.enableAutoExplainForService(su);
        }
        probeDs = svcPool("rp-probe");
        probeScope = new TenantScope(probeDs);
        vec384 = new PgVectorRepository(probeScope, new HashEmbedder(384), new HashEmbedder(384));
        vec768 = new PgVectorRepository(probeScope, new HashEmbedder(768), new HashEmbedder(768));
        vec1024 = new PgVectorRepository(probeScope, new HashEmbedder(1024), new HashEmbedder(1024));
        centroids = new TaxonomyCentroidRepository(probeScope);
        taxonomy = new TaxonomyRepository(probeScope);
    }

    private HikariDataSource svcPool(String name) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(3);
        cfg.setPoolName(name);
        cfg.setAutoCommit(true);
        return new HikariDataSource(cfg);
    }

    @AfterAll
    void stopAll() {
        if (probeDs != null) probeDs.close();
        if (seedDs != null) seedDs.close();
        if (pg != null) pg.stop();
    }

    PgVectorRepository vecRepo(String collection) {
        return switch (dimOf(collection)) {
            case 384 -> vec384;
            case 768 -> vec768;
            default -> vec1024;
        };
    }

    // ── fixture ─────────────────────────────────────────────────────────────

    private void seed() throws Exception {
        List<String> all = List.of(CTX_1, CTX_2, CODE_1, BGE_1, MINI_1);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (String tenant : List.of(TA, TB)) {
                for (String coll : all) {
                    PgContainerHelper.insertCollection(dsl, tenant, coll);
                    seedChunks(dsl, tenant, coll);
                    seedCatalog(dsl, tenant, coll);
                }
            }
        }
        for (String tenant : List.of(TA, TB)) {
            for (String coll : all) {
                new TaxonomyCentroidRepository(seedScope).upsertCentroids(tenant, List.of(
                    new TaxonomyCentroidRepository.CentroidRecord(
                        coll, topicId(tenant, coll), unit(new Random((tenant + coll).hashCode()), dimOf(coll)),
                        TOPIC, ROWS)));
            }
        }
    }

    private final Map<String, Long> topicIds = new java.util.HashMap<>();

    private long topicId(String tenant, String coll) {
        return topicIds.get(tenant + "/" + coll);
    }

    private void seedChunks(DSLContext dsl, String tenant, String coll) {
        int dim = dimOf(coll);
        Random rnd = new Random((tenant + "/" + coll).hashCode());
        List<String> hex = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<float[]> vecs = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < ROWS; i++) {
            String text = COMMON + " " + (i < RARE_ROWS ? RARE + " " : "") + "rp fixture chunk " + i
                + " alpha bravo charlie delta";
            hex.add(dev.nexus.service.db.Chash.ofText(tenant + "/" + coll + "/" + i).toHex());
            texts.add(text);
            vecs.add(unit(rnd, dim));
            metas.add(Map.of());
        }
        PgContainerHelper.insertChunks(dsl, tenant, coll, hex, texts, vecs, metas);
        PgContainerHelper.ownChunks(dsl, tenant, coll, hex.toArray(new String[0]));
        chashes.put(tenant + "/" + coll, hex);
    }

    private void seedCatalog(DSLContext dsl, String tenant, String coll) {
        String doc = "own-" + coll;
        dsl.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.SOURCE_URI, DOCUMENT_ASPECTS.DOC_ID)
            .values(tenant, coll, "rp/" + coll, OffsetDateTime.now(), "v1", "rp", "file:///rp/" + coll, doc)
            .onConflictDoNothing()
            .execute();
        long topic = dsl.insertInto(TOPICS, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.CREATED_AT)
            .values(tenant, TOPIC, coll, OffsetDateTime.parse("2026-01-01T00:00:00+00:00"))
            .returning(TOPICS.ID).fetchOne().getId();
        topicIds.put(tenant + "/" + coll, topic);
        String model = modelOf(coll);
        for (String h : chashes.get(tenant + "/" + coll)) {
            dsl.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                    TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                    TOPIC_ASSIGNMENTS.EMBEDDING_MODEL, TOPIC_ASSIGNMENTS.ASSIGNED_AT)
                .values(tenant, HexFormat.of().parseHex(h), topic, coll, model,
                    OffsetDateTime.parse("2026-01-01T00:00:00+00:00"))
                .onConflictDoNothing()
                .execute();
        }
    }

    // ── observation ─────────────────────────────────────────────────────────

    /** What the plans logged for one call name. */
    record Seen(Set<String> leaves, boolean subplansRemoved, String delta) {}

    /**
     * Run {@code call} on the logging pool and return what the plans it logged name. The server log is written
     * asynchronously, so a sentinel statement from the same pool marks the end: once its own entry is in the log,
     * everything the call ran is.
     */
    <T> Seen observe(Supplier<T> call) throws Exception {
        int from = pg.getLogs().length();
        call.get();
        String mark = "rp-sentinel-" + sentinel.incrementAndGet();
        probeScope.withTenant(TA, ctx -> ctx.fetch(DSL.select(DSL.inline(mark))).size());
        String delta = null;
        for (int attempt = 0; attempt < 200 && delta == null; attempt++) {
            String logs = pg.getLogs();
            int at = logs.indexOf(mark, from);
            if (at >= 0) {
                delta = logs.substring(from, at);
            } else {
                Thread.sleep(100);
            }
        }
        assertThat(delta).as("the log shows the sentinel after the call").isNotNull();
        Set<String> leaves = new TreeSet<>();
        Matcher m = LEAF.matcher(delta);
        while (m.find()) leaves.add(m.group(1));
        return new Seen(leaves, delta.contains("Subplans Removed"), delta);
    }

    void assertLeaves(String what, Seen s, String... expected) {
        assertThat(s.leaves())
            .as("%s: the plans name exactly the expected (model, tenant) leaves. Log was:%n%s", what, s.delta())
            .containsExactlyInAnyOrder(expected);
        assertThat(s.subplansRemoved())
            .as("%s: no plan carries 'Subplans Removed' (pruning at plan time, not at executor start-up). Log was:%n%s",
                what, s.delta())
            .isFalse();
    }

    private String q(String text) {
        return text;
    }

    // ── chunk families ──────────────────────────────────────────────────────

    @Test
    void plainSearch_oneLeaf_everyModel() throws Exception {
        for (String coll : List.of(CTX_1, CODE_1, BGE_1, MINI_1)) {
            Seen s = observe(() -> vecRepo(coll).search(TA, q("rp plain " + coll), List.of(coll), 5, null));
            assertLeaves("plain_search " + coll, s, chunkLeaf(coll, TA));
        }
    }

    @Test
    void plainSearch_twoCollectionsOfOneModel_oneLeaf() throws Exception {
        Seen s = observe(() -> vec1024.search(TA, "rp plain two", List.of(CTX_1, CTX_2), 5, null));
        assertLeaves("plain_search over two collections of one model", s, chunkLeaf(CTX_1, TA));
    }

    @Test
    void plainSearch_otherTenant_itsOwnLeaf() throws Exception {
        Seen s = observe(() -> vec1024.search(TB, "rp plain b", List.of(CTX_1), 5, null));
        assertLeaves("plain_search as tenant B", s, chunkLeaf(CTX_1, TB));
    }

    @Test
    void plainSearch_perCollectionFanOut_oneLeaf() throws Exception {
        Seen s = observe(() -> vec1024.searchPerCollection(TA, "rp fanout", List.of(CTX_1, CTX_2), 5, 10,
            null, null, false));
        assertLeaves("plain_search per-collection fan-out", s, chunkLeaf(CTX_1, TA));
    }

    @Test
    void hybridSearch_selectiveGate_probeAndByChash_oneLeaf() throws Exception {
        for (String coll : List.of(CTX_1, CODE_1, BGE_1, MINI_1)) {
            Seen s = observe(() -> vecRepo(coll).hybridSearch(TA, RARE, List.of(coll), 5, null, 10));
            assertLeaves("text_gate_probe + text_gated_search_by_chash " + coll, s, chunkLeaf(coll, TA));
        }
    }

    @Test
    void hybridSearch_denseGate_probeAndHnswFirst_oneLeaf() throws Exception {
        for (String coll : List.of(CTX_1, CODE_1, BGE_1, MINI_1)) {
            Seen s = observe(() -> vecRepo(coll).hybridSearch(TA, COMMON, List.of(coll), 5, null, 2));
            assertLeaves("text_gate_probe + text_gated_search_hnsw_first " + coll, s, chunkLeaf(coll, TA));
        }
    }

    @Test
    void metadataScoped_oneLeaf() throws Exception {
        for (String coll : List.of(CTX_1, CODE_1, BGE_1, MINI_1)) {
            Seen s = observe(() -> vecRepo(coll).searchMetadataScoped(
                TA, "rp meta", List.of(coll), null, null, null, null, 5));
            assertLeaves("search_metadata_scoped " + coll, s, chunkLeaf(coll, TA));
        }
    }

    @Test
    void aspectScoped_oneLeaf() throws Exception {
        for (String coll : List.of(CTX_1, CODE_1, BGE_1, MINI_1)) {
            Seen s = observe(() -> vecRepo(coll).searchAspectScopedWithTokens(
                TA, "rp aspect", List.of(coll), null, null, null, null, 5));
            assertLeaves("search_aspect_scoped " + coll, s, chunkLeaf(coll, TA));
        }
    }

    @Test
    void graphHop_oneLeaf() throws Exception {
        for (String coll : List.of(CTX_1, CODE_1, BGE_1, MINI_1)) {
            Seen s = observe(() -> vecRepo(coll).searchGraphHop(
                TA, "rp hop", List.of("own-" + coll), List.of(coll), null, 1, "both", 5));
            assertLeaves("search_graph_hop " + coll, s, chunkLeaf(coll, TA));
        }
    }

    @Test
    void topicScoped_oneLeaf() throws Exception {
        for (String coll : List.of(CTX_1, CODE_1, BGE_1, MINI_1)) {
            Seen s = observe(() -> vecRepo(coll).searchTopicScoped(TA, "rp topic", TOPIC, coll, 5));
            assertLeaves("search_topic_scoped " + coll, s, chunkLeaf(coll, TA));
        }
    }

    // ── taxonomy families ───────────────────────────────────────────────────

    @Test
    void taxonomyAnnQuery_ownCollection_oneCentroidLeaf() throws Exception {
        for (String coll : List.of(CTX_1, CODE_1, BGE_1, MINI_1)) {
            float[] v = unit(new Random(7), dimOf(coll));
            Seen s = observe(() -> centroids.annQuery(TA, v, coll, false, 3));
            assertLeaves("taxonomy_ann_query own " + coll, s, centroidLeaf(coll, TA));
        }
    }

    @Test
    void taxonomyAnnQuery_crossCollection_oneCentroidLeaf_ofTheSourceModel() throws Exception {
        float[] v = unit(new Random(7), 1024);
        Seen s = observe(() -> centroids.annQuery(TA, v, CTX_1, true, 3));
        assertLeaves("taxonomy_ann_query cross from " + CTX_1, s, centroidLeaf(CTX_1, TA));
    }

    @Test
    void assignFromChashes_ownPass_oneChunkLeaf_oneCentroidLeaf() throws Exception {
        for (String coll : List.of(CTX_1, CODE_1, BGE_1, MINI_1)) {
            List<String> some = chashes.get(TA + "/" + coll).subList(0, 4);
            Seen s = observe(() -> taxonomy.assignFromChashes(TA, coll, some, false));
            assertLeaves("assign_from_chashes own " + coll, s, chunkLeaf(coll, TA), centroidLeaf(coll, TA));
        }
    }

    @Test
    void assignFromChashes_crossPass_oneChunkLeaf_oneCentroidLeaf() throws Exception {
        List<String> some = chashes.get(TA + "/" + CTX_1).subList(0, 4);
        Seen s = observe(() -> taxonomy.assignFromChashes(TA, CTX_1, some, true));
        assertLeaves("assign_from_chashes cross " + CTX_1, s, chunkLeaf(CTX_1, TA), centroidLeaf(CTX_1, TA));
    }

    @Test
    void crossPreview_oneChunkLeaf_oneCentroidLeaf() throws Exception {
        for (String coll : List.of(CTX_1, CODE_1, BGE_1, MINI_1)) {
            List<String> some = chashes.get(TA + "/" + coll).subList(0, 4);
            Seen s = observe(() -> taxonomy.crossPreview(TA, coll, some));
            assertLeaves("cross_preview " + coll, s, chunkLeaf(coll, TA), centroidLeaf(coll, TA));
        }
    }

    // ── isolation, at the function (acceptance 2) ───────────────────────────

    private static final List<String> FAMILIES = List.of(
        "plain_search", "text_gated_search_hnsw_first", "text_gated_search_by_chash", "search_metadata_scoped",
        "search_aspect_scoped", "search_graph_hop", "search_topic_scoped", "text_gate_probe", "taxonomy_ann_query",
        "assign_from_chashes", "cross_preview");

    /** The generated call of {@code family} at the dimension of {@code coll}, for the model of {@code coll} and the tenant PARAMETER {@code paramTenant}. */
    private Table<?> fnFor(String family, String coll, String paramTenant, boolean cross) {
        int dim = dimOf(coll);
        String model = modelOf(coll);
        Vector q = Vector.of(unit(new Random(1), dim));
        String[] colls = {coll};
        List<String> some = chashes.get(paramTenant + "/" + coll).subList(0, 4);
        String[] hex = some.toArray(String[]::new);
        byte[][] raw = some.stream().map(h -> HexFormat.of().parseHex(h)).toArray(byte[][]::new);
        String[] seeds = {"own-" + coll};
        String t = paramTenant;
        return switch (family) {
            case "plain_search" -> switch (dim) {
                case 384 -> PLAIN_SEARCH_384.call(q, colls, null, null, 10, model, t);
                case 768 -> PLAIN_SEARCH_768.call(q, colls, null, null, 10, model, t);
                default -> PLAIN_SEARCH_1024.call(q, colls, null, null, 10, model, t);
            };
            case "text_gated_search_hnsw_first" -> switch (dim) {
                case 384 -> TEXT_GATED_SEARCH_HNSW_FIRST_384.call(q, COMMON, colls, null, null, 10, model, t);
                case 768 -> TEXT_GATED_SEARCH_HNSW_FIRST_768.call(q, COMMON, colls, null, null, 10, model, t);
                default -> TEXT_GATED_SEARCH_HNSW_FIRST_1024.call(q, COMMON, colls, null, null, 10, model, t);
            };
            case "text_gated_search_by_chash" -> switch (dim) {
                case 384 -> TEXT_GATED_SEARCH_BY_CHASH_384.call(q, raw, colls, null, null, 10, model, t);
                case 768 -> TEXT_GATED_SEARCH_BY_CHASH_768.call(q, raw, colls, null, null, 10, model, t);
                default -> TEXT_GATED_SEARCH_BY_CHASH_1024.call(q, raw, colls, null, null, 10, model, t);
            };
            case "search_metadata_scoped" -> switch (dim) {
                case 384 -> SEARCH_METADATA_SCOPED_384.call(q, colls, null, null, null, null, null, null, 10, model, t);
                case 768 -> SEARCH_METADATA_SCOPED_768.call(q, colls, null, null, null, null, null, null, 10, model, t);
                default -> SEARCH_METADATA_SCOPED_1024.call(q, colls, null, null, null, null, null, null, 10, model, t);
            };
            case "search_aspect_scoped" -> switch (dim) {
                case 384 -> SEARCH_ASPECT_SCOPED_384.call(q, colls, null, null, null, null, 10, model, t);
                case 768 -> SEARCH_ASPECT_SCOPED_768.call(q, colls, null, null, null, null, 10, model, t);
                default -> SEARCH_ASPECT_SCOPED_1024.call(q, colls, null, null, null, null, 10, model, t);
            };
            case "search_graph_hop" -> switch (dim) {
                case 384 -> SEARCH_GRAPH_HOP_384.call(q, seeds, colls, null, 1, "both", null, 10, model, t);
                case 768 -> SEARCH_GRAPH_HOP_768.call(q, seeds, colls, null, 1, "both", null, 10, model, t);
                default -> SEARCH_GRAPH_HOP_1024.call(q, seeds, colls, null, 1, "both", null, 10, model, t);
            };
            case "search_topic_scoped" -> switch (dim) {
                case 384 -> SEARCH_TOPIC_SCOPED_384.call(q, TOPIC, coll, 10, model, t);
                case 768 -> SEARCH_TOPIC_SCOPED_768.call(q, TOPIC, coll, 10, model, t);
                default -> SEARCH_TOPIC_SCOPED_1024.call(q, TOPIC, coll, 10, model, t);
            };
            case "text_gate_probe" -> switch (dim) {
                case 384 -> TEXT_GATE_PROBE_384.call(COMMON, colls, null, null, 100, model, t);
                case 768 -> TEXT_GATE_PROBE_768.call(COMMON, colls, null, null, 100, model, t);
                default -> TEXT_GATE_PROBE_1024.call(COMMON, colls, null, null, 100, model, t);
            };
            case "taxonomy_ann_query" -> switch (dim) {
                case 384 -> TAXONOMY_ANN_QUERY_384.call(q, coll, cross, 10, model, t);
                case 768 -> TAXONOMY_ANN_QUERY_768.call(q, coll, cross, 10, model, t);
                default -> TAXONOMY_ANN_QUERY_1024.call(q, coll, cross, 10, model, t);
            };
            case "assign_from_chashes" -> switch (dim) {
                case 384 -> ASSIGN_FROM_CHASHES_384.call(coll, hex, cross, model, t);
                case 768 -> ASSIGN_FROM_CHASHES_768.call(coll, hex, cross, model, t);
                default -> ASSIGN_FROM_CHASHES_1024.call(coll, hex, cross, model, t);
            };
            case "cross_preview" -> switch (dim) {
                case 384 -> CROSS_PREVIEW_384.call(coll, hex, model, t);
                case 768 -> CROSS_PREVIEW_768.call(coll, hex, model, t);
                default -> CROSS_PREVIEW_1024.call(coll, hex, model, t);
            };
            default -> throw new IllegalArgumentException(family);
        };
    }

    /** The identity of each returned row: the chash column when the function has one, else the first column. */
    private static List<String> keys(org.jooq.Result<?> rows) {
        List<String> out = new ArrayList<>();
        for (var r : rows) {
            Object v = r.field("chash") != null ? r.get("chash") : r.get(0);
            if (v == null && r.field("id") != null) v = r.get("id");
            out.add(v instanceof byte[] b ? HexFormat.of().formatHex(b) : String.valueOf(v));
        }
        return out;
    }

    private List<String> rowsAs(String sessionTenant, Table<?> fn) {
        return probeScope.withTenant(sessionTenant, ctx -> keys(ctx.selectFrom(fn).fetch()));
    }

    private List<String> rowsWithNoTenant(Table<?> fn) throws Exception {
        try (Connection c = probeDs.getConnection()) {
            return keys(DSL.using(c, SQLDialect.POSTGRES).selectFrom(fn).fetch());
        }
    }

    /**
     * Acceptance 2. Every family, at three dimensions: under tenant A's GUC the call returns rows (so the
     * empty results are not vacuous); under tenant B's GUC, with tenant A's id as the parameter, and with no
     * GUC at all, it returns nothing; and under tenant B's GUC with tenant B's id it returns B's own rows and
     * not one of A's. The parameter narrows what row-level security allows and never widens it.
     */
    @Test
    void everyFamily_returnsNoRowsWithoutTheTenantGuc_andNoRowsOfAnotherTenant() throws Exception {
        int checked = 0;
        for (String coll : List.of(CTX_1, BGE_1, MINI_1)) {
            for (String family : FAMILIES) {
                // a cross preview needs a centroid of ANOTHER collection of the same model: only the
                // voyage-context-3 collections have one (crossCollectionFamilies_... covers it)
                if (family.equals("cross_preview") && !coll.equals(CTX_1)) continue;
                List<String> asA = rowsAs(TA, fnFor(family, coll, TA, false));
                assertThat(asA).as("%s %s, tenant A's session and parameter: rows (non-vacuity)", family, coll)
                    .isNotEmpty();
                assertThat(rowsAs(TB, fnFor(family, coll, TA, false)))
                    .as("%s %s, tenant B's session with tenant A's id as the parameter", family, coll).isEmpty();
                assertThat(rowsWithNoTenant(fnFor(family, coll, TA, false)))
                    .as("%s %s, no tenant GUC", family, coll).isEmpty();
                List<String> asB = rowsAs(TB, fnFor(family, coll, TB, false));
                assertThat(asB).as("%s %s, tenant B's own session and parameter: rows (non-vacuity)", family, coll)
                    .isNotEmpty();
                if (!family.equals("search_metadata_scoped") && !family.equals("search_aspect_scoped")
                        && !family.equals("search_graph_hop")) {
                    // those three return the document tumbler as their id, the same text in both tenants;
                    // every other family's rows are named by chunk hash, which differs by tenant
                    assertThat(asB).as("%s %s: none of tenant A's rows in tenant B's", family, coll)
                        .doesNotContainAnyElementsOf(asA);
                }
                checked += 4;
            }
        }
        assertThat(checked).as("non-vacuity: every family at every dimension was run").isEqualTo((3 * FAMILIES.size() - 2) * 4);
    }

    @Test
    void crossCollectionFamilies_returnNothingForAnotherTenantsSession() throws Exception {
        for (String family : List.of("taxonomy_ann_query", "assign_from_chashes")) {
            assertThat(rowsAs(TA, fnFor(family, CTX_1, TA, true)))
                .as("%s cross, tenant A: rows (non-vacuity)", family).isNotEmpty();
            assertThat(rowsAs(TB, fnFor(family, CTX_1, TA, true)))
                .as("%s cross, tenant B's session with tenant A's id", family).isEmpty();
        }
        assertThat(rowsAs(TA, fnFor("cross_preview", CTX_1, TA, true))).as("cross_preview: rows").isNotEmpty();
        assertThat(rowsAs(TB, fnFor("cross_preview", CTX_1, TA, true))).as("cross_preview, tenant B, A's id").isEmpty();
    }

    // ── one call, one model ─────────────────────────────────────────────────

    @Test
    void aCallNamingTwoModels_isRefused_forPlainAndHybridSearch() {
        assertThatThrownBy(() -> vec1024.search(TA, "rp mixed", List.of(CTX_1, CODE_1), 5, null))
            .as("plain search over two 1024-dimension collections of different models")
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("mixed embedding models");
        assertThatThrownBy(() -> vec1024.hybridSearch(TA, COMMON, List.of(CTX_1, CODE_1), 5, null, 10))
            .as("hybrid search over the same two")
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("mixed embedding models");
    }

    // ── TS2: planning does not grow with the tenant count ───────────────────

    /**
     * Plans per set. The rule is on a p95, and the p95 of 20 samples is the second-largest of 20, which moves by 2x
     * from one run to the next on a shared box for a statement that plans in about a millisecond. The p95 of 200 is
     * the tenth-largest, and does not.
     */
    private static final int TS2_SAMPLES = 200;
    /** Plans run and discarded at the start of every set (plan cache, JIT, server caches). */
    private static final int TS2_WARMUP = 20;
    private static final int TS2_TENANTS = 300;
    /**
     * Each figure, at 2 tenants and at 300, is the best (lowest) p95 of this many sets, so both phases are read
     * with the same statistic. A box that is also running a build or another suite inflates single sets by 5x and
     * more in either phase; a planning cost that GROWS with the tenant count shows in every set, so the minimum
     * keeps the signal and drops the load. The bound itself is the RDR's, unchanged: p95 at 300 tenants at most
     * 2x the 2-tenant p95 and at most 25 ms, no floor.
     */
    private static final int TS2_ATTEMPTS = 5;

    /** Families whose body is not inlined into the caller's statement: the call is timed, with its inner planning. */
    private static final Set<String> TS2_OPAQUE = Set.of("text_gate_probe", "assign_from_chashes", "cross_preview");

    /** The pool TS2 times on: its sessions were opened with auto_explain off (see the test). */
    private TenantScope ts2Scope;

    /** Best p95 of {@link #TS2_ATTEMPTS} sets of {@link #TS2_SAMPLES} timings of one family at one dimension, in ms. */
    private double ts2P95(String family, String coll) {
        double best = Double.MAX_VALUE;
        for (int attempt = 0; attempt < TS2_ATTEMPTS; attempt++) {
            best = Math.min(best, ts2P95Once(family, coll));
        }
        return best;
    }

    private double ts2P95Once(String family, String coll) {
        long[] ns = new long[TS2_SAMPLES];
        // One transaction, the serving settings set once, only the statement timed: the transaction's own round
        // trips (tenant stamp, GUCs, commit) are the same at any tenant count and would bury a planning cost of a
        // fraction of a millisecond.
        ts2Scope.withTenant(TA, ctx -> {
            PgSession.setSearchPlanCacheMode(ctx);
            for (int i = -TS2_WARMUP; i < TS2_SAMPLES; i++) {      // warm-up runs, not recorded
                Table<?> fn = fnFor(family, coll, TA, false);
                long t0 = System.nanoTime();
                if (TS2_OPAQUE.contains(family)) {
                    ctx.selectFrom(fn).fetch();
                } else {
                    ctx.explain(ctx.selectFrom(fn));
                }
                long dt = System.nanoTime() - t0;
                if (i >= 0) ns[i] = dt;
            }
            return null;
        });
        java.util.Arrays.sort(ns);
        return ns[(int) Math.ceil(0.95 * TS2_SAMPLES) - 1] / 1e6;
    }

    /**
     * TS2, as the RDR writes it: every search family planned 200 times per set at 2 tenants and at 300, p95 at 300 at most
     * twice the 2-tenant p95 and at most 25 ms. A family is timed as EXPLAIN of its call (parse and plan, no
     * execution) when it is inlined into the caller's statement, and as the call itself when it is not (a
     * plpgsql function plans its own statements at call time: the probe, assign_from_chashes, cross_preview;
     * their inputs are four rows, so the call is dominated by planning). The numbers are printed for the record.
     */
    @Test
    void ts2_planningAt300Tenants_isWithinTwiceTheTwoTenantP95_andUnder25ms() throws Exception {
        // TS2 times statements, so it runs on sessions that do NOT log every plan. auto_explain is per role and
        // session_preload_libraries is read when a session starts, so a pool opened after the RESET below has no
        // auto_explain. This is not only a timing nicety: TS2 executes the opaque families about ten thousand
        // times, auto_explain wrote a nested plan for each into the server log, and observe() reads that whole log
        // back (twice per call), so every leaf-pruning test that ran after TS2 paid for the bloat (20 minutes for
        // the class, against under 10 seconds for the same tests run before TS2).
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.runSuperuserDdl(su, "ALTER ROLE " + PgContainerHelper.SVC_USERNAME
                + " RESET session_preload_libraries");
        }
        try (HikariDataSource ts2Ds = svcPool("rp-ts2")) {
            ts2Scope = new TenantScope(ts2Ds);
            ts2Body();
        } finally {
            // the pooled sessions of the other tests keep what they were opened with; this is for any new one
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.enableAutoExplainForService(su);
            }
        }
    }

    private void ts2Body() throws Exception {
        long started = System.nanoTime();
        // one unrecorded pass over every family first, so the first family measured does not carry the cold
        // start of the server's caches and the JIT of this JVM into the 2-tenant baseline
        for (String coll : List.of(CTX_1, BGE_1, MINI_1)) {
            for (String family : FAMILIES) {
                if (family.equals("cross_preview") && !coll.equals(CTX_1)) continue;
                ts2P95(family, coll);
            }
        }
        long warmDone = System.nanoTime();
        Map<String, Double> at2 = new java.util.LinkedHashMap<>();
        for (String coll : List.of(CTX_1, BGE_1, MINI_1)) {
            for (String family : FAMILIES) {
                if (family.equals("cross_preview") && !coll.equals(CTX_1)) continue;
                at2.put(family + " " + dimOf(coll), ts2P95(family, coll));
            }
        }
        long at2Done = System.nanoTime();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (int i = 0; i < TS2_TENANTS - 2; i++) {
                PgContainerHelper.ensureTenantPartitions(dsl, "ts2-tenant-" + i);
            }
        }
        long tenantsDone = System.nanoTime();
        List<String> failures = new ArrayList<>();
        StringBuilder table = new StringBuilder("TS2 planning p95 (ms), best of " + TS2_ATTEMPTS + " sets of "
            + TS2_SAMPLES + " plans\n");
        for (String coll : List.of(CTX_1, BGE_1, MINI_1)) {
            for (String family : FAMILIES) {
                if (family.equals("cross_preview") && !coll.equals(CTX_1)) continue;
                String key = family + " " + dimOf(coll);
                double p2 = at2.get(key);
                double p300 = ts2P95(family, coll);
                table.append(String.format("  %-34s  2 tenants %7.3f   300 tenants %7.3f%n", key, p2, p300));
                if (p300 > 2 * p2 || p300 > 25.0) failures.add(String.format("%s: p95 %.3f ms at 300 vs %.3f ms at 2", key, p300, p2));
            }
        }
        long end = System.nanoTime();
        table.append(String.format("  wall: warm pass %.1f s, 2-tenant pass %.1f s, 298 tenants created %.1f s, 300-tenant pass %.1f s%n",
            (warmDone - started) / 1e9, (at2Done - warmDone) / 1e9, (tenantsDone - at2Done) / 1e9, (end - tenantsDone) / 1e9));
        System.out.println(table);
        assertThat(failures).as("TS2 (p95 at 300 tenants <= 2x the 2-tenant p95 and <= 25 ms):%n%s", table).isEmpty();
    }

    /**
     * A plpgsql function caches the plan of each of its statements per session and, from the sixth call, may
     * switch to a generic plan, which lists every leaf. assign_from_chashes, cross_preview and the probe carry a
     * function-level plan_cache_mode = force_custom_plan so that cannot happen; this makes ten calls in one session
     * and expects the last to have been planned to one leaf as the first. A control run that removed the clause
     * from assign_from_chashes_1024 and cross_preview_1024 did NOT fail this test: the planner's own cost
     * comparison keeps the custom plan, because the generic one, which holds every leaf, costs more. So this test
     * pins the outcome, and the clause (pinned by TaxonomyAssignCrossLateralHnswTest's proconfig assertion) is the
     * guarantee for the case where the two costs tie, as they do on a tiny table.
     */
    @Test
    void plpgsqlFunctions_keepPlanTimePruning_pastTheFifthCallOfASession() throws Exception {
        for (String family : List.of("assign_from_chashes", "cross_preview", "text_gate_probe")) {
            Seen s = observe(() -> probeScope.withTenant(TA, ctx -> {
                for (int i = 0; i < 10; i++) ctx.selectFrom(fnFor(family, CTX_1, TA, false)).fetch();
                return null;
            }));
            assertThat(s.subplansRemoved())
                .as("%s: eight calls in one session, none planned with every leaf. Log was:%n%s", family, s.delta())
                .isFalse();
            assertThat(s.leaves()).as("%s: the leaves the eight plans name", family)
                .isSubsetOf(chunkLeaf(CTX_1, TA), centroidLeaf(CTX_1, TA));
        }
    }

    @Test
    void taxonomyAnnQuery_unregisteredCollection_findsNothing_ratherThanFailing() {
        float[] v = unit(new Random(7), 1024);
        String unregistered = "knowledge__rp-not-registered__voyage-context-3__v1";
        assertThat(centroids.annQuery(TA, v, unregistered, false, 3)).as("own-collection query").isEmpty();
        assertThat(centroids.annQuery(TA, v, unregistered, true, 3))
            .as("cross-collection query from a source with no registered model").isEmpty();
    }
}
