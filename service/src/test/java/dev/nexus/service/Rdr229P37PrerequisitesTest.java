// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TaxonomyRepository;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.jooq.nexus.Routines;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.function.Function;

/**
 * RDR-229 P3.7's prerequisites (nexus-p2m1x.25, elsinore's list of 2026-11-23): what the readers and writers P3.5b and P3.5c switched do
 * when P3.7's one-row rename has changed the registry row's label and left every child row on the old one. The rename is simulated the
 * way P3.7 will do it: the registry row takes the new name in place and keeps its id, and nothing else is written. The database is
 * walked to HEAD, the stamps are completed and the form A changesets applied ({@link Rdr229CompleteStamps#completeAndApplyFormA}), then
 * the foreign keys onto the registry's name are dropped, which P3.7's name-key drop does.
 *
 * <ul>
 *   <li>{@code assign_from_chashes} under the NEW label finds the chunks of the collection by its id and assigns them;</li>
 *   <li>the owner trigger (form A, after the stamps complete) derives a renamed collection's owner from a document that names it
 *       by id under a stale name;</li>
 *   <li>the canary ring upsert under the new label refreshes the ring row the old label wrote: one row, no 23505.</li>
 * </ul>
 *
 * <p>D1 (the departed-span clear's held pair by id) is {@code db.Rdr229P37ClearDisplacedByIdTest}, in the package of the helper it drives.
 */
class Rdr229P37PrerequisitesTest extends AtomicWriteTestBase {

    private static final String MODEL = "minilm-l6-v2-384";
    private static final Gate3 LATCHED = () -> true;
    private static final String TS = "2026-11-23T00:00:00Z";

    @BeforeAll
    void formAThenTheNameKeysGo() throws Exception {
        try (Connection su = pg.createConnection("")) {
            Rdr229CompleteStamps.completeAndApplyFormA(su);
        }
        su(d -> {
            RegistryRenameSupport.dropForeignKeysOntoTheRegistry(d);
            return null;
        });
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.dropCatalogChunksFk(su);
            for (int dim : new int[] {384}) {
                PgContainerHelper.grantExecuteOnFunction(su, "nexus.assign_from_chashes_" + dim
                    + "(text, text[], boolean, text, text)", SVC_ROLE);
            }
        }
    }

    // ---- fixture ----------------------------------------------------------------------------------------------------------

    private <T> T su(Function<DSLContext, T> body) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return body.apply(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    private String label(String tag) {
        return "code__p37" + tag + seq.incrementAndGet() + "__" + MODEL + "__v1";
    }

    private long idOf(String name) throws Exception {
        return su(d -> d.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID));
    }

    private String ownerOfId(long id) throws Exception {
        return su(d -> d.select(CATALOG_COLLECTIONS.OWNER_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(id))
            .fetchOne(CATALOG_COLLECTIONS.OWNER_ID));
    }

    /** P3.7's one-row rename: the registry row takes the new label, keeps its id, and nothing else is written. */
    private String rename(String old) throws Exception {
        String neu = old.replaceFirst("^code__p37", "code__p37n");
        su(d -> {
            RegistryRenameSupport.rename(d, TENANT, old, neu);
            return null;
        });
        return neu;
    }

    private static float[] unit() {
        float[] v = new float[384];
        v[0] = 1f;
        return v;
    }

    private void seedChunks(String collection, List<String> hexes, Map<String, Object> meta) throws Exception {
        List<String> texts = new ArrayList<>();
        List<float[]> vecs = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (String h : hexes) {
            texts.add("p37 text " + h);
            vecs.add(unit());
            metas.add(meta);
        }
        su(d -> {
            PgContainerHelper.insertChunks(d, TENANT, collection, hexes, texts, vecs, metas);
            return null;
        });
    }

    // ---- 1: assign_from_chashes under the new label -----------------------------------------------------------------------------

    @Test
    void assignFromChashes_underTheNewLabel_isAcceptedWithTheChunkNameKeyInPlace_theTriggerStampsTheChunksName() throws Exception {
        String old = label("as");
        registerCollection(old);
        List<String> hexes = List.of(Chash.ofText("p37-as-a-" + seq.incrementAndGet()).toHex(), Chash.ofText("p37-as-b-" + seq.incrementAndGet()).toHex());
        seedChunks(old, hexes, Map.of());
        var taxonomy = new TaxonomyRepository(tenantScope, LATCHED);
        long topic = taxonomy.insertTopic(TENANT, "p37-topic", null, old, 0, TS, null);
        long id = idOf(old);
        su(d -> d.insertInto(TAXONOMY_CENTROIDS)
            .set(TAXONOMY_CENTROIDS.TENANT_ID, TENANT).set(TAXONOMY_CENTROIDS.COLLECTION, old)
            .set(TAXONOMY_CENTROIDS.COLLECTION_ID, id).set(TAXONOMY_CENTROIDS.TOPIC_ID, topic)
            .set(TAXONOMY_CENTROIDS.EMBEDDING_MODEL, MODEL).set(TAXONOMY_CENTROIDS.LABEL, "p37-centroid")
            .set(TAXONOMY_CENTROIDS.EMBEDDING_384, Vector.of(unit())).execute());
        String neu = rename(old);
        int stale = su(d -> d.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(neu))));
        assertThat(stale).as("precondition: no chunk row carries the new label").isZero();

        // P3.5b's assign_from_chashes_384 writes source_collection = the caller's label, and topic_assignments_chunk_fk keys that name to the chunk's
        // own: a chunk still named old would refuse the new label. P3.7's vectors-079 BEFORE INSERT OR UPDATE trigger sets source_collection from the
        // chunk row the assignment references, so the key stays in place and accepts the write. The key is asserted present (non-vacuity), never dropped.
        boolean fk = su(d -> d.fetchExists(DSL.table(DSL.name("pg_catalog", "pg_constraint")),
            DSL.field(DSL.name("conname"), String.class).eq("topic_assignments_chunk_fk")));
        assertThat(fk).as("non-vacuity: topic_assignments_chunk_fk is in place").isTrue();

        Map<String, Object> out = taxonomy.assignFromChashes(TENANT, neu, hexes, false);

        assertThat(out.get("assigned")).as("both chunks are found by id under the new label: %s", out).isEqualTo(2);
        assertThat((List<?>) out.get("unmatched_chashes")).as("none is reported unmatched").isEmpty();
        int rows = su(d -> d.fetchCount(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID.eq(TENANT)
            .and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID.eq(id)).and(TOPIC_ASSIGNMENTS.TOPIC_ID.eq(topic))));
        assertThat(rows).as("and the assignments carry the collection's id").isEqualTo(2);
        // the foreign key accepted the write because the vectors-079 trigger stamped the chunk's stored name, not the label the caller named
        List<String> sourceCollections = su(d -> d.selectDistinct(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION).from(TOPIC_ASSIGNMENTS)
            .where(TOPIC_ASSIGNMENTS.TENANT_ID.eq(TENANT)).and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID.eq(id)).and(TOPIC_ASSIGNMENTS.TOPIC_ID.eq(topic))
            .fetch(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION));
        assertThat(sourceCollections).as("source_collection is the chunks' stored (old) name %s, set by the trigger, not the caller's %s", old, neu)
            .containsExactly(old);
        int stillOld = su(d -> d.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(old)).and(CHUNKS.COLLECTION_ID.eq(id))));
        assertThat(stillOld).as("the chunks still carry the old name").isEqualTo(2);
    }

    // ---- 2: the owner trigger by id (form A) for a renamed collection's document ----------------------------------------------------

    @Test
    void ownerTrigger_afterTheStampsComplete_derivesTheOwnerOfARenamedCollectionFromADocumentThatNamesItById() throws Exception {
        String old = label("ow");
        registerCollection(old);
        long id = idOf(old);
        String slug = ownerOfId(id);
        assertThat(slug).as("non-vacuity: a slug owner, replaceable, and no document derived one yet").doesNotStartWith("1-");
        String neu = rename(old);
        assertThat(idOf(neu)).as("a rename keeps the id").isEqualTo(id);
        String tumbler = "1.15." + (4200 + seq.incrementAndGet());

        su(d -> d.insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT).set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
            .set(CATALOG_DOCUMENTS.TITLE, "p37 renamed document")
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, old)   // stale: the registry row is called neu now
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, id).execute());

        assertThat(ownerOfId(id)).as("the owner derives from the registry row of the document's collection_id, not from its stale name")
            .isEqualTo("1-15");
    }

    // ---- 3: the canary ring upsert under the new label -------------------------------------------------------------------------------

    @Test
    void canaryRingUpsert_underTheNewLabel_refreshesTheRowTheOldLabelWrote_oneRow() throws Exception {
        String old = label("cn");
        registerCollection(old);
        long id = idOf(old);
        String vector = "[" + String.join(",", java.util.Collections.nCopies(384, "0.5")) + "]";
        byte[] digest = java.security.MessageDigest.getInstance("SHA-256").digest(("p37-canary-" + seq.incrementAndGet()).getBytes());
        assertThat(upsertCanary(old, vector, digest)).as("the ring row is written under the old label").isEqualTo(1);
        String neu = rename(old);
        var ring = DSL.table(DSL.name("nexus", "canary_query_ring"));
        var mine = DSL.field(DSL.name("tenant_id"), String.class).eq(TENANT)
            .and(DSL.field(DSL.name("vector_digest"), byte[].class).eq(digest));
        int before = su(d -> d.fetchCount(ring, mine));
        assertThat(before).as("precondition: one ring row, carrying the collection's id")
            .isEqualTo(1);

        int written = upsertCanary(neu, vector, digest);

        assertThat(written).as("the new label's entry is written: it refreshes the row the id names, no 23505").isEqualTo(1);
        int after = su(d -> d.fetchCount(ring, mine));
        int byId = su(d -> d.fetchCount(ring, mine.and(DSL.field(DSL.name("collection_id"), Long.class).eq(id))));
        assertThat(after).as("one ring row for the digest, not one per label").isEqualTo(1);
        assertThat(byId).as("and it is the collection's").isEqualTo(1);
    }

    private int upsertCanary(String label, String vector, byte[] digest) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            Integer n = Routines.canaryQueryRingUpsert(DSL.using(c, SQLDialect.POSTGRES).configuration(), TENANT,
                new String[] {label}, new String[] {MODEL}, new String[] {vector}, new byte[][] {digest});
            return n == null ? 0 : n;
        }
    }
}
