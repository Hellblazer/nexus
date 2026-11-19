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
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-254 route wiring: {@code POST /v1/catalog/documents/move} and {@code POST /v1/catalog/owners/move} through
 * {@link CatalogHandler}: the method check, the 400s for a missing or malformed field (including a missing
 * {@code dry_run}, which has no default), the 409 of a refusal, and the report of a dry run and of a write. The
 * transactions are pinned by {@code CatalogDocumentMoveTest} and {@code CatalogOwnerMoveTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogMoveRoutesTest {

    private static final String SVC_ROLE = "svc_cat_move_routes";
    private static final String SVC_PASS = "svc_cat_move_routes_pass";
    private static final String TENANT = "cat-move-routes";
    private static final ObjectMapper MAPPER = new ObjectMapper();

    PostgreSQLContainer<?> pg;
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
        repo = new CatalogRepository(new TenantScope(svcDs));
        handler = new CatalogHandler(repo);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private String curatorDoc(String prefix, String uri, String path) {
        repo.upsertOwner(TENANT, Map.of("tumbler_prefix", prefix, "name", "curator-" + prefix, "owner_type", "curator"));
        Map<String, Object> f = new LinkedHashMap<>();
        f.put("title", "a");
        f.put("file_path", path);
        f.put("source_uri", uri);
        return repo.registerDocument(TENANT, prefix, f);
    }

    // ── documents/move ──────────────────────────────────────────────────────

    @Test
    @SuppressWarnings("unchecked")
    void documentsMove_dryRunThenWrite_answerTheReport() throws Exception {
        String s = curatorDoc("7.1", "file:///old/a.pdf", "/old/a.pdf");
        String t = repo.registerDocument(TENANT, "7.1", Map.of("title", "a2", "file_path", "/new/a.pdf",
            "source_uri", "file:///new/a.pdf"));
        String body = "{\"tumbler\":\"" + s + "\",\"new_source_uri\":\"file:///new/a.pdf\",\"new_file_path\":\"/new/a.pdf\",";

        CapturingExchange dry = send("POST", "/v1/catalog/documents/move", body + "\"dry_run\":true}");
        assertThat(dry.status).as(dry.bodyString()).isEqualTo(200);
        Map<String, Object> dryReport = MAPPER.readValue(dry.bodyString(), Map.class);
        assertThat(dryReport).containsEntry("dry_run", true).containsEntry("absorbed", 1).containsEntry("repointed", 1);
        assertThat((List<String>) dryReport.get("absorbed_tumblers")).containsExactly(t);
        assertThat(repo.getDocument(TENANT, t)).as("a dry run wrote nothing: T is still live").isNotNull();

        CapturingExchange write = send("POST", "/v1/catalog/documents/move", body + "\"dry_run\":false}");
        assertThat(write.status).as(write.bodyString()).isEqualTo(200);
        Map<String, Object> report = MAPPER.readValue(write.bodyString(), Map.class);
        assertThat(report).containsEntry("dry_run", false);
        Map<String, Object> counts = new LinkedHashMap<>(report);
        counts.remove("dry_run");
        Map<String, Object> dryCounts = new LinkedHashMap<>(dryReport);
        dryCounts.remove("dry_run");
        assertThat(counts).isEqualTo(dryCounts);
        assertThat(repo.getDocument(TENANT, t)).as("T is trashed").isNull();
        assertThat(repo.getDocument(TENANT, s).get("source_uri")).isEqualTo("file:///new/a.pdf");
    }

    @Test
    void documentsMove_aRefusalIs409WithTheReason() throws Exception {
        CapturingExchange ex = send("POST", "/v1/catalog/documents/move",
            "{\"tumbler\":\"7.2.9\",\"new_source_uri\":\"file:///n/a.pdf\",\"new_file_path\":\"/n/a.pdf\",\"dry_run\":false}");
        assertThat(ex.status).isEqualTo(409);
        assertThat(ex.bodyString()).contains("7.2.9");
    }

    @Test
    void documentsMove_badRequestsAre400() throws Exception {
        String ok = "\"tumbler\":\"1.1.1\",\"new_source_uri\":\"file:///n/a.pdf\",\"new_file_path\":\"/n/a.pdf\"";
        assertThat(send("POST", "/v1/catalog/documents/move", "{" + ok + "}").status)
            .as("dry_run missing").isEqualTo(400);
        assertThat(send("POST", "/v1/catalog/documents/move", "{" + ok + ",\"dry_run\":\"yes\"}").status)
            .as("dry_run not a boolean").isEqualTo(400);
        assertThat(send("POST", "/v1/catalog/documents/move",
            "{\"new_source_uri\":\"file:///n\",\"new_file_path\":\"/n\",\"dry_run\":true}").status)
            .as("tumbler missing").isEqualTo(400);
        assertThat(send("POST", "/v1/catalog/documents/move",
            "{\"tumbler\":\"1.1\",\"new_source_uri\":\"file:///n\",\"new_file_path\":\"/n\",\"dry_run\":true}").status)
            .as("an owner prefix is not a document tumbler").isEqualTo(400);
        assertThat(send("POST", "/v1/catalog/documents/move",
            "{\"tumbler\":\"1.1.1\",\"new_file_path\":\"/n\",\"dry_run\":true}").status)
            .as("new_source_uri missing").isEqualTo(400);
        assertThat(send("POST", "/v1/catalog/documents/move",
            "{\"tumbler\":\"1.1.1\",\"new_source_uri\":\"file:///n\",\"dry_run\":true}").status)
            .as("new_file_path missing").isEqualTo(400);
    }

    @Test
    void movesRefuseARelativePathAndANonFileUriWith400() throws Exception {
        assertThat(send("POST", "/v1/catalog/documents/move",
            "{\"tumbler\":\"1.1.1\",\"new_source_uri\":\"file:///n/a.pdf\",\"new_file_path\":\"n/a.pdf\",\"dry_run\":true}").status)
            .as("relative new_file_path").isEqualTo(400);
        assertThat(send("POST", "/v1/catalog/documents/move",
            "{\"tumbler\":\"1.1.1\",\"new_source_uri\":\"https://n/a.pdf\",\"new_file_path\":\"/n/a.pdf\",\"dry_run\":true}").status)
            .as("new_source_uri not a file:// URI").isEqualTo(400);
        assertThat(send("POST", "/v1/catalog/owners/move",
            "{\"owner\":\"1.1\",\"new_root\":\"n/proj\",\"new_repo_hash\":\"h\",\"dry_run\":true}").status)
            .as("relative new_root").isEqualTo(400);
    }

    @Test
    void documentsMove_getIsNotAllowed() throws Exception {
        assertThat(send("GET", "/v1/catalog/documents/move", "").status).isEqualTo(405);
    }

    // ── owners/move ─────────────────────────────────────────────────────────

    @Test
    @SuppressWarnings("unchecked")
    void ownersMove_dryRunThenWrite_answerTheReport() throws Exception {
        repo.upsertOwner(TENANT, Map.of("tumbler_prefix", "8.1", "name", "proj", "owner_type", "repo",
            "repo_hash", "h-old", "repo_root", "/old/proj"));
        String d = repo.registerDocument(TENANT, "8.1", Map.of("title", "x", "file_path", "src/x.py"));
        assertThat(repo.getDocument(TENANT, d).get("source_uri")).isEqualTo("file:///old/proj/src/x.py");
        String body = "{\"owner\":\"8.1\",\"new_root\":\"/new/proj\",\"new_repo_hash\":\"h-new\",";

        CapturingExchange dry = send("POST", "/v1/catalog/owners/move", body + "\"dry_run\":true}");
        assertThat(dry.status).as(dry.bodyString()).isEqualTo(200);
        Map<String, Object> dryReport = MAPPER.readValue(dry.bodyString(), Map.class);
        assertThat(dryReport).containsEntry("dry_run", true).containsEntry("repointed", 1).containsEntry("owners_folded", 0);
        assertThat(repo.getDocument(TENANT, d).get("source_uri")).as("a dry run wrote nothing").isEqualTo("file:///old/proj/src/x.py");

        CapturingExchange write = send("POST", "/v1/catalog/owners/move", body + "\"dry_run\":false}");
        assertThat(write.status).as(write.bodyString()).isEqualTo(200);
        assertThat(MAPPER.readValue(write.bodyString(), Map.class)).containsEntry("dry_run", false).containsEntry("repointed", 1);
        assertThat(repo.getDocument(TENANT, d).get("source_uri")).isEqualTo("file:///new/proj/src/x.py");
        assertThat(repo.ownerByRepoHash(TENANT, "h-new").get("tumbler_prefix")).isEqualTo("8.1");
    }

    @Test
    void ownersMove_aRefusalIs409AndBadRequestsAre400() throws Exception {
        CapturingExchange missing = send("POST", "/v1/catalog/owners/move",
            "{\"owner\":\"8.9\",\"new_root\":\"/n\",\"new_repo_hash\":\"h\",\"dry_run\":false}");
        assertThat(missing.status).isEqualTo(409);
        assertThat(missing.bodyString()).contains("8.9");
        String ok = "\"owner\":\"8.1\",\"new_root\":\"/n\",\"new_repo_hash\":\"h\"";
        assertThat(send("POST", "/v1/catalog/owners/move", "{" + ok + "}").status).as("dry_run missing").isEqualTo(400);
        assertThat(send("POST", "/v1/catalog/owners/move", "{\"new_root\":\"/n\",\"new_repo_hash\":\"h\",\"dry_run\":true}").status)
            .as("owner missing").isEqualTo(400);
        assertThat(send("POST", "/v1/catalog/owners/move", "{\"owner\":\"8.1.1\",\"new_root\":\"/n\",\"new_repo_hash\":\"h\",\"dry_run\":true}").status)
            .as("a document tumbler is not an owner prefix").isEqualTo(400);
        assertThat(send("POST", "/v1/catalog/owners/move", "{\"owner\":\"8.1\",\"new_repo_hash\":\"h\",\"dry_run\":true}").status)
            .as("new_root missing").isEqualTo(400);
        assertThat(send("POST", "/v1/catalog/owners/move", "{\"owner\":\"8.1\",\"new_root\":\"/n\",\"dry_run\":true}").status)
            .as("new_repo_hash missing").isEqualTo(400);
        assertThat(send("GET", "/v1/catalog/owners/move", "").status).isEqualTo(405);
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    private CapturingExchange send(String method, String path, String body) throws Exception {
        CapturingExchange ex = new CapturingExchange(method, URI.create(path), body);
        RequestContext.set(new RequestContext.Principal(TENANT, null, false, false, "tenant", "test-credential-hash"));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }
        return ex;
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
