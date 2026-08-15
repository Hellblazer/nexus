// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
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
 * RDR-172 follow-up (nexus-7e057) — {@code POST /v1/catalog/manifest/write} and
 * {@code /manifest/append} map a class-23 integrity violation to a typed 409,
 * ahead of the generic 500.
 *
 * <p>{@code writeManifest}/{@code appendManifestChunks} insert into
 * {@code catalog_document_chunks}, whose {@code doc_id} column has a real FK to
 * {@code catalog_documents(tenant_id, tumbler)} (fk-001). A non-blank but
 * UNREGISTERED {@code doc_id} — the exact bug class fixed for AspectHandler in
 * nexus-gfl3y (bug nexus-ov0sw) — previously hit the generic
 * {@code catch (Exception)} → bare 500. {@link HttpUtil#sqlState23} (extracted
 * from {@code AspectHandler}) now catches it first.
 *
 * <p>Hermetic: Testcontainers PG (real {@link CatalogRepository}); drives the
 * handler directly via {@link CatalogHandler#handle} with a capturing
 * {@link HttpExchange} (same pattern as {@code AspectHandlerEnqueueErrorTest}).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerManifestFkTest {

    private static final String SVC_ROLE = "svc_cat_manifest_fk_test";
    private static final String SVC_PASS = "svc_cat_manifest_fk_test_pass";
    private static final String TENANT   = "cat-manifest-fk-tenant";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    CatalogRepository repo;
    CatalogHandler handler;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN "
                + "CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "'; END IF; END $$");
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN "
                + "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; END IF; END $$");
        }
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db).update(new Contexts());
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            for (String table : java.util.List.of(
                    "catalog_owners", "catalog_documents", "catalog_document_chunks", "catalog_collections")) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus." + table + " TO " + SVC_ROLE);
            }
            su.createStatement().execute("ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
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

    @Test
    void manifestWrite_unregisteredDocId_returns409_documentNotFound() throws Exception {
        // nexus-s13u0 (RDR-191 GATE-2): catalog_document_chunks.collection going
        // NOT NULL retired the FK-violation path this test used to exercise —
        // requireDocumentExists's case 1 (no catalog_documents row at all, formerly
        // physicalCollectionOf's case 1 before RDR-191 renamed it to a pure
        // existence check) now throws DocumentNotFoundException BEFORE the INSERT
        // is ever attempted, so Postgres never gets a chance to reject it and
        // there is no SQLState to report. Still a refusal, still 409 — just an
        // explicit throw instead
        // of a caught class-23 violation.
        CapturingExchange ex = post("/v1/catalog/manifest/write",
            "{\"doc_id\":\"unregistered-tumbler-zzz\",\"collection\":\"knowledge__fk__v1\",\"rows\":[{\"position\":0,"
            + "\"chash\":\"" + "a".repeat(64) + "\"}]}");
        handleWithTenant(ex);
        assertThat(ex.status)
            .as("a non-blank UNREGISTERED doc_id is an explicit DocumentNotFoundException -> 409, not 500")
            .isEqualTo(409);
        assertThat(ex.bodyString())
            .as("no SQLException in this path any more -- no \"sqlstate\" key at all")
            .doesNotContain("sqlstate")
            .contains("\"error\":\"manifest write refused: document not registered: unregistered-tumbler-zzz\"");
    }

    @Test
    void manifestAppend_unregisteredDocId_returns409_documentNotFound() throws Exception {
        // Same requireDocumentExists case-1 throw as the write test above —
        // appendManifestChunks resolves through the identical helper.
        CapturingExchange ex = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"unregistered-tumbler-yyy\",\"collection\":\"knowledge__fk__v1\",\"rows\":[{\"position\":0,"
            + "\"chash\":\"" + "b".repeat(64) + "\"}]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(409);
        assertThat(ex.bodyString())
            .doesNotContain("sqlstate")
            .contains("\"error\":\"manifest write refused: document not registered: unregistered-tumbler-yyy\"");
    }

    @Test
    void manifestWrite_registeredDocId_stillReturns200() throws Exception {
        // Non-regression: register the owner + document first, then a valid
        // manifest write must still succeed (the guard doesn't over-fire).
        repo.upsertDocument(TENANT, java.util.Map.of(
            "tumbler", "5.1", "title", "FK test doc", "content_type", "paper",
            "corpus", "knowledge", "physical_collection", "knowledge__fk__v1"));
        // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires a
        // matching nexus.chunks row for this manifest write to succeed.
        stubChunk("knowledge__fk__v1", "c".repeat(64));
        CapturingExchange ex = post("/v1/catalog/manifest/write",
            "{\"doc_id\":\"5.1\",\"collection\":\"knowledge__fk__v1\",\"rows\":[{\"position\":0,"
            + "\"chash\":\"" + "c".repeat(64) + "\"}]}");
        handleWithTenant(ex);
        assertThat(ex.status).as("registered doc_id: manifest write succeeds").isEqualTo(200);
    }

    // ── chash boundary validation (nexus-z4skl, inverted by RDR-180 nexus-jxizy.8) ──

    @Test
    void manifestWrite_full64Chash_accepted200() throws Exception {
        // POLARITY INVERSION (RDR-180): pre-flip a FULL sha256 hex was the classic
        // mistake — canon was 32 chars, so 64 chars died reason-less at the DB CHECK
        // (or, post-z4skl, 400'd at the boundary). Post-flip the FULL 64-hex digest
        // IS the canonical chash — this must now succeed, not 400.
        repo.upsertDocument(TENANT, java.util.Map.of(
            "tumbler", "5.1b", "title", "full64 accept doc", "content_type", "paper",
            "corpus", "knowledge", "physical_collection", "knowledge__fk__v1"));
        String full64 = "a".repeat(64);
        // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires a
        // matching nexus.chunks row for this manifest write to succeed.
        stubChunk("knowledge__fk__v1", full64);
        CapturingExchange ex = post("/v1/catalog/manifest/write",
            "{\"doc_id\":\"5.1b\",\"collection\":\"knowledge__fk__v1\",\"rows\":[{\"position\":0,"
            + "\"chash\":\"" + full64 + "\"}]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(200);
    }

    @Test
    void manifestWrite_legacy32CharChash_rejected400() throws Exception {
        // THE INVERSION: a bare 32-hex value was the canonical accept pre-flip;
        // it is now a legacy reference that must resolve through chash_alias,
        // never accepted fresh at this boundary.
        CapturingExchange ex = post("/v1/catalog/manifest/write",
            "{\"doc_id\":\"5.1\",\"collection\":\"knowledge__fk__v1\",\"rows\":[{\"position\":0,"
            + "\"chash\":\"" + "a".repeat(32) + "\"}]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString())
            .contains("got 32 chars")
            .contains("legacy 32-hex")
            .contains("rows[0]");
    }

    @Test
    void manifestAppend_uppercaseChash_returns400() throws Exception {
        // Must be the FULL 64-char width so the rejection actually exercises the
        // lowercase check, not the (now unrelated) legacy-32-hex length branch.
        CapturingExchange ex = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"5.1\",\"collection\":\"knowledge__fk__v1\",\"rows\":[{\"position\":0,"
            + "\"chash\":\"" + "A".repeat(64) + "\"}]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("LOWERCASE");
    }

    @Test
    void manifestWriteMany_badChashInSecondDoc_wholeBatch400_beforeAnyTxn() throws Exception {
        // Both docs must be validated BEFORE any per-doc transaction: doc 0
        // is valid but must NOT be written when doc 1 carries a bad chash.
        repo.upsertDocument(TENANT, java.util.Map.of(
            "tumbler", "5.2", "title", "batch doc a", "content_type", "paper",
            "corpus", "knowledge", "physical_collection", "knowledge__fk__v1"));
        repo.upsertDocument(TENANT, java.util.Map.of(
            "tumbler", "5.3", "title", "batch doc b", "content_type", "paper",
            "corpus", "knowledge", "physical_collection", "knowledge__fk__v1"));
        // doc 0 carries a VALID full-64-hex chash; doc 1 carries a legacy 32-hex
        // value (the now-invalid shape, RDR-180 inversion) — proves BOTH docs
        // are validated up front, before any per-doc transaction.
        CapturingExchange ex = post("/v1/catalog/manifest/write_many",
            "{\"collection\":\"knowledge__fk__v1\",\"docs\":[{\"doc_id\":\"5.2\",\"rows\":[{\"position\":0,"
            + "\"chash\":\"" + "d".repeat(64) + "\"}]},"
            + "{\"doc_id\":\"5.3\",\"rows\":[{\"position\":0,"
            + "\"chash\":\"" + "e".repeat(32) + "\"}]}]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("docs[1]").contains("got 32 chars").contains("legacy 32-hex");
        // Doc 0's manifest must be untouched (validation ran before ANY txn).
        assertThat(repo.getManifest(TENANT, "5.2")).isEmpty();
    }

    @Test
    void manifestWriteMany_nonMapRow_returns400_notSilentlyFiltered() throws Exception {
        // Review M-1: castRows silently FILTERED junk elements while the repo
        // re-extracted the ORIGINAL list — a null row validated-away at the
        // boundary reappeared mid-transaction and died reason-less into
        // failed_doc_ids. strictRows rejects the shape up front.
        CapturingExchange ex = post("/v1/catalog/manifest/write_many",
            "{\"collection\":\"knowledge__fk__v1\",\"docs\":[{\"doc_id\":\"5.1\",\"rows\":[null,"
            + "{\"position\":0,\"chash\":\"cccccccccccccccccccccccccccccccc\"}]}]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("rows[0]").contains("must be an object");
    }

    @Test
    void manifestWrite_missingChash_returns400() throws Exception {
        CapturingExchange ex = post("/v1/catalog/manifest/write",
            "{\"doc_id\":\"5.1\",\"collection\":\"knowledge__fk__v1\",\"rows\":[{\"position\":0}]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("'chash' required");
    }

    // ── per-doc failure reasons (nexus-fhhwf) ────────────────────────────────

    @Test
    void manifestWriteMany_unregisteredDoc_failedCarriesReason() throws Exception {
        // nexus-fhhwf: the per-doc catch used to swallow the CAUSE — a
        // constraint violation surfaced as a bare id in failed_doc_ids
        // (3 deploy-gate iterations on the v0.1.24 probe). The response
        // carries {failed:[{doc_id, reason}]} alongside.
        //
        // nexus-s13u0 (RDR-191 GATE-2): this doc_id's failure used to be a real
        // PSQLException (FK violation, sqlstate 23503, constraint
        // fk_catalog_chunks_catalog_doc) because the INSERT was attempted and
        // Postgres rejected it. Now requireDocumentExists's case 1 throws
        // DocumentNotFoundException BEFORE any INSERT is attempted --
        // failureDetail's non-SQL allowlist branch reports its message
        // verbatim and sets no "sqlstate" key at all (that key only appears
        // for a real java.sql.SQLException with a SQLState).
        CapturingExchange ex = post("/v1/catalog/manifest/write_many",
            "{\"collection\":\"knowledge__fk__v1\",\"docs\":[{\"doc_id\":\"never-registered-zz\",\"rows\":[{\"position\":0,"
            + "\"chash\":\"" + "f".repeat(64) + "\"}]}]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(200);
        String body = ex.bodyString();
        assertThat(body).contains("\"failed_doc_ids\":[\"never-registered-zz\"]");  // back-compat
        assertThat(body)
            .doesNotContain("sqlstate")
            .contains("\"reason\":\"manifest write refused: document not registered: never-registered-zz\"");
    }

    @Test
    void manifestWriteMany_negativePosition_failedCarriesCheckReason() throws Exception {
        // Critique: the chash CHECK is dead-via-HTTP post-z4skl, but the
        // position CHECK (position >= 0, catalog-002) is NOT boundary-
        // validated — the one still-HTTP-reachable 23514. Prove the
        // failureDetail classification handles it end-to-end.
        repo.upsertDocument(TENANT, java.util.Map.of(
            "tumbler", "5.4", "title", "pos check doc", "content_type", "paper",
            "corpus", "knowledge", "physical_collection", "knowledge__fk__v1"));
        CapturingExchange ex = post("/v1/catalog/manifest/write_many",
            "{\"collection\":\"knowledge__fk__v1\",\"docs\":[{\"doc_id\":\"5.4\",\"rows\":[{\"position\":-1,"
            + "\"chash\":\"" + "e".repeat(64) + "\"}]}]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(200);
        String body = ex.bodyString();
        assertThat(body).contains("\"failed_doc_ids\":[\"5.4\"]");
        assertThat(body)
            .contains("\"reason\":\"check constraint violation")
            .contains("position")
            .contains("\"sqlstate\":\"23514\"");
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    /**
     * RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires a
     * matching nexus.chunks row for every catalog_document_chunks insert. Stub a
     * minimal chunk (single embedding_384 vector, arbitrary text).
     */
    private void stubChunk(String collection, String chashHex) throws Exception {
        // SVC_ROLE's grants (startAll) are scoped to catalog_owners/documents/
        // document_chunks/collections only -- superuser connection bypasses RLS
        // and the missing GRANT alike for this fixture-only insert.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // RDR-191 Phase 5 (nexus-o8dil.49): nexus.chunks now carries
            // chunks_collection_fk (tenant_id, collection) -> catalog_collections
            // (tenant_id, name) — stub-register the collection first, mirroring
            // PgVectorRepository#upsertChunks' own ensure-registered step.
            try (var regPs = su.prepareStatement(
                    "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES (?, ?) "
                    + "ON CONFLICT (tenant_id, name) DO NOTHING")) {
                regPs.setString(1, TENANT);
                regPs.setString(2, collection);
                regPs.execute();
            }
            try (var ps = su.prepareStatement(
                    "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_384) "
                    + "VALUES (?, ?, decode(?, 'hex'), 'stub', ?::vector) "
                    + "ON CONFLICT (tenant_id, collection, chash) DO NOTHING")) {
                ps.setString(1, TENANT);
                ps.setString(2, collection);
                ps.setString(3, chashHex);
                ps.setString(4, "[" + "0.1,".repeat(383) + "0.1]");
                ps.execute();
            }
        }
    }

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
