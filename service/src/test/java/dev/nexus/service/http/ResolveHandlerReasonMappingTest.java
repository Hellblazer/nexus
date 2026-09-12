// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import dev.nexus.service.resolver.ResolveResult;
import dev.nexus.service.resolver.UriSchemeHandler;
import dev.nexus.service.resolver.UriSchemeResolverRegistry;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-169 G3 fix round 1 (bead nexus-aphki, T2 critique-nexus-aphki-rdr169-gap3-resolve-2026-09-11
 * Critical) — deterministic coverage of {@link ResolveHandler#handle}'s status-code branching,
 * independent of any REAL {@link dev.nexus.service.resolver.UriSchemeHandler} implementation or
 * {@code PgVectorRepository}.
 *
 * <p>Hermetic: a bare {@link HttpServer} bound to {@link ResolveHandler} directly, no {@code
 * NexusService}/DataSource/Postgres involved (mirrors {@code StatusHandlerTest}'s pattern) — this
 * suite needs no substrate the fast loop lacks. {@link RequestContext#set}/{@code clear} are
 * package-private and called directly here (this file is in the same {@code
 * dev.nexus.service.http} package) to stamp the tenant {@link AuthFilter} would otherwise supply,
 * so {@link ResolveHandler#requireTenant} sees a resolved tenant without a real auth stack.
 *
 * <p>A hand-rolled stub {@link UriSchemeHandler} registered under a synthetic scheme lets each
 * test dictate the exact {@link ResolveResult#errorReason()} the registry returns, so every
 * branch in {@code ResolveHandler#resolveAndRespond} is exercised directly: this is what the
 * Critical finding's own fix needs proven, since neither real handler (Chroma or Https) can be
 * driven to every reason token without a live network call or a populated Postgres row (those
 * two real-row cases stay in {@code ResolveHandlerTest}, the Testcontainers-backed suite).
 *
 * <p>Cases:
 * <ol>
 *   <li>400 — both {@code source_uri} and {@code collection}/{@code chash} given.</li>
 *   <li>400 — neither form given.</li>
 *   <li>400 — non-canonical {@code chash}.</li>
 *   <li>502 — a reason token that is neither {@code "reference_only"} nor {@code "malformed"}/
 *       {@code "unreachable"} (a genuine fetch/resolver failure, e.g. an https 200-empty-body).</li>
 *   <li>422 — reason {@code "malformed"} via a registered handler (not the unregistered-scheme
 *       path, which {@code ResolveHandlerTest} already covers).</li>
 *   <li>422 — reason {@code "unreachable"} via a registered handler.</li>
 *   <li>503 — {@code (collection, chash)} form with no {@code PgVectorRepository} wired.</li>
 * </ol>
 */
class ResolveHandlerReasonMappingTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String TENANT = "reason-mapping-tenant";

    private HttpServer server;
    private String baseUrl;
    private final HttpClient http = HttpClient.newHttpClient();

    /** Records the reason/detail a stub handler should return for {@code stub://} URIs. */
    private static final class StubHandler implements UriSchemeHandler {
        private final String reason;
        private final String detail;

        StubHandler(String reason, String detail) {
            this.reason = reason;
            this.detail = detail;
        }

        @Override
        public ResolveResult resolve(String uri, String tenant) {
            return ResolveResult.error(reason, detail);
        }
    }

    /**
     * Starts a bare HttpServer serving {@code /v1/vectors/resolve} through a wrapper that stamps
     * {@link RequestContext} before delegating to a real {@link ResolveHandler} — the
     * package-private AuthFilter contract, replicated directly rather than standing up the real
     * filter (which needs a TokenStore/DataSource this suite has none of).
     */
    private void start(UriSchemeResolverRegistry registry, dev.nexus.service.vectors.PgVectorRepository pgRepo)
            throws IOException {
        ResolveHandler resolveHandler = new ResolveHandler(registry, pgRepo);
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/vectors/resolve", exchange -> {
            RequestContext.set(new RequestContext.Principal(
                    TENANT, "reason-mapping-session", true, false, "tenant", "test-hash"));
            try {
                resolveHandler.handle(exchange);
            } finally {
                RequestContext.clear();
            }
        });
        server.start();
        baseUrl = "http://127.0.0.1:" + server.getAddress().getPort();
    }

    @AfterEach
    void stop() {
        if (server != null) server.stop(0);
    }

    private HttpResponse<String> post(Object body) throws Exception {
        var req = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + "/v1/vectors/resolve"))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
                .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> jsonBody(HttpResponse<String> resp) throws Exception {
        return MAPPER.readValue(resp.body(), Map.class);
    }

    // -------------------------------------------------------------------------
    // 400 sub-cases
    // -------------------------------------------------------------------------

    @Test
    void bothFormsGiven_returns400() throws Exception {
        start(new UriSchemeResolverRegistry(), null);

        var resp = post(Map.of(
                "source_uri", "stub://irrelevant",
                "collection", "col",
                "chash", "a".repeat(64)));

        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(400);
        assertThat((String) jsonBody(resp).get("error")).contains("not both");
    }

    @Test
    void neitherFormGiven_returns400() throws Exception {
        start(new UriSchemeResolverRegistry(), null);

        var resp = post(Map.of());

        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(400);
        assertThat((String) jsonBody(resp).get("error")).contains("requires");
    }

    @Test
    void nonCanonicalChash_returns400() throws Exception {
        start(new UriSchemeResolverRegistry(), null);

        var resp = post(Map.of("collection", "col", "chash", "not-hex-at-all"));

        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(400);
        assertThat((String) jsonBody(resp).get("error")).contains("chash");
    }

    // -------------------------------------------------------------------------
    // 502 — a genuine fetch/resolver failure (any reason other than
    // reference_only/malformed/unreachable)
    // -------------------------------------------------------------------------

    @Test
    void otherReason_maps502_carryingReasonAndDetail() throws Exception {
        var registry = new UriSchemeResolverRegistry();
        // "empty" is HttpsSchemeHandler's own reason for a 200-with-blank-body fetch --
        // the one live-reachable 502 case (RDR-169 G3 fix round 1) -- reproduced here via a
        // stub so the test is deterministic and needs no live socket.
        registry.register("stub", new StubHandler("empty", "empty body at 'stub://x' (HTTP 200)"));
        start(registry, null);

        var resp = post(Map.of("source_uri", "stub://x"));

        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(502);
        Map<String, Object> body = jsonBody(resp);
        assertThat(body.get("error")).isEqualTo("empty");
        assertThat(body.get("detail")).isEqualTo("empty body at 'stub://x' (HTTP 200)");
    }

    // -------------------------------------------------------------------------
    // 422 — caller-shaped URI errors from a REGISTERED handler (distinct from the
    // unregistered-scheme 422 ResolveHandlerTest already covers)
    // -------------------------------------------------------------------------

    @Test
    void malformedReason_maps422() throws Exception {
        var registry = new UriSchemeResolverRegistry();
        registry.register("stub", new StubHandler("malformed", "malformed chash segment"));
        start(registry, null);

        var resp = post(Map.of("source_uri", "stub://x"));

        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(422);
        Map<String, Object> body = jsonBody(resp);
        assertThat(body.get("error")).isEqualTo("malformed");
        assertThat(body.get("detail")).isEqualTo("malformed chash segment");
    }

    @Test
    void unreachableReason_maps422() throws Exception {
        var registry = new UriSchemeResolverRegistry();
        registry.register("stub", new StubHandler("unreachable", "missing collection"));
        start(registry, null);

        var resp = post(Map.of("source_uri", "stub://x"));

        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(422);
        Map<String, Object> body = jsonBody(resp);
        assertThat(body.get("error")).isEqualTo("unreachable");
        assertThat(body.get("detail")).isEqualTo("missing collection");
    }

    // -------------------------------------------------------------------------
    // 404 — reference_only, mapped independent of which real handler produces it
    // -------------------------------------------------------------------------

    @Test
    void referenceOnlyReason_maps404_echoingTheRequestedUri() throws Exception {
        var registry = new UriSchemeResolverRegistry();
        registry.register("stub", new StubHandler("reference_only",
                "chunk (col, abc123) has no stored text"));
        start(registry, null);

        var resp = post(Map.of("source_uri", "stub://col/abc123"));

        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(404);
        Map<String, Object> body = jsonBody(resp);
        assertThat(body.get("error")).isEqualTo("reference_only");
        assertThat(body.get("detail")).isEqualTo("chunk (col, abc123) has no stored text");
        // The REQUESTED uri is echoed back (ResolveResult.error carries a null sourceUri()).
        assertThat(body.get("source_uri")).isEqualTo("stub://col/abc123");
    }

    // -------------------------------------------------------------------------
    // 503 — (collection, chash) form with no PgVectorRepository wired
    // -------------------------------------------------------------------------

    @Test
    void noPgRepo_chashForm_returns503() throws Exception {
        start(new UriSchemeResolverRegistry(), null);

        var resp = post(Map.of("collection", "col", "chash", "a".repeat(64)));

        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(503);
        assertThat((String) jsonBody(resp).get("error")).contains("pgvector repository");
    }
}
