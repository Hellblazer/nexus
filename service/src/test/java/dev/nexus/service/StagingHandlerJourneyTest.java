package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TokenHashing;
import dev.nexus.service.vectors.DimTables;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.PgVectorRepository;
import liquibase.Contexts;
import liquibase.Liquibase;
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
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-180 land-then-transform HTTP journey (nexus-jxizy.10.4): the whole
 * migration surface over the wire — land (with + without vectors) →
 * embed_fill → promote → finalize → counts → clear — against the real
 * NexusService + Liquibase schema, Bearer + tenant-header authenticated.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class StagingHandlerJourneyTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    private static final String TOKEN  = "staging-journey-token-0123456789abcdef";
    private static final String TENANT = "staging-journey-tenant";
    private static final String COLL   = "knowledge__journey__voyage-context-3__v1";

    private static final String TEXT_REUSE = "journey chunk with reusable vector";
    private static final String TEXT_FILL  = "journey chunk needing embed fill";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    NexusService service;
    HttpClient http;
    TenantScope scope;

    private static String digestHex(String text) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                .digest(text.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') "
                + "THEN CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; "
                + "END IF; END $$");
        }
        try (Connection su = pg.createConnection("")) {
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(su)))
                .update(new Contexts());
        }
        try (Connection su = pg.createConnection("");
             var ps = su.prepareStatement(
                 "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label)"
                 + " VALUES (?, ?, 'staging-journey') ON CONFLICT (token_hash) DO NOTHING")) {
            su.setAutoCommit(true);
            ps.setString(1, TokenHashing.sha256Hex(TOKEN));
            ps.setString(2, TENANT);
            ps.executeUpdate();
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "ALTER ROLE nexus_svc SET search_path TO nexus, public");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername("nexus_svc");
        cfg.setPassword("nexus_svc_pass");
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        scope = new TenantScope(svcDs);

        var fake = new PgVectorRepositoryContractTest.FakeEmbedder(1024);
        fake.register(TEXT_FILL, 0.70710678f, 0.70710678f);
        // The router keys its dispatch by modelToken(); the fake defaults to
        // "unknown", so wrap it under the model this journey's collection
        // names (voyage-context-3 — 1024-dim per MODEL_DIMS).
        var embedder = new dev.nexus.service.vectors.Embedder() {
            @Override public java.util.List<float[]> embed(java.util.List<String> texts) {
                return fake.embed(texts);
            }
            @Override public String modelToken() { return "voyage-context-3"; }
        };
        var router = new EmbedderRouter(embedder, "document");
        var pgRepo = new PgVectorRepository(scope, embedder, embedder);
        service = new NexusService(0, TOKEN, svcDs, router, pgRepo);
        service.start();
        http = HttpClient.newHttpClient();
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs   != null) svcDs.close();
        if (pg      != null) pg.stop();
    }

    private HttpResponse<String> post(String path, Object body) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", TENANT)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private Map<String, Object> postOk(String path, Object body) throws Exception {
        var resp = post(path, body);
        assertThat(resp.statusCode())
            .as("%s must 200 (body: %s)", path, resp.body()).isEqualTo(200);
        return MAPPER.readValue(resp.body(), MAP_TYPE);
    }

    private Map<String, Object> getOk(String path) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", TENANT)
            .GET().build();
        var resp = http.send(req, HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).as("%s must 200 (body: %s)", path, resp.body()).isEqualTo(200);
        return MAPPER.readValue(resp.body(), MAP_TYPE);
    }

    private int count(String sql) {
        return scope.withTenant(TENANT, ctx -> ctx.fetchOne(sql).get(0, Integer.class));
    }

    /** nexus-4okz4 increment 5: read back a staged chunk's text by legacy_ref
     *  (re-land regression coverage — proves update-in-place, not just row
     *  count). */
    private String fetchStagedChunkText(String legacyRef) {
        return scope.withTenant(TENANT, ctx -> ctx.fetchOne(
            "SELECT chunk_text FROM staging.chunks WHERE legacy_ref = '" + legacyRef + "'")
            .get(0, String.class));
    }

    private static List<Double> unitVec1024() {
        List<Double> v = new ArrayList<>(1024);
        v.add(1.0);
        for (int i = 1; i < 1024; i++) v.add(0.0);
        return v;
    }

    @Test
    @Order(1)
    void land_chunksAndPointerStores() throws Exception {
        Map<String, Object> landed = postOk("/v1/staging/load/chunks", Map.of("rows", List.of(
            Map.of("collection", COLL, "dim", 1024,
                   "legacy_ref", digestHex(TEXT_REUSE).substring(0, 32),
                   "chunk_text", TEXT_REUSE, "embedding", unitVec1024(),
                   "model", "voyage-context-3"),
            // No embedding — reuse not legal; embed_fill covers it.
            Map.of("collection", COLL, "dim", 1024,
                   "legacy_ref", "b46c7915c303245f",
                   "chunk_text", TEXT_FILL,
                   "model", "voyage-context-3"))));
        assertThat(((Number) landed.get("landed")).intValue()).isEqualTo(2);

        // A manifest row + its FK parent doc (docs ride the catalog ETL leg;
        // the journey stands in for it).
        // nexus-7nrvr: real collection — ghost-ness was incidental (the
        // embed_fill/promote/finalize journey is the point). COLL is this
        // doc's real home: everything it references is landed there above.
        scope.withTenant(TENANT, ctx -> {
            ctx.execute("INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) "
                + "VALUES (?, '9.9.1', 'journey-doc', ?) ON CONFLICT DO NOTHING", TENANT, COLL);
            return null;
        });
        // nexus-lgdel.l1: the staged manifest pointer's chash must be the
        // CANONICAL 64-hex digest, not the legacy32-truncated ref — finalize's
        // manifest promote is direct-64-hex-only now (chash_alias is retired),
        // so a legacy-shaped chash here would never resolve and Order(2)'s
        // manifest_promoted assertion would see 0, not 1.
        postOk("/v1/staging/load/document_chunks", Map.of("rows", List.of(
            Map.of("doc_id", "9.9.1", "position", 0,
                   "chash", digestHex(TEXT_REUSE)))));
        // RDR-187 nexus-piwya.11: the chash_index staging store is retired —
        // an old client's landing attempt answers 400 unknown-store, and the
        // counts envelope no longer carries the key.
        assertThat(post("/v1/staging/load/chash_index", Map.of("rows", List.of(
            Map.of("chash", digestHex(TEXT_REUSE).substring(0, 32),
                   "physical_collection", COLL,
                   "created_at", "2026-07-01T00:00:00Z")))).statusCode())
            .as("chash_index landing store retired (nexus-piwya.11)")
            .isEqualTo(400);

        Map<String, Object> counts = getOk("/v1/staging/counts");
        assertThat(((Number) counts.get("chunks")).intValue()).isEqualTo(2);
        assertThat(((Number) counts.get("document_chunks")).intValue()).isEqualTo(1);
        assertThat(counts).doesNotContainKey("chash_index");

        // nexus-4okz4 increment 5 (reviewer Significant, T2 review-4okz4-
        // increment4-2026-08-09 [21970]): permanent regression coverage for
        // the "chunks" store's ON CONFLICT DO UPDATE re-land path. Increment
        // 4's gate proved this correct with a throwaway probe and left zero
        // permanent coverage behind — re-land the SAME (tenant, collection,
        // legacy_ref) conflict key with a CHANGED payload and assert
        // update-in-place (exactly one row, new content wins), rather than
        // trusting handleLoad's onConflict(...).doUpdate().set(Map) clause
        // by inspection alone. Scaffolding, not migration product: this
        // row is deleted before the method returns so Order(2)/(3) see
        // exactly the 2-row baseline they were already written against.
        String relandRef = "reland-regression-ref";
        Map<String, Object> firstLand = postOk("/v1/staging/load/chunks", Map.of("rows", List.of(
            Map.of("collection", COLL, "dim", 1024, "legacy_ref", relandRef,
                   "chunk_text", "reland original text", "embedding", unitVec1024(),
                   "model", "voyage-context-3"))));
        assertThat(((Number) firstLand.get("landed")).intValue()).isEqualTo(1);
        assertThat(count("SELECT count(*) FROM staging.chunks WHERE legacy_ref = '" + relandRef + "'"))
            .as("initial land creates exactly one staged row").isEqualTo(1);
        assertThat(fetchStagedChunkText(relandRef)).isEqualTo("reland original text");

        Map<String, Object> secondLand = postOk("/v1/staging/load/chunks", Map.of("rows", List.of(
            Map.of("collection", COLL, "dim", 1024, "legacy_ref", relandRef,
                   "chunk_text", "reland UPDATED text", "embedding", unitVec1024(),
                   "model", "voyage-context-3"))));
        assertThat(((Number) secondLand.get("landed")).intValue())
            .as("ON CONFLICT DO UPDATE affects exactly the one conflicting row").isEqualTo(1);
        assertThat(count("SELECT count(*) FROM staging.chunks WHERE legacy_ref = '" + relandRef + "'"))
            .as("re-land updates in place -- no duplicate row created").isEqualTo(1);
        assertThat(fetchStagedChunkText(relandRef))
            .as("re-land's new payload wins; the stale text is gone").isEqualTo("reland UPDATED text");

        scope.withTenant(TENANT, ctx -> {
            ctx.execute("DELETE FROM staging.chunks WHERE legacy_ref = '" + relandRef + "'");
            return null;
        });
        Map<String, Object> countsAfterCleanup = getOk("/v1/staging/counts");
        assertThat(((Number) countsAfterCleanup.get("chunks")).intValue())
            .as("re-land scaffolding row removed; baseline restored for Order(2)/(3)").isEqualTo(2);
    }

    @Test
    @Order(2)
    void embedFill_thenPromote_thenFinalize() throws Exception {
        Map<String, Object> fill = postOk("/v1/staging/embed_fill", Map.of("collection", COLL));
        assertThat(((Number) fill.get("filled")).intValue()).isEqualTo(1);
        assertThat(((Number) fill.get("remaining")).intValue()).isEqualTo(0);

        // nexus-lgdel.l1: "alias_rows" no longer exists in the promote
        // envelope — chash_alias and the C1 guard/alias-fact recording it
        // fed are retired.
        Map<String, Object> promoted = postOk("/v1/staging/promote", Map.of("collection", COLL));
        assertThat(((Number) promoted.get("promoted")).intValue()).isEqualTo(2);

        Map<String, Object> fin = postOk("/v1/staging/finalize", Map.of("orphan_policy", "drop"));
        assertThat(((Number) fin.get("manifest_promoted")).intValue()).isEqualTo(1);
        assertThat(((Number) fin.get("residual_mismatched")).intValue()).isEqualTo(0);
        assertThat(((Number) fin.get("dangling_manifest")).intValue()).isEqualTo(0);

        String canon = digestHex(TEXT_REUSE);
        assertThat(count("SELECT count(*) FROM " + DimTables.CHUNKS_TABLE_NAME + " "
            + "WHERE encode(chash,'hex') = '" + canon + "'")).isEqualTo(1);
        assertThat(count("SELECT count(*) FROM nexus.catalog_document_chunks "
            + "WHERE doc_id = '9.9.1' AND encode(chash,'hex') = '" + canon + "'")).isEqualTo(1);
        // RDR-187 (nexus-piwya.7/.9/.11): the staging chash promote leg is
        // retired, the router TABLE is dropped, and the staging landing twin
        // is dropped too; the chunks promote IS the registration.
        assertThat(count("SELECT count(*) FROM information_schema.tables "
            + "WHERE table_schema = 'nexus' AND table_name = 'chash_index'"))
            .as("the router table stays dropped; a resurrected promote target fails here")
            .isZero();
        assertThat(count("SELECT count(*) FROM information_schema.tables "
            + "WHERE table_schema = 'staging' AND table_name = 'chash_index'"))
            .as("the staging landing twin stays dropped (nexus-piwya.11)")
            .isZero();
    }

    @Test
    @Order(3)
    void clear_emptiesTheTenantStaging() throws Exception {
        Map<String, Object> cleared = postOk("/v1/staging/clear", Map.of());
        @SuppressWarnings("unchecked")
        Map<String, Object> per = (Map<String, Object>) cleared.get("cleared");
        assertThat(((Number) per.get("chunks")).intValue()).isEqualTo(2);
        Map<String, Object> counts = getOk("/v1/staging/counts");
        for (Map.Entry<String, Object> e : counts.entrySet()) {
            assertThat(((Number) e.getValue()).intValue())
                .as("staging store %s empty after clear", e.getKey()).isEqualTo(0);
        }
        // Promoted data survives the clear (staging rows are transient; the
        // nexus rows are the migration's product).
        assertThat(count("SELECT count(*) FROM " + DimTables.CHUNKS_TABLE_NAME + " "
            + "WHERE encode(chash,'hex') = '" + digestHex(TEXT_REUSE) + "'")).isEqualTo(1);
    }
}
