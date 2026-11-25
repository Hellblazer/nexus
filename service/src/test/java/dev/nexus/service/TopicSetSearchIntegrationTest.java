// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CollectionIds;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
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
import org.junit.jupiter.params.provider.ValueSource;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-7fku2.1.1 (RDR-134 Step 1): {@code nexus.search_topic_set_<dim>}, the topic-SET variant of
 * {@code search_topic_scoped_<dim>}. It takes the ids of the topics a question was routed to (the centroid ANN
 * returns ids) instead of one label, and returns one collection's nearest live chunks that carry ANY of them.
 *
 * <p>What the test pins, per dimension (the functions exist once per dimension, so a missed dimension is a hole
 * nothing else notices):
 * <ul>
 *   <li>a set of one topic returns exactly the rows {@code search_topic_scoped} returns for its label: same
 *       ids, same order, same distances;</li>
 *   <li>a set of two returns the union, a chunk assigned to both once, nearest first, capped at {@code p_n}
 *       (the per-collection k);</li>
 *   <li>{@code p_exclude_sensitive} (true or NULL) drops a chunk with a live sensitive owner; a TOMBSTONED
 *       sensitive owner does not;</li>
 *   <li>the threshold: the function takes none and returns raw cosine distances, the same ones
 *       {@code plain_search} returns, so the Java merge applies the client threshold (and counts
 *       {@code rawCount} and {@code dropped}) exactly as it does for the plain search. Decision recorded in
 *       vectors-062-1;</li>
 *   <li>a topic of another collection never matches, whichever side of the pair is wrong: the
 *       {@code t.collection = p_collection} guard is what stops a same-chash chunk of the searched collection;</li>
 *   <li>an empty or NULL set returns no rows; an unassigned chunk, a chunk with no live owner and a chunk whose
 *       only owner is tombstoned are never returned.</li>
 * </ul>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TopicSetSearchIntegrationTest {

    private static final String TENANT = "7fku211-topicset";
    private static final int[] DIMS = {384, 768, 1024};
    private static final OffsetDateTime SEED_TS = OffsetDateTime.parse("2026-01-01T00:00:00+00:00");

    /** Chunk key to angle (radians) from the query vector (1, 0): the nearest key has the smallest angle. */
    private static final Map<String, Double> ANGLE = new LinkedHashMap<>();
    static {
        ANGLE.put("dT", 0.01);   // topic B; owned by a tombstoned sensitive doc AND a live plain doc: returned
        ANGLE.put("dead", 0.02); // topic A; no owner row at all: never returned
        ANGLE.put("u1", 0.03);   // assigned to no topic: never returned
        ANGLE.put("z1", 0.04);   // no topic in collection 1; assigned to topic C of the OTHER collection
        ANGLE.put("tomb", 0.045);// topic B; its only owner is tombstoned: never returned
        ANGLE.put("sA", 0.05);   // topic A; its owner is sensitive and live: excluded when asked
        ANGLE.put("a1", 0.10);
        ANGLE.put("p1", 0.12);   // topic A through a PROJECTION row: its source_collection is collection 2, not A's
        ANGLE.put("ab", 0.15);   // in topics A and B
        ANGLE.put("b1", 0.20);
        ANGLE.put("a2", 0.30);
        ANGLE.put("b2", 0.40);
        ANGLE.put("a3", 0.50);
        ANGLE.put("far", 2.50);  // topic A; a distance (1 - cos 2.5 = 1.80) above any client threshold
    }

    /** Document stem to the chunk key it owns, then whether the doc is sensitive and whether it is tombstoned. */
    private record Owner(String chunk, boolean sensitive, boolean tombstoned) {}
    private static final Map<String, Owner> OWNERS = new LinkedHashMap<>();
    static {
        OWNERS.put("o-dTs", new Owner("dT", true, true));
        OWNERS.put("o-dTn", new Owner("dT", false, false));
        OWNERS.put("o-u1", new Owner("u1", false, false));
        OWNERS.put("o-z1", new Owner("z1", false, false));
        OWNERS.put("o-tomb", new Owner("tomb", false, true));
        OWNERS.put("o-sA", new Owner("sA", true, false));
        for (String k : List.of("a1", "p1", "ab", "b1", "a2", "b2", "a3", "far")) {
            OWNERS.put("o-" + k, new Owner(k, false, false));
        }
    }

    private static final Map<String, List<String>> TOPIC_A_B = new LinkedHashMap<>();
    static {
        TOPIC_A_B.put("A", List.of("dead", "sA", "a1", "p1", "ab", "a2", "a3", "far"));
        TOPIC_A_B.put("B", List.of("dT", "tomb", "ab", "b1", "b2"));
    }

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    TenantScope scope;
    /** "dim/label" to the topic id; labels A and B in collection 1, C in collection 2. */
    private final Map<String, Long> topicIds = new ConcurrentHashMap<>();
    private final Map<Integer, String> models = new ConcurrentHashMap<>();

    static String coll(int dim) {
        return switch (dim) {
            case 384 -> "knowledge__7fku211a-384__minilm-l6-v2-384__v1";
            case 768 -> "knowledge__7fku211a-768__bge-base-en-v15-768__v1";
            default -> "knowledge__7fku211a-1024__voyage-context-3__v1";
        };
    }

    /** Collection 2: the same model as {@link #coll}, holding chunks that share a chash with collection 1's. */
    static String collB(int dim) {
        return coll(dim).replace("7fku211a", "7fku211b");
    }

    private static String text(String key) {
        return "topic set chunk " + key;
    }

    private static byte[] chash(String key) {
        return HexFormat.of().parseHex(Chash.ofText(text(key)).toHex());
    }

    private static String hex(String key) {
        return Chash.ofText(text(key)).toHex();
    }

    private static List<String> hexes(String... keys) {
        List<String> out = new ArrayList<>();
        for (String k : keys) out.add(hex(k));
        return out;
    }

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (int dim : DIMS) seedDim(dsl, dim);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
        scope = new TenantScope(ds);
    }

    @AfterAll
    void stopAll() {
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    // ── fixture ───────────────────────────────────────────────────────────────

    private void insertChunk(DSLContext dsl, int dim, String collection, String model, String key, double theta) {
        dsl.insertInto(CHUNKS)
            .set(CHUNKS.TENANT_ID, TENANT).set(CHUNKS.COLLECTION, collection).set(CHUNKS.COLLECTION_ID, CollectionIds.idOf(TENANT, collection)).set(CHUNKS.CHASH, chash(key))
            .set(CHUNKS.EMBEDDING_MODEL, model).set(CHUNKS.CHUNK_TEXT, text(key))
            .set(VectorSeeds.chunkEmbedding(dim),
                 VectorSeeds.vecXY(dim, (float) Math.cos(theta), (float) Math.sin(theta)))
            .execute();
    }

    private long insertTopic(DSLContext dsl, String label, String collection) {
        return dsl.insertInto(TOPICS)
            .set(TOPICS.TENANT_ID, TENANT).set(TOPICS.LABEL, label).set(TOPICS.COLLECTION, collection).set(TOPICS.COLLECTION_ID, CollectionIds.idOf(TENANT, collection))
            .set(TOPICS.CREATED_AT, SEED_TS)
            .returningResult(TOPICS.ID).fetchOne(TOPICS.ID);
    }

    private void assign(DSLContext dsl, long topic, String sourceCollection, String model, String key) {
        dsl.insertInto(TOPIC_ASSIGNMENTS)
            .set(TOPIC_ASSIGNMENTS.TENANT_ID, TENANT).set(TOPIC_ASSIGNMENTS.DOC_ID, chash(key))
            .set(TOPIC_ASSIGNMENTS.TOPIC_ID, topic).set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, sourceCollection).set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID, CollectionIds.idOf(TENANT, sourceCollection))
            .set(TOPIC_ASSIGNMENTS.ASSIGNED_AT, SEED_TS).set(TOPIC_ASSIGNMENTS.EMBEDDING_MODEL, model)
            .execute();
    }

    private void own(DSLContext dsl, String doc, String collection, String model, String key, boolean sensitive,
                     boolean tombstoned) {
        dsl.insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT).set(CATALOG_DOCUMENTS.TUMBLER, doc)
            .set(CATALOG_DOCUMENTS.TITLE, "Doc " + doc).set(CATALOG_DOCUMENTS.CONTENT_TYPE, "paper")
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection)
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, CollectionIds.idOf(TENANT, collection))
            .set(CATALOG_DOCUMENTS.SENSITIVE, sensitive)
            .set(CATALOG_DOCUMENTS.DELETED_AT, tombstoned ? SEED_TS : null)
            .execute();
        dsl.insertInto(CATALOG_DOCUMENT_CHUNKS)
            .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, TENANT).set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, doc)
            .set(CATALOG_DOCUMENT_CHUNKS.POSITION, 0).set(CATALOG_DOCUMENT_CHUNKS.CHASH, chash(key))
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, collection)
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, CollectionIds.idOf(TENANT, collection))
            .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, model)
            .execute();
    }

    private void seedDim(DSLContext dsl, int dim) {
        String c1 = coll(dim);
        String c2 = collB(dim);
        PgContainerHelper.insertCollection(dsl, TENANT, c1);
        PgContainerHelper.insertCollection(dsl, TENANT, c2);
        String model = PgContainerHelper.collectionModel(dsl, TENANT, c1);
        models.put(dim, model);

        for (var e : ANGLE.entrySet()) insertChunk(dsl, dim, c1, model, e.getKey(), e.getValue());
        for (var e : OWNERS.entrySet()) {
            Owner o = e.getValue();
            own(dsl, e.getKey() + "-" + dim, c1, model, o.chunk(), o.sensitive(), o.tombstoned());
        }
        // Collection 2 holds a1, p1 and z1 under the SAME chashes. Its topic C assigns z1; topic A (collection 1)
        // assigns a1. Without the t.collection = p_collection guard the join by chash alone would let C match
        // z1 in collection 1, and A match a1 in collection 2.
        insertChunk(dsl, dim, c2, model, "a1", 0.10);
        insertChunk(dsl, dim, c2, model, "p1", 0.12);
        insertChunk(dsl, dim, c2, model, "z1", 0.04);
        own(dsl, "o2-a1-" + dim, c2, model, "a1", false, false);
        own(dsl, "o2-z1-" + dim, c2, model, "z1", false, false);
        for (var t : TOPIC_A_B.entrySet()) {
            long id = insertTopic(dsl, t.getKey(), c1);
            topicIds.put(dim + "/" + t.getKey(), id);
            // p1's assignment is a projection row: topic A is collection 1's, the row's source is collection 2
            for (String key : t.getValue()) assign(dsl, id, key.equals("p1") ? c2 : c1, model, key);
        }
        long c = insertTopic(dsl, "C", c2);
        topicIds.put(dim + "/C", c);
        assign(dsl, c, c2, model, "z1");
    }

    // ── calling the functions ────────────────────────────────────────────────

    private static Field<?> vec(int dim) {
        return DSL.val(VectorSeeds.vecXY(dim, 1f, 0f), VectorSeeds.chunkEmbedding(dim));
    }

    private long topic(int dim, String label) {
        return topicIds.get(dim + "/" + label);
    }

    private static Field<?> ids(Long... ids) {
        return DSL.val(ids, SQLDataType.BIGINT.array());
    }

    private Result<Record> callFn(String sql, QueryPart... args) {
        Table<?> t = DSL.table(sql, args);
        return scope.withTenant(TENANT, ctx -> ctx.select().from(t).fetch());
    }

    /** {@code nexus.search_topic_set_<dim>(query, ids, collection, n, model, tenant, exclude)}. */
    private Result<Record> set(int dim, Field<?> topicIds, String collection, int n, Boolean exclude) {
        return callFn("nexus.search_topic_set_" + dim + "({0}, {1}, {2}, {3}, {4}, {5}, {6})",
            vec(dim), topicIds, DSL.val(collection), DSL.val(n), DSL.val(models.get(dim)), DSL.val(TENANT),
            DSL.val(exclude, SQLDataType.BOOLEAN));
    }

    private Result<Record> single(int dim, String label, int n, Boolean exclude) {
        return callFn("nexus.search_topic_scoped_" + dim + "({0}, {1}, {2}, {3}, {4}, {5}, {6})",
            vec(dim), DSL.val(label), DSL.val(coll(dim)), DSL.val(n), DSL.val(models.get(dim)), DSL.val(TENANT),
            DSL.val(exclude, SQLDataType.BOOLEAN));
    }

    private static List<String> idsOf(Result<Record> r) {
        List<String> out = new ArrayList<>();
        for (Record rec : r) out.add(rec.get("id", String.class));
        return out;
    }

    private static List<Double> distancesOf(Result<Record> r) {
        List<Double> out = new ArrayList<>();
        for (Record rec : r) out.add(rec.get("distance", Double.class));
        return out;
    }

    // ── tests ─────────────────────────────────────────────────────────────────

    @ParameterizedTest(name = "at {0}")
    @ValueSource(ints = {384, 768, 1024})
    void aSetOfOneTopic_returnsWhatTheSingleTopicFunctionReturns(int dim) {
        for (String label : List.of("A", "B")) {
            for (Boolean exclude : new Boolean[] {false, true}) {
                var one = single(dim, label, 20, exclude);
                var many = set(dim, ids(topic(dim, label)), coll(dim), 20, exclude);
                assertThat(one).as("non-vacuity: topic %s exclude=%s has rows", label, exclude).isNotEmpty();
                if (label.equals("A")) {
                    assertThat(idsOf(one)).as("the single-topic function matches the projection row p1 (chash only)")
                        .contains(hex("p1"));
                }
                assertThat(idsOf(many)).as("topic %s exclude=%s: same ids in the same order", label, exclude)
                    .containsExactlyElementsOf(idsOf(one));
                assertThat(distancesOf(many)).as("topic %s exclude=%s: same distances", label, exclude)
                    .containsExactlyElementsOf(distancesOf(one));
                assertThat(many.getValues("content", String.class))
                    .containsExactlyElementsOf(one.getValues("content", String.class));
                assertThat(many.getValues("collection", String.class)).containsOnly(coll(dim));
            }
        }
    }

    @ParameterizedTest(name = "at {0}")
    @ValueSource(ints = {384, 768, 1024})
    void aSetOfTwoTopics_isTheUnion_deduplicated_nearestFirst(int dim) {
        var r = set(dim, ids(topic(dim, "A"), topic(dim, "B")), coll(dim), 50, true);
        // exclude=true: sA (sensitive owner) and dead (no owner) and tomb (tombstoned owner) are out; ab once
        assertThat(idsOf(r)).as("A union B, live and not sensitive, nearest first (p1 is a projection row of A)")
            .containsExactlyElementsOf(hexes("dT", "a1", "p1", "ab", "b1", "a2", "b2", "a3", "far"));
        assertThat(distancesOf(r)).isSorted();

        // the order of the ids in the array does not matter
        assertThat(idsOf(set(dim, ids(topic(dim, "B"), topic(dim, "A")), coll(dim), 50, true)))
            .containsExactlyElementsOf(idsOf(r));
        // a repeated id does not repeat a row
        assertThat(idsOf(set(dim, ids(topic(dim, "A"), topic(dim, "A"), topic(dim, "B")), coll(dim), 50, true)))
            .containsExactlyElementsOf(idsOf(r));
    }

    @ParameterizedTest(name = "at {0}")
    @ValueSource(ints = {384, 768, 1024})
    void perCollectionK_capsTheRows_andNeverPads(int dim) {
        Field<?> both = ids(topic(dim, "A"), topic(dim, "B"));
        assertThat(idsOf(set(dim, both, coll(dim), 3, true)))
            .as("p_n is the per-collection k: the nearest three of the union")
            .containsExactlyElementsOf(hexes("dT", "a1", "p1"));
        assertThat(idsOf(set(dim, both, coll(dim), 1, true))).containsExactly(hex("dT"));
        assertThat(set(dim, both, coll(dim), 1000, true)).as("k above the set returns the whole set, no padding")
            .hasSize(9);
        assertThat(set(dim, both, coll(dim), 0, true)).as("k = 0").isEmpty();
    }

    @ParameterizedTest(name = "at {0}")
    @ValueSource(ints = {384, 768, 1024})
    void sensitiveChunks_areExcludedUnlessTheFlagIsFalse_andATombstonedSensitiveOwnerDoesNotExclude(int dim) {
        Field<?> both = ids(topic(dim, "A"), topic(dim, "B"));
        assertThat(idsOf(set(dim, both, coll(dim), 50, false)))
            .as("flag false: the sensitive chunk sA comes back, so the exclusion below is not a fixture with none")
            .containsExactlyElementsOf(hexes("dT", "sA", "a1", "p1", "ab", "b1", "a2", "b2", "a3", "far"));
        assertThat(idsOf(set(dim, both, coll(dim), 50, true))).doesNotContain(hex("sA"));
        assertThat(idsOf(set(dim, both, coll(dim), 50, null)))
            .as("a NULL flag excludes, as every other family does (IS NOT FALSE)")
            .doesNotContain(hex("sA"));
        assertThat(idsOf(set(dim, both, coll(dim), 50, true)))
            .as("dT has a sensitive owner that is tombstoned and a live plain owner: it is returned")
            .contains(hex("dT"));
        // exclusion happens before the limit: k rows come back when enough non-sensitive rows exist
        assertThat(set(dim, ids(topic(dim, "A")), coll(dim), 2, true)).hasSize(2);
    }

    @ParameterizedTest(name = "at {0}")
    @ValueSource(ints = {384, 768, 1024})
    void theFunctionAppliesNoThreshold_andItsDistancesAreThePlainSearchsOwn(int dim) {
        var r = set(dim, ids(topic(dim, "A")), coll(dim), 50, true);
        assertThat(idsOf(r)).as("far (distance 1.80) comes back: a threshold is the caller's, not the function's")
            .contains(hex("far"));
        var plain = callFn("nexus.plain_search_" + dim + "({0}, {1}, {2}, {3}, {4}, {5}, {6}, {7})",
            vec(dim), DSL.val(new String[] {coll(dim)}, SQLDataType.VARCHAR.array()),
            DSL.val(null, SQLDataType.JSONB), DSL.val(null, SQLDataType.VARCHAR), DSL.val(50),
            DSL.val(models.get(dim)), DSL.val(TENANT), DSL.val(true, SQLDataType.BOOLEAN));
        Map<String, Double> plainDistance = new LinkedHashMap<>();
        for (Record rec : plain) plainDistance.put(rec.get("id", String.class), rec.get("distance", Double.class));
        for (Record rec : r) {
            assertThat(plainDistance).as("a chunk the plain search also returns").containsKey(rec.get("id", String.class));
            assertThat(rec.get("distance", Double.class)).as("the same distance as plain_search for %s", rec.get("id"))
                .isEqualTo(plainDistance.get(rec.get("id", String.class)));
        }
    }

    @ParameterizedTest(name = "at {0}")
    @ValueSource(ints = {384, 768, 1024})
    void aTopicOfAnotherCollection_neverMatches(int dim) {
        assertThat(set(dim, ids(topic(dim, "C")), coll(dim), 50, false))
            .as("topic C belongs to collection 2 and assigns z1, whose chash is also a chunk of collection 1")
            .isEmpty();
        assertThat(idsOf(set(dim, ids(topic(dim, "A"), topic(dim, "C")), coll(dim), 50, true)))
            .as("A with C: C adds nothing")
            .containsExactlyElementsOf(idsOf(set(dim, ids(topic(dim, "A")), coll(dim), 50, true)));
        assertThat(set(dim, ids(topic(dim, "A")), collB(dim), 50, false))
            .as("topic A belongs to collection 1; collection 2 holds a1 under the same chash")
            .isEmpty();
        assertThat(idsOf(set(dim, ids(topic(dim, "C")), collB(dim), 50, false)))
            .as("topic C in its own collection: z1")
            .containsExactly(hex("z1"));
    }

    @ParameterizedTest(name = "at {0}")
    @ValueSource(ints = {384, 768, 1024})
    void anEmptyOrNullSet_returnsNoRows(int dim) {
        // pins the behaviour, not a guard: the function has none, ANY of an empty array is false and of NULL is NULL
        assertThat(set(dim, DSL.val(new Long[0], SQLDataType.BIGINT.array()), coll(dim), 50, false)).isEmpty();
        assertThat(set(dim, DSL.val(null, SQLDataType.BIGINT.array()), coll(dim), 50, false)).isEmpty();
        assertThat(set(dim, ids(Long.MAX_VALUE), coll(dim), 50, false)).as("an id no topic has").isEmpty();
    }

    @ParameterizedTest(name = "at {0}")
    @ValueSource(ints = {384, 768, 1024})
    void unassignedDeadAndTombstonedChunks_areNeverReturned(int dim) {
        List<String> all = idsOf(set(dim, ids(topic(dim, "A"), topic(dim, "B")), coll(dim), 100, false));
        assertThat(all).as("u1 is assigned to no topic").doesNotContain(hex("u1"));
        assertThat(all).as("z1 is assigned only in the other collection").doesNotContain(hex("z1"));
        assertThat(all).as("dead has no owner row").doesNotContain(hex("dead"));
        assertThat(all).as("tomb's only owner is tombstoned").doesNotContain(hex("tomb"));
    }

    @Test
    void oneFunctionPerDimension_withTheGrantsAndTheSignatureTheCallerReads() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            var all = PgCatalogProbes.functionDefinitionsIn(ctx, "nexus");
            var defs = all.values().stream()
                .filter(d -> d.identity().matches("search_topic_set_(384|768|1024)\\(.*"))
                .toList();
            assertThat(defs).as("exactly one signature per dimension, no overload").hasSize(3);
            assertThat(defs.stream().map(d -> d.identity().substring(0, d.identity().indexOf('('))).toList())
                .containsExactlyInAnyOrder("search_topic_set_384", "search_topic_set_768", "search_topic_set_1024");
            for (var d : defs) {
                String fn = d.identity();
                assertThat(d.arguments()).as(fn)
                    .contains("p_topic_ids bigint[]")
                    .contains("p_collection text")
                    .contains("p_n integer")
                    .doesNotContain("threshold")
                    .endsWith("p_exclude_sensitive boolean DEFAULT false");
                // an explicit ACL: a NULL one is the schema default, which leaves EXECUTE to PUBLIC
                assertThat(d.acl()).as(fn + " has an explicit ACL").isNotNull()
                    .as(fn + " EXECUTE is not left to PUBLIC (an aclitem with an empty grantee reads =X/)")
                    .doesNotContainPattern("(^\\{|,)=X/")
                    .as(fn + " nexus_svc holds an explicit EXECUTE grant").contains("nexus_svc=X/");
                assertThat(PgCatalogProbes.canExecuteFunction(ctx, "nexus_svc",
                        "nexus." + fn.substring(0, fn.indexOf('(')) + "(nexus.vector,bigint[],text,integer,text,text,boolean)"))
                    .as(fn + " nexus_svc can execute").isTrue();
                assertThat(d.language()).as(fn).isEqualTo("sql");
                assertThat(d.volatility()).as(fn + " STABLE").isEqualTo("s");
                assertThat(d.securityDefiner()).as(fn + " SECURITY INVOKER, so row-level security applies").isFalse();
            }
        }
    }
}
