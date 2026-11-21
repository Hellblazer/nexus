// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.PgContainerHelper;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-254 aspect-queue race (found by the P5 minimum viable validation, GH #1571): an aspect extraction for a document
 * T is queued, then a document move absorbs T into S (T is trashed, its aspect row at the new path is re-keyed to S),
 * then the extraction completes. The late {@link AspectRepository#upsertAspect} carries T's tumbler, matches S's row by
 * (tenant, collection, source_path) and used to set {@code doc_id} back to the trashed T, so
 * {@code getAspectByDocId(S)} found nothing.
 *
 * <p>Behaviour pinned here: an upsert whose {@code doc_id} names a trashed document or an alias row never changes the
 * {@code doc_id} of the row it lands on; the aspect FIELDS of the late write still land (the absorbed T and S are the
 * same file at the same path, and the move itself keeps the newer extraction when T's row is the newer one). Real
 * Postgres.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class AspectUpsertAbsorbedDocumentTest {

    private static final String SVC_ROLE = "svc_aspect_absorbed_test";
    private static final String SVC_PASS = "svc_aspect_absorbed_pass";
    private static final String COLL = "knowledge__aspectabsorbed__bge-base-en-v15-768__v1";
    private static final String OLD_URI = "file:///old/a.pdf";
    private static final String NEW_URI = "file:///new/a.pdf";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    CatalogRepository repo;
    CatalogMoves moves;
    AspectRepository aspects;
    MoveTestDb db;
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
        config.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(config);
        var scope = new TenantScope(svcDs);
        repo = new CatalogRepository(scope);
        moves = new CatalogMoves(repo);
        aspects = new AspectRepository(scope);
        db = new MoveTestDb(pg);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── fixtures ────────────────────────────────────────────────────────────

    private String tenant() {
        return "aspect-absorbed-" + (++tenantSeq);
    }

    private void curatorOwner(String t) {
        Map<String, Object> o = new LinkedHashMap<>();
        o.put("tumbler_prefix", "1.1");
        o.put("name", "pdfs");
        o.put("owner_type", "curator");
        repo.upsertOwner(t, o);
    }

    private String doc(String t, String title, String sourceUri, String filePath) {
        Map<String, Object> f = new LinkedHashMap<>();
        f.put("title", title);
        f.put("file_path", filePath);
        f.put("content_type", "paper");
        f.put("corpus", "papers");
        f.put("physical_collection", COLL);
        f.put("source_uri", sourceUri);
        return repo.registerDocument(t, "1.1", f);
    }

    private long upsert(String t, String docId, String sourcePath, String sourceUri, String text) throws Exception {
        db.registerCollection(t, COLL);
        Map<String, Object> b = new LinkedHashMap<>();
        b.put("collection", COLL);
        b.put("source_path", sourcePath);
        b.put("source_uri", sourceUri);
        b.put("doc_id", docId);
        b.put("problem_formulation", text);
        b.put("confidence", 0.9);
        b.put("extracted_at", OffsetDateTime.now().toString());
        b.put("model_version", "v1");
        b.put("extractor_name", "test");
        return aspects.upsertAspect(t, b);
    }

    private Map<String, Object> theRow(String t) throws Exception {
        List<Map<String, Object>> rows = db.aspects(t);
        assertThat(rows).as("one aspect row").hasSize(1);
        return rows.get(0);
    }

    // ── the race ────────────────────────────────────────────────────────────

    @Test
    void aLateUpsertForTheAbsorbedDocumentKeepsSsDocIdWhenSsRowWasRewritten() throws Exception {
        // The MVV state: T's extraction is in flight (T has no aspect row yet), S's row is rewritten to the new path.
        String t = tenant();
        curatorOwner(t);
        String s = doc(t, "a", OLD_URI, "/old/a.pdf");
        String tt = doc(t, "a-new", NEW_URI, "/new/a.pdf");
        upsert(t, s, "/old/a.pdf", OLD_URI, "s extraction");
        moves.moveDocument(t, s, NEW_URI, "/new/a.pdf", false);
        assertThat(db.doc(t, tt).get("deleted_at")).as("T was absorbed and trashed").isNotNull();

        upsert(t, tt, "/new/a.pdf", NEW_URI, "t extraction, completing late");

        assertThat(theRow(t)).containsEntry("doc_id", s).containsEntry("source_path", "/new/a.pdf");
        assertThat(aspects.getAspectByDocId(t, s)).as("get_by_doc_id(S) still finds the row").isPresent();
        assertThat(aspects.getAspectByDocId(t, tt)).as("nothing is attributed to the trashed T").isEmpty();
    }

    @Test
    void aLateUpsertStillUpdatesTheAspectFieldsOfSsRow() throws Exception {
        // The extraction read the file at the new path, which S now owns: its fields are S's newest, so they land.
        String t = tenant();
        curatorOwner(t);
        String s = doc(t, "a", OLD_URI, "/old/a.pdf");
        String tt = doc(t, "a-new", NEW_URI, "/new/a.pdf");
        upsert(t, s, "/old/a.pdf", OLD_URI, "s extraction");
        moves.moveDocument(t, s, NEW_URI, "/new/a.pdf", false);

        upsert(t, tt, "/new/a.pdf", NEW_URI, "t extraction, completing late");

        assertThat(theRow(t)).containsEntry("doc_id", s).containsEntry("source_uri", NEW_URI)
            .containsEntry("problem_formulation", "t extraction, completing late");
    }

    @Test
    void aLateUpsertKeepsSsDocIdWhenTsRowWasRekeyedToS() throws Exception {
        // T's first extraction drained before the move (re-keyed to S); a second one for T completes after it.
        String t = tenant();
        curatorOwner(t);
        String s = doc(t, "a", OLD_URI, "/old/a.pdf");
        String tt = doc(t, "a-new", NEW_URI, "/new/a.pdf");
        upsert(t, s, "/old/a.pdf", OLD_URI, "s extraction");
        upsert(t, tt, "/new/a.pdf", NEW_URI, "t first extraction");
        moves.moveDocument(t, s, NEW_URI, "/new/a.pdf", false);
        assertThat(theRow(t)).containsEntry("doc_id", s);

        upsert(t, tt, "/new/a.pdf", NEW_URI, "t second extraction");

        assertThat(theRow(t)).containsEntry("doc_id", s).containsEntry("problem_formulation", "t second extraction");
    }

    @Test
    void anUpsertForATrashedDocumentDoesNotTakeOverAnExistingRow() throws Exception {
        // Not only a move: any trashed document's late write must leave another document's row attributed as it is.
        String t = tenant();
        curatorOwner(t);
        String live = doc(t, "live", "file:///p/live.pdf", "/p/live.pdf");
        String gone = doc(t, "gone", "file:///p/gone.pdf", "/p/gone.pdf");
        upsert(t, live, "/p/shared.pdf", "file:///p/live.pdf", "live extraction");
        repo.deleteDocument(t, gone);

        upsert(t, gone, "/p/shared.pdf", "file:///p/gone.pdf", "a trashed document's late write");

        assertThat(theRow(t)).containsEntry("doc_id", live);
    }

    @Test
    void anUpsertForAnAliasRowDoesNotTakeOverAnExistingRow() throws Exception {
        String t = tenant();
        curatorOwner(t);
        String canonical = doc(t, "canonical", "file:///p/c.pdf", "/p/c.pdf");
        String alias = doc(t, "alias", "file:///p/dup.pdf", "/p/dup.pdf");
        db.setAlias(t, alias, canonical);
        upsert(t, canonical, "/p/shared.pdf", "file:///p/c.pdf", "canonical extraction");

        upsert(t, alias, "/p/shared.pdf", "file:///p/dup.pdf", "the duplicate's extraction");

        assertThat(theRow(t)).containsEntry("doc_id", canonical);
    }

    @Test
    void aLiveDocumentsUpsertStillSetsDocIdAsBefore() throws Exception {
        // The guard is narrow: two LIVE documents keep today's last-writer attribution.
        String t = tenant();
        curatorOwner(t);
        String first = doc(t, "first", "file:///p/1.pdf", "/p/1.pdf");
        String second = doc(t, "second", "file:///p/2.pdf", "/p/2.pdf");
        upsert(t, first, "/p/shared.pdf", "file:///p/1.pdf", "first");

        upsert(t, second, "/p/shared.pdf", "file:///p/2.pdf", "second");

        assertThat(theRow(t)).containsEntry("doc_id", second).containsEntry("problem_formulation", "second");
    }

    @Test
    void aFirstWriteForALiveDocumentInsertsItsRow() throws Exception {
        String t = tenant();
        curatorOwner(t);
        String s = doc(t, "a", OLD_URI, "/old/a.pdf");

        assertThat(upsert(t, s, "/old/a.pdf", OLD_URI, "first extraction")).isPositive();

        assertThat(theRow(t)).containsEntry("doc_id", s);
    }

    // ── the window ──────────────────────────────────────────────────────────

    @Test
    void anUpsertThatStartsWhileTheMoveIsUncommittedWaitsForItAndThenKeepsSsDocId() throws Exception {
        // The completion arrives while the move's transaction holds T's catalog row (T trashed, not yet committed): a
        // catalog read that did not wait would still see T live and write doc_id T once the move commits.
        String t = tenant();
        curatorOwner(t);
        String s = doc(t, "a", OLD_URI, "/old/a.pdf");
        String tt = doc(t, "a-new", NEW_URI, "/new/a.pdf");
        upsert(t, s, "/old/a.pdf", OLD_URI, "s extraction");

        var late = new java.util.concurrent.atomic.AtomicReference<CompletableFuture<Long>>();
        boolean[] finishedInsideTheMove = new boolean[1];
        CatalogMoves.afterWritesHookForTests = () -> {
            late.set(CompletableFuture.supplyAsync(() -> {
                try {
                    return upsert(t, tt, "/new/a.pdf", NEW_URI, "t extraction, completing mid-move");
                } catch (Exception e) {
                    throw new IllegalStateException(e);
                }
            }));
            try {
                late.get().get(1500, TimeUnit.MILLISECONDS);
                finishedInsideTheMove[0] = true;     // it did not wait for the move's locks
            } catch (java.util.concurrent.TimeoutException expected) {
                // blocked on the uncommitted move, as it must be
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        };
        try {
            moves.moveDocument(t, s, NEW_URI, "/new/a.pdf", false);
        } finally {
            CatalogMoves.afterWritesHookForTests = null;
        }

        late.get().get(30, TimeUnit.SECONDS);
        assertThat(finishedInsideTheMove[0]).as("the late upsert waited for the move").isFalse();
        assertThat(theRow(t)).containsEntry("doc_id", s)
            .containsEntry("problem_formulation", "t extraction, completing mid-move");
    }
}
