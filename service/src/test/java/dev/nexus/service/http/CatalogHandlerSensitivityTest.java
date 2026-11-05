// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-rfvyy.6.5 (RDR-243 Phase 1 Step 2c): the audited sensitivity route and the wire.
 *
 * <p>{@code POST /v1/catalog/sensitivity {tumblers, sensitive, reason}}: setting needs no reason, clearing
 * refuses (400, before any write) without a non-blank one, and every change writes an audit row with the actor
 * derived from the request's principal. The general update route still refuses the column (400 single, -1 per
 * entry in a batch). The document JSON carries {@code sensitive} on every reader, and
 * {@code /manifest/chash_positions} carries it per (chash, doc_id) row. Runs on PostgreSQL as the NOBYPASSRLS
 * service role; flag and audit rows are read back over a superuser connection with plain SQL.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerSensitivityTest {

    private static final String SVC_ROLE = "svc_cat_sens_http";
    private static final String SVC_PASS = "svc_cat_sens_http_pass";
    private static final String OWNER = "49";
    private static final String COLL = "knowledge__senshttp__minilm-l6-v2-384__v1";
    private static final ObjectMapper MAPPER = new ObjectMapper();

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    CatalogRepository repo;
    CatalogHandler handler;
    com.zaxxer.hikari.HikariDataSource svcDs;
    int tenantSeq = 0;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        repo = new CatalogRepository(tenantScope);
        handler = new CatalogHandler(repo);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private String tenant() {
        return "senshttp-" + (++tenantSeq);
    }

    private String register(String tenant, String name, boolean sensitive) {
        var f = new LinkedHashMap<String, Object>();
        f.put("title", name);
        f.put("file_path", name + ".md");
        f.put("source_uri", "file:///r/" + name + ".md");
        f.put("physical_collection", COLL);
        if (sensitive) f.put("sensitive", true);
        return repo.registerDocument(tenant, OWNER, f);
    }

    // ── the audited route ───────────────────────────────────────────────

    @Test
    void clearWithoutAReason_is400_beforeAnyWrite_andABlankReasonIsTheSame() throws Exception {
        String t = tenant();
        String doc = register(t, "a", true);
        for (String body : List.of(
                "{\"tumblers\":[\"" + doc + "\"],\"sensitive\":false}",
                "{\"tumblers\":[\"" + doc + "\"],\"sensitive\":false,\"reason\":\"   \"}",
                "{\"tumblers\":[\"" + doc + "\"],\"sensitive\":false,\"reason\":null}")) {
            Resp r = call(t, "POST", "/v1/catalog/sensitivity", body, null);
            assertThat(r.status).as(r.body).isEqualTo(400);
            assertThat(r.json().get("error").asText()).containsIgnoringCase("reason");
        }
        assertThat(flag(t, doc)).as("nothing was written").isTrue();
        assertThat(audit(t)).isEmpty();
    }

    @Test
    void clearWithAReason_writesTheFlagAndOneAuditRowWithTheActor() throws Exception {
        String t = tenant();
        String doc = register(t, "b", true);
        Resp r = call(t, "POST", "/v1/catalog/sensitivity",
            "{\"tumblers\":[\"" + doc + "\"],\"sensitive\":false,\"reason\":\"reviewed with legal 2026-11-03\"}", "agent-7");
        assertThat(r.status).as(r.body).isEqualTo(200);
        assertThat(r.json().get("updated").get(0).asInt()).isEqualTo(1);
        assertThat(flag(t, doc)).isFalse();
        assertThat(audit(t)).containsExactly(doc + "|true|false|reviewed with legal 2026-11-03|agent-7");

        // clearing again is a no-op: the row is already in that state, so no second audit row
        Resp again = call(t, "POST", "/v1/catalog/sensitivity",
            "{\"tumblers\":[\"" + doc + "\"],\"sensitive\":false,\"reason\":\"again\"}", "agent-7");
        assertThat(again.status).isEqualTo(200);
        assertThat(again.json().get("updated").get(0).asInt()).isZero();
        assertThat(audit(t)).hasSize(1);
    }

    @Test
    void setNeedsNoReason_writesTheFlagAndAnAuditRow_andTheActorFallsBackToTheScope() throws Exception {
        String t = tenant();
        String doc = register(t, "c", false);
        Resp r = call(t, "POST", "/v1/catalog/sensitivity",
            "{\"tumblers\":[\"" + doc + "\"],\"sensitive\":true}", null);
        assertThat(r.status).as(r.body).isEqualTo(200);
        assertThat(r.json().get("updated").get(0).asInt()).isEqualTo(1);
        assertThat(flag(t, doc)).isTrue();
        assertThat(audit(t)).hasSize(1);
        assertThat(audit(t).get(0)).startsWith(doc + "|false|true|").endsWith("|tenant")
            .as("no session on the principal: the actor is its scope").isNotBlank();

        Resp again = call(t, "POST", "/v1/catalog/sensitivity", "{\"tumblers\":[\"" + doc + "\"],\"sensitive\":true}", null);
        assertThat(again.json().get("updated").get(0).asInt()).as("already set: no change").isZero();
        assertThat(audit(t)).hasSize(1);
    }

    @Test
    void aBatch_isPositionalAndSkipsUnknownAndTombstonedTumblers() throws Exception {
        String t = tenant();
        String live = register(t, "d1", false);
        String dead = register(t, "d2", false);
        repo.deleteDocument(t, dead);
        String already = register(t, "d3", true);
        Resp r = call(t, "POST", "/v1/catalog/sensitivity",
            "{\"tumblers\":[\"" + live + "\",\"" + dead + "\",\"" + already + "\",\"nope.1\",\"" + live + "\"],\"sensitive\":true}", "agent-7");
        assertThat(r.status).as(r.body).isEqualTo(200);
        List<Integer> updated = new ArrayList<>();
        r.json().get("updated").forEach(n -> updated.add(n.asInt()));
        assertThat(updated).as("live +1, tombstoned 0, already set 0, unknown 0, a repeat in the same call 0")
            .containsExactly(1, 0, 0, 0, 0);
        assertThat(flag(t, dead)).as("a tombstoned row is not touched").isFalse();
        assertThat(audit(t)).hasSize(1);
    }

    @Test
    void withNoPrincipalAtAll_aBodyActorApplies_elseOperator_andAPrincipalIgnoresTheBodyActor() throws Exception {
        String t = tenant();
        String named = register(t, "p1", false);
        String anon = register(t, "p2", false);
        String guarded = register(t, "p3", false);

        // a direct handler call outside AuthFilter: no Principal on the thread
        Resp withActor = callWithoutPrincipal(t,
            "{\"tumblers\":[\"" + named + "\"],\"sensitive\":true,\"actor\":\"alice\"}");
        assertThat(withActor.status).as(withActor.body).isEqualTo(200);
        Resp noActor = callWithoutPrincipal(t, "{\"tumblers\":[\"" + anon + "\"],\"sensitive\":true}");
        assertThat(noActor.status).as(noActor.body).isEqualTo(200);
        assertThat(audit(t)).containsExactly(
            named + "|false|true|set|alice",
            anon + "|false|true|set|operator");

        // an authenticated caller cannot name someone else
        Resp withPrincipal = call(t, "POST", "/v1/catalog/sensitivity",
            "{\"tumblers\":[\"" + guarded + "\"],\"sensitive\":true,\"actor\":\"mallory\"}", "agent-9");
        assertThat(withPrincipal.status).as(withPrincipal.body).isEqualTo(200);
        assertThat(audit(t)).last().isEqualTo(guarded + "|false|true|set|agent-9");
    }

    @Test
    void aNonBooleanSensitive_onRegisterDocRegisterManyAndImport_is400_andWritesNothing() throws Exception {
        String t = tenant();
        for (String bad : List.of("1", "\"yes\"", "\"true\"", "[true]")) {
            for (String[] route : new String[][] {
                {"/v1/catalog/register", "{\"tumbler\":\"49.1.77\",\"title\":\"s\",\"sensitive\":" + bad + "}"},
                {"/v1/catalog/doc/register", "{\"owner_prefix\":\"49.1\",\"title\":\"s\",\"file_path\":\"s.md\",\"sensitive\":" + bad + "}"},
                {"/v1/catalog/doc/register_many", "{\"owner_prefix\":\"49.1\",\"docs\":[{\"title\":\"s\",\"file_path\":\"s.md\",\"sensitive\":" + bad + "}]}"},
                {"/v1/catalog/import/document", "{\"tumbler\":\"49.1.78\",\"title\":\"s\",\"sensitive\":" + bad + "}"},
                {"/v1/catalog/import/document", "{\"rows\":[{\"tumbler\":\"49.1.79\",\"title\":\"s\",\"sensitive\":" + bad + "}]}"}}) {
                Resp r = call(t, "POST", route[0], route[1], null);
                assertThat(r.status).as("%s with sensitive=%s -> %s", route[0], bad, r.body).isEqualTo(400);
                assertThat(r.json().get("error").asText()).contains("'sensitive' must be a boolean");
            }
        }
        try (Connection su = pg.createConnection("")) {
            assertThat(DSL.using(su, SQLDialect.POSTGRES).fetchOne(
                "SELECT count(*)::int FROM nexus.catalog_documents WHERE tenant_id = ?", t).get(0, Integer.class))
                .as("no refused request wrote a row").isZero();
        }
        // real booleans and null still pass
        assertThat(call(t, "POST", "/v1/catalog/register",
            "{\"tumbler\":\"49.1.80\",\"title\":\"ok\",\"sensitive\":true}", null).status).isEqualTo(200);
        assertThat(call(t, "POST", "/v1/catalog/register",
            "{\"tumbler\":\"49.1.81\",\"title\":\"ok\",\"sensitive\":null}", null).status).isEqualTo(200);
        assertThat(flag(t, "49.1.80")).isTrue();
        assertThat(flag(t, "49.1.81")).isFalse();
    }

    @Test
    void aNonBooleanSensitive_onRegisterWithAnOwnerPrefix_writesNeitherTheOwnerNorTheDocument() throws Exception {
        String t = tenant();
        for (String bad : List.of("1", "\"yes\"", "[true]")) {
            Resp r = call(t, "POST", "/v1/catalog/register",
                "{\"tumbler_prefix\":\"49.2\",\"name\":\"owner-x\",\"owner_type\":\"repo\","
                    + "\"tumbler\":\"49.2.1\",\"title\":\"s\",\"sensitive\":" + bad + "}", null);
            assertThat(r.status).as("sensitive=%s -> %s", bad, r.body).isEqualTo(400);
            assertThat(r.json().get("error").asText()).contains("'sensitive' must be a boolean");
        }
        try (Connection su = pg.createConnection("")) {
            var d = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(d.fetchOne("SELECT count(*)::int FROM nexus.catalog_owners WHERE tenant_id = ?", t)
                .get(0, Integer.class)).as("the owner row is written only after the flag validates").isZero();
            assertThat(d.fetchOne("SELECT count(*)::int FROM nexus.catalog_documents WHERE tenant_id = ?", t)
                .get(0, Integer.class)).as("no document row").isZero();
        }
        // the same body with a real boolean writes both
        Resp ok = call(t, "POST", "/v1/catalog/register",
            "{\"tumbler_prefix\":\"49.2\",\"name\":\"owner-x\",\"owner_type\":\"repo\","
                + "\"tumbler\":\"49.2.1\",\"title\":\"s\",\"sensitive\":true}", null);
        assertThat(ok.status).as(ok.body).isEqualTo(200);
        assertThat(flag(t, "49.2.1")).isTrue();
        try (Connection su = pg.createConnection("")) {
            assertThat(DSL.using(su, SQLDialect.POSTGRES).fetchOne(
                "SELECT count(*)::int FROM nexus.catalog_owners WHERE tenant_id = ?", t).get(0, Integer.class)).isOne();
        }
    }

    @Test
    void badRequests_areRefused() throws Exception {
        String t = tenant();
        String doc = register(t, "e", false);
        assertThat(call(t, "GET", "/v1/catalog/sensitivity", "", null).status).isEqualTo(405);
        for (String body : List.of(
                "{\"sensitive\":true}",                                              // no tumblers
                "{\"tumblers\":[],\"sensitive\":true}",                              // empty
                "{\"tumblers\":\"" + doc + "\",\"sensitive\":true}",                 // not a list
                "{\"tumblers\":[1],\"sensitive\":true}",                             // not strings
                "{\"tumblers\":[\"" + doc + "\"]}",                                  // no sensitive
                "{\"tumblers\":[\"" + doc + "\"],\"sensitive\":\"true\"}")) {        // not a boolean
            Resp r = call(t, "POST", "/v1/catalog/sensitivity", body, null);
            assertThat(r.status).as("%s -> %s", body, r.body).isEqualTo(400);
        }
        var many = new StringBuilder("{\"sensitive\":true,\"tumblers\":[");
        for (int i = 0; i <= 1000; i++) many.append(i == 0 ? "" : ",").append("\"x.").append(i).append('"');
        many.append("]}");
        assertThat(call(t, "POST", "/v1/catalog/sensitivity", many.toString(), null).status)
            .as("1001 tumblers: over the handler's MAX_BATCH_DOC_IDS of 1000").isEqualTo(400);
        assertThat(flag(t, doc)).isFalse();
    }

    // ── the general update route ────────────────────────────────────────

    @Test
    void theGeneralUpdateRoutes_refuseTheColumn() throws Exception {
        String t = tenant();
        String doc = register(t, "f", false);
        Resp one = call(t, "POST", "/v1/catalog/update", "{\"tumbler\":\"" + doc + "\",\"sensitive\":true}", null);
        assertThat(one.status).as(one.body).isEqualTo(400);
        assertThat(one.json().get("error").asText()).contains("sensitive");

        Resp many = call(t, "POST", "/v1/catalog/update_many",
            "{\"updates\":[{\"tumbler\":\"" + doc + "\",\"sensitive\":true}]}", null);
        assertThat(many.status).as(many.body).isEqualTo(200);
        assertThat(many.json().get("updated").get(0).asInt()).as("the entry is marked malformed").isEqualTo(-1);
        assertThat(flag(t, doc)).as("neither route moved the flag").isFalse();
    }

    // ── the wire ────────────────────────────────────────────────────────

    @Test
    void documentJson_carriesSensitive_onShowAndList() throws Exception {
        String t = tenant();
        String flagged = register(t, "g1", true);
        String plain = register(t, "g2", false);
        JsonNode showFlagged = call(t, "GET", "/v1/catalog/show?tumbler=" + flagged, "", null).json();
        JsonNode showPlain = call(t, "GET", "/v1/catalog/show?tumbler=" + plain, "", null).json();
        assertThat(showFlagged.get("sensitive").isBoolean()).isTrue();
        assertThat(showFlagged.get("sensitive").asBoolean()).isTrue();
        assertThat(showPlain.get("sensitive").asBoolean()).isFalse();

        JsonNode list = call(t, "GET", "/v1/catalog/list?limit=50", "", null).json();
        Map<String, Boolean> byTumbler = new LinkedHashMap<>();
        list.get("documents").forEach(d -> byTumbler.put(d.get("tumbler").asText(), d.get("sensitive").asBoolean()));
        assertThat(byTumbler).containsEntry(flagged, true).containsEntry(plain, false);
    }

    @Test
    void chashPositions_carriesSensitivePerChashAndDocument() throws Exception {
        String t = tenant();
        String shared = hex("shared-" + t);
        String solo = hex("solo-" + t);
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, t, COLL);
            PgContainerHelper.insertChunks(dsl, t, COLL, List.of(shared, solo), List.of("s", "o"),
                List.of(new float[384], new float[384]), List.of(Map.<String, Object>of(), Map.<String, Object>of()));
        }
        String sensitiveDoc = register(t, "h1", true);
        String plainDoc = register(t, "h2", false);
        repo.writeManifest(t, sensitiveDoc, COLL, List.of(
            Map.<String, Object>of("position", 0, "chash", shared, "chunk_index", 0)));
        repo.writeManifest(t, plainDoc, COLL, List.of(
            Map.<String, Object>of("position", 0, "chash", shared, "chunk_index", 0),
            Map.<String, Object>of("position", 1, "chash", solo, "chunk_index", 1)));

        Resp r = call(t, "POST", "/v1/catalog/manifest/chash_positions",
            "{\"chashes\":[\"" + shared + "\",\"" + solo + "\"]}", null);
        assertThat(r.status).as(r.body).isEqualTo(200);
        Map<String, Boolean> got = new LinkedHashMap<>();
        r.json().get("rows").forEach(row -> {
            assertThat(row.get("sensitive").isBoolean()).as("every row carries the key: %s", row).isTrue();
            got.put(row.get("chash").asText() + "@" + row.get("doc_id").asText(), row.get("sensitive").asBoolean());
        });
        assertThat(got).as("per (chash, doc_id): the same shared chunk is sensitive under one owner and not the other")
            .containsEntry(shared + "@" + sensitiveDoc, true)
            .containsEntry(shared + "@" + plainDoc, false)
            .containsEntry(solo + "@" + plainDoc, false)
            .hasSize(3);

        // the flag is read live: clear it and the same call answers false
        call(t, "POST", "/v1/catalog/sensitivity",
            "{\"tumblers\":[\"" + sensitiveDoc + "\"],\"sensitive\":false,\"reason\":\"test\"}", null);
        Resp after = call(t, "POST", "/v1/catalog/manifest/chash_positions", "{\"chashes\":[\"" + shared + "\"]}", null);
        after.json().get("rows").forEach(row -> assertThat(row.get("sensitive").asBoolean()).isFalse());
    }

    // ── two tenants ─────────────────────────────────────────────────────

    @Test
    void twoTenants_oneTenantsAuditRowsAndFlagStayOutOfTheOthersReads() throws Exception {
        String a = tenant();
        String b = tenant();
        String docA = register(a, "t1", false);
        register(b, "t1", false);
        call(a, "POST", "/v1/catalog/sensitivity", "{\"tumblers\":[\"" + docA + "\"],\"sensitive\":true}", null);

        int rowsSeenByB = tenantScope.withTenant(b, ctx -> ctx.fetchOne(
            "SELECT count(*)::int FROM nexus.catalog_sensitivity_changes").get(0, Integer.class));
        int rowsSeenByA = tenantScope.withTenant(a, ctx -> ctx.fetchOne(
            "SELECT count(*)::int FROM nexus.catalog_sensitivity_changes").get(0, Integer.class));
        assertThat(rowsSeenByA).isEqualTo(1);
        assertThat(rowsSeenByB).as("RLS keeps A's audit rows out of B's reads").isZero();

        Resp crossTenant = call(b, "POST", "/v1/catalog/sensitivity",
            "{\"tumblers\":[\"" + docA + "\"],\"sensitive\":false,\"reason\":\"x\"}", null);
        assertThat(crossTenant.json().get("updated").get(0).asInt())
            .as("tenant B cannot clear tenant A's document: the tumbler is not visible to it").isZero();
        assertThat(flag(a, docA)).isTrue();
    }

    // ── helpers ─────────────────────────────────────────────────────────

    private static String hex(String seed) {
        try {
            var md = java.security.MessageDigest.getInstance("SHA-256");
            var out = new StringBuilder();
            for (byte x : md.digest(seed.getBytes(StandardCharsets.UTF_8))) out.append(String.format("%02x", x));
            return out.toString();
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private boolean flag(String tenant, String tumbler) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var r = DSL.using(su, SQLDialect.POSTGRES).fetchOne(
                "SELECT sensitive FROM nexus.catalog_documents WHERE tenant_id = ? AND tumbler = ?", tenant, tumbler);
            assertThat(r).as("row %s/%s exists", tenant, tumbler).isNotNull();
            return r.get(0, Boolean.class);
        }
    }

    /** Audit rows as {@code tumbler|old|new|reason|actor}, oldest first. */
    private List<String> audit(String tenant) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var out = new ArrayList<String>();
            for (var r : DSL.using(su, SQLDialect.POSTGRES).fetch(
                    "SELECT tumbler, old, new, reason, actor FROM nexus.catalog_sensitivity_changes "
                        + "WHERE tenant_id = ? ORDER BY id", tenant)) {
                out.add(r.get(0) + "|" + r.get(1) + "|" + r.get(2) + "|" + r.get(3) + "|" + r.get(4));
            }
            return out;
        }
    }

    private record Resp(int status, String body) {
        JsonNode json() throws Exception { return MAPPER.readTree(body); }
    }

    /** A direct handler call with NO principal on the thread (outside AuthFilter): only the sensitivity op. */
    private Resp callWithoutPrincipal(String tenant, String body) throws Exception {
        CapturingExchange ex = new CapturingExchange("POST", URI.create("/v1/catalog/sensitivity"), body);
        RequestContext.clear();
        handler.handleSensitivity(ex, tenant, "POST");
        return new Resp(ex.status, ex.bodyString());
    }

    private Resp call(String tenant, String method, String pathAndQuery, String body, String session) throws Exception {
        CapturingExchange ex = new CapturingExchange(method, URI.create(pathAndQuery), body);
        RequestContext.set(new RequestContext.Principal(tenant, session, false, false, "tenant", "test-credential-hash"));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }
        return new Resp(ex.status, ex.bodyString());
    }

    /** Minimal {@link HttpExchange} that captures the response status + body. */
    private static final class CapturingExchange extends HttpExchange {
        private final String method;
        private final URI uri;
        private final InputStream requestBody;
        private final Headers responseHeaders = new Headers();
        private final ByteArrayOutputStream responseBody = new ByteArrayOutputStream();
        int status = -1;

        CapturingExchange(String method, URI uri, String body) {
            this.method = method;
            this.uri = uri;
            this.requestBody = new ByteArrayInputStream(body.getBytes(StandardCharsets.UTF_8));
        }

        String bodyString() { return responseBody.toString(StandardCharsets.UTF_8); }

        @Override public Headers getRequestHeaders() { return new Headers(); }
        @Override public Headers getResponseHeaders() { return responseHeaders; }
        @Override public URI getRequestURI() { return uri; }
        @Override public String getRequestMethod() { return method; }
        @Override public HttpContext getHttpContext() { return null; }
        @Override public void close() {}
        @Override public InputStream getRequestBody() { return requestBody; }
        @Override public OutputStream getResponseBody() { return responseBody; }
        @Override public void sendResponseHeaders(int rCode, long responseLength) { this.status = rCode; }
        @Override public InetSocketAddress getRemoteAddress() { return null; }
        @Override public int getResponseCode() { return status; }
        @Override public InetSocketAddress getLocalAddress() { return null; }
        @Override public String getProtocol() { return "HTTP/1.1"; }
        @Override public Object getAttribute(String name) { return null; }
        @Override public void setAttribute(String name, Object value) {}
        @Override public void setStreams(InputStream i, OutputStream o) {}
        @Override public HttpPrincipal getPrincipal() { return null; }
    }
}
