// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TokenStore;
import dev.nexus.service.tuples.TemplateRegistry;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-253 Step 2 test harness: the real {@link NexusService} (the real {@code AuthFilter}, the real identity tokens
 * and the real handlers) over a real PostgreSQL, with a tenant credential, a data credential and a second tenant's
 * credential seeded. A test creates seats and binds them over HTTP the way the hook and the MCP server do, keeps the
 * {@code identity_token} each bind returns, and sends it in {@code X-Nexus-Identity-Token}. The service runs on the
 * system clock; a test that needs an expired token moves the token's {@code expires_at} in the table.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
abstract class IdentityFenceHarness {

    protected static final String BOOT = "boot-identity-fence-test";
    protected static final String TENANT_TOKEN = "identity-fence-tenant-token";
    protected static final String DATA_TOKEN = "identity-fence-data-token";
    protected static final String OTHER_TENANT_TOKEN = "identity-fence-other-tenant-token";
    protected static final String TENANT = "identity-fence-tenant";
    protected static final String OTHER_TENANT = "identity-fence-other-tenant";
    protected static final ObjectMapper MAPPER = new ObjectMapper();
    protected static final String ID_HEADER = "X-Nexus-Identity-Token";

    protected PostgreSQLContainer<?> pg;
    protected HikariDataSource ds;
    protected TenantScope scope;
    protected NexusService service;
    protected int port;
    protected final HttpClient http = TestHttp.client();

    /** One response: status, parsed JSON body (null when empty), and the headers. */
    protected record Resp(int status, JsonNode json, java.net.http.HttpHeaders headers, String raw) {
        String error() {
            return json == null || json.get("error") == null ? null : json.get("error").asText();
        }

        String text(String field) {
            return json == null || json.get(field) == null || json.get(field).isNull() ? null : json.get(field).asText();
        }

        boolean unattested() {
            return headers.firstValue("X-Nexus-Unattested").isPresent();
        }
    }

    /** A seat as a client holds it: its id, the generation and token of its current binding, and its process. */
    protected record Seat(String id, String name, int generation, String token, String sessionId, int pid,
                          Map<String, Object> evidence) {
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
            PgContainerHelper.seedServiceToken(dsl, BOOT, TenantConstants.DEFAULT_TENANT, TokenStore.ROOT_TOKEN_LABEL,
                TokenStore.SCOPE_ROOT, null, null);
            PgContainerHelper.seedServiceToken(dsl, TENANT_TOKEN, TENANT, "tenant", TokenStore.SCOPE_TENANT, null, null);
            PgContainerHelper.seedServiceToken(dsl, DATA_TOKEN, TENANT, "data", TokenStore.SCOPE_DATA, null, null);
            PgContainerHelper.seedServiceToken(dsl, OTHER_TENANT_TOKEN, OTHER_TENANT, "tenant",
                TokenStore.SCOPE_TENANT, null, null);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(8);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
        scope = new TenantScope(ds);
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

    // ── HTTP ─────────────────────────────────────────────────────────────────

    protected Resp call(String bearer, String identityToken, String sessionToken, String method, String path,
                        Object body) {
        try {
            var b = TestHttp.request("http://127.0.0.1:" + port + path)
                .header("Authorization", "Bearer " + bearer).header("Content-Type", "application/json");
            if (identityToken != null) {
                b.header(ID_HEADER, identityToken);
            }
            if (sessionToken != null) {
                b.header("X-Nexus-T1-Session", sessionToken);
            }
            HttpRequest req = "GET".equals(method) ? b.GET().build()
                : b.method(method, HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body == null ? Map.of() : body)))
                    .build();
            HttpResponse<String> r = http.send(req, HttpResponse.BodyHandlers.ofString());
            String raw = r.body();
            JsonNode json = raw == null || raw.isBlank() ? null : MAPPER.readTree(raw);
            return new Resp(r.statusCode(), json, r.headers(), raw);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /** A call with the tenant credential, carrying {@code identityToken} (null: none). */
    protected Resp post(String identityToken, String path, Object body) {
        return call(TENANT_TOKEN, identityToken, null, "POST", path, body);
    }

    protected Resp get(String identityToken, String path) {
        return call(TENANT_TOKEN, identityToken, null, "GET", path, null);
    }

    protected Resp delete(String identityToken, String path, Object body) {
        return call(TENANT_TOKEN, identityToken, null, "DELETE", path, body);
    }

    // ── seats and bindings ───────────────────────────────────────────────────

    protected static String unique(String stem) {
        return stem + "-" + UUID.randomUUID().toString().substring(0, 8);
    }

    protected static Map<String, Object> evidence(String sessionId, int pid, String lease) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("session_id", sessionId);
        m.put("host", "h1");
        m.put("pid", pid);
        m.put("started_at", 1000L + pid);
        m.put("proc_start", "2026-11-17T10:00:" + String.format("%02d", pid % 60) + "Z");
        m.put("client_version", "7.99.0");
        if (lease != null) {
            m.put("lease", lease);
        }
        return m;
    }

    private static Map<String, Object> verdictOf(Map<String, Object> ev) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (String k : new String[] {"host", "pid", "started_at", "proc_start"}) {
            m.put(k, ev.get(k));
        }
        return m;
    }

    /** Creates a seat (unheld, generation 0) and returns its id. */
    protected String createSeat(String name) {
        Resp r = post(null, "/v1/identity", Map.of("kind", "seat", "name", name, "created_by", "sam"));
        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        return r.text("id");
    }

    private Seat held(String id, String name, Resp r, String sid, int pid, Map<String, Object> ev) {
        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        assertThat(r.text("identity_token")).as("a bind that leaves the caller holding the seat returns a token").isNotBlank();
        return new Seat(id, name, r.json().get("generation").asInt(), r.text("identity_token"), sid, pid, ev);
    }

    /** A seat that does not exist yet, created and bound by a process {@code pid} with event {@code start}. */
    protected Seat bindSeat(String stem, int pid) {
        return bindSeat(stem, pid, null);
    }

    protected Seat bindSeat(String stem, int pid, String lease) {
        String name = unique(stem);
        String id = createSeat(name);
        String sid = "sess-" + UUID.randomUUID();
        Map<String, Object> ev = evidence(sid, pid, lease);
        Map<String, Object> body = new LinkedHashMap<>(ev);
        body.put("event", "start");
        body.put("expected_generation", 0);
        return held(id, name, post(null, "/v1/identity/" + id + "/bind", body), sid, pid, ev);
    }

    /** An ephemeral identity created already bound to process {@code pid} (a launch that named no seat). */
    protected Seat bindEphemeral(int pid) {
        String sid = "sess-" + UUID.randomUUID();
        Map<String, Object> ev = evidence(sid, pid, null);
        Resp r = post(null, "/v1/identity", Map.of("kind", "ephemeral", "created_by", "session", "bind", ev));
        return held(r.text("id"), null, r, sid, pid, ev);
    }

    /**
     * A new process takes the seat the old one holds: the client probed the old holder, found it gone, and sends the
     * verdict with the bind (the policy table's {@code resume} row), so the generation goes up and the old
     * process's token is superseded. {@code sameSession} keeps the session id, as a restart does.
     */
    protected Seat resumeInNewProcess(Seat old, int newPid, boolean sameSession) {
        String sid = sameSession ? old.sessionId() : "sess-" + UUID.randomUUID();
        Map<String, Object> ev = evidence(sid, newPid, null);
        Map<String, Object> body = new LinkedHashMap<>(ev);
        body.put("event", "resume");
        body.put("expected_generation", old.generation());
        body.put("holder_gone", verdictOf(old.evidence()));
        return held(old.id(), old.name(), post(null, "/v1/identity/" + old.id() + "/bind", body), sid, newPid, ev);
    }

    /** {@code /clear}: the same process, the same generation, a new session id; the response carries another token. */
    protected Seat clear(Seat s, String newSession) {
        Map<String, Object> ev = new LinkedHashMap<>(s.evidence());
        ev.put("session_id", newSession);
        Map<String, Object> body = new LinkedHashMap<>(ev);
        body.put("event", "clear");
        body.put("expected_generation", s.generation());
        Resp r = post(null, "/v1/identity/" + s.id() + "/bind", body);
        return held(s.id(), s.name(), r, newSession, s.pid(), ev);
    }

    /** A bind event of the seat's own process at its generation ({@code end}, {@code release}, {@code refresh}). */
    protected Resp seatEvent(Seat s, String event) {
        Map<String, Object> body = new LinkedHashMap<>(s.evidence());
        body.put("event", event);
        body.put("expected_generation", s.generation());
        return post(null, "/v1/identity/" + s.id() + "/bind", body);
    }

    // ── tuples ───────────────────────────────────────────────────────────────

    /** The send {@code from} makes to {@code to}'s mailbox; any token, or none. */
    protected Resp mailTo(String to, String nonce, String identityToken) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("subspace", "mailbox/" + to);
        m.put("keys", Map.of("to", to));
        m.put("dims", Map.of("from", "peer"));
        m.put("body", "hello " + nonce);
        m.put("nonce", nonce);
        return post(identityToken, "/v1/tuples/out", m);
    }

    protected Resp claim(String op, String to, String claimant, String identityToken) {
        return post(identityToken, "/v1/tuples/" + op, Map.of("subspace", "mailbox/" + to,
            "keys_pattern", Map.of("to", to), "claimant", claimant, "lease_s", 60));
    }
}
