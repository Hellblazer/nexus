// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.Filter;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.TestHttp;
import dev.nexus.service.db.IdentityRepository;
import dev.nexus.service.db.TenantScope;
import org.jooq.JSONB;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.Clock;
import java.time.OffsetDateTime;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicReference;

import static dev.nexus.service.jooq.nexus.Tables.GC_AUDIT;
import static dev.nexus.service.jooq.nexus.Tables.MEMORY;
import static dev.nexus.service.jooq.nexus.Tables.TUPLES;

/**
 * RDR-253 Step 1 test fixture: a real PostgreSQL (the shared test cluster, product schema), an
 * {@link IdentityRepository} on a clock the test moves, and the real {@link IdentityHandler} behind a local
 * HTTP server. The server's filter stands in for {@code AuthFilter}: it sets the request principal from the
 * {@code X-Test-Tenant} and {@code X-Test-Scope} headers, so a test chooses the tenant and the credential scope
 * of each call. Routes, status codes and bodies are the ones a client sees. (The real filter and real tokens are
 * covered by {@code IdentityCredentialRoutesTest}.)
 */
public final class IdentityTestEnv implements AutoCloseable {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final TypeReference<Map<String, Object>> MAP = new TypeReference<>() { };

    /** The scopes a test names. */
    public static final String TENANT = "tenant";
    public static final String DATA = "data";

    /** A clock the test sets; never moves by itself. */
    public static final class MutableClock extends Clock {
        private final AtomicReference<Instant> now = new AtomicReference<>(Instant.parse("2026-11-16T12:00:00Z"));

        @Override public java.time.ZoneId getZone() { return ZoneOffset.UTC; }
        @Override public Clock withZone(java.time.ZoneId zone) { return this; }
        @Override public Instant instant() { return now.get(); }

        public void set(Instant t) { now.set(t); }
        public void advance(Duration d) { now.updateAndGet(t -> t.plus(d)); }
    }

    /** One response: the status and the JSON object body. */
    public record Resp(int status, Map<String, Object> body) {
        public String error() { return (String) body.get("error"); }
        @SuppressWarnings("unchecked")
        public List<Map<String, Object>> list(String key) { return (List<Map<String, Object>>) body.get(key); }
        @SuppressWarnings("unchecked")
        public Map<String, Object> map(String key) { return (Map<String, Object>) body.get(key); }
        public String str(String key) { return (String) body.get(key); }
        public int integer(String key) { return ((Number) body.get(key)).intValue(); }
    }

    public final MutableClock clock = new MutableClock();
    public final PostgreSQLContainer<?> pg;
    public final HikariDataSource ds;
    public final TenantScope scope;
    public final IdentityRepository repo;
    private final HttpServer server;
    private final HttpClient http = TestHttp.client();

    private IdentityTestEnv(int pruneBatch, int pruneMaxBatches) throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(8);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
        scope = new TenantScope(ds);
        repo = new IdentityRepository(scope, clock, pruneBatch, pruneMaxBatches);
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/identity", new IdentityHandler(repo)).getFilters().add(new Filter() {
            @Override
            public void doFilter(HttpExchange ex, Chain chain) throws IOException {
                String tenant = ex.getRequestHeaders().getFirst("X-Test-Tenant");
                String sc = ex.getRequestHeaders().getFirst("X-Test-Scope");
                RequestContext.set(new RequestContext.Principal(tenant, null, false, "root".equals(sc), sc, "test-hash"));
                try {
                    chain.doFilter(ex);
                } finally {
                    RequestContext.clear();
                }
            }

            @Override
            public String description() { return "test principal"; }
        });
        server.setExecutor(Executors.newVirtualThreadPerTaskExecutor());
        server.start();
    }

    public static IdentityTestEnv start() throws Exception {
        return new IdentityTestEnv(5000, 20);
    }

    /** An environment whose retention prune takes {@code pruneBatch} rows a statement and {@code pruneMaxBatches} a pass. */
    public static IdentityTestEnv start(int pruneBatch, int pruneMaxBatches) throws Exception {
        return new IdentityTestEnv(pruneBatch, pruneMaxBatches);
    }

    /** A tenant no other test has used, so a test sees only its own rows. */
    public static String freshTenant() {
        return "t-" + UUID.randomUUID().toString().substring(0, 8);
    }

    public Resp call(String scopeName, String tenant, String method, String path, Object body) {
        try {
            var req = TestHttp.request("http://127.0.0.1:" + server.getAddress().getPort() + "/v1/identity" + path)
                .header("X-Test-Tenant", tenant).header("X-Test-Scope", scopeName)
                .header("Content-Type", "application/json");
            req = "GET".equals(method) ? req.GET()
                : req.method(method, HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body == null ? Map.of() : body)));
            HttpResponse<String> r = http.send(req.build(), HttpResponse.BodyHandlers.ofString());
            Map<String, Object> parsed = r.body() == null || r.body().isBlank() ? Map.of() : MAPPER.readValue(r.body(), MAP);
            return new Resp(r.statusCode(), parsed);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /** A call with the body text exactly as given (a test of malformed JSON sends what Jackson would never write). */
    public Resp callRaw(String scopeName, String tenant, String method, String path, String rawBody) {
        try {
            var req = TestHttp.request("http://127.0.0.1:" + server.getAddress().getPort() + "/v1/identity" + path)
                .header("X-Test-Tenant", tenant).header("X-Test-Scope", scopeName)
                .header("Content-Type", "application/json")
                .method(method, HttpRequest.BodyPublishers.ofString(rawBody)).build();
            HttpResponse<String> r = http.send(req, HttpResponse.BodyHandlers.ofString());
            Map<String, Object> parsed = r.body() == null || r.body().isBlank() ? Map.of() : MAPPER.readValue(r.body(), MAP);
            return new Resp(r.statusCode(), parsed);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    public Resp post(String tenant, String path, Object body) {
        return call(TENANT, tenant, "POST", path, body);
    }

    public Resp get(String tenant, String path) {
        return call(TENANT, tenant, "GET", path, null);
    }

    // ── fixtures ─────────────────────────────────────────────────────────────

    /** Evidence of one process: host h1, the given pid and start, and a proc_start that follows the pid. */
    public static Map<String, Object> ev(String sessionId, int pid, long startedAt) {
        return evOn("h1", sessionId, pid, startedAt);
    }

    public static Map<String, Object> evOn(String host, String sessionId, int pid, long startedAt) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("session_id", sessionId);
        m.put("host", host);
        m.put("pid", pid);
        m.put("started_at", startedAt);
        m.put("proc_start", "2026-11-16T0" + (pid % 10) + ":00:00Z");
        m.put("pane", "%" + pid);
        m.put("client_version", "7.99.0");
        return m;
    }

    /** The verdict that names the process {@link #ev} describes. */
    public static Map<String, Object> verdictOf(Map<String, Object> ev) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (String k : new String[] {"host", "pid", "started_at", "proc_start"}) {
            m.put(k, ev.get(k));
        }
        return m;
    }

    public static Map<String, Object> with(Map<String, Object> base, Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>(base);
        for (int i = 0; i < kv.length; i += 2) {
            m.put((String) kv[i], kv[i + 1]);
        }
        return m;
    }

    public String createSeat(String tenant, String name) {
        Resp r = post(tenant, "", Map.of("kind", "seat", "name", name, "created_by", "sam"));
        if (r.status() != 200) {
            throw new IllegalStateException("createSeat " + name + ": " + r);
        }
        return r.str("id");
    }

    /** Binds the seat with an event and expected generation; the caller checks the status. */
    public Resp bind(String tenant, String id, String event, int expected, Map<String, Object> ev) {
        return post(tenant, "/" + id + "/bind", with(ev, "event", event, "expected_generation", expected));
    }

    public Map<String, Object> show(String tenant, String id) {
        Resp r = get(tenant, "/" + id);
        if (r.status() != 200) {
            throw new IllegalStateException("show " + id + ": " + r);
        }
        return r.body();
    }

    public List<Map<String, Object>> log(String tenant, String id) {
        return get(tenant, "/" + id + "/bindings").list("bindings");
    }

    public List<String> events(String tenant, String id) {
        return log(tenant, id).stream().map(m -> (String) m.get("event")).toList();
    }

    // ── what an identity owns outside its tables: board entries, tuples, the audit ───────────────────────

    /** Writes a board entry in project threads the way a session does, carrying the identity line. */
    public void putThreadsEntry(String tenant, String title, String identityId) {
        scope.withTenant(tenant, ctx -> ctx.insertInto(MEMORY)
            .set(MEMORY.TENANT_ID, tenant).set(MEMORY.PROJECT, "threads").set(MEMORY.TITLE, title)
            .set(MEMORY.CONTENT, "session: sess-" + title + "\n"
                + (identityId == null ? "" : "identity: " + identityId + "\n") + "## the work\nstate: running\n")
            .set(MEMORY.TIMESTAMP, OffsetDateTime.now(clock)).execute());
    }

    /** Hides a board entry the way {@code MemoryRepository#expire} does: quarantined_at set, every read path skips it. */
    public void quarantineThreadsEntry(String tenant, String title) {
        scope.withTenant(tenant, ctx -> ctx.update(MEMORY).set(MEMORY.QUARANTINED_AT, OffsetDateTime.now(clock))
            .where(MEMORY.TENANT_ID.eq(tenant)).and(MEMORY.PROJECT.eq("threads")).and(MEMORY.TITLE.eq(title)).execute());
    }

    public List<String> threadsTitles(String tenant) {
        return scope.withTenant(tenant, ctx -> ctx.select(MEMORY.TITLE).from(MEMORY)
            .where(MEMORY.TENANT_ID.eq(tenant)).and(MEMORY.PROJECT.eq("threads")).orderBy(MEMORY.TITLE)
            .fetch(MEMORY.TITLE));
    }

    /** Puts one tuple into a subspace (the retention and purge tests need rows to remove). */
    public void putTuple(String tenant, String subspace) {
        putTuple(tenant, subspace, OffsetDateTime.now(clock).plusDays(400));
    }

    public void putTuple(String tenant, String subspace, OffsetDateTime expiresAt) {
        byte[] id = new byte[32];
        new java.security.SecureRandom().nextBytes(id);
        scope.withTenant(tenant, ctx -> ctx.insertInto(TUPLES)
            .set(TUPLES.ID, id).set(TUPLES.TENANT_ID, tenant).set(TUPLES.SUBSPACE, subspace)
            .set(TUPLES.TEMPLATE, subspace.substring(0, subspace.indexOf('/')) + "/<id>")
            .set(TUPLES.KEYS, JSONB.valueOf("{}"))
            .set(TUPLES.EXPIRES_AT, expiresAt)
            .set(TUPLES.CREATED_AT, OffsetDateTime.now(clock)).execute());
    }

    /** The expiry of the (single) tuple in a subspace. */
    public Instant tupleExpiry(String tenant, String subspace) {
        return scope.withTenant(tenant, ctx -> ctx.select(TUPLES.EXPIRES_AT).from(TUPLES)
            .where(TUPLES.TENANT_ID.eq(tenant)).and(TUPLES.SUBSPACE.eq(subspace)).fetchOne(r -> r.value1().toInstant()));
    }

    public int tupleCount(String tenant, String subspace) {
        return scope.withTenant(tenant, ctx -> ctx.fetchCount(TUPLES,
            TUPLES.TENANT_ID.eq(tenant).and(TUPLES.SUBSPACE.eq(subspace))));
    }

    /** The gc_audit rows of one operation for the tenant: actor, then details text, oldest first. */
    public List<List<String>> audit(String tenant, String operation) {
        return scope.withTenant(tenant, ctx -> ctx.select(GC_AUDIT.ACTOR, GC_AUDIT.DETAILS).from(GC_AUDIT)
            .where(GC_AUDIT.TENANT_ID.eq(tenant)).and(GC_AUDIT.OPERATION.eq(operation)).orderBy(GC_AUDIT.ID)
            .fetch(r -> List.of(r.value1(), String.valueOf(r.value2()))));
    }

    /** Rows of one identity table for a tenant, by a count of the generated table (the tombstone tests). */
    public int rows(String tenant, org.jooq.Table<?> table, org.jooq.Field<String> tenantColumn) {
        return scope.withTenant(tenant, ctx -> ctx.fetchCount(table, tenantColumn.eq(tenant)));
    }

    @Override
    public void close() throws Exception {
        server.stop(0);
        ds.close();
        pg.stop();
    }
}
