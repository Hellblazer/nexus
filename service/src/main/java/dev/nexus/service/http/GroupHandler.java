// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.GroupRepository;
import dev.nexus.service.db.IdentityException;
import dev.nexus.service.db.TokenStore;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;

/**
 * RDR-252 Step 1: the {@code /v1/group} routes of a group record.
 *
 * <pre>
 *   POST /v1/group                     form {name, leader, purpose?, requested_by?, ends?, ack_deadline?,
 *                                      ack_deadline_by_topic?, serves?}; a disbanded name is re-formed by the same call
 *   GET  /v1/group/&lt;name&gt;              the record (members in join order: GET /v1/identity/groups/&lt;name&gt;)
 *   POST /v1/group/&lt;name&gt;/set          {leader?, ends?, ack_deadline?, ack_deadline_by_topic?, serves?} or {ended_at}
 *   POST /v1/group/&lt;name&gt;/disband      end the group now
 *   POST /v1/group/&lt;name&gt;/override     {leader} or {ended_at}: the tenant credential only, for a leader that cannot write
 * </pre>
 *
 * <p><b>Level 1</b> (RDR-252 "Trust and attestation"): a write is checked by {@link IdentityFence#requireOwner}
 * against the leader of the record, at the leader's current generation; a first write and a re-form against the
 * leader the call names. A write with no identity token is refused ({@code 403 identity_token_required}) <em>even
 * when the leader is {@code unleased}</em>: RDR-253's unattested exception covers memberships only, since anyone
 * could rewrite a record whose leader writes without a token. The one write with no token is the operator
 * override, which only the tenant credential reaches and which is audited.
 *
 * <p>Both the tenant and the data credential carry an identity token (the token names the identity, the
 * credential names the tenant), so both may call the identity-checked routes; reads admit both too.
 */
public final class GroupHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(GroupHandler.class);

    private static final ObjectMapper MAPPER = new ObjectMapper()
        .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false);
    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() { };

    private final GroupRepository repo;
    private final IdentityFence fence;

    public GroupHandler(GroupRepository repo, IdentityFence fence) {
        this.repo = repo;
        this.fence = fence;
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        String tenant = RequestContext.tenant();
        if (tenant == null) {
            HttpUtil.send(exchange, 500, "{\"error\":\"internal: tenant not set\"}");
            return;
        }
        String path = exchange.getRequestURI().getPath().replaceFirst("^/v1/group/?", "");
        String method = exchange.getRequestMethod().toUpperCase(Locale.ROOT);
        String[] seg = path.isEmpty() ? new String[0] : path.split("/", -1);
        try {
            Map<String, Object> out = dispatch(exchange, tenant, method, seg);
            HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(out));
        } catch (IdentityException e) {
            IdentityFence.refuse(exchange, e);
        } catch (IllegalArgumentException e) {
            HttpUtil.send(exchange, 400, "{\"error\":" + MAPPER.writeValueAsString(e.getMessage()) + "}");
        } catch (Exception e) {
            if (!HttpUtil.sendTypedDbError(exchange, e, log, "group_handler", "path=" + path + " tenant=" + tenant)) {
                log.error("event=group_handler_error path={} tenant={} error={}", path, tenant, e.getMessage(), e);
                HttpUtil.send(exchange, 500, "{\"error\":\"internal server error\"}");
            }
        }
    }

    private static boolean tenantScope(String scope) {
        return TokenStore.SCOPE_TENANT.equals(scope) || TokenStore.SCOPE_ROOT.equals(scope);
    }

    private static boolean dataScope(String scope) {
        return TokenStore.SCOPE_DATA.equals(scope);
    }

    private static IdentityException forbidden(String scope, String what) {
        return new IdentityException(403, "forbidden", "a '" + scope + "' credential may not " + what + " on /v1/group");
    }

    private Map<String, Object> dispatch(HttpExchange ex, String tenant, String method, String[] seg) throws IOException {
        String scope = RequestContext.scope();
        if (seg.length == 0) {
            if (!"POST".equals(method)) throw new IdentityException(405, "method_not_allowed", "method not allowed");
            if (!tenantScope(scope) && !dataScope(scope)) throw forbidden(scope, "form a group");
            return form(ex, tenant);
        }
        String name = seg[0];
        if (seg.length == 1) {
            if (!"GET".equals(method)) throw new IdentityException(405, "method_not_allowed", "method not allowed");
            if (!tenantScope(scope) && !dataScope(scope)) throw forbidden(scope, "read a group");
            return repo.read(tenant, name);
        }
        if (seg.length != 2 || !"POST".equals(method)) {
            throw new IdentityException(seg.length != 2 ? 404 : 405, seg.length != 2 ? "not_found" : "method_not_allowed",
                seg.length != 2 ? "not found" : "method not allowed");
        }
        switch (seg[1]) {
            case "set" -> {
                if (!tenantScope(scope) && !dataScope(scope)) throw forbidden(scope, "set a group");
                Map<String, Object> body = readBody(ex);
                String leader = writer(ex, tenant, name);
                return repo.update(tenant, name, leader, generation(), actor(), body);
            }
            case "disband" -> {
                if (!tenantScope(scope) && !dataScope(scope)) throw forbidden(scope, "disband a group");
                String leader = writer(ex, tenant, name);
                return repo.disband(tenant, name, leader, generation(), actor());
            }
            case "override" -> {
                if (!tenantScope(scope)) throw forbidden(scope, "override a group");
                return repo.override(tenant, name, operator(scope), readBody(ex));
            }
            default -> throw new IdentityException(404, "not_found", "not found");
        }
    }

    private Map<String, Object> form(HttpExchange ex, String tenant) throws IOException {
        Map<String, Object> body = readBody(ex);
        Object leader = body.get("leader");
        if (!(leader instanceof String l)) {
            throw new IdentityException(400, "bad_field", "'leader' is required and is an identity id");
        }
        // A first write, and a re-form: the writer is the leader the call names.
        requireToken(ex, tenant, l);
        return repo.form(tenant, actor(), generation(), body);
    }

    /** The level-1 check against the stored leader of {@code name}; returns that leader. */
    private String writer(HttpExchange ex, String tenant, String name) {
        String leader = repo.leaderOf(tenant, name);
        requireToken(ex, tenant, leader);
        return leader;
    }

    private void requireToken(HttpExchange ex, String tenant, String leader) {
        if (RequestContext.identity() == null) {
            throw new IdentityException(403, "identity_token_required",
                "a write to a group needs the identity token of its leader " + leader
                    + " (no unattested exception applies to a group record)", Map.of("identity", leader));
        }
        fence.requireOwner(ex, tenant, leader);
    }

    /** The generation of the request's identity token, which {@code requireToken} has just checked against the leader. */
    private static int generation() {
        Integer g = RequestContext.identityGeneration();
        return g == null ? -1 : g;
    }

    private static String actor() {
        return "identity:" + RequestContext.identity();
    }

    private static String operator(String scope) {
        String credential = RequestContext.credentialHash();
        String named = credential == null ? "" : ":" + credential.substring(0, Math.min(12, credential.length()));
        return "operator:" + scope + named;
    }

    private static Map<String, Object> readBody(HttpExchange exchange) throws IOException {
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
            return new LinkedHashMap<>(body);
        }
    }
}
