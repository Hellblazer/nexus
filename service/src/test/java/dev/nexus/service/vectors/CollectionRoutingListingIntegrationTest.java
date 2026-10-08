// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.NexusService;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.TestHttp;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.Record1;
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
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-mz9jv: the routing listing reads the registry, not the chunk rows.
 *
 * <p>The full stats route ({@code GET /v1/vectors/stats}) counts the live chunks of every collection; a router
 * (corpus resolution, sibling lookup, the collection-row cache) reads only a name and a registry row. The routing
 * listing ({@code GET /v1/vectors/stats?fields=routing}) is the same population (collections that hold a chunk),
 * the same lifecycle filter, read from {@code catalog_collections} alone (0.8 ms measured, T2
 * {@code nexus/search-latency-root-cause-2026-10-08}).
 *
 * <p>Fixture: two tenants x two models (384, 1024). Per tenant: a live collection per model, a collection whose
 * chunks are all unowned, a quarantine sibling, a rename tombstone (superseded, still 'live'), and a registered
 * collection with no chunk. Rows are bulk-inserted ({@code -Dcli.rows=N} per collection, default 4000) so that a
 * scan of the chunk rows costs something to compare against.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionRoutingListingIntegrationTest {

    static final String SVC_ROLE = "svc_clr";
    static final String SVC_PASS = "svc_clr_pass";
    static final String TOKEN = "tok-clr-routing-0123456789abcdef0000";
    static final String T1 = "clr-tenant-1";
    static final String T2 = "clr-tenant-2";
    static final int ROWS = Integer.getInteger("cli.rows", 4000);
    static final ObjectMapper MAPPER = new ObjectMapper();

    static final String LIVE_384 = "knowledge__clr-live__minilm-l6-v2-384__v1";
    static final String LIVE_1024 = "code__clr-live__voyage-code-3__v1";
    static final String UNOWNED = "knowledge__clr-unowned__voyage-context-3__v1";
    static final String QUARANTINE = "quarantine-knowledge__clr-q__minilm-l6-v2-384__v1";
    static final String TOMBSTONE = "knowledge__clr-old__minilm-l6-v2-384__v1";
    static final String EMPTY = "knowledge__clr-empty__minilm-l6-v2-384__v1";
    static final String OTHER_TENANT_ONLY = "knowledge__clr-t2only__minilm-l6-v2-384__v1";

    /** The vectors-019-5 definition (the rollback text of vectors-032-1), under a probe name. */
    static final String OLD_VIEW_DDL = """
        CREATE VIEW nexus.collection_vector_stats_v019
            WITH (security_invoker = true)
        AS
        SELECT c.tenant_id,
               c.collection,
               CASE WHEN c.embedding_384  IS NOT NULL THEN 384
                    WHEN c.embedding_768  IS NOT NULL THEN 768
                    WHEN c.embedding_1024 IS NOT NULL THEN 1024
               END AS dim,
               count(*) FILTER (WHERE EXISTS (SELECT 1 FROM nexus.chunk_live_owners(c.tenant_id, c.collection, c.chash)))          AS chunk_count,
               max(c.created_at) FILTER (WHERE EXISTS (SELECT 1 FROM nexus.chunk_live_owners(c.tenant_id, c.collection, c.chash))) AS last_write,
               count(*)                                                AS stored_count
          FROM nexus.chunks c
         GROUP BY c.tenant_id, c.collection, 3
        """;

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    PgVectorRepository repo;
    NexusService service;
    HttpClient http;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN, T1, "clr");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        Embedder none = new Embedder() {
            @Override public List<float[]> embed(List<String> texts) {
                throw new UnsupportedOperationException("the listings never embed");
            }
            @Override public void close() { }
        };
        repo = new PgVectorRepository(tenantScope, none, none);
        seed();
        try (Connection su = pg.createConnection("")) {
            // The vectors-019-5 view under another name, for the latency comparison below.
            PgContainerHelper.runSuperuserDdl(su, OLD_VIEW_DDL);
            PgContainerHelper.runSuperuserDdl(su, "GRANT SELECT ON nexus.collection_vector_stats_v019 TO PUBLIC");
        }
        service = new NexusService(0, TOKEN, svcDs, null, repo);
        service.start();
        http = TestHttp.client();
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /**
     * {@code rows} chunks of {@code coll} cloned from one seed row: chash = sha256(coll || n) for n in 1..rows, the
     * seed row's text and vector.
     */
    private static void bulk(DSLContext dsl, String tenant, String coll, String model, int dim, int rows) {
        String seedHex = Chash.ofText("seed " + coll).toHex();
        PgContainerHelper.insertChunks(dsl, tenant, coll, List.of(seedHex), List.of("seed"),
            List.of(new float[dim]), List.of(Map.of()));
        org.jooq.Table<Record1<Integer>> series = DSL.generateSeries(1, rows).as("s", "g");
        org.jooq.Field<Integer> g = series.field(0, Integer.class);
        org.jooq.Field<Vector> emb = dim == 384 ? CHUNKS.EMBEDDING_384 : CHUNKS.EMBEDDING_1024;
        dsl.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL,
                CHUNKS.CHUNK_TEXT, emb)
            .select(dsl.select(CHUNKS.TENANT_ID, CHUNKS.COLLECTION, chashOf(coll, g), CHUNKS.EMBEDDING_MODEL,
                    CHUNKS.CHUNK_TEXT, emb)
                .from(CHUNKS, series)
                .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(coll))
                .and(CHUNKS.CHASH.eq(Chash.fromHex(seedHex).toBytes())))
            .execute();
    }

    private static org.jooq.Field<byte[]> chashOf(String coll, org.jooq.Field<Integer> g) {
        return DSL.function("sha256", byte[].class,
            DSL.function("convert_to", byte[].class, DSL.concat(DSL.inline(coll), g.cast(String.class)),
                DSL.inline("UTF8")));
    }

    private void seed() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (String t : List.of(T1, T2)) {
                for (String c : List.of(LIVE_384, LIVE_1024, UNOWNED, QUARANTINE, TOMBSTONE, EMPTY)) {
                    PgContainerHelper.insertCollection(dsl, t, c);
                }
                bulk(dsl, t, LIVE_384, "minilm-l6-v2-384", 384, ROWS);
                bulk(dsl, t, LIVE_1024, "voyage-code-3", 1024, ROWS);
                bulk(dsl, t, UNOWNED, "voyage-context-3", 1024, 50);
                bulk(dsl, t, QUARANTINE, "minilm-l6-v2-384", 384, 50);
                bulk(dsl, t, TOMBSTONE, "minilm-l6-v2-384", 384, 50);
            }
            PgContainerHelper.insertCollection(dsl, T2, OTHER_TENANT_ONLY);
            bulk(dsl, T2, OTHER_TENANT_ONLY, "minilm-l6-v2-384", 384, 10);
            // A rename tombstone: still 'live', superseded by the new name.
            dsl.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, LIVE_384)
                .where(CATALOG_COLLECTIONS.NAME.eq(TOMBSTONE)).execute();
            // Own the first 20 chunks of each live collection so the stats view has live counts.
            for (String t : List.of(T1, T2)) {
                for (String[] c : new String[][] {{LIVE_384, "minilm-l6-v2-384"}, {LIVE_1024, "voyage-code-3"}}) {
                    String doc = "own-" + c[0];
                    dsl.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                            CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
                        .values(t, doc, "o", c[0]).execute();
                    org.jooq.Table<Record1<Integer>> series = DSL.generateSeries(1, 20).as("s", "g");
                    org.jooq.Field<Integer> g = series.field(0, Integer.class);
                    dsl.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
                            CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION,
                            CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION,
                            CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL)
                        .select(dsl.select(DSL.inline(t), DSL.inline(doc), g, chashOf(c[0], g), DSL.inline(c[0]),
                                DSL.inline(c[1]))
                            .from(series))
                        .execute();
                }
            }
            // As in production: the service role has MAINTAIN and every leaf carries the parent's privileges.
            PgContainerHelper.runSuperuserDdl(su, "GRANT MAINTAIN ON nexus.chunks TO " + SVC_ROLE);
            @SuppressWarnings("deprecation")
            int synced = dev.nexus.service.jooq.nexus.Routines.partitionSyncAccess(dsl.configuration(),
                "nexus.chunks", true);
            assertThat(synced).as("the leaves took the parent's privileges").isPositive();
        }
        var vacuumed = tenantScope.vacuumAnalyze(List.of("nexus.chunks"));
        assertThat(vacuumed.get("nexus.chunks").vacuumed()).as("vacuumed: %s", vacuumed).isTrue();
    }

    // ── routing listing (nexus-mz9jv) ─────────────────────────────────────────

    private static Map<String, Map<String, Object>> byName(List<Map<String, Object>> rows) {
        Map<String, Map<String, Object>> out = new LinkedHashMap<>();
        for (var r : rows) out.put((String) r.get("name"), r);
        return out;
    }

    @Test
    void routing_isTheStatsPopulationAndAttributes_forEveryLifecycleFilter() {
        for (String filter : new String[] {null, "live", "quarantine", "dormant", "disputed"}) {
            var stats = byName(repo.collectionStats(T1, filter));
            var routing = byName(repo.collectionRouting(T1, filter));
            assertThat(routing.keySet()).as("filter %s: same collections as the stats route", filter)
                .containsExactlyElementsOf(stats.keySet());
            for (var e : routing.entrySet()) {
                Map<String, Object> s = stats.get(e.getKey());
                for (String key : List.of("content_type", "owner_id", "embedding_model", "lifecycle_state",
                                          "superseded_by")) {
                    assertThat(e.getValue().get(key)).as("filter %s, %s.%s", filter, e.getKey(), key)
                        .isEqualTo(s.get(key));
                }
                assertThat(e.getValue().keySet()).as("a routing row carries no liveness figure")
                    .doesNotContain("dim", "count", "stored_count", "last_write");
            }
        }
        // Non-vacuity: the filters select different things.
        assertThat(repo.collectionRouting(T1, null)).extracting(r -> r.get("name"))
            .containsExactly(LIVE_1024, LIVE_384, TOMBSTONE, UNOWNED, QUARANTINE);
        assertThat(repo.collectionRouting(T1, "live")).extracting(r -> r.get("name"))
            .as("live excludes the quarantine sibling and the rename tombstone")
            .containsExactly(LIVE_1024, LIVE_384, UNOWNED);
        assertThat(repo.collectionRouting(T1, "quarantine")).extracting(r -> r.get("name"))
            .containsExactly(QUARANTINE);
        assertThat(byName(repo.collectionRouting(T1, null)).get(TOMBSTONE).get("superseded_by")).isEqualTo(LIVE_384);
    }

    private HttpResponse<String> get(String path) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + TOKEN).GET().build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    @Test
    void theStatsRoute_servesRoutingWhenAsked_andTheFullRowsOtherwise() throws Exception {
        var routing = get("/v1/vectors/stats?fields=routing&lifecycle_state=live");
        assertThat(routing.statusCode()).as(routing.body()).isEqualTo(200);
        JsonNode rows = MAPPER.readTree(routing.body());
        List<String> names = new ArrayList<>();
        for (JsonNode r : rows) {
            names.add(r.get("name").asText());
            assertThat(r.has("count")).as("routing rows carry no count").isFalse();
            assertThat(r.has("lifecycle_state")).isTrue();
        }
        assertThat(names).containsExactly(LIVE_1024, LIVE_384, UNOWNED);

        var full = get("/v1/vectors/stats?lifecycle_state=live");
        assertThat(full.statusCode()).isEqualTo(200);
        JsonNode fullRows = MAPPER.readTree(full.body());
        assertThat(fullRows.get(0).has("count")).as("the default route is the full stats").isTrue();
        assertThat(fullRows.get(0).has("dim")).isTrue();

        var bad = get("/v1/vectors/stats?fields=everything");
        assertThat(bad.statusCode()).isEqualTo(400);
        assertThat(get("/v1/vectors/stats?fields=routing&lifecycle_state=nonsense").statusCode()).isEqualTo(400);
    }

    // ── latency, printed (bounds nothing; run with -Dcli.rows=N to size it) ───────────────────────

    private long statsMicros(String view, boolean jitOff) {
        return tenantScope.withTenant(T1, ctx -> {
            if (jitOff) {
                dev.nexus.service.db.PgSession.disableJit(ctx);
            }
            long t0 = System.nanoTime();
            int n = ctx.selectFrom(DSL.table(DSL.name("nexus", view))).fetch().size();
            long us = (System.nanoTime() - t0) / 1_000L;
            assertThat(n).isPositive();
            return us;
        });
    }

    private static long median(long[] us) {
        java.util.Arrays.sort(us);
        return us[us.length / 2];
    }

    @Test
    void latency_previousViewVersusSetBasedViewVersusRouting_printed() {
        int runs = 9;
        String[] labels = {"v019 jit=on", "v019 jit=off", "set-based jit=on", "set-based jit=off"};
        String[] views = {"collection_vector_stats_v019", "collection_vector_stats_v019",
                          "collection_vector_stats", "collection_vector_stats"};
        boolean[] jitOff = {false, true, false, true};
        StringBuilder out = new StringBuilder("stats latency rows_per_collection=" + ROWS + " (tenant chunks ~"
            + (2 * ROWS + 150) + "), median of " + runs + ":");
        for (int v = 0; v < labels.length; v++) {
            long[] us = new long[runs];
            for (int i = 0; i < runs; i++) {
                us[i] = statsMicros(views[v], jitOff[v]);
            }
            out.append(' ').append(labels[v]).append('=').append(median(us) / 1000.0).append("ms");
        }
        long[] us = new long[runs];
        for (int i = 0; i < runs; i++) {
            long t0 = System.nanoTime();
            repo.collectionRouting(T1, null);
            us[i] = (System.nanoTime() - t0) / 1_000L;
        }
        out.append(" routing=").append(median(us) / 1000.0).append("ms");
        long[] listUs = new long[runs];
        for (int i = 0; i < runs; i++) {
            long t0 = System.nanoTime();
            repo.listCollections(T1);
            listUs[i] = (System.nanoTime() - t0) / 1_000L;
        }
        out.append(" listCollections=").append(median(listUs) / 1000.0).append("ms");
        System.out.println(out);
    }
}
