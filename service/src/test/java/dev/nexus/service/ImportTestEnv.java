// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantExportSessions;
import dev.nexus.service.db.TokenStore;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.Function;

/**
 * The environment of the tenant import tests (RDR-248 Step 5, bead nexus-csayy.6): a real PostgreSQL migrated with the
 * product schema, a real {@link NexusService} behind its auth filter that declares the singleton lock held, tokens
 * for tenants, an HTTP client, and a driver for a whole export and a whole import over the routes. A test never
 * calls an import class directly except where it is testing that class; everything else goes through HTTP.
 */
final class ImportTestEnv implements AutoCloseable {

    static final ObjectMapper MAPPER = new ObjectMapper();
    static final String ROOT = "tok-root-import-0123456789abcdefghijkl";
    static final String IMPORT = "/v1/tenant/import";

    final PostgreSQLContainer<?> pg;
    final HikariDataSource ds;
    final NexusService service;
    final HttpClient http = TestHttp.client();
    private int minted;

    private ImportTestEnv(PostgreSQLContainer<?> pg, HikariDataSource ds, NexusService service) {
        this.pg = pg;
        this.ds = ds;
        this.service = service;
    }

    static ImportTestEnv start() throws Exception {
        return start(null);
    }

    /** As above, with the engine's own embedder router (null: none), which is where the open reads the models it serves. */
    static ImportTestEnv start(dev.nexus.service.vectors.EmbedderRouter router) throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(dsl, ROOT, TenantConstants.DEFAULT_TENANT, TokenStore.ROOT_TOKEN_LABEL,
                TokenStore.SCOPE_ROOT, null, null);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(14);
        cfg.setAutoCommit(true);
        HikariDataSource ds = new HikariDataSource(cfg);
        NexusService service = router == null ? new NexusService(0, ROOT, ds) : new NexusService(0, ROOT, ds, router);
        service.importWiring().declareSingletonLockHeld(true);
        service.start();
        return new ImportTestEnv(pg, ds, service);
    }

    @Override
    public void close() {
        if (service != null) service.stop();
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    // ── database ────────────────────────────────────────────────────────────────────────────────

    /** Runs {@code work} as the superuser, autocommit, on a connection of its own. */
    <T> T su(Function<DSLContext, T> work) {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    /** The rows {@code tenant} holds in {@code nexus.<table>} (the superuser bypasses RLS, so the tenant is named). */
    long count(String table, String tenant) {
        return su(ctx -> ctx.fetchCount(DSL.table(DSL.name("nexus", table)),
            DSL.field(DSL.name("tenant_id"), String.class).eq(tenant)));
    }

    // ── tenants and tokens ──────────────────────────────────────────────────────────────────────

    /** A tenant with a token and its partition leaves; its first request (the ghost sweep) is made here, so a test starts clean. */
    String newTenant(String tenant) throws Exception {
        String token = "tok-" + tenant + "-" + (minted++) + "-0123456789abcdefghijklmnopqrstuv";
        su(ctx -> {
            PgContainerHelper.seedServiceToken(ctx, token, tenant, "label-" + tenant);
            PgContainerHelper.ensureTenantPartitions(ctx, tenant);
            return null;
        });
        assertThat(call("GET", "/v1/_whoami", token).statusCode()).isEqualTo(200);
        return token;
    }

    /** A further token of an existing tenant. */
    String anotherToken(String tenant) {
        String token = "tok-" + tenant + "-" + (minted++) + "-0123456789abcdefghijklmnopqrstuv";
        su(ctx -> {
            PgContainerHelper.seedServiceToken(ctx, token, tenant, "label2-" + tenant);
            return null;
        });
        return token;
    }

    // ── HTTP ────────────────────────────────────────────────────────────────────────────────────

    String url(String path) {
        return "http://127.0.0.1:" + service.getPort() + path;
    }

    HttpResponse<String> call(String method, String path, String token) throws Exception {
        var b = TestHttp.request(url(path)).method(method, HttpRequest.BodyPublishers.noBody());
        if (token != null) b.header("Authorization", "Bearer " + token);
        return http.send(b.build(), HttpResponse.BodyHandlers.ofString());
    }

    HttpResponse<String> callJson(String method, String path, String token, String body) throws Exception {
        var b = TestHttp.request(url(path)).method(method, HttpRequest.BodyPublishers.ofString(body))
            .header("Content-Type", "application/json");
        if (token != null) b.header("Authorization", "Bearer " + token);
        return http.send(b.build(), HttpResponse.BodyHandlers.ofString());
    }

    HttpResponse<String> putPart(String token, String id, String table, int part, byte[] body, String sha) throws Exception {
        var b = TestHttp.request(url(IMPORT + "/" + id + "/rows?table=" + enc(table) + "&part=" + part))
            .PUT(HttpRequest.BodyPublishers.ofByteArray(body)).header("Authorization", "Bearer " + token);
        if (sha != null) b.header("X-Nexus-Part-Sha256", sha);
        return http.send(b.build(), HttpResponse.BodyHandlers.ofString());
    }

    static String enc(String s) {
        return java.net.URLEncoder.encode(s, StandardCharsets.UTF_8);
    }

    static JsonNode json(HttpResponse<String> r) throws Exception {
        return MAPPER.readTree(r.body());
    }

    static String sha256(byte[] body) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(body));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    // ── a backup file ───────────────────────────────────────────────────────────────────────────

    /** An export, as the client holds it: the header and each table's pages. {@link #seal} fixes its hash and byte count. */
    static final class BackupFile {
        final ObjectNode header;
        final Map<String, List<byte[]>> pages = new LinkedHashMap<>();

        BackupFile(ObjectNode header) {
            this.header = header;
        }

        BackupFile copy() {
            BackupFile c = new BackupFile(header.deepCopy());
            pages.forEach((k, v) -> c.pages.put(k, new ArrayList<>(v)));
            return c;
        }

        ObjectNode table(String name) {
            for (JsonNode t : header.withArray("tables")) {
                if (name.equals(t.path("name").asText())) return (ObjectNode) t;
            }
            throw new IllegalArgumentException("no table " + name + " in the header");
        }

        List<String> tableNames() {
            List<String> names = new ArrayList<>();
            header.withArray("tables").forEach(t -> names.add(t.path("name").asText()));
            return names;
        }

        long rows(String table) {
            return pages.getOrDefault(table, List.of()).stream().mapToLong(BinaryCopyTestSupport::rowCount).sum();
        }

        int columnIndex(String table, String column) {
            ArrayNode cols = table(table).withArray("columns");
            for (int i = 0; i < cols.size(); i++) if (column.equals(cols.get(i).path("name").asText())) return i;
            throw new IllegalArgumentException("no column " + column + " in " + table);
        }

        /** The file as an older engine wrote it: {@code column} is gone from the header and from every row. */
        BackupFile withoutColumn(String table, String column) {
            BackupFile c = copy();
            int i = columnIndex(table, column);
            c.table(table).withArray("columns").remove(i);
            c.pages.computeIfPresent(table, (k, v) -> v.stream().map(p -> BinaryCopyTestSupport.dropField(p, i)).toList());
            return c;
        }

        /** The file as a newer engine wrote it: a last column {@code name} of {@code type}, {@code value} in every row. */
        BackupFile withColumn(String table, String name, String type, byte[] value) {
            BackupFile c = copy();
            c.table(table).withArray("columns").addObject().put("name", name).put("type", type);
            c.pages.computeIfPresent(table, (k, v) -> v.stream().map(p -> BinaryCopyTestSupport.appendField(p, value)).toList());
            return c;
        }

        /**
         * The header's hash and the file's row bytes, set as a client sets them before it opens. The hash is the SHA-256 of
         * the header frame's payload exactly as {@link TenantBackupFiles} writes it into a file ({@code nx backup restore}
         * hashes the same bytes), so a marker that names it names the file.
         */
        BackupFile seal() {
            try {
                header.put("header_sha256", sha256(TenantBackupFiles.headerPayload(header)));
                header.put("row_bytes", pages.values().stream().flatMap(List::stream).mapToLong(p -> p.length).sum());
                return this;
            } catch (java.io.IOException e) {
                throw new IllegalStateException(e);
            }
        }

        String headerSha() {
            return header.path("header_sha256").asText();
        }

        String openBody() throws Exception {
            return MAPPER.writeValueAsString(header);
        }
    }

    /** Exports {@code token}'s tenant over the export routes into a file (every table, every page). */
    BackupFile export(String token) throws Exception {
        HttpResponse<String> open = call("POST", "/v1/tenant/export", token);
        assertThat(open.statusCode()).as(open.body()).isEqualTo(200);
        ObjectNode header = (ObjectNode) json(open);
        String id = header.get("session_id").asText();
        for (String drop : List.of("session_id", "created_at", "expires_at", "idle_timeout_seconds", "total_timeout_seconds")) {
            header.remove(drop);
        }
        BackupFile file = new BackupFile(header);
        for (JsonNode t : header.get("tables")) {
            String table = t.get("name").asText();
            List<byte[]> pages = new ArrayList<>();
            String after = null;
            while (true) {
                String q = "?table=" + enc(table) + (after == null ? "" : "&after=" + enc(after));
                var req = TestHttp.request(url("/v1/tenant/export/" + id + "/rows" + q))
                    .header("Authorization", "Bearer " + token).GET().build();
                HttpResponse<byte[]> page = http.send(req, HttpResponse.BodyHandlers.ofByteArray());
                assertThat(page.statusCode()).as(table).isEqualTo(200);
                pages.add(page.body());
                after = page.headers().firstValue("X-Nexus-Export-Last-Key").orElse(null);
                if (Boolean.parseBoolean(page.headers().firstValue("X-Nexus-Export-Done").orElse("true"))) break;
            }
            file.pages.put(table, pages);
        }
        assertThat(call("DELETE", "/v1/tenant/export/" + id, token).statusCode()).isEqualTo(200);
        return file.seal();
    }

    // ── an import ───────────────────────────────────────────────────────────────────────────────

    HttpResponse<String> open(String token, BackupFile file) throws Exception {
        return callJson("POST", IMPORT, token, file.openBody());
    }

    /** Uploads and closes every table the open answer lists, in its order; {@code rowsPerPart} cuts each page into parts. */
    void upload(String token, String id, BackupFile file, JsonNode opened, int rowsPerPart) throws Exception {
        for (JsonNode t : opened.get("tables")) {
            uploadTable(token, id, file, t.asText(), rowsPerPart);
        }
    }

    void uploadTable(String token, String id, BackupFile file, String table, int rowsPerPart) throws Exception {
        int part = 0;
        for (byte[] page : file.pages.getOrDefault(table, List.of())) {
            if (BinaryCopyTestSupport.rowCount(page) == 0) continue;
            for (byte[] p : BinaryCopyTestSupport.split(page, rowsPerPart)) {
                HttpResponse<String> r = putPart(token, id, table, part++, p, sha256(p));
                assertThat(r.statusCode()).as("part " + table + "/" + (part - 1) + " " + r.body()).isEqualTo(200);
            }
        }
        HttpResponse<String> c = call("POST", IMPORT + "/" + id + "/close?table=" + enc(table) + "&rows=" + file.rows(table), token);
        assertThat(c.statusCode()).as("close " + table + " " + c.body()).isEqualTo(200);
    }

    /** Polls the status until it is committed or failed (at most {@code timeout}). */
    JsonNode awaitEnd(String token, String id, Duration timeout) throws Exception {
        long deadline = System.nanoTime() + timeout.toNanos();
        while (true) {
            HttpResponse<String> r = call("GET", IMPORT + "/" + id, token);
            assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
            JsonNode s = json(r);
            if (Set.of("committed", "failed").contains(s.get("state").asText())) return s;
            assertThat(System.nanoTime()).as("the import ended in time; last status " + s).isLessThan(deadline);
            Thread.sleep(50);
        }
    }

    /** The whole restore: open, upload, commit, wait; returns the final status. */
    JsonNode restore(String token, BackupFile file) throws Exception {
        return restore(token, file, 3);
    }

    JsonNode restore(String token, BackupFile file, int rowsPerPart) throws Exception {
        HttpResponse<String> open = open(token, file);
        assertThat(open.statusCode()).as(open.body()).isEqualTo(200);
        JsonNode opened = json(open);
        String id = opened.get("session_id").asText();
        upload(token, id, file, opened, rowsPerPart);
        HttpResponse<String> commit = call("POST", IMPORT + "/" + id + "/commit", token);
        assertThat(commit.statusCode()).as(commit.body()).isEqualTo(202);
        return awaitEnd(token, id, Duration.ofMinutes(2));
    }

    /** An export session cap and bounds a test sets; the production defaults otherwise. */
    void exportSessions(TenantExportSessions.Config config, java.time.Clock clock) {
        service.exportSessions(new TenantExportSessions(ds, config, clock));
    }

    /** The import sessions of this test: its own limits, the singleton lock declared, and an optional retired list. */
    dev.nexus.service.db.TenantImportSessions importSessions(dev.nexus.service.db.TenantImportSessions.Config config,
                                                             List<dev.nexus.service.db.TenantExport.Retired> retired) {
        return importSessions(config, retired, () -> null);
    }

    /** As above, with the engine's schema changeset count a test names (the open compares the file's with it). */
    dev.nexus.service.db.TenantImportSessions importSessions(dev.nexus.service.db.TenantImportSessions.Config config,
                                                             List<dev.nexus.service.db.TenantExport.Retired> retired,
                                                             java.util.function.Supplier<Long> engineChangesets) {
        var sessions = new dev.nexus.service.db.TenantImportSessions(service::exportSessions, ds, config,
            dev.nexus.service.db.CollectionRegistry::evictTenant, service.importWiring()::singletonLockHeld, engineChangesets, retired);
        service.importWiring().sessions(sessions);
        return sessions;
    }

    /** A fresh start: production defaults for both kinds of session, so no test depends on another's bounds or clock. */
    void freshSessions() {
        exportSessions(TenantExportSessions.Config.defaults().withMaxSessions(6), java.time.Clock.systemUTC());
        importSessions(dev.nexus.service.db.TenantImportSessions.Config.defaults(), List.of());
    }

    /** Waits until {@code holder}'s statement has another backend queued behind its lock (a load stuck on it). */
    void awaitBlockedBackends(Connection holder, int n) throws Exception {
        long deadline = System.nanoTime() + Duration.ofSeconds(60).toNanos();
        while (PgContainerHelper.backendsBlockedOnLocks(DSL.using(holder, SQLDialect.POSTGRES)) < n) {
            assertThat(System.nanoTime()).as("a backend queued behind the lock").isLessThan(deadline);
            Thread.sleep(50);
        }
    }
}
