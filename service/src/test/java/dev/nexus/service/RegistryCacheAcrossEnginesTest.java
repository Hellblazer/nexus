// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.TenantConstants;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 Phase 3 blocking row 15 (nexus-p2m1x.22): an engine answers from its own database, never from a registry row another
 * engine cached in the same JVM. CollectionRegistry's caches are process-wide; surefire reuses a fork JVM across test classes, and
 * each class starts its own database, so two databases can give the same {@code collection_id} to different labels of one tenant.
 * Without an eviction at engine start, the second engine's {@code GET /v1/catalog/collections/get?collection_id=} resolved that id
 * through the first engine's cached entry to the first database's label, and the handler answered 404 for a live row
 * (CatalogCollectionIdReadTest.getTakesAnIdAndListAndForTupleIgnoreOne, once in the needs-operator pre-gate run).
 *
 * <p>This test makes the collision on purpose: database A registers {@code a-label} and a by-id get caches it; database B registers
 * {@code b-label} and its id is set to A's; B's engine is asked by that id.
 */
class RegistryCacheAcrossEnginesTest {

    private static final String TOKEN = "registry-cache-across-engines-token";
    private static final String SVC_ROLE = "svc_registry_across";
    private static final String SVC_PASS = "svc_registry_across_pass";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;
    private static final String MODEL = "voyage-context-3";
    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};
    private static final ObjectMapper MAPPER = new ObjectMapper();

    @FunctionalInterface
    private interface ThrowingRunnable {
        void run() throws Exception;
    }

    /** One engine on its own database. */
    private static final class Engine implements AutoCloseable {
        final PostgreSQLContainer<?> pg;
        final com.zaxxer.hikari.HikariDataSource ds;
        final NexusService service;
        final HttpClient http = TestHttp.client();

        Engine() throws Exception {
            this(() -> { });
        }

        /** {@code beforeStart} runs after the database exists and before the engine starts: the test's way to put another database's row in the caches. */
        Engine(ThrowingRunnable beforeStart) throws Exception {
            pg = PgContainerHelper.start();
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.applyProductSchema(su);
            }
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
                PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "test-bound");
            }
            var cfg = new com.zaxxer.hikari.HikariConfig();
            cfg.setJdbcUrl(pg.getJdbcUrl());
            cfg.setUsername(SVC_ROLE);
            cfg.setPassword(SVC_PASS);
            cfg.setMaximumPoolSize(4);
            cfg.setAutoCommit(true);
            ds = new com.zaxxer.hikari.HikariDataSource(cfg);
            service = new NexusService(0, TOKEN, ds);
            beforeStart.run();
            service.start();
            get("/v1/catalog/collections/list");   // the tenant's once-per-process ghost sweep runs before anything is registered
        }

        HttpResponse<String> get(String path) throws Exception {
            return http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
                .header("Authorization", "Bearer " + TOKEN).GET().build(), HttpResponse.BodyHandlers.ofString());
        }

        void register(String name, String owner) throws Exception {
            String body = "{\"name\":\"" + name + "\",\"content_type\":\"knowledge\",\"owner_id\":\"" + owner + "\","
                + "\"embedding_model\":\"" + MODEL + "\",\"model_version\":\"v1\",\"display_name\":\"\",\"legacy_grandfathered\":true}";
            var up = http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/catalog/collections/upsert")
                .header("Authorization", "Bearer " + TOKEN).header("Content-Type", "application/json")
                .POST(java.net.http.HttpRequest.BodyPublishers.ofString(body)).build(), HttpResponse.BodyHandlers.ofString());
            assertThat(up.statusCode()).as("guard: %s registers: %s", name, up.body()).isEqualTo(200);
        }

        long id(String name) throws Exception {
            try (Connection su = pg.createConnection("")) {
                return DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name))
                    .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
            }
        }

        void setId(String name, long id) throws Exception {
            try (Connection su = pg.createConnection("")) {
                int n = DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.COLLECTION_ID, id)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name)).execute();
                assertThat(n).as("guard: one row takes the id").isEqualTo(1);
            }
        }

        @Override
        public void close() {
            service.stop();
            ds.close();
            pg.stop();
        }
    }

    @Test
    void aSecondEngineInTheSameJvmAnswersAnIdFromItsOwnDatabase_notFromTheFirstEnginesCachedRow() throws Exception {
        long sharedId;
        try (Engine a = new Engine()) {
            a.register("a-label", "owner-a");
            sharedId = a.id("a-label");
            var first = a.get("/v1/catalog/collections/get?collection_id=" + sharedId);
            assertThat(first.statusCode()).as("A answers its own row by id: %s", first.body()).isEqualTo(200);
            assertThat(MAPPER.readValue(first.body(), MAP_T).get("name")).isEqualTo("a-label");
            // A is still running when B's database is created and B starts, so A's row is in the caches when B's start() runs
            // (creating B's database empties them, so A caches again just before B starts)
            try (Engine b = new Engine(() -> a.get("/v1/catalog/collections/get?collection_id=" + sharedId))) {
                b.register("b-label", "owner-b");
                if (b.id("b-label") != sharedId) {
                    b.setId("b-label", sharedId);
                }
                assertThat(b.id("b-label")).as("non-vacuity: the two databases give one id to two labels").isEqualTo(sharedId);

                var answer = b.get("/v1/catalog/collections/get?collection_id=" + sharedId);

                assertThat(answer.statusCode()).as("B answers the id from its own database: %s", answer.body()).isEqualTo(200);
                assertThat(MAPPER.readValue(answer.body(), MAP_T).get("name")).as("B's row, not A's cached label").isEqualTo("b-label");
            }
        }
    }

    /** Stopping an engine empties the caches, so a repository built on another database afterwards is answered from its own (critic S6). */
    @Test
    void stoppingAnEngineEmptiesTheRegistryCaches() throws Exception {
        try (Engine a = new Engine()) {
            a.register("stop-label", "owner-stop");
            long id = a.id("stop-label");
            assertThat(a.get("/v1/catalog/collections/get?collection_id=" + id).statusCode()).isEqualTo(200);
            assertThat(dev.nexus.service.db.CollectionRegistry.cached(TENANT, "stop-label"))
                .as("non-vacuity: the by-id read cached the row").isPresent();
            a.service.stop();
            assertThat(dev.nexus.service.db.CollectionRegistry.cached(TENANT, "stop-label"))
                .as("the stopped engine's row is gone from the process-wide cache").isEmpty();
        }
    }

    /** A test database created after an engine ran starts with empty caches, with no engine started on it (a repository-only class). */
    @Test
    void aTestDatabaseCreatedAfterAnEngineRanStartsWithEmptyRegistryCaches() throws Exception {
        try (Engine a = new Engine()) {
            a.register("fresh-label", "owner-fresh");
            long id = a.id("fresh-label");
            assertThat(a.get("/v1/catalog/collections/get?collection_id=" + id).statusCode()).isEqualTo(200);
            assertThat(dev.nexus.service.db.CollectionRegistry.cached(TENANT, "fresh-label"))
                .as("non-vacuity: the by-id read cached the row").isPresent();
            PostgreSQLContainer<?> other = PgContainerHelper.start();
            try {
                assertThat(dev.nexus.service.db.CollectionRegistry.cached(TENANT, "fresh-label"))
                    .as("the caches were emptied when the second database was created").isEmpty();
            } finally {
                other.stop();
            }
        }
    }
}
