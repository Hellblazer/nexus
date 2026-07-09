// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TokenHashing;
import dev.nexus.service.vectors.PgVectorRepository;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-155 P4a.1 (bead nexus-655hc): the SERVING surface on pgvector — locked
 * cutover contract for {@code /v1/vectors/*}.
 *
 * <p><strong>TDD-RED: every test fails until bead nexus-1k8s1 (P4a.2) rewrites
 * {@code VectorHandler}'s serving routes onto {@link PgVectorRepository}.</strong>
 * The service here is wired with a PgVectorRepository ONLY — no Chroma repository,
 * no Chroma server, no chroma binary on the machine. Today every serving route
 * dereferences the absent Chroma repository (500); after P4a.2 each op serves from
 * the {@code nexus.chunks_<dim>} tables with the SAME response envelopes the
 * Python {@code _ServiceCollectionStub} already speaks, so the client ports
 * unchanged:
 * <pre>
 *   upsert-chunks   {"upserted": N}
 *   search          flat row list (id, content, distance, collection, metadata...)
 *   store-put       {"id": "..."}
 *   get             {"ids": [...], "documents": [...], "metadatas": [...]}
 *   store-get       {"ids": [...], "documents": [...], "metadatas": [...]}
 *   store-list      {"ids": [...], "metadatas": [...]}
 *   store-delete    {"deleted": N}
 *   update-metadata {"updated": N}
 *   collections     [{"name": "..."}, ...]
 *   count           {"count": N}
 * </pre>
 *
 * <p><strong>Tenant contract.</strong> Unlike the Chroma path (collection names
 * were the access boundary, X-Nexus-Tenant ignored), every pgvector serving op is
 * scoped by the SERVER-RESOLVED tenant under FORCE RLS — a bearer bound to another
 * tenant sees and affects exactly 0 rows. This is the skp06 supersession made
 * concrete: tenant isolation is native RLS, not an app-layer guard.
 *
 * <p>Open P4a.2 decisions deliberately NOT pinned here (recorded on nexus-1k8s1):
 * manifest-cleanup enforcement on delete (the fixture uses chunks with no manifest
 * rows) and the port-interface vs direct-rewrite choice for the handler. The
 * operator-form {@code where} gap is now closed for the common subset
 * ({@code $eq}/{@code $ne}/{@code $in}/{@code $nin}) across the search and /get
 * routes (nexus-05bfd); compound {@code $and}/{@code $or} and range operators
 * remain untranslated and fail loud with 400.
 *
 * <p>Hermetic: Testcontainers pgvector/pgvector:pg17, FakeEmbedder unit vectors,
 * port 0, PER_CLASS. Ordered: the suite walks one collection through its
 * lifecycle (upsert → read ops → mutate → delete) so each op's contract is
 * asserted against exactly-known state.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class PgVectorServingContractTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    private static final String TOKEN_A  = "p4a-serving-token-a-0123456789abcdef";
    private static final String TOKEN_B  = "p4a-serving-token-b-0123456789abcdef";
    private static final String TENANT_A = "p4a-tenant-a";
    private static final String TENANT_B = "p4a-tenant-b";

    // Chunk IDs are chosen so lexicographic chash order matches the expected
    // sequences in the ordered assertions: 'p4a-c1' < 'p4a-c2' < 'p4a-c3' < 'p4a-put1'.
    private static final String COL = "knowledge__p4aserve__voyage-context-3__v1";
    private static final String Q   = "tenant isolation policy";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    PgVectorRepository pgRepo;
    NexusService service;
    HttpClient http;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; " +
                "  END IF; " +
                "END $$");
        }
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            try (Liquibase liquibase = new Liquibase(
                    "db/changelog/db.changelog-master.xml",
                    new ClassLoaderResourceAccessor(), db)) {
                liquibase.update(new Contexts());
            }
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "ALTER ROLE nexus_svc SET search_path TO nexus, public");
        }
        try (Connection su = pg.createConnection("");
             var ps = su.prepareStatement(
                 "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label)"
                 + " VALUES (?, ?, ?) ON CONFLICT (token_hash) DO NOTHING")) {
            su.setAutoCommit(true);
            for (var bound : List.of(Map.entry(TOKEN_A, TENANT_A),
                                     Map.entry(TOKEN_B, TENANT_B))) {
                ps.setString(1, TokenHashing.sha256Hex(bound.getKey()));
                ps.setString(2, bound.getValue());
                ps.setString(3, "p4a-serving-test");
                ps.executeUpdate();
            }
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername("nexus_svc");
        cfg.setPassword("nexus_svc_pass");
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);

        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(1024);
        embedder.register(Q, 1.0f, 0.0f);
        embedder.register("the tenant isolation policy guards every row",     1.0f, 0.0f);
        embedder.register("tenant isolation policy enforcement in postgres",  0.8f, 0.6f);
        embedder.register("a tenant isolation policy for vector chunks",      0.6f, 0.8f);
        embedder.register("single put chunk about tenant isolation policy",   0.0f, 1.0f);
        pgRepo = new PgVectorRepository(tenantScope, embedder, embedder);

        // THE WIRING UNDER TEST: pgvector only. No Chroma repository, no Chroma
        // server, no EmbedderRouter — the serving surface must work from this.
        service = new NexusService(0, TOKEN_A, svcDs, null, null, pgRepo);
        service.start();
        http = HttpClient.newHttpClient();
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs   != null) svcDs.close();
        if (pg      != null) pg.stop();
    }

    // ---------------------------------------------------------------------------
    // HTTP helpers
    // ---------------------------------------------------------------------------

    private HttpResponse<String> post(String path, String token, Object body)
            throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + token)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> get(String pathAndQuery, String token) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + pathAndQuery))
            .header("Authorization", "Bearer " + token)
            .GET()
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private Map<String, Object> postOk(String path, String token, Object body)
            throws Exception {
        var resp = post(path, token, body);
        assertThat(resp.statusCode())
            .as("%s must serve 200 from the pgvector-only wiring (got body: %s)",
                path, resp.body())
            .isEqualTo(200);
        return MAPPER.readValue(resp.body(), MAP_TYPE);
    }

    // ---------------------------------------------------------------------------
    // Lifecycle walk — every serving op against exactly-known pgvector state
    // ---------------------------------------------------------------------------

    @Test
    @Order(1)
    void upsertChunks_servesFromPgvector() throws Exception {
        Map<String, Object> resp = postOk("/v1/vectors/upsert-chunks", TOKEN_A, Map.of(
            "collection", COL,
            "ids",        List.of("p4a-c100000000000000000000000000", "p4a-c200000000000000000000000000", "p4a-c300000000000000000000000000"),
            "documents",  List.of(
                "the tenant isolation policy guards every row",
                "tenant isolation policy enforcement in postgres",
                "a tenant isolation policy for vector chunks"),
            "metadatas",  List.of(
                Map.of("lang", "java"), Map.of("lang", "py"), Map.of("lang", "java"))));

        assertThat(((Number) resp.get("upserted")).intValue())
            .as("upsert envelope {\"upserted\": N} preserved").isEqualTo(3);

        // The rows must be IN PGVECTOR — count them in nexus.chunks_1024 as
        // superuser. This is what makes the suite a cutover proof rather than a
        // status-code check.
        try (Connection su = pg.createConnection("");
             var ps = su.prepareStatement(
                 "SELECT count(*) FROM nexus.chunks_1024 WHERE collection = ?")) {
            ps.setString(1, COL);
            try (var rs = ps.executeQuery()) {
                rs.next();
                assertThat(rs.getLong(1))
                    .as("upserted chunks land in nexus.chunks_1024")
                    .isEqualTo(3L);
            }
        }
    }

    @Test
    @Order(2)
    void upsertChunks_full64Id_returns400WithIndex() throws Exception {
        // nexus-e0hd2: the classic full-sha256 id now 400s at the boundary
        // (with its index + the truncate hint) instead of dying reason-poor
        // at the chunks CHECK inside the transaction. Non-hex 32-char ids
        // (like this suite's own fixtures) remain contract-legal.
        var resp = post("/v1/vectors/upsert-chunks", TOKEN_A, Map.of(
            "collection", COL,
            "ids",        List.of("a".repeat(64)),
            "documents",  List.of("doomed")));
        assertThat(resp.statusCode()).isEqualTo(400);
        assertThat(resp.body())
            .contains("ids[0]")
            .contains("got 64 chars");
    }

    @Test
    @Order(2)
    void search_servesRankedRowsFromPgvector() throws Exception {
        var resp = post("/v1/vectors/search", TOKEN_A, Map.of(
            "query", Q, "collections", List.of(COL), "n_results", 10));
        assertThat(resp.statusCode())
            .as("search must serve 200 from pgvector (got: %s)", resp.body())
            .isEqualTo(200);

        List<Map<String, Object>> rows = MAPPER.readValue(resp.body(), List.class);
        assertThat(rows.stream().map(r -> r.get("id")).toList())
            .as("cosine-ranked flat rows: distances 0.0, 0.2, 0.4 exactly")
            .containsExactly("p4a-c100000000000000000000000000", "p4a-c200000000000000000000000000", "p4a-c300000000000000000000000000");
        assertThat(rows.get(0).get("lang"))
            .as("metadata flattens into rows, same envelope as the Chroma path")
            .isEqualTo("java");
    }

    @Test
    @Order(3)
    void storePut_singleChunk() throws Exception {
        Map<String, Object> resp = postOk("/v1/vectors/store-put", TOKEN_A, Map.of(
            "collection", COL,
            "doc_id",     "p4a-put1000000000000000000000000",
            "content",    "single put chunk about tenant isolation policy",
            "metadata",   Map.of("kind", "put")));
        assertThat(resp.get("id"))
            .as("store-put envelope {\"id\": ...} preserved")
            .isEqualTo("p4a-put1000000000000000000000000");
    }

    @Test
    @Order(4)
    void storeGet_byIds_chromaEnvelope() throws Exception {
        Map<String, Object> resp = postOk("/v1/vectors/store-get", TOKEN_A, Map.of(
            "collection", COL,
            "ids",        List.of("p4a-c100000000000000000000000000", "p4a-put1000000000000000000000000")));

        assertThat((List<Object>) resp.get("ids"))
            .as("store-get envelope: ids aligned ascending by chash")
            .containsExactly("p4a-c100000000000000000000000000", "p4a-put1000000000000000000000000");
        assertThat((List<Object>) resp.get("documents"))
            .containsExactly(
                "the tenant isolation policy guards every row",
                "single put chunk about tenant isolation policy");
        List<Map<String, Object>> metas = (List<Map<String, Object>>) resp.get("metadatas");
        assertThat(metas.get(0).get("lang")).isEqualTo("java");
        assertThat(metas.get(1).get("kind")).isEqualTo("put");
    }

    @Test
    @Order(5)
    void get_whereEquality_filtersOnMetadata() throws Exception {
        Map<String, Object> resp = postOk("/v1/vectors/get", TOKEN_A, Map.of(
            "collection", COL,
            "where",      Map.of("lang", "py"),
            "limit",      10));

        assertThat((List<Object>) resp.get("ids"))
            .as("plain-equality where filter (the incremental-sync staleness "
                + "check's shape) returns exactly the matching chunk")
            .containsExactly("p4a-c200000000000000000000000000");
    }

    @Test
    @Order(5)
    void search_operatorFormWhere_servesFilteredOverHttp() throws Exception {
        // nexus-05bfd: operator-form $ne over the real HTTP search route (c2 is lang=py,
        // c1/c3 are lang=java) — proves the bridge translates exclusion end-to-end.
        var resp = post("/v1/vectors/search", TOKEN_A, Map.of(
            "query", Q, "collections", List.of(COL), "n_results", 10,
            "where", Map.of("lang", Map.of("$ne", "py"))));
        assertThat(resp.statusCode())
            .as("operator-form $ne must serve 200 (got: %s)", resp.body())
            .isEqualTo(200);
        List<Map<String, Object>> rows = MAPPER.readValue(resp.body(), List.class);
        // Mutation-robust (this is an ordered class; an earlier store-put added a
        // lang-less chunk that $ne correctly keeps): assert only the contract claim —
        // the py chunk is excluded, both java chunks are kept.
        assertThat(rows.stream().map(r -> r.get("id")).toList())
            .as("{lang:{$ne:py}} drops the lang=py chunk, keeps the lang=java chunks")
            .contains("p4a-c100000000000000000000000000", "p4a-c300000000000000000000000000")
            .doesNotContain("p4a-c200000000000000000000000000");
    }

    @Test
    @Order(5)
    void search_unsupportedOperatorForm_returns400OverHttp() throws Exception {
        // nexus-05bfd fail-loud contract pinned at the WIRE, not just the repo layer:
        // an unsupported operator must surface HTTP 400 (was silently zero-hits before).
        var resp = post("/v1/vectors/search", TOKEN_A, Map.of(
            "query", Q, "collections", List.of(COL), "n_results", 10,
            "where", Map.of("section_type", Map.of("$regex", ".*"))));
        assertThat(resp.statusCode())
            .as("unsupported operator-form must be rejected 400, never silently match nothing (got: %s)", resp.body())
            .isEqualTo(400);
        assertThat(resp.body())
            .as("400 body carries an error message naming the bad operator")
            .contains("error").contains("$regex");
    }

    @Test
    @Order(6)
    void storeList_paginatedEnvelope() throws Exception {
        Map<String, Object> resp = postOk("/v1/vectors/store-list", TOKEN_A, Map.of(
            "collection", COL, "limit", 2, "offset", 0));

        assertThat((List<Object>) resp.get("ids"))
            .as("store-list paginates in chash order")
            .containsExactly("p4a-c100000000000000000000000000", "p4a-c200000000000000000000000000");
        assertThat((List<?>) resp.get("metadatas"))
            .as("metadatas aligned with the page of ids")
            .hasSize(2);
    }

    @Test
    @Order(7)
    void updateMetadata_metadataOnly_textAndVectorUntouched() throws Exception {
        Map<String, Object> resp = postOk("/v1/vectors/update-metadata", TOKEN_A, Map.of(
            "collection", COL,
            "ids",        List.of("p4a-c100000000000000000000000000"),
            "metadatas",  List.of(Map.of("lang", "java", "frecency_score", 0.75))));
        assertThat(((Number) resp.get("updated")).intValue()).isEqualTo(1);

        // chunk_text untouched (no re-embed, no rewrite).
        try (Connection su = pg.createConnection("");
             var ps = su.prepareStatement(
                 "SELECT chunk_text, metadata->>'frecency_score' FROM nexus.chunks_1024"
                 + " WHERE collection = ? AND chash = ?")) {
            ps.setString(1, COL);
            ps.setString(2, "p4a-c100000000000000000000000000");
            try (var rs = ps.executeQuery()) {
                assertThat(rs.next()).isTrue();
                assertThat(rs.getString(1))
                    .isEqualTo("the tenant isolation policy guards every row");
                assertThat(rs.getString(2)).isEqualTo("0.75");
            }
        }
    }

    @Test
    @Order(8)
    void count_endpoint() throws Exception {
        var resp = get("/v1/vectors/count?collection=" + COL, TOKEN_A);
        assertThat(resp.statusCode())
            .as("count must serve 200 from pgvector (got: %s)", resp.body())
            .isEqualTo(200);
        Map<String, Object> body = MAPPER.readValue(resp.body(), MAP_TYPE);
        assertThat(((Number) body.get("count")).intValue())
            .as("3 upserted + 1 put").isEqualTo(4);
    }

    @Test
    @Order(9)
    void collections_listsThePgCollection() throws Exception {
        var resp = get("/v1/vectors/collections", TOKEN_A);
        assertThat(resp.statusCode())
            .as("collections must serve 200 from pgvector (got: %s)", resp.body())
            .isEqualTo(200);
        List<Map<String, Object>> cols = MAPPER.readValue(resp.body(), List.class);
        assertThat(cols.stream().map(c -> c.get("name")).toList())
            .as("the tenant's pgvector collections are listed")
            .contains(COL);
    }

    @Test
    @Order(9)
    void stats_perCollectionLiveStats_tenantScoped() throws Exception {
        // RDR-156 P3 (nexus-70r3c.12): GET /v1/vectors/stats serves
        // nexus.collection_vector_stats — one round-trip, all collections,
        // tombstone-filtered live counts. State here: 4 chunks in COL
        // (3 upserted + 1 store-put), all manifest-less → all live.
        var resp = get("/v1/vectors/stats", TOKEN_A);
        assertThat(resp.statusCode())
            .as("stats must serve 200 from pgvector (got: %s)", resp.body())
            .isEqualTo(200);
        List<Map<String, Object>> stats = MAPPER.readValue(resp.body(), List.class);
        var col = stats.stream()
                       .filter(s -> COL.equals(s.get("name")))
                       .findFirst()
                       .orElseThrow(() -> new AssertionError(
                           "stats must contain " + COL + " (got: " + stats + ")"));
        assertThat(((Number) col.get("count")).longValue())
            .as("live chunk_count must be exactly 4 (3 upserted + 1 put, no tombstones)")
            .isEqualTo(4L);
        assertThat(((Number) col.get("dim")).intValue())
            .as("voyage-context-3 collection routes to chunks_1024")
            .isEqualTo(1024);
        assertThat((String) col.get("last_write"))
            .as("last_write must be present for a written collection")
            .isNotEmpty();

        // Foreign bearer: RLS through the security_invoker view → 0 rows.
        var foreign = get("/v1/vectors/stats", TOKEN_B);
        assertThat(foreign.statusCode()).isEqualTo(200);
        assertThat((List<?>) MAPPER.readValue(foreign.body(), List.class))
            .as("a bearer bound to another tenant sees exactly 0 stats rows")
            .isEmpty();
    }

    // ---------------------------------------------------------------------------
    // Tenant contract — RLS replaces the never-built skp06 app-layer guard
    // ---------------------------------------------------------------------------

    @Test
    @Order(10)
    void search_tenantIsolated_foreignBearerSeesNothing() throws Exception {
        var resp = post("/v1/vectors/search", TOKEN_B, Map.of(
            "query", Q, "collections", List.of(COL), "n_results", 10));
        assertThat(resp.statusCode()).isEqualTo(200);
        assertThat((List<?>) MAPPER.readValue(resp.body(), List.class))
            .as("a bearer bound to another tenant sees exactly 0 rows — native "
                + "FORCE RLS is the boundary, not collection names")
            .isEmpty();
    }

    @Test
    @Order(11)
    void storeDelete_tenantIsolated_thenOwnerDeletes() throws Exception {
        // Foreign tenant deletes exactly 0 of tenant-a's rows.
        Map<String, Object> foreign = postOk("/v1/vectors/store-delete", TOKEN_B, Map.of(
            "collection", COL, "ids", List.of("p4a-c100000000000000000000000000", "p4a-c200000000000000000000000000")));
        assertThat(((Number) foreign.get("deleted")).intValue())
            .as("cross-tenant delete affects exactly 0 rows under RLS")
            .isEqualTo(0);

        // Owner deletes for real.
        Map<String, Object> owner = postOk("/v1/vectors/store-delete", TOKEN_A, Map.of(
            "collection", COL, "ids", List.of("p4a-c300000000000000000000000000", "p4a-put1000000000000000000000000")));
        assertThat(((Number) owner.get("deleted")).intValue()).isEqualTo(2);

        try (Connection su = pg.createConnection("");
             var ps = su.prepareStatement(
                 "SELECT count(*) FROM nexus.chunks_1024 WHERE collection = ?")) {
            ps.setString(1, COL);
            try (var rs = ps.executeQuery()) {
                rs.next();
                assertThat(rs.getLong(1))
                    .as("2 of 4 rows remain after the owner delete")
                    .isEqualTo(2L);
            }
        }
    }

    @Test
    @Order(12)
    void upsert_crossTenant_landsInOwnPartitionOnly() throws Exception {
        // WRITE-side isolation: INSERT goes through the RLS WITH CHECK policy,
        // a separate path from the SELECT USING policy the read tests exercise.
        // TOKEN_B may write to the SAME collection name — the row must land in
        // tenant-B's partition and tenant-A's rows must be untouched.
        Map<String, Object> resp = postOk("/v1/vectors/upsert-chunks", TOKEN_B, Map.of(
            "collection", COL,
            "ids",        List.of("p4a-b100000000000000000000000000"),
            "documents",  List.of("the tenant isolation policy guards every row"),
            "metadatas",  List.of(Map.of("owner", "b"))));
        assertThat(((Number) resp.get("upserted")).intValue()).isEqualTo(1);

        try (Connection su = pg.createConnection("");
             var ps = su.prepareStatement(
                 "SELECT tenant_id, count(*) FROM nexus.chunks_1024"
                 + " WHERE collection = ? GROUP BY tenant_id ORDER BY tenant_id")) {
            ps.setString(1, COL);
            try (var rs = ps.executeQuery()) {
                assertThat(rs.next()).isTrue();
                assertThat(rs.getString(1)).isEqualTo(TENANT_A);
                assertThat(rs.getLong(2))
                    .as("tenant-a still owns exactly its 2 remaining rows — the "
                        + "foreign write must not touch them (WITH CHECK)")
                    .isEqualTo(2L);
                assertThat(rs.next()).isTrue();
                assertThat(rs.getString(1)).isEqualTo(TENANT_B);
                assertThat(rs.getLong(2))
                    .as("tenant-b's write landed in tenant-b's partition")
                    .isEqualTo(1L);
                assertThat(rs.next()).isFalse();
            }
        }
    }

    @Test
    @Order(13)
    void embedEndpoint_returns503_withoutRouter() throws Exception {
        // Invariant pin (GREEN now, by design): with no EmbedderRouter wired,
        // /embed stays an explicit 503 — P4a.2's rewiring of NexusService must
        // not silently change the embed endpoint's absent-backend behaviour.
        var resp = post("/v1/vectors/embed", TOKEN_A, Map.of(
            "collection", COL, "texts", List.of("probe")));
        assertThat(resp.statusCode())
            .as("embed without a router is an explicit 503, never a fallback")
            .isEqualTo(503);
    }
}
