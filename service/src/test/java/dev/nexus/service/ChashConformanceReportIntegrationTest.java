// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * substantive-critic CRITICAL (nexus-5nrzk, T2 [21458]): the "seeding a
 * non-conformant chash row is impossible by construction" claim in the
 * Python test suite (tests/db/test_du2dw_chash_conformance_report_engine.py,
 * tests/test_health_service_checks.py::TestCheckChashConformanceReport) was
 * FALSE as originally written — {@link RekeyOpsIntegrationTest}'s {@code
 * withChecksDropped} helper already does exactly this, at the Java layer,
 * via a superuser CHECK-constraint-drop maneuver. THIS class is that
 * missing proof: {@code nexus.chash_conformance_report(dim)} counting a
 * REAL poisoned row and returning its hex in {@code sample_chashes} — the
 * function's {@code non_conformant > 0} branch executing against real
 * poison for the first time (RDR-180, bead nexus-du2dw).
 *
 * <p>Self-contained (own Postgres container via {@link PgContainerHelper},
 * own service role, own Liquibase run) rather than added to {@code
 * CatalogRepositoryTest} — that file is a large {@code @Order}-dependent,
 * shared-fixture class, and a poisoning maneuver (dropping/re-adding CHECK
 * constraints mid-suite) is exactly the kind of cross-test side effect its
 * ordering discipline is fragile against. Mirrors {@link
 * RekeyOpsIntegrationTest}'s fixture shape (own container, own role, own
 * {@code withChecksDropped}) rather than reusing its private helpers
 * directly (package-private-but-different-class visibility, and this
 * class's seeding needs are narrower — one poisoned row, not a full rekey
 * scenario).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class ChashConformanceReportIntegrationTest {

    private static final String SVC_ROLE = "svc_chashconf_test";
    private static final String SVC_PASS = "svc_chashconf_pw";
    private static final String TENANT_A = "t-chashconf-a";
    private static final String TENANT_B = "t-chashconf-b";
    // A routable (bge-768) collection name, so catalog_document_chunks rows
    // for it are ALSO counted (matches nexus.chash_conformance_report's
    // dim=768 IN-list, the same routing the now-retired manifest_orphans
    // used, RDR-191 Phase 6 nexus-o8dil.33).
    private static final String COLLECTION = "knowledge__chashconf__bge-base-en-v15-768__v1";
    // RDR-225: the model the collection name's token registers it under (insertCollection).
    private static final String MODEL = "bge-base-en-v15-768";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    CatalogRepository repo;

    private static byte[] sha256(String text) {
        try {
            return MessageDigest.getInstance("SHA-256")
                .digest(text.getBytes(StandardCharsets.UTF_8));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private static Vector vec(int dim) {
        return Vector.of(new float[dim]);
    }

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(3);
        config.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(config);
        repo = new CatalogRepository(new TenantScope(svcDs));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── seeding (superuser: same reconstruction RekeyOpsIntegrationTest's
    //    withChecksDropped uses — drop the width CHECKs, seed the poisoned
    //    row(s), re-add NOT VALID; the client-facing write path enforces the
    //    CHECK even NOT VALID, so this maneuver is the ONLY way to get a
    //    non-conformant row into a real Postgres instance) ─────────────────

    // RDR-191 Phase 4 (repoint-batch lane F1): nexus.chunks_768 unified into
    // nexus.chunks -- the octet-width CHECK is now ONE constraint shared
    // across all three embedding_<dim> columns (chunks_chash_octet_check),
    // not a per-dim constraint. Dropping it opens the poisoning window for
    // whichever dim column this test's insert populates.
    private void withChecksDropped(Connection su, Runnable seed) throws Exception {
        DSL.using(su, SQLDialect.POSTGRES).alterTable(CHUNKS).dropConstraint("chunks_chash_octet_check").execute();
        DSL.using(su, SQLDialect.POSTGRES).alterTable(CATALOG_DOCUMENT_CHUNKS)
            .dropConstraint("catalog_document_chunks_chash_octet_check").execute();
        try {
            seed.run();
        } finally {
            PgContainerHelper.readdChashOctetChecksNotValid(su);
        }
    }

    private static void insertChunk768(Connection su, String tenant, byte[] chash, String text) {
        DSL.using(su, SQLDialect.POSTGRES).insertInto(CHUNKS)
            .set(CHUNKS.TENANT_ID, tenant)
            .set(CHUNKS.COLLECTION, COLLECTION)
                .set(CHUNKS.COLLECTION_ID, CollectionIds.idOf(tenant, COLLECTION))
            .set(CHUNKS.CHASH, chash)
            .set(CHUNKS.EMBEDDING_MODEL, MODEL)
            .set(CHUNKS.CHUNK_TEXT, text)
            .set(CHUNKS.EMBEDDING_768, vec(768))
            .execute();
    }

    private static Map<String, Object> tableRow(List<Map<String, Object>> report, String tableName) {
        return report.stream()
            .filter(r -> tableName.equals(r.get("table_name")))
            .findFirst()
            .orElseThrow(() -> new AssertionError(
                "no row for table_name=" + tableName + " in report=" + report));
    }

    // ── Test 1: the real poison, counted and sampled ────────────────────────

    @Test
    @Order(1)
    void poisoned_row_is_counted_and_sampled_by_chash_conformance_report() throws Exception {
        // 16 bytes -- the pre-RDR-180 legacy width, width-non-conformant
        // under the current (post-rekey) 32-byte-bytea schema.
        byte[] legacyChash = HexFormat.of().parseHex("a".repeat(32));
        String legacyHex = HexFormat.of().formatHex(legacyChash);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // RDR-204 nexus-ft04v.4/.5: routed through PgContainerHelper.insertCollection,
            // which derives the constraint-satisfying attributes hygiene-002-1 now
            // requires (the bare two-column raw INSERT this used to run 23502s on
            // lifecycle_state NOT NULL).
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT_A, COLLECTION);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            seed.insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT_A)
                .set(CATALOG_DOCUMENTS.TUMBLER, "1.1")
                .set(CATALOG_DOCUMENTS.TITLE, "doc")
                .onConflictDoNothing()
                .execute();
            withChecksDropped(su, () -> {
                // a conformant sibling row too, so total > non_conformant is
                // asserted meaningfully (not just "1 row total, 1 poisoned").
                insertChunk768(su, TENANT_A, sha256("chashconf clean sibling"), "clean sibling");
                insertChunk768(su, TENANT_A, legacyChash, "poisoned legacy row");
                seed.insertInto(CATALOG_DOCUMENT_CHUNKS)
                    .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, TENANT_A)
                    .set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, "1.1")
                    .set(CATALOG_DOCUMENT_CHUNKS.POSITION, 0)
                    .set(CATALOG_DOCUMENT_CHUNKS.CHASH, legacyChash)
                    .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, COLLECTION)
                .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, CollectionIds.idOf(TENANT_A, COLLECTION))
                    .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, MODEL)
                    .execute();
            });
        }

        List<Map<String, Object>> report = repo.chashConformanceReport(TENANT_A, 768);
        assertThat(report).hasSize(2);

        Map<String, Object> chunksRow = tableRow(report, "nexus.chunks_768");
        assertThat(((Number) chunksRow.get("total")).longValue()).isEqualTo(2L);
        assertThat(((Number) chunksRow.get("non_conformant")).longValue()).isEqualTo(1L);
        // jOOQ maps the PG text[] column to a Java String[] (not List) via
        // Record::intoMap.
        assertThat((String[]) chunksRow.get("sample_chashes")).containsExactly(legacyHex);

        Map<String, Object> manifestRow = tableRow(report, "nexus.catalog_document_chunks");
        assertThat(((Number) manifestRow.get("total")).longValue()).isEqualTo(1L);
        assertThat(((Number) manifestRow.get("non_conformant")).longValue()).isEqualTo(1L);
        assertThat((String[]) manifestRow.get("sample_chashes")).containsExactly(legacyHex);
    }

    // ── Test 2: the poison is invisible to another tenant (RLS) ─────────────

    @Test
    @Order(2)
    void poison_is_tenant_isolated_via_rls() throws Exception {
        // TENANT_B has seeded NOTHING -- SECURITY INVOKER + FORCE RLS scopes
        // the function to the request tenant GUC (refutes the 'cross-tenant
        // scan' reading; the SAME class of falsification the now-retired
        // manifest_orphans coverage used to demonstrate, RDR-191 Phase 6
        // nexus-o8dil.33).
        List<Map<String, Object>> report = repo.chashConformanceReport(TENANT_B, 768);
        Map<String, Object> chunksRow = tableRow(report, "nexus.chunks_768");
        assertThat(((Number) chunksRow.get("total")).longValue()).isZero();
        assertThat(((Number) chunksRow.get("non_conformant")).longValue()).isZero();
        assertThat((String[]) chunksRow.get("sample_chashes")).isEmpty();
    }

    // ── RDR-229 P2.6 (nexus-p2m1x.11): the report reads the collection's catalog row, not its name ─────────────

    private static final String NO_ROW_LINE = "nexus.catalog_document_chunks (no catalog row)";
    private static final String TENANT_LABEL = "t-chashconf-label";
    private static final String TENANT_AGREE = "t-chashconf-agree";

    private static Vector vecOf(int dim) {
        return Vector.of(new float[dim]);
    }

    /** A chunk and its manifest row under {@code collection}, written at {@code dim} wide with {@code model}. */
    private static void chunkAndManifest(Connection su, String tenant, String collection, String model, int dim,
                                         String doc, int position, byte[] chash, String text) {
        DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
        var ins = seed.insertInto(CHUNKS)
            .set(CHUNKS.TENANT_ID, tenant)
            .set(CHUNKS.COLLECTION, collection)
                .set(CHUNKS.COLLECTION_ID, CollectionIds.idOf(tenant, collection))
            .set(CHUNKS.CHASH, chash)
            .set(CHUNKS.EMBEDDING_MODEL, model)
            .set(CHUNKS.CHUNK_TEXT, text);
        switch (dim) {
            case 384 -> ins.set(CHUNKS.EMBEDDING_384, vecOf(384)).execute();
            case 768 -> ins.set(CHUNKS.EMBEDDING_768, vecOf(768)).execute();
            default -> ins.set(CHUNKS.EMBEDDING_1024, vecOf(1024)).execute();
        }
        manifestRow(seed, tenant, collection, model, doc, position, chash);
    }

    private static void manifestRow(DSLContext seed, String tenant, String collection, String model, String doc,
                                    int position, byte[] chash) {
        seed.insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, tenant)
            .set(CATALOG_DOCUMENTS.TUMBLER, doc)
            .set(CATALOG_DOCUMENTS.TITLE, "doc " + doc)
            .onConflictDoNothing()
            .execute();
        seed.insertInto(CATALOG_DOCUMENT_CHUNKS)
            .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, tenant)
            .set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, doc)
            .set(CATALOG_DOCUMENT_CHUNKS.POSITION, position)
            // the stamp trigger fills it, and it is off while the foreign keys are set aside for a row-less seed
            .set(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, position)
            .set(CATALOG_DOCUMENT_CHUNKS.CHASH, chash)
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, collection)
            // RDR-229 P3.1: the id beside the name; a name no registry row backs (the legacy and no-catalog-row
            // cases) gets one the registry never issued, so the CHECK passes and the foreign key is what refuses.
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, registeredIdOrUnissued(seed, tenant, collection))
            .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, model)
            .execute();
    }

    private static Long registeredIdOrUnissued(DSLContext seed, String tenant, String collection) {
        Long id = seed.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(collection))
            .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
        return id != null ? id : Long.MAX_VALUE;
    }

    private static long total(List<Map<String, Object>> report, String tableName) {
        return report.stream().filter(r -> tableName.equals(r.get("table_name")))
            .mapToLong(r -> ((Number) r.get("total")).longValue()).sum();
    }

    private static boolean hasLine(List<Map<String, Object>> report, String tableName) {
        return report.stream().anyMatch(r -> tableName.equals(r.get("table_name")));
    }

    @Test
    @Order(4)
    void a_collection_whose_catalog_row_names_the_model_is_counted_though_its_name_has_no_model_token() throws Exception {
        // A label with no "__" and no model token: the name says nothing, the row says bge-base-en-v15-768.
        String label = "plain-label-no-token";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT_LABEL, label, MODEL);
            chunkAndManifest(su, TENANT_LABEL, label, MODEL, 768, "2.1", 0, sha256("label one"), "label one");
            chunkAndManifest(su, TENANT_LABEL, label, MODEL, 768, "2.1", 1, sha256("label two"), "label two");
        }

        List<Map<String, Object>> r768 = repo.chashConformanceReport(TENANT_LABEL, 768);
        assertThat(total(r768, "nexus.catalog_document_chunks"))
            .as("the collection's row says the 768 model: its two manifest rows are counted at dim 768").isEqualTo(2L);
        assertThat(hasLine(r768, NO_ROW_LINE)).as("it has a catalog row: not on the no-catalog-row line").isFalse();
        assertThat(total(repo.chashConformanceReport(TENANT_LABEL, 384), "nexus.catalog_document_chunks")).isZero();
        assertThat(total(repo.chashConformanceReport(TENANT_LABEL, 1024), "nexus.catalog_document_chunks")).isZero();
    }

    @Test
    @Order(5)
    void a_name_that_says_one_model_while_the_row_says_another_is_counted_under_the_row() throws Exception {
        // The name carries the 1024 token and the row registers the 768 model: the row decides.
        String liar = "knowledge__liar__voyage-context-3__v1";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT_LABEL, liar, MODEL);
            chunkAndManifest(su, TENANT_LABEL, liar, MODEL, 768, "2.2", 0, sha256("liar one"), "liar one");
        }

        assertThat(total(repo.chashConformanceReport(TENANT_LABEL, 768), "nexus.catalog_document_chunks"))
            .as("two plain-label rows plus the row under the 768 model").isEqualTo(3L);
        assertThat(total(repo.chashConformanceReport(TENANT_LABEL, 1024), "nexus.catalog_document_chunks"))
            .as("the name's 1024 token is not read").isZero();
    }

    /** The vectors-005-11 manifest arm (the name-parsing predicate this changeset removes), as a query over one tenant. */
    private long[] oldManifestCounts(DSLContext d, String tenant, int dim) {
        List<String> models = switch (dim) {
            case 384 -> List.of("minilm-l6-v2-384");
            case 768 -> List.of("bge-base-en-v15-768");
            default -> List.of("voyage-code-3", "voyage-context-3", "voyage-3");
        };
        var modelFromName = DSL.function("split_part", String.class, CATALOG_DOCUMENT_CHUNKS.COLLECTION,
            DSL.inline("__"), DSL.inline(3));
        var rec = d.select(DSL.count(), DSL.count().filterWhere(DSL.function("octet_length", Integer.class, CATALOG_DOCUMENT_CHUNKS.CHASH).ne(32)))
            .from(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant)
                .and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.isNotNull()).and(modelFromName.in(models)))
            .fetchOne();
        return new long[] {rec.value1(), rec.value2()};
    }

    @Test
    @Order(6)
    void old_and_new_totals_agree_per_dim_when_a_legacy_collection_has_no_catalog_row() throws Exception {
        String c384 = "knowledge__agree384__minilm-l6-v2-384__v1";
        String c768 = "knowledge__agree768__bge-base-en-v15-768__v1";
        String c1024 = "knowledge__agree1024__voyage-context-3__v1";
        // Legacy: manifest rows whose collection has no catalog row at all (a name nothing registered).
        String legacy = "knowledge__legacy__bge-base-en-v15-768__v1";
        byte[] poisoned = HexFormat.of().parseHex("b".repeat(32));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext d = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(d, TENANT_AGREE, c384);
            PgContainerHelper.insertCollection(d, TENANT_AGREE, c768);
            PgContainerHelper.insertCollection(d, TENANT_AGREE, c1024);
            chunkAndManifest(su, TENANT_AGREE, c384, "minilm-l6-v2-384", 384, "3.1", 0, sha256("a384"), "a384");
            chunkAndManifest(su, TENANT_AGREE, c768, MODEL, 768, "3.2", 0, sha256("a768 clean"), "a768 clean");
            chunkAndManifest(su, TENANT_AGREE, c1024, "voyage-context-3", 1024, "3.3", 0, sha256("a1024"), "a1024");
            // The row-less rows: no catalog row and no chunk row, so the foreign keys are set aside for the insert.
            d.set(DSL.name("session_replication_role"), DSL.inline("replica")).execute();
            try {
                withChecksDropped(su, () -> {
                    manifestRow(d, TENANT_AGREE, legacy, MODEL, "3.4", 0, sha256("legacy clean"));
                    manifestRow(d, TENANT_AGREE, legacy, MODEL, "3.4", 1, poisoned);
                });
            } finally {
                d.set(DSL.name("session_replication_role"), DSL.inline("origin")).execute();
            }
            assertThat(d.fetchExists(CATALOG_COLLECTIONS,
                CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT_AGREE).and(CATALOG_COLLECTIONS.NAME.eq(legacy))))
                .as("the legacy collection has no catalog row").isFalse();

            for (int dim : new int[] {384, 768, 1024}) {
                long[] old = oldManifestCounts(d, TENANT_AGREE, dim);
                List<Map<String, Object>> report = repo.chashConformanceReport(TENANT_AGREE, dim);
                long newTotal = 0;
                long newBad = 0;
                for (Map<String, Object> row : report) {
                    String name = (String) row.get("table_name");
                    if (name.startsWith("nexus.catalog_document_chunks")) {
                        newTotal += ((Number) row.get("total")).longValue();
                        newBad += ((Number) row.get("non_conformant")).longValue();
                    }
                }
                assertThat(newTotal).as("dim %d: manifest total, old name read vs new row read", dim).isEqualTo(old[0]);
                assertThat(newBad).as("dim %d: manifest non-conformant, old vs new", dim).isEqualTo(old[1]);
            }
        }

        List<Map<String, Object>> r768 = repo.chashConformanceReport(TENANT_AGREE, 768);
        assertThat(total(r768, "nexus.catalog_document_chunks")).as("the registered 768 collection").isEqualTo(1L);
        Map<String, Object> noRow = tableRow(r768, NO_ROW_LINE);
        assertThat(((Number) noRow.get("total")).longValue()).as("the two row-less manifest rows").isEqualTo(2L);
        assertThat(((Number) noRow.get("non_conformant")).longValue()).isEqualTo(1L);
        assertThat((String[]) noRow.get("sample_chashes")).containsExactly(HexFormat.of().formatHex(poisoned));
        assertThat(hasLine(repo.chashConformanceReport(TENANT_AGREE, 384), NO_ROW_LINE))
            .as("no row-less rows at 384: no line").isFalse();
        assertThat(hasLine(repo.chashConformanceReport(TENANT_AGREE, 1024), NO_ROW_LINE)).isFalse();
    }

    private static final String TENANT_FK = "t-chashconf-fk";

    @Test
    @Order(7)
    void no_manifest_row_can_name_a_collection_without_a_catalog_row_or_a_model_outside_embedding_models() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext d = DSL.using(su, SQLDialect.POSTGRES);
            // The chain: manifest (fk_catalog_chunks_chunk, vectors-030 7.6) -> chunks (chunks_collection_fk, vectors-030 7.6)
            // -> catalog_collections (catalog_collections_embedding_model_fk, hygiene-002-1) -> embedding_models.
            for (String con : List.of("fk_catalog_chunks_chunk", "chunks_collection_fk", "catalog_collections_embedding_model_fk")) {
                assertThat(PgCatalogProbes.constraintValidated(d, con)).as(con + " exists and is validated").isTrue();
            }

            // A model outside embedding_models cannot be registered, 'voyage-3' (the old 1024 list's third entry) included.
            for (String model : List.of("voyage-3", "mystery-model")) {
                String name = "knowledge__fk" + model.length() + "__bge-base-en-v15-768__v1";
                assertThatThrownBy(() -> PgContainerHelper.insertCollection(d, TENANT_FK, name, model))
                    .as("registering a collection under " + model)
                    .hasMessageContaining("catalog_collections_embedding_model_fk");
            }

            // A manifest row cannot be written for a collection with no catalog row, under a known model or an unknown one.
            for (String model : List.of(MODEL, "mystery-model")) {
                assertThatThrownBy(() -> manifestRow(d, TENANT_FK, "knowledge__nocat__bge-base-en-v15-768__v1", model, "4.1", 0,
                    sha256("no catalog row " + model)))
                    .as("a manifest row under a collection with no catalog row, model " + model)
                    .hasMessageContaining("fk_catalog_chunks_chunk");
            }

            // A chunk cannot carry a model other than its collection's registered one, so neither can its manifest row.
            String bge = "knowledge__fkbge__bge-base-en-v15-768__v1";
            PgContainerHelper.insertCollection(d, TENANT_FK, bge);
            assertThatThrownBy(() -> d.insertInto(CHUNKS)
                .set(CHUNKS.TENANT_ID, TENANT_FK).set(CHUNKS.COLLECTION, bge)
                .set(CHUNKS.COLLECTION_ID, CollectionIds.idOf(TENANT_FK, bge)).set(CHUNKS.CHASH, sha256("wrong model"))
                .set(CHUNKS.EMBEDDING_MODEL, "minilm-l6-v2-384").set(CHUNKS.CHUNK_TEXT, "wrong model")
                .set(CHUNKS.EMBEDDING_384, vecOf(384)).execute())
                .hasMessageContaining("chunks_collection_fk");
        }
    }

    // ── Test 3: dim validation ────────────────────────────────────────────

    @Test
    @Order(3)
    void rejects_unsupported_dim() {
        assertThatThrownBy(() -> repo.chashConformanceReport(TENANT_A, 999))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("unsupported dim");
    }
}
