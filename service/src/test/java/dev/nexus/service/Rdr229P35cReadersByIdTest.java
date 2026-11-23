// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.assertj.core.api.Assertions.within;

import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.db.AspectRepository;
import dev.nexus.service.db.ChashRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CollectionKey;
import dev.nexus.service.db.ReembedJobRepository;
import dev.nexus.service.db.ReembedSourceReader;
import dev.nexus.service.db.RemapRepository;
import dev.nexus.service.db.TaxonomyRepository;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.vectors.ReaperRepository;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25), the taxonomy / aspect / remap / chash / reaper / re-embed group: a reader of a collection name
 * column reads the id once gate 3 has latched on the install, and keeps reading the name before.
 *
 * <p><b>The fixture.</b> A collection is registered (id Y, name {@code old}), rows are written under it by the repository's own
 * writers (name {@code old}, id Y), and the registry row is then renamed in place to {@code neu} with FK triggers off
 * ({@code session_replication_role = replica}, so the name foreign keys' ON UPDATE CASCADE does not rewrite the children). That is the
 * state P3.7's one-row rename leaves: the registry row is Y/{@code neu}, every child row still says {@code old} and Y. A reader that
 * names the collection by its name finds the rows under {@code old} only; one that names it by its id finds them under {@code neu}
 * only, and reads the registry's current label back as the collection name.
 *
 * <p>Each repository gets one test with the same four assertions: gate 3 not holding, the rows are found by {@code old} and not by
 * {@code neu}; gate 3 latched (a fresh repository over {@code () -> true}), by {@code neu} and not by {@code old}, and the collection
 * a reader reports is {@code neu}. {@link #aNullIdIsNoCollection} pins linda's 2026-11-20 ruling on {@link CollectionKey} itself.
 */
class Rdr229P35cReadersByIdTest extends AtomicWriteTestBase {

    private static final String MODEL = "minilm-l6-v2-384";
    private static final Gate3 LATCHED = () -> true;

    // ---- fixture -----------------------------------------------------------------------------------------------

    private String register(String tag) {
        String c = "code__p35cr" + tag + seq.incrementAndGet() + "__" + MODEL + "__v1";
        registerCollection(c);
        return c;
    }

    /** Renames the registry row of {@code old} in place and returns the new name; the children keep {@code old}. */
    private String renameRegistryRow(String old) throws Exception {
        String neu = old.replaceFirst("code__p35cr", "code__p35crn");
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.set(DSL.name("session_replication_role"), DSL.inline("replica")).execute();
            try {
                ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, neu)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(old)).execute();
            } finally {
                ctx.set(DSL.name("session_replication_role"), DSL.inline("origin")).execute();
            }
        }
        return neu;
    }

    private void insertChunk(String collection, Chash chash) throws Exception {
        float[] v = new float[384];
        v[0] = 1f;
        var row = new LinkedHashMap<Field<?>, Object>();
        row.put(CHUNKS.TENANT_ID, TENANT);
        row.put(CHUNKS.COLLECTION, collection);
        row.put(CHUNKS.CHASH, chash.toBytes());
        row.put(CHUNKS.EMBEDDING_MODEL, MODEL);
        row.put(CHUNKS.COLLECTION_ID, registryId(collection));
        row.put(CHUNKS.CHUNK_TEXT, "p35c reader text " + chash.toHex());
        row.put(CHUNKS.EMBEDDING_384, Vector.of(v));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).insertInto(CHUNKS).set(row).execute();
        }
    }

    private Long registryId(String name) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name))
                .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
        }
    }

    // ---- TaxonomyRepository: topics, taxonomy_meta, topic_assignments -------------------------------------------

    @Test
    void taxonomyRepository_readsTheIdFromGate3() throws Exception {
        var byName = new TaxonomyRepository(tenantScope);
        var byId = new TaxonomyRepository(tenantScope, LATCHED);
        String old = register("tx");
        Chash chash = Chash.ofText("p35c-taxonomy-" + seq.incrementAndGet());
        insertChunk(old, chash);
        long topic = byName.insertTopic(TENANT, "p35c-root-" + seq.incrementAndGet(), null, old, 0, null, null);
        byName.recordDiscoverCount(TENANT, old, 7, "2026-06-01T10:00:00Z");
        assertThat(byName.assignTopic(TENANT, chash.toHex(), topic, "projection", 0.9, old, null)).isTrue();
        String neu = renameRegistryRow(old);

        // gate 3 not holding: the name path, unchanged
        assertThat(byName.getAllTopics(TENANT, old)).hasSize(1);
        assertThat(byName.getAllTopics(TENANT, neu)).isEmpty();
        assertThat(byName.getLastDiscoverDocCount(TENANT, old)).contains(7);
        assertThat(byName.getLastDiscoverDocCount(TENANT, neu)).isEmpty();
        assertThat(byName.getProjectionCountsByCollection(TENANT)).extracting(m -> m.get("source_collection")).contains(old);
        assertThat(byName.topTopicsForCollection(TENANT, old, 5)).hasSize(1);
        assertThat(byName.topTopicsForCollection(TENANT, neu, 5)).isEmpty();

        // gate 3 latched: the id path, and the registry's current label is what is reported
        var topics = byId.getAllTopics(TENANT, neu);
        assertThat(topics).hasSize(1);
        assertThat(topics.get(0).get("collection")).isEqualTo(neu);
        assertThat(byId.getAllTopics(TENANT, old)).isEmpty();
        assertThat(byId.getDistinctCollections(TENANT)).contains(neu).doesNotContain(old);
        assertThat(byId.getLastDiscoverDocCount(TENANT, neu)).contains(7);
        assertThat(byId.getLastDiscoverDocCount(TENANT, old)).isEmpty();
        assertThat(byId.getLastDiscoverStamps(TENANT, List.of(neu))).extracting(m -> m.get("collection")).containsExactly(neu);
        assertThat(byId.getLastDiscoverStamps(TENANT, List.of(old))).isEmpty();
        assertThat(byId.getProjectionCountsByCollection(TENANT)).extracting(m -> m.get("source_collection"))
            .contains(neu).doesNotContain(old);
        assertThat(byId.topTopicsForCollection(TENANT, neu, 5)).hasSize(1);
        assertThat(byId.topTopicsForCollection(TENANT, old, 5)).isEmpty();
        assertThat(byId.chunkGroundedIn(TENANT, chash.toHex(), neu)).hasValueSatisfying(v -> assertThat(v).isCloseTo(0.9, within(1e-6)));
        assertThat(byId.chunkGroundedIn(TENANT, chash.toHex(), old)).isEmpty();
        assertThat(byId.getAssignmentDetails(TENANT, List.of(chash.toHex()))).extracting(m -> m.get("source_collection"))
            .containsExactly(neu);
        assertThat(byId.getAssignmentDetails(TENANT, List.of(chash.toHex()))).hasSize(1);

        // the purge by name of a label that names nothing removes nothing; by the label that names the id it removes the rows
        assertThat(byId.purgeCollection(TENANT, old).get("topics")).isZero();
        assertThat(byId.purgeCollection(TENANT, neu).get("topics")).isEqualTo(1);
    }

    // ---- AspectRepository: document_aspects, document_highlights ------------------------------------------------

    @Test
    void aspectRepository_readsTheIdFromGate3() throws Exception {
        var byName = new AspectRepository(tenantScope);
        var byId = new AspectRepository(tenantScope, LATCHED);
        String old = register("as");
        String doc = "p35c.as." + seq.incrementAndGet();
        registerDoc(doc, old);
        byName.upsertAspect(TENANT, aspect(old, "paper.pdf", doc));
        assertThat(byName.upsertHighlight(TENANT, Map.of("doc_id", doc, "ingested_at", "2026-06-01T10:00:00Z",
            "highlights_md", "h", "collection", old))).isTrue();
        String neu = renameRegistryRow(old);

        assertThat(byName.getAspect(TENANT, old, "paper.pdf")).isPresent();
        assertThat(byName.getAspect(TENANT, neu, "paper.pdf")).isEmpty();
        assertThat(byName.listByCollection(TENANT, neu, 10, 0)).isEmpty();
        assertThat(byName.getHighlight(TENANT, doc).orElseThrow().get("collection")).isEqualTo(old);

        assertThat(byId.getAspect(TENANT, neu, "paper.pdf")).isPresent();
        assertThat(byId.getAspect(TENANT, neu, "paper.pdf").orElseThrow().get("collection")).isEqualTo(neu);
        assertThat(byId.getAspect(TENANT, old, "paper.pdf")).isEmpty();
        assertThat(byId.listByCollection(TENANT, neu, 10, 0)).hasSize(1);
        assertThat(byId.listByCollection(TENANT, old, 10, 0)).isEmpty();
        assertThat(byId.getHighlight(TENANT, doc).orElseThrow().get("collection")).isEqualTo(neu);
        assertThat(byId.deleteAspect(TENANT, old, "paper.pdf")).isZero();
        assertThat(byId.deleteAspect(TENANT, neu, "paper.pdf")).isEqualTo(1);
    }

    static Map<String, Object> aspect(String collection, String path, String docId) {
        return new LinkedHashMap<>(Map.of("collection", collection, "source_path", path,
            "problem_formulation", "p", "proposed_method", "m", "experimental_results", "r", "confidence", 0.9,
            "extracted_at", "2026-06-01T10:00:00.000000Z", "model_version", "v1", "extractor_name", "e", "doc_id", docId));
    }

    // ---- RemapRepository: chash_remap ---------------------------------------------------------------------------

    @Test
    void remapRepository_readsTheIdFromGate3() throws Exception {
        var byName = new RemapRepository(tenantScope);
        var byId = new RemapRepository(tenantScope, LATCHED);
        String old = register("rm");
        String target = register("rt");
        byName.recordBatch(TENANT, List.of(new RemapRepository.RemapEntry(old, "old-id-" + seq.incrementAndGet(),
            Chash.ofText("p35c-remap-" + seq.get()).toHex(), target, "p35c")));
        String neu = renameRegistryRow(old);

        assertThat(byName.count(TENANT, old)).isEqualTo(1);
        assertThat(byName.count(TENANT, neu)).isZero();
        assertThat(byName.sourceCollections(TENANT)).contains(old).doesNotContain(neu);
        assertThat(byName.entriesForCollection(TENANT, neu)).isEmpty();

        assertThat(byId.count(TENANT, neu)).isEqualTo(1);
        assertThat(byId.count(TENANT, old)).isZero();
        assertThat(byId.sourceCollections(TENANT)).contains(neu).doesNotContain(old);
        assertThat(byId.entriesForCollection(TENANT, neu)).hasSize(1);
        assertThat(byId.entriesForCollection(TENANT, neu).get(0).get("target_collection")).isEqualTo(target);
        assertThat(byId.entriesForCollection(TENANT, old)).isEmpty();
        // the target is read by id too: rename it and the leg is cleared under its new label only
        String targetNew = renameRegistryRow(target);
        assertThat(byId.clearLeg(TENANT, neu, target)).isZero();
        assertThat(byId.entriesForCollection(TENANT, neu).get(0).get("target_collection")).isEqualTo(targetNew);
        assertThat(byId.clearLeg(TENANT, neu, targetNew)).isEqualTo(1);
    }

    // ---- ChashRepository, ReaperRepository: chunks -------------------------------------------------------------

    @Test
    void chashAndReaperRepositories_readTheIdFromGate3() throws Exception {
        var chashByName = new ChashRepository(tenantScope);
        var chashById = new ChashRepository(tenantScope, LATCHED);
        var reaperByName = new ReaperRepository(tenantScope);
        var reaperById = new ReaperRepository(tenantScope, LATCHED);
        String old = register("ch");
        Chash chash = Chash.ofText("p35c-chash-" + seq.incrementAndGet());
        insertChunk(old, chash);
        String neu = renameRegistryRow(old);

        assertThat(chashByName.countForCollection(TENANT, old)).isEqualTo(1);
        assertThat(chashByName.countForCollection(TENANT, neu)).isZero();
        assertThat(chashByName.distinctCollections(TENANT)).contains(old).doesNotContain(neu);
        assertThat(chashByName.registeredChashesForCollection(TENANT, neu)).isEmpty();
        assertThat(chashByName.lookup(TENANT, chash)).extracting(m -> m.get("collection")).containsExactly(old);
        assertThat(reaperByName.collectionsWithChunks(TENANT, Duration.ofSeconds(30))).contains(old).doesNotContain(neu);
        assertThat(reaperByName.describe(TENANT, old, List.of(chash.toHex()))).hasSize(1);
        assertThat(reaperByName.describe(TENANT, neu, List.of(chash.toHex()))).isEmpty();

        assertThat(chashById.countForCollection(TENANT, neu)).isEqualTo(1);
        assertThat(chashById.countForCollection(TENANT, old)).isZero();
        assertThat(chashById.distinctCollections(TENANT)).contains(neu).doesNotContain(old);
        assertThat(chashById.registeredChashesForCollection(TENANT, neu)).containsExactly(chash.toHex());
        assertThat(chashById.registeredChashesForCollection(TENANT, old)).isEmpty();
        assertThat(chashById.lookup(TENANT, chash)).extracting(m -> m.get("collection")).containsExactly(neu);
        assertThat(reaperById.collectionsWithChunks(TENANT, Duration.ofSeconds(30))).contains(neu).doesNotContain(old);
        assertThat(reaperById.describe(TENANT, neu, List.of(chash.toHex()))).hasSize(1);
        assertThat(reaperById.describe(TENANT, old, List.of(chash.toHex()))).isEmpty();
    }

    /** linda 2026-11-23: a chunk id no registry row holds is still a collection the chash path lists, under its stored name (ghost detection needs it). */
    @Test
    void chashDistinctCollections_listsADanglingIdUnderItsStoredName() throws Exception {
        var chashById = new ChashRepository(tenantScope, LATCHED);
        String live = register("dg-live"), ghost = register("dg-ghost");
        insertChunk(live, Chash.ofText("p35c-dg-live-" + seq.incrementAndGet()));
        insertChunk(ghost, Chash.ofText("p35c-dg-ghost-" + seq.incrementAndGet()));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.set(DSL.name("session_replication_role"), DSL.inline("replica")).execute();
            try {
                int deleted = ctx.deleteFrom(CATALOG_COLLECTIONS)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(ghost)).execute();
                assertThat(deleted).as("the registry no longer holds the ghost's id").isEqualTo(1);
            } finally {
                ctx.set(DSL.name("session_replication_role"), DSL.inline("origin")).execute();
            }
        }
        assertThat(chashById.distinctCollections(TENANT)).as("the dangling id is listed under its stored name").contains(live, ghost);
        assertThat(new ReaperRepository(tenantScope, LATCHED).collectionsWithChunks(TENANT, Duration.ofSeconds(30)))
            .as("the reaper lists it too").contains(live, ghost);
    }

    // ---- ReembedJobRepository, ReembedSourceReader --------------------------------------------------------------

    /** A job A to B holds B as a target. B is then renamed (the job row keeps "B" and B's id). A start from the renamed B is refused by id, accepted by name. */
    @Test
    void reembedJobRepository_readsTheIdFromGate3() throws Exception {
        for (boolean latched : new boolean[] {false, true}) {
            var jobs = new ReembedJobRepository(tenantScope, latched ? LATCHED : Gate3.never());
            String a = register("ja"), b = register("jb"), c = register("jc");
            jobs.start(TENANT, a, b, null, true, false);
            String bNew = renameRegistryRow(b);
            if (latched) {
                assertThatThrownBy(() -> jobs.start(TENANT, bNew, c, null, true, false))
                    .as("by id the renamed B is still the target of the job that is filling it")
                    .isInstanceOf(ReembedJobRepository.JobLockHeld.class);
            } else {
                assertThat(jobs.start(TENANT, bNew, c, null, true, false)).as("by name B2 is nobody's target").isNotNull();
            }
        }
    }

    @Test
    void reembedSourceReader_readsTheIdFromGate3() throws Exception {
        var byName = new ReembedSourceReader(tenantScope);
        var byId = new ReembedSourceReader(tenantScope, LATCHED);
        var aspects = new AspectRepository(tenantScope);
        String old = register("rs");
        String doc = "p35c.rs." + seq.incrementAndGet();
        registerDoc(doc, old);
        aspects.upsertAspect(TENANT, aspect(old, "paper.pdf", doc));
        String neu = renameRegistryRow(old);

        assertThat(byName.residue(TENANT, old).aspects()).isEqualTo(1);
        assertThat(byName.residue(TENANT, neu).aspects()).isZero();
        assertThat(byId.residue(TENANT, neu).aspects()).isEqualTo(1);
        assertThat(byId.residue(TENANT, old).aspects()).isZero();
    }

    // ---- P3.7 census (linda 2026-11-23): after a simulated one-row rename, a by-key write under the NEW label touches exactly that collection ----

    @Test
    void setSalientSentencesByKey_underTheNewLabel_touchesExactlyTheRenamedCollectionsRow() throws Exception {
        var latched = new AspectRepository(tenantScope, LATCHED);
        var byName = new AspectRepository(tenantScope);
        String old = register("sk"), other = register("sko");
        String docA = "p35c.sk.a." + seq.incrementAndGet(), docB = "p35c.sk.b." + seq.incrementAndGet();
        registerDoc(docA, old);
        registerDoc(docB, other);
        byName.upsertAspect(TENANT, aspect(old, "same.pdf", docA));
        byName.upsertAspect(TENANT, aspect(other, "same.pdf", docB));   // the same source path under another collection
        String neu = renameRegistryRow(old);

        assertThat(latched.setSalientSentencesByKey(TENANT, neu, "same.pdf", "[\"only-the-renamed\"]"))
            .as("one row: the renamed collection's, found by id under the new label").isEqualTo(1);

        assertThat(byName.getSalientSentences(TENANT, docA)).as("the renamed collection's row took it").contains("only-the-renamed");
        assertThat(byName.getSalientSentences(TENANT, docB)).as("the other collection's row of the same source path is untouched")
            .satisfiesAnyOf(v -> assertThat(v).isNull(), v -> assertThat(v).doesNotContain("only-the-renamed"));
        assertThat(latched.setSalientSentencesByKey(TENANT, old, "same.pdf", "[\"x\"]"))
            .as("the old label is no collection from the latch: it touches nothing").isZero();
    }

    // ---- a NULL id is no collection (linda 2026-11-20) ------------------------------------------------------------

    @Test
    void aNullIdIsNoCollection() throws Exception {
        String emptyDoc = "p35c.ni.empty." + seq.incrementAndGet();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // production holds '' in catalog_documents.physical_collection for notes: its id stays NULL (P3.1's NULLIF rule)
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE,
                CATALOG_DOCUMENTS.PHYSICAL_COLLECTION).values(TENANT, emptyDoc, "empty", "").execute();
            var one = CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.TUMBLER.eq(emptyDoc));
            var byName = CollectionKey.of(Gate3.never()).eq(TENANT, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION,
                CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, "");
            var byId = CollectionKey.of(LATCHED).eq(TENANT, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION,
                CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, "");
            assertThat(ctx.fetchCount(CATALOG_DOCUMENTS, one.and(byName))).as("by name the '' row matches today").isEqualTo(1);
            assertThat(ctx.fetchCount(CATALOG_DOCUMENTS, one.and(byId))).as("by id a NULL id is no collection, never the '' name").isZero();
            // the output label of a NULL id is NULL, never the '' name
            var label = CollectionKey.of(LATCHED).label(TENANT, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION,
                CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID);
            assertThat(ctx.select(label).from(CATALOG_DOCUMENTS).where(one).fetchOne(label)).isNull();
            // present() is false for it from gate 3
            assertThat(ctx.fetchCount(CATALOG_DOCUMENTS, one.and(CollectionKey.of(LATCHED)
                .present(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID)))).isZero();
            assertThat(ctx.fetchCount(CATALOG_DOCUMENTS, one.and(CollectionKey.of(Gate3.never())
                .present(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID)))).isZero();
        }
    }
}
