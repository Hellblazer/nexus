// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.IdentityEvidence;
import dev.nexus.service.db.IdentityException;
import dev.nexus.service.db.IdentityRepository;
import dev.nexus.service.db.IdentityTokenStore;
import dev.nexus.service.db.TokenStore;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

/**
 * RDR-253 Step 1: the {@code /v1/identity} routes of session identity.
 *
 * <pre>
 *   POST /v1/identity                      create a seat {kind:seat, name, display_name?, created_by} (tenant; or data with a live token) or an
 *                                          ephemeral identity {kind:ephemeral, display_name?, created_by, bind:{evidence}}
 *                                          (the MCP server's pending bind may do the second with its data credential)
 *   POST /v1/identity/delegated            {parent, display_name?, bind:{evidence}}: a delegated identity, generation 1
 *   POST /v1/identity/rename               {old, new}: retire the old alias, take the new, retitle the board entry
 *   POST /v1/identity/&lt;id&gt;/promote         {name}: an ephemeral identity becomes a seat
 *   POST /v1/identity/&lt;id&gt;/bind            {event, expected_generation, ...evidence, holder_gone?}: the compare-and-set; an
 *                                          event after which the caller holds the identity also returns an identity_token
 *   POST /v1/identity/&lt;id&gt;/gone            {expected_generation, holder_gone}: record that the holder is gone
 *   POST /v1/identity/&lt;id&gt;/renew           {generation, client_version?}
 *   POST /v1/identity/&lt;id&gt;/purge           {operator:true}: remove the fenced state of an identity that is not live
 *   GET  /v1/identity/&lt;id&gt;                  the identity, its state and holder as of now (alive)
 *   GET  /v1/identity/&lt;id&gt;/bindings         its binding log
 *   POST /v1/identity/&lt;id&gt;/work             {kind, ref, state}: register work the identity owns, or change its state
 *   DELETE /v1/identity/&lt;id&gt;/work?kind=&amp;ref=  forget it (idempotent)
 *   GET  /v1/identity/&lt;id&gt;/work             the work of one identity
 *   GET  /v1/identity/work?kind=&amp;ref=        the identities that own that resource (several may)
 *   POST /v1/identity/&lt;id&gt;/memberships      {group, role}: join a group, or change the role (the join time stays); fenced
 *   DELETE /v1/identity/&lt;id&gt;/memberships/&lt;group&gt;  leave a group (idempotent); fenced
 *   GET  /v1/identity/&lt;id&gt;/memberships      the groups of one identity, in join order
 *   GET  /v1/identity/groups/&lt;group&gt;        the roster of a group, in join order
 *   GET  /v1/identity/alias/&lt;name&gt;         resolve a name; a retired one for 7 days with a notice, then 410
 *   GET  /v1/identity/sessions/&lt;session id&gt; the session index
 *   GET  /v1/identity/bindings?host=&amp;pid=    the live and lapsed holders on a host
 *   GET  /v1/identity/bindings?host=&amp;pid=&amp;proc_start=&amp;history=1  (all three required) the newest binding row of every identity that ever
 *                                          held that process, ended or released ones included (the in-place /restart rebind)
 * </pre>
 *
 * <p>Two credentials reach this surface (RDR-253): the hook, {@code nx identity} and the {@code nx} dispatcher use
 * the tenant credential (the root token counts as one); the MCP server uses the data credential for the calls it
 * makes in a running session. Each route admits exactly the scopes that need it, in {@link #admit}; purge, rename
 * and the {@code gone} verdict are tenant-only; seat creation, promotion and delegated creation take the tenant
 * credential, or the data credential for the identity its own identity token names only ({@link #selfService}; RDR-253
 * amendment, Sam 2026-11-19), and a data credential binds only the events {@link #DATA_EVENTS}. Any other scope is a
 * 403, as is a scope not listed. Reads admit both.
 */
public final class IdentityHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(IdentityHandler.class);

    static final ObjectMapper MAPPER = new ObjectMapper()
        .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false);
    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() { };

    /** The bind events the MCP server's data credential may send: pending bind, fork row, refresh and end. */
    public static final Set<String> DATA_EVENTS = Set.of("start", "resume", "fork", "refresh", "end");

    /** The bind events after which the caller holds the identity, so the response carries a token for the binding. */
    static final Set<String> TOKEN_EVENTS = Set.of("start", "resume", "takeover", "clear", "fork", "refresh");
    /** The data events that mint a token for an identity already held, and so need its own live token (RDR-253, Sam 2026-11-19). */
    static final Set<String> TOKEN_GUARDED_DATA_EVENTS = Set.of("refresh", "fork");

    private final IdentityRepository repo;
    private final IdentityTokenStore tokens;
    private final IdentityFence fence;

    public IdentityHandler(IdentityRepository repo, IdentityTokenStore tokens, IdentityFence fence) {
        this.repo = repo;
        this.tokens = tokens;
        this.fence = fence;
    }

    /**
     * Adds the identity token of the binding {@code view} describes ({@code identity_token},
     * {@code identity_token_expires_in_seconds}) when the identity is held; a binding that ended or released
     * the seat gets none. The token names the generation the view reports, so a refresh (the same generation)
     * and a {@code clear} or {@code fork} (a new session id under the same generation) mint another token for the
     * binding they keep, and every earlier token of it keeps working until it expires.
     */
    private Map<String, Object> withToken(String tenant, Map<String, Object> view) {
        Object state = view.get("state");
        if (!"live".equals(state) && !"lapsed".equals(state)) {
            return view;
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> holder = (Map<String, Object>) view.get("holder");
        IdentityTokenStore.Issued issued = tokens.issue(tenant, (String) view.get("id"),
            ((Number) view.get("generation")).intValue(), holder == null ? null : (String) holder.get("proc_start"));
        Map<String, Object> out = new LinkedHashMap<>(view);
        out.put("identity_token", issued.rawToken());
        out.put("identity_token_expires_in_seconds", issued.expiresInSeconds());
        return out;
    }

    private static boolean tenantScope(String scope) {
        return TokenStore.SCOPE_TENANT.equals(scope) || TokenStore.SCOPE_ROOT.equals(scope);
    }

    private static boolean dataScope(String scope) {
        return TokenStore.SCOPE_DATA.equals(scope);
    }

    /** The route a request is, for the credential check. */
    enum Route { CREATE, CREATE_EPHEMERAL, DELEGATED, RENAME, PROMOTE, BIND, GONE, RENEW, PURGE, SHOW, LOG, ALIAS, SESSION, BY_HOST,
        MEMBER_WRITE, MEMBER_READ, WORK_WRITE, WORK_READ }

    /** Whether {@code scope} may call {@code route} (the bind event is checked by the caller). */
    static boolean admit(Route route, String scope) {
        return switch (route) {
            case RENAME, GONE, PURGE -> tenantScope(scope);
            // RDR-253 amendment (Sam 2026-11-19, option 2): the data credential reaches these three only for the
            // identity its own identity token names; selfService() fences each call once the target is known.
            case CREATE, DELEGATED, PROMOTE,
                 BIND, RENEW, SHOW, LOG, ALIAS, SESSION, BY_HOST, MEMBER_WRITE, MEMBER_READ,
                 CREATE_EPHEMERAL, WORK_WRITE, WORK_READ -> tenantScope(scope) || dataScope(scope);
        };
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        String tenant = RequestContext.tenant();
        if (tenant == null) {
            HttpUtil.send(exchange, 500, "{\"error\":\"internal: tenant not set\"}");
            return;
        }
        String path = exchange.getRequestURI().getPath().replaceFirst("^/v1/identity/?", "");
        String method = exchange.getRequestMethod().toUpperCase(Locale.ROOT);
        String[] seg = path.isEmpty() ? new String[0] : path.split("/", -1);
        try {
            Map<String, Object> out = dispatch(exchange, tenant, method, seg);
            if (out != null) {
                HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(out));
            }
        } catch (IdentityException e) {
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("error", e.code());
            body.put("message", e.getMessage());
            body.putAll(e.details());
            HttpUtil.send(exchange, e.status(), MAPPER.writeValueAsString(body));
        } catch (IllegalArgumentException e) {
            HttpUtil.send(exchange, 400, "{\"error\":" + MAPPER.writeValueAsString(e.getMessage()) + "}");
        } catch (Exception e) {
            if (!HttpUtil.sendTypedDbError(exchange, e, log, "identity_handler", "path=" + path + " tenant=" + tenant)) {
                log.error("event=identity_handler_error path={} tenant={} error={}", path, tenant, e.getMessage(), e);
                HttpUtil.send(exchange, 500, "{\"error\":\"internal server error\"}");
            }
        }
    }

    /**
     * The fence of a data-credential seat create, promote or delegated create (RDR-253 amendment, Sam 2026-11-19):
     * the caller's identity token must name {@code owner} at its current generation, and {@code owner} must be held.
     * The tenant credential is not fenced here, as before. Returns the token's identity, or null for the tenant
     * credential.
     */
    private String selfService(HttpExchange ex, String tenant, String scope, String owner) {
        if (tenantScope(scope)) {
            return null;
        }
        String tokenIdentity = RequestContext.identity();
        if (tokenIdentity == null) {
            throw new IdentityException(403, "identity_token_required",
                "the data credential may do this only for the identity its own live identity token names; send that token"
                    + " (an expired token counts as none: the session rebinds on its next start)");
        }
        fence.requireOwner(ex, tenant, owner == null ? tokenIdentity : owner);
        return tokenIdentity;
    }

    private static IdentityException forbidden(Route route, String scope) {
        return new IdentityException(403, "forbidden", "a '" + scope + "' credential may not call " + route
            + " on /v1/identity");
    }

    private static IdentityException notFound() {
        return new IdentityException(404, "not_found", "not found");
    }

    private static IdentityException methodNotAllowed() {
        return new IdentityException(405, "method_not_allowed", "method not allowed");
    }

    private Map<String, Object> dispatch(HttpExchange ex, String tenant, String method, String[] seg) throws IOException {
        String scope = RequestContext.scope();
        if (seg.length == 0) {
            return create(ex, tenant, method, scope);
        }
        switch (seg[0]) {
            case "delegated" -> {
                return delegated(ex, tenant, method, scope, seg);
            }
            case "rename" -> {
                return rename(ex, tenant, method, scope, seg);
            }
            case "work" -> {
                if (seg.length != 1) throw notFound();
                if (!"GET".equals(method)) throw methodNotAllowed();
                require(Route.WORK_READ, scope);
                Map<String, String> q = query(ex);
                return Map.of("kind", String.valueOf(q.get("kind")), "ref", String.valueOf(q.get("ref")),
                    "owners", repo.ownersOfWork(tenant, q.get("kind"), q.get("ref")));
            }
            case "bindings" -> {
                if (seg.length != 1) throw notFound();
                if (!"GET".equals(method)) throw methodNotAllowed();
                require(Route.BY_HOST, scope);
                Map<String, String> q = query(ex);
                boolean history = "1".equals(q.get("history")) || "true".equals(q.get("history"));
                List<Map<String, Object>> rows = history
                    ? repo.bindingHistoryByProcess(tenant, q.get("host"), intParam(q.get("pid")), q.get("proc_start"))
                    : repo.bindingsByHost(tenant, q.get("host"), intParam(q.get("pid")));
                return Map.of("bindings", rows);
            }
            case "sessions" -> {
                if (seg.length != 2) throw notFound();
                if (!"GET".equals(method)) throw methodNotAllowed();
                require(Route.SESSION, scope);
                return repo.lookupSession(tenant, seg[1]);
            }
            case "groups" -> {
                if (seg.length != 2) throw notFound();
                if (!"GET".equals(method)) throw methodNotAllowed();
                require(Route.MEMBER_READ, scope);
                return Map.of("group", seg[1], "members", repo.rosterOf(tenant, seg[1]));
            }
            case "alias" -> {
                if (seg.length != 2) throw notFound();
                if (!"GET".equals(method)) throw methodNotAllowed();
                require(Route.ALIAS, scope);
                return repo.resolveAlias(tenant, seg[1]);
            }
            default -> {
                return byId(ex, tenant, method, scope, seg);
            }
        }
    }

    private static void require(Route route, String scope) {
        if (!admit(route, scope)) {
            throw forbidden(route, scope);
        }
    }

    private Map<String, Object> create(HttpExchange ex, String tenant, String method, String scope) throws IOException {
        if (!"POST".equals(method)) throw methodNotAllowed();
        // Any scope that is neither a tenant/root nor a data credential is refused before the body is read; the
        // kind-specific check (a seat is the tenant's, or the data credential's through its own token) follows once the kind is known.
        if (!tenantScope(scope) && !dataScope(scope)) throw forbidden(Route.CREATE, scope);
        Map<String, Object> body = readBody(ex);
        String kind = string(body, "kind");
        String display = string(body, "display_name");
        String createdBy = string(body, "created_by");
        if ("seat".equals(kind)) {
            require(Route.CREATE, scope);
            // A data credential creates a seat only with a live token; the seat starts unheld, so it touches no
            // identity, and its creator is the token's identity whatever the body says.
            String caller = selfService(ex, tenant, scope, null);
            return caller == null ? repo.createSeat(tenant, string(body, "name"), display, createdBy)
                : repo.createSeatForIdentity(tenant, string(body, "name"), display, caller);
        }
        if ("ephemeral".equals(kind)) {
            require(Route.CREATE_EPHEMERAL, scope);
            return withToken(tenant, repo.createEphemeral(tenant, display, createdBy, evidence(map(body, "bind"))));
        }
        throw new IdentityException(400, "bad_kind", "kind must be seat or ephemeral");
    }

    private Map<String, Object> delegated(HttpExchange ex, String tenant, String method, String scope, String[] seg)
        throws IOException {
        if (seg.length != 1) throw notFound();
        if (!"POST".equals(method)) throw methodNotAllowed();
        require(Route.DELEGATED, scope);
        Map<String, Object> body = readBody(ex);
        String parent = string(body, "parent");
        if (!tenantScope(scope)) {
            if (parent == null || parent.isBlank()) throw new IllegalArgumentException("parent is required");
            selfService(ex, tenant, scope, parent);
            // The child's session id must be its own: a data-credential delegation never repoints a session another
            // identity holds in the session index (review: "touches no other identity" on this route too).
            Object childSession = map(body, "bind") == null ? null : map(body, "bind").get("session_id");
            if (childSession instanceof String sid && !sid.isBlank() && repo.sessionIndexed(tenant, sid)) {
                throw new IdentityException(409, "session_held",
                    "session " + sid + " already belongs to an identity; a delegated child needs a session of its own");
            }
        }
        return withToken(tenant, repo.createDelegated(tenant, parent, string(body, "display_name"),
            evidence(map(body, "bind"))));
    }

    private Map<String, Object> rename(HttpExchange ex, String tenant, String method, String scope, String[] seg)
        throws IOException {
        if (seg.length != 1) throw notFound();
        if (!"POST".equals(method)) throw methodNotAllowed();
        require(Route.RENAME, scope);
        Map<String, Object> body = readBody(ex);
        return repo.rename(tenant, string(body, "old"), string(body, "new"));
    }

    private Map<String, Object> byId(HttpExchange ex, String tenant, String method, String scope, String[] seg)
        throws IOException {
        String id = seg[0];
        if (seg.length == 1) {
            if (!"GET".equals(method)) throw methodNotAllowed();
            require(Route.SHOW, scope);
            return repo.show(tenant, id);
        }
        if (seg.length == 3 && "memberships".equals(seg[1])) {
            if (!"DELETE".equals(method)) throw methodNotAllowed();
            require(Route.MEMBER_WRITE, scope);
            fence.requireOwner(ex, tenant, id);
            return repo.leaveGroup(tenant, id, seg[2]);
        }
        if (seg.length != 2) throw notFound();
        switch (seg[1]) {
            case "work" -> {
                if ("GET".equals(method)) {
                    require(Route.WORK_READ, scope);
                    return Map.of("identity", id, "work", repo.workOf(tenant, id));
                }
                if ("DELETE".equals(method)) {
                    require(Route.WORK_WRITE, scope);
                    Map<String, String> q = query(ex);
                    return repo.removeWork(tenant, id, q.get("kind"), q.get("ref"));
                }
                if (!"POST".equals(method)) throw methodNotAllowed();
                require(Route.WORK_WRITE, scope);
                Map<String, Object> body = readBody(ex);
                return repo.putWork(tenant, id, string(body, "kind"), string(body, "ref"), string(body, "state"));
            }
            case "memberships" -> {
                if ("GET".equals(method)) {
                    require(Route.MEMBER_READ, scope);
                    return Map.of("identity", id, "memberships", repo.groupsOf(tenant, id));
                }
                if (!"POST".equals(method)) throw methodNotAllowed();
                require(Route.MEMBER_WRITE, scope);
                Map<String, Object> body = readBody(ex);
                fence.requireOwner(ex, tenant, id);
                return repo.joinGroup(tenant, id, string(body, "group"), string(body, "role"));
            }
            case "bindings" -> {
                if (!"GET".equals(method)) throw methodNotAllowed();
                require(Route.LOG, scope);
                return Map.of("identity", id, "bindings", repo.bindingLog(tenant, id));
            }
            case "promote" -> {
                if (!"POST".equals(method)) throw methodNotAllowed();
                require(Route.PROMOTE, scope);
                selfService(ex, tenant, scope, id);
                return repo.promote(tenant, id, string(readBody(ex), "name"));
            }
            case "bind" -> {
                if (!"POST".equals(method)) throw methodNotAllowed();
                require(Route.BIND, scope);
                Map<String, Object> body = readBody(ex);
                String event = string(body, "event");
                if (event == null) {
                    throw new IdentityException(400, "bad_event", "event is required");
                }
                if (!tenantScope(scope) && !DATA_EVENTS.contains(event)) {
                    throw new IdentityException(403, "forbidden",
                        "a '" + scope + "' credential may not send the '" + event + "' event");
                }
                if (TOKEN_GUARDED_DATA_EVENTS.contains(event)) {
                    // RDR-253 (Sam 2026-11-19, option A): a data-credential refresh or fork mints a token for a held
                    // identity, so it needs that identity's own live token. Naming the holder's process is not enough:
                    // the holder is readable by the same credential. The tenant credential is not fenced here.
                    selfService(ex, tenant, scope, id);
                }
                Map<String, Object> bound = repo.bind(tenant, id, event, integer(body, "expected_generation"), evidence(body));
                return TOKEN_EVENTS.contains(event) ? withToken(tenant, bound) : bound;
            }
            case "gone" -> {
                if (!"POST".equals(method)) throw methodNotAllowed();
                require(Route.GONE, scope);
                Map<String, Object> body = readBody(ex);
                IdentityEvidence.Process verdict = process(map(body, "holder_gone"));
                return repo.gone(tenant, id, integer(body, "expected_generation"), verdict);
            }
            case "renew" -> {
                if (!"POST".equals(method)) throw methodNotAllowed();
                require(Route.RENEW, scope);
                Map<String, Object> body = readBody(ex);
                return repo.renew(tenant, id, integer(body, "generation"), string(body, "client_version"));
            }
            case "purge" -> {
                if (!"POST".equals(method)) throw methodNotAllowed();
                require(Route.PURGE, scope);
                Map<String, Object> body = readBody(ex);
                if (!Boolean.TRUE.equals(body.get("operator"))) {
                    throw new IdentityException(400, "operator_required", "purge needs \"operator\": true");
                }
                String credential = RequestContext.credentialHash();
                String named = credential == null ? "" : ":" + credential.substring(0, Math.min(12, credential.length()));
                return repo.purge(tenant, id, "operator:" + scope + named);
            }
            default -> throw notFound();
        }
    }

    // ── parsing ──────────────────────────────────────────────────────────────

    private Map<String, Object> readBody(HttpExchange exchange) throws IOException {
        try (InputStream in = exchange.getRequestBody()) {
            byte[] bytes = in.readAllBytes();
            if (bytes.length == 0) {
                throw new IdentityException(400, "bad_body", "request body is required");
            }
            Map<String, Object> body;
            try {
                body = MAPPER.readValue(bytes, MAP_TYPE);
            } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
                throw new IdentityException(400, "bad_body", "the request body is not a JSON object");
            }
            if (body == null) {
                throw new IdentityException(400, "bad_body", "the request body is not a JSON object");
            }
            return body;
        }
    }

    private static Map<String, String> query(HttpExchange ex) {
        Map<String, String> q = new LinkedHashMap<>();
        String raw = ex.getRequestURI().getRawQuery();
        if (raw != null) {
            for (String pair : raw.split("&")) {
                int eq = pair.indexOf('=');
                String k = eq < 0 ? pair : pair.substring(0, eq);
                String v = eq < 0 ? "" : pair.substring(eq + 1);
                q.put(java.net.URLDecoder.decode(k, java.nio.charset.StandardCharsets.UTF_8),
                    java.net.URLDecoder.decode(v, java.nio.charset.StandardCharsets.UTF_8));
            }
        }
        return q;
    }

    private static Integer intParam(String v) {
        if (v == null || v.isBlank()) {
            return null;
        }
        try {
            return Integer.valueOf(v);
        } catch (NumberFormatException e) {
            throw new IdentityException(400, "bad_field", "pid must be an integer");
        }
    }

    private static String string(Map<String, Object> m, String key) {
        Object v = m == null ? null : m.get(key);
        if (v == null) {
            return null;
        }
        if (!(v instanceof String s)) {
            throw new IdentityException(400, "bad_field", "'" + key + "' must be a string");
        }
        return s;
    }

    private static Integer integer(Map<String, Object> m, String key) {
        Object v = m == null ? null : m.get(key);
        if (v == null) {
            return null;
        }
        if (v instanceof Number n && n.doubleValue() == n.intValue()) {
            return n.intValue();
        }
        throw new IdentityException(400, "bad_field", "'" + key + "' must be an integer");
    }

    private static Long longValue(Map<String, Object> m, String key) {
        Object v = m == null ? null : m.get(key);
        if (v == null) {
            return null;
        }
        if (v instanceof Number n) {
            return n.longValue();
        }
        throw new IdentityException(400, "bad_field", "'" + key + "' must be a number");
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> map(Map<String, Object> m, String key) {
        Object v = m == null ? null : m.get(key);
        if (v == null) {
            return null;
        }
        if (v instanceof Map<?, ?> mm) {
            return (Map<String, Object>) mm;
        }
        throw new IdentityException(400, "bad_field", "'" + key + "' must be an object");
    }

    private static IdentityEvidence.Process process(Map<String, Object> m) {
        if (m == null) {
            return null;
        }
        return new IdentityEvidence.Process(string(m, "host"), integer(m, "pid"), longValue(m, "started_at"),
            string(m, "proc_start"));
    }

    private static IdentityEvidence evidence(Map<String, Object> m) {
        if (m == null) {
            return null;
        }
        try {
            return new IdentityEvidence(string(m, "session_id"), string(m, "host"), integer(m, "pid"),
                longValue(m, "started_at"), string(m, "proc_start"), string(m, "pane"), string(m, "client_version"),
                string(m, "lease"), string(m, "prior_session_id"), process(map(m, "holder_gone")));
        } catch (IllegalArgumentException e) {
            throw new IdentityException(400, "bad_field", e.getMessage());
        }
    }
}
