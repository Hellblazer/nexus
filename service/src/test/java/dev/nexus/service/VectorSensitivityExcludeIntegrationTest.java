// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.QueryPart;
import org.jooq.Record;
import org.jooq.Result;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.MethodSource;
import org.junit.jupiter.params.provider.ValueSource;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Stream;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_LINKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-rfvyy.7.3 (RDR-243 Phase 2 Step 2.1a): {@code sensitivity=exclude} on every vector search function family,
 * and {@code sensitive} on every hit.
 *
 * <p>Every family takes a trailing {@code p_exclude_sensitive}; when it is true the statement carries a NOT EXISTS
 * over the chunk's live sensitive owners INSIDE the search predicate, so the HNSW iterative scan fills k from
 * rows that are not sensitive. The test pins, per family and per dimension (the functions exist once per
 * dimension, so a dimension that was missed would be a hole nothing else notices):
 * <ul>
 *   <li>no chunk with a live sensitive owner is returned, and the page is still k rows when enough
 *       non-sensitive rows exist (a post-filter would return fewer);</li>
 *   <li>a SHARED chunk (one sensitive live owner, one non-sensitive owner) is excluded while the sensitive
 *       owner is live and returned once that owner is tombstoned;</li>
 *   <li>the baseline (parameter false) does return the sensitive rows, so the exclusion is not a fixture
 *       that never held any;</li>
 *   <li>one function per name remains, each with the new trailing argument, grants preserved.</li>
 * </ul>
 * The route half drives the real HTTP handler: the eight vector search routes accept {@code "sensitivity":
 * "exclude"}, refuse any other value with 400, and carry {@code sensitive} on every hit whether or not the
 * request asked for the exclusion. The last test forces the HNSW walk over a crowd of sensitive near rows.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorSensitivityExcludeIntegrationTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String TOKEN = "tok-rfvyy73-sensitivity-exclude-0123456789ab";
    private static final String TENANT = "rfvyy73-sensitivity";
    private static final String GATE = "zebracorn";
    private static final String QUERY = "rfvyy73 query";
    private static final String TOPIC = "Sensitivity Topic";
    private static final int[] DIMS = {384, 768, 1024};
    private static final int K = 5;
    private static final OffsetDateTime SEED_TS = OffsetDateTime.parse("2026-01-01T00:00:00+00:00");

    /** The chunk keys, nearest the query first: s1, x, s2 are the near ones; n1..n6 follow. */
    private static final String S1 = "s1", S2 = "s2", X = "x";
    private static final List<String> NON_SENSITIVE = List.of("n1", "n2", "n3", "n4", "n5", "n6");
    private static final Map<String, Double> ANGLE = new LinkedHashMap<>();
    static {
        ANGLE.put(S1, 0.0);
        ANGLE.put(X, 0.02);
        ANGLE.put(S2, 0.05);
        for (int i = 0; i < NON_SENSITIVE.size(); i++) ANGLE.put(NON_SENSITIVE.get(i), 0.2 + 0.1 * i);
    }
    /** document tumbler stem to the chunk key it owns; sx is sensitive, nx is not, and both own the shared chunk. */
    private static final Map<String, String> OWNS = new LinkedHashMap<>();
    static {
        for (String n : NON_SENSITIVE) OWNS.put(n, n);
        OWNS.put("s1", S1);
        OWNS.put("s2", S2);
        OWNS.put("sx", X);
        OWNS.put("nx", X);
    }
    private static final Set<String> SENSITIVE_DOCS = Set.of("s1", "s2", "sx");

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    TenantScope scope;
    PgVectorRepository repo;
    NexusService service;
    HttpClient http;
    PgVectorRepositoryContractTest.FakeEmbedder embedder;

    static String coll(int dim) {
        return switch (dim) {
            case 384 -> "knowledge__rfvyy73-384__minilm-l6-v2-384__v1";
            case 768 -> "knowledge__rfvyy73-768__bge-base-en-v15-768__v1";
            default -> "knowledge__rfvyy73-1024__voyage-context-3__v1";
        };
    }

    static String text(String key) {
        return "chunk " + key + " " + GATE;
    }

    static String hex(String key) {
        return Chash.ofText(text(key)).toHex();
    }

    static String tumbler(String stem, int dim) {
        return stem + "-" + dim;
    }

    private static List<String> hexes(String... keys) {
        List<String> out = new ArrayList<>();
        for (String k : keys) out.add(hex(k));
        return out;
    }

    private static final List<String> SENSITIVE_HEX = hexes(S1, S2, X);
    /** dimension to the embedding model its collection is registered under (set by the seed). */
    private static final Map<Integer, String> MODELS = new java.util.concurrent.ConcurrentHashMap<>();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(dsl, TOKEN, TENANT, "rfvyy73-sensitivity");
            for (int dim : DIMS) seedDim(dsl, dim);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(8);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
        scope = new TenantScope(ds);
        embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        embedder.register(QUERY, 1f, 0f);
        embedder.register(GATE, 1f, 0f);
        repo = new PgVectorRepository(scope, embedder, embedder);
        service = new NexusService(0, TOKEN, ds, null, repo);
        service.start();
        http = TestHttp.client();
    }

    @AfterAll
    void stopAll() {
        PgSession.resetScanBudgetForTests();
        PgSession.resetEfSearchFloorForTests();
        PgSession.resetSearchExactMaxRowsForTests();
        if (service != null) service.stop();
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    // ── fixture ───────────────────────────────────────────────────────────────

    private void seedDim(DSLContext dsl, int dim) {
        String collection = coll(dim);
        PgContainerHelper.insertCollection(dsl, TENANT, collection);
        String model = PgContainerHelper.collectionModel(dsl, TENANT, collection);
        MODELS.put(dim, model);
        long topic = dsl.insertInto(TOPICS)
            .set(TOPICS.TENANT_ID, TENANT).set(TOPICS.LABEL, TOPIC).set(TOPICS.COLLECTION, collection)
            .set(TOPICS.CREATED_AT, SEED_TS)
            .returningResult(TOPICS.ID).fetchOne(TOPICS.ID);
        for (var e : ANGLE.entrySet()) {
            byte[] chash = HexFormat.of().parseHex(hex(e.getKey()));
            double theta = e.getValue();
            dsl.insertInto(CHUNKS)
                .set(CHUNKS.TENANT_ID, TENANT).set(CHUNKS.COLLECTION, collection).set(CHUNKS.CHASH, chash)
                .set(CHUNKS.EMBEDDING_MODEL, model).set(CHUNKS.CHUNK_TEXT, text(e.getKey()))
                .set(VectorSeeds.chunkEmbedding(dim),
                     VectorSeeds.vecXY(dim, (float) Math.cos(theta), (float) Math.sin(theta)))
                .onConflict(CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL).doNothing()
                .execute();
            dsl.insertInto(TOPIC_ASSIGNMENTS)
                .set(TOPIC_ASSIGNMENTS.TENANT_ID, TENANT).set(TOPIC_ASSIGNMENTS.DOC_ID, chash)
                .set(TOPIC_ASSIGNMENTS.TOPIC_ID, topic).set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, collection)
                .set(TOPIC_ASSIGNMENTS.ASSIGNED_AT, SEED_TS).set(TOPIC_ASSIGNMENTS.EMBEDDING_MODEL, model)
                .execute();
        }
        for (var e : OWNS.entrySet()) {
            String doc = tumbler(e.getKey(), dim);
            dsl.insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT).set(CATALOG_DOCUMENTS.TUMBLER, doc)
                .set(CATALOG_DOCUMENTS.TITLE, "Doc " + doc).set(CATALOG_DOCUMENTS.CONTENT_TYPE, "paper")
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection)
                .set(CATALOG_DOCUMENTS.SENSITIVE, SENSITIVE_DOCS.contains(e.getKey()))
                .execute();
            dsl.insertInto(CATALOG_DOCUMENT_CHUNKS)
                .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, TENANT).set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, doc)
                .set(CATALOG_DOCUMENT_CHUNKS.POSITION, 0)
                .set(CATALOG_DOCUMENT_CHUNKS.CHASH, HexFormat.of().parseHex(hex(e.getValue())))
                .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, collection)
                .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, model)
                .execute();
            dsl.insertInto(DOCUMENT_ASPECTS)
                .set(DOCUMENT_ASPECTS.TENANT_ID, TENANT).set(DOCUMENT_ASPECTS.COLLECTION, collection)
                .set(DOCUMENT_ASPECTS.SOURCE_PATH, doc).set(DOCUMENT_ASPECTS.PROPOSED_METHOD, "gradient descent")
                .set(DOCUMENT_ASPECTS.CONFIDENCE, 0.9).set(DOCUMENT_ASPECTS.EXTRACTED_AT, SEED_TS)
                .set(DOCUMENT_ASPECTS.MODEL_VERSION, "rfvyy73").set(DOCUMENT_ASPECTS.EXTRACTOR_NAME, "rfvyy73")
                .set(DOCUMENT_ASPECTS.DOC_ID, doc).set(DOCUMENT_ASPECTS.SOURCE_URI, "rfvyy73://" + doc)
                .execute();
        }
        // the graph-hop seed reaches every document by a cites edge
        String seed = tumbler("seed", dim);
        dsl.insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT).set(CATALOG_DOCUMENTS.TUMBLER, seed)
            .set(CATALOG_DOCUMENTS.TITLE, "Seed " + dim).set(CATALOG_DOCUMENTS.CONTENT_TYPE, "paper")
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection)
            .execute();
        for (String stem : OWNS.keySet()) {
            dsl.insertInto(CATALOG_LINKS, CATALOG_LINKS.TENANT_ID, CATALOG_LINKS.FROM_TUMBLER,
                    CATALOG_LINKS.TO_TUMBLER, CATALOG_LINKS.LINK_TYPE, CATALOG_LINKS.CREATED_BY)
                .values(TENANT, seed, tumbler(stem, dim), "cites", "test")
                .execute();
        }
    }

    /** Tombstone or revive a document, as the superuser, so the shared-chunk case can be run both ways. */
    private void setTombstone(String doc, boolean tombstoned) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.DELETED_AT, tombstoned ? SEED_TS : null)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENTS.TUMBLER.eq(doc))
                .execute();
        }
    }

    // ── function-level: every family, every dimension ─────────────────────────

    record Family(String fn, boolean perDim, boolean ordered) {
        @Override public String toString() { return fn; }
    }

    static final List<Family> FAMILIES = List.of(
        new Family("plain_search", true, true),
        new Family("hybrid_search", true, false),
        new Family("manifest_search", true, true),
        new Family("text_gated_search_allowed", true, true),
        new Family("text_gated_search_by_chash", true, true),
        new Family("text_gated_search_hnsw_first", true, true),
        new Family("search_metadata_scoped", true, true),
        new Family("search_aspect_scoped", true, true),
        new Family("search_graph_hop", true, true),
        new Family("search_topic_scoped", true, true));

    static Stream<org.junit.jupiter.params.provider.Arguments> familyByDim() {
        List<org.junit.jupiter.params.provider.Arguments> out = new ArrayList<>();
        for (Family f : FAMILIES) {
            for (int dim : DIMS) out.add(org.junit.jupiter.params.provider.Arguments.of(f, dim));
        }
        return out.stream();
    }

    private static Field<?> vec(int dim) {
        return DSL.val(VectorSeeds.vecXY(dim, 1f, 0f), VectorSeeds.chunkEmbedding(dim));
    }

    private static Field<?> textArray(String... values) {
        return DSL.val(values, SQLDataType.VARCHAR.array());
    }

    private static Field<?> byteaArray(List<String> hexes) {
        byte[][] bytes = hexes.stream().map(h -> HexFormat.of().parseHex(h)).toArray(byte[][]::new);
        return DSL.val(bytes, SQLDataType.VARBINARY.array());
    }

    private static Field<?> nullText() { return DSL.val(null, SQLDataType.VARCHAR); }
    private static Field<?> nullJsonb() { return DSL.val(null, SQLDataType.JSONB); }
    private static Field<?> nullInt() { return DSL.val(null, SQLDataType.INTEGER); }
    private static Field<?> nullDouble() { return DSL.val(null, SQLDataType.DOUBLE); }
    private static Field<?> nullBytea() { return DSL.val(null, SQLDataType.VARBINARY.array()); }

    private static Table<?> fnTable(String fn, int dim, boolean exclude, Field<?>... args) {
        List<Field<?>> all = new ArrayList<>(List.of(args));
        all.add(DSL.val(exclude));
        StringBuilder sql = new StringBuilder("nexus.").append(fn);
        if (dim > 0) sql.append('_').append(dim);
        sql.append('(');
        for (int i = 0; i < all.size(); i++) sql.append(i == 0 ? "{0}" : ", {" + i + "}");
        sql.append(')');
        return DSL.table(sql.toString(), all.toArray(new QueryPart[0]));
    }

    private Result<Record> call(String fn, int dim, boolean exclude, Field<?>... args) {
        Table<?> t = fnTable(fn, dim, exclude, args);
        return scope.withTenant(TENANT, ctx -> ctx.select().from(t).fetch());
    }

    /** The chunk hashes (hex) the family returns for the fixture's query, nearest first. */
    private List<String> run(Family family, int dim, boolean exclude, int n) {
        String c = coll(dim);
        Field<?> m = DSL.val(MODELS.get(dim));
        Field<?> t = DSL.val(TENANT);
        Field<?> colls = textArray(c);
        List<String> all = hexes(S1, X, S2, "n1", "n2", "n3", "n4", "n5", "n6");
        Result<Record> r;
        String col = "id";
        switch (family.fn()) {
            case "plain_search" ->
                r = call("plain_search", dim, exclude, vec(dim), colls, nullJsonb(), nullText(), DSL.val(n), m, t);
            case "hybrid_search" ->
                r = call("hybrid_search", dim, exclude, vec(dim), DSL.val(GATE), colls, nullJsonb(), DSL.val(n));
            case "manifest_search" ->
                r = call("manifest_search", dim, exclude, vec(dim), nullText(), nullBytea(), colls, nullJsonb(),
                         nullText(), nullJsonb(), nullText(), DSL.val(n), m, t);
            case "text_gated_search_allowed" ->
                r = call("text_gated_search_allowed", dim, exclude, vec(dim), DSL.val(GATE), byteaArray(all), colls,
                         nullJsonb(), nullText(), DSL.val(n), m, t);
            case "text_gated_search_by_chash" ->
                r = call("text_gated_search_by_chash", dim, exclude, vec(dim), byteaArray(all), colls, nullJsonb(),
                         nullText(), DSL.val(n), m, t);
            case "text_gated_search_hnsw_first" ->
                r = call("text_gated_search_hnsw_first", dim, exclude, vec(dim), DSL.val(GATE), colls, nullJsonb(),
                         nullText(), DSL.val(n), m, t);
            case "search_metadata_scoped" -> {
                col = "chash";
                r = call("search_metadata_scoped", dim, exclude, vec(dim), colls, nullText(), nullText(), nullInt(),
                         nullText(), nullText(), nullJsonb(), nullJsonb(), nullText(), DSL.val(n), m, t);
            }
            case "search_aspect_scoped" -> {
                col = "chash";
                r = call("search_aspect_scoped", dim, exclude, vec(dim), colls, nullText(), nullText(), nullDouble(),
                         nullJsonb(), nullJsonb(), nullText(), DSL.val(n), m, t);
            }
            case "search_graph_hop" -> {
                col = "chash";
                r = call("search_graph_hop", dim, exclude, vec(dim), textArray(tumbler("seed", dim)), colls,
                         DSL.val("cites"), DSL.val(1), DSL.val("out"), nullJsonb(), nullJsonb(), nullText(),
                         DSL.val(n), m, t);
            }
            case "search_topic_scoped" ->
                r = call("search_topic_scoped", dim, exclude, vec(dim), DSL.val(TOPIC), DSL.val(c), DSL.val(n), m, t);
            default -> throw new IllegalArgumentException(family.fn());
        }
        List<String> out = new ArrayList<>();
        for (Record rec : r) out.add(rec.get(col, String.class));
        return out;
    }

    @ParameterizedTest(name = "{0} at {1}")
    @MethodSource("familyByDim")
    void exclude_dropsSensitiveChunks_andStillFillsK(Family family, int dim) throws Exception {
        // the fixture holds sensitive rows nearer than any other: the baseline must return them
        List<String> baseline = run(family, dim, false, 10);
        assertThat(baseline).as("baseline (parameter false) returns every sensitive chunk")
            .containsAll(SENSITIVE_HEX);

        List<String> excluded = run(family, dim, true, K);
        assertThat(excluded).as("no sensitive chunk, and k rows when non-sensitive rows exist").hasSize(K);
        assertThat(excluded).doesNotContainAnyElementsOf(SENSITIVE_HEX);
        if (family.ordered()) {
            assertThat(excluded).as("the next-nearest non-sensitive rows, in distance order")
                .containsExactlyElementsOf(hexes("n1", "n2", "n3", "n4", "n5"));
        } else {
            assertThat(excluded).isSubsetOf(hexes("n1", "n2", "n3", "n4", "n5", "n6"));
        }
    }

    @ParameterizedTest(name = "{0} at {1}")
    @MethodSource("familyByDim")
    void sharedChunk_isExcludedWhileItsSensitiveOwnerIsLive_andReturnedAfterTombstone(Family family, int dim)
            throws Exception {
        List<String> nonSensitive = hexes("n1", "n2", "n3", "n4", "n5", "n6");
        List<String> live = run(family, dim, true, 10);
        assertThat(live).as("shared chunk x has a live sensitive owner (sx): excluded although nx is not sensitive")
            .doesNotContain(hex(X));
        assertThat(live).containsExactlyInAnyOrderElementsOf(nonSensitive);

        String sx = tumbler("sx", dim);
        setTombstone(sx, true);
        try {
            List<String> after = run(family, dim, true, 10);
            assertThat(after).as("sx tombstoned: x has only the non-sensitive owner nx left").contains(hex(X));
            assertThat(new LinkedHashSet<>(after)).as("x once, plus the six non-sensitive chunks")
                .containsExactlyInAnyOrderElementsOf(
                    new LinkedHashSet<>(hexes("x", "n1", "n2", "n3", "n4", "n5", "n6")));
            assertThat(after).doesNotContain(hex(S1), hex(S2));
        } finally {
            setTombstone(sx, false);
        }
    }

    @ParameterizedTest(name = "manifest_qualifying_hits at {0}")
    @ValueSource(ints = {384, 768, 1024})
    void manifestQualifyingHits_excludesSensitiveChunks_andTracksTheTombstone(int dim) throws Exception {
        Set<String> live = qualifying(dim, true);
        assertThat(live).containsExactlyInAnyOrderElementsOf(hexes("n1", "n2", "n3", "n4", "n5", "n6"));
        assertThat(qualifying(dim, false)).as("baseline holds the sensitive chunks")
            .containsAll(SENSITIVE_HEX);
        String sx = tumbler("sx", dim);
        setTombstone(sx, true);
        try {
            assertThat(qualifying(dim, true)).contains(hex(X)).doesNotContain(hex(S1), hex(S2));
        } finally {
            setTombstone(sx, false);
        }
    }

    private Set<String> qualifying(int dim, boolean exclude) {
        String c = coll(dim);
        Result<Record> r = call("manifest_qualifying_hits", 0, exclude, textArray(c), nullJsonb(), nullText(),
                                nullJsonb(), nullText(), DSL.val(100), DSL.val(MODELS.get(dim)), DSL.val(TENANT),
                                nullText());
        Set<String> out = new LinkedHashSet<>();
        for (Record rec : r) out.add(HexFormat.of().formatHex(rec.get("chunk_chash", byte[].class)));
        return out;
    }

    @Test
    void everyFamilyHasOneSignature_withTheTrailingArgument_andTheGrantsItHad() throws Exception {
        var proc = DSL.table(DSL.name("pg_catalog", "pg_proc")).as("p");
        var nsp = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
        Field<String> name = DSL.field(DSL.name("p", "proname"), String.class);
        Field<Object> oid = DSL.field(DSL.name("p", "oid"), Object.class);
        Field<String> args = DSL.function("pg_get_function_arguments", String.class, oid);
        Field<String> src = DSL.field(DSL.name("p", "prosrc"), String.class);
        Field<Boolean> svcExec = DSL.function("has_function_privilege", Boolean.class,
            DSL.inline("nexus_svc"), oid, DSL.inline("EXECUTE"));
        String families = "^(plain_search|hybrid_search|manifest_search|text_gated_search_allowed|"
            + "text_gated_search_by_chash|text_gated_search_hnsw_first|search_metadata_scoped|"
            + "search_aspect_scoped|search_graph_hop|search_topic_scoped)_(384|768|1024)$";
        try (Connection su = pg.createConnection("")) {
            var rows = DSL.using(su, SQLDialect.POSTGRES)
                .select(name, args, src, svcExec)
                .from(proc)
                .join(nsp).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("p", "pronamespace"))))
                .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus")
                    .and(name.likeRegex(families).or(name.eq("manifest_qualifying_hits"))))
                .orderBy(name)
                .fetch();
            assertThat(rows).as("10 families x 3 dimensions + manifest_qualifying_hits, one signature each")
                .hasSize(31);
            assertThat(rows.getValues(name)).doesNotHaveDuplicates();
            for (var rec : rows) {
                String fn = rec.get(name);
                assertThat(rec.get(args)).as(fn).endsWith("p_exclude_sensitive boolean DEFAULT false");
                assertThat(rec.get(src)).as(fn).contains("p_exclude_sensitive").contains("sd.sensitive");
                assertThat(rec.get(svcExec)).as(fn + " nexus_svc EXECUTE").isTrue();
            }
        }
    }

    // ── routes ────────────────────────────────────────────────────────────────

    record Route(String path, Map<String, Object> body, boolean ordered) {
        @Override public String toString() { return path; }
    }

    private static Map<String, Object> body(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) m.put((String) kv[i], kv[i + 1]);
        return m;
    }

    static Stream<Route> routes() {
        String c = coll(384);
        return Stream.of(
            new Route("/v1/vectors/search", body("query", QUERY, "collections", List.of(c), "n_results", K), true),
            new Route("/v1/vectors/query", body("query", QUERY, "collections", List.of(c), "n_results", K), true),
            new Route("/v1/vectors/search-per-collection",
                      body("query", QUERY, "collections", List.of(c), "per_collection_k", K, "limit", K), true),
            new Route("/v1/vectors/hybrid-search",
                      body("query", GATE, "collections", List.of(c), "n_results", K), false),
            new Route("/v1/vectors/search-metadata-scoped",
                      body("query", QUERY, "collections", List.of(c), "n_results", K), true),
            new Route("/v1/vectors/search-topic-scoped",
                      body("query", QUERY, "topic", TOPIC, "collection", c, "n_results", K), true),
            new Route("/v1/vectors/search-graph-hop",
                      body("query", QUERY, "seeds", List.of(tumbler("seed", 384)), "collections", List.of(c),
                           "link_type", "cites", "depth", 1, "direction", "out", "n_results", K), true),
            new Route("/v1/vectors/search-aspect-scoped",
                      body("query", QUERY, "collections", List.of(c), "n_results", K), true));
    }

    private HttpResponse<String> post(Route route, Object sensitivity, boolean withSensitivity) throws Exception {
        Map<String, Object> b = new LinkedHashMap<>(route.body());
        if (withSensitivity) b.put("sensitivity", sensitivity);
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + route.path())
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(b)))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private static JsonNode rows(HttpResponse<String> r) throws Exception {
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode root = MAPPER.readTree(r.body());
        return root.isArray() ? root : root.get("results");
    }

    /** The chunk hash of a hit: document-level rows carry it as {@code chash} (their id is the tumbler). */
    private static String chashOf(JsonNode row) {
        return row.has("chash") ? row.get("chash").asText() : row.get("id").asText();
    }

    @ParameterizedTest(name = "{0}")
    @MethodSource("routes")
    void route_excludeDropsSensitiveChunks_andEveryHitCarriesSensitive(Route route) throws Exception {
        List<String> got = new ArrayList<>();
        for (JsonNode row : rows(post(route, "exclude", true))) {
            got.add(chashOf(row));
            assertThat(row.has("sensitive")).as("sensitive on every hit: " + row).isTrue();
            assertThat(row.get("sensitive").asBoolean()).as("an excluded page holds no sensitive hit").isFalse();
        }
        assertThat(got).hasSize(K).doesNotContainAnyElementsOf(SENSITIVE_HEX);
        if (route.ordered()) {
            assertThat(got).containsExactlyElementsOf(hexes("n1", "n2", "n3", "n4", "n5"));
        }

        // without the parameter the sensitive rows are served, and flagged
        boolean sawSensitive = false;
        for (JsonNode row : rows(post(route, null, false))) {
            assertThat(row.has("sensitive")).as("sensitive even when the request did not ask: " + row).isTrue();
            boolean flag = row.get("sensitive").asBoolean();
            assertThat(flag).as("sensitive agrees with the chunk's owners: " + chashOf(row))
                .isEqualTo(SENSITIVE_HEX.contains(chashOf(row)));
            sawSensitive |= flag;
        }
        assertThat(sawSensitive).as("the baseline page holds a sensitive hit").isTrue();
    }

    @ParameterizedTest(name = "{0}")
    @MethodSource("routes")
    void route_refusesAnySensitivityValueButExclude(Route route) throws Exception {
        for (Object bad : new Object[] {"include", "EXCLUDE", "", "none", 1, true, List.of("exclude")}) {
            var r = post(route, bad, true);
            assertThat(r.statusCode()).as(route.path() + " sensitivity=" + bad + ": " + r.body()).isEqualTo(400);
            assertThat(r.body()).contains("sensitivity");
        }
        assertThat(post(route, null, true).statusCode()).as("JSON null is absent").isEqualTo(200);
    }

    @Test
    void searchRoute_sharedChunkFollowsTheSensitiveOwnersTombstone() throws Exception {
        Route search = routes().findFirst().orElseThrow();
        List<String> live = new ArrayList<>();
        for (JsonNode row : rows(post(search, "exclude", true))) live.add(chashOf(row));
        assertThat(live).doesNotContain(hex(X));

        String sx = tumbler("sx", 384);
        setTombstone(sx, true);
        try {
            List<String> after = new ArrayList<>();
            for (JsonNode row : rows(post(search, "exclude", true))) {
                after.add(chashOf(row));
                assertThat(row.get("sensitive").asBoolean()).isFalse();
            }
            assertThat(after).containsExactlyElementsOf(hexes("x", "n1", "n2", "n3", "n4"));
        } finally {
            setTombstone(sx, false);
        }
    }

    // ── get-by-id, store-get and the catalog by-address reads (nexus-rfvyy.7.4) ──

    /** The routes this class drives for the get reads; the coverage test below ties them to the derived set. */
    private static final String GET = "/v1/vectors/get";
    private static final String STORE_GET = "/v1/vectors/store-get";
    private static final List<String> ALL_HEX = hexes(S1, X, S2, "n1", "n2", "n3", "n4", "n5", "n6");
    private static final List<String> NON_SENSITIVE_HEX = hexes("n1", "n2", "n3", "n4", "n5", "n6");

    private JsonNode postJson(String path, Map<String, Object> b, int expectedStatus) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(b)))
            .build();
        var r = http.send(req, HttpResponse.BodyHandlers.ofString());
        assertThat(r.statusCode()).as(path + " " + r.body()).isEqualTo(expectedStatus);
        return MAPPER.readTree(r.body());
    }

    private static List<String> texts(JsonNode arr) {
        List<String> out = new ArrayList<>();
        for (JsonNode n : arr) out.add(n.asText());
        return out;
    }

    private static List<Boolean> flags(JsonNode arr) {
        List<Boolean> out = new ArrayList<>();
        for (JsonNode n : arr) out.add(n.asBoolean());
        return out;
    }

    /** A get request for {@code route}: store-get names every chunk id, get scans the collection. */
    private static Map<String, Object> getBody(String route, int dim, Object... extra) {
        Map<String, Object> b = body("collection", coll(dim), "limit", 50);
        if (route.equals(STORE_GET)) b.put("ids", ALL_HEX);
        for (int i = 0; i < extra.length; i += 2) b.put((String) extra[i], extra[i + 1]);
        return b;
    }

    static Stream<org.junit.jupiter.params.provider.Arguments> getRouteByDim() {
        List<org.junit.jupiter.params.provider.Arguments> out = new ArrayList<>();
        for (String route : new String[] {GET, STORE_GET}) {
            for (int dim : DIMS) out.add(org.junit.jupiter.params.provider.Arguments.of(route, dim));
        }
        return out.stream();
    }

    @ParameterizedTest(name = "{0} dim {1}")
    @MethodSource("getRouteByDim")
    void getRoutes_excludeDropsSensitiveChunks_andSensitivesIsParallelToIds(String route, int dim) throws Exception {
        // baseline: every chunk is served, and flagged
        JsonNode all = postJson(route, getBody(route, dim), 200);
        List<String> ids = texts(all.get("ids"));
        assertThat(ids).containsExactlyInAnyOrderElementsOf(ALL_HEX);
        List<Boolean> flags = flags(all.get("sensitives"));
        assertThat(flags).as("sensitives parallel to ids").hasSameSizeAs(ids);
        for (int i = 0; i < ids.size(); i++) {
            assertThat(flags.get(i)).as("sensitive flag of " + ids.get(i)).isEqualTo(SENSITIVE_HEX.contains(ids.get(i)));
        }
        assertThat(flags).as("the baseline holds a sensitive row").contains(true);
        assertThat(all.get("documents")).hasSameSizeAs(all.get("ids"));

        // exclude: the three sensitive chunks (s1, s2 and the shared x) are gone, the rest remain, none flagged
        JsonNode ex = postJson(route, getBody(route, dim, "sensitivity", "exclude"), 200);
        assertThat(texts(ex.get("ids"))).containsExactlyInAnyOrderElementsOf(NON_SENSITIVE_HEX);
        assertThat(flags(ex.get("sensitives"))).hasSize(6).containsOnly(false);
        assertThat(ex.get("documents")).hasSize(6);
        for (JsonNode d : ex.get("documents")) {
            assertThat(d.asText()).as("no sensitive chunk's text is served").doesNotContain("chunk s1 ").doesNotContain("chunk s2 ")
                .doesNotContain("chunk x ");
        }
        if (route.equals(STORE_GET)) {
            assertThat(ex.get("count").asLong()).as("the matched-live count is over the surviving rows").isEqualTo(6);
            assertThat(all.get("count").asLong()).isEqualTo(9);
        }
    }

    @ParameterizedTest(name = "{0}")
    @ValueSource(strings = {GET, STORE_GET})
    void getRoutes_pageOverTheSurvivingRows_notTheFetchedOnes(String route) throws Exception {
        // limit 4 over six survivors: a post-filter over a nine-row page would come back short or empty
        List<String> seen = new ArrayList<>();
        for (int offset = 0; offset < 8; offset += 4) {
            JsonNode page = postJson(route, getBody(route, 384, "sensitivity", "exclude", "limit", 4,
                                                     "offset", offset), 200);
            assertThat(page.get("ids").size()).as("offset " + offset).isEqualTo(offset == 0 ? 4 : 2);
            seen.addAll(texts(page.get("ids")));
        }
        assertThat(seen).containsExactlyInAnyOrderElementsOf(NON_SENSITIVE_HEX);
        if (route.equals(STORE_GET)) {
            JsonNode first = postJson(route, getBody(route, 384, "sensitivity", "exclude", "limit", 4), 200);
            assertThat(first.get("count").asLong()).as("count is the surviving total, not the page").isEqualTo(6);
        }
    }

    @ParameterizedTest(name = "{0}")
    @ValueSource(strings = {GET, STORE_GET})
    void getRoutes_refuseAnySensitivityValueButExclude(String route) throws Exception {
        for (Object bad : new Object[] {"include", "EXCLUDE", "", "none", 1, true, List.of("exclude")}) {
            JsonNode err = postJson(route, getBody(route, 384, "sensitivity", bad), 400);
            assertThat(err.toString()).contains("sensitivity");
        }
        postJson(route, getBody(route, 384, "sensitivity", null), 200);
    }

    @ParameterizedTest(name = "{0}")
    @ValueSource(strings = {GET, STORE_GET})
    void getRoutes_sharedChunkFollowsTheSensitiveOwnersTombstone(String route) throws Exception {
        assertThat(texts(postJson(route, getBody(route, 384, "sensitivity", "exclude"), 200).get("ids")))
            .doesNotContain(hex(X));
        String sx = tumbler("sx", 384);
        setTombstone(sx, true);
        try {
            JsonNode after = postJson(route, getBody(route, 384, "sensitivity", "exclude"), 200);
            assertThat(texts(after.get("ids"))).as("x comes back once its sensitive owner is tombstoned")
                .contains(hex(X)).doesNotContain(hex(S1), hex(S2));
            assertThat(flags(after.get("sensitives"))).containsOnly(false);
            // and the baseline flags it false too, since no live owner is sensitive now
            JsonNode base = postJson(route, getBody(route, 384), 200);
            int at = texts(base.get("ids")).indexOf(hex(X));
            assertThat(flags(base.get("sensitives")).get(at)).isFalse();
        } finally {
            setTombstone(sx, false);
        }
    }

    @Test
    void getRoutes_theInventoryBranchesDropSensitiveChunksToo() throws Exception {
        JsonNode viaGet = postJson(GET, getBody(GET, 384, "include_non_live", true, "sensitivity", "exclude"), 200);
        assertThat(texts(viaGet.get("ids"))).containsExactlyInAnyOrderElementsOf(NON_SENSITIVE_HEX);
        JsonNode viaStoreGet = postJson(STORE_GET,
            getBody(STORE_GET, 384, "include_non_live", true, "sensitivity", "exclude"), 200);
        assertThat(texts(viaStoreGet.get("ids"))).containsExactlyInAnyOrderElementsOf(NON_SENSITIVE_HEX);
        // without the parameter the inventory is complete, as it always was
        JsonNode full = postJson(STORE_GET, getBody(STORE_GET, 384, "include_non_live", true), 200);
        assertThat(texts(full.get("ids"))).containsExactlyInAnyOrderElementsOf(ALL_HEX);
    }

    private HttpResponse<String> getCatalog(String path) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + TOKEN).GET().build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    @Test
    void resolveSpan_carriesSensitive_andExcludeMakesASensitiveChunkAMiss() throws Exception {
        String c = coll(384);
        String base = "/v1/catalog/resolve_span?collection=" + c + "&span_chash=";
        var sens = getCatalog(base + hex(S1));
        assertThat(sens.statusCode()).isEqualTo(200);
        assertThat(MAPPER.readTree(sens.body()).get("sensitive").asBoolean()).isTrue();
        var plain = getCatalog(base + hex("n1"));
        assertThat(plain.statusCode()).isEqualTo(200);
        assertThat(MAPPER.readTree(plain.body()).get("sensitive").asBoolean()).isFalse();

        assertThat(getCatalog(base + hex(S1) + "&sensitivity=exclude").statusCode()).as("sensitive: a miss").isEqualTo(404);
        assertThat(getCatalog(base + hex(X) + "&sensitivity=exclude").statusCode()).as("shared with a live sensitive owner")
            .isEqualTo(404);
        var kept = getCatalog(base + hex("n1") + "&sensitivity=exclude");
        assertThat(kept.statusCode()).isEqualTo(200);
        assertThat(MAPPER.readTree(kept.body()).get("chunk_text").asText()).isEqualTo(text("n1"));
        for (String bad : new String[] {"include", "EXCLUDE", ""}) {
            var r = getCatalog(base + hex("n1") + "&sensitivity=" + bad);
            assertThat(r.statusCode()).as("sensitivity=" + bad + ": " + r.body()).isEqualTo(400);
            assertThat(r.body()).contains("sensitivity");
        }
    }

    @Test
    void resolveChash_excludeSkipsSensitiveCandidates_andFallsThroughToAnotherCopy() throws Exception {
        String base = "/v1/catalog/resolve_chash?chash=";
        var sens = getCatalog(base + hex(S1) + "&prefer_collection=" + coll(384));
        assertThat(sens.statusCode()).isEqualTo(200);
        assertThat(MAPPER.readTree(sens.body()).get("sensitive").asBoolean()).isTrue();
        assertThat(getCatalog(base + hex(S1) + "&sensitivity=exclude").statusCode())
            .as("held only by sensitive chunks: a miss").isEqualTo(404);
        var plain = getCatalog(base + hex("n1") + "&sensitivity=exclude");
        assertThat(plain.statusCode()).isEqualTo(200);
        assertThat(MAPPER.readTree(plain.body()).get("sensitive").asBoolean()).isFalse();
        var bad = getCatalog(base + hex("n1") + "&sensitivity=nope");
        assertThat(bad.statusCode()).isEqualTo(400);

        // x is sensitive in all three collections; once its sensitive owners in 768 and 1024 are tombstoned the
        // copies there are not, and an exclude read that prefers 384 resolves to one of them
        String s768 = tumbler("sx", 768), s1024 = tumbler("sx", 1024);
        setTombstone(s768, true);
        setTombstone(s1024, true);
        try {
            var prefer = base + hex(X) + "&prefer_collection=" + coll(384);
            var withSensitive = MAPPER.readTree(getCatalog(prefer).body());
            assertThat(withSensitive.get("physical_collection").asText()).isEqualTo(coll(384));
            assertThat(withSensitive.get("sensitive").asBoolean()).isTrue();
            var excluded = getCatalog(prefer + "&sensitivity=exclude");
            assertThat(excluded.statusCode()).isEqualTo(200);
            var node = MAPPER.readTree(excluded.body());
            assertThat(node.get("physical_collection").asText()).isIn(coll(768), coll(1024));
            assertThat(node.get("sensitive").asBoolean()).isFalse();
        } finally {
            setTombstone(s768, false);
            setTombstone(s1024, false);
        }
    }

    /** Tombstone every dimension's copy of the document stems, returning the tumblers so the caller can revive them. */
    private List<String> tombstoneAll(String... stems) throws Exception {
        List<String> docs = new ArrayList<>();
        for (String stem : stems) for (int dim : DIMS) docs.add(tumbler(stem, dim));
        for (String d : docs) setTombstone(d, true);
        return docs;
    }

    private void revive(List<String> docs) throws Exception {
        for (String d : docs) setTombstone(d, false);
    }

    /**
     * Review fix F1 (critic S1, nexus-rfvyy.7.4): the by-address reads gate on "no live sensitive owner", which a chunk
     * with NO live owner passes trivially, so a sensitive document's chunk that the reaper has not collected yet was
     * served under exclude, while search and get (which require a live owner) hide it. Under exclude a chunk must
     * also have a live owner.
     */
    @Test
    void resolveSpan_excludeNeedsALiveOwner_aTombstonedSoleSensitiveOwnerIsAMiss() throws Exception {
        String base = "/v1/catalog/resolve_span?collection=" + coll(384) + "&span_chash=";
        String sole = tumbler("s1", 384);
        setTombstone(sole, true);
        try {
            assertThat(getCatalog(base + hex(S1) + "&sensitivity=exclude").statusCode())
                .as("sole owner tombstoned, chunk not yet reaped: not served under exclude").isEqualTo(404);
            var plain = getCatalog(base + hex(S1));
            assertThat(plain.statusCode()).as("without exclude the read is as it always was").isEqualTo(200);
            assertThat(MAPPER.readTree(plain.body()).get("sensitive").asBoolean()).isFalse();
        } finally {
            setTombstone(sole, false);
        }

        // shared chunk x (owners sx sensitive, nx not): the live non-sensitive owner keeps it served once sx is gone,
        // and with both owners gone it is a miss
        String sx = tumbler("sx", 384), nx = tumbler("nx", 384);
        setTombstone(sx, true);
        try {
            var kept = getCatalog(base + hex(X) + "&sensitivity=exclude");
            assertThat(kept.statusCode()).as("a live non-sensitive owner remains").isEqualTo(200);
            assertThat(MAPPER.readTree(kept.body()).get("sensitive").asBoolean()).isFalse();
            setTombstone(nx, true);
            assertThat(getCatalog(base + hex(X) + "&sensitivity=exclude").statusCode())
                .as("both owners tombstoned: no live owner").isEqualTo(404);
        } finally {
            setTombstone(sx, false);
            setTombstone(nx, false);
        }
        assertThat(getCatalog(base + hex(X) + "&sensitivity=exclude").statusCode())
            .as("sx live again: sensitive").isEqualTo(404);
        assertThat(getCatalog(base + hex("n1") + "&sensitivity=exclude").statusCode()).isEqualTo(200);
    }

    @Test
    void resolveChash_excludeNeedsALiveOwner_aCopyWithNoLiveOwnerIsNotServed() throws Exception {
        String base = "/v1/catalog/resolve_chash?chash=";
        // s1 is sensitive-owned in all three collections; with every owner tombstoned no copy has a live owner
        List<String> gone = tombstoneAll("s1");
        try {
            assertThat(getCatalog(base + hex(S1) + "&sensitivity=exclude").statusCode())
                .as("no copy has a live owner: a miss under exclude").isEqualTo(404);
            assertThat(getCatalog(base + hex(S1)).statusCode()).as("without exclude: as it always was").isEqualTo(200);
        } finally {
            revive(gone);
        }

        // x: sensitive owners sx in every collection; tombstone them all, and the non-sensitive owners nx in 768 and
        // 1024, leaving only nx-384 live: the exclude read falls through to that copy, preferred or not
        List<String> sx = tombstoneAll("sx");
        List<String> nx = List.of(tumbler("nx", 768), tumbler("nx", 1024));
        for (String d : nx) setTombstone(d, true);
        try {
            var r = getCatalog(base + hex(X) + "&prefer_collection=" + coll(1024) + "&sensitivity=exclude");
            assertThat(r.statusCode()).isEqualTo(200);
            var node = MAPPER.readTree(r.body());
            assertThat(node.get("physical_collection").asText()).as("the only copy with a live owner").isEqualTo(coll(384));
            assertThat(node.get("sensitive").asBoolean()).isFalse();
            setTombstone(tumbler("nx", 384), true);
            assertThat(getCatalog(base + hex(X) + "&sensitivity=exclude").statusCode())
                .as("the last live owner gone").isEqualTo(404);
        } finally {
            revive(sx);
            for (String d : nx) setTombstone(d, false);
            setTombstone(tumbler("nx", 384), false);
        }
    }

    // ── every route the parity census derives has a behaviour recipe here ─────

    @Test
    void everyChunkTextRouteTheCensusDerivesHasARecipeInThisClass() throws Exception {
        Set<String> derived = VectorSensitivityRouteParityTest.derivedTextRoutes(
            VectorSensitivityRouteParityTest.findings(
                VectorSensitivityRouteParityTest.textFunctionsFromPgProc(pg).keySet()));
        Set<String> recipes = new java.util.TreeSet<>();
        routes().forEach(r -> recipes.add(r.path()));
        recipes.add(GET);
        recipes.add(STORE_GET);
        assertThat(derived).as("non-vacuity").hasSizeGreaterThanOrEqualTo(10);
        assertThat(recipes).as("a derived chunk-text route with no behaviour recipe: add one, and its exclusion test")
            .containsAll(derived);
    }

    // ── the planner: the term is inlined, and gone when the argument is false ──

    private String planOf(boolean exclude) {
        Table<?> t = fnTable("plain_search", 384, exclude, vec(384), textArray(coll(384)), nullJsonb(), nullText(),
                             DSL.val(K), DSL.val(MODELS.get(384)), DSL.val(TENANT));
        return scope.withTenant(TENANT, ctx -> PgContainerHelper.explainPlanInlined(ctx, ctx.selectFrom(t)));
    }

    @Test
    void plan_inlinesTheSensitiveTest_andFoldsItAwayWhenTheArgumentIsFalse() {
        String on = planOf(true);
        String off = planOf(false);
        assertThat(on).as("exclude=true: the sensitive owner test is part of the statement").contains("sensitive");
        assertThat(on).as("a plain NOT EXISTS over the owners, not a function call per row")
            .doesNotContain("chunk_sensitive");
        assertThat(off).as("exclude=false: the term is folded away, the plan is the one it was")
            .doesNotContain("sensitive");
    }

    // ── a manifest-answered where keeps the exclusion (manifest_search and the route (b) functions) ──

    @Test
    void manifestAnsweredWhere_keepsTheExclusion_onSearchAndMetadataScoped() throws Exception {
        Map<String, Object> where = body("content_type", "paper");
        for (String path : List.of("/v1/vectors/search", "/v1/vectors/search-metadata-scoped")) {
            Route route = new Route(path, body("query", QUERY, "collections", List.of(coll(384)), "n_results", 10,
                                               "where", where), true);
            List<String> got = new ArrayList<>();
            for (JsonNode row : rows(post(route, "exclude", true))) {
                got.add(chashOf(row));
                assertThat(row.get("sensitive").asBoolean()).isFalse();
            }
            assertThat(got).as(path).containsExactlyInAnyOrderElementsOf(hexes("n1", "n2", "n3", "n4", "n5", "n6"));
            List<String> all = new ArrayList<>();
            for (JsonNode row : rows(post(route, null, false))) all.add(chashOf(row));
            assertThat(all).as(path + " baseline").containsAll(SENSITIVE_HEX);
        }
    }

    // ── the HNSW walk: a crowd of sensitive near rows must not shorten the page ──

    @Test
    void exclude_fillsKFromNonSensitiveRows_whenSensitiveRowsCrowdTheNearNeighbourhood() throws Exception {
        String crowd = "knowledge__rfvyy73-crowd__minilm-l6-v2-384__v1";
        int sensitiveRows = 400;
        int plainRows = 40;
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, crowd);
        }
        List<String> sensitiveIds = new ArrayList<>();
        List<String> sensitiveTexts = new ArrayList<>();
        for (int i = 0; i < sensitiveRows; i++) {
            String t = "crowd sensitive " + i;
            embedder.register(t, (float) Math.cos(i * 0.0005), (float) Math.sin(i * 0.0005));
            sensitiveIds.add(Chash.ofText(t).toHex());
            sensitiveTexts.add(t);
        }
        List<String> plainIds = new ArrayList<>();
        List<String> plainTexts = new ArrayList<>();
        for (int j = 0; j < plainRows; j++) {
            String t = "crowd plain " + j;
            embedder.register(t, (float) Math.cos(2.5 + j * 0.01), (float) Math.sin(2.5 + j * 0.01));
            plainIds.add(Chash.ofText(t).toHex());
            plainTexts.add(t);
        }
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < sensitiveRows; i++) metas.add(Map.of());
        repo.upsertChunks(TENANT, crowd, sensitiveIds, sensitiveTexts, metas);
        List<Map<String, Object>> plainMetas = new ArrayList<>();
        for (int j = 0; j < plainRows; j++) plainMetas.add(Map.of());
        repo.upsertChunks(TENANT, crowd, plainIds, plainTexts, plainMetas);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            String model = PgContainerHelper.collectionModel(dsl, TENANT, crowd);
            ownAll(dsl, crowd, model, "crowd-sensitive", true, sensitiveIds);
            ownAll(dsl, crowd, model, "crowd-plain", false, plainIds);
        }

        // Penalise every non-HNSW plan and cap the walk's first batch, so the statement is an index-ordered walk
        // that has to keep scanning past the 400 sensitive neighbours to find ten rows it may return.
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(2);
        cfg.setAutoCommit(true);
        cfg.addDataSourceProperty("options", "-c enable_seqscan=off -c enable_bitmapscan=off -c enable_sort=off");
        try (HikariDataSource walkDs = new HikariDataSource(cfg)) {
            PgSession.overrideEfSearchFloorForTests(40);
            PgSession.overrideSearchExactMaxRowsForTests(0);
            var walkRepo = new PgVectorRepository(new TenantScope(walkDs), embedder, embedder);
            embedder.register("crowd query", 1f, 0f);
            var plain = walkRepo.searchWithTokens(TENANT, "crowd query", List.of(crowd), 10, null, false, null, false)
                .value();
            assertThat(plain.stream().map(r -> (String) r.get("id")).toList())
                .as("baseline: the ten nearest are all sensitive").allMatch(sensitiveIds::contains).hasSize(10);

            var excluded = walkRepo.searchWithTokens(TENANT, "crowd query", List.of(crowd), 10, null, false, null, true)
                .value();
            assertThat(excluded.stream().map(r -> (String) r.get("id")).toList())
                .as("k rows from the non-sensitive far rows, nearest first")
                .containsExactlyElementsOf(plainIds.subList(0, 10));
            assertThat(excluded).allSatisfy(r -> assertThat(r.get("sensitive")).isEqualTo(false));
        }
    }

    private static void ownAll(DSLContext dsl, String collection, String model, String stem, boolean sensitive,
                               List<String> chashes) {
        dsl.insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT).set(CATALOG_DOCUMENTS.TUMBLER, stem)
            .set(CATALOG_DOCUMENTS.TITLE, stem).set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection)
            .set(CATALOG_DOCUMENTS.SENSITIVE, sensitive)
            .execute();
        for (int from = 0; from < chashes.size(); from += 200) {
            var step = dsl.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
                CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL);
            org.jooq.InsertValuesStep6<?, String, String, Integer, byte[], String, String> values = null;
            for (int i = from; i < Math.min(chashes.size(), from + 200); i++) {
                byte[] chash = HexFormat.of().parseHex(chashes.get(i));
                values = (values == null ? step : values).values(TENANT, stem, i, chash, collection, model);
            }
            values.execute();
        }
    }
}
