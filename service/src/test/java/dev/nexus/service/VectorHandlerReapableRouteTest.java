// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgVectorRepositoryContractTest.FakeEmbedder;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-192 Step 8 (bead nexus-wbfpw.17): {@code POST /v1/vectors/reapable}, the read-only
 * listing of the chunks reapable(c) selects in one collection. Over real HTTP, through the
 * RLS-subject service role, with fixture chunks built by substrate SQL (the write routes refuse an
 * ownerless write from RDR-223 Phase 3).
 *
 * <p>The listing's selection is {@code nexus.chunk_is_reapable} and nothing else, so the S1a rows
 * (R1 to R9, aged past the default grace) must list exactly the rows whose REAP cell is true:
 * R1, R4, R6 and R8. The rest pins what the route adds: the response shape, grace_seconds, paging
 * at 300, tenant isolation, every collection prefix, the quarantine refusal, and that it writes
 * nothing.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorHandlerReapableRouteTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final String TOKEN_A = "tok-reap-tenant-a-0123456789abcdef0000";
    private static final String TOKEN_B = "tok-reap-tenant-b-0123456789abcdef0000";
    private static final String SVC_ROLE = "svc_reap_route";
    private static final String SVC_PASS = "svc_reap_route_pass";
    private static final String TENANT_A = "reap-route-a";
    private static final String TENANT_B = "reap-route-b";

    private static final String COL_A = "knowledge__reaproute-a__minilm-l6-v2-384__v1";
    private static final String COL_B = "knowledge__reaproute-b__minilm-l6-v2-384__v1";

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private NexusService service;
    private HttpClient http;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN_A, TENANT_A, "reap-a");
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN_B, TENANT_B, "reap-b");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        FakeEmbedder embedder = new FakeEmbedder(384);
        var repo = new PgVectorRepository(new TenantScope(svcDs), embedder, embedder);
        service = new NexusService(0, TOKEN_A, svcDs, null, repo);
        service.start();
        http = HttpClient.newHttpClient();

        // Burn each tenant's first-request ghost sweep before registering collections
        // (see VectorHandlerUpsertReferenceOnlyTest's identical bootstrap).
        for (String token : List.of(TOKEN_A, TOKEN_B)) {
            http.send(HttpRequest.newBuilder()
                .uri(URI.create("http://127.0.0.1:" + service.getPort() + "/v1/catalog/collections/list"))
                .header("Authorization", "Bearer " + token).GET().build(), HttpResponse.BodyHandlers.ofString());
        }
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── http ────────────────────────────────────────────────────────────────

    private HttpResponse<String> post(String token, String path, Object body) throws Exception {
        return http.send(HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + token)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))).build(),
            HttpResponse.BodyHandlers.ofString());
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> json(HttpResponse<String> r) throws Exception {
        return MAPPER.readValue(r.body(), Map.class);
    }

    @SuppressWarnings("unchecked")
    private List<Map<String, Object>> chunks(Map<String, Object> body) {
        return (List<Map<String, Object>>) body.get("chunks");
    }

    private Set<String> chashes(Map<String, Object> body) {
        Set<String> out = new HashSet<>();
        for (var c : chunks(body)) out.add((String) c.get("chash"));
        return out;
    }

    private Map<String, Object> reapable(String token, String collection, Map<String, Object> extra) throws Exception {
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("collection", collection);
        body.putAll(extra);
        HttpResponse<String> r = post(token, "/v1/vectors/reapable", body);
        assertThat(r.statusCode()).as("body: " + r.body()).isEqualTo(200);
        return json(r);
    }

    // ── fixture (substrate SQL) ──────────────────────────────────────────────

    private static String ch(String seed) {
        return Chash.ofText(seed).toHex();
    }

    private static byte[] bytes(String hex) {
        return Chash.fromHex(hex).toBytes();
    }

    private void su(java.util.function.Consumer<DSLContext> work) throws Exception {
        try (Connection c = pg.createConnection("")) {
            work.accept(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    private void register(String tenant, String collection) throws Exception {
        su(ctx -> PgContainerHelper.insertCollection(ctx, tenant, collection));
    }

    /** One chunk in {@code collection}, last written {@code age} ago. */
    private String chunk(String tenant, String collection, String seed, Duration age, Map<String, Object> meta)
            throws Exception {
        String hex = ch(tenant + "/" + seed);
        su(ctx -> {
            PgContainerHelper.insertChunks(ctx, tenant, collection, List.of(hex), List.of(seed + " text"),
                List.of(new float[384]), List.of(meta));
            OffsetDateTime then = OffsetDateTime.now().minus(age);
            ctx.update(CHUNKS).set(CHUNKS.CREATED_AT, then).set(CHUNKS.LAST_WRITTEN_AT, then)
               .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection))
                      .and(CHUNKS.CHASH.eq(bytes(hex)))).execute();
        });
        return hex;
    }

    private void doc(String tenant, String tumbler, String collection, boolean tombstoned, String noteChashHex)
            throws Exception {
        su(ctx -> ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.DELETED_AT,
                CATALOG_DOCUMENTS.METADATA)
            .values(tenant, tumbler, "doc " + tumbler, collection,
                tombstoned ? OffsetDateTime.now().minusDays(1) : null,
                noteChashHex == null ? null : org.jooq.JSONB.jsonb("{\"doc_id\":\"" + noteChashHex + "\"}"))
            .onConflictDoNothing().execute());
    }

    private void manifest(String tenant, String docId, String collection, String hex) throws Exception {
        su(ctx -> ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
                CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                CATALOG_DOCUMENT_CHUNKS.COLLECTION)
            .values(tenant, docId, 0, bytes(hex), collection).onConflictDoNothing().execute());
    }

    /** The S1a rows R1 to R9 (docs/rdr/rdr-192, Step 1), in tenant A's COL_A and COL_B, aged 40 days. */
    private record S1a(String r1, String r2, String r3, String r4, String r5, String r6, String r7, String r8,
                       String r9) { }

    private S1a seedS1a(String tenant) throws Exception {
        register(tenant, COL_A);
        register(tenant, COL_B);
        Duration old = Duration.ofDays(40);
        String r1 = chunk(tenant, COL_A, "r1", old, Map.of("title", "r1.txt:1-1"));
        String r2 = chunk(tenant, COL_A, "r2", old, Map.of());
        doc(tenant, "d2", COL_A, false, null);
        manifest(tenant, "d2", COL_A, r2);
        String r3 = chunk(tenant, COL_A, "r3", old, Map.of());
        doc(tenant, "d3", COL_A, true, null);
        manifest(tenant, "d3", COL_A, r3);
        String r4 = chunk(tenant, COL_A, "r4", old, Map.of());
        chunk(tenant, COL_B, "r4", old, Map.of());   // the same seed is the same chash in B
        doc(tenant, "d4", COL_B, false, null);
        manifest(tenant, "d4", COL_B, r4);
        String r5 = chunk(tenant, COL_A, "r5", old, Map.of());
        doc(tenant, "d5a", COL_A, false, null);
        manifest(tenant, "d5a", COL_A, r5);
        String r6 = chunk(tenant, COL_A, "r6", old, Map.of("catalog_doc_id", "d6"));
        chunk(tenant, COL_B, "r6", old, Map.of());
        doc(tenant, "d6", COL_B, false, null);
        manifest(tenant, "d6", COL_B, r6);
        String r7 = chunk(tenant, COL_A, "r7", old, Map.of());
        doc(tenant, "d7note", COL_A, false, null);
        manifest(tenant, "d7note", COL_A, r7);
        String r8 = chunk(tenant, COL_A, "r8", old, Map.of("title", "legacy note"));
        doc(tenant, "d8note", COL_A, false, r8);
        String r9 = chunk(tenant, COL_A, "r9", old, Map.of());
        chunk(tenant, COL_B, "r9", old, Map.of());
        doc(tenant, "d9a", COL_A, true, null);
        doc(tenant, "d9b", COL_B, false, null);
        manifest(tenant, "d9a", COL_A, r9);
        manifest(tenant, "d9b", COL_B, r9);
        return new S1a(r1, r2, r3, r4, r5, r6, r7, r8, r9);
    }

    private long chunkRows(String tenant) throws Exception {
        long[] n = new long[1];
        su(ctx -> n[0] = ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant)));
        return n[0];
    }

    // ── the S1a rows ──────────────────────────────────────────────────────────

    @Test
    void listsExactlyTheS1aRowsWhoseReapableCellIsTrue() throws Exception {
        S1a fx = seedS1a(TENANT_A);

        Map<String, Object> body = reapable(TOKEN_A, COL_A, Map.of());

        assertThat(chashes(body)).as("R1, R4, R6, R8: no own-collection manifest row in any state, aged")
            .containsExactlyInAnyOrder(fx.r1(), fx.r4(), fx.r6(), fx.r8());
        assertThat(body.get("collection")).isEqualTo(COL_A);
        assertThat(body.get("returned")).isEqualTo(4);
        assertThat(body.get("grace_seconds")).as("null means the engine default (30 days)").isNull();
    }

    @Test
    void eachItemCarriesChashCreatedAtLastWrittenAtTitleAndCatalogDocId() throws Exception {
        S1a fx = seedS1a(TENANT_A);

        Map<String, Object> body = reapable(TOKEN_A, COL_A, Map.of());

        Map<String, Object> r1 = chunks(body).stream().filter(c -> fx.r1().equals(c.get("chash"))).findFirst().get();
        assertThat(r1.get("title")).isEqualTo("r1.txt:1-1");
        assertThat(r1.get("catalog_doc_id")).as("R1 names no document").isNull();
        assertThat(OffsetDateTime.parse((String) r1.get("created_at"))).isBefore(OffsetDateTime.now().minusDays(30));
        assertThat(OffsetDateTime.parse((String) r1.get("last_written_at")))
            .isBefore(OffsetDateTime.now().minusDays(30));
        Map<String, Object> r6 = chunks(body).stream().filter(c -> fx.r6().equals(c.get("chash"))).findFirst().get();
        assertThat(r6.get("catalog_doc_id")).as("R6 names its (other-collection) owner").isEqualTo("d6");
        assertThat(r6.get("title")).isNull();
    }

    @Test
    void theListingIsAscendingByChash() throws Exception {
        seedS1a(TENANT_A);

        List<String> order = new ArrayList<>();
        for (var c : chunks(reapable(TOKEN_A, COL_A, Map.of()))) order.add((String) c.get("chash"));

        assertThat(order).isSorted();
    }

    @Test
    void theListingWritesNothing() throws Exception {
        seedS1a(TENANT_A);
        long before = chunkRows(TENANT_A);

        reapable(TOKEN_A, COL_A, Map.of());
        reapable(TOKEN_A, COL_A, Map.of("grace_seconds", 0));

        assertThat(chunkRows(TENANT_A)).as("read-only: no chunk is moved or deleted").isEqualTo(before);
    }

    // ── grace_seconds ────────────────────────────────────────────────────────

    @Test
    void graceSecondsIsHonoured_andEchoed() throws Exception {
        String t = TENANT_A;
        String col = "knowledge__reaproute-grace__minilm-l6-v2-384__v1";
        register(t, col);
        String twoHours = chunk(t, col, "two-hours", Duration.ofHours(2), Map.of());
        String fresh = chunk(t, col, "fresh", Duration.ZERO, Map.of());

        assertThat(chashes(reapable(TOKEN_A, col, Map.of()))).as("default 30 days: nothing").isEmpty();
        var oneHour = reapable(TOKEN_A, col, Map.of("grace_seconds", 3600));
        assertThat(chashes(oneHour)).containsExactly(twoHours);
        assertThat(oneHour.get("grace_seconds")).isEqualTo(3600);
        assertThat(chashes(reapable(TOKEN_A, col, Map.of("grace_seconds", 3 * 3600)))).isEmpty();
        assertThat(chashes(reapable(TOKEN_A, col, Map.of("grace_seconds", 0)))).containsExactlyInAnyOrder(twoHours,
            fresh);
    }

    @Test
    void anInFlightIndexRunKeepsItsChunksOffTheList() throws Exception {
        String t = TENANT_A;
        String col = "docs__reaproute-pin__minilm-l6-v2-384__v1";
        register(t, col);
        su(ctx -> ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.INDEX_STATE,
                CATALOG_DOCUMENTS.INDEX_STARTED_AT)
            .values(t, "pin-run", "run", col, "indexing", OffsetDateTime.now().minusHours(1)).execute());
        chunk(t, col, "pinned", Duration.ofDays(40), Map.of("catalog_doc_id", "pin-run"));
        String free = chunk(t, col, "free", Duration.ofDays(40), Map.of());

        assertThat(chashes(reapable(TOKEN_A, col, Map.of()))).containsExactly(free);
    }

    // ── paging at 300 ────────────────────────────────────────────────────────

    @Test
    void pagesAtThreeHundred_clampsLargerLimits_andCoversEveryRowOnce() throws Exception {
        String t = TENANT_A;
        String col = "code__reaproute-paging__minilm-l6-v2-384__v1";
        register(t, col);
        List<String> hashes = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<float[]> vecs = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < 650; i++) {
            hashes.add(ch("paging-" + i));
            texts.add("t" + i);
            vecs.add(new float[384]);
            metas.add(Map.of());
        }
        su(ctx -> {
            PgContainerHelper.insertChunks(ctx, t, col, hashes, texts, vecs, metas);
            OffsetDateTime then = OffsetDateTime.now().minusDays(40);
            ctx.update(CHUNKS).set(CHUNKS.LAST_WRITTEN_AT, then)
               .where(CHUNKS.TENANT_ID.eq(t).and(CHUNKS.COLLECTION.eq(col))).execute();
        });

        Set<String> seen = new HashSet<>();
        int[] pageSizes = new int[3];
        for (int p = 0; p < 3; p++) {
            Map<String, Object> page = reapable(TOKEN_A, col, Map.of("limit", 300, "offset", p * 300));
            pageSizes[p] = chunks(page).size();
            assertThat(page.get("returned")).isEqualTo(pageSizes[p]);
            seen.addAll(chashes(page));
        }
        assertThat(pageSizes).containsExactly(300, 300, 50);
        assertThat(seen).as("every row exactly once across the pages").hasSize(650);

        assertThat(chunks(reapable(TOKEN_A, col, Map.of("limit", 1000)))).as("limit above 300 clamps").hasSize(300);
        assertThat(chunks(reapable(TOKEN_A, col, Map.of()))).as("default page is 100").hasSize(100);
        assertThat(chunks(reapable(TOKEN_A, col, Map.of("limit", 0)))).as("limit below 1 clamps up").hasSize(1);
    }

    // ── every prefix, tenant isolation, refusals ──────────────────────────────

    @Test
    void everyContentTypePrefixIsListed() throws Exception {
        for (String prefix : List.of("knowledge", "docs", "code", "rdr")) {
            String col = prefix + "__reaproute-prefix__minilm-l6-v2-384__v1";
            register(TENANT_A, col);
            String hex = chunk(TENANT_A, col, "p", Duration.ofDays(40), Map.of());
            assertThat(chashes(reapable(TOKEN_A, col, Map.of()))).as("%s__", prefix).containsExactly(hex);
        }
    }

    @Test
    void anotherTenantSeesNoneOfIt() throws Exception {
        seedS1a(TENANT_A);
        register(TENANT_B, COL_A);

        assertThat(chunks(reapable(TOKEN_B, COL_A, Map.of()))).isEmpty();
        assertThat(reapable(TOKEN_B, COL_A, Map.of()).get("returned")).isEqualTo(0);
    }

    @Test
    void aQuarantineCollectionIsRefused() throws Exception {
        HttpResponse<String> r = post(TOKEN_A, "/v1/vectors/reapable",
            Map.of("collection", "quarantine-knowledge__x__minilm-l6-v2-384__v1"));
        assertThat(r.statusCode()).isEqualTo(400);
        assertThat(r.body()).contains("quarantine");
    }

    @Test
    void badRequestsAreRefused() throws Exception {
        assertThat(post(TOKEN_A, "/v1/vectors/reapable", Map.of()).statusCode())
            .as("collection is required").isEqualTo(400);
        assertThat(post(TOKEN_A, "/v1/vectors/reapable", Map.of("collection", COL_A, "grace_seconds", -1))
            .statusCode()).as("a negative grace").isEqualTo(400);
        assertThat(post(TOKEN_A, "/v1/vectors/reapable", Map.of("collection", COL_A, "grace_seconds", "soon"))
            .statusCode()).as("a non-numeric grace").isEqualTo(400);
        var get = http.send(HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + "/v1/vectors/reapable"))
            .header("Authorization", "Bearer " + TOKEN_A).GET().build(), HttpResponse.BodyHandlers.ofString());
        assertThat(get.statusCode()).as("POST only").isEqualTo(405);
    }

    @Test
    void anUnknownCollectionListsNothing() throws Exception {
        Map<String, Object> body = reapable(TOKEN_A, "knowledge__reaproute-nothing__minilm-l6-v2-384__v1", Map.of());
        assertThat(chunks(body)).isEmpty();
    }
}
