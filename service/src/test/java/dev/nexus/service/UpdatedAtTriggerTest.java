// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.impl.DSL;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.*;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-154 P2 (bead nexus-2zv75) — updated_at BEFORE UPDATE triggers.
 *
 * <p>Asserts: updated_at exists on EXACTLY document_aspects + topics; the shared
 * stamp function is SECURITY INVOKER; a partial UPDATE moves updated_at forward;
 * and the four append-only logs provably have NO updated_at column.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class UpdatedAtTriggerTest {

    private static final String TENANT = "uat-tenant";
    private static final OffsetDateTime OLD_TS = OffsetDateTime.parse("2020-01-01T00:00:00+00:00");

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        // role-001 (the master changelog's first include) creates nexus_svc.
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
    }

    @AfterAll
    void stopAll() {
        if (pg != null) pg.stop();
    }

    @Test
    void updatedAt_presentOnExactlyTheTwoTables() throws Exception {
        try (Connection c = pg.createConnection("")) {
            assertThat(hasColumn(c, "document_aspects", "updated_at"))
                .as("document_aspects must have updated_at").isTrue();
            assertThat(hasColumn(c, "topics", "updated_at"))
                .as("topics must have updated_at").isTrue();
        }
    }

    @Test
    void updatedAt_absentFromEveryOtherTable() throws Exception {
        // The boundary is "EXACTLY document_aspects + topics" (Decision 2). Assert
        // updated_at is absent BOTH from the append-only logs (must not imply
        // mutability) AND from the mutable tenant tables that already carry a
        // purpose-built timestamp (a future author must not reflexively add it).
        try (Connection c = pg.createConnection("")) {
            for (String t : List.of(
                    // append-only logs
                    "chash_index", "nx_answer_runs", "hook_failures", "aspect_promotion_log",
                    // mutable tables deliberately excluded (have fit-for-purpose stamps)
                    "catalog_documents", "catalog_collections", "catalog_links",
                    "memory", "plans", "taxonomy_meta", "topic_links")) {
                assertThat(hasColumn(c, t, "updated_at"))
                    .as("nexus.%s must NOT have updated_at (only document_aspects + topics do)", t)
                    .isFalse();
            }
        }
    }

    @Test
    void stampFunction_isSecurityInvoker() throws Exception {
        try (Connection c = pg.createConnection("")) {
            Boolean prosecdef = PgCatalogProbes.routineSecurityDefiner(
                DSL.using(c, SQLDialect.POSTGRES), "nexus", "stamp_updated_at");
            assertThat(prosecdef).as("stamp_updated_at must exist").isNotNull();
            assertThat(prosecdef)
                .as("stamp_updated_at must be SECURITY INVOKER (prosecdef=false)").isFalse();
        }
    }

    @Test
    void topics_partialUpdate_movesUpdatedAt() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // Insert with a deliberately OLD updated_at (INSERT does not fire the
            // BEFORE UPDATE trigger), then UPDATE a single column.
            // RDR-164 P1a: register collection 'c' (topics_collection_fk). RDR-204
            // nexus-ft04v.4/.5: routed through PgContainerHelper.insertCollection.
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, "c");
            DSL.using(su, SQLDialect.POSTGRES).insertInto(TOPICS)
                .set(TOPICS.TENANT_ID, TENANT)
                .set(TOPICS.LABEL, "uat-topic")
                .set(TOPICS.COLLECTION, "c")
                .set(TOPICS.DOC_COUNT, 0)
                .set(TOPICS.CREATED_AT, DSL.currentOffsetDateTime())
                .set(TOPICS.REVIEW_STATUS, "pending")
                .set(TOPICS.UPDATED_AT, OLD_TS)
                .execute();
            DSL.using(su, SQLDialect.POSTGRES).update(TOPICS)
                .set(TOPICS.LABEL, "uat-topic-renamed")
                .where(TOPICS.TENANT_ID.eq(TENANT).and(TOPICS.LABEL.eq("uat-topic")))
                .execute();
            OffsetDateTime stamped = DSL.using(su, SQLDialect.POSTGRES).select(TOPICS.UPDATED_AT)
                .from(TOPICS)
                .where(TOPICS.TENANT_ID.eq(TENANT).and(TOPICS.LABEL.eq("uat-topic-renamed")))
                .fetchOne(TOPICS.UPDATED_AT);
            assertThat(stamped).isNotNull();
            var updatedAt = stamped.toInstant();
            assertThat(updatedAt)
                .as("BEFORE UPDATE trigger must move updated_at off the seeded 2020 value")
                .isAfter(java.time.OffsetDateTime.parse("2020-01-02T00:00:00+00:00").toInstant());
        }
    }

    @Test
    void documentAspects_partialUpdate_movesUpdatedAt() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // RDR-164 P1a: register collection 'c' (document_aspects_collection_fk).
            // RDR-204 nexus-ft04v.4/.5: routed through PgContainerHelper.insertCollection.
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, "c");
            // hygiene-001 step 1 (nexus-tk070.p6a follow-on): doc_id/source_uri are NOT
            // NULL now -- seed a real catalog-document parent and supply both.
            DSL.using(su, SQLDialect.POSTGRES).insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
                .set(CATALOG_DOCUMENTS.TUMBLER, "uat-doc-1")
                .set(CATALOG_DOCUMENTS.TITLE, "Doc")
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, "c")
                .onConflict(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER).doNothing()
                .execute();
            // nexus-cefa1.4: salient_sentences is jsonb now — bare 'before'/'after'
            // TEXT literals are no longer valid input; use quoted JSON strings.
            DSL.using(su, SQLDialect.POSTGRES).insertInto(DOCUMENT_ASPECTS)
                .set(DOCUMENT_ASPECTS.TENANT_ID, TENANT)
                .set(DOCUMENT_ASPECTS.COLLECTION, "c")
                .set(DOCUMENT_ASPECTS.SOURCE_PATH, "p1")
                .set(DOCUMENT_ASPECTS.EXTRACTED_AT, DSL.currentOffsetDateTime())
                .set(DOCUMENT_ASPECTS.MODEL_VERSION, "v1")
                .set(DOCUMENT_ASPECTS.EXTRACTOR_NAME, "ex")
                .set(DOCUMENT_ASPECTS.SALIENT_SENTENCES, JSONB.valueOf("\"before\""))
                .set(DOCUMENT_ASPECTS.UPDATED_AT, OLD_TS)
                .set(DOCUMENT_ASPECTS.DOC_ID, "uat-doc-1")
                .set(DOCUMENT_ASPECTS.SOURCE_URI, "file:///p1")
                .execute();
            // Partial UPDATE to salient_sentences (the path that bypasses extracted_at).
            DSL.using(su, SQLDialect.POSTGRES).update(DOCUMENT_ASPECTS)
                .set(DOCUMENT_ASPECTS.SALIENT_SENTENCES, JSONB.valueOf("\"after\""))
                .where(DOCUMENT_ASPECTS.TENANT_ID.eq(TENANT)
                    .and(DOCUMENT_ASPECTS.COLLECTION.eq("c"))
                    .and(DOCUMENT_ASPECTS.SOURCE_PATH.eq("p1")))
                .execute();
            OffsetDateTime stamped = DSL.using(su, SQLDialect.POSTGRES).select(DOCUMENT_ASPECTS.UPDATED_AT)
                .from(DOCUMENT_ASPECTS)
                .where(DOCUMENT_ASPECTS.TENANT_ID.eq(TENANT)
                    .and(DOCUMENT_ASPECTS.COLLECTION.eq("c"))
                    .and(DOCUMENT_ASPECTS.SOURCE_PATH.eq("p1")))
                .fetchOne(DOCUMENT_ASPECTS.UPDATED_AT);
            assertThat(stamped).isNotNull();
            var updatedAt = stamped.toInstant();
            assertThat(updatedAt)
                .as("partial UPDATE to salient_sentences must move updated_at")
                .isAfter(java.time.OffsetDateTime.parse("2020-01-02T00:00:00+00:00").toInstant());
        }
    }

    private static boolean hasColumn(Connection c, String table, String col) throws Exception {
        return PgCatalogProbes.columnExists(DSL.using(c, SQLDialect.POSTGRES), "nexus", table, col);
    }
}
