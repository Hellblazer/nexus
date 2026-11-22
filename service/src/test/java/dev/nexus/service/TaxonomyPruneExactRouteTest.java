// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CollectionIds;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.EmbedResult;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-p2m1x.9 (RDR-229 P2.4, engine half): the exact-name mode of
 * {@code POST /v1/taxonomy/assignments/prune_projection}.
 *
 * <p>The route deleted by {@code SOURCE_COLLECTION startsWith(prefix)}. With a per-collection threshold,
 * pruning {@code ..__v1} also reached {@code ..__v10}, and pruning {@code docs__art} reached {@code docs__art-x},
 * deleting the longer name's rows below the SHORTER name's threshold. {@code match: "exact"} deletes only the named
 * collection; {@code "prefix"}, and an absent field, keep the old behaviour; anything else is a 400. The engine
 * advertises the mode as {@code taxonomy_prune_exact_supported} on {@code /version}, the only way a client can tell
 * an older engine that ignored the field from one that honoured it.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TaxonomyPruneExactRouteTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String TOKEN = "tok-p2m1x9-0123456789abcdef0123456789abcdef";
    private static final String TENANT = "p2m1x9-prune";
    private static final String MODEL = "minilm-l6-v2-384";
    private static final OffsetDateTime SEED_TS = OffsetDateTime.parse("2026-01-01T00:00:00+00:00");
    private static final String ROUTE = "/v1/taxonomy/assignments/prune_projection";
    private static final double WEAK = 0.31;
    private static final double STRONG = 0.91;
    private static final double FLOOR = 0.70;

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    NexusService service;
    HttpClient http;
    private final AtomicInteger seq = new AtomicInteger();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "p2m1x9-test");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(6);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        var embedder = new FakeEmbedder();
        var scope = new TenantScope(svcDs);
        var vectors = new PgVectorRepository(scope, embedder, embedder);
        service = new NexusService(0, TOKEN, svcDs, new EmbedderRouter(embedder, "document"), vectors);
        service.start();
        http = TestHttp.client();
        // Burn the per-tenant ghost sweep before registering collections (see OwnerlessWriteRefusalTest).
        http.send(TestHttp.request(url("/v1/catalog/collections/list"))
            .header("Authorization", "Bearer " + TOKEN).GET().build(), HttpResponse.BodyHandlers.ofString());
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── fixture ───────────────────────────────────────────────────────────────

    private String url(String path) {
        return "http://127.0.0.1:" + service.getPort() + path;
    }

    private static String chash(String seed) {
        return Chash.ofText(seed).toHex();
    }

    /** A fresh stem so no test sees another's rows; the pair is {@code stem} and {@code stem + suffix}. */
    private String stem(String base) {
        // Fixed width: a stem must never be a prefix of another test's stem (art1 / art10).
        return base + String.format("%03d", seq.incrementAndGet());
    }

    private long topic;

    private void ensureTopic() throws Exception {
        if (topic != 0) return;
        String home = "knowledge__p2m1x9-home__" + MODEL + "__v1";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, TENANT, home);
            topic = dsl.insertInto(TOPICS)
                .set(TOPICS.TENANT_ID, TENANT).set(TOPICS.LABEL, "prune topic").set(TOPICS.COLLECTION, home)
                .set(TOPICS.COLLECTION_ID, CollectionIds.idOf(TENANT, home))   // RDR-229 P3.1: the id beside the name
                .set(TOPICS.CREATED_AT, SEED_TS)
                .returningResult(TOPICS.ID).fetchOne(TOPICS.ID);
        }
    }

    /**
     * Register {@code name} and give it one weak and one strong projection row, so a prune at {@link #FLOOR}
     * removes exactly the weak one wherever it reaches.
     */
    private void collection(String name) throws Exception {
        ensureTopic();
        // A conformant name carries its model; a bare one takes the registry's 768-wide fallback.
        int dim = name.contains(MODEL) ? 384 : 768;
        String weak = chash(name + "/weak");
        String strong = chash(name + "/strong");
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, TENANT, name);
            PgContainerHelper.insertOwnedChunks(dsl, TENANT, name, dim, weak, strong);
            String model = PgContainerHelper.collectionModel(dsl, TENANT, name);
            assign(dsl, name, weak, WEAK, model);
            assign(dsl, name, strong, STRONG, model);
        }
    }

    private void assign(org.jooq.DSLContext dsl, String name, String hex, double similarity, String model) {
        dsl.insertInto(TOPIC_ASSIGNMENTS)
            .set(TOPIC_ASSIGNMENTS.TENANT_ID, TENANT).set(TOPIC_ASSIGNMENTS.DOC_ID, HexFormat.of().parseHex(hex))
            .set(TOPIC_ASSIGNMENTS.TOPIC_ID, topic).set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, name)
            .set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID, CollectionIds.idOf(TENANT, name))   // RDR-229 P3.1: the id beside the name
            .set(TOPIC_ASSIGNMENTS.ASSIGNED_BY, "projection").set(TOPIC_ASSIGNMENTS.SIMILARITY, similarity)
            .set(TOPIC_ASSIGNMENTS.ASSIGNED_AT, SEED_TS).set(TOPIC_ASSIGNMENTS.EMBEDDING_MODEL, model)
            .execute();
    }

    private int rows(String name) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).fetchCount(TOPIC_ASSIGNMENTS,
                TOPIC_ASSIGNMENTS.TENANT_ID.eq(TENANT).and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.eq(name)));
        }
    }

    private HttpResponse<String> prune(Map<String, Object> body) throws Exception {
        var req = TestHttp.request(url(ROUTE))
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> prune(String name, String match) throws Exception {
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("source_collection_prefix", name);
        body.put("min_similarity", FLOOR);
        if (match != null) body.put("match", match);
        return prune(body);
    }

    private int removed(HttpResponse<String> resp) throws Exception {
        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(200);
        return MAPPER.readTree(resp.body()).get("removed").asInt();
    }

    // ── the v1 / v10 pair ─────────────────────────────────────────────────────

    @Test
    void exact_prunesTheShorterVersionName_andLeavesTheLongerVersionUntouched() throws Exception {
        String s = stem("docs__ver");
        String v1 = s + "__" + MODEL + "__v1";
        String v10 = s + "__" + MODEL + "__v10";
        collection(v1);
        collection(v10);

        assertThat(removed(prune(v1, "exact"))).as("only v1's weak row").isEqualTo(1);

        assertThat(rows(v1)).as("v1 lost its weak row, kept its strong one").isEqualTo(1);
        assertThat(rows(v10)).as("v10 is a different collection: both rows survive").isEqualTo(2);
    }

    @Test
    void prefix_stillReachesTheLongerVersionName() throws Exception {
        String s = stem("docs__ver");
        String v1 = s + "__" + MODEL + "__v1";
        String v10 = s + "__" + MODEL + "__v10";
        collection(v1);
        collection(v10);

        assertThat(removed(prune(v1, "prefix"))).as("today's behaviour: both weak rows").isEqualTo(2);

        assertThat(rows(v1)).isEqualTo(1);
        assertThat(rows(v10)).isEqualTo(1);
    }

    // ── the docs__art / docs__art-x pair ──────────────────────────────────────

    @Test
    void exact_prunesTheBareName_andLeavesTheHyphenatedLongerNameUntouched() throws Exception {
        String art = stem("docs__art");
        String artX = art + "-x";
        collection(art);
        collection(artX);

        assertThat(removed(prune(art, "exact"))).isEqualTo(1);

        assertThat(rows(art)).isEqualTo(1);
        assertThat(rows(artX)).as("docs__art-x is not docs__art").isEqualTo(2);
    }

    @Test
    void prefix_stillReachesTheHyphenatedLongerName() throws Exception {
        String art = stem("docs__art");
        String artX = art + "-x";
        collection(art);
        collection(artX);

        assertThat(removed(prune(art, "prefix"))).isEqualTo(2);

        assertThat(rows(art)).isEqualTo(1);
        assertThat(rows(artX)).isEqualTo(1);
    }

    // ── the field's contract ──────────────────────────────────────────────────

    @Test
    void anAbsentMatchField_behavesAsPrefix() throws Exception {
        String art = stem("docs__art");
        String artX = art + "-x";
        collection(art);
        collection(artX);

        assertThat(removed(prune(art, null))).as("an old client sees no change").isEqualTo(2);

        assertThat(rows(art)).isEqualTo(1);
        assertThat(rows(artX)).isEqualTo(1);
    }

    @Test
    void aNullMatchField_behavesAsAbsent_whichIsPrefix() throws Exception {
        String art = stem("docs__art");
        String artX = art + "-x";
        collection(art);
        collection(artX);
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("source_collection_prefix", art);
        body.put("min_similarity", FLOOR);
        body.put("match", null);

        // A longer-named pair, so null read as "exact" (removed 1) is told apart from null read as prefix (2).
        assertThat(removed(prune(body))).as("null is prefix: both weak rows go").isEqualTo(2);

        assertThat(rows(art)).isEqualTo(1);
        assertThat(rows(artX)).isEqualTo(1);
    }

    // ── the reply echoes the mode the engine applied ──────────────────────────

    private String echoed(HttpResponse<String> resp) throws Exception {
        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(200);
        JsonNode match = MAPPER.readTree(resp.body()).get("match");
        assertThat(match).as("the reply carries match: %s", resp.body()).isNotNull();
        return match.asText();
    }

    @Test
    void theReplyEchoesExact_soAClientCanConfirmTheEngineHonouredIt() throws Exception {
        String art = stem("docs__art");
        collection(art);
        assertThat(echoed(prune(art, "exact"))).isEqualTo("exact");
    }

    @Test
    void theReplyEchoesPrefix_forAnExplicitAnAbsentAndANullMatch() throws Exception {
        String art = stem("docs__art");
        collection(art);
        assertThat(echoed(prune(art, "prefix"))).isEqualTo("prefix");
        assertThat(echoed(prune(art, null))).as("absent: an old client's reply names prefix").isEqualTo("prefix");
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("source_collection_prefix", art);
        body.put("min_similarity", FLOOR);
        body.put("match", null);
        assertThat(echoed(prune(body))).as("null").isEqualTo("prefix");
    }

    @Test
    void anUnknownMatchValue_isRefusedWithA400_andDeletesNothing() throws Exception {
        String art = stem("docs__art");
        collection(art);

        for (String bad : List.of("Exact", "EXACT", "fuzzy", "", "regex")) {
            var resp = prune(art, bad);
            assertThat(resp.statusCode()).as("match=%s body: %s", bad, resp.body()).isEqualTo(400);
            assertThat(MAPPER.readTree(resp.body()).get("error").asText()).contains("match");
        }
        var notAString = new java.util.LinkedHashMap<String, Object>();
        notAString.put("source_collection_prefix", art);
        notAString.put("min_similarity", FLOOR);
        notAString.put("match", 5);
        assertThat(prune(notAString).statusCode()).as("a non-string match is refused too").isEqualTo(400);

        assertThat(rows(art)).as("a refused request deletes nothing").isEqualTo(2);
    }

    /**
     * The client's verification prune, sent before it trusts exact mode: a sentinel name that no collection carries,
     * exact, at 0.0. The route puts no collection-name grammar on {@code source_collection_prefix}, so the sentinel
     * is accepted; it answers removed 0 with the echo, and deletes nothing even with rows present.
     */
    @Test
    void theClientsSentinelProbe_isAcceptedAnswersRemovedZeroWithTheEcho_andDeletesNothing() throws Exception {
        String art = stem("docs__art");
        collection(art);
        String sentinel = "nexus-exact-probe-" + java.util.UUID.randomUUID().toString().replace("-", "");
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("source_collection_prefix", sentinel);
        body.put("min_similarity", 0.0);
        body.put("match", "exact");

        var resp = prune(body);

        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(200);
        JsonNode reply = MAPPER.readTree(resp.body());
        assertThat(reply.get("removed").asInt()).isZero();
        assertThat(reply.get("match").asText()).isEqualTo("exact");
        assertThat(rows(art)).as("rows exist and none was touched").isEqualTo(2);
    }

    // ── the capability flag ───────────────────────────────────────────────────

    @Test
    void version_advertisesExactPruneSupport() throws Exception {
        var resp = http.send(TestHttp.request(url("/version")).GET().build(), HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(200);
        JsonNode body = MAPPER.readTree(resp.body());
        assertThat(body.get("taxonomy_prune_exact_supported")).as("flag present").isNotNull();
        assertThat(body.get("taxonomy_prune_exact_supported").isBoolean()).isTrue();
        assertThat(body.get("taxonomy_prune_exact_supported").asBoolean()).isTrue();
    }

    private static final class FakeEmbedder implements Embedder {
        @Override
        public List<float[]> embed(List<String> texts) {
            List<float[]> out = new ArrayList<>(texts.size());
            for (String t : texts) {
                float[] v = new float[384];
                v[Math.floorMod(t.hashCode(), 384)] = 1.0f;
                out.add(v);
            }
            return out;
        }

        @Override
        public EmbedResult embedWithUsage(List<String> texts) {
            return new EmbedResult(embed(texts), texts.size());
        }

        @Override
        public String modelToken() {
            return MODEL;
        }
    }
}
