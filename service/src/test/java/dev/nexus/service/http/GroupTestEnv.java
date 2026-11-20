// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.Filter;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import dev.nexus.service.TestHttp;
import dev.nexus.service.db.GroupRepository;
import dev.nexus.service.db.IdentityFenceReads;
import dev.nexus.service.http.IdentityTestEnv.Resp;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.http.IdentityTestEnv.ev;

/**
 * RDR-252 Step 1 test fixture: {@link IdentityTestEnv} (the shared PostgreSQL, the identity routes, the clock the
 * test moves) with the real {@link GroupHandler} mounted beside it on {@code /v1/group}. The filter stands in for
 * {@code AuthFilter} exactly as the identity fixture's does: the request principal comes from the
 * {@code X-Test-Tenant}, {@code X-Test-Scope}, {@code X-Test-Identity} and {@code X-Test-Generation} headers, so a
 * test chooses the tenant, the credential and the identity token of each call.
 */
public final class GroupTestEnv implements AutoCloseable {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final TypeReference<Map<String, Object>> MAP = new TypeReference<>() { };

    /** The identity fixture: its routes, clock, tenant scope and fixtures are this one's. */
    public final IdentityTestEnv ids;
    public final GroupRepository groups;
    private final HttpServer server;
    private final HttpClient http = TestHttp.client();
    private final AtomicInteger pids = new AtomicInteger(1000);
    /** The evidence each seat was last bound with, by identity id: what an {@code end} or a {@code gone} names. */
    private final Map<String, Map<String, Object>> holders = new ConcurrentHashMap<>();
    /** The identity token each seat's last bind returned, by identity id: what its MCP server sends as {@code X-Nexus-Identity-Token}. */
    private final Map<String, String> tokens = new ConcurrentHashMap<>();

    private GroupTestEnv(IdentityTestEnv ids) throws IOException {
        this.ids = ids;
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        groups = new GroupRepository(ids.scope, ids.clock);
        server.createContext("/v1/group", new GroupHandler(groups, new IdentityFence(new IdentityFenceReads(ids.scope))))
            .getFilters().add(principalFilter());
        server.createContext("/v1/identity", new IdentityHandler(ids.repo, ids.tokens, new IdentityFence(new IdentityFenceReads(ids.scope))))
            .getFilters().add(principalFilter());
        server.setExecutor(Executors.newVirtualThreadPerTaskExecutor());
        server.start();
    }

    public static GroupTestEnv start() throws Exception {
        return new GroupTestEnv(IdentityTestEnv.start());
    }

    static Filter principalFilter() {
        return new Filter() {
            @Override
            public void doFilter(HttpExchange ex, Chain chain) throws IOException {
                String tenant = ex.getRequestHeaders().getFirst("X-Test-Tenant");
                String sc = ex.getRequestHeaders().getFirst("X-Test-Scope");
                String identity = ex.getRequestHeaders().getFirst("X-Test-Identity");
                String generation = ex.getRequestHeaders().getFirst("X-Test-Generation");
                RequestContext.set(new RequestContext.Principal(tenant, null, false, "root".equals(sc), sc, "test-hash",
                    identity, generation == null ? null : Integer.valueOf(generation)));
                try {
                    chain.doFilter(ex);
                } finally {
                    RequestContext.clear();
                }
            }

            @Override
            public String description() { return "test principal"; }
        };
    }

    // ── fixtures ─────────────────────────────────────────────────────────────

    /** A seat that holds a leased, live binding: a seat that may lead a group. */
    public String seat(String tenant, String name) {
        String id = ids.createSeat(tenant, name);
        int pid = pids.incrementAndGet();
        return bound(tenant, id, name, ev("sess-" + name, pid, 10_000L + pid));
    }

    /** A seat held by a Desktop session: live, with a lease of {@code unleased} (liveness unknown, no token). */
    public String desktopSeat(String tenant, String name) {
        String id = ids.createSeat(tenant, name);
        int pid = pids.incrementAndGet();
        return bound(tenant, id, name, IdentityTestEnv.with(ev("sess-" + name, pid, 10_000L + pid), "lease", "unleased"));
    }

    private String bound(String tenant, String id, String name, Map<String, Object> evidence) {
        Resp r = ids.bind(tenant, id, "start", 0, evidence);
        if (r.status() != 200) {
            throw new IllegalStateException("bind " + name + ": " + r);
        }
        holders.put(id, evidence);
        if (r.body().get("identity_token") instanceof String tok) {
            tokens.put(id, tok);
        }
        return id;
    }

    /** The identity token the seat's bind returned, or null when it returned none. */
    public String tokenOf(String id) {
        return tokens.get(id);
    }

    /** An ephemeral identity (a launch that named no seat), bound: live and leased, and not a seat. */
    public String ephemeral(String tenant, String name) {
        int pid = pids.incrementAndGet();
        Map<String, Object> evidence = ev("sess-" + name, pid, 10_000L + pid);
        Resp r = ids.post(tenant, "", Map.of("kind", "ephemeral", "display_name", name, "created_by", "session", "bind", evidence));
        if (r.status() != 200) {
            throw new IllegalStateException("ephemeral " + name + ": " + r);
        }
        holders.put(r.str("id"), evidence);
        return r.str("id");
    }

    /** A delegated identity (a {@code claude -p} child of {@code parent}), bound. */
    public String delegated(String tenant, String parent, String name) {
        int pid = pids.incrementAndGet();
        Map<String, Object> evidence = ev("sess-" + name, pid, 10_000L + pid);
        Resp r = ids.post(tenant, "/delegated", Map.of("parent", parent, "display_name", name, "bind", evidence));
        if (r.status() != 200) {
            throw new IllegalStateException("delegated " + name + ": " + r);
        }
        holders.put(r.str("id"), evidence);
        if (r.body().get("identity_token") instanceof String tok) {
            tokens.put(r.str("id"), tok);
        }
        return r.str("id");
    }

    private Resp event(String tenant, String id, String event) {
        int generation = ((Number) ids.show(tenant, id).get("generation")).intValue();
        return ids.bind(tenant, id, event, generation, holders.get(id));
    }

    /** The holder's {@code end}: the identity is {@code ended}, and its token fences nothing. */
    public void end(String tenant, String id) {
        check(event(tenant, id, "end"), "end " + id);
    }

    /** The holder's {@code release}: the seat is {@code unheld}. */
    public void release(String tenant, String id) {
        check(event(tenant, id, "release"), "release " + id);
    }

    /** A client's verdict that the holder is gone: the identity is {@code lapsed} with a recorded gone. */
    public void gone(String tenant, String id) {
        int generation = ((Number) ids.show(tenant, id).get("generation")).intValue();
        check(ids.post(tenant, "/" + id + "/gone",
            Map.of("expected_generation", generation, "holder_gone", IdentityTestEnv.verdictOf(holders.get(id)))), "gone " + id);
    }

    /** A new process takes the seat over (a resume elsewhere): the generation rises and the old token is superseded. */
    public void supersede(String tenant, String id) {
        supersede(tenant, id, "leased");
    }

    /** The same, by a holder with this lease: {@code unleased} is a Desktop session, whose liveness is unknown. */
    public void supersede(String tenant, String id, String lease) {
        int generation = ((Number) ids.show(tenant, id).get("generation")).intValue();
        int pid = pids.incrementAndGet();
        Map<String, Object> next = IdentityTestEnv.with(ev("sess-again-" + pid, pid, 10_000L + pid), "lease", lease);
        check(ids.bind(tenant, id, "resume", generation,
            IdentityTestEnv.with(next, "holder_gone", IdentityTestEnv.verdictOf(holders.get(id)))), "resume " + id);
        holders.put(id, next);
    }

    /** The holder renews its lease at the generation it holds, so a clock that moves does not lapse it. */
    public void renew(String tenant, String id) {
        int generation = ((Number) ids.show(tenant, id).get("generation")).intValue();
        check(ids.post(tenant, "/" + id + "/renew", Map.of("generation", generation)), "renew " + id);
    }

    private static void check(Resp r, String what) {
        if (r.status() != 200) {
            throw new IllegalStateException(what + ": " + r);
        }
    }

    /** A membership written by the identity itself, with its token. */
    public Resp join(String tenant, String id, String group, String role) {
        return ids.asOwner(tenant, id, "POST", "/" + id + "/memberships", Map.of("group", group, "role", role));
    }

    public Resp leave(String tenant, String id, String group) {
        return ids.asOwner(tenant, id, "DELETE", "/" + id + "/memberships/" + group, null);
    }

    /** The members of a group, as identity ids in join order. */
    public List<String> members(String tenant, String group) {
        return roster(tenant, group).stream().map(m -> (String) m.get("identity")).toList();
    }

    /** A form body for {@code name} led by {@code leader}, serving nothing. */
    public static Map<String, Object> body(String name, String leader) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name", name);
        m.put("leader", leader);
        m.put("purpose", "develop push order");
        m.put("requested_by", "sam");
        return m;
    }

    public static Map<String, Object> body(String name, String leader, List<?> serves) {
        Map<String, Object> m = body(name, leader);
        m.put("serves", serves);
        return m;
    }

    // ── calls ────────────────────────────────────────────────────────────────

    /** A call to {@code /v1/group}{@code path} with the principal of an identity token ({@code identity} null: none). */
    public Resp callAs(String scopeName, String tenant, String identity, int generation, String method, String path,
                       Object body) {
        try {
            var req = TestHttp.request("http://127.0.0.1:" + server.getAddress().getPort() + "/v1/group" + path)
                .header("X-Test-Tenant", tenant).header("X-Test-Scope", scopeName)
                .header("Content-Type", "application/json");
            if (identity != null) {
                req = req.header("X-Test-Identity", identity).header("X-Test-Generation", String.valueOf(generation));
            }
            req = "GET".equals(method) ? req.GET()
                : req.method(method, HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body == null ? Map.of() : body)));
            HttpResponse<String> r = http.send(req.build(), HttpResponse.BodyHandlers.ofString());
            Map<String, Object> parsed;
            try {
                parsed = r.body() == null || r.body().isBlank() ? Map.of() : MAPPER.readValue(r.body(), MAP);
            } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
                parsed = Map.of("raw", r.body());
            }
            return new Resp(r.statusCode(), parsed);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /** A tokenless call to {@code /v1/identity}{@code path}, with the whole response (the unattested label is a header). */
    public HttpResponse<String> identityRaw(String tenant, String method, String path, Object body) {
        try {
            var req = TestHttp.request("http://127.0.0.1:" + server.getAddress().getPort() + "/v1/identity" + path)
                .header("X-Test-Tenant", tenant).header("X-Test-Scope", IdentityTestEnv.TENANT)
                .header("Content-Type", "application/json")
                .method(method, HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body == null ? Map.of() : body)));
            return http.send(req.build(), HttpResponse.BodyHandlers.ofString());
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /** A call as the current process of {@code id} (the token its bind returned names the generation it holds now). */
    public Resp as(String tenant, String id, String method, String path, Object body) {
        int generation = ((Number) ids.show(tenant, id).get("generation")).intValue();
        return callAs(IdentityTestEnv.TENANT, tenant, id, generation, method, path, body);
    }

    /** A call with no identity token, as the tenant credential. */
    public Resp tokenless(String tenant, String method, String path, Object body) {
        return callAs(IdentityTestEnv.TENANT, tenant, null, 0, method, path, body);
    }

    public Resp form(String tenant, String asId, Map<String, Object> body) {
        return as(tenant, asId, "POST", "", body);
    }

    public Resp read(String tenant, String name) {
        return callAs(IdentityTestEnv.TENANT, tenant, null, 0, "GET", "/" + name, null);
    }

    /** The roster route of RDR-253: members in join order. */
    public List<Map<String, Object>> roster(String tenant, String group) {
        return ids.get(tenant, "/groups/" + group).list("members");
    }

    @Override
    public void close() throws Exception {
        server.stop(0);
        ids.close();
    }
}
