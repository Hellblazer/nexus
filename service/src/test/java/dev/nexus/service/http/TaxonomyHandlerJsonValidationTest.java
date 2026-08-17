// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;
import dev.nexus.service.db.TaxonomyRepository;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.TaxonomyCentroidRepository;
import org.junit.jupiter.api.Test;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-cefa1.6 — TaxonomyHandler's handler-side JSON validation for
 * {@code link_types} (taxonomy-008-link-types-jsonb.xml made it jsonb).
 *
 * <p>Mirrors {@code PlanHandlerJsonValidationTest} (nexus-cefa1.5) and
 * {@code AspectHandlerJsonValidationTest} (nexus-cefa1.4) exactly: malformed
 * JSON in {@code link_types} 400s HERE, at the handler ({@code
 * HttpUtil#rejectMalformedJson}, the shared helper all three handlers now
 * call), NOT via the class-22 SQLSTATE branch a malformed value would
 * otherwise hit at the repository. The SQLSTATE branch ({@code
 * HttpUtil.sendTypedDbError}, 422) stays wired as a backstop for any write
 * path that bypasses this handler (e.g. a direct repository call).
 *
 * <p>Unlike {@code plan_json} (required at the handler via {@code
 * requireString}), {@code link_types} is optional at THIS handler
 * (@code optStringOrNull}) — its NOT NULL enforcement lives one layer down,
 * in {@code TaxonomyRepository}'s {@code JsonbSupport.jsonbRequired} call,
 * which runs before the (null-backed) {@code TenantScope} is ever touched.
 * A missing/blank link_types therefore still 400s end to end, just via that
 * repository-layer backstop rather than the handler's JSON-shape gate —
 * covered below.
 *
 * <p>No database needed: a malformed/missing link_types 400s before the
 * repository ever reaches its {@link TenantScope}, and {@code
 * handler.handle(...)} catches every exception internally (never
 * propagates), so a repository backed by a null {@link javax.sql.DataSource}
 * is safe here — the same setup {@code PlanHandlerJsonValidationTest} uses.
 */
class TaxonomyHandlerJsonValidationTest {

    private static final String TENANT = "cefa1-taxonomy-link-types-validation-tenant";

    private final TaxonomyHandler handler = new TaxonomyHandler(
        new TaxonomyRepository(new TenantScope(null)),
        new TaxonomyCentroidRepository(new TenantScope(null)));

    @Test
    void upsertLink_malformedLinkTypes_returns400() throws Exception {
        CapturingExchange ex = post("/v1/taxonomy/links/upsert",
            "{\"from_topic_id\":1,\"to_topic_id\":2,\"link_types\":\"not-json-at-all\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("malformed link_types must 400 at the handler").isEqualTo(400);
        assertThat(ex.bodyString()).contains("link_types").contains("valid JSON");
    }

    @Test
    void upsertLink_validLinkTypes_doesNotFailJsonValidation() throws Exception {
        // handle() never propagates, so any status OTHER than 400 proves the
        // request cleared the JSON-validity gate (it then fails downstream
        // against the null DataSource, which is expected and irrelevant here).
        CapturingExchange ex = post("/v1/taxonomy/links/upsert",
            "{\"from_topic_id\":1,\"to_topic_id\":2,\"link_types\":\"[\\\"cooccurrence\\\"]\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("valid link_types must not 400 the JSON-validity gate").isNotEqualTo(400);
    }

    @Test
    void upsertLink_missingLinkTypes_returns400ViaRepositoryBackstop() throws Exception {
        // No link_types key at all: rejectMalformedJson skips a non-String
        // (null) value (nothing to 400 on JSON-shape grounds), but
        // TaxonomyRepository.upsertTopicLink's jsonbRequired backstop still
        // 400s — link_types is NOT NULL with no NULLIF step in its changeset,
        // so a null value is never valid, missing or malformed alike.
        CapturingExchange ex = post("/v1/taxonomy/links/upsert",
            "{\"from_topic_id\":1,\"to_topic_id\":2}");
        handleWithTenant(ex);
        assertThat(ex.status).as("missing link_types must still 400 (repository backstop)").isEqualTo(400);
        assertThat(ex.bodyString()).contains("link_types");
    }

    @Test
    void importLink_malformedLinkTypes_returns400() throws Exception {
        CapturingExchange ex = post("/v1/taxonomy/import/link",
            "{\"from_topic_id\":1,\"to_topic_id\":2,\"link_count\":1,"
            + "\"link_types\":\"not-json-at-all\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("malformed link_types must 400 on /import/link too").isEqualTo(400);
        assertThat(ex.bodyString()).contains("link_types").contains("valid JSON");
    }

    @Test
    void importBatch_malformedLinkTypes_namesFailingRowIndex() throws Exception {
        CapturingExchange ex = post("/v1/taxonomy/import_batch",
            "{\"kind\":\"link\",\"rows\":["
            + "{\"from_topic_id\":1,\"to_topic_id\":2,\"link_count\":1,"
            + "\"link_types\":\"[\\\"cooccurrence\\\"]\"},"
            + "{\"from_topic_id\":1,\"to_topic_id\":3,\"link_count\":1,"
            + "\"link_types\":\"not-json-at-all\"}"
            + "]}");
        handleWithTenant(ex);
        assertThat(ex.status).as("malformed link_types in a batch row must 400").isEqualTo(400);
        assertThat(ex.bodyString())
            .as("error must name the failing row index (row 1, the second row)")
            .contains("rows[1]");
    }

    @Test
    void importBatch_nativeArrayLinkTypes_isCoercedAndRejectedLikeSingleRowPaths() throws Exception {
        // A client that sends link_types as a NATIVE JSON array (not the
        // pre-stringified form the wire contract specifies) must not skip the
        // gate: the batch path coerces through optStringOrNull exactly like the
        // single-row /upsert/link and /import/link paths, so the List's
        // toString() form ("[a, b]", not JSON) is what gets validated and 400s,
        // naming the row (substantive-critique finding, nexus-cefa1.6).
        CapturingExchange ex = post("/v1/taxonomy/import_batch",
            "{\"kind\":\"link\",\"rows\":["
            + "{\"from_topic_id\":1,\"to_topic_id\":2,\"link_count\":1,"
            + "\"link_types\":[\"cooccurrence\",\"projection\"]}"
            + "]}");
        handleWithTenant(ex);
        assertThat(ex.status).as("native-array link_types in a batch row must 400 at the handler").isEqualTo(400);
        assertThat(ex.bodyString()).contains("rows[0]").contains("link_types");
    }

    @Test
    void importBatch_otherKind_doesNotValidateLinkTypes() throws Exception {
        // The rows[i] link_types gate is scoped to kind="link" only — a "topic"
        // batch's rows are untouched by this bead's validation, so a garbage
        // "link_types"-named field on an unrelated kind must not spuriously 400.
        CapturingExchange ex = post("/v1/taxonomy/import_batch",
            "{\"kind\":\"topic\",\"rows\":["
            + "{\"id\":1,\"label\":\"t\",\"collection\":\"c\",\"doc_count\":0,"
            + "\"created_at\":\"2026-01-01T00:00:00Z\",\"review_status\":\"pending\","
            + "\"link_types\":\"not-json-at-all\"}"
            + "]}");
        handleWithTenant(ex);
        assertThat(ex.status).as("non-link kind must not 400 on the link_types gate").isNotEqualTo(400);
    }

    // ── Helpers (mirrors PlanHandlerJsonValidationTest's CapturingExchange) ─────

    private void handleWithTenant(CapturingExchange ex) throws Exception {
        RequestContext.set(new RequestContext.Principal(TENANT, null, false, false, "tenant", "test-credential-hash"));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }
    }

    private static CapturingExchange post(String path, String jsonBody) {
        return new CapturingExchange("POST", URI.create(path), jsonBody);
    }

    /** Minimal {@link HttpExchange} that captures the response status + body. */
    private static final class CapturingExchange extends HttpExchange {
        private final String method;
        private final URI uri;
        private final InputStream requestBody;
        private final Headers responseHeaders = new Headers();
        private final ByteArrayOutputStream responseBody = new ByteArrayOutputStream();
        int status = -1;

        CapturingExchange(String method, URI uri, String jsonBody) {
            this.method = method;
            this.uri = uri;
            this.requestBody = new ByteArrayInputStream(jsonBody.getBytes(StandardCharsets.UTF_8));
        }

        String bodyString() {
            return responseBody.toString(StandardCharsets.UTF_8);
        }

        @Override public Headers getRequestHeaders() { return new Headers(); }
        @Override public Headers getResponseHeaders() { return responseHeaders; }
        @Override public URI getRequestURI() { return uri; }
        @Override public String getRequestMethod() { return method; }
        @Override public HttpContext getHttpContext() { return null; }
        @Override public void close() { }
        @Override public InputStream getRequestBody() { return requestBody; }
        @Override public OutputStream getResponseBody() { return responseBody; }
        @Override public void sendResponseHeaders(int code, long contentLength) { this.status = code; }
        @Override public InetSocketAddress getRemoteAddress() { return null; }
        @Override public int getResponseCode() { return status; }
        @Override public InetSocketAddress getLocalAddress() { return null; }
        @Override public String getProtocol() { return "HTTP/1.1"; }
        @Override public Object getAttribute(String name) { return null; }
        @Override public void setAttribute(String name, Object value) { }
        @Override public void setStreams(InputStream i, OutputStream o) { }
        @Override public HttpPrincipal getPrincipal() { return null; }
    }
}
