// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
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
 * rows are found under A, not B) and with it latched (found under B, not A), flipped in process with no restart.
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
            dropForeignKeysOntoTheRegistry(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    /** The name keys P3.7 drops, here every foreign key onto the registry (typed, from pg_constraint), so a registry row can be renamed. */
    private static void dropForeignKeysOntoTheRegistry(DSLContext dsl) {
        var con = DSL.table(DSL.name("pg_catalog", "pg_constraint"));
        var cls = DSL.table(DSL.name("pg_catalog", "pg_class"));
        var nsp = DSL.table(DSL.name("pg_catalog", "pg_namespace"));
        var target = DSL.function("to_regclass", Object.class, DSL.val("nexus.catalog_collections"));
        for (Record3<String, String, String> fk : dsl
                .select(DSL.field(DSL.name("pg_namespace", "nspname"), String.class),
                        DSL.field(DSL.name("pg_class", "relname"), String.class),
                        DSL.field(DSL.name("pg_constraint", "conname"), String.class))
                .from(con)
                .join(cls).on(DSL.field(DSL.name("pg_class", "oid")).eq(DSL.field(DSL.name("pg_constraint", "conrelid"))))
                .join(nsp).on(DSL.field(DSL.name("pg_namespace", "oid")).eq(DSL.field(DSL.name("pg_class", "relnamespace"))))
                .where(DSL.field(DSL.name("pg_constraint", "contype"), String.class).eq("f"))
                .and(DSL.field(DSL.name("pg_constraint", "confrelid")).eq(target))
                .and(DSL.field(DSL.name("pg_constraint", "conparentid")).eq(0L))
                .fetch()) {
            dsl.alterTable(DSL.table(DSL.name(fk.value1(), fk.value2()))).dropConstraint(DSL.name(fk.value3())).execute();
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
        Fx fx = fixture("rdr");
        String chash = ch("p35c catalog " + seq.incrementAndGet());
        gatedSvc.writeManyCombined(TENANT, fx.collection(), List.of(chunk(chash, "p35c text " + chash)),
            List.of(doc(fx.docId(), List.of(row(0, chash)))), null, false, true);
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

        latched.set(false);
        assertThat(tumblers(gated.documentsByCollection(TENANT, r.oldLabel(), 0, 0)))
            .as("the gate is read on every call: latched, then not held, the name path again").containsExactly(r.doc());
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
}
