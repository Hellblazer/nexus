// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.PgActivityProbe;
import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-254 deadlock retry follow-up: the aspect upsert reads its document's catalog row {@code FOR SHARE}
 * ({@link AspectDocIdAttribution}) BEFORE it writes {@code document_aspects}; the collection rename and the
 * collection delete write {@code document_aspects} BEFORE they touch {@code catalog_documents}
 * ({@code CatalogRepository.COLLECTION_SCOPED_TABLES} order). Opposite orders, so an unfenced upsert racing a
 * rename or delete of its collection can deadlock, and PostgreSQL aborts one side with 40P01. Both sides must
 * retry, so neither surfaces an error.
 *
 * <p>Every test builds the cycle with a genuine, unmocked 40P01. The opposing side is a superuser connection
 * whose own {@code deadlock_timeout} is set far above the service's (the pattern of
 * {@code ManifestWriteDeadlockRetryTest}): the service statement starts waiting first, runs its deadlock check
 * first and is the victim every time, so which side is aborted is not left to timing. Each test asserts the
 * retry happened ({@link DeadlockRetry#retryAttemptCount()} moved) and that the data is right afterwards.
 *
 * <ul>
 *   <li>{@link #theUpsertIsRetriedWhenItIsTheDeadlockVictim}: real upsert against a writer that took the aspect
 *       row first and then wants the catalog row. This is the side that had no retry before this change.</li>
 *   <li>{@link #theRenameIsRetriedWhenItIsTheDeadlockVictim} and
 *       {@link #theCollectionDeleteIsRetriedWhenItIsTheDeadlockVictim}: real rename and delete against a writer
 *       that holds the catalog row {@code FOR SHARE} (what the upsert holds) and then wants the aspect row.
 *       Both already ran under {@code DeadlockRetry} through {@code manifestWriteTxn} (nexus-wbfpw.66); these
 *       pin that for this specific cycle.</li>
 * </ul>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class AspectUpsertDeadlockRetryTest {

    private static final String SVC_ROLE = "svc_aspect_deadlock_test";
    private static final String SVC_PASS = "svc_aspect_deadlock_pass";
    private static final String COLL = "knowledge__aspectdeadlock__bge-base-en-v15-768__v1";
    private static final String COLL2 = "knowledge__aspectdeadlocktwo__bge-base-en-v15-768__v1";
    private static final String URI = "file:///d/a.pdf";
    private static final String PATH = "/d/a.pdf";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    CatalogRepository repo;
    AspectRepository aspects;
    MoveTestDb db;
    ExecutorService pool;
    int tenantSeq;

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
        config.setMaximumPoolSize(6);
        config.setConnectionTimeout(5000);
        config.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(config);
        var scope = new TenantScope(svcDs);
        repo = new CatalogRepository(scope);
        aspects = new AspectRepository(scope);
        db = new MoveTestDb(pg);
        pool = Executors.newFixedThreadPool(3);
    }

    @AfterAll
    void stopAll() {
        if (pool != null) pool.shutdownNow();
        if (repo != null) repo.close();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── fixtures ────────────────────────────────────────────────────────────

    private record Seeded(String tenant, String doc) { }

    /** A live document in COLL with its first aspect row written. */
    private Seeded seed() throws Exception {
        String t = "aspect-deadlock-" + (++tenantSeq);
        Map<String, Object> o = new LinkedHashMap<>();
        o.put("tumbler_prefix", "1.1");
        o.put("name", "pdfs");
        o.put("owner_type", "curator");
        repo.upsertOwner(t, o);
        Map<String, Object> f = new LinkedHashMap<>();
        f.put("title", "a");
        f.put("file_path", PATH);
        f.put("content_type", "paper");
        f.put("corpus", "papers");
        f.put("physical_collection", COLL);
        f.put("source_uri", URI);
        String doc = repo.registerDocument(t, "1.1", f);
        db.registerCollection(t, COLL);
        upsert(t, doc, "first extraction");
        return new Seeded(t, doc);
    }

    private long upsert(String t, String docId, String text) {
        Map<String, Object> b = new LinkedHashMap<>();
        b.put("collection", COLL);
        b.put("source_path", PATH);
        b.put("source_uri", URI);
        b.put("doc_id", docId);
        b.put("problem_formulation", text);
        b.put("confidence", 0.9);
        b.put("extracted_at", OffsetDateTime.now().toString());
        b.put("model_version", "v1");
        b.put("extractor_name", "test");
        return aspects.upsertAspect(t, b);
    }

    /** A superuser writer in an open transaction that never runs its own deadlock check first. */
    private DSLContext otherWriter(Connection other) {
        DSLContext ex = DSL.using(other, SQLDialect.POSTGRES);
        ex.select(DSL.function("set_config", String.class,
            DSL.val("deadlock_timeout"), DSL.val("120s"), DSL.val(false))).fetch();
        return ex;
    }

    private void lockAspectRow(DSLContext ex, Seeded s) {
        ex.update(DOCUMENT_ASPECTS).set(DOCUMENT_ASPECTS.EXTRACTOR_NAME, "other-writer")
            .where(DOCUMENT_ASPECTS.TENANT_ID.eq(s.tenant()).and(DOCUMENT_ASPECTS.COLLECTION.eq(COLL))).execute();
    }

    private void touchCatalogRow(DSLContext ex, Seeded s) {
        ex.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.TITLE, "touched")
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(s.tenant()).and(CATALOG_DOCUMENTS.TUMBLER.eq(s.doc()))).execute();
    }

    private void shareLockCatalogRow(DSLContext ex, Seeded s) {
        ex.select(CATALOG_DOCUMENTS.TUMBLER).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(s.tenant()).and(CATALOG_DOCUMENTS.TUMBLER.eq(s.doc())))
            .forShare().fetch();
    }

    // ── the upsert is the victim ────────────────────────────────────────────

    @Test
    void theUpsertIsRetriedWhenItIsTheDeadlockVictim() throws Exception {
        Seeded s = seed();
        long retriesBefore = DeadlockRetry.retryAttemptCount();
        try (Connection other = pg.createConnection("")) {
            other.setAutoCommit(false);
            DSLContext ex = otherWriter(other);
            // A rename's order: the aspect row first.
            lockAspectRow(ex, s);

            // The upsert takes the catalog row FOR SHARE, then waits for the aspect row the other writer holds.
            Future<Long> upsert = pool.submit(() -> upsert(s.tenant(), s.doc(), "second extraction"));
            assertThat(PgActivityProbe.waitsOnALock(pg, "%document_aspects%"))
                .as("the upsert must block on the aspect row (else no race happened)").isTrue();

            // The other writer now wants the catalog row the upsert holds FOR SHARE: the cycle.
            Future<?> closeCycle = pool.submit(() -> touchCatalogRow(ex, s));
            // Returns when the upsert was killed as the deadlock victim and its locks went.
            closeCycle.get(60, TimeUnit.SECONDS);
            other.commit();

            assertThat(upsert.get(60, TimeUnit.SECONDS)).as("the upsert answers with the row id, not a deadlock")
                .isPositive();
        }
        assertThat(DeadlockRetry.retryAttemptCount() - retriesBefore)
            .as("the upsert must have been killed by a real 40P01 and retried").isGreaterThanOrEqualTo(1);
        var rows = db.aspects(s.tenant());
        assertThat(rows).hasSize(1);
        assertThat(rows.get(0)).containsEntry("doc_id", s.doc()).containsEntry("collection", COLL)
            .containsEntry("problem_formulation", "second extraction");
    }

    // ── the rename and the delete are the victim ────────────────────────────

    @Test
    void theRenameIsRetriedWhenItIsTheDeadlockVictim() throws Exception {
        Seeded s = seed();
        long retriesBefore = DeadlockRetry.retryAttemptCount();
        Map<String, Integer> counts;
        try (Connection other = pg.createConnection("")) {
            other.setAutoCommit(false);
            DSLContext ex = otherWriter(other);
            // An upsert's order: the catalog row FOR SHARE first.
            shareLockCatalogRow(ex, s);

            // The rename writes the aspect rows, then waits for the catalog row the other writer holds.
            Future<Map<String, Integer>> rename = pool.submit(() -> repo.renameCollection(s.tenant(), COLL, COLL2));
            assertThat(PgActivityProbe.waitsOnALock(pg, "%catalog_documents%"))
                .as("the rename must block on the catalog row (else no race happened)").isTrue();

            // The other writer now wants the aspect row the rename holds: the cycle.
            Future<?> closeCycle = pool.submit(() -> lockAspectRow(ex, s));
            closeCycle.get(60, TimeUnit.SECONDS);
            other.commit();

            counts = rename.get(60, TimeUnit.SECONDS);
        }
        assertThat(DeadlockRetry.retryAttemptCount() - retriesBefore)
            .as("the rename must have been killed by a real 40P01 and retried").isGreaterThanOrEqualTo(1);
        assertThat(counts).containsEntry("document_aspects", 1).containsEntry("catalog_documents", 1);
        var rows = db.aspects(s.tenant());
        assertThat(rows).hasSize(1);
        assertThat(rows.get(0)).containsEntry("doc_id", s.doc()).containsEntry("collection", COLL2)
            .containsEntry("problem_formulation", "first extraction");
    }

    @Test
    void theCollectionDeleteIsRetriedWhenItIsTheDeadlockVictim() throws Exception {
        Seeded s = seed();
        long retriesBefore = DeadlockRetry.retryAttemptCount();
        Map<String, Integer> counts;
        try (Connection other = pg.createConnection("")) {
            other.setAutoCommit(false);
            DSLContext ex = otherWriter(other);
            shareLockCatalogRow(ex, s);

            Future<Map<String, Integer>> delete = pool.submit(() -> repo.deleteCollection(s.tenant(), COLL));
            assertThat(PgActivityProbe.waitsOnALock(pg, "%catalog_documents%"))
                .as("the delete must block on the catalog row (else no race happened)").isTrue();

            Future<?> closeCycle = pool.submit(() -> lockAspectRow(ex, s));
            closeCycle.get(60, TimeUnit.SECONDS);
            other.commit();

            counts = delete.get(60, TimeUnit.SECONDS);
        }
        assertThat(DeadlockRetry.retryAttemptCount() - retriesBefore)
            .as("the delete must have been killed by a real 40P01 and retried").isGreaterThanOrEqualTo(1);
        assertThat(counts).containsEntry("document_aspects", 1);
        assertThat(db.aspects(s.tenant())).as("the delete removed the collection's aspect rows").isEmpty();
    }
}
