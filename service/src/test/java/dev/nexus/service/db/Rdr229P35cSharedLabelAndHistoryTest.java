// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.GC_AUDIT;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.AtomicWriteTestBase;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.RegistryRenameSupport;
import dev.nexus.service.datamove.Gate3;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25), linda's rulings of 2026-11-23, on the harness that drops the registry's name unique keys (ruling 4:
 * the foreign keys onto the name, {@code catalog_collections_pk} and {@code catalog_collections_tenant_name_model_uq}), so a
 * tombstone and a live row can share a label:
 *
 * <ul>
 *   <li>Q3, live first: the resolvers that mean "the collection called v" answer the live row, and the one-row-per-label
 *       resolvers do not throw on two rows;</li>
 *   <li>Q3, a re-point by id: a tombstone already pointing at a real registry row is not moved onto a row that took its label later;</li>
 *   <li>Q4, the wire: {@code superseded_by} is byte-identical to the text column for a live row and for a retired tombstone with no
 *       successor, and shows the successor's current label while the successor exists;</li>
 *   <li>the history ruling: a deleted collection's audit trail still lists by its name from the latch; the rows with no id are shown
 *       apart, marked, never as the current holder's own and never under the collection that was renamed.</li>
 * </ul>
 */
class Rdr229P35cSharedLabelAndHistoryTest extends AtomicWriteTestBase {

    private static final String MODEL = "minilm-l6-v2-384";
    private final AtomicBoolean latched = new AtomicBoolean();
    private CatalogRepository gated;

    @BeforeAll
    void dropTheNameKeys() throws Exception {
        gated = new CatalogRepository(tenantScope, latched::get, dev.nexus.service.db.RenameGuard.open());
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext d = DSL.using(su, SQLDialect.POSTGRES);
            RegistryRenameSupport.dropForeignKeysOntoTheRegistry(d);
            PgContainerHelper.dropConstraint(su, CATALOG_COLLECTIONS, "catalog_collections_pk");
            PgContainerHelper.dropConstraint(su, CATALOG_COLLECTIONS, "catalog_collections_tenant_name_model_uq");
        }
    }

    private String label(String tag) {
        return "knowledge__" + tag + seq.incrementAndGet() + "__" + MODEL + "__v1";
    }

    private <T> T su(java.util.function.Function<DSLContext, T> body) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return body.apply(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    private String suString(java.util.function.Function<DSLContext, String> body) throws Exception {
        return su(body);
    }

    private Long suLong(java.util.function.Function<DSLContext, Long> body) throws Exception {
        return su(body);
    }

    private List<Long> suLongs(java.util.function.Function<DSLContext, List<Long>> body) throws Exception {
        return su(body);
    }

    private long registerRow(String name, String supersededBy, Long supersededById, String owner) throws Exception {
        return su(d -> {
            d.insertInto(CATALOG_COLLECTIONS)
                .set(CATALOG_COLLECTIONS.TENANT_ID, TENANT).set(CATALOG_COLLECTIONS.NAME, name)
                .set(CATALOG_COLLECTIONS.CONTENT_TYPE, "knowledge").set(CATALOG_COLLECTIONS.OWNER_ID, owner)
                .set(CATALOG_COLLECTIONS.EMBEDDING_MODEL, MODEL).set(CATALOG_COLLECTIONS.MODEL_VERSION, "v1")
                .set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, "live").set(CATALOG_COLLECTIONS.SUPERSEDED_BY, supersededBy)
                .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, supersededById).execute();
            return d.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name))
                .orderBy(CATALOG_COLLECTIONS.COLLECTION_ID.desc()).limit(1).fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
        });
    }

    /** A tombstone of {@code name} that names no successor that exists: its id is its own (R7), the text is the dead successor's. */
    private long registerRetiredNoSuccessor(String name, String deadSuccessor, String owner) throws Exception {
        long id = registerRow(name, "", null, owner);
        su(d -> d.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, deadSuccessor)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, id).where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT))
            .and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(id)).execute());
        return id;
    }

    // ---- Q3: a label shared by a live row and a tombstone --------------------------------------------------------------

    @Test
    void theResolversOfALabelAnswerTheLiveRow_andNeverThrowOnTwoRows() throws Exception {
        String shared = label("shared");
        long tombstone = registerRetiredNoSuccessor(shared, "gone-" + seq.incrementAndGet(), "sharedowner-t");
        long live = registerRow(shared, "", null, "sharedowner-l");
        assertThat(live).isGreaterThan(tombstone);
        for (boolean gate : new boolean[] {false, true}) {
            latched.set(gate);

            assertThat(((Number) gated.getCollection(TENANT, shared).get("collection_id")).longValue())
                .as("gate " + gate + ": getCollection answers the live row").isEqualTo(live);
            java.util.Optional<String> state = tenantScope.withTenant(TENANT, ctx -> CollectionRegistry.lifecycleState(ctx, TENANT, shared));
            assertThat(state.isPresent()).as("lifecycleState reads one row").isTrue();
        }
    }

    @Test
    void theRegistryMissReadAnswersTheLiveRowOfASharedLabel_whicheverRowWasWrittenLast() throws Exception {
        for (boolean tombstoneLast : new boolean[] {false, true}) {
            String shared = label("fill" + tombstoneLast);
            long live, tombstone;
            if (tombstoneLast) {
                live = registerRow(shared, "", null, "fillowner-l" + seq.incrementAndGet());
                tombstone = registerRetiredNoSuccessor(shared, "gone-" + seq.incrementAndGet(), "fillowner-t" + seq.incrementAndGet());
            } else {
                tombstone = registerRetiredNoSuccessor(shared, "gone-" + seq.incrementAndGet(), "fillowner-t" + seq.incrementAndGet());
                live = registerRow(shared, "", null, "fillowner-l" + seq.incrementAndGet());
            }
            assertThat(tombstone).as("non-vacuity: two rows of one label").isNotEqualTo(live);

            CollectionRegistry.evictAll();   // a cache miss: the read under test is the fill
            long found = tenantScope.withTenant(TENANT, ctx -> CollectionRegistry.require(ctx, TENANT, shared)).collectionId();

            assertThat(found).as("the live row answers, whichever of the two has the higher id (tombstone last: " + tombstoneLast + ")")
                .isEqualTo(live);
        }
    }

    @Test
    void theSuccessorIdOfASharedLabelIsAFreshDrawNowThatTheNameKeyIsGone() throws Exception {
        String shared = label("succ");
        long tombstone = registerRetiredNoSuccessor(shared, "gone-" + seq.incrementAndGet(), "succowner-t");
        registerRow(shared, "", null, "succowner-l");

        long drawn = tenantScope.withTenant(TENANT, ctx -> CollectionRename.successorId(ctx, TENANT, shared));

        assertThat(drawn).as("no TooManyRows, and not the tombstone's id: the new row gets its own").isNotEqualTo(tombstone);
    }

    @Test
    void repointTombstonesMovesExactlyTheTombstonesPointingAtFromIdOntoToIdWithItsLabel() throws Exception {
        String from = label("rpfrom"), to = label("rpto"), other = label("rpother");
        long fromId = registerRow(from, "", null, "rpowner-f");
        long toId = registerRow(to, "", null, "rpowner-t");
        long otherId = registerRow(other, "", null, "rpowner-o");
        long x1 = registerRow(label("rpx"), from, fromId, "rpowner-x1");       // two tombstones name from
        long x2 = registerRow(label("rpx"), from, fromId, "rpowner-x2");
        long untouched = registerRow(label("rpu"), other, otherId, "rpowner-u");  // one names another row
        // P3.7's order, in one transaction: the registry row of fromId goes, then its tombstones are re-pointed (no foreign key blocks it)
        int moved = tenantScope.withTenant(TENANT, ctx -> {
            ctx.deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT))
                .and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(fromId)).execute();
            return CollectionTombstones.repointTombstones(ctx, TENANT, fromId, toId);
        });

        assertThat(moved).isEqualTo(2);
        for (long x : new long[] {x1, x2}) {
            assertThat(supersededId(x)).as("onto toId").isEqualTo(toId);
            assertThat(suString(d -> d.select(CATALOG_COLLECTIONS.SUPERSEDED_BY).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(x))
                .fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_BY))).as("with toId's label").isEqualTo(to);
        }
        assertThat(supersededId(untouched)).as("a tombstone of another row is untouched").isEqualTo(otherId);
        assertThat(suLongs(d -> d.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT))
            .and(CATALOG_COLLECTIONS.COLLECTION_ID.in(toId, otherId, untouched)).fetch(CATALOG_COLLECTIONS.COLLECTION_ID)))
            .as("no other row of the tenant is written").hasSize(3);
    }

    private Long supersededId(long collectionId) throws Exception {
        return su(d -> d.select(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(collectionId))
            .fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID));
    }

    @Test
    void theDocumentsOfASharedLabelAreTheLiveCollectionsFromTheLatch() throws Exception {
        String shared = label("docs");
        long tombstone = registerRetiredNoSuccessor(shared, "gone-" + seq.incrementAndGet(), "docowner-t");
        long live = registerRow(shared, "", null, "docowner-l");
        su(d -> {
            for (var e : List.of(Map.entry("old.doc." + seq.incrementAndGet(), tombstone), Map.entry("new.doc." + seq.incrementAndGet(), live))) {
                d.insertInto(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.TENANT_ID, TENANT).set(CATALOG_DOCUMENTS.TUMBLER, e.getKey())
                    .set(CATALOG_DOCUMENTS.TITLE, e.getKey()).set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, shared)
                    .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, e.getValue()).execute();
            }
            return null;
        });

        latched.set(false);
        assertThat(gated.documentsByCollection(TENANT, shared, 0, 0)).as("the name path: both, as today").hasSize(2);
        latched.set(true);
        assertThat(gated.documentsByCollection(TENANT, shared, 0, 0)).as("the id path: the live collection's own").hasSize(1)
            .allSatisfy(m -> assertThat(String.valueOf(m.get("tumbler"))).startsWith("new.doc."));
    }

    // ---- Q4: the wire stays byte-identical -----------------------------------------------------------------------------

    @Test
    void aRetiredNoSuccessorRowsWireOutputIsTheStoredText_andALiveRowsIsEmpty_beforeAndAfterTheLatch() throws Exception {
        String dead = "the-dead-successor-" + seq.incrementAndGet();
        String retired = label("retired");
        registerRetiredNoSuccessor(retired, dead, "wireowner-r");
        String live = label("wirelive");
        registerRow(live, "", null, "wireowner-l");
        for (boolean gate : new boolean[] {false, true}) {
            latched.set(gate);
            assertThat(gated.getCollection(TENANT, retired).get("superseded_by")).as("gate " + gate + ": the stored text, not its own name")
                .isEqualTo(dead);
            assertThat(gated.getCollection(TENANT, live).get("superseded_by")).as("gate " + gate + ": live").isEqualTo("");
            assertThat(gated.listCollections(TENANT, null, null)).filteredOn(m -> retired.equals(m.get("name")))
                .extracting(m -> m.get("superseded_by")).containsExactly(dead);
            assertThat(gated.collectionForTuple(TENANT, "knowledge", "wireowner-l", MODEL).get("superseded_by")).isEqualTo("");
        }
    }

    @Test
    void aTombstoneWhoseSuccessorStillExistsShowsTheSuccessorsCurrentLabelFromTheLatch() throws Exception {
        String successor = label("succlabel");
        long successorId = registerRow(successor, "", null, "wireowner-s");
        String tombstone = label("pointsat");
        registerRow(tombstone, successor, successorId, "wireowner-p");
        String renamed = label("succrenamed");
        su(d -> {
            RegistryRenameSupport.rename(d, TENANT, successor, renamed);
            return null;
        });

        latched.set(false);
        assertThat(gated.getCollection(TENANT, tombstone).get("superseded_by")).as("the name path: the stored text").isEqualTo(successor);
        latched.set(true);
        assertThat(gated.getCollection(TENANT, tombstone).get("superseded_by")).as("by id: the successor's current label").isEqualTo(renamed);
    }

    // ---- the history ruling: gc_audit -----------------------------------------------------------------------------------

    private void audit(String collection) throws Exception {
        tenantScope.withTenant(TENANT, ctx -> CatalogRepository.insertGcAuditRow(ctx, TENANT, "test_op", collection, "test", false,
            List.of(Chash.ofText(collection + seq.incrementAndGet()).toHex()), Map.of()));
    }

    private List<String> listed(String collection) {
        return gated.listGcAudit(TENANT, collection, null, 100, 0).stream().map(m -> m.get("collection") + (m.containsKey("pre_id_label_history") ? "!pre" : "")).toList();
    }

    @Test
    void aDeletedCollectionsAuditTrailStillListsByItsNameFromTheLatch() throws Exception {
        String gone = label("deleted");
        registerRow(gone, "", null, "histowner-d");
        audit(gone);
        su(d -> d.deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(gone)).execute());

        latched.set(false);
        assertThat(listed(gone)).as("the name path").containsExactly(gone);
        latched.set(true);
        assertThat(listed(gone)).as("by name, the id no live row holds").containsExactly(gone);
    }

    @Test
    void afterARenameAndAReusedLabel_theNewHoldersHistoryIsItsOwn_andTheOldRowsAreOnlyMarkedPreIdLabelHistory() throws Exception {
        String a = label("histA");
        registerRow(a, "", null, "histowner-a");
        audit(a);                                         // stamped with id1
        audit(a);
        su(d -> d.update(GC_AUDIT).set(GC_AUDIT.COLLECTION_ID, (Long) null).where(GC_AUDIT.TENANT_ID.eq(TENANT))
            .and(GC_AUDIT.COLLECTION.eq(a)).and(GC_AUDIT.ID.eq(d.select(DSL.max(GC_AUDIT.ID)).from(GC_AUDIT)
                .where(GC_AUDIT.TENANT_ID.eq(TENANT)).and(GC_AUDIT.COLLECTION.eq(a)).fetchOne(0, Long.class))).execute());  // one row pre-id
        String b = label("histB");
        su(d -> {
            RegistryRenameSupport.rename(d, TENANT, a, b);
            return null;
        });
        registerRow(a, "", null, "histowner-a2");        // a new collection takes label A
        audit(a);                                         // stamped with id2

        latched.set(true);

        assertThat(listed(a)).as("the new A: its own row, and the pre-id row only as marked label history; never id1's stamped row")
            .containsExactlyInAnyOrder(a, a + "!pre");
        assertThat(listed(b)).as("B (id1): its stamped row under its current label; the pre-id row is not shown under B")
            .containsExactly(b);
    }

    // ---- writes into a collection retired with data still in it (linda 2026-11-23, Q3 corrected) -----------------------

    @Test
    void aDocumentHomedAtASupersededCollectionIsUpsertedWithTheTombstonesId() throws Exception {
        String retired = label("superseded");
        long tombstone = registerRetiredNoSuccessor(retired, "gone-" + seq.incrementAndGet(), "supowner");
        String tumbler = "sup.doc." + seq.incrementAndGet();
        latched.set(true);

        gated.upsertDocument(TENANT, Map.of("tumbler", tumbler, "title", "homed at a tombstone", "content_type", "knowledge",
            "corpus", "knowledge", "physical_collection", retired, "chunk_count", 0));

        assertThat(suLong(d -> d.select(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))
            .fetchOne(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID))).as("stamped with the tombstone's id, not NULL").isEqualTo(tombstone);
    }

    @Test
    void aFidelityImportOfManifestRowsIntoASupersededCollectionSucceeds() throws Exception {
        String retired = label("importsup");
        long tombstone = registerRetiredNoSuccessor(retired, "gone-" + seq.incrementAndGet(), "impowner");
        String tumbler = "imp.doc." + seq.incrementAndGet();
        String hex = Chash.ofText("import " + tumbler).toHex();
        su(d -> {
            PgContainerHelper.ensureTenantPartitions(d, TENANT);   // the registry rows here are inserted directly, not through insertCollection
            PgContainerHelper.insertChunks(d, TENANT, retired, List.of(hex), List.of("imported text"), List.of(new float[384]), List.of(Map.of()));
            return null;
        });
        gated.upsertDocument(TENANT, Map.of("tumbler", tumbler, "title", "import target", "content_type", "knowledge",
            "corpus", "knowledge", "physical_collection", retired, "chunk_count", 1));
        latched.set(true);

        int written = gated.importChunksBatch(TENANT, tumbler, retired, List.of(Map.of("position", 0, "chash", hex, "chunk_index", 0)));

        assertThat(written).isEqualTo(1);
        assertThat(suLongs(d -> d.select(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID)
            .from(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS)
            .where(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(tumbler))
            .fetch(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID))).containsExactly(tombstone);
    }

    @Test
    void aHistoryWriteForASupersededCollectionStampsTheTombstonesId() throws Exception {
        String retired = label("histsup");
        long tombstone = registerRetiredNoSuccessor(retired, "gone-" + seq.incrementAndGet(), "histsupowner");

        audit(retired);

        assertThat(suLongs(d -> d.select(GC_AUDIT.COLLECTION_ID).from(GC_AUDIT).where(GC_AUDIT.TENANT_ID.eq(TENANT))
            .and(GC_AUDIT.COLLECTION.eq(retired)).fetch(GC_AUDIT.COLLECTION_ID))).containsExactly(tombstone);
    }

    // ---- no gate flip-back in these readers ---------------------------------------------------------------------------

    @Test
    void theLabelHelpersReadTheGateWhenTheyBuildTheClause() {
        var live = CollectionIdReads.live(Gate3.never(), CATALOG_COLLECTIONS.SUPERSEDED_BY, CATALOG_COLLECTIONS.SUPERSEDED_BY_ID);
        var latchedLive = CollectionIdReads.live(() -> true, CATALOG_COLLECTIONS.SUPERSEDED_BY, CATALOG_COLLECTIONS.SUPERSEDED_BY_ID);
        assertThat(live.toString()).contains("superseded_by\" = ''");
        assertThat(latchedLive.toString()).contains("superseded_by_id\" is null");
    }
}
