// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.AspectRepository;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.LockSupport;
import java.util.function.Supplier;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23, row 14): the two writers of {@link AspectRepository} that name a row by {@code doc_id} alone,
 * {@code setSalientSentences} and {@code deleteHighlight}, read the collection of the doc's row {@code FOR KEY SHARE} in their own write
 * transaction before they write (StrictWriterCensus rows AspectRepository.setSalientSentences and AspectRepository.deleteHighlight, no registry
 * read). A verb-race test for each, in two shapes:
 * <ul>
 * <li>the write meets a verb holding the row {@code FOR UPDATE}: it waits (it never writes ahead of the verb), then lands once the verb lets go,
 *     the row keeping its own id and label;</li>
 * <li>the verb re-homes the doc's row to another collection while the write waits: the write follows the row, reads and holds the new
 *     collection too, and lands under the row's current id, never in a collection it did not hold.</li>
 * </ul>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class AspectDocIdWritersRegistryRaceTest {

    private static final String TENANT = "aspect-race-tenant";
    private static final String SVC_ROLE = "svc_aspect_race";
    private static final String SVC_PASS = "svc_aspect_race_pass";
    private static final String STAMP = "2026-06-01T12:00:00.000000Z";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    AspectRepository repo;
    ExecutorService pool;
    int n;

    @BeforeAll
    void start() throws Exception {
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
        repo = new AspectRepository(new TenantScope(svcDs));
        pool = Executors.newFixedThreadPool(2);
    }

    @AfterAll
    void stop() {
        if (pool != null) pool.shutdownNow();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @AfterEach
    void drain() {
        // a failed assertion must not leave a writer parked behind a verb the test never released
        pool.shutdownNow();
        pool = Executors.newFixedThreadPool(2);
    }

    private <T> T asSuperuser(java.util.function.Function<DSLContext, T> work) {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            return work.apply(DSL.using(su, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private long register(String name) {
        return asSuperuser(ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT, name);
            return ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT).and(CATALOG_COLLECTIONS.NAME.eq(name)))
                .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
        });
    }

    private void document(String tumbler) {
        asSuperuser(ctx -> ctx.insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
            .set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
            .set(CATALOG_DOCUMENTS.TITLE, "race fixture " + tumbler)
            .onConflict(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER).doNothing()
            .execute());
    }

    private void seedAspect(String collection, String docId) {
        document(docId);
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("collection", collection);
        body.put("source_path", "race-" + docId + ".pdf");
        body.put("problem_formulation", "p");
        body.put("confidence", 0.9);
        body.put("extracted_at", "2026-06-01T10:00:00.000000Z");
        body.put("model_version", "v2");
        body.put("extractor_name", "scholarly-paper");
        body.put("doc_id", docId);
        assertThat(repo.upsertAspect(TENANT, body)).as("setup: the aspect row was written").isPositive();
    }

    private void seedHighlight(String collection, String docId) {
        document(docId);
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("doc_id", docId);
        body.put("collection", collection);
        body.put("highlights_md", "race highlight");
        body.put("ingested_at", STAMP);
        assertThat(repo.upsertHighlight(TENANT, body)).as("setup: the highlight row was written").isTrue();
    }

    /**
     * Retires {@code name}'s row the way a supersede does: it names its successor, and the children stay where they are (a supersede
     * re-homes nothing). P3.1's CHECK ties {@code superseded_by} to {@code superseded_by_id}.
     */
    private void supersede(String name, String successor, long successorId) {
        int retired = asSuperuser(ctx -> ctx.update(CATALOG_COLLECTIONS)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY, successor).set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, successorId)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT).and(CATALOG_COLLECTIONS.NAME.eq(name))).execute());
        assertThat(retired).as("setup: the row was retired").isEqualTo(1);
    }

    private void lockRow(Connection verb, String name) {
        DSL.using(verb, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name)).forUpdate().fetch();
    }

    private int waitingRows() {
        return asSuperuser(ctx -> ctx.fetchCount(DSL.table(DSL.name("pg_catalog", "pg_locks")),
            DSL.field(DSL.name("granted"), Boolean.class).isFalse()
                .and(DSL.field(DSL.name("locktype"), String.class).in("tuple", "transactionid"))));
    }

    private void awaitWaiting(Future<?> writer, String what) {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
        while (waitingRows() < 1) {
            if (writer.isDone() || System.nanoTime() > deadline) {
                throw new AssertionError(what + ": no lock request ever showed waiting in pg_locks (writer done: " + writer.isDone() + ")");
            }
            LockSupport.parkNanos(TimeUnit.MILLISECONDS.toNanos(5));
        }
    }

    /**
     * Runs {@code write} on a second thread while a hand session holds {@code held}'s registry row {@code FOR UPDATE}; once the write shows
     * waiting, runs {@code verbWork} in the verb's own transaction, commits it, and returns the write's answer.
     */
    private <T> T whileAVerbHolds(String held, java.util.function.Consumer<DSLContext> verbWork, Supplier<T> write) throws Exception {
        Future<T> writer;
        try (Connection verb = pg.createConnection("")) {
            verb.setAutoCommit(false);
            lockRow(verb, held);
            writer = pool.submit(write::get);
            try {
                awaitWaiting(writer, "the write waits for the row the verb holds");
                assertThat(writer.isDone()).as("the write has not gone ahead of the verb").isFalse();
                verbWork.accept(DSL.using(verb, SQLDialect.POSTGRES));
                verb.commit();
            } finally {
                verb.rollback();   // a no-op after the commit
            }
        }
        return writer.get(120, TimeUnit.SECONDS);
    }

    // ── setSalientSentences (document_aspects, by doc_id) ───────────────────────

    @Timeout(180)
    @Test
    void setSalientSentences_meetingAVerbHoldingTheRow_waits_thenLandsUnderTheRowsOwnIdAndLabel() throws Exception {
        String coll = "race-asp-held-" + (n++);
        long id = register(coll);
        String doc = "race-asp-doc-" + (n++);
        seedAspect(coll, doc);

        int updated = whileAVerbHolds(coll, v -> { }, () -> repo.setSalientSentences(TENANT, doc, "[\"after the verb\"]"));

        assertThat(updated).as("the write landed once the verb let go").isEqualTo(1);
        var row = asSuperuser(ctx -> ctx.select(DOCUMENT_ASPECTS.COLLECTION, DOCUMENT_ASPECTS.COLLECTION_ID, DOCUMENT_ASPECTS.SALIENT_SENTENCES)
            .from(DOCUMENT_ASPECTS).where(DOCUMENT_ASPECTS.TENANT_ID.eq(TENANT).and(DOCUMENT_ASPECTS.DOC_ID.eq(doc))).fetchOne());
        assertThat(row.get(DOCUMENT_ASPECTS.COLLECTION)).isEqualTo(coll);
        assertThat(row.get(DOCUMENT_ASPECTS.COLLECTION_ID)).as("the row keeps its own id").isEqualTo(id);
        assertThat(row.get(DOCUMENT_ASPECTS.SALIENT_SENTENCES).data()).contains("after the verb");
    }

    @Timeout(180)
    @Test
    void setSalientSentences_whenTheVerbRehomesTheRowWhileItWaits_followsTheRowAndHoldsTheNewCollection() throws Exception {
        String from = "race-asp-from-" + (n++);
        String to = "race-asp-to-" + (n++);
        register(from);
        long toId = register(to);
        String doc = "race-asp-moved-" + (n++);
        seedAspect(from, doc);

        int updated = whileAVerbHolds(from,
            v -> v.update(DOCUMENT_ASPECTS).set(DOCUMENT_ASPECTS.COLLECTION, to).set(DOCUMENT_ASPECTS.COLLECTION_ID, toId)
                .where(DOCUMENT_ASPECTS.TENANT_ID.eq(TENANT).and(DOCUMENT_ASPECTS.DOC_ID.eq(doc))).execute(),
            () -> repo.setSalientSentences(TENANT, doc, "[\"followed\"]"));

        assertThat(updated).as("the write followed the re-homed row").isEqualTo(1);
        var row = asSuperuser(ctx -> ctx.select(DOCUMENT_ASPECTS.COLLECTION, DOCUMENT_ASPECTS.COLLECTION_ID, DOCUMENT_ASPECTS.SALIENT_SENTENCES)
            .from(DOCUMENT_ASPECTS).where(DOCUMENT_ASPECTS.TENANT_ID.eq(TENANT).and(DOCUMENT_ASPECTS.DOC_ID.eq(doc))).fetchOne());
        assertThat(row.get(DOCUMENT_ASPECTS.COLLECTION)).as("the row is where the verb put it").isEqualTo(to);
        assertThat(row.get(DOCUMENT_ASPECTS.COLLECTION_ID)).as("under its current id").isEqualTo(toId);
        assertThat(row.get(DOCUMENT_ASPECTS.SALIENT_SENTENCES).data()).contains("followed");
    }

    // ── deleteHighlight (document_highlights, by doc_id) ────────────────────────

    @Timeout(180)
    @Test
    void deleteHighlight_meetingAVerbHoldingTheRow_waits_thenDeletes() throws Exception {
        String coll = "race-hl-held-" + (n++);
        register(coll);
        String doc = "race-hl-doc-" + (n++);
        seedHighlight(coll, doc);

        boolean deleted = whileAVerbHolds(coll, v -> { }, () -> repo.deleteHighlight(TENANT, doc));

        assertThat(deleted).as("the delete landed once the verb let go").isTrue();
        int left = asSuperuser(ctx -> ctx.fetchCount(DOCUMENT_HIGHLIGHTS,
            DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(TENANT).and(DOCUMENT_HIGHLIGHTS.DOC_ID.eq(doc))));
        assertThat(left).isZero();
    }

    @Timeout(180)
    @Test
    void deleteHighlight_whenTheVerbRehomesTheRowWhileItWaits_followsTheRowAndHoldsTheNewCollection() throws Exception {
        String from = "race-hl-from-" + (n++);
        String to = "race-hl-to-" + (n++);
        register(from);
        long toId = register(to);
        String doc = "race-hl-moved-" + (n++);
        seedHighlight(from, doc);

        boolean deleted = whileAVerbHolds(from,
            v -> v.update(DOCUMENT_HIGHLIGHTS).set(DOCUMENT_HIGHLIGHTS.COLLECTION, to).set(DOCUMENT_HIGHLIGHTS.COLLECTION_ID, toId)
                .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(TENANT).and(DOCUMENT_HIGHLIGHTS.DOC_ID.eq(doc))).execute(),
            () -> repo.deleteHighlight(TENANT, doc));

        assertThat(deleted).as("the delete followed the re-homed row").isTrue();
        int left = asSuperuser(ctx -> ctx.fetchCount(DOCUMENT_HIGHLIGHTS,
            DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(TENANT).and(DOCUMENT_HIGHLIGHTS.DOC_ID.eq(doc))));
        assertThat(left).isZero();
    }

    // ── a row stamped to a retired collection: the routes answer what they answered before the read existed ──

    @Timeout(180)
    @Test
    void setSalientSentences_ofARowStampedToASupersededCollection_stillWrites_noRefusal() {
        String retired = "race-asp-retired-" + (n++);
        String successor = "race-asp-succ-" + (n++);
        register(retired);
        long successorId = register(successor);
        String doc = "race-asp-retired-doc-" + (n++);
        seedAspect(retired, doc);
        supersede(retired, successor, successorId);

        int updated = repo.setSalientSentences(TENANT, doc, "[\"retired still writes\"]");

        assertThat(updated).as("a supersede re-homes no child: the write lands, with no 409 for the retired id").isEqualTo(1);
        var data = asSuperuser(ctx -> ctx.select(DOCUMENT_ASPECTS.SALIENT_SENTENCES).from(DOCUMENT_ASPECTS)
            .where(DOCUMENT_ASPECTS.TENANT_ID.eq(TENANT).and(DOCUMENT_ASPECTS.DOC_ID.eq(doc))).fetchOne(DOCUMENT_ASPECTS.SALIENT_SENTENCES));
        assertThat(data.data()).contains("retired still writes");
    }

    @Timeout(180)
    @Test
    void deleteHighlight_ofARowStampedToASupersededCollection_stillDeletes_noRefusal() {
        String retired = "race-hl-retired-" + (n++);
        String successor = "race-hl-succ-" + (n++);
        register(retired);
        long successorId = register(successor);
        String doc = "race-hl-retired-doc-" + (n++);
        seedHighlight(retired, doc);
        supersede(retired, successor, successorId);

        boolean deleted = repo.deleteHighlight(TENANT, doc);

        assertThat(deleted).as("no 409 for the retired id: the delete lands").isTrue();
        int left = asSuperuser(ctx -> ctx.fetchCount(DOCUMENT_HIGHLIGHTS,
            DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(TENANT).and(DOCUMENT_HIGHLIGHTS.DOC_ID.eq(doc))));
        assertThat(left).isZero();
    }
}
