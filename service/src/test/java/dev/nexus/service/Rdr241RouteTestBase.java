// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CollectionIds;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantConstants;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-241 Phase 2 (nexus-uwggq.2.3 to .5): the shared fixture of the three engine read-route tests: real PostgreSQL,
 * the real {@link NexusService}, two tenants with their own tokens, and direct row inserts as the superuser (a route
 * test needs rows no register call can make: a given author, a metadata document, a tombstone, an alias).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
abstract class Rdr241RouteTestBase {

    static final String TENANT   = TenantConstants.DEFAULT_TENANT;
    static final String TENANT_B = "tenant-b-rdr241";
    static final String TOKEN    = "rdr241-route-token-a";
    static final String TOKEN_B  = "rdr241-route-token-b";
    static final String COLL     = "knowledge__rdr241__v1";
    static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};

    PostgreSQLContainer<?> pg;
    NexusService service;
    HttpClient http;
    HikariDataSource svcDs;
    final ObjectMapper mapper = new ObjectMapper();

    /** The service role and its password; the role is unique per test class (TestRoleUniquenessScanTest). */
    abstract String svcRole();

    abstract String svcPass();

    /** Creates the role with {@code PgContainerHelper.bootstrapServiceRole(su, <literal constant>, <constant>)}: the census reads the literals. */
    abstract void bootstrapRole(Connection su) throws Exception;

    /** The Postgres the class runs on: a clone on the per-fork shared cluster, which a subclass may replace with its own boot. */
    PostgreSQLContainer<?> startPg() throws Exception {
        return PgContainerHelper.start();
    }

    @BeforeAll
    void startAll() throws Exception {
        pg = startPg();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            bootstrapRole(su);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(dsl, TOKEN, TENANT, "test-bound");
            PgContainerHelper.seedServiceToken(dsl, TOKEN_B, TENANT_B, "test-bound-b");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(svcRole());
        cfg.setPassword(svcPass());
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        service = new NexusService(0, TOKEN, svcDs);
        service.start();
        http = TestHttp.client();
        // Burn the per-tenant ghost sweep (nexus-ft04v.3) on a request that registers nothing.
        get(TOKEN, TENANT, "/v1/catalog/collections/list");
        get(TOKEN_B, TENANT_B, "/v1/catalog/collections/list");
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void wipe() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).deleteFrom(CATALOG_DOCUMENTS).execute();
        }
    }

    /** One catalog_documents row to insert; every field has a plain default. */
    record Row(String tenant, String tumbler, String author, String contentType, String sourceUri, String filePath,
               String aliasOf, boolean sensitive, boolean deleted, String metadataJson) {
        static Row of(String tumbler) {
            return new Row(TENANT, tumbler, null, "prose", "file:///rdr241/" + tumbler + ".md", "/rdr241/" + tumbler + ".md",
                "", false, false, "{}");
        }
        Row tenant(String v) { return new Row(v, tumbler, author, contentType, sourceUri, filePath, aliasOf, sensitive, deleted, metadataJson); }
        Row author(String v) { return new Row(tenant, tumbler, v, contentType, sourceUri, filePath, aliasOf, sensitive, deleted, metadataJson); }
        Row type(String v) { return new Row(tenant, tumbler, author, v, sourceUri, filePath, aliasOf, sensitive, deleted, metadataJson); }
        Row uri(String v) { return new Row(tenant, tumbler, author, contentType, v, filePath, aliasOf, sensitive, deleted, metadataJson); }
        Row path(String v) { return new Row(tenant, tumbler, author, contentType, sourceUri, v, aliasOf, sensitive, deleted, metadataJson); }
        Row alias(String v) { return new Row(tenant, tumbler, author, contentType, sourceUri, filePath, v, sensitive, deleted, metadataJson); }
        Row flagged() { return new Row(tenant, tumbler, author, contentType, sourceUri, filePath, aliasOf, true, deleted, metadataJson); }
        Row tombstoned() { return new Row(tenant, tumbler, author, contentType, sourceUri, filePath, aliasOf, sensitive, true, metadataJson); }
        Row meta(String json) { return new Row(tenant, tumbler, author, contentType, sourceUri, filePath, aliasOf, sensitive, deleted, json); }
    }

    /** Inserts the rows as the superuser (RLS does not apply to it). Tumblers must be unique within a tenant. */
    void insert(Row... rows) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (Row r : rows) {
                dsl.insertInto(CATALOG_DOCUMENTS)
                    .set(CATALOG_DOCUMENTS.TENANT_ID, r.tenant())
                    .set(CATALOG_DOCUMENTS.TUMBLER, r.tumbler())
                    .set(CATALOG_DOCUMENTS.TITLE, "doc " + r.tumbler())
                    .set(CATALOG_DOCUMENTS.AUTHOR, r.author())
                    .set(CATALOG_DOCUMENTS.CONTENT_TYPE, r.contentType())
                    .set(CATALOG_DOCUMENTS.FILE_PATH, r.filePath())
                    .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, COLL)
                    .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, CollectionIds.idOf(r.tenant(), COLL))
                    .set(CATALOG_DOCUMENTS.SOURCE_URI, r.sourceUri())
                    .set(CATALOG_DOCUMENTS.METADATA, JSONB.valueOf(r.metadataJson()))
                    .set(CATALOG_DOCUMENTS.ALIAS_OF, r.aliasOf())
                    .set(CATALOG_DOCUMENTS.SENSITIVE, r.sensitive())
                    .set(CATALOG_DOCUMENTS.DELETED_AT, r.deleted() ? OffsetDateTime.parse("2026-11-01T00:00:00+00:00") : null)
                    .execute();
            }
        }
    }

    /** {@code pg_database.datctype} of the database the class runs on: {@code C} for the local bundle, a locale name on the test cluster. */
    String databaseCtype() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var db = DSL.table(DSL.name("pg_catalog", "pg_database"));
            return DSL.using(su, SQLDialect.POSTGRES).select(DSL.field(DSL.name("datctype"), String.class)).from(db)
                .where(DSL.field(DSL.name("datname"), String.class).eq(DSL.function("current_database", String.class)))
                .fetchOne(0, String.class);
        }
    }

    Map<String, Object> ok(String token, String tenant, String path) throws Exception {
        var resp = get(token, tenant, path);
        assertThat(resp.statusCode()).as(path + " -> " + resp.body()).isEqualTo(200);
        return mapper.readValue(resp.body(), MAP_T);
    }

    Map<String, Object> ok(String path) throws Exception {
        return ok(TOKEN, TENANT, path);
    }

    int status(String path) throws Exception {
        return get(TOKEN, TENANT, path).statusCode();
    }

    @SuppressWarnings("unchecked")
    static List<Map<String, Object>> documents(Map<String, Object> body) {
        return (List<Map<String, Object>>) body.get("documents");
    }

    static List<Object> tumblers(Map<String, Object> body) {
        return documents(body).stream().map(d -> d.get("tumbler")).toList();
    }

    HttpResponse<String> post(String token, String tenant, String path, String body) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + token)
            .header("X-Nexus-Tenant", tenant)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    HttpResponse<String> get(String token, String tenant, String path) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + token)
            .header("X-Nexus-Tenant", tenant)
            .GET()
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
