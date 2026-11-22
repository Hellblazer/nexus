// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.UnregisteredCollectionException;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.SQLException;
import java.util.List;
import java.util.Map;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.GC_AUDIT;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.1 stage 2b (nexus-p2m1x.17), catalog family: the catalog writers set the registry's id beside every
 * collection name they write (the dual write), on real Postgres under the NOT VALID CHECKs of vectors-068.
 *
 * <p>Three kinds of case. A writer that names a collection ends with the row's id equal to the registry row's id. A
 * writer that names none (a tombstone, an alias, an index-run stamp) updates a row the stamp move has not reached
 * (name set, id NULL, planted here with the CHECK lifted for the insert) and must stamp it, because the CHECK is
 * evaluated on every updated row version. And a name no registry row holds is refused, as the CHECK demands.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogDualWriteIdTest {

    private static final String T = "dw-a";
    private static final String C1 = "knowledge__dw1__minilm-l6-v2-384__v1";
    private static final String C2 = "knowledge__dw2__minilm-l6-v2-384__v1";
    private static final String SVC_ROLE = "svc_dw_id";
    private static final String SVC_PASS = "svc_dw_id_pass";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    CatalogRepository repo;

    @BeforeAll
    void startAll() throws Exception {
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
        repo = new CatalogRepository(new TenantScope(svcDs));
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, T, C1);
            PgContainerHelper.insertCollection(ctx, T, C2);
            return null;
        });
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── helpers ───────────────────────────────────────────────────────────────

    private <R> R su(Function<DSLContext, R> work) {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private Long regId(String name) {
        return su(ctx -> ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID));
    }

    private Long docId(String tumbler) {
        return su(ctx -> ctx.select(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(T)).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))
            .fetchOne(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID));
    }

    private Long supersededId(String name) {
        return su(ctx -> ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID));
    }

    /** Plants a document the stamp has not reached (name set, id NULL): the CHECK is lifted for the insert and put back NOT VALID. */
    private void plantUnstampedDocument(String tumbler, String collection) {
        plantUnstampedDocument(tumbler, collection, false);
    }

    private void plantUnstampedDocument(String tumbler, String collection, boolean tombstoned) {
        su(ctx -> {
            P31CheckReplay.lift(ctx, P31CheckReplay.CATALOG_DOCUMENTS);
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                    CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
                .values(T, tumbler, "unstamped " + tumbler, collection).execute();
            if (tombstoned) {
                ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.DELETED_AT, java.time.OffsetDateTime.now())
                    .where(CATALOG_DOCUMENTS.TENANT_ID.eq(T)).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)).execute();
            }
            P31CheckReplay.restore(ctx, P31CheckReplay.CATALOG_DOCUMENTS);
            return null;
        });
        assertThat(docId(tumbler)).as("precondition: " + tumbler + " is unstamped").isNull();
    }

    private static Map<String, Object> doc(String tumbler, String collection) {
        return Map.of("tumbler", tumbler, "title", "Doc " + tumbler, "content_type", "paper",
            "corpus", "knowledge", "physical_collection", collection, "chunk_count", 0);
    }

    // ── documents: writers that name the collection ───────────────────────────

    @Test
    void upsertDocument_setsTheId_andTheConflictArmMovesItWithTheName() {
        repo.upsertDocument(T, doc("dw.u1", C1));
        assertThat(docId("dw.u1")).isEqualTo(regId(C1));
        repo.upsertDocument(T, doc("dw.u1", C2));
        assertThat(docId("dw.u1")).as("the DO UPDATE arm moves the id with the name").isEqualTo(regId(C2));
    }

    @Test
    void registerDocument_single_and_many_setTheId() {
        String t1 = repo.registerDocument(T, "dw-reg", Map.of("title", "r1", "content_type", "paper",
            "corpus", "knowledge", "file_path", "/dw/r1.md", "physical_collection", C1));
        List<String> many = repo.registerDocumentMany(T, "dw-reg", List.of(
            Map.of("title", "r2", "content_type", "paper", "corpus", "knowledge", "file_path", "/dw/r2.md",
                "physical_collection", C2),
            Map.of("title", "r3", "content_type", "paper", "corpus", "knowledge", "file_path", "/dw/r3.md",
                "physical_collection", C1)));
        assertThat(docId(t1)).isEqualTo(regId(C1));
        assertThat(docId(many.get(0))).isEqualTo(regId(C2));
        assertThat(docId(many.get(1))).isEqualTo(regId(C1));
    }

    @Test
    void importDocument_single_and_batch_setTheId() {
        repo.importDocument(T, doc("dw.i1", C1));
        repo.importDocumentsBatch(T, List.of(doc("dw.i2", C2), doc("dw.i3", C1)));
        assertThat(docId("dw.i1")).isEqualTo(regId(C1));
        assertThat(docId("dw.i2")).isEqualTo(regId(C2));
        assertThat(docId("dw.i3")).isEqualTo(regId(C1));
    }

    @Test
    void aDocumentWithNoCollection_keepsANullId() {
        repo.upsertDocument(T, Map.of("tumbler", "dw.n1", "title", "no collection", "content_type", "paper",
            "corpus", "knowledge", "chunk_count", 0));
        assertThat(docId("dw.n1")).isNull();
    }

    private static final String GHOST = "knowledge__never-registered__minilm-l6-v2-384__v1";

    private int docsNaming(String collection) {
        return su(ctx -> ctx.fetchCount(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID.eq(T)
            .and(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION.eq(collection))));
    }

    @Test
    void everyRegisterPath_refusesAnUnregisteredCollectionWithTheTypedError_andWritesNothing() {
        Map<String, Object> fields = Map.of("title", "ghost", "content_type", "paper", "corpus", "knowledge",
            "file_path", "/dw/ghost.md", "physical_collection", GHOST);
        assertThatThrownBy(() -> repo.upsertDocument(T, doc("dw.x1", GHOST)))
            .as("upsertDocument").isInstanceOf(UnregisteredCollectionException.class);
        assertThatThrownBy(() -> repo.registerDocument(T, "dw-ghost", fields))
            .as("registerDocument").isInstanceOf(UnregisteredCollectionException.class);
        assertThatThrownBy(() -> repo.registerDocumentMany(T, "dw-ghost", List.of(fields)))
            .as("registerDocumentMany").isInstanceOf(UnregisteredCollectionException.class);
        assertThatThrownBy(() -> repo.importDocument(T, doc("dw.x2", GHOST)))
            .as("importDocument").isInstanceOf(UnregisteredCollectionException.class);
        assertThatThrownBy(() -> repo.importDocumentsBatch(T, List.of(doc("dw.x3", C1), doc("dw.x4", GHOST))))
            .as("importDocumentsBatch refuses the whole batch").isInstanceOf(UnregisteredCollectionException.class);
        assertThat(docsNaming(GHOST)).isZero();
        assertThat(docId("dw.x3")).as("the registered row of the refused batch was not written either").isNull();
    }

    @Test
    void everyUpdatePath_refusesAMoveToAnUnregisteredCollectionWithTheTypedError_andWritesNothing() {
        repo.upsertDocument(T, doc("dw.x5", C1));
        assertThatThrownBy(() -> repo.updateDocument(T, "dw.x5", Map.of("physical_collection", GHOST)))
            .as("updateDocument").isInstanceOf(UnregisteredCollectionException.class);
        assertThatThrownBy(() -> repo.updateDocumentsMany(T, List.of(
                Map.of("tumbler", "dw.x5", "title", "fine"), Map.of("tumbler", "dw.x5", "physical_collection", GHOST))))
            .as("updateDocumentsMany refuses the whole request").isInstanceOf(UnregisteredCollectionException.class);
        assertThat(docsNaming(GHOST)).isZero();
        assertThat(docId("dw.x5")).isEqualTo(regId(C1));
        String title = su(ctx -> ctx.select(CATALOG_DOCUMENTS.TITLE).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TUMBLER.eq("dw.x5")).fetchOne(CATALOG_DOCUMENTS.TITLE));
        assertThat(title).as("the title update of the refused batch did not land").isEqualTo("Doc dw.x5");
    }

    @Test
    void anUpdateOfAnUnknownOrTombstonedTumblerIsANoOpWhateverTheCollection_notTheTypedRefusal() {
        // bob's finding (P3.5a): the collection was resolved before the tumbler was looked up, so this was a 422
        // (tests/test_catalog_collections.py::test_update_document_collection_returns_false_on_unknown_tumbler)
        assertThat(repo.updateDocument(T, "dw.nobody", Map.of("physical_collection", GHOST)))
            .as("updateDocument of an unknown tumbler to an unregistered collection").isZero();
        assertThat(repo.updateDocument(T, "dw.nobody", Map.of("physical_collection", C2)))
            .as("updateDocument of an unknown tumbler to a registered collection").isZero();
        assertThat(repo.updateDocumentsMany(T, List.of(
                Map.of("tumbler", "dw.nobody", "physical_collection", GHOST),
                Map.of("tumbler", "dw.nobody", "title", "x"))))
            .as("updateDocumentsMany: an unknown tumbler's slot is 0, the batch is not refused").containsExactly(0, 0);

        repo.upsertDocument(T, doc("dw.gone", C1));
        su(ctx -> ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.DELETED_AT, java.time.OffsetDateTime.now())
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(T)).and(CATALOG_DOCUMENTS.TUMBLER.eq("dw.gone")).execute());
        assertThat(repo.updateDocument(T, "dw.gone", Map.of("physical_collection", GHOST)))
            .as("a tombstoned document is a no-op too (it was before the dual write)").isZero();
        assertThat(docId("dw.gone")).as("nothing was written").isEqualTo(regId(C1));
        assertThat(docsNaming(GHOST)).isZero();
    }

    @Test
    void updateDocument_movingTheCollection_carriesTheNewId_andOtherUpdatesKeepIt() {
        repo.upsertDocument(T, doc("dw.m1", C1));
        repo.updateDocument(T, "dw.m1", Map.of("title", "retitled"));
        assertThat(docId("dw.m1")).as("an update that names no collection keeps the id").isEqualTo(regId(C1));
        repo.updateDocument(T, "dw.m1", Map.of("physical_collection", C2));
        assertThat(docId("dw.m1")).as("an update that moves the collection moves the id").isEqualTo(regId(C2));
        repo.updateDocumentsMany(T, List.of(Map.of("tumbler", "dw.m1", "physical_collection", C1)));
        assertThat(docId("dw.m1")).isEqualTo(regId(C1));
    }

    // ── documents: writers that name no collection, on a row the stamp has not reached ─────────────

    @Test
    void everyWriterThatNamesNoCollection_stampsAnUnstampedDocument() {
        record Case(String label, Runnable write) {}
        List<Case> cases = List.of(
            new Case("updateDocument", () -> repo.updateDocument(T, "dw.s.update", Map.of("title", "t2"))),
            new Case("updateDocumentsMany", () -> repo.updateDocumentsMany(T,
                List.of(Map.of("tumbler", "dw.s.many", "title", "t3")))),
            new Case("deleteDocument", () -> repo.deleteDocument(T, "dw.s.delete")),
            new Case("deleteDocumentsMany", () -> repo.deleteDocumentsMany(T, List.of("dw.s.deletemany"))),
            new Case("setAlias", () -> repo.setAlias(T, "dw.s.alias", "dw.s.update")),
            new Case("beginIndexRun", () -> repo.beginIndexRun(T, "dw.s.begin", "hash", "run-1", C1)),
            new Case("failIndexRun", () -> repo.failIndexRun(T, "dw.s.fail", "boom")),
            new Case("resyncChunkCount", () -> repo.resyncChunkCount(T, "dw.s.resync")),
            new Case("purgeManifest", () -> repo.purgeManifest(T, "dw.s.purge")),
            new Case("setSensitivity", () -> repo.setSensitivity(T, List.of("dw.s.sens"), true, "", "test")));
        for (Case c : cases) {
            String tumbler = switch (c.label()) {
                case "updateDocument" -> "dw.s.update";
                case "updateDocumentsMany" -> "dw.s.many";
                case "deleteDocument" -> "dw.s.delete";
                case "deleteDocumentsMany" -> "dw.s.deletemany";
                case "setAlias" -> "dw.s.alias";
                case "beginIndexRun" -> "dw.s.begin";
                case "failIndexRun" -> "dw.s.fail";
                case "resyncChunkCount" -> "dw.s.resync";
                case "purgeManifest" -> "dw.s.purge";
                default -> "dw.s.sens";
            };
            plantUnstampedDocument(tumbler, C1);
            c.write().run();
            assertThat(docId(tumbler)).as(c.label() + " stamps the unstamped row it touched").isEqualTo(regId(C1));
        }
    }

    @Test
    void restoreDocument_stampsAnUnstampedTombstone() {
        plantUnstampedDocument("dw.s.restore", C2, true);
        assertThat(repo.restoreDocument(T, "dw.s.restore")).isEqualTo(1);
        assertThat(docId("dw.s.restore")).isEqualTo(regId(C2));
    }

    // ── the manifest ──────────────────────────────────────────────────────────

    private static String hex(String seed) {
        return dev.nexus.service.db.Chash.ofText(seed).toHex();
    }

    private void seedChunk(String collection, String chashHex) {
        su(ctx -> {
            PgContainerHelper.insertChunk384(ctx, T, collection, java.util.HexFormat.of().parseHex(chashHex),
                VectorSeeds.vec(384, 0.1f));
            return null;
        });
    }

    private List<Long> manifestIds(String docId) {
        return su(ctx -> ctx.select(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID).from(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(T)).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(docId))
            .fetch(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID));
    }

    @Test
    void manifestWriters_setTheId_onInsertReplaceAndAppend() {
        repo.upsertDocument(T, doc("dw.mf1", C1));
        for (String s : List.of("mf-a", "mf-b", "mf-c")) seedChunk(C1, hex(s));
        repo.writeManifest(T, "dw.mf1", C1, List.of(
            Map.of("position", 0, "chash", hex("mf-a"), "chunk_index", 0),
            Map.of("position", 1, "chash", hex("mf-b"), "chunk_index", 1)));
        assertThat(manifestIds("dw.mf1")).containsExactly(regId(C1), regId(C1));
        // a replace that rewrites row 0's payload (a section title, the diff's UPDATE path; the chash stays, so no chunk
        // is orphaned) and adds row 2 (the INSERT path)
        repo.writeManifest(T, "dw.mf1", C1, List.of(
            Map.of("position", 0, "chash", hex("mf-a"), "chunk_index", 0, "section_title", "Intro"),
            Map.of("position", 1, "chash", hex("mf-b"), "chunk_index", 1),
            Map.of("position", 2, "chash", hex("mf-c"), "chunk_index", 2)));
        assertThat(manifestIds("dw.mf1")).hasSize(3).containsOnly(regId(C1));
        repo.appendManifestChunks(T, "dw.mf1", C1, List.of(Map.of("position", 3, "chash", hex("mf-a"), "chunk_index", 3)));
        assertThat(manifestIds("dw.mf1")).hasSize(4).containsOnly(regId(C1));
        assertThat(docId("dw.mf1")).isEqualTo(regId(C1));
    }

    // ── collections ───────────────────────────────────────────────────────────

    @Test
    void upsertCollection_namingATombstone_setsTheSuccessorsId() {
        String successor = "knowledge__dw-succ__minilm-l6-v2-384__v1";
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, T, successor);
            return null;
        });
        repo.upsertCollection(T, Map.of("name", "knowledge__dw-old1__minilm-l6-v2-384__v1", "content_type", "knowledge",
            "owner_id", "dw-old1", "embedding_model", "minilm-l6-v2-384", "model_version", "v1",
            "superseded_by", successor));
        assertThat(supersededId("knowledge__dw-old1__minilm-l6-v2-384__v1")).isEqualTo(regId(successor));
    }

    @Test
    void aTombstoneWhoseSuccessorHasNoRow_readsRetiredNoSuccessor_itsOwnId() {
        String name = "knowledge__dw-old2__minilm-l6-v2-384__v1";
        repo.upsertCollection(T, Map.of("name", name, "content_type", "knowledge", "owner_id", "dw-old2",
            "embedding_model", "minilm-l6-v2-384", "model_version", "v1",
            "superseded_by", "knowledge__dw-never__minilm-l6-v2-384__v1"));
        assertThat(supersededId(name)).as("R7: the row's own collection_id").isEqualTo(regId(name));
    }

    @Test
    void importCollectionsBatch_resolvesASuccessorLaterInTheSameBatch_andAMissingOneToTheRowsOwnId() {
        String a = "knowledge__dw-ia__minilm-l6-v2-384__v1";
        String b = "knowledge__dw-ib__minilm-l6-v2-384__v1";
        String c = "knowledge__dw-ic__minilm-l6-v2-384__v1";
        Map<String, Object> base = Map.of("content_type", "knowledge", "embedding_model", "minilm-l6-v2-384",
            "model_version", "v1");
        java.util.function.BiFunction<String, String, Map<String, Object>> row = (n, by) -> {
            var m = new java.util.LinkedHashMap<String, Object>(base);
            m.put("name", n);
            m.put("owner_id", "dw");
            m.put("superseded_by", by);
            return m;
        };
        repo.importCollectionsBatch(T, List.of(row.apply(a, b), row.apply(b, ""), row.apply(c, "knowledge__dw-gone__minilm-l6-v2-384__v1")));
        assertThat(supersededId(a)).as("a names b, inserted after it in the batch").isEqualTo(regId(b));
        assertThat(supersededId(b)).isNull();
        assertThat(supersededId(c)).as("a missing successor gives the row's own id").isEqualTo(regId(c));
    }

    @Test
    void anUpdateOfAnUnstampedTombstone_stampsItsSupersededId() {
        String succ = "knowledge__dw-ts-succ__minilm-l6-v2-384__v1";
        String name = "knowledge__dw-ts-old__minilm-l6-v2-384__v1";
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, T, succ);
            PgContainerHelper.insertCollection(ctx, T, name);
            P31CheckReplay.lift(ctx, P31CheckReplay.COLLECTIONS_SUPERSEDED);
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, succ)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(name)).execute();
            P31CheckReplay.restore(ctx, P31CheckReplay.COLLECTIONS_SUPERSEDED);
            return null;
        });
        assertThat(supersededId(name)).as("precondition: unstamped tombstone").isNull();
        assertThat(repo.setCollectionAspectsEnabled(T, name, true)).isEqualTo(1);
        assertThat(supersededId(name)).as("the update that names no superseded_by stamps it").isEqualTo(regId(succ));
    }

    // ── audit ─────────────────────────────────────────────────────────────────

    @Test
    void recordGcAudit_setsTheCollectionId() {
        long id = repo.recordGcAudit(T, Map.of("operation", "purge", "collection", C1, "actor", "test"));
        Long stored = su(ctx -> ctx.select(GC_AUDIT.COLLECTION_ID).from(GC_AUDIT).where(GC_AUDIT.ID.eq(id))
            .fetchOne(GC_AUDIT.COLLECTION_ID));
        assertThat(stored).isEqualTo(regId(C1));
    }
}
