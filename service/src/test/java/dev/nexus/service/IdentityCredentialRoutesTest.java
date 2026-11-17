// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TokenStore;
import dev.nexus.service.tuples.TemplateRegistry;
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

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-253 Step 1 (nexus-hczdo.1.1): each {@code /v1/identity} route admits exactly the credential that needs it,
 * through the real {@link NexusService} (the auth filter and the real token scopes). The hook, {@code nx identity}
 * and the dispatcher hold the tenant credential; the MCP server holds the data credential and may renew, refresh,
 * end, send the fork row, make the pending bind and keep memberships, and nothing else. Purge, creation, promotion, rename and the
 * {@code gone} verdict are tenant-only, a data credential may not send take-over, release or clear, and a mint or
 * board-ci credential reaches none of it.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class IdentityCredentialRoutesTest {

    private static final String BOOT = "boot-identity-credential-test";
    private static final String TENANT_TOKEN = "identity-tenant-token";
    private static final String DATA_TOKEN = "identity-data-token";
    private static final String MINT_TOKEN = "identity-mint-token";
    private static final String BOARD_CI_TOKEN = "identity-board-ci-token";
    private static final String TENANT_NAME = "identity-credential-tenant";
    private static final ObjectMapper MAPPER = new ObjectMapper();

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    NexusService service;
    int port;
    final HttpClient http = TestHttp.client();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(dsl, BOOT, TenantConstants.DEFAULT_TENANT,
                TokenStore.ROOT_TOKEN_LABEL, TokenStore.SCOPE_ROOT, null, null);
            PgContainerHelper.seedServiceToken(dsl, TENANT_TOKEN, TENANT_NAME, "tenant", TokenStore.SCOPE_TENANT, null, null);
            PgContainerHelper.seedServiceToken(dsl, DATA_TOKEN, TENANT_NAME, "data", TokenStore.SCOPE_DATA, null, null);
            PgContainerHelper.seedServiceToken(dsl, MINT_TOKEN, TENANT_NAME, "mint", TokenStore.SCOPE_MINT, null, null);
            PgContainerHelper.seedServiceToken(dsl, BOARD_CI_TOKEN, TENANT_NAME, "ci", TokenStore.SCOPE_BOARD_CI, null, null);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
        TemplateRegistry registry = TemplateRegistry.loadAtBoot(null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L);
        service = new NexusService(0, BOOT, ds, null, null, null, null, registry);
        service.start();
        port = service.getPort();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    private HttpResponse<String> send(String bearer, String method, String path, Object body) throws Exception {
        var b = TestHttp.request("http://127.0.0.1:" + port + "/v1/identity" + path)
            .header("Authorization", "Bearer " + bearer).header("Content-Type", "application/json");
        var req = "GET".equals(method) ? b.GET().build()
            : b.method(method, HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))).build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> json(HttpResponse<String> r) throws Exception {
        return MAPPER.readValue(r.body(), Map.class);
    }

    private static Map<String, Object> evidence(String session, int pid) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("session_id", session);
        m.put("host", "h1");
        m.put("pid", pid);
        m.put("started_at", 1000L + pid);
        m.put("proc_start", "2026-11-16T10:00:00Z");
        return m;
    }

    /** One route to try: its description, method, path and body, and the scopes that must reach it. */
    record Probe(String name, String method, String path, Object body, boolean dataAllowed) { }

    private static List<Probe> probes() {
        Map<String, Object> bind = evidence("probe", 7);
        List<Probe> p = new ArrayList<>();
        p.add(new Probe("create", "POST", "", Map.of("kind", "seat", "name", "probe", "created_by", "sam"), false));
        p.add(new Probe("delegated", "POST", "/delegated", Map.of("parent", "i999999", "bind", bind), false));
        p.add(new Probe("rename", "POST", "/rename", Map.of("old", "nobody", "new", "somebody"), false));
        p.add(new Probe("promote", "POST", "/i999999/promote", Map.of("name", "x"), false));
        p.add(new Probe("gone", "POST", "/i999999/gone", Map.of("expected_generation", 1,
            "holder_gone", Map.of("host", "h1", "pid", 1, "started_at", 1)), false));
        p.add(new Probe("purge", "POST", "/i999999/purge", Map.of("operator", true), false));
        for (String event : new String[] {"takeover", "release", "clear"}) {
            Map<String, Object> b = new LinkedHashMap<>(bind);
            b.put("event", event);
            b.put("expected_generation", 0);
            p.add(new Probe("bind " + event, "POST", "/i999999/bind", b, false));
        }
        for (String event : new String[] {"start", "resume", "fork", "refresh", "end"}) {
            Map<String, Object> b = new LinkedHashMap<>(bind);
            b.put("event", event);
            b.put("expected_generation", 0);
            p.add(new Probe("bind " + event, "POST", "/i999999/bind", b, true));
        }
        p.add(new Probe("renew", "POST", "/i999999/renew", Map.of("generation", 1), true));
        p.add(new Probe("show", "GET", "/i999999", null, true));
        p.add(new Probe("log", "GET", "/i999999/bindings", null, true));
        p.add(new Probe("alias", "GET", "/alias/nobody", null, true));
        p.add(new Probe("session", "GET", "/sessions/nobody", null, true));
        p.add(new Probe("by host", "GET", "/bindings?host=h1", null, true));
        p.add(new Probe("join", "POST", "/i999999/memberships", Map.of("group", "crew", "role", "member"), true));
        p.add(new Probe("leave", "DELETE", "/i999999/memberships/crew", null, true));
        p.add(new Probe("groups of", "GET", "/i999999/memberships", null, true));
        p.add(new Probe("roster", "GET", "/groups/crew", null, true));
        return p;
    }

    @Test
    void eachRouteAdmitsExactlyTheCredentialsThatNeedIt() throws Exception {
        int examined = 0;
        List<String> wrong = new ArrayList<>();
        for (Probe p : probes()) {
            int tenant = send(TENANT_TOKEN, p.method(), p.path(), p.body()).statusCode();
            int root = send(BOOT, p.method(), p.path(), p.body()).statusCode();
            int data = send(DATA_TOKEN, p.method(), p.path(), p.body()).statusCode();
            int mint = send(MINT_TOKEN, p.method(), p.path(), p.body()).statusCode();
            int boardCi = send(BOARD_CI_TOKEN, p.method(), p.path(), p.body()).statusCode();
            examined++;
            if (tenant == 403) wrong.add(p.name() + ": the tenant credential was refused");
            if (root == 403) wrong.add(p.name() + ": the root credential was refused");
            if (p.dataAllowed() && data == 403) wrong.add(p.name() + ": the data credential was refused");
            if (!p.dataAllowed() && data != 403) wrong.add(p.name() + ": the data credential got " + data + ", not 403");
            if (mint != 403) wrong.add(p.name() + ": a mint credential got " + mint);
            if (boardCi != 403) wrong.add(p.name() + ": a board-ci credential got " + boardCi);
        }
        assertThat(examined).as("non-vacuity: routes examined").isGreaterThanOrEqualTo(23);
        assertThat(wrong).isEmpty();
    }

    @Test
    void anUnauthenticatedCallIsRefused() throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + port + "/v1/identity/i1").GET().build();
        assertThat(http.send(req, HttpResponse.BodyHandlers.ofString()).statusCode()).isIn(401, 403);
    }

    @Test
    void theMcpServersCallsWorkOnTheDataCredentialAndPurgeStaysTenantOnly() throws Exception {
        var created = send(TENANT_TOKEN, "POST", "", Map.of("kind", "seat", "name", "credseat", "created_by", "sam"));
        assertThat(created.statusCode()).isEqualTo(200);
        String id = (String) json(created).get("id");

        // The hook binds with the tenant credential; the MCP server then renews, refreshes and ends with its own.
        Map<String, Object> start = new LinkedHashMap<>(evidence("s1", 11));
        start.put("event", "start");
        start.put("expected_generation", 0);
        assertThat(send(TENANT_TOKEN, "POST", "/" + id + "/bind", start).statusCode()).isEqualTo(200);
        assertThat(send(DATA_TOKEN, "POST", "/" + id + "/renew", Map.of("generation", 1, "client_version", "7.99.0"))
            .statusCode()).isEqualTo(200);
        assertThat(send(DATA_TOKEN, "POST", "/" + id + "/bind", Map.of("event", "refresh", "expected_generation", 1))
            .statusCode()).isEqualTo(200);
        Map<String, Object> fork = new LinkedHashMap<>();
        fork.put("event", "fork");
        fork.put("expected_generation", 1);
        fork.put("session_id", "s1-fork");
        assertThat(send(DATA_TOKEN, "POST", "/" + id + "/bind", fork).statusCode()).isEqualTo(200);
        assertThat(send(DATA_TOKEN, "GET", "/sessions/s1-fork", null).statusCode()).isEqualTo(200);
        assertThat(send(DATA_TOKEN, "POST", "/" + id + "/bind", Map.of("event", "end", "expected_generation", 1))
            .statusCode()).isEqualTo(200);

        var viaData = send(DATA_TOKEN, "POST", "/" + id + "/purge", Map.of("operator", true));
        assertThat(viaData.statusCode()).isEqualTo(403);
        assertThat(send(TENANT_TOKEN, "POST", "/" + id + "/purge", Map.of("operator", true)).statusCode()).isEqualTo(200);
    }
}
