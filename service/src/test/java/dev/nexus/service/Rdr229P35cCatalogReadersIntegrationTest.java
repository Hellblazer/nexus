// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.CombinedWriteService;
import dev.nexus.service.vectors.EmbedderRouter;
import org.jooq.DSLContext;
import org.jooq.Record3;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25), the catalog group: the readers of {@code CatalogRepository} read the collection's id once gate 3
 * has latched, and the name until then (RDR item 6, "Readers during the stamp").
 *
 * <p>Fixture. A collection is registered under label A with a document, a chunk and a manifest row, every row carrying the
 * registry's id. The registry row is then renamed to label B by a one-row {@code UPDATE ... SET name} (what P3.7's one-row rename
 * does), with the foreign keys onto the registry's name dropped as P3.7's name-key drop does: every other table keeps the name A, so
 * the name and the id now disagree about the collection. The same repository instance is read with gate 3 not held (as today: the
 * rows are found under A, not B) and with it latched (found under B, not A), latched in process with no restart. The gate is the latched, monotone value, so no test returns to
 * the name path once latched (PciIdGate3IntegrationTest pins that the latched value stays true).
 *
 * <p>The scratch container is per class and the name keys are dropped once, for the whole class; every test uses its own collection.
 */
class Rdr229P35cCatalogReadersIntegrationTest extends AtomicWriteTestBase {

    private final AtomicBoolean latched = new AtomicBoolean();
    private CatalogRepository gated;
    private CombinedWriteService gatedSvc;

    @BeforeAll
    void gatedRepository() throws Exception {
        gated = new CatalogRepository(tenantScope, latched::get);
        gatedSvc = new CombinedWriteService(tenantScope, gated, new EmbedderRouter(embedder, "document"));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            RegistryRenameSupport.dropForeignKeysOntoTheRegistry(dsl);
            // the id arbiters P3.3c builds, over the partitioned parent: a latched chunk upsert needs them
            dsl.createUniqueIndexIfNotExists("p35c_cat_chunks_id_key")
                .on(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION_ID, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL).execute();
        }
    }

    private void renameRegistryRow(String from, String to) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            int n = DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, to)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(from)).execute();
            assertThat(n).as("the registry row of " + from + " is renamed").isEqualTo(1);
        }
    }

    private Long registryId(String name) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name))
                .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
        }
    }

    /** A collection with one document holding one chunk and one manifest row, written through the combined write. */
    private record Renamed(String oldLabel, String newLabel, String doc, String chash, long id) {}

    private Renamed renamedCollection() throws Exception {
        latched.set(false);  // the fixture is written before the latch; the tests then latch
        Fx fx = fixture("rdr");
        String chash = ch("p35c catalog " + seq.incrementAndGet());
        var written = gatedSvc.writeManyCombined(TENANT, fx.collection(), List.of(chunk(chash, "p35c text " + chash)),
            List.of(doc(fx.docId(), List.of(row(0, chash)))), null, false, true);
        assertThat(chunkExists(fx.collection(), chash)).as("fixture: the chunk is stored; " + written.response()).isTrue();
        assertThat(manifestChashes(fx.docId())).as("fixture: the manifest row is stored; " + written.response()).contains(chash);
        long id = registryId(fx.collection());
        String newLabel = "code__aw" + "rdrnew" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        renameRegistryRow(fx.collection(), newLabel);
        assertThat(registryId(newLabel)).as("a rename keeps the id").isEqualTo(id);
        return new Renamed(fx.collection(), newLabel, fx.docId(), chash, id);
    }

    private static List<String> tumblers(List<Map<String, Object>> docs) {
        return docs.stream().map(d -> String.valueOf(d.get("tumbler"))).toList();
    }

    // ---- document listing -------------------------------------------------------------------------------------

    @Test
    void documentListingFindsTheCollectionByIdUnderItsNewLabelOnceGate3Latches() throws Exception {
        Renamed r = renamedCollection();

        latched.set(false);
        assertThat(tumblers(gated.documentsByCollection(TENANT, r.oldLabel(), 0, 0)))
            .as("before the latch: the name path, as today: the documents still name the old label").containsExactly(r.doc());
        assertThat(gated.documentsByCollection(TENANT, r.newLabel(), 0, 0)).as("before the latch: the new label names no document").isEmpty();

        latched.set(true);
        var byNewLabel = gated.documentsByCollection(TENANT, r.newLabel(), 0, 0);
        assertThat(tumblers(byNewLabel)).as("from the latch: found by id under the new label").containsExactly(r.doc());
        assertThat(byNewLabel.get(0).get("physical_collection")).as("and shown under the registry's label, not the stale name")
            .isEqualTo(r.newLabel());
        assertThat(gated.documentsByCollection(TENANT, r.oldLabel(), 0, 0)).as("the old label names no registry row: no collection").isEmpty();
    }

    // ---- collection lookup ------------------------------------------------------------------------------------

    @Test
    void collectionLookupAnswersTheRenamedRowByItsLabelBeforeAndAfterTheLatch() throws Exception {
        Renamed r = renamedCollection();
        for (boolean gate : new boolean[] {false, true}) {
            latched.set(gate);
            var row = gated.getCollection(TENANT, r.newLabel());
            assertThat(row).as("gate " + gate + ": the registry row is found under its new label").isNotNull();
            assertThat(row.get("name")).isEqualTo(r.newLabel());
            assertThat(((Number) row.get("collection_id")).longValue()).as("the same id").isEqualTo(r.id());
            assertThat(gated.getCollection(TENANT, r.oldLabel())).as("gate " + gate + ": the old label names nothing").isNull();
            assertThat(gated.collectionForTuple(TENANT, "code", r.oldLabel().split("__")[1], "minilm-l6-v2-384"))
                .as("gate " + gate + ": the tuple resolves to the live row under its new label")
                .extracting(m -> m.get("name")).isEqualTo(r.newLabel());
            assertThat(gated.listCollections(TENANT, null, "live")).extracting(m -> m.get("name"))
                .contains(r.newLabel()).doesNotContain(r.oldLabel());
            assertThat(gated.getCollection(TENANT, r.newLabel()).get("superseded_by"))
                .as("a live row names no successor, by superseded_by or by superseded_by_id").isEqualTo("");
        }
    }

    // ---- the reads behind catalog routes ----------------------------------------------------------------------

    @Test
    void theReadsBehindCatalogRoutesFindTheRenamedCollectionByIdFromTheLatch() throws Exception {
        Renamed r = renamedCollection();

        latched.set(false);
        assertThat(gated.chashesForCollection(TENANT, r.oldLabel())).as("before: by name").containsExactly(r.chash());
        assertThat(gated.chashesForCollection(TENANT, r.newLabel())).isEmpty();
        assertThat(gated.getManifest(TENANT, r.doc())).extracting(m -> m.get("collection")).containsExactly(r.oldLabel());
        assertThat(gated.collectionDocCountsIncludingDeleted(TENANT)).containsKey(r.oldLabel()).doesNotContainKey(r.newLabel());
        assertThat(gated.distinctDocCollections(TENANT)).contains(r.oldLabel()).doesNotContain(r.newLabel());
        assertThat(gated.rehomeStatus(TENANT, r.newLabel()).remainingChunks()).isZero();

        latched.set(true);
        assertThat(gated.chashesForCollection(TENANT, r.newLabel())).as("from the latch: by id").containsExactly(r.chash());
        assertThat(gated.chashesForCollection(TENANT, r.oldLabel())).isEmpty();
        assertThat(gated.getManifest(TENANT, r.doc())).as("the manifest row shows the registry's label")
            .extracting(m -> m.get("collection")).containsExactly(r.newLabel());
        assertThat(gated.collectionDocCountsIncludingDeleted(TENANT)).containsEntry(r.newLabel(), 1L).doesNotContainKey(r.oldLabel());
        assertThat(gated.distinctDocCollections(TENANT)).contains(r.newLabel()).doesNotContain(r.oldLabel());
        var status = gated.rehomeStatus(TENANT, r.newLabel());
        assertThat(status.remainingChunks()).as("the rows still naming the collection, by id").isEqualTo(1);
        assertThat(status.remainingDocuments()).isEqualTo(1);
        assertThat(gated.chashPositions(TENANT, List.of(r.chash()))).extracting(m -> m.get("collection"))
            .as("a chash's holder shows the registry's label").containsExactly(r.newLabel());
        Map<String, Object> resolved = gated.resolveChash(TENANT, r.chash(), r.newLabel());
        assertThat(resolved).as("the chunk is found by chash and shown under the label").isNotNull();
        assertThat(resolved.get("physical_collection")).isEqualTo(r.newLabel());
    }

    @Test
    void deletingARenamedCollectionRemovesItsRowsByIdFromTheLatch() throws Exception {
        Renamed r = renamedCollection();
        latched.set(true);

        Map<String, Integer> counts = gated.deleteCollection(TENANT, r.newLabel());

        assertThat(counts.get("chunks")).as("the chunk, found by id").isEqualTo(1);
        assertThat(counts.get("catalog_documents")).as("the document, found by id").isEqualTo(1);
        assertThat(counts.get("catalog_collections")).isEqualTo(1);
        assertThat(chunkExists(r.oldLabel(), r.chash())).isFalse();
    }

    // ---- a NULL id is no collection ---------------------------------------------------------------------------

    @Test
    void aNullIdIsNoCollectionAndNeverFallsBackToAnEmptyName() {
        String tumbler = "aw.nullid." + seq.incrementAndGet();
        gated.upsertDocument(TENANT, Map.of("tumbler", tumbler, "title", "no collection", "content_type", "knowledge",
            "corpus", "knowledge", "physical_collection", "", "chunk_count", 0));

        latched.set(false);
        assertThat(tumblers(gated.documentsByCollection(TENANT, "", 0, 0))).as("today: the '' name is matched").contains(tumbler);
        assertThat(gated.distinctDocCollections(TENANT)).doesNotContain("");

        latched.set(true);
        assertThat(gated.documentsByCollection(TENANT, "", 0, 0)).as("from the latch: a '' label resolves to no id, so no rows").isEmpty();
        assertThat(gated.distinctDocCollections(TENANT)).as("a document with no id is under no collection").doesNotContain("");
        assertThat(gated.collectionDocCountsIncludingDeleted(TENANT)).doesNotContainKey("");
    }

    // ---- the sensitive flag follows the chunk's stored name, not the label (review F3) -------------------------------

    @Test
    void aSensitiveChunkStillReadsSensitiveAfterAOneRowRename_andExcludeStillMissesIt() throws Exception {
        latched.set(false);
        String collection = "code__awsens" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        registerCollection(collection);
        String docId = "aw.sens." + seq.incrementAndGet();
        gated.upsertDocument(TENANT, Map.of("tumbler", docId, "title", "sensitive doc", "content_type", "code", "corpus", "code",
            "physical_collection", collection, "chunk_count", 0, "sensitive", true));
        String chash = ch("p35c sensitive " + seq.incrementAndGet());
        gatedSvc.writeManyCombined(TENANT, collection, List.of(chunk(chash, "secret " + chash)),
            List.of(doc(docId, List.of(row(0, chash)))), null, false, true);
        assertThat(gated.resolveChash(TENANT, chash, collection).get("sensitive")).as("fixture: sensitive before the rename").isEqualTo(true);
        String renamed = "code__awsens" + "new" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        renameRegistryRow(collection, renamed);

        latched.set(true);

        assertThat(gated.resolveChash(TENANT, chash, renamed).get("sensitive")).as("by chash, latched").isEqualTo(true);
        assertThat(gated.resolveChash(TENANT, chash, renamed, true)).as("exclude: a miss").isNull();
        assertThat(gated.resolveSpan(TENANT, renamed, chash, false).get("sensitive")).as("by span, latched").isEqualTo(true);
        assertThat(gated.resolveSpan(TENANT, renamed, chash, true)).as("exclude: a miss").isNull();
    }

    // ---- by-title lookups show the registry's label (review F2) ------------------------------------------------------

    @Test
    void documentsByTitleShowTheRegistryLabelFromTheLatch() throws Exception {
        Renamed r = renamedCollection();
        String title = "atomic-write-" + r.doc();

        latched.set(false);
        assertThat(gated.documentsByTitle(TENANT, title, null, null, null)).extracting(m -> m.get("physical_collection"))
            .containsExactly(r.oldLabel());

        latched.set(true);
        assertThat(gated.documentsByTitle(TENANT, title, null, null, null)).as("after the latch the label, not the stale name")
            .extracting(m -> m.get("physical_collection")).containsExactly(r.newLabel());
    }

    // ---- rehome: the id repair keeps a renamed collection's rows (review F1) ----------------------------------------

    @Test
    void aRehomeOntoALabelAnotherCollectionRenamedAwayLeavesThatCollectionsRowsItsOwnId() throws Exception {
        Renamed a = renamedCollection();            // A (id1) is now B; its manifest and chunk rows still name A
        Renamed c = renamedCollection();            // C (id2) ...
        String reusedLabel = a.oldLabel();          // ... is renamed to A's old label, which the rows of id1 still carry
        renameRegistryRow(c.newLabel(), reusedLabel);
        Fx source = fixture("rhsrc");
        String sourceChash = ch("p35c rehome source " + seq.incrementAndGet());
        gatedSvc.writeManyCombined(TENANT, source.collection(), List.of(chunk(sourceChash, "source " + sourceChash)),
            List.of(doc(source.docId(), List.of(row(0, sourceChash)))), null, false, true);
        latched.set(true);

        gated.rehomeCollection(TENANT, source.collection(), reusedLabel);

        assertThat(manifestIdsOf(a.doc())).as("the renamed collection's manifest rows keep id1, whatever name they carry")
            .containsExactly(a.id());
        assertThat(manifestIdsOf(source.docId())).as("the rows the cascade carried take the target's id (C's, id2)")
            .containsExactly(c.id());
    }

    private List<Long> manifestIdsOf(String docId) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID).from(CATALOG_DOCUMENT_CHUNKS)
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(docId))
                .fetch(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID);
        }
    }

    // ---- a latched write over a renamed registry row (G17, linda 2026-11-23) ------------------------------------------

    @Test
    void aLatchedManifestWriteOverARenamedCollectionTakesTheChunksStoredName_andClearsTheOrphanRecordsById() throws Exception {
        Renamed r = renamedCollection();            // the chunk is named r.oldLabel(), the registry says r.newLabel()
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).insertInto(CHUNK_ORPHANED_AT)
                .set(CHUNK_ORPHANED_AT.TENANT_ID, TENANT).set(CHUNK_ORPHANED_AT.COLLECTION, r.oldLabel())
                .set(CHUNK_ORPHANED_AT.COLLECTION_ID, r.id()).set(CHUNK_ORPHANED_AT.CHASH, java.util.HexFormat.of().parseHex(r.chash()))
                .set(CHUNK_ORPHANED_AT.ORPHANED_AT, java.time.OffsetDateTime.now().minusDays(3))
                .set(CHUNK_ORPHANED_AT.EMBEDDING_MODEL, "minilm-l6-v2-384").execute();
        }
        latched.set(true);
        String second = freshDoc("g17", r.newLabel());

        // a manifest row for a chunk that already exists: the chunk is found by id, and the row names the chunk's stored name
        var result = gatedSvc.writeManyCombined(TENANT, r.newLabel(), List.of(), List.of(doc(second, List.of(row(0, r.chash())))),
            null, false, false);
        // a replace of the first document's manifest: the orphan record is cleared by id
        var again = gatedSvc.writeManyCombined(TENANT, r.newLabel(), List.of(), List.of(doc(r.doc(), List.of(row(0, r.chash())))),
            null, false, false);

        assertThat(result.response()).as("no 23503 from fk_catalog_chunks_chunk").containsEntry("failed_doc_ids", List.of());
        assertThat(again.response()).containsEntry("failed_doc_ids", List.of());
        try (Connection su = pg.createConnection("")) {
            var d = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(d.select(CATALOG_DOCUMENT_CHUNKS.COLLECTION).from(CATALOG_DOCUMENT_CHUNKS)
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(second))
                .fetch(CATALOG_DOCUMENT_CHUNKS.COLLECTION)).as("the manifest row's name is the chunk's").containsExactly(r.oldLabel());
            assertThat(d.fetchCount(CHUNK_ORPHANED_AT, CHUNK_ORPHANED_AT.TENANT_ID.eq(TENANT)
                .and(CHUNK_ORPHANED_AT.COLLECTION_ID.eq(r.id())))).as("the orphan record, matched by id, is cleared").isZero();
            assertThat(d.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION_ID.eq(r.id()))))
                .as("one chunk row of the collection, still").isEqualTo(1);
        }
    }

    // ---- a latched chunk upsert over a renamed registry row ----------------------------------------------------------

    @Test
    void aLatchedCombinedWriteOverARenamedCollectionUpdatesItsChunkByIdInsteadOfInsertingASecond() throws Exception {
        Renamed r = renamedCollection();
        latched.set(true);
        String doc2 = freshDoc("lw", r.newLabel());

        var result = gatedSvc.writeManyCombined(TENANT, r.newLabel(), List.of(chunk(r.chash(), "rewritten text")),
            List.of(doc(doc2, List.of(row(0, r.chash())))), null, false, true);

        assertThat(result.response()).containsEntry("failed_doc_ids", List.of());
        try (Connection su = pg.createConnection("")) {
            assertThat(DSL.using(su, SQLDialect.POSTGRES).fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT)
                .and(CHUNKS.COLLECTION_ID.eq(r.id())))).as("the id key matched the existing row: still one row").isEqualTo(1);
        }
    }
}
