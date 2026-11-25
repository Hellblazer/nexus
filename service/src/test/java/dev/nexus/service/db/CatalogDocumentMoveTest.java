// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-254 plan item 254-P1-S3: {@code POST /v1/catalog/documents/move}'s transaction, {@link CatalogMoves#moveDocument}.
 * Real Postgres. A curator document S is re-pointed at a new path P; when a live row T already holds
 * {@code file://P} (the document was indexed again at its new path before the move) T is absorbed into S: S keeps
 * its tumbler, links, aspects and highlights, T is cleared and trashed.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogDocumentMoveTest {

    private static final String SVC_ROLE = "svc_catalog_doc_move_test";
    private static final String SVC_PASS = "svc_doc_move_pass";
    /** A routable (bge-768) collection, so a manifest row can be seeded against a real chunk. */
    private static final String COLL = "knowledge__docmove__bge-base-en-v15-768__v1";
    private static final String MODEL = "bge-base-en-v15-768";

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
        config.setMaximumPoolSize(4);
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

    /** A fresh tenant per test, so a test reads and snapshots only its own rows. */
    private String tenant() {
        return "doc-move-" + (++tenantSeq);
    }

    private void owner(String t, String prefix, String name, String type) {
        Map<String, Object> o = new LinkedHashMap<>();
        o.put("tumbler_prefix", prefix);
        o.put("name", name);
        o.put("owner_type", type);
        if ("repo".equals(type)) {
            o.put("repo_hash", "hash-" + name);
            o.put("repo_root", "/repo/" + name);
        }
        repo.upsertOwner(t, o);
    }

    private String doc(String t, String ownerPrefix, String title, String sourceUri, String filePath) throws Exception {
        registerCollection(t, COLL);   // RDR-229 P3.1: a document names its collection by registry id
        Map<String, Object> f = new LinkedHashMap<>();
        f.put("title", title);
        f.put("file_path", filePath);
        f.put("content_type", "paper");
        f.put("corpus", "papers");
        f.put("physical_collection", COLL);
        if (sourceUri != null) f.put("source_uri", sourceUri);
        return repo.registerDocument(t, ownerPrefix, f);
    }

    private void link(String t, String from, String to, String type) {
        Map<String, Object> l = new LinkedHashMap<>();
        l.put("from_tumbler", from);
        l.put("to_tumbler", to);
        l.put("link_type", type);
        l.put("created_by", "test");
        repo.upsertLink(t, l);
    }

    private void registerCollection(String t, String collection) throws Exception { db.registerCollection(t, collection); }

    private void aspect(String t, String docId, String collection, String sourcePath, String sourceUri, String text) throws Exception {
        registerCollection(t, collection);
        Map<String, Object> b = new LinkedHashMap<>();
        b.put("collection", collection);
        b.put("source_path", sourcePath);
        b.put("source_uri", sourceUri);
        b.put("doc_id", docId);
        b.put("problem_formulation", text);
        b.put("confidence", 0.9);
        b.put("extracted_at", OffsetDateTime.now().toString());
        b.put("model_version", "v1");
        b.put("extractor_name", "test");
        aspects.upsertAspect(t, b);
    }

    private void highlight(String t, String docId, String collection, String sourceUri, String text) throws Exception {
        registerCollection(t, collection);
        Map<String, Object> b = new LinkedHashMap<>();
        b.put("doc_id", docId);
        b.put("collection", collection);
        b.put("source_uri", sourceUri);
        b.put("highlights_md", text);
        b.put("ingested_at", OffsetDateTime.now().toString());
        aspects.upsertHighlight(t, b);
    }

    private Map<String, Object> row(String t, String tumbler) throws Exception { return db.doc(t, tumbler); }

    private List<Map<String, Object>> aspectRows(String t) throws Exception { return db.aspects(t); }

    private List<Map<String, Object>> highlightRows(String t) throws Exception { return db.highlights(t); }

    private List<Map<String, Object>> linkRows(String t) throws Exception { return db.links(t); }

    /** Every row the move can touch, as text: equal before and after means nothing was written. */
    private String snapshot(String t) throws Exception { return db.snapshot(t); }

    private static Map<String, Object> countsOf(Map<String, Object> report) {
        Map<String, Object> m = new LinkedHashMap<>(report);
        m.remove("dry_run");
        return m;
    }

    private static List<Object> absorbed(Map<String, Object> report) {
        return new java.util.ArrayList<>((List<?>) report.get("absorbed_tumblers"));
    }

    private Map<String, Object> move(String t, String tumbler, boolean dryRun) {
        return moves.moveDocument(t, tumbler, NEW_URI, "/new/a.pdf", dryRun).toMap();
    }

    private static byte[] sha256(String text) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(text.getBytes(StandardCharsets.UTF_8));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /** One chunk of {@link #COLL} and a manifest row of {@code docId} pointing at it. */
    private void manifest(String t, String docId, String text) throws Exception {
        try (Connection c = pg.createConnection("")) {
            DSLContext dsl = DSL.using(c, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, t, COLL);
            byte[] chash = sha256(text);
            dsl.insertInto(CHUNKS)
                .set(CHUNKS.TENANT_ID, t).set(CHUNKS.COLLECTION, COLL)
                .set(CHUNKS.COLLECTION_ID, CollectionIds.idOf(t, COLL)).set(CHUNKS.CHASH, chash)
                .set(CHUNKS.EMBEDDING_MODEL, MODEL).set(CHUNKS.CHUNK_TEXT, text)
                .set(CHUNKS.EMBEDDING_768, Vector.of(new float[768]))
                .execute();
            dsl.insertInto(CATALOG_DOCUMENT_CHUNKS)
                .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, t).set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, docId)
                .set(CATALOG_DOCUMENT_CHUNKS.POSITION, 0).set(CATALOG_DOCUMENT_CHUNKS.CHASH, chash)
                .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, COLL)
                .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, CollectionIds.idOf(t, COLL)).set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, MODEL)
                .execute();
        }
    }

    private int manifestCount(String t, String docId) throws Exception { return db.manifestRows(t, docId); }

    // ── plain re-point ──────────────────────────────────────────────────────

    @Test
    void plainRepointSetsSourceUriAndFilePath() throws Exception {
        String t = tenant();
        owner(t, "1.1", "pdfs", "curator");
        String s = doc(t, "1.1", "a", OLD_URI, "/old/a.pdf");

        var report = move(t, s, false);

        var r = row(t, s);
        assertThat(r.get("source_uri")).isEqualTo(NEW_URI);
        assertThat(r.get("file_path")).isEqualTo("/new/a.pdf");
        assertThat(r.get("deleted_at")).isNull();
        assertThat(report).containsEntry("dry_run", false).containsEntry("repointed", 1)
            .containsEntry("absorbed", 0).containsEntry("trashed", 0).containsEntry("skipped", 0);
        assertThat(absorbed(report)).isEmpty();
    }

    @Test
    void legacyRelativeFilePathBecomesAbsolute() throws Exception {
        String t = tenant();
        owner(t, "1.1", "pdfs", "curator");
        // A legacy curator row: a relative file_path and no source_uri (the owner has no root to derive one from).
        String s = doc(t, "1.1", "a", null, "papers/a.pdf");
        assertThat(row(t, s).get("source_uri")).isEqualTo("");
        assertThat(row(t, s).get("file_path")).isEqualTo("papers/a.pdf");

        move(t, s, false);

        assertThat(row(t, s).get("file_path")).isEqualTo("/new/a.pdf");
        assertThat(row(t, s).get("source_uri")).isEqualTo(NEW_URI);
    }

    // ── absorb ──────────────────────────────────────────────────────────────

    @Test
    void absorbRemapsLinksOntoS() throws Exception {
        String t = tenant();
        owner(t, "1.1", "pdfs", "curator");
        String s = doc(t, "1.1", "a", OLD_URI, "/old/a.pdf");
        String x = doc(t, "1.1", "x", "file:///other/x.pdf", "/other/x.pdf");
        String tt = doc(t, "1.1", "a-new", NEW_URI, "/new/a.pdf");
        link(t, tt, x, "cites");          // T -> X: becomes S -> X
        link(t, x, tt, "relates");        // X -> T: becomes X -> S
        link(t, s, tt, "same-as");        // S -> T: a self-link after the remap, dropped

        var report = move(t, s, false);

        assertThat(linkRows(t)).containsExactlyInAnyOrder(
            Map.of("from_tumbler", s, "to_tumbler", x, "link_type", "cites"),
            Map.of("from_tumbler", x, "to_tumbler", s, "link_type", "relates"));
        assertThat(report).containsEntry("links_remapped", 2).containsEntry("links_dropped", 1);
    }

    @Test
    void absorbRepointsAliasOfRowsNamingT() throws Exception {
        String t = tenant();
        owner(t, "1.1", "pdfs", "curator");
        String s = doc(t, "1.1", "a", OLD_URI, "/old/a.pdf");
        String tt = doc(t, "1.1", "a-new", NEW_URI, "/new/a.pdf");
        String y = doc(t, "1.1", "y", "file:///y.pdf", "/y.pdf");
        db.setAlias(t, y, tt);

        move(t, s, false);

        assertThat(row(t, y).get("alias_of")).isEqualTo(s);
    }

    @Test
    void absorbClearsSelfAliasWhenSAliasedT() throws Exception {
        String t = tenant();
        owner(t, "1.1", "pdfs", "curator");
        String s = doc(t, "1.1", "a", OLD_URI, "/old/a.pdf");
        String tt = doc(t, "1.1", "a-new", NEW_URI, "/new/a.pdf");
        db.setAlias(t, s, tt);

        move(t, s, false);

        assertThat(row(t, s).get("alias_of")).as("S ends with no alias_of").isEqualTo("");
        assertThat(row(t, s).get("source_uri")).isEqualTo(NEW_URI);
    }

    @Test
    void refusesWhenSIsItselfAnAliasOfAnotherRow() throws Exception {
        // RDR-254 review M5: an alias row is not given a live URI of its own (this was the alias-cycle shape, S -> X -> T).
        String t = tenant();
        owner(t, "1.1", "pdfs", "curator");
        String s = doc(t, "1.1", "a", OLD_URI, "/old/a.pdf");
        String tt = doc(t, "1.1", "a-new", NEW_URI, "/new/a.pdf");
        String x = doc(t, "1.1", "x", "file:///x.pdf", "/x.pdf");
        db.setAlias(t, s, x);
        db.setAlias(t, x, tt);
        String before = snapshot(t);

        assertThatThrownBy(() -> move(t, s, false))
            .isInstanceOf(CatalogRepository.MergeRefused.class)
            .hasMessageContaining(s).hasMessageContaining(x).hasMessageContaining("alias");
        assertThat(snapshot(t)).as("refused before any write").isEqualTo(before);
        assertThatThrownBy(() -> move(t, s, true)).isInstanceOf(CatalogRepository.MergeRefused.class);
    }

    @Test
    void absorbRaisesSensitivityFromT() throws Exception {
        String t = tenant();
        owner(t, "1.1", "pdfs", "curator");
        String s = doc(t, "1.1", "a", OLD_URI, "/old/a.pdf");
        String tt = doc(t, "1.1", "a-new", NEW_URI, "/new/a.pdf");
        repo.setSensitivity(t, List.of(tt), true, "test", "test");
        assertThat(row(t, s).get("sensitive")).isEqualTo(false);

        move(t, s, false);

        assertThat(row(t, s).get("sensitive")).as("S takes T's flag").isEqualTo(true);
        assertThat(db.sensitivityAudits(t, s)).as("the raise is audited").isEqualTo(1);
    }

    @Test
    void absorbClearsAndTrashesT() throws Exception {
        String t = tenant();
        owner(t, "1.1", "pdfs", "curator");
        String s = doc(t, "1.1", "a", OLD_URI, "/old/a.pdf");
        String tt = doc(t, "1.1", "a-new", NEW_URI, "/new/a.pdf");
        manifest(t, tt, "the text of the new copy");

        var report = move(t, s, false);

        var r = row(t, tt);
        assertThat(r.get("source_uri")).isEqualTo("");
        assertThat(r.get("deleted_at")).isNotNull();
        assertThat(manifestCount(t, tt)).as("T's manifest stays on the trashed row").isEqualTo(1);
        assertThat(row(t, s).get("source_uri")).isEqualTo(NEW_URI);
        assertThat(report).containsEntry("absorbed", 1).containsEntry("repointed", 1);
    }

    // ── aspects ─────────────────────────────────────────────────────────────

    @Test
    void aspectRekeyNewerRowWithoutUniqueViolation() throws Exception {
        String t = tenant();
        owner(t, "1.1", "pdfs", "curator");
        String s = doc(t, "1.1", "a", OLD_URI, "/old/a.pdf");
        String tt = doc(t, "1.1", "a-new", NEW_URI, "/new/a.pdf");
        aspect(t, s, COLL, "/old/a.pdf", OLD_URI, "older extraction");
        aspect(t, tt, COLL, "/new/a.pdf", NEW_URI, "newer extraction");

        var report = move(t, s, false);

        var rows = aspectRows(t);
        assertThat(rows).hasSize(1);
        assertThat(rows.get(0)).containsEntry("doc_id", s).containsEntry("collection", COLL)
            .containsEntry("source_path", "/new/a.pdf").containsEntry("source_uri", NEW_URI)
            .containsEntry("problem_formulation", "newer extraction");
        assertThat(report).containsEntry("aspect_rows_rewritten", 1).containsEntry("aspect_rows_deleted", 1);
    }

    @Test
    void aspectRewriteWhenTHasNone() throws Exception {
        String t = tenant();
        owner(t, "1.1", "pdfs", "curator");
        String s = doc(t, "1.1", "a", OLD_URI, "/old/a.pdf");
        doc(t, "1.1", "a-new", NEW_URI, "/new/a.pdf");
        aspect(t, s, COLL, "/old/a.pdf", OLD_URI, "kept extraction");

        var report = move(t, s, false);

        var rows = aspectRows(t);
        assertThat(rows).hasSize(1);
        assertThat(rows.get(0)).containsEntry("doc_id", s).containsEntry("source_path", "/new/a.pdf")
            .containsEntry("source_uri", NEW_URI).containsEntry("problem_formulation", "kept extraction");
        assertThat(report).containsEntry("aspect_rows_rewritten", 1).containsEntry("aspect_rows_deleted", 0);
    }

    @Test
    void aspectRowOfAnotherDocumentAtTheNewPathRefusesTheMove() throws Exception {
        String t = tenant();
        owner(t, "1.1", "pdfs", "curator");
        String s = doc(t, "1.1", "a", OLD_URI, "/old/a.pdf");
        String other = doc(t, "1.1", "other", "file:///other.pdf", "/other.pdf");
        aspect(t, s, COLL, "/old/a.pdf", OLD_URI, "s extraction");
        aspect(t, other, COLL, "/new/a.pdf", "file:///other.pdf", "another document's row");
        String before = snapshot(t);

        assertThatThrownBy(() -> move(t, s, false))
            .isInstanceOf(CatalogRepository.MergeRefused.class).hasMessageContaining(other)
            .hasMessageContaining("nx enrich delete '" + COLL + "' '/new/a.pdf'");
        assertThat(snapshot(t)).isEqualTo(before);
    }

    @Test
    void aStaleAspectRowOfATrashedDocumentAtTheNewPathIsSupersededAndDeleted() throws Exception {
        // RDR-254 review B6: the row's document is trashed, so the path now belongs to S; the row goes inside the move.
        String t = tenant();
        owner(t, "1.1", "pdfs", "curator");
        String s = doc(t, "1.1", "a", OLD_URI, "/old/a.pdf");
        String ghost = doc(t, "1.1", "ghost", "file:///ghost.pdf", "/ghost.pdf");
        aspect(t, s, COLL, "/old/a.pdf", OLD_URI, "s extraction");
        aspect(t, ghost, COLL, "/new/a.pdf", "file:///ghost.pdf", "a trashed document's row");
        repo.deleteDocument(t, ghost);

        var report = move(t, s, false);

        var rows = aspectRows(t);
        assertThat(rows).hasSize(1);
        assertThat(rows.get(0)).containsEntry("doc_id", s).containsEntry("source_path", "/new/a.pdf")
            .containsEntry("problem_formulation", "s extraction");
        assertThat(report).containsEntry("aspect_rows_deleted", 1).containsEntry("aspect_rows_rewritten", 1);
    }

    @Test
    void aRefusalAfterTheAbsorbWroteRollsEverythingBack() throws Exception {
        // RDR-254 review B4: the absorb has written (links, alias, trash) when the aspect step refuses; every row must
        // be as it was. With auto-commit this test is red.
        String t = tenant();
        owner(t, "1.1", "pdfs", "curator");
        String s = doc(t, "1.1", "a", OLD_URI, "/old/a.pdf");
        String tt = doc(t, "1.1", "a-new", NEW_URI, "/new/a.pdf");
        String x = doc(t, "1.1", "x", "file:///x.pdf", "/x.pdf");
        String other = doc(t, "1.1", "other", "file:///other.pdf", "/other.pdf");
        link(t, tt, x, "cites");
        db.setAlias(t, x, tt);
        highlight(t, tt, COLL, NEW_URI, "new marks");
        aspect(t, s, COLL, "/old/a.pdf", OLD_URI, "s extraction");
        aspect(t, other, COLL, "/new/a.pdf", "file:///other.pdf", "another document's row");
        String before = snapshot(t);

        assertThatThrownBy(() -> move(t, s, false)).isInstanceOf(CatalogRepository.MergeRefused.class);

        assertThat(snapshot(t)).as("the absorb's writes were rolled back").isEqualTo(before);
        assertThat(row(t, tt).get("deleted_at")).isNull();
        assertThat(row(t, x).get("alias_of")).isEqualTo(tt);
    }

    @Test
    void aFailureAfterTheLastWriteRollsEverythingBack() throws Exception {
        String t = tenant();
        owner(t, "1.1", "pdfs", "curator");
        String s = doc(t, "1.1", "a", OLD_URI, "/old/a.pdf");
        doc(t, "1.1", "a-new", NEW_URI, "/new/a.pdf");
        aspect(t, s, COLL, "/old/a.pdf", OLD_URI, "s extraction");
        String before = snapshot(t);
        CatalogMoves.afterWritesHookForTests = () -> { throw new IllegalStateException("injected after the last write"); };
        try {
            assertThatThrownBy(() -> move(t, s, false)).hasMessageContaining("injected");
        } finally {
            CatalogMoves.afterWritesHookForTests = null;
        }
        assertThat(snapshot(t)).isEqualTo(before);
    }

    // ── highlights ──────────────────────────────────────────────────────────

    @Test
    void highlightsMoveFromTWhenSHasNone() throws Exception {
        String t = tenant();
        owner(t, "1.1", "pdfs", "curator");
        String s = doc(t, "1.1", "a", OLD_URI, "/old/a.pdf");
        String tt = doc(t, "1.1", "a-new", NEW_URI, "/new/a.pdf");
        highlight(t, tt, COLL, NEW_URI, "marks made on the new copy");

        var report = move(t, s, false);

        var rows = highlightRows(t);
        assertThat(rows).hasSize(1);
        assertThat(rows.get(0)).containsEntry("doc_id", s).containsEntry("collection", COLL)
            .containsEntry("source_uri", NEW_URI).containsEntry("highlights_md", "marks made on the new copy");
        assertThat(report).containsEntry("highlight_rows_rewritten", 1);
    }

    @Test
    void highlightsOfTDeletedWhenSHasOne() throws Exception {
        String t = tenant();
        owner(t, "1.1", "pdfs", "curator");
        String s = doc(t, "1.1", "a", OLD_URI, "/old/a.pdf");
        String tt = doc(t, "1.1", "a-new", NEW_URI, "/new/a.pdf");
        highlight(t, s, COLL, OLD_URI, "marks made on the old copy");
        highlight(t, tt, COLL, NEW_URI, "marks made on the new copy");

        var report = move(t, s, false);

        var rows = highlightRows(t);
        assertThat(rows).hasSize(1);
        assertThat(rows.get(0)).containsEntry("doc_id", s).containsEntry("source_uri", NEW_URI)
            .containsEntry("highlights_md", "marks made on the old copy");
        assertThat(report).containsEntry("highlight_rows_deleted", 1).containsEntry("highlight_rows_rewritten", 1);
    }

    @Test
    void highlightsKeepTheirCollectionWhenSHasNoPhysicalCollection() throws Exception {
        // RDR-254 review M1: highlights.collection is NOT NULL, so a NULL physical_collection must not overwrite it.
        String t = tenant();
        owner(t, "1.1", "pdfs", "curator");
        Map<String, Object> f = new LinkedHashMap<>();
        f.put("title", "a");
        f.put("file_path", "/old/a.pdf");
        f.put("source_uri", OLD_URI);
        String s = repo.registerDocument(t, "1.1", f);
        String tt = doc(t, "1.1", "a-new", NEW_URI, "/new/a.pdf");
        highlight(t, tt, COLL, NEW_URI, "marks made on the new copy");

        move(t, s, false);

        var rows = highlightRows(t);
        assertThat(rows).hasSize(1);
        assertThat(rows.get(0)).containsEntry("doc_id", s).containsEntry("collection", COLL);
    }

    // ── refusals ────────────────────────────────────────────────────────────

    @Test
    void refusesMissingTumbler() throws Exception {
        String t = tenant();
        owner(t, "1.1", "pdfs", "curator");
        assertThatThrownBy(() -> move(t, "1.1.99", false))
            .isInstanceOf(CatalogRepository.MergeRefused.class).hasMessageContaining("1.1.99");
    }

    @Test
    void refusesTrashedTumbler() throws Exception {
        String t = tenant();
        owner(t, "1.1", "pdfs", "curator");
        String s = doc(t, "1.1", "a", OLD_URI, "/old/a.pdf");
        repo.deleteDocument(t, s);
        String before = snapshot(t);
        assertThatThrownBy(() -> move(t, s, false))
            .isInstanceOf(CatalogRepository.MergeRefused.class).hasMessageContaining(s);
        assertThat(snapshot(t)).isEqualTo(before);
    }

    @Test
    void refusesRepoOwnedS() throws Exception {
        String t = tenant();
        owner(t, "1.1", "proj", "repo");
        String s = doc(t, "1.1", "a", null, "docs/a.md");
        String before = snapshot(t);
        assertThatThrownBy(() -> move(t, s, false))
            .isInstanceOf(CatalogRepository.MergeRefused.class).hasMessageContaining("repo");
        assertThat(snapshot(t)).isEqualTo(before);
    }

    @Test
    void refusesTUnderAnotherOwner() throws Exception {
        String t = tenant();
        owner(t, "1.1", "pdfs", "curator");
        owner(t, "1.2", "other-pdfs", "curator");
        String s = doc(t, "1.1", "a", OLD_URI, "/old/a.pdf");
        String tt = doc(t, "1.2", "a-elsewhere", NEW_URI, "/new/a.pdf");
        String before = snapshot(t);

        assertThatThrownBy(() -> move(t, s, false))
            .isInstanceOf(CatalogRepository.MergeRefused.class).hasMessageContaining(tt).hasMessageContaining("owner");
        assertThat(snapshot(t)).isEqualTo(before);
        assertThat(row(t, tt).get("deleted_at")).isNull();
    }

    @Test
    void refusesARelativePathAndAUriThatIsNotAFileUri() {
        String t = tenant();
        assertThatThrownBy(() -> moves.moveDocument(t, "1.1.1", NEW_URI, "new/a.pdf", false))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("absolute");
        assertThatThrownBy(() -> moves.moveDocument(t, "1.1.1", "https://example.org/a.pdf", "/new/a.pdf", false))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("file://");
        assertThatThrownBy(() -> moves.moveDocument(t, "1.1.1", "/new/a.pdf", "/new/a.pdf", true))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void acceptsAWindowsDriveLetterPathAndAUncPathWhateverTheEngineHost() throws Exception {
        // RDR-254 confirmation: absoluteness is a shape check, not the engine host's Paths.get, so a Windows client's
        // path is accepted by a Linux engine (curator registration already accepts such documents).
        String t = tenant();
        owner(t, "1.1", "pdfs", "curator");
        String drive = doc(t, "1.1", "drive", "file:///C:/old/a.pdf", "C:\\old\\a.pdf");
        String unc = doc(t, "1.1", "unc", "file://server/old/b.pdf", "\\\\server\\old\\b.pdf");

        var r1 = moves.moveDocument(t, drive, "file:///C:/docs/a.pdf", "C:\\docs\\a.pdf", false).toMap();
        var r2 = moves.moveDocument(t, unc, "file://server/share/b.pdf", "\\\\server\\share\\b.pdf", false).toMap();

        assertThat(r1).containsEntry("repointed", 1);
        assertThat(r2).containsEntry("repointed", 1);
        assertThat(row(t, drive)).containsEntry("source_uri", "file:///C:/docs/a.pdf").containsEntry("file_path", "C:\\docs\\a.pdf");
        assertThat(row(t, unc)).containsEntry("source_uri", "file://server/share/b.pdf")
            .containsEntry("file_path", "\\\\server\\share\\b.pdf");
        // a forward-slash drive path too
        assertThat(moves.moveDocument(t, drive, "file:///C:/more/a.pdf", "C:/more/a.pdf", true).toMap()).containsEntry("dry_run", true);
    }

    @Test
    void stillRefusesADriveWithNoSeparatorAndARelativePath() {
        String t = tenant();
        assertThatThrownBy(() -> moves.moveDocument(t, "1.1.1", "file:///C:docs/a.pdf", "C:docs", false))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("absolute");
        assertThatThrownBy(() -> moves.moveDocument(t, "1.1.1", NEW_URI, "docs/a.pdf", false))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("absolute");
        assertThatThrownBy(() -> moves.moveDocument(t, "1.1.1", NEW_URI, "..\\a.pdf", false))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("absolute");
    }

    @Test
    void refusesBlankNewPath() {
        String t = tenant();
        assertThatThrownBy(() -> moves.moveDocument(t, "1.1.1", "", "/new/a.pdf", false))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> moves.moveDocument(t, "1.1.1", NEW_URI, " ", false))
            .isInstanceOf(IllegalArgumentException.class);
    }

    // ── report and dry run ──────────────────────────────────────────────────

    @Test
    void responseListsAbsorbedTumblers() throws Exception {
        String t = tenant();
        owner(t, "1.1", "pdfs", "curator");
        String s = doc(t, "1.1", "a", OLD_URI, "/old/a.pdf");
        String tt = doc(t, "1.1", "a-new", NEW_URI, "/new/a.pdf");

        var report = move(t, s, false);

        assertThat(absorbed(report)).containsExactly(tt);
    }

    @Test
    void dryRunWritesNothingAndReportsSameCountsAsWrite() throws Exception {
        String t = tenant();
        owner(t, "1.1", "pdfs", "curator");
        String s = doc(t, "1.1", "a", OLD_URI, "/old/a.pdf");
        String tt = doc(t, "1.1", "a-new", NEW_URI, "/new/a.pdf");
        String x = doc(t, "1.1", "x", "file:///other/x.pdf", "/other/x.pdf");
        link(t, tt, x, "cites");
        link(t, s, tt, "same-as");
        repo.setSensitivity(t, List.of(tt), true, "test", "test");
        aspect(t, s, COLL, "/old/a.pdf", OLD_URI, "older extraction");
        aspect(t, tt, COLL, "/new/a.pdf", NEW_URI, "newer extraction");
        highlight(t, s, COLL, OLD_URI, "old marks");
        highlight(t, tt, COLL, NEW_URI, "new marks");
        manifest(t, tt, "the text of the new copy, dry run");
        String before = snapshot(t);

        var dry = move(t, s, true);

        assertThat(snapshot(t)).as("a dry run leaves every row as it was").isEqualTo(before);
        assertThat(dry).containsEntry("dry_run", true);

        var written = move(t, s, false);

        assertThat(snapshot(t)).as("the write changes rows").isNotEqualTo(before);
        assertThat(written).containsEntry("dry_run", false);
        assertThat(countsOf(written)).isEqualTo(countsOf(dry));
        assertThat(written).containsEntry("absorbed", 1).containsEntry("repointed", 1)
            .containsEntry("links_remapped", 1).containsEntry("links_dropped", 1)
            .containsEntry("aspect_rows_rewritten", 1).containsEntry("aspect_rows_deleted", 1)
            .containsEntry("highlight_rows_deleted", 1).containsEntry("highlight_rows_rewritten", 1);
        assertThat(absorbed(written)).containsExactly(tt);
    }

    @Test
    void aDocumentAlreadyAtTheNewPathChangesNothing() throws Exception {
        String t = tenant();
        owner(t, "1.1", "pdfs", "curator");
        String s = doc(t, "1.1", "a", NEW_URI, "/new/a.pdf");
        String before = snapshot(t);

        var report = move(t, s, false);

        assertThat(snapshot(t)).isEqualTo(before);
        assertThat(report).containsEntry("repointed", 0).containsEntry("absorbed", 0);
    }

    @Test
    void theTrashedRowIsRestorableButComesBackWithoutItsUri() throws Exception {
        // Undo is partial by design (RDR-254 Undo): restoring an absorbed row brings back a live row with no source_uri.
        String t = tenant();
        owner(t, "1.1", "pdfs", "curator");
        String s = doc(t, "1.1", "a", OLD_URI, "/old/a.pdf");
        String tt = doc(t, "1.1", "a-new", NEW_URI, "/new/a.pdf");
        move(t, s, false);

        repo.restoreDocument(t, tt);

        assertThat(row(t, tt).get("deleted_at")).isNull();
        assertThat(row(t, tt).get("source_uri")).isEqualTo("");
    }
}
