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
import dev.nexus.service.PgVectorRepositoryContractTest.FakeEmbedder;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
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
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-169 G5 (bead nexus-jkv85): verify that /v1/vectors/search and /v1/vectors/get
 * return the address triple (chash, source_uri, span) as ADDITIVE fields.
 *
 * <p>Additive contract (binding constraint 4):
 * <ul>
 *   <li>Existing fields (id, content, distance, collection for search;
 *       ids/documents/metadatas for get) are PRESENT and byte-compatible.
 *   <li>Content-Type is unchanged (application/json).
 *   <li>chash, source_uri, span are NEW fields present alongside existing ones.
 *   <li>When no catalog row exists for a chash, source_uri is null (missing
 *       catalog entry must not crash — graceful null, not 500).
 * </ul>
 *
 * <p>Hermetic: Testcontainers pgvector/pgvector:pg17, FakeEmbedder, port 0.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class BridgeAddressFieldsTest {

    private static final ObjectMapper MAPPER  = new ObjectMapper();
    private static final TypeReference<List<Map<String, Object>>> LIST_TYPE = new TypeReference<>() {};
    private static final TypeReference<Map<String, Object>>       MAP_TYPE  = new TypeReference<>() {};

    private static final String TOKEN   = "g5-addr-token-0123456789abcdef012345";
    private static final String TENANT  = "g5-addr-tenant";
    private static final String COL     = "knowledge__g5addr__voyage-context-3__v1";
    private static final String QUERY   = "address triple test query";

    // Two chunks: one WITH a catalog document+manifest row (has source_uri),
    // one WITHOUT (graceful-null path). Full 64-hex canonical chash (RDR-180:
    // chunks_*/manifest columns are bytea(32), CHECK octet_length=32).
    private static final String CHASH_WITH_URI    = dev.nexus.service.db.Chash.ofText("g5c1").toHex();
    private static final String CHASH_WITHOUT_URI = dev.nexus.service.db.Chash.ofText("g5c2").toHex();
    private static final String SOURCE_URI        = "file:///vault/notes/g5-test.md";
    // Full sha256 stored in chunk metadata (chunk_text_hash field)
    private static final String FULL_HASH_1 =
        "aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899";
    private static final String FULL_HASH_2 =
        "1122334455667788990011223344556677889900112233445566778899001122";

    // Second tenant for H2 isolation test: same tumbler 'g5addr.1' but different tenant
    // — must NOT return SOURCE_URI from TENANT's catalog_documents row.
    private static final String TOKEN2  = "g5-addr-token-second-tenant-9999";
    private static final String TENANT2 = "g5-addr-tenant-two";

    PostgreSQLContainer<?> pg;
    HikariDataSource       svcDs;
    PgVectorRepository     pgRepo;
    NexusService           service;
    HttpClient             http;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        // Role
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN "
                + "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='nexus_svc') THEN "
                + "  CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; "
                + "END IF; END $$");
        }
        // Schema
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            try (Liquibase liq = new Liquibase("db/changelog/db.changelog-master.xml",
                                               new ClassLoaderResourceAccessor(), db)) {
                liq.update(new Contexts());
            }
        }
        // search_path for nexus_svc
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "ALTER ROLE nexus_svc SET search_path TO nexus, public");
        }
        // Tokens — one per tenant (TENANT and TENANT2 for H2 isolation test)
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            try (var ps = su.prepareStatement(
                     "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label)"
                     + " VALUES (?, ?, ?) ON CONFLICT (token_hash) DO NOTHING")) {
                ps.setString(1, TokenHashing.sha256Hex(TOKEN));
                ps.setString(2, TENANT);
                ps.setString(3, "g5-addr-test");
                ps.executeUpdate();
                ps.setString(1, TokenHashing.sha256Hex(TOKEN2));
                ps.setString(2, TENANT2);
                ps.setString(3, "g5-addr-test-2");
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

        TenantScope tenantScope = new TenantScope(svcDs);
        var embedder = new FakeEmbedder(1024);
        embedder.register(QUERY,                             1.0f, 0.0f);
        embedder.register("chunk with source_uri in catalog", 1.0f, 0.0f);
        embedder.register("chunk without catalog entry",      0.8f, 0.6f);
        pgRepo  = new PgVectorRepository(tenantScope, embedder, embedder);
        service = new NexusService(0, TOKEN, svcDs, null, null, pgRepo);
        service.start();
        http = HttpClient.newHttpClient();

        // Upsert chunks (metadata carries chunk_text_hash = full sha256 hex)
        post("/v1/vectors/upsert-chunks", Map.of(
            "collection", COL,
            "ids",        List.of(CHASH_WITH_URI, CHASH_WITHOUT_URI),
            "documents",  List.of("chunk with source_uri in catalog",
                                  "chunk without catalog entry"),
            "metadatas",  List.of(
                Map.of("chunk_text_hash", FULL_HASH_1, "line_start", 10, "line_end", 20),
                Map.of("chunk_text_hash", FULL_HASH_2, "line_start",  5, "line_end", 15))));

        // Register a catalog owner+document for CHASH_WITH_URI only.
        // Schema: catalog_owners PK is (tenant_id, tumbler_prefix); catalog_documents PK is (tenant_id, tumbler).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // owner — actual schema has (tenant_id, tumbler_prefix, name, owner_type, repo_root)
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_owners (tenant_id, tumbler_prefix, name, owner_type, repo_root)"
                + " VALUES ('" + TENANT + "', 'g5addr', 'G5 Addr Test', 'user', '')"
                + " ON CONFLICT DO NOTHING");
            // document — PK is (tenant_id, tumbler)
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_documents"
                + " (tenant_id, tumbler, title, content_type, source_uri, indexed_at)"
                + " VALUES ('" + TENANT + "', 'g5addr.1', 'G5 Test Note', 'markdown', '"
                + SOURCE_URI + "', now())"
                + " ON CONFLICT DO NOTHING");
            // manifest row linking document -> chunk
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_document_chunks"
                + " (tenant_id, doc_id, position, chash, chunk_index)"
                + " VALUES ('" + TENANT + "', 'g5addr.1', 0, decode('" + CHASH_WITH_URI + "', 'hex'), 0)"
                + " ON CONFLICT DO NOTHING");

            // H2 isolation: TENANT2 owns the SAME tumbler string 'g5addr.1' with a DIFFERENT
            // source_uri. The JOIN must use d.tenant_id=? so TENANT2's search never returns
            // TENANT's SOURCE_URI (and vice versa).
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_owners (tenant_id, tumbler_prefix, name, owner_type, repo_root)"
                + " VALUES ('" + TENANT2 + "', 'g5addr', 'G5 Addr Test 2', 'user', '')"
                + " ON CONFLICT DO NOTHING");
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_documents"
                + " (tenant_id, tumbler, title, content_type, source_uri, indexed_at)"
                + " VALUES ('" + TENANT2 + "', 'g5addr.1', 'G5 Test Note 2', 'markdown',"
                + " 'file:///vault/tenant2/other.md', now())"
                + " ON CONFLICT DO NOTHING");
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_document_chunks"
                + " (tenant_id, doc_id, position, chash, chunk_index)"
                + " VALUES ('" + TENANT2 + "', 'g5addr.1', 0, decode('" + CHASH_WITH_URI + "', 'hex'), 0)"
                + " ON CONFLICT DO NOTHING");
        }

        // Upsert CHASH_WITH_URI also for TENANT2 (needs the chunk to search against)
        postAs(TOKEN2, "/v1/vectors/upsert-chunks", Map.of(
            "collection", COL,
            "ids",        List.of(CHASH_WITH_URI),
            "documents",  List.of("chunk with source_uri in catalog"),
            "metadatas",  List.of(Map.of("chunk_text_hash", FULL_HASH_1))));
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs   != null) svcDs.close();
        if (pg      != null) pg.stop();
    }

    // ── Tests ──────────────────────────────────────────────────────────────────

    /**
     * Default path (no include_source_uri flag): chash + span present; source_uri ABSENT.
     * Wire shape is byte-identical to pre-G5 for existing callers — no JOIN cost paid.
     */
    @Test
    void search_defaultPath_noSourceUri() throws Exception {
        var resp = post("/v1/vectors/search", Map.of(
            "query", QUERY, "collections", List.of(COL), "n_results", 10));

        assertThat(resp.statusCode())
            .as("search must return 200 (body: %s)", resp.body())
            .isEqualTo(200);
        assertThat(resp.headers().firstValue("Content-Type").orElse(""))
            .as("Content-Type must remain application/json — no framing change (RDR-169 binding constraint 4)")
            .contains("application/json");

        List<Map<String, Object>> rows = MAPPER.readValue(resp.body(), LIST_TYPE);
        assertThat(rows).hasSizeGreaterThanOrEqualTo(2);

        for (var row : rows) {
            // Existing fields PRESERVED
            assertThat(row).containsKeys("id", "content", "distance", "collection");
            // chash and span are always-on (free — no I/O)
            assertThat(row).containsKey("chash");
            assertThat(row).containsKey("span");
            // source_uri MUST be absent on the default path (zero JOIN cost)
            assertThat(row).doesNotContainKey("source_uri");
        }
    }

    /**
     * Opt-in path (include_source_uri=true): chash + span + source_uri present.
     * Existing fields (id, content, distance, collection) are UNTOUCHED.
     * Graceful null when no catalog row exists — not 500.
     */
    @Test
    void search_optIn_carriesSourceUri() throws Exception {
        var resp = post("/v1/vectors/search", Map.of(
            "query", QUERY, "collections", List.of(COL), "n_results", 10,
            "include_source_uri", true));

        assertThat(resp.statusCode())
            .as("search with include_source_uri must return 200 (body: %s)", resp.body())
            .isEqualTo(200);

        List<Map<String, Object>> rows = MAPPER.readValue(resp.body(), LIST_TYPE);
        assertThat(rows).hasSizeGreaterThanOrEqualTo(2);

        for (var row : rows) {
            assertThat(row).containsKeys("id", "content", "distance", "collection");
            assertThat(row).containsKey("chash");
            assertThat(row).containsKey("span");
            // source_uri key is PRESENT when opt-in (value may be null for uncatalogued chunks)
            assertThat(row).containsKey("source_uri");
        }

        // WITH-URI row: source_uri populated from catalog
        Map<String, Object> withUri = rows.stream()
            .filter(r -> CHASH_WITH_URI.equals(r.get("id")))
            .findFirst()
            .orElseThrow(() -> new AssertionError("CHASH_WITH_URI row not found"));

        assertThat(withUri.get("chash")).isEqualTo(CHASH_WITH_URI);
        assertThat(withUri.get("source_uri"))
            .as("source_uri resolved from catalog via JOIN")
            .isEqualTo(SOURCE_URI);
        // S2: exact chash: span form (not just non-blank) — proves wikilink-resolvable form
        assertThat((String) withUri.get("span"))
            .as("span must be the chash:<full_sha256> form from chunk_text_hash")
            .isEqualTo("chash:" + FULL_HASH_1);

        // WITHOUT-URI row: source_uri is null (graceful, not 500)
        Map<String, Object> withoutUri = rows.stream()
            .filter(r -> CHASH_WITHOUT_URI.equals(r.get("id")))
            .findFirst()
            .orElseThrow(() -> new AssertionError("CHASH_WITHOUT_URI row not found"));

        assertThat(withoutUri.get("chash")).isEqualTo(CHASH_WITHOUT_URI);
        assertThat(withoutUri.get("source_uri"))
            .as("source_uri is null when no catalog row exists")
            .isNull();
        assertThat((String) withoutUri.get("span")).isNotBlank();
    }

    /**
     * /v1/vectors/hybrid-search: default path has no source_uri;
     * opt-in path carries it additively.
     */
    @Test
    void hybridSearch_optIn_carriesSourceUri() throws Exception {
        // default path — source_uri absent
        var defResp = post("/v1/vectors/hybrid-search", Map.of(
            "query", QUERY, "collections", List.of(COL), "n_results", 10));
        assertThat(defResp.statusCode()).isEqualTo(200);
        List<Map<String, Object>> defRows = MAPPER.readValue(defResp.body(), LIST_TYPE);
        for (var row : defRows) {
            assertThat(row).containsKeys("id", "content", "distance", "collection", "chash", "span");
            assertThat(row).doesNotContainKey("source_uri");
        }

        // opt-in path — source_uri present
        var optResp = post("/v1/vectors/hybrid-search", Map.of(
            "query", QUERY, "collections", List.of(COL), "n_results", 10,
            "include_source_uri", true));
        assertThat(optResp.statusCode()).isEqualTo(200);
        List<Map<String, Object>> optRows = MAPPER.readValue(optResp.body(), LIST_TYPE);
        for (var row : optRows) {
            assertThat(row).containsKeys("id", "content", "distance", "collection",
                                         "chash", "span", "source_uri");
        }
    }

    /**
     * /v1/vectors/get: default path has chashes + spans but NO source_uris;
     * opt-in path adds source_uris. Existing {ids, documents, metadatas} untouched.
     */
    @Test
    void get_optIn_carriesSourceUris() throws Exception {
        // default path — source_uris absent
        var defResp = post("/v1/vectors/get", Map.of("collection", COL, "limit", 10));
        assertThat(defResp.statusCode()).isEqualTo(200);
        Map<String, Object> defEnv = MAPPER.readValue(defResp.body(), MAP_TYPE);
        assertThat(defEnv).containsKeys("ids", "documents", "metadatas", "chashes", "spans");
        assertThat(defEnv).doesNotContainKey("source_uris");

        // opt-in path
        var optResp = post("/v1/vectors/get", Map.of(
            "collection", COL, "limit", 10, "include_source_uri", true));
        assertThat(optResp.statusCode()).isEqualTo(200);
        Map<String, Object> envelope = MAPPER.readValue(optResp.body(), MAP_TYPE);

        assertThat(envelope).containsKeys("ids", "documents", "metadatas",
                                          "chashes", "source_uris", "spans");

        List<String> ids     = (List<String>) envelope.get("ids");
        List<String> chashes = (List<String>) envelope.get("chashes");
        List<Object> uris    = (List<Object>) envelope.get("source_uris");
        List<String> spans   = (List<String>) envelope.get("spans");

        assertThat(chashes).hasSameSizeAs(ids);
        assertThat(uris).hasSameSizeAs(ids);
        assertThat(spans).hasSameSizeAs(ids);

        int withUriIdx = ids.indexOf(CHASH_WITH_URI);
        assertThat(withUriIdx).as("CHASH_WITH_URI in results").isGreaterThanOrEqualTo(0);
        assertThat(uris.get(withUriIdx)).isEqualTo(SOURCE_URI);

        int withoutUriIdx = ids.indexOf(CHASH_WITHOUT_URI);
        assertThat(withoutUriIdx).as("CHASH_WITHOUT_URI in results").isGreaterThanOrEqualTo(0);
        assertThat(uris.get(withoutUriIdx)).isNull();
    }

    /**
     * /v1/vectors/store-get: default path has chashes + spans but NO source_uris;
     * opt-in path adds source_uris aligned with ids.
     */
    @Test
    void storeGet_optIn_carriesSourceUri() throws Exception {
        // default path — source_uris absent
        var defResp = post("/v1/vectors/store-get", Map.of(
            "collection", COL, "ids", List.of(CHASH_WITH_URI)));
        assertThat(defResp.statusCode()).isEqualTo(200);
        Map<String, Object> defEnv = MAPPER.readValue(defResp.body(), MAP_TYPE);
        assertThat(defEnv).containsKeys("ids", "documents", "metadatas", "chashes", "spans");
        assertThat(defEnv).doesNotContainKey("source_uris");

        // opt-in path
        var optResp = post("/v1/vectors/store-get", Map.of(
            "collection", COL, "ids", List.of(CHASH_WITH_URI),
            "include_source_uri", true));
        assertThat(optResp.statusCode()).isEqualTo(200);
        Map<String, Object> envelope = MAPPER.readValue(optResp.body(), MAP_TYPE);

        assertThat(envelope).containsKeys("ids", "documents", "metadatas",
                                          "chashes", "source_uris", "spans");

        List<String> chashes = (List<String>) envelope.get("chashes");
        List<Object> uris    = (List<Object>) envelope.get("source_uris");
        List<String> spans   = (List<String>) envelope.get("spans");
        assertThat(chashes.get(0)).isEqualTo(CHASH_WITH_URI);
        assertThat(uris.get(0)).isEqualTo(SOURCE_URI);
        // S2: exact chash: span form
        assertThat(spans.get(0))
            .as("span must be the chash:<full_sha256> form from chunk_text_hash")
            .isEqualTo("chash:" + FULL_HASH_1);
    }

    /**
     * S1 — Zero-JOIN proof: default path (includeSourceUri=false) must invoke ZERO catalog
     * JOIN batches. Verified via {@link PgVectorRepository#sourceUriJoinCalls}, a
     * package-private {@link java.util.concurrent.atomic.AtomicInteger} that
     * {@code sourceUrisByChash} increments at entry. Package visibility (same package as
     * this test) lets us read the counter directly — no subclassing required.
     *
     * <p>Two assertions lock the latency guarantee end-to-end:
     * <ol>
     *   <li>Default path (includeSourceUri=false): counter stays at its pre-call value → zero
     *       catalog JOINs executed. A future refactor that inverts the guard fails this count.</li>
     *   <li>Opt-in path (includeSourceUri=true) on a result set with catalog rows: counter
     *       increments by ≥1 → the JOIN actually ran.</li>
     * </ol>
     */
    @Test
    void defaultPath_producesNoSourceUri_optInProducesSourceUri() {
        // Reset counter to isolate this test from any prior calls in @BeforeAll setup
        pgRepo.resetSourceUriJoinCallsForTests();

        // Default path: sourceUrisByChash must NOT be called
        var defaultRows = pgRepo.searchWithTokens(TENANT, QUERY, List.of(COL), 10, null, false).value();
        assertThat(defaultRows).as("search must return rows").isNotEmpty();
        assertThat(pgRepo.sourceUriJoinCallCount())
            .as("default path (includeSourceUri=false): ZERO catalog JOIN calls")
            .isEqualTo(0);
        for (var row : defaultRows) {
            assertThat(row).as("default path: chash always present").containsKey("chash");
            assertThat(row).as("default path: span always present").containsKey("span");
            assertThat(row).as("default path: source_uri must be ABSENT")
                           .doesNotContainKey("source_uri");
        }

        // Opt-in path: sourceUrisByChash must be called ≥1 time
        int callsBefore = pgRepo.sourceUriJoinCallCount();
        var optInRows = pgRepo.searchWithTokens(TENANT, QUERY, List.of(COL), 10, null, true).value();
        assertThat(pgRepo.sourceUriJoinCallCount())
            .as("opt-in path (includeSourceUri=true): ≥1 catalog JOIN call")
            .isGreaterThan(callsBefore);
        Map<String, Object> withUri = optInRows.stream()
            .filter(r -> CHASH_WITH_URI.equals(r.get("id")))
            .findFirst()
            .orElseThrow(() -> new AssertionError("CHASH_WITH_URI row not found in opt-in search"));
        assertThat(withUri).as("opt-in path: source_uri key present").containsKey("source_uri");
        assertThat(withUri.get("source_uri"))
            .as("opt-in path: source_uri populated from catalog")
            .isEqualTo(SOURCE_URI);
    }

    /**
     * H2 — Tenant isolation on catalog JOIN: TENANT2 searching with include_source_uri=true
     * must NOT see TENANT's source_uri even though both tenants have a catalog row for the
     * same tumbler 'g5addr.1' and the same chash.
     */
    @Test
    void sourceUri_isTenantIsolated() throws Exception {
        // TENANT gets SOURCE_URI
        var resp1 = postAs(TOKEN, "/v1/vectors/search", Map.of(
            "query", QUERY, "collections", List.of(COL), "n_results", 10,
            "include_source_uri", true));
        assertThat(resp1.statusCode()).isEqualTo(200);
        List<Map<String, Object>> rows1 = MAPPER.readValue(resp1.body(), LIST_TYPE);
        Map<String, Object> row1 = rows1.stream()
            .filter(r -> CHASH_WITH_URI.equals(r.get("id")))
            .findFirst()
            .orElseThrow();
        assertThat(row1.get("source_uri"))
            .as("TENANT must get its own source_uri")
            .isEqualTo(SOURCE_URI);

        // TENANT2 must get its OWN source_uri, not TENANT's
        var resp2 = postAs(TOKEN2, "/v1/vectors/search", Map.of(
            "query", QUERY, "collections", List.of(COL), "n_results", 10,
            "include_source_uri", true));
        assertThat(resp2.statusCode()).isEqualTo(200);
        List<Map<String, Object>> rows2 = MAPPER.readValue(resp2.body(), LIST_TYPE);
        Map<String, Object> row2 = rows2.stream()
            .filter(r -> CHASH_WITH_URI.equals(r.get("id")))
            .findFirst()
            .orElseThrow();
        assertThat(row2.get("source_uri"))
            .as("TENANT2 must NOT see TENANT's source_uri — tenants are isolated")
            .isNotEqualTo(SOURCE_URI)
            .isEqualTo("file:///vault/tenant2/other.md");
    }

    /**
     * M3 — hybridSearch(5-arg) symmetric with search(5-arg): both return chash+span.
     * Tests the repo directly (not via HTTP) to confirm the 5-arg delegates route
     * through the enriched path.
     */
    @Test
    void hybridSearch5Arg_hasChashAndSpan_symmetricWithSearch() {
        TenantScope tenantScope = new TenantScope(svcDs);
        var embedder = new FakeEmbedder(1024);
        embedder.register(QUERY, 1.0f, 0.0f);
        embedder.register("chunk with source_uri in catalog", 1.0f, 0.0f);
        embedder.register("chunk without catalog entry", 0.8f, 0.6f);
        PgVectorRepository repo = new PgVectorRepository(tenantScope, embedder, embedder);

        var searchRows  = repo.search(TENANT, QUERY, List.of(COL), 10, null);
        var hybridRows  = repo.hybridSearch(TENANT, QUERY, List.of(COL), 10, null);

        for (var row : searchRows) {
            assertThat(row).as("search(5-arg) must carry chash").containsKey("chash");
            assertThat(row).as("search(5-arg) must carry span").containsKey("span");
            assertThat(row).as("search(5-arg) must NOT carry source_uri (default path)")
                           .doesNotContainKey("source_uri");
        }
        for (var row : hybridRows) {
            assertThat(row).as("hybridSearch(5-arg) must carry chash").containsKey("chash");
            assertThat(row).as("hybridSearch(5-arg) must carry span").containsKey("span");
            assertThat(row).as("hybridSearch(5-arg) must NOT carry source_uri (default path)")
                           .doesNotContainKey("source_uri");
        }
    }

    // ── Helpers ────────────────────────────────────────────────────────────────

    private HttpResponse<String> post(String path, Object body) throws Exception {
        return postAs(TOKEN, path, body);
    }

    private HttpResponse<String> postAs(String tok, String path, Object body) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + tok)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
