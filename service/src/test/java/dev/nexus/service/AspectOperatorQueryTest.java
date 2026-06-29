package dev.nexus.service;

import dev.nexus.service.db.AspectRepository;
import dev.nexus.service.db.TenantScope;
import org.testcontainers.containers.PostgreSQLContainer;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.*;

import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.*;

/**
 * RDR-152 bead nexus-l9hd8 — AspectRepository operator-query methods.
 *
 * <p>Tests the three SQL fast-path queries that mirror the Python
 * {@code aspect_sql._query_filter}, {@code _query_groupby}, and
 * {@code _query_confidence_aggregate} functions (RDR-089):
 * <ol>
 *   <li>filterBySourceUris: LIKE match on scalar_text and json_array fields;
 *       extras.key match via Postgres JSON extract; unknown source_uri excluded</li>
 *   <li>groupByField: scalar_text groupby; extras.key groupby; unassigned bucket</li>
 *   <li>confidenceAggregate: avg/min/max across matched URIs; empty set returns null</li>
 *   <li>Cross-tenant RLS isolation: tenant B cannot see tenant A rows</li>
 *   <li>Parity semantics: exact same result for same data as the SQLite path would produce</li>
 * </ol>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class AspectOperatorQueryTest {

    private static final String TENANT_A = "op-query-tenant-a";
    private static final String TENANT_B = "op-query-tenant-b";
    private static final String SVC_ROLE = "svc_op_query_test";
    private static final String SVC_PASS = "svc_op_query_test_pass";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    AspectRepository repo;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN " +
                "    CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "'; " +
                "  END IF; " +
                "END $$");
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; " +
                "  END IF; " +
                "END $$");
        }

        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            Liquibase liquibase = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db);
            liquibase.update(new Contexts());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            for (String table : List.of("document_aspects", "document_highlights",
                                        "aspect_extraction_queue", "aspect_promotion_log")) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus." + table + " TO " + SVC_ROLE);
                su.createStatement().execute(
                    "GRANT USAGE ON SEQUENCE nexus." + table + "_id_seq TO " + SVC_ROLE);
            }
            // RDR-164 P1a: aspect writes ensure-register their collection (catalog_collections
            // stub) to satisfy the new fk-003 collection FKs.
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.catalog_collections TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");

            // Seed catalog_documents tumblers for doc_id FK constraint (nexus-b7v6i)
            for (String tumbler : List.of("op-doc-1", "op-doc-2", "op-doc-3", "op-doc-4", "op-doc-b1")) {
                for (String tenant : List.of(TENANT_A, TENANT_B)) {
                    su.createStatement().execute(
                        "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) " +
                        "VALUES ('" + tenant + "', '" + tumbler + "', 'Fixture " + tumbler + "') " +
                        "ON CONFLICT (tenant_id, tumbler) DO NOTHING");
                }
            }
        }

        com.zaxxer.hikari.HikariConfig svcCfg = new com.zaxxer.hikari.HikariConfig();
        svcCfg.setJdbcUrl(pg.getJdbcUrl());
        svcCfg.setUsername(SVC_ROLE);
        svcCfg.setPassword(SVC_PASS);
        svcCfg.setMaximumPoolSize(4);
        svcCfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(svcCfg);
        tenantScope = new TenantScope(svcDs);
        repo = new AspectRepository(tenantScope);
    }

    @AfterAll
    void stopAll() throws Exception {
        if (svcDs != null) svcDs.close();
        if (pg != null)    pg.stop();
    }

    /** Seed a minimal aspect row for operator-query tests. */
    private void seed(String tenant, String collection, String sourcePath, String sourceUri,
                      String proposedMethod, String expDatasets, String extras, double confidence,
                      String docId) {
        java.util.LinkedHashMap<String, Object> body = new java.util.LinkedHashMap<>();
        body.put("collection",             collection);
        body.put("source_path",            sourcePath);
        body.put("source_uri",             sourceUri);
        body.put("proposed_method",        proposedMethod);
        body.put("experimental_datasets",  expDatasets);
        body.put("extras",                 extras);
        body.put("confidence",             confidence);
        body.put("extracted_at",           "2026-06-01T10:00:00.000000Z");
        body.put("model_version",          "v1");
        body.put("extractor_name",         "scholarly");
        body.put("doc_id",                 docId);
        repo.upsertAspect(tenant, body);
    }

    // ── filter tests ───────────────────────────────────────────────────────────

    @Test @Order(1)
    void filter_scalarText_matchFound() {
        seed(TENANT_A, "coll-op", "filter-a.pdf", "file:///filter-a.pdf",
             "Paxos consensus algorithm", "[]", "{}", 0.9, "op-doc-1");

        List<String> matched = repo.filterBySourceUris(
            TENANT_A,
            List.of("file:///filter-a.pdf"),
            "proposed_method",
            "%paxos%");

        assertThat(matched).containsExactly("file:///filter-a.pdf");
    }

    @Test @Order(2)
    void filter_scalarText_noMatch() {
        seed(TENANT_A, "coll-op", "filter-b.pdf", "file:///filter-b.pdf",
             "Raft consensus", "[]", "{}", 0.85, "op-doc-2");

        List<String> matched = repo.filterBySourceUris(
            TENANT_A,
            List.of("file:///filter-b.pdf"),
            "proposed_method",
            "%paxos%");

        assertThat(matched).isEmpty();
    }

    @Test @Order(3)
    void filter_jsonArray_tokenMatch() {
        // experimental_datasets stored as JSON array text: ["TPC-C","YCSB"]
        seed(TENANT_A, "coll-op", "filter-c.pdf", "file:///filter-c.pdf",
             "Raft consensus", "[\"TPC-C\",\"YCSB\"]", "{}", 0.88, "op-doc-3");

        List<String> matched = repo.filterBySourceUris(
            TENANT_A,
            List.of("file:///filter-c.pdf"),
            "experimental_datasets",
            "%\"TPC-C\"%");

        assertThat(matched).containsExactly("file:///filter-c.pdf");
    }

    @Test @Order(4)
    void filter_extrasKey_match() {
        seed(TENANT_A, "coll-op", "filter-d.pdf", "file:///filter-d.pdf",
             "CRDT approach", "[]", "{\"venue\":\"VLDB\"}", 0.82, "op-doc-4");

        // extras.venue LIKE '%VLDB%'
        List<String> matched = repo.filterBySourceUris(
            TENANT_A,
            List.of("file:///filter-d.pdf"),
            "extras.venue",
            "%VLDB%");

        assertThat(matched).containsExactly("file:///filter-d.pdf");
    }

    @Test @Order(5)
    void filter_unknownSourceUri_excluded() {
        List<String> matched = repo.filterBySourceUris(
            TENANT_A,
            List.of("file:///does-not-exist.pdf"),
            "proposed_method",
            "%paxos%");

        assertThat(matched).isEmpty();
    }

    @Test @Order(6)
    void filter_crossTenant_rls_isolation() {
        // filter-a.pdf was seeded under TENANT_A; must not appear for TENANT_B
        List<String> matched = repo.filterBySourceUris(
            TENANT_B,
            List.of("file:///filter-a.pdf"),
            "proposed_method",
            "%paxos%");

        assertThat(matched).as("TENANT_B must not see TENANT_A rows").isEmpty();
    }

    // ── groupby tests ──────────────────────────────────────────────────────────

    @Test @Order(10)
    void groupby_scalarText_groupsCorrectly() {
        // filter-a.pdf has proposed_method = "Paxos consensus algorithm"
        // filter-b.pdf has proposed_method = "Raft consensus"
        Map<String, String> groups = repo.groupByField(
            TENANT_A,
            List.of("file:///filter-a.pdf", "file:///filter-b.pdf"),
            "proposed_method");

        assertThat(groups).containsEntry("file:///filter-a.pdf", "Paxos consensus algorithm");
        assertThat(groups).containsEntry("file:///filter-b.pdf", "Raft consensus");
    }

    @Test @Order(11)
    void groupby_extrasKey_groupsCorrectly() {
        // filter-d.pdf has extras.venue = "VLDB"
        Map<String, String> groups = repo.groupByField(
            TENANT_A,
            List.of("file:///filter-d.pdf"),
            "extras.venue");

        assertThat(groups).containsEntry("file:///filter-d.pdf", "VLDB");
    }

    @Test @Order(12)
    void groupby_missingUri_returnsNullValue() {
        // An unknown URI should return null (maps to "unassigned" in Python caller)
        Map<String, String> groups = repo.groupByField(
            TENANT_A,
            List.of("file:///filter-a.pdf", "file:///does-not-exist.pdf"),
            "proposed_method");

        assertThat(groups).containsKey("file:///filter-a.pdf");
        // Missing URI: either absent from map or mapped to null
        assertThat(groups.containsKey("file:///does-not-exist.pdf")).isFalse();
    }

    @Test @Order(13)
    void groupby_crossTenant_rls_isolation() {
        // TENANT_B must not see TENANT_A rows; unknown URIs omitted
        Map<String, String> groups = repo.groupByField(
            TENANT_B,
            List.of("file:///filter-a.pdf"),
            "proposed_method");

        assertThat(groups).as("TENANT_B groupby must not see TENANT_A rows").isEmpty();
    }

    // ── confidence_aggregate tests ─────────────────────────────────────────────

    @Test @Order(20)
    void confidenceAggregate_avg() {
        // filter-a.pdf: confidence=0.9, filter-b.pdf: confidence=0.85
        Double avg = repo.confidenceAggregate(
            TENANT_A,
            List.of("file:///filter-a.pdf", "file:///filter-b.pdf"),
            "avg_confidence");

        assertThat(avg).as("avg confidence must be approx 0.875").isNotNull();
        assertThat(avg).isCloseTo((0.9 + 0.85) / 2.0, within(1e-6));
    }

    @Test @Order(21)
    void confidenceAggregate_min() {
        Double min = repo.confidenceAggregate(
            TENANT_A,
            List.of("file:///filter-a.pdf", "file:///filter-b.pdf"),
            "min_confidence");

        assertThat(min).as("min confidence").isCloseTo(0.85, within(1e-6));
    }

    @Test @Order(22)
    void confidenceAggregate_max() {
        Double max = repo.confidenceAggregate(
            TENANT_A,
            List.of("file:///filter-a.pdf", "file:///filter-b.pdf"),
            "max_confidence");

        assertThat(max).as("max confidence").isCloseTo(0.9, within(1e-6));
    }

    @Test @Order(23)
    void confidenceAggregate_emptySet_returnsNull() {
        Double val = repo.confidenceAggregate(
            TENANT_A,
            List.of("file:///does-not-exist.pdf"),
            "avg_confidence");

        assertThat(val).as("empty result set must return null").isNull();
    }

    @Test @Order(24)
    void confidenceAggregate_crossTenant_rls_isolation() {
        // Seed a row for TENANT_B to verify isolation
        seed(TENANT_B, "coll-b", "b-doc.pdf", "file:///b-doc.pdf",
             "Method B", "[]", "{}", 0.5, "op-doc-b1");

        Double val = repo.confidenceAggregate(
            TENANT_B,
            List.of("file:///filter-a.pdf"),  // belongs to TENANT_A
            "avg_confidence");

        assertThat(val).as("TENANT_B must not see TENANT_A confidence").isNull();
    }

    @Test @Order(25)
    void confidenceAggregate_unknownReducerKind_returnsNull() {
        Double val = repo.confidenceAggregate(
            TENANT_A,
            List.of("file:///filter-a.pdf"),
            "unknown_reducer");

        assertThat(val).as("unknown reducer kind must return null").isNull();
    }

    // ── C1: field allowlist / injection-rejection tests ────────────────────────

    @Test @Order(30)
    void validateField_knownColumn_passes() {
        // All ALLOWED_ASPECT_COLUMNS must validate without exception
        for (String col : AspectRepository.ALLOWED_ASPECT_COLUMNS) {
            // Should not throw
            assertThatCode(() -> AspectRepository.validateField(col))
                .as("known column " + col + " must pass validation")
                .doesNotThrowAnyException();
        }
    }

    @Test @Order(31)
    void validateField_extrasKey_passes() {
        assertThatCode(() -> AspectRepository.validateField("extras.venue"))
            .doesNotThrowAnyException();
        assertThatCode(() -> AspectRepository.validateField("extras.meta.year"))
            .doesNotThrowAnyException();
    }

    @Test @Order(32)
    void validateField_injectionAttempt_throws() {
        // Classic SQL injection attempt must be rejected with IllegalArgumentException
        assertThatThrownBy(() -> AspectRepository.validateField(
                "x; DROP TABLE nexus.document_aspects; --"))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test @Order(33)
    void validateField_unknownColumn_throws() {
        assertThatThrownBy(() -> AspectRepository.validateField("not_a_real_column"))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test @Order(34)
    void validateField_blankField_throws() {
        assertThatThrownBy(() -> AspectRepository.validateField(""))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> AspectRepository.validateField("   "))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test @Order(35)
    void filter_unknownField_throws400() {
        // filterBySourceUris must throw IllegalArgumentException for unknown field
        // (AspectHandler routes this to HTTP 400)
        assertThatThrownBy(() -> repo.filterBySourceUris(
                TENANT_A,
                List.of("file:///filter-a.pdf"),
                "injected; DROP TABLE nexus.document_aspects; --",
                "%paxos%"))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test @Order(36)
    void groupby_unknownField_throws400() {
        assertThatThrownBy(() -> repo.groupByField(
                TENANT_A,
                List.of("file:///filter-a.pdf"),
                "injected; SELECT 1; --"))
            .isInstanceOf(IllegalArgumentException.class);
    }

    // ── H1: nested extras key traversal (extras.meta.year) ────────────────────

    @Test @Order(40)
    void groupby_nestedExtrasKey_traversesCorrectly() {
        // Seed a row with nested extras: {"meta": {"year": "2023"}}
        seed(TENANT_A, "coll-op", "nested-extras.pdf", "file:///nested-extras.pdf",
             "Nested method", "[]",
             "{\"meta\":{\"year\":\"2023\"}}", 0.80, null);

        // extras.meta.year — Postgres must use #>>'{meta,year}' (not ->>'meta.year')
        Map<String, String> groups = repo.groupByField(
            TENANT_A,
            List.of("file:///nested-extras.pdf"),
            "extras.meta.year");

        assertThat(groups).as("nested extras.meta.year must resolve to '2023'")
            .containsEntry("file:///nested-extras.pdf", "2023");
    }

    @Test @Order(41)
    void filter_nestedExtrasKey_matchesCorrectly() {
        // Use the row seeded in test @Order(40)
        List<String> matched = repo.filterBySourceUris(
            TENANT_A,
            List.of("file:///nested-extras.pdf"),
            "extras.meta.year",
            "%2023%");

        assertThat(matched).as("extras.meta.year ILIKE '%2023%' must match")
            .containsExactly("file:///nested-extras.pdf");
    }
}
