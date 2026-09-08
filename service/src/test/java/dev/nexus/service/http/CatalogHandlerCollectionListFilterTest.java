// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
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
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-204 Phase 2, bead nexus-ft04v.24 — {@code GET /v1/catalog/collections/list}
 * gains optional {@code content_type} and {@code lifecycle_state} query filters.
 * No filter parameter reproduces the pre-P2.4 unfiltered result exactly (a
 * pre-Phase-2 client calling the unfiltered route is unaffected). Model: {@link
 * CatalogHandlerListPaginationTest} (hermetic Testcontainers PG, drives {@link
 * CatalogHandler#handle} directly via a capturing {@link HttpExchange}).
 *
 * <p>Fixture: five {@code catalog_collections} rows seeded directly via jOOQ DSL
 * (not {@link PgContainerHelper#insertCollection}, which only derives {@code live}
 * or {@code quarantine} from the name — this suite needs explicit control over all
 * four {@code lifecycle_state} values, {@code disputed} and {@code dormant}
 * included):
 * <ul>
 *   <li>{@code CLF_LIVE_CODE}    — content_type=code, lifecycle_state=live</li>
 *   <li>{@code CLF_LIVE_DOCS}    — content_type=docs, lifecycle_state=live</li>
 *   <li>{@code CLF_DISPUTED}     — content_type=code, lifecycle_state=disputed</li>
 *   <li>{@code CLF_DORMANT}      — content_type=code, lifecycle_state=dormant</li>
 *   <li>{@code CLF_QUARANTINE}   — content_type=code, lifecycle_state=quarantine</li>
 * </ul>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerCollectionListFilterTest {

    private static final String SVC_ROLE = "svc_cat_clf_test";
    private static final String SVC_PASS = "svc_cat_clf_test_pass";
    private static final String TENANT   = "cat-coll-filter-tenant";
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final String CLF_LIVE_CODE  = "code__clf-owner-live__voyage-code-3__v1";
    private static final String CLF_LIVE_DOCS  = "docs__clf-owner-live__voyage-context-3__v1";
    private static final String CLF_DISPUTED   = "code__clf-owner-disputed__voyage-code-3__v1";
    private static final String CLF_DORMANT    = "code__clf-owner-dormant__voyage-code-3__v1";
    private static final String CLF_QUARANTINE = "code__clf-owner-quarantine__voyage-code-3__v1";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    CatalogRepository repo;
    CatalogHandler handler;
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
        repo = new CatalogRepository(tenantScope);
        handler = new CatalogHandler(repo);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            seed(dsl, CLF_LIVE_CODE,  "code", "live");
            seed(dsl, CLF_LIVE_DOCS,  "docs", "live");
            seed(dsl, CLF_DISPUTED,   "code", "disputed");
            seed(dsl, CLF_DORMANT,    "code", "dormant");
            seed(dsl, CLF_QUARANTINE, "code", "quarantine");
        }
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static void seed(DSLContext dsl, String name, String contentType, String lifecycleState) {
        dsl.insertInto(CATALOG_COLLECTIONS,
                CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.LIFECYCLE_STATE)
            .values(TENANT, name, contentType, "clf-owner", "voyage-code-3", lifecycleState)
            .onConflictDoNothing()
            .execute();
    }

    @Test
    void noFilter_returnsEverything() throws Exception {
        var colls = listCollections("");
        assertThat(names(colls)).containsExactlyInAnyOrder(
            CLF_LIVE_CODE, CLF_LIVE_DOCS, CLF_DISPUTED, CLF_DORMANT, CLF_QUARANTINE);
    }

    @Test
    void filterByContentType_docs_returnsOnlyDocs() throws Exception {
        var colls = listCollections("content_type=docs");
        assertThat(names(colls)).containsExactly(CLF_LIVE_DOCS);
    }

    @Test
    void filterByLifecycleState_live_excludesDisputedDormantAndQuarantine() throws Exception {
        var colls = listCollections("lifecycle_state=live");
        assertThat(names(colls)).containsExactlyInAnyOrder(CLF_LIVE_CODE, CLF_LIVE_DOCS);
        assertThat(names(colls))
            .as("lifecycle_state=live must exclude a disputed, a dormant AND a quarantine fixture")
            .doesNotContain(CLF_DISPUTED, CLF_DORMANT, CLF_QUARANTINE);
    }

    @Test
    void filterByContentTypeAndLifecycleState_bothApplied() throws Exception {
        var colls = listCollections("content_type=code&lifecycle_state=live");
        assertThat(names(colls)).containsExactly(CLF_LIVE_CODE);
    }

    @Test
    void listedRow_carriesLifecycleStateAndDimension() throws Exception {
        var colls = listCollections("content_type=docs");
        assertThat(colls).hasSize(1);
        Map<String, Object> row = colls.get(0);
        assertThat(row.get("lifecycle_state")).isEqualTo("live");
        assertThat(row).containsKey("dimension");
        // Pre-existing keys untouched.
        assertThat(row.get("name")).isEqualTo(CLF_LIVE_DOCS);
        assertThat(row.get("content_type")).isEqualTo("docs");
        assertThat(row.get("owner_id")).isEqualTo("clf-owner");
        assertThat(row.get("embedding_model")).isEqualTo("voyage-code-3");
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private Set<Object> names(List<Map<String, Object>> colls) {
        return colls.stream().map(c -> c.get("name")).collect(Collectors.toSet());
    }

    @SuppressWarnings("unchecked")
    private List<Map<String, Object>> listCollections(String query) throws Exception {
        String path = "/v1/catalog/collections/list" + (query.isBlank() ? "" : "?" + query);
        CapturingExchange ex = get(path);
        RequestContext.set(new RequestContext.Principal(TENANT, null, false, false, "tenant", "test-credential-hash"));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(200);
        Map<String, Object> body = MAPPER.readValue(ex.bodyString(), Map.class);
        return (List<Map<String, Object>>) body.get("collections");
    }

    private static CapturingExchange get(String uri) {
        return new CapturingExchange("GET", URI.create(uri));
    }

    /** Minimal {@link HttpExchange} that captures the response status + body (GET, no body). */
    private static final class CapturingExchange extends HttpExchange {
        private final String method;
        private final URI uri;
        private final Headers responseHeaders = new Headers();
        private final ByteArrayOutputStream responseBody = new ByteArrayOutputStream();
        int status = -1;

        CapturingExchange(String method, URI uri) {
            this.method = method;
            this.uri = uri;
        }

        String bodyString() { return responseBody.toString(StandardCharsets.UTF_8); }

        @Override public Headers getRequestHeaders() { return new Headers(); }
        @Override public Headers getResponseHeaders() { return responseHeaders; }
        @Override public URI getRequestURI() { return uri; }
        @Override public String getRequestMethod() { return method; }
        @Override public HttpContext getHttpContext() { return null; }
        @Override public void close() {}
        @Override public InputStream getRequestBody() { return new ByteArrayInputStream(new byte[0]); }
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
