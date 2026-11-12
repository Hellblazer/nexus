// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.MEMORY;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.ExportTestClock;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantExport;
import dev.nexus.service.db.TenantExportKeys;
import dev.nexus.service.db.TenantExportSessions;
import dev.nexus.service.db.TokenStore;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;

/**
 * The tenant export routes through the real {@link NexusService} and its auth filter, on a real PostgreSQL
 * (RDR-248, bead nexus-csayy.2): the header, the pages and their headers, who may call and what each other
 * caller is told (gate S4a/b), the table whitelist and the cursor (S4c/d), the bounds and caps, the held
 * transaction against a real tenant create, and snapshot consistency over HTTP. The sessions' own mechanics
 * (the held transaction's locks, the key walk, the plan) are {@code TenantExportSessionsTest}'s.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TenantExportRoutesTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String ROOT = "tok-root-export-0123456789abcdefghij";
    private static final String A_TOKEN = "tok-a-export-0123456789abcdefghijkl";
    private static final String A_TOKEN_2 = "tok-a2-export-0123456789abcdefghijk";
    private static final String B_TOKEN = "tok-b-export-0123456789abcdefghijkl";
    private static final String A_DATA_TOKEN = "tok-adata-export-0123456789abcdefghi";
    private static final String A_MINT_TOKEN = "tok-amint-export-0123456789abcdefghi";
    private static final String A = "t-routes-a";
    private static final String B = "t-routes-b";
    private static final String MODEL = "bge-base-en-v15-768";
    private static final String COLLECTION = "knowledge__routes__bge-base-en-v15-768__v1";
    private static final int CHUNK_COUNT = 30;
    private static final String PATH = "/v1/tenant/export";
    private static final String UNKNOWN_ID = "00000000-0000-4000-8000-000000000000";

    private PostgreSQLContainer<?> pg;
    private HikariDataSource ds;
    private NexusService service;
    private final HttpClient http = TestHttp.client();
    private int newcomers;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(dsl, ROOT, TenantConstants.DEFAULT_TENANT, TokenStore.ROOT_TOKEN_LABEL,
                TokenStore.SCOPE_ROOT, null, null);
            PgContainerHelper.seedServiceToken(dsl, A_TOKEN, A, "tenant-a");
            PgContainerHelper.seedServiceToken(dsl, A_TOKEN_2, A, "tenant-a-2");
            PgContainerHelper.seedServiceToken(dsl, B_TOKEN, B, "tenant-b");
            OffsetDateTime later = OffsetDateTime.now().plusHours(1);
            PgContainerHelper.seedServiceToken(dsl, A_DATA_TOKEN, A, "data-a", TokenStore.SCOPE_DATA, later, null);
            PgContainerHelper.seedServiceToken(dsl, A_MINT_TOKEN, A, "mint-a", TokenStore.SCOPE_MINT, null, null);
            PgContainerHelper.ensureTenantPartitions(dsl, A);
            PgContainerHelper.ensureTenantPartitions(dsl, B);
            PgContainerHelper.insertCollection(dsl, A, COLLECTION, MODEL);
            for (int i = 0; i < CHUNK_COUNT; i++) {
                String text = "routes chunk " + i;
                dsl.insertInto(CHUNKS)
                    .set(CHUNKS.TENANT_ID, A).set(CHUNKS.COLLECTION, COLLECTION)
                    .set(CHUNKS.CHASH, sha256(A + text)).set(CHUNKS.EMBEDDING_MODEL, MODEL)
                    .set(CHUNKS.CHUNK_TEXT, text).set(CHUNKS.EMBEDDING_768, Vector.of(new float[768]))
                    .execute();
            }
            for (int i = 0; i < 5; i++) insertMemory(dsl, A, "title-" + i);
            for (int i = 0; i < 3; i++) insertMemory(dsl, B, "title-" + i);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(8);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
        service = new NexusService(0, ROOT, ds);
        service.start();
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    /** Every test starts with no session open and the production defaults. */
    @BeforeEach
    @AfterEach
    void freshSessions() {
        service.exportSessions(new TenantExportSessions(ds, TenantExportSessions.Config.defaults(),
            java.time.Clock.systemUTC()));
    }

    // ── fixtures and helpers ─────────────────────────────────────────────────────────────────────

    private static byte[] sha256(String text) throws Exception {
        return MessageDigest.getInstance("SHA-256").digest(text.getBytes(StandardCharsets.UTF_8));
    }

    private static void insertMemory(DSLContext ctx, String tenant, String title) {
        ctx.insertInto(MEMORY)
            .columns(MEMORY.TENANT_ID, MEMORY.PROJECT, MEMORY.TITLE, MEMORY.CONTENT, MEMORY.TIMESTAMP)
            .values(tenant, "p", title, "content of " + title, OffsetDateTime.now())
            .execute();
    }

    private String url(String path) {
        return "http://127.0.0.1:" + service.getPort() + path;
    }

    private HttpResponse<String> call(String method, String path, String token) throws Exception {
        var b = TestHttp.request(url(path)).method(method, HttpRequest.BodyPublishers.noBody());
        if (token != null) b.header("Authorization", "Bearer " + token);
        return http.send(b.build(), HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<byte[]> rowsBytes(String token, String id, String query) throws Exception {
        var req = TestHttp.request(url(PATH + "/" + id + "/rows" + query))
            .header("Authorization", "Bearer " + token).GET().build();
        return http.send(req, HttpResponse.BodyHandlers.ofByteArray());
    }

    private JsonNode json(HttpResponse<String> r) throws Exception {
        return MAPPER.readTree(r.body());
    }

    private JsonNode open(String token) throws Exception {
        var r = call("POST", PATH, token);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        return json(r);
    }

    private static String rowsQuery(String table, String after) {
        String q = "?table=" + java.net.URLEncoder.encode(table, StandardCharsets.UTF_8);
        return after == null ? q : q + "&after=" + java.net.URLEncoder.encode(after, StandardCharsets.UTF_8);
    }

    private static String header(HttpResponse<?> r, String name) {
        return r.headers().firstValue(name).orElse(null);
    }

    private static String sha256Hex(byte[] body) throws Exception {
        return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(body));
    }

    // ── the header ───────────────────────────────────────────────────────────────────────────────

    @Test
    void openingASessionAnswersTheHeaderWithNoRowCounts() throws Exception {
        var header = open(A_TOKEN);

        assertThat(header.get("session_id").asText()).matches("[0-9a-f-]{36}");
        assertThat(header.get("format_version").asInt()).isEqualTo(1);
        assertThat(header.get("engine_version").asText()).isNotBlank();
        assertThat(header.get("tenant_id").asText()).isEqualTo(A);
        assertThat(Duration.between(Instant.parse(header.get("created_at").asText()), Instant.now()).abs())
            .isLessThan(Duration.ofMinutes(1));
        assertThat(Duration.between(Instant.parse(header.get("created_at").asText()),
            Instant.parse(header.get("expires_at").asText()))).as("the total bound is 60 minutes").isEqualTo(Duration.ofMinutes(60));
        assertThat(header.get("idle_timeout_seconds").asLong()).isEqualTo(600);
        assertThat(header.get("total_timeout_seconds").asLong()).isEqualTo(3600);

        var version = MAPPER.readTree(http.send(TestHttp.request(url("/version")).GET().build(),
            HttpResponse.BodyHandlers.ofString()).body());
        assertThat(header.get("schema_changeset_count").asLong()).as("/version's own memoized count")
            .isEqualTo(version.get("schema_changeset_count").asLong()).isPositive();

        List<String> names = new ArrayList<>();
        for (JsonNode t : header.get("tables")) {
            names.add(t.get("name").asText());
            assertThat(t.fieldNames()).toIterable().as("a table entry carries no row count")
                .containsExactlyInAnyOrder("name", "class", "primary_key", "columns");
            assertThat(t.get("class").asText()).isIn("data", "derived");
            assertThat(t.get("columns").size()).isPositive();
            assertThat(t.get("columns").get(0).fieldNames()).toIterable().containsExactly("name", "type");
        }
        assertThat(names).containsExactlyElementsOf(TenantExport.TABLES.stream()
            .filter(TenantExport.ExportTable::exported).map(TenantExport.ExportTable::name).toList());
        assertThat(header.get("embedding_models")).hasSize(1);
        assertThat(header.get("embedding_models").get(0).get("embedding_model").asText()).isEqualTo(MODEL);
    }

    @Test
    void versionAdvertisesTheExportRoutesAndTheirFormat() throws Exception {
        var version = MAPPER.readTree(http.send(TestHttp.request(url("/version")).GET().build(),
            HttpResponse.BodyHandlers.ofString()).body());
        assertThat(version.get("tenant_export_supported").asBoolean()).isTrue();
        assertThat(version.get("tenant_export_format_version").asInt()).isEqualTo(1);
    }

    // ── the held transaction against a real tenant create ────────────────────────────────────────

    @Test
    void aTenantCreateSucceedsPromptlyWhileASessionIsOpenAndHasServedPages() throws Exception {
        var session = open(A_TOKEN);
        var id = session.get("session_id").asText();
        assertThat(rowsBytes(A_TOKEN, id, rowsQuery("chunks", null)).statusCode()).isEqualTo(200);

        long t0 = System.nanoTime();
        var req = TestHttp.request(url("/v1/tenants/create")).header("Authorization", "Bearer " + ROOT)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString("{\"name\":\"t-routes-newcomer-" + (++newcomers) + "\"}")).build();
        var resp = http.send(req, HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        assertThat(Duration.ofNanos(System.nanoTime() - t0)).as("tenant create beside a held export session")
            .isLessThan(Duration.ofSeconds(3));
        // the old failure was a 503 tenant_creation_busy after 3.45 s with the held transaction reading tables
        assertThat(resp.body()).doesNotContain("tenant_creation_busy");
    }

    // ── pages ────────────────────────────────────────────────────────────────────────────────────

    @Test
    void aFirstPageCarriesTheRowCountItsLastKeyAndAHashOfItsBody() throws Exception {
        var id = open(A_TOKEN).get("session_id").asText();
        var page = rowsBytes(A_TOKEN, id, rowsQuery("memory", null));

        assertThat(page.statusCode()).isEqualTo(200);
        assertThat(header(page, "Content-Type")).isEqualTo("application/octet-stream");
        assertThat(header(page, "X-Nexus-Export-Table")).isEqualTo("memory");
        assertThat(header(page, "X-Nexus-Export-Rows")).isEqualTo("5");
        assertThat(header(page, "X-Nexus-Export-Table-Rows")).isEqualTo("5");
        assertThat(header(page, "X-Nexus-Export-Done")).isEqualTo("true");
        assertThat(header(page, "X-Nexus-Export-Sha256")).isEqualTo(sha256Hex(page.body()));
        assertThat(new String(page.body(), 0, 6, StandardCharsets.US_ASCII)).isEqualTo("PGCOPY");
        var memory = TenantExport.exportable("memory").orElseThrow();
        assertThat(TenantExportKeys.decode(header(page, "X-Nexus-Export-Last-Key"), memory.primaryKey())).hasSize(1);

        // an empty table: a valid empty COPY stream, done, no last key
        var empty = rowsBytes(A_TOKEN, id, rowsQuery("plans", null));
        assertThat(empty.statusCode()).isEqualTo(200);
        assertThat(header(empty, "X-Nexus-Export-Rows")).isEqualTo("0");
        assertThat(header(empty, "X-Nexus-Export-Table-Rows")).isEqualTo("0");
        assertThat(header(empty, "X-Nexus-Export-Done")).isEqualTo("true");
        assertThat(header(empty, "X-Nexus-Export-Last-Key")).isNull();
    }

    @Test
    void theChunksCompositeKeyPagesByBytesThroughTheLastKeyHeaderAndEndsWithTheTable() throws Exception {
        var small = new TenantExportSessions.Config(Duration.ofMinutes(60), Duration.ofMinutes(10), 2, 16_000, 100_000,
            5_000, 30, Duration.ofHours(1));
        service.exportSessions(new TenantExportSessions(ds, small, java.time.Clock.systemUTC()));
        var id = open(A_TOKEN).get("session_id").asText();

        String after = null;
        int total = 0;
        int pages = 0;
        String previous = null;
        while (pages++ < 100) {
            var page = rowsBytes(A_TOKEN, id, rowsQuery("chunks", after));
            assertThat(page.statusCode()).isEqualTo(200);
            assertThat(header(page, "X-Nexus-Export-Sha256")).isEqualTo(sha256Hex(page.body()));
            if (after == null) {
                assertThat(header(page, "X-Nexus-Export-Table-Rows")).isEqualTo(Integer.toString(CHUNK_COUNT));
            } else {
                assertThat(header(page, "X-Nexus-Export-Table-Rows")).isNull();
            }
            total += Integer.parseInt(header(page, "X-Nexus-Export-Rows"));
            String last = header(page, "X-Nexus-Export-Last-Key");
            if (last != null) {
                assertThat(last).as("the cursor moves forward").isNotEqualTo(previous);
                previous = last;
            }
            if (Boolean.parseBoolean(header(page, "X-Nexus-Export-Done"))) break;
            after = last;
        }
        assertThat(total).isEqualTo(CHUNK_COUNT);
        assertThat(pages).as("a 16 KB target cuts 30 rows of about 3.3 KB into several pages").isGreaterThan(3);
    }

    @Test
    void laterWritesNeverReachLaterPages() throws Exception {
        var id = open(A_TOKEN).get("session_id").asText();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertMemory(DSL.using(su, SQLDialect.POSTGRES), A, "after-open-" + System.nanoTime());
        }
        var page = rowsBytes(A_TOKEN, id, rowsQuery("memory", null));
        assertThat(header(page, "X-Nexus-Export-Table-Rows")).as("the snapshot's five, not the table's six").isEqualTo("5");
        assertThat(header(page, "X-Nexus-Export-Rows")).isEqualTo("5");
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).deleteFrom(MEMORY).where(MEMORY.TITLE.like("after-open-%")).execute();
        }
    }

    // ── who may call (gate S4a/b) ────────────────────────────────────────────────────────────────

    @Test
    void theRootMintAndBoardCiScopesAreRefused() throws Exception {
        var root = call("POST", PATH, ROOT);
        assertThat(root.statusCode()).isEqualTo(403);
        assertThat(json(root).get("reason").asText()).isEqualTo("export_scope_forbidden");
        assertThat(call("POST", PATH, A_MINT_TOKEN).statusCode()).as("a mint token's own surface is the mint route").isEqualTo(403);
        assertThat(call("POST", PATH, null).statusCode()).isEqualTo(401);
        assertThat(call("POST", PATH, "tok-not-a-token").statusCode()).isEqualTo(401);
        // and a root token cannot read, close or probe a session either
        var id = open(A_TOKEN).get("session_id").asText();
        assertThat(rowsBytes(ROOT, id, rowsQuery("memory", null)).statusCode()).isEqualTo(403);
        assertThat(call("DELETE", PATH + "/" + id, ROOT).statusCode()).isEqualTo(403);
    }

    @Test
    void anotherTenantsTokenSeesNoSuchSessionExactlyAsForAnIdThatNeverExisted() throws Exception {
        var id = open(A_TOKEN).get("session_id").asText();

        var strangerRows = rowsBytes(B_TOKEN, id, rowsQuery("memory", null));
        var neverRows = rowsBytes(B_TOKEN, UNKNOWN_ID, rowsQuery("memory", null));
        assertThat(strangerRows.statusCode()).isEqualTo(404);
        assertThat(new String(strangerRows.body(), StandardCharsets.UTF_8))
            .isEqualTo(new String(neverRows.body(), StandardCharsets.UTF_8)).contains("no_such_session");
        var strangerDelete = call("DELETE", PATH + "/" + id, B_TOKEN);
        assertThat(strangerDelete.statusCode()).isEqualTo(404);
        assertThat(strangerDelete.body()).isEqualTo(call("DELETE", PATH + "/" + UNKNOWN_ID, B_TOKEN).body());
        // a stranger's table or cursor is not looked at before the session is: nothing about the route leaks
        assertThat(rowsBytes(B_TOKEN, id, "?table=nope&after=%25%25").statusCode()).isEqualTo(404);

        // the owner's session is untouched, and B's own session reads B's rows, not A's
        assertThat(rowsBytes(A_TOKEN, id, rowsQuery("memory", null)).statusCode()).isEqualTo(200);
        var bId = open(B_TOKEN).get("session_id").asText();
        assertThat(header(rowsBytes(B_TOKEN, bId, rowsQuery("memory", null)), "X-Nexus-Export-Table-Rows")).isEqualTo("3");
        assertThat(header(rowsBytes(B_TOKEN, bId, rowsQuery("chunks", null)), "X-Nexus-Export-Table-Rows")).isEqualTo("0");
        assertThat(header(rowsBytes(A_TOKEN, id, rowsQuery("memory", null)), "X-Nexus-Export-Table-Rows")).isEqualTo("5");
    }

    @Test
    void aFreshTokenOfTheSameTenantContinuesTheSessionAndADataTokenIsAnOwner() throws Exception {
        var id = open(A_TOKEN).get("session_id").asText();
        assertThat(rowsBytes(A_TOKEN_2, id, rowsQuery("memory", null)).statusCode()).isEqualTo(200);
        assertThat(rowsBytes(A_DATA_TOKEN, id, rowsQuery("memory", null)).statusCode()).isEqualTo(200);
        assertThat(call("DELETE", PATH + "/" + id, A_DATA_TOKEN).statusCode()).isEqualTo(200);
        assertThat(rowsBytes(A_TOKEN, id, rowsQuery("memory", null)).statusCode()).isEqualTo(404);
    }

    @Test
    void anExpiredTokenIsRefusedWithoutClosingTheSessionAndAFreshOneContinues() throws Exception {
        String shortLived = "tok-a-shortlived-0123456789abcdefghi";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), shortLived, A, "short-a",
                TokenStore.SCOPE_DATA, OffsetDateTime.now().plusSeconds(3), null);
        }
        var id = open(shortLived).get("session_id").asText();
        assertThat(rowsBytes(shortLived, id, rowsQuery("memory", null)).statusCode()).isEqualTo(200);

        long deadline = System.nanoTime() + Duration.ofSeconds(30).toNanos();
        int status;
        do {
            Thread.sleep(250);
            status = rowsBytes(shortLived, id, rowsQuery("memory", null)).statusCode();
        } while (status == 200 && System.nanoTime() < deadline);
        assertThat(status).as("the token expired").isEqualTo(401);

        assertThat(rowsBytes(A_TOKEN, id, rowsQuery("memory", null)).statusCode())
            .as("the session belongs to the tenant, so it outlived the token").isEqualTo(200);
    }

    // ── the table whitelist and the cursor (gate S4c/d) ──────────────────────────────────────────

    @Test
    void onlyARegistryDataOrDerivedTableIsReadAndTheNameIsNeverSql() throws Exception {
        var id = open(A_TOKEN).get("session_id").asText();
        List<String> refused = List.of("tuples", "tuple_claim_log", "t1.scratch", "pdf_pipeline", "request_ledger",
            "reembed_jobs", "service_tokens", "embedding_models", "nope", "MEMORY", "nexus.memory", "memory ",
            "memory; DROP TABLE nexus.memory", "memory\" , (select 1)", "memory--", "pg_catalog.pg_class", "", "%00");
        for (String table : refused) {
            var r = rowsBytes(A_TOKEN, id, rowsQuery(table, null));
            assertThat(r.statusCode()).as("table=" + table).isEqualTo(400);
            assertThat(new String(r.body(), StandardCharsets.UTF_8)).contains("unknown_table");
        }
        assertThat(rowsBytes(A_TOKEN, id, "").statusCode()).as("no table parameter").isEqualTo(400);
        // every exportable table is served
        int served = 0;
        for (var t : TenantExport.TABLES) {
            if (!t.exported()) continue;
            var r = rowsBytes(A_TOKEN, id, rowsQuery(t.name(), null));
            assertThat(r.statusCode()).as(t.name()).isEqualTo(200);
            served++;
        }
        assertThat(served).isGreaterThan(20);
        // and the table the injections named is still there
        assertThat(header(rowsBytes(A_TOKEN, id, rowsQuery("memory", null)), "X-Nexus-Export-Table-Rows")).isEqualTo("5");
    }

    @Test
    void aMalformedAfterIsA400NamingTheCursor() throws Exception {
        var id = open(A_TOKEN).get("session_id").asText();
        String chunksKeyOfWrongArity = TenantExportKeys.encode(List.of(A, COLLECTION));
        String numbers = java.util.Base64.getUrlEncoder().withoutPadding()
            .encodeToString("[1,2,3,4]".getBytes(StandardCharsets.UTF_8));
        String badHash = TenantExportKeys.encode(List.of(A, COLLECTION, "not-hex", MODEL));
        for (String after : List.of("%%%", "bm90IGpzb24", chunksKeyOfWrongArity, numbers, badHash, "")) {
            var r = rowsBytes(A_TOKEN, id, rowsQuery("chunks", after));
            assertThat(r.statusCode()).as("after=" + after).isEqualTo(400);
            assertThat(new String(r.body(), StandardCharsets.UTF_8)).contains("malformed_after");
        }
        // a memory key is one bigint
        var r = rowsBytes(A_TOKEN, id, rowsQuery("memory", TenantExportKeys.encode(List.of("1.5"))));
        assertThat(r.statusCode()).isEqualTo(400);
        // and a well-formed one beyond the table's last key is an empty, finished page
        var beyond = rowsBytes(A_TOKEN, id, rowsQuery("memory", TenantExportKeys.encode(List.of("9223372036854775807"))));
        assertThat(beyond.statusCode()).isEqualTo(200);
        assertThat(header(beyond, "X-Nexus-Export-Rows")).isEqualTo("0");
        assertThat(header(beyond, "X-Nexus-Export-Done")).isEqualTo("true");
    }

    @Test
    void routesRefuseTheWrongMethodAndAMalformedId() throws Exception {
        var id = open(A_TOKEN).get("session_id").asText();
        assertThat(call("GET", PATH, A_TOKEN).statusCode()).isEqualTo(405);
        assertThat(call("POST", PATH + "/" + id + "/rows?table=memory", A_TOKEN).statusCode()).isEqualTo(405);
        assertThat(call("GET", PATH + "/" + id, A_TOKEN).statusCode()).isEqualTo(405);
        assertThat(call("DELETE", PATH + "/not-a-uuid", A_TOKEN).statusCode()).isEqualTo(404);
        assertThat(call("GET", PATH + "/" + id + "/other", A_TOKEN).statusCode()).isEqualTo(404);
        assertThat(call("GET", PATH + "x", A_TOKEN).statusCode()).isEqualTo(404);
    }

    // ── close, caps and bounds ───────────────────────────────────────────────────────────────────

    @Test
    void deleteClosesTheSessionAndFreesTheTenantsSlot() throws Exception {
        var id = open(A_TOKEN).get("session_id").asText();
        var again = call("POST", PATH, A_TOKEN);
        assertThat(again.statusCode()).isEqualTo(409);
        assertThat(json(again).get("reason").asText()).isEqualTo("export_session_exists");
        assertThat(json(again).get("session_id").asText()).as("the tenant's own id, so a lost response can be closed")
            .isEqualTo(id);

        var closed = call("DELETE", PATH + "/" + id, A_TOKEN);
        assertThat(closed.statusCode()).isEqualTo(200);
        assertThat(json(closed).get("closed").asBoolean()).isTrue();
        assertThat(call("DELETE", PATH + "/" + id, A_TOKEN).statusCode()).isEqualTo(404);
        assertThat(rowsBytes(A_TOKEN, id, rowsQuery("memory", null)).statusCode()).isEqualTo(404);
        assertThat(open(A_TOKEN).get("session_id").asText()).isNotEqualTo(id);
    }

    @Test
    void aFullEngineRefusesWith503AndRetryAfterUntilASlotFrees() throws Exception {
        var one = new TenantExportSessions.Config(Duration.ofMinutes(60), Duration.ofMinutes(10), 1, 16 * 1024 * 1024,
            100_000, 5_000, 17, Duration.ofHours(1));
        service.exportSessions(new TenantExportSessions(ds, one, java.time.Clock.systemUTC()));
        var id = open(A_TOKEN).get("session_id").asText();

        var full = call("POST", PATH, B_TOKEN);
        assertThat(full.statusCode()).isEqualTo(503);
        assertThat(full.headers().firstValue("Retry-After")).contains("17");
        assertThat(json(full).get("reason").asText()).isEqualTo("export_capacity_full");
        assertThat(json(full).get("retry_after_seconds").asInt()).isEqualTo(17);

        assertThat(call("DELETE", PATH + "/" + id, A_TOKEN).statusCode()).isEqualTo(200);
        assertThat(open(B_TOKEN).get("tenant_id").asText()).isEqualTo(B);
    }

    @Test
    void theEngineClosesASessionAtTheIdleAndTotalBounds() throws Exception {
        var clock = new ExportTestClock(Instant.parse("2026-11-12T00:00:00Z"));
        service.exportSessions(new TenantExportSessions(ds, TenantExportSessions.Config.defaults(), clock));

        var idle = open(A_TOKEN).get("session_id").asText();
        clock.advance(Duration.ofMinutes(9));
        assertThat(rowsBytes(A_TOKEN, idle, rowsQuery("memory", null)).statusCode()).isEqualTo(200);
        clock.advance(Duration.ofMinutes(10).plusSeconds(1));
        assertThat(rowsBytes(A_TOKEN, idle, rowsQuery("memory", null)).statusCode()).as("10 minutes idle").isEqualTo(404);

        var busy = open(A_TOKEN).get("session_id").asText();
        for (int i = 0; i < 6; i++) {
            clock.advance(Duration.ofMinutes(9));
            assertThat(rowsBytes(A_TOKEN, busy, rowsQuery("memory", null)).statusCode()).isEqualTo(200);
        }
        clock.advance(Duration.ofMinutes(6));
        assertThat(rowsBytes(A_TOKEN, busy, rowsQuery("memory", null)).statusCode()).as("60 minutes old").isEqualTo(404);
    }

    @Test
    void aSnapshotThatEndedOutsideTheEngineAnswers410AndFreesTheSlot() throws Exception {
        var id = open(A_TOKEN).get("session_id").asText();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // the database's own idle-in-transaction timeout would do this: the held backend goes away
            var pids = ctx.select(DSL.field(DSL.name("pid"), Integer.class))
                .from(DSL.table(DSL.name("pg_stat_activity")))
                .where(DSL.field(DSL.name("query"), String.class).likeIgnoreCase("%pg_export_snapshot%"))
                .and(DSL.field(DSL.name("state"), String.class).eq("idle in transaction"))
                .fetch(DSL.field(DSL.name("pid"), Integer.class));
            assertThat(pids).hasSize(1);
            ctx.select(DSL.function("pg_terminate_backend", Boolean.class, DSL.val(pids.get(0)))).fetch();
        }
        var gone = rowsBytes(A_TOKEN, id, rowsQuery("memory", null));
        assertThat(gone.statusCode()).isEqualTo(410);
        assertThat(new String(gone.body(), StandardCharsets.UTF_8)).contains("session_closed");
        assertThat(rowsBytes(A_TOKEN, id, rowsQuery("memory", null)).statusCode()).as("then it is simply gone").isEqualTo(404);
        assertThat(open(A_TOKEN).get("session_id").asText()).as("the slot is free").isNotEqualTo(id);
    }

    @Test
    void theTenantResetEvictorClosesTheTenantsSession() throws Exception {
        open(A_TOKEN);
        var evictor = service.tenantKeyedState().stream().filter(s -> s.name().equals("TenantExportSessions")).findFirst();
        assertThat(evictor).isPresent();
        assertThat(evictor.get().evictTenant(B)).isZero();
        assertThat(evictor.get().evictTenant(A)).isEqualTo(1);
        assertThat(call("POST", PATH, A_TOKEN).statusCode()).isEqualTo(200);
    }
}
