// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.TaxonomyRepository;
import dev.nexus.service.db.TenantScope;
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

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-172 follow-up (nexus-7e057) — {@code POST /v1/taxonomy/assignments/assign}
 * maps a class-23 integrity violation to a typed 409, ahead of the generic 500.
 *
 * <p>{@code TaxonomyRepository.assignTopic} inserts into
 * {@code topic_assignments}, whose {@code topic_id} column has a real FK to
 * {@code topics(id)} (taxonomy-001-baseline). A client-supplied {@code topic_id}
 * that does not exist — the same bug class fixed for AspectHandler in
 * nexus-gfl3y (bug nexus-ov0sw) — previously hit the generic
 * {@code catch (Exception)} → bare 500. {@link HttpUtil#sqlState23} now catches
 * it first.
 *
 * <p>Hermetic: Testcontainers PG (real {@link TaxonomyRepository}); drives the
 * handler directly via {@link TaxonomyHandler#handle} with a capturing
 * {@link HttpExchange}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TaxonomyHandlerAssignFkTest {

    private static final String SVC_ROLE = "svc_tax_assign_fk_test";
    private static final String SVC_PASS = "svc_tax_assign_fk_test_pass";
    private static final String TENANT   = "tax-assign-fk-tenant";
    // nexus-tk070.p3c: doc_id is bytea now; TaxonomyRepository.assignOne parses it
    // via Chash.fromHex, so the wire value here must be genuine 64-lowercase-hex,
    // not the old free-text "some-doc" placeholder.
    private static final String DOC_ID_HEX = hexChash("some-doc");

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    TaxonomyRepository repo;
    TaxonomyHandler handler;
    com.zaxxer.hikari.HikariDataSource svcDs;

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
        repo = new TaxonomyRepository(tenantScope);
        handler = new TaxonomyHandler(repo, null);  // centroid repo unused by /assignments/assign
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test
    void assign_nonexistentTopicId_returns409_withSqlstate() throws Exception {
        // RDR-194 D1/P3b (nexus-tk070.p3b): source_collection is now NOT NULL
        // and is auto-stubbed by BOTH branches (RDR-156 P0.2), so a real value
        // is required here to isolate the topic_id FK specifically -- without
        // it, the NOT NULL constraint fires first and this test would observe
        // sqlstate 23502, not the topic_id FK's 23503 it means to exercise.
        //
        // RDR-194 P5a (nexus-tk070.p5a): taxonomy-014-3 repoints
        // topic_assignments_topic_id_fkey onto the tenant-scoped composite
        // fk_topic_assignments_topic_tenant AND (same phase) taxonomy-012's
        // topic_assignments_chunk_fk (doc_id -> nexus.chunks) is now an
        // independently-live constraint on this same INSERT. Since neither
        // topic 999999 nor a matching chunk for DOC_ID_HEX/"coll" exists, BOTH
        // FKs are violated by this statement, and repointing topic_id's FK
        // changed its constraint OID (dropped and re-added), which changed
        // WHICH violation Postgres reports first. seedChunk isolates the
        // topic_id FK specifically, matching assign_existingTopicId_stillReturns200's
        // own isolation discipline below.
        seedChunk(TENANT, "coll", DOC_ID_HEX, 384);
        CapturingExchange ex = post("/v1/taxonomy/assignments/assign",
            "{\"doc_id\":\"" + DOC_ID_HEX + "\",\"topic_id\":999999,\"assigned_by\":\"manual\","
            + "\"source_collection\":\"coll\"}");
        handleWithTenant(ex);
        assertThat(ex.status)
            .as("a nonexistent topic_id violates the tenant-scoped topics FK → typed 409, not 500")
            .isEqualTo(409);
        assertThat(ex.bodyString()).contains("\"sqlstate\":\"23503\"");
        assertThat(ex.bodyString())
            .contains("\"error\":\"integrity constraint violation\"");

        // nexus-0ehwe item 6 SUPERSEDES the original `.doesNotContain(
        // "topic_assignments")` here. That assertion's intent was "send a CLEAN
        // TYPED body, not the raw driver message" — not "never name a
        // constraint". A bare "integrity constraint violation" is
        // undiagnosable from the client: it cost the entire nexus-pbawi
        // investigation, where the real answer (a TUMBLER collision on
        // catalog_documents_pkey, not the source_uri arbiter the insert
        // declares) was in the driver's exception the whole time and was being
        // discarded. So the body now carries the constraint NAME as a
        // structured field.
        //
        // Constraint name updated for RDR-194 P5a (nexus-tk070.p5a):
        // topic_assignments_topic_id_fkey was DROPPED and re-ADDED as the
        // tenant-scoped composite fk_topic_assignments_topic_tenant
        // (taxonomy-014-3).
        assertThat(ex.bodyString())
            .as("the violated constraint must be nameable by the caller")
            .contains("\"constraint\":\"fk_topic_assignments_topic_tenant\"");

        // The ORIGINAL intent still holds: no raw driver prose. A structured
        // name is not the same as echoing the exception text, which would carry
        // the failing SQL, the offending key values, and PG's own hint lines.
        assertThat(ex.bodyString())
            .as("the raw driver message must not be echoed into the response")
            .doesNotContain("Detail:")
            .doesNotContain("Key (")
            .doesNotContain("insert or update on table");
    }

    @Test
    void assign_existingTopicId_stillReturns200() throws Exception {
        // Non-regression: register a real topic first, then a valid assignment
        // must still succeed (the guard doesn't over-fire).
        long topicId = repo.insertTopic(TENANT, "fk-test-topic", null, "coll", 0, "2026-07-01T00:00:00Z", null);
        // RDR-194 P3d (nexus-tk070.p3d): topic_assignments_chunk_fk now requires
        // a matching nexus.chunks row for this assign to succeed at all.
        seedChunk(TENANT, "coll", DOC_ID_HEX, 384);
        CapturingExchange ex = post("/v1/taxonomy/assignments/assign",
            "{\"doc_id\":\"" + DOC_ID_HEX + "\",\"topic_id\":" + topicId + ",\"assigned_by\":\"manual\","
            + "\"source_collection\":\"coll\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("existing topic_id: assignment succeeds").isEqualTo(200);
    }

    @Test
    void assign_missingSourceCollection_nonProjection_returns409() throws Exception {
        // Substantive-critic Sig-1 (RDR-194 P3b/nexus-11pe7 stacked review,
        // T2 nexus/rdr194-p3b-substantive-critique-2026-08-16 [22755]): the
        // wire endpoint accepts source_collection as OPTIONAL (Java
        // optStringOrNull), and TaxonomyRepository.assignOne's non-projection
        // branch now writes it verbatim -- a caller (old SDK, an integration
        // outside the in-tree writer-sweep this phase audited) posting
        // assigned_by != "projection" with source_collection omitted hits
        // topic_assignments.source_collection's NOT NULL constraint
        // (SQLSTATE 23502) at INSERT. This pins that the degrade is graceful
        // (HttpUtil.sendTypedDbError's existing class-23 branch already
        // covers 23502 generically, ahead of the generic 500) rather than
        // merely asserted by review narrative -- the sibling
        // assign_nonexistentTopicId_returns409_withSqlstate test above had to
        // be PATCHED to supply source_collection precisely to avoid tripping
        // this exposure while isolating a DIFFERENT violation (the topic_id
        // FK); this test isolates the NOT NULL violation itself, on an
        // otherwise-valid request (existing topic_id, real assigned_by).
        long topicId = repo.insertTopic(
            TENANT, "missing-sc-topic", null, "coll", 0, "2026-07-01T00:00:00Z", null);
        CapturingExchange ex = post("/v1/taxonomy/assignments/assign",
            "{\"doc_id\":\"" + DOC_ID_HEX + "\",\"topic_id\":" + topicId + ",\"assigned_by\":\"manual\"}");
        handleWithTenant(ex);
        assertThat(ex.status)
            .as("a non-projection assign with no source_collection violates the NOT NULL "
                + "constraint -> typed 409, not a raw 500")
            .isEqualTo(409);
        assertThat(ex.bodyString()).contains("\"sqlstate\":\"23502\"");
        assertThat(ex.bodyString())
            .contains("\"error\":\"integrity constraint violation\"");
        // Same info-disclosure discipline as the topic_id-FK test above: no
        // raw driver prose (offending value, PG hint lines) in the body.
        assertThat(ex.bodyString())
            .as("the raw driver message must not be echoed into the response")
            .doesNotContain("Detail:")
            .doesNotContain("null value in column");
    }

    // ── helpers ──────────────────────────────────────────────────────────────

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

    /**
     * Insert a minimal nexus.chunks row (RDR-194 P3d, nexus-tk070.p3d): every
     * topic_assignments row now requires a matching (tenant_id, source_collection,
     * doc_id) -> chunks(tenant_id, collection, chash) parent via
     * topic_assignments_chunk_fk. Also registers the collection (ON CONFLICT DO
     * NOTHING).
     */
    private void seedChunk(String tenant, String collection, String chashHex, int dim) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + tenant + "', '"
                + collection + "') ON CONFLICT (tenant_id, name) DO NOTHING");
            String embeddingCol = "embedding_" + dim;
            su.createStatement().execute(
                "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, " + embeddingCol + ") VALUES " +
                "('" + tenant + "', '" + collection + "', decode('" + chashHex + "', 'hex'), 'fk-assign-test chunk', " +
                "('[" + "0.1,".repeat(dim - 1) + "0.1]')::vector) " +
                "ON CONFLICT (tenant_id, collection, chash) DO NOTHING");
        }
    }

    /** Genuine 64-lowercase-hex sha256 chash — required for topic_assignments.doc_id
     *  (bytea since nexus-tk070.p3c). */
    private static String hexChash(String seed) {
        try {
            byte[] digest = java.security.MessageDigest.getInstance("SHA-256")
                .digest(seed.getBytes(StandardCharsets.UTF_8));
            return java.util.HexFormat.of().formatHex(digest);
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
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
