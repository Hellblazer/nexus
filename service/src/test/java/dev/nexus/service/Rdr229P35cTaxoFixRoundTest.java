// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHASH_REMAP;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.REEMBED_JOBS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_META;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.datamove.IdUniqueIndexPass;
import dev.nexus.service.db.AspectRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.ReembedJobRepository;
import dev.nexus.service.db.ReembedSourceReader;
import dev.nexus.service.db.RemapRepository;
import dev.nexus.service.db.RemapRepository.RemapEntry;
import dev.nexus.service.db.TaxonomyRepository;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.function.Function;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25), the review fix round for the taxonomy group (reviews of 21fcaffa5e): the latched upserts under the
 * new label, the FK-less ledgers (chash_remap, reembed_jobs) as history (linda 2026-11-23), the drift labels, the NULL-id polarity and
 * the readers the first test class could not falsify.
 *
 * <p>The fixture is {@link Rdr229P35cReadersByIdTest}'s: a registered collection with rows, then the registry row renamed in place
 * ({@code session_replication_role = replica}, so the children keep the old name and the id). The id unique indexes the latched upserts
 * arbitrate on are built from {@link IdUniqueIndexPass#SPECS} (table, index, columns, predicate), not retyped.
 */
class Rdr229P35cTaxoFixRoundTest extends AtomicWriteTestBase {

    private static final String MODEL = "minilm-l6-v2-384";
    private static final Gate3 LATCHED = () -> true;
    private static final String TS = "2026-06-01T10:00:00Z";

    // ---- fixture -----------------------------------------------------------------------------------------------

    private String register(String tag) {
        String c = "code__p35cf" + tag + seq.incrementAndGet() + "__" + MODEL + "__v1";
        registerCollection(c);
        return c;
    }

    private <T> T su(Function<DSLContext, T> body) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            return body.apply(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    private Long idOf(String name) throws Exception {
        return su(ctx -> ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID));
    }

    private String rename(String old) throws Exception {
        String neu = old.replaceFirst("^code__", "code__n");
        su(ctx -> {
            ctx.set(DSL.name("session_replication_role"), DSL.inline("replica")).execute();
            try {
                ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, neu)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(old)).execute();
            } finally {
                ctx.set(DSL.name("session_replication_role"), DSL.inline("origin")).execute();
            }
            return null;
        });
        return neu;
    }

    /** Deletes the registry row of {@code name} (the collection no longer exists); ledger rows that carry its id are left dangling. */
    private void deleteRegistryRow(String name) throws Exception {
        su(ctx -> ctx.deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT))
            .and(CATALOG_COLLECTIONS.NAME.eq(name)).execute());
    }

    /** The arbiter indexes P3.4's pass builds for {@code table}, created from its spec (typed DDL), if not there yet. */
    private void ensureArbiterIndex(String table) throws Exception {
        var spec = IdUniqueIndexPass.SPECS.stream().filter(s -> s.table().equals(table) && s.unique()).findFirst().orElseThrow();
        List<Field<?>> cols = java.util.Arrays.stream(spec.columns().split(",\\s*")).<Field<?>>map(c -> DSL.field(DSL.name(c))).toList();
        su(ctx -> {
            var step = ctx.createUniqueIndexIfNotExists(spec.index()).on(DSL.table(DSL.name("nexus", table)), cols);
            if (spec.predicate() != null) {
                assertThat(table).as("the only partial arbiter this test builds is topics'").isEqualTo("topics");
                step.where(TOPICS.PARENT_ID.isNull()).execute();
            } else {
                step.execute();
            }
            return null;
        });
    }

    private void insertChunk(String collection, Chash chash) throws Exception {
        float[] v = new float[384];
        v[0] = 1f;
        var row = new LinkedHashMap<Field<?>, Object>();
        row.put(CHUNKS.TENANT_ID, TENANT);
        row.put(CHUNKS.COLLECTION, collection);
        row.put(CHUNKS.CHASH, chash.toBytes());
        row.put(CHUNKS.EMBEDDING_MODEL, MODEL);
        row.put(CHUNKS.COLLECTION_ID, idOf(collection));
        row.put(CHUNKS.CHUNK_TEXT, "p35cf text " + chash.toHex());
        row.put(CHUNKS.EMBEDDING_384, Vector.of(v));
        su(ctx -> ctx.insertInto(CHUNKS).set(row).execute());
    }

    /** P3.4's arbiter indexes exist before any latched write (the id arbiter needs them), whatever order the tests run in. */
    @org.junit.jupiter.api.BeforeAll
    void buildArbiterIndexes() throws Exception {
        for (String t : List.of("taxonomy_meta", "topics", "chash_remap", "document_aspects")) {
            ensureArbiterIndex(t);
        }
    }

    // ---- 4: every switched upsert, latched, under the NEW label after a rename, on a schema with P3.4's id unique ----------

    @Test
    void everyLatchedUpsertArbitratesOnTheIdKeyUnderTheNewLabel() throws Exception {
        var byName = new TaxonomyRepository(tenantScope);
        var taxo = new TaxonomyRepository(tenantScope, LATCHED);
        var aspects = new AspectRepository(tenantScope, LATCHED);
        var aspectsByName = new AspectRepository(tenantScope);
        var remap = new RemapRepository(tenantScope, LATCHED);
        var remapByName = new RemapRepository(tenantScope);

        // taxonomy_meta x3: one row per collection id whichever writer, name path first under the old label, the id path under the new
        String meta = register("mt");
        long metaId = idOf(meta);
        byName.recordDiscoverCount(TENANT, meta, 3, TS);
        String metaNew = rename(meta);
        taxo.recordDiscoverCount(TENANT, metaNew, 5, TS);
        assertThat(metaRows(metaId)).as("recordDiscoverCount").containsExactly(5);
        taxo.importTaxonomyMeta(TENANT, metaNew, 9, TS);
        assertThat(metaRows(metaId)).as("importTaxonomyMeta").containsExactly(9);
        taxo.importBatch(TENANT, "meta", List.of(Map.of("collection", metaNew, "last_discover_doc_count", 11, "last_discover_at", TS)));
        assertThat(metaRows(metaId)).as("importBatch meta").containsExactly(11);

        // topics x2: a spec list that repeats a label hits the root-label arbiter inside the write (DO NOTHING on the id key)
        for (String kind : List.of("discovered", "rebuild")) {
            String c = register("tp" + kind.charAt(0));
            long cid = idOf(c);
            String cNew = rename(c);
            var specs = List.<Map<String, Object>>of(Map.of("label", "dup", "doc_ids", List.of()), Map.of("label", "dup", "doc_ids", List.of()));
            List<Long> ids = kind.equals("discovered") ? taxo.persistDiscoveredTopics(TENANT, cNew, specs)
                : taxo.persistRebuildTopics(TENANT, cNew, specs, Map.of());
            assertThat(ids).as("%s: both specs resolve to one root topic", kind).hasSize(2).containsOnly(ids.get(0));
            assertThat(this.<Integer>su(ctx -> ctx.fetchCount(TOPICS, TOPICS.TENANT_ID.eq(TENANT).and(TOPICS.COLLECTION_ID.eq(cid)))).intValue())
                .as("%s: one topic row", kind).isEqualTo(1);
        }

        // chash_remap: the same fact written under the new label updates the row the old label wrote
        String src = register("rs"), tgt = register("rt");
        long srcId = idOf(src);
        String chash = Chash.ofText("p35cf-remap-" + seq.incrementAndGet()).toHex();
        remapByName.recordBatch(TENANT, List.of(new RemapEntry(src, "fact-1", chash, tgt, "p35c")));
        String srcNew = rename(src);
        remap.recordBatch(TENANT, List.of(new RemapEntry(srcNew, "fact-1", Chash.ofText("p35cf-remap-2-" + seq.get()).toHex(), tgt, "p35c")));
        assertThat(this.<Integer>su(ctx -> ctx.fetchCount(CHASH_REMAP, CHASH_REMAP.TENANT_ID.eq(TENANT).and(CHASH_REMAP.SOURCE_COLLECTION_ID.eq(srcId)))).intValue())
            .as("chash_remap: one row for the id").isEqualTo(1);

        // document_aspects x3 (upsert, import, import batch)
        String asp = register("as");
        long aspId = idOf(asp);
        String doc = "p35cf.as." + seq.incrementAndGet();
        registerDoc(doc, asp);
        aspectsByName.upsertAspect(TENANT, Rdr229P35cReadersByIdTest.aspect(asp, "p.pdf", doc));
        String aspNew = rename(asp);
        aspects.upsertAspect(TENANT, Rdr229P35cReadersByIdTest.aspect(aspNew, "p.pdf", doc));
        assertThat(aspectRows(aspId)).as("upsertAspect").isEqualTo(1);
        aspects.importAspect(TENANT, Rdr229P35cReadersByIdTest.aspect(aspNew, "p.pdf", doc));
        assertThat(aspectRows(aspId)).as("importAspect").isEqualTo(1);
        aspects.importAspectsBatch(TENANT, List.of(Rdr229P35cReadersByIdTest.aspect(aspNew, "p.pdf", doc)));
        assertThat(aspectRows(aspId)).as("importAspectsBatch").isEqualTo(1);
    }

    private List<Integer> metaRows(long collectionId) throws Exception {
        return su(ctx -> ctx.select(TAXONOMY_META.LAST_DISCOVER_DOC_COUNT).from(TAXONOMY_META)
            .where(TAXONOMY_META.TENANT_ID.eq(TENANT)).and(TAXONOMY_META.COLLECTION_ID.eq(collectionId))
            .fetch(TAXONOMY_META.LAST_DISCOVER_DOC_COUNT));
    }

    private int aspectRows(long collectionId) throws Exception {
        return su(ctx -> ctx.fetchCount(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID.eq(TENANT).and(DOCUMENT_ASPECTS.COLLECTION_ID.eq(collectionId))));
    }

    // ---- S1: Job reports the live label, and a re-entered job runs under it ---------------------------------------

    @Test
    void reembedJobs_reportTheLiveLabel_andReenterRunsUnderIt() throws Exception {
        var byName = new ReembedJobRepository(tenantScope);
        var jobs = new ReembedJobRepository(tenantScope, LATCHED);
        String a = register("ja"), b = register("jb");
        var job = byName.start(TENANT, a, b, null, true, false);
        String aNew = rename(a);

        assertThat(byName.get(TENANT, job.jobId()).orElseThrow().sourceCollection()).as("before gate 3: the stored name").isEqualTo(a);
        var seen = jobs.get(TENANT, job.jobId()).orElseThrow();
        assertThat(seen.sourceCollection()).as("get: the live label of the source id").isEqualTo(aNew);
        assertThat(seen.targetCollection()).isEqualTo(b);
        assertThat(seen.sourceGone()).isFalse();
        assertThat(jobs.listAll(TENANT)).filteredOn(j -> j.jobId().equals(job.jobId())).extracting(ReembedJobRepository.Job::sourceCollection)
            .containsExactly(aNew);
        assertThat(jobs.listByStates(TENANT, List.of(ReembedJobRepository.PLANNED))).extracting(ReembedJobRepository.Job::sourceCollection)
            .contains(aNew);

        // a DONE kept_tombstoned job re-enters under the labels its ids carry now, and stores them
        su(ctx -> ctx.update(REEMBED_JOBS).set(REEMBED_JOBS.STATE, ReembedJobRepository.DONE).set(REEMBED_JOBS.DISPOSITION, "kept_tombstoned")
            .set(REEMBED_JOBS.FINISHED_AT, OffsetDateTime.now()).where(REEMBED_JOBS.JOB_ID.eq(job.jobId())).execute());
        var again = jobs.reenter(TENANT, job.jobId(), ReembedJobRepository.RUNNING).orElseThrow();
        assertThat(again.sourceCollection()).isEqualTo(aNew);
        assertThat(this.<String>su(ctx -> ctx.select(REEMBED_JOBS.SOURCE_COLLECTION).from(REEMBED_JOBS).where(REEMBED_JOBS.JOB_ID.eq(job.jobId()))
            .fetchOne(REEMBED_JOBS.SOURCE_COLLECTION)).toString()).as("the name column a runner looks the source up by").isEqualTo(aNew);

        // history: a gone end reports its stored name and the flag
        deleteRegistryRow(b);
        var gone = jobs.get(TENANT, job.jobId()).orElseThrow();
        assertThat(gone.targetCollection()).isEqualTo(b);
        assertThat(gone.targetGone()).isTrue();
        assertThat(gone.sourceGone()).isFalse();
    }

    // ---- S2: the drift functions' collection is the live label -----------------------------------------------------

    @Test
    void docCountDrift_reportsTheLiveLabel() throws Exception {
        var byName = new TaxonomyRepository(tenantScope);
        var byId = new TaxonomyRepository(tenantScope, LATCHED);
        String old = register("dr");
        long topic = byName.insertTopic(TENANT, "drift-" + seq.incrementAndGet(), null, old, 0, null, null);
        su(ctx -> ctx.update(TOPICS).set(TOPICS.DOC_COUNT, 5).where(TOPICS.ID.eq(topic)).execute());
        String neu = rename(old);

        assertThat(byName.getDocCountDrift(TENANT)).filteredOn(m -> m.get("topic_id").equals(topic)).extracting(m -> m.get("collection"))
            .containsExactly(old);
        assertThat(byId.getDocCountDrift(TENANT)).filteredOn(m -> m.get("topic_id").equals(topic)).extracting(m -> m.get("collection"))
            .containsExactly(neu);
        @SuppressWarnings("unchecked")
        var recount = (List<Map<String, Object>>) byId.recountDocCount(TENANT, true).get("topics");
        assertThat(recount).filteredOn(m -> m.get("topic_id").equals(topic)).extracting(m -> m.get("collection")).containsExactly(neu);
    }

    // ---- the ledgers as history (linda 2026-11-23) --------------------------------------------------------------

    private static RemapEntry fact(String source, String oldId, String target) {
        return new RemapEntry(source, oldId, Chash.ofText("p35cf-" + oldId).toHex(), target, "p35c");
    }

    /** S3: a target whose registry row is gone is reported by its stored name and a flag, not an NPE. */
    @Test
    void remap_aGoneTargetKeepsItsLastLabel() throws Exception {
        var remap = new RemapRepository(tenantScope, LATCHED);
        String src = register("gs"), tgt = register("gt");
        remap.recordBatch(TENANT, List.of(fact(src, "g1", tgt)));
        deleteRegistryRow(tgt);
        var entries = remap.entriesForCollection(TENANT, src);
        assertThat(entries).hasSize(1);
        assertThat(entries.get(0).get("target_collection")).isEqualTo(tgt);
        assertThat(entries.get(0).get("target_gone")).isEqualTo("true");
    }

    /** (a) rename, then find by the new label, with the id and the flag. (c) source gone, then sourceCollections lists it. */
    @Test
    void remap_renamedIsFoundByTheNewLabel_andAGoneSourceIsListed() throws Exception {
        var remap = new RemapRepository(tenantScope, LATCHED);
        String a = register("ra"), b = register("rb");
        remap.recordBatch(TENANT, List.of(fact(a, "r1", b)));
        String aNew = rename(a);
        assertThat(remap.entriesForCollection(TENANT, aNew)).hasSize(1);
        assertThat(remap.entriesForCollection(TENANT, aNew).get(0).get("target_gone")).isEqualTo("false");
        assertThat(remap.entriesForCollection(TENANT, aNew).get(0)).containsKey("target_collection_id");
        assertThat(remap.sourceCollections(TENANT)).contains(aNew).doesNotContain(a);

        deleteRegistryRow(aNew);
        // the row's stored name, `a`, is its last label: written when the fact was
        assertThat(remap.sourceCollections(TENANT)).as("(c) a gone source stays listed under its stored name").contains(a).doesNotContain(aNew);
        assertThat(remap.entriesForCollection(TENANT, a)).as("and its facts stay reachable by that name").hasSize(1);
        assertThat(remap.count(TENANT, a)).isEqualTo(1);
    }

    /** (b) delete A, register the same name again (a new id), then clearLeg(A, B) clears the dangling old-id rows and the new ones. */
    @Test
    void remap_clearLegClearsTheDanglingOldIdAndTheNewRows() throws Exception {
        var remap = new RemapRepository(tenantScope, LATCHED);
        String a = register("ca"), b = register("cb");
        remap.recordBatch(TENANT, List.of(fact(a, "c1", b)));
        deleteRegistryRow(a);
        registerCollection(a);
        remap.recordBatch(TENANT, List.of(fact(a, "c2", b)));
        assertThat(remap.count(TENANT, a)).as("both the dangling row and the new one answer to the label").isEqualTo(2);
        assertThat(remap.clearLeg(TENANT, a, b)).isEqualTo(2);
        assertThat(remap.count(TENANT, a)).isZero();
    }

    /** (d) a stale name whose live id was renamed, beside a NEW collection that holds the old label: only the new collection's rows. */
    @Test
    void remap_aStaleNameNeverMatchesADifferentLiveCollection() throws Exception {
        var remap = new RemapRepository(tenantScope, LATCHED);
        String x = register("dx"), t = register("dt");
        remap.recordBatch(TENANT, List.of(fact(x, "d-old", t)));      // the collection later renamed
        String y = rename(x);
        registerCollection(x);                                         // a NEW collection holds the old label x
        remap.recordBatch(TENANT, List.of(fact(x, "d-new", t)));
        var entries = remap.entriesForCollection(TENANT, x);
        assertThat(entries).extracting(m -> m.get("old_id")).containsExactly("d-new");
        assertThat(remap.count(TENANT, x)).isEqualTo(1);
        assertThat(remap.entriesForCollection(TENANT, y)).extracting(m -> m.get("old_id")).containsExactly("d-old");
        assertThat(remap.clearLeg(TENANT, x, t)).isEqualTo(1);
        assertThat(remap.count(TENANT, y)).as("the renamed collection's row is untouched").isEqualTo(1);
    }

    /** reembed_jobs is history too: a job whose source is gone is still found by its last label; a stale name never matches the new holder. */
    @Test
    void reembedJobs_matchAsHistory() throws Exception {
        var jobs = new ReembedJobRepository(tenantScope, LATCHED);
        String a = register("ha"), b = register("hb"), c = register("hc");
        jobs.start(TENANT, a, b, null, true, false);
        deleteRegistryRow(b);                                          // b is gone: its stored name is its last label
        registerCollection(b);                                         // a NEW collection takes the label b
        // by name only a GONE row matches: the old job's target id no longer exists, so a start from b is refused as the target of a live job...
        assertThat(org.assertj.core.api.Assertions.catchThrowable(() -> jobs.start(TENANT, b, c, null, true, false)))
            .as("the dangling target id is gone history; its stored name reaches it").isInstanceOf(ReembedJobRepository.JobLockHeld.class);
    }

    /** Retires {@code name} to a tombstone naming {@code successor}: the row stays, but it is no longer a live collection. */
    private void retire(String name, String successor) throws Exception {
        long successorId = idOf(successor);
        su(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, successor)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, successorId)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name)).execute());
    }

    /** A tombstone is not a LIVE registry row: its ledger entries are history, found by the old label with the gone flag, and a job reports its stored name. */
    @Test
    void aTombstoneIsGoneHistory() throws Exception {
        var remap = new RemapRepository(tenantScope, LATCHED);
        var jobs = new ReembedJobRepository(tenantScope, LATCHED);
        String old = register("tb"), tgt = register("tt"), succ = register("ts");
        remap.recordBatch(TENANT, List.of(fact(old, "t1", tgt)));
        remap.recordBatch(TENANT, List.of(fact(tgt, "t2", old)));        // old as the TARGET of another leg
        var job = jobs.start(TENANT, old, tgt, null, true, false);
        retire(old, succ);

        var asSource = remap.entriesForCollection(TENANT, old);
        assertThat(asSource).as("found by its old label: the name arm reaches the tombstone's history").hasSize(1);
        assertThat(remap.count(TENANT, old)).isEqualTo(1);
        assertThat(remap.sourceCollections(TENANT)).contains(old);
        var asTarget = remap.entriesForCollection(TENANT, tgt);
        assertThat(asTarget).hasSize(1);
        assertThat(asTarget.get(0).get("target_collection")).as("a tombstoned target keeps its stored name").isEqualTo(old);
        assertThat(asTarget.get(0).get("target_gone")).isEqualTo("true");

        var seen = jobs.get(TENANT, job.jobId()).orElseThrow();
        assertThat(seen.sourceCollection()).isEqualTo(old);
        assertThat(seen.sourceGone()).as("a tombstoned source is gone").isTrue();
        assertThat(seen.targetGone()).isFalse();
    }

    // ---- readers the first class could not falsify ---------------------------------------------------------------

    @Test
    void topicReaders_readTheId() throws Exception {
        var byId = new TaxonomyRepository(tenantScope, LATCHED);
        var byName = new TaxonomyRepository(tenantScope);
        String old = register("tr");
        String src = register("ts");
        Chash chash = Chash.ofText("p35cf-topic-" + seq.incrementAndGet());
        insertChunk(src, chash);
        String label = "pending-" + seq.incrementAndGet();
        long keep = byName.insertTopic(TENANT, label, null, old, 0, null, null);
        long moved = byName.insertTopic(TENANT, "moved-" + seq.get(), null, old, 0, null, null);
        assertThat(byName.assignTopic(TENANT, chash.toHex(), moved, "projection", 0.5, src, null)).isTrue();
        String neu = rename(old);

        assertThat(byId.getUnreviewedTopics(TENANT, neu, 10)).extracting(m -> m.get("id")).contains(keep, moved);
        assertThat(byId.getUnreviewedTopics(TENANT, old, 10)).isEmpty();
        assertThat(byId.resolveLabel(TENANT, label, neu)).contains(keep);
        assertThat(byId.resolveLabel(TENANT, label, old)).isEmpty();
        assertThat(byName.resolveLabel(TENANT, label, old)).contains(keep);
        // merge: the source topic's collection comes back as its live label; its assignment moves to the target
        assertThat(byId.mergeTopics(TENANT, moved, keep)).contains(neu);
        assertThat(this.<Integer>su(ctx -> ctx.fetchCount(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TOPIC_ID.eq(keep))).intValue()).isEqualTo(1);
    }

    @Test
    void reembedSourceReader_readsTheIdOnEveryShape() throws Exception {
        var byName = new ReembedSourceReader(tenantScope);
        var byId = new ReembedSourceReader(tenantScope, LATCHED);
        Fx f = fixture("sr");
        String chash = ch("p35cf-sr-" + seq.incrementAndGet());
        svc.writeManyCombined(TENANT, f.collection(), List.of(chunk(chash, "p35cf source text")), List.of(doc(f.docId(), List.of(row(0, chash)))),
            null, false, false);
        String neu = rename(f.collection());

        assertThat(byName.candidates(TENANT, f.collection(), "", 10)).hasSize(1);
        assertThat(byName.candidates(TENANT, neu, "", 10)).isEmpty();
        assertThat(byId.candidates(TENANT, neu, "", 10)).hasSize(1);
        assertThat(byId.candidates(TENANT, f.collection(), "", 10)).isEmpty();
        assertThat(byId.anyCandidate(TENANT, neu)).isTrue();
        assertThat(byId.anyCandidate(TENANT, f.collection())).isFalse();
        var plan = byId.planCounts(TENANT, neu);
        assertThat(plan.documents()).isEqualTo(1);
        assertThat(plan.chunks()).isEqualTo(1);
        assertThat(plan.ownerlessChunks()).isZero();
        assertThat(plan.tokenEstimate()).isPositive();
        assertThat(byName.planCounts(TENANT, neu).documents()).isZero();
        assertThat(byName.planCounts(TENANT, neu).tokenEstimate()).as("by name the chunk row is found under the old name only").isZero();
        var parity = byId.parity(TENANT, neu, neu, UUID.randomUUID(), 0);
        assertThat(parity.manifestRowsNamingSource()).as("the manifest row names the source's id").isEqualTo(1);
        assertThat(parity.liveHomedAtSource()).as("the document is homed at the source id").isEqualTo(1);
        assertThat(byName.parity(TENANT, neu, neu, UUID.randomUUID(), 0).manifestRowsNamingSource()).isZero();
        assertThat(byId.chunks(TENANT, neu, List.of(chash))).hasSize(1);
        assertThat(byId.chunks(TENANT, f.collection(), List.of(chash))).isEmpty();
        assertThat(byName.chunks(TENANT, f.collection(), List.of(chash))).hasSize(1);
        assertThat(byId.tombstonedDocuments(TENANT, neu)).isZero();
    }

    // ---- NULL-id polarity: a row with no collection is "not the source" (M2) -------------------------------------

    @Test
    void ne_isTrueForARowWithNoCollection() throws Exception {
        String emptyDoc = "p35cf.ne." + seq.incrementAndGet();
        String registered = register("ne");
        su(ctx -> ctx.insertInto(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS,
                dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS.TENANT_ID, dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS.TUMBLER,
                dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS.TITLE, dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
            .values(TENANT, emptyDoc, "empty", "").execute());
        var d = dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
        var one = d.TENANT_ID.eq(TENANT).and(d.TUMBLER.eq(emptyDoc));
        for (Gate3 g : List.of(Gate3.never(), LATCHED)) {
            var k = dev.nexus.service.db.CollectionKey.of(g);
            assertThat(this.<Integer>su(ctx -> ctx.fetchCount(d, one.and(k.ne(TENANT, d.PHYSICAL_COLLECTION, d.PHYSICAL_COLLECTION_ID, registered)))).intValue())
                .as("a '' / NULL-id row differs from the source (latched=%s)", k.byId()).isEqualTo(1);
        }
        // and a row that names the source does not differ from it
        String named = "p35cf.ne.named." + seq.incrementAndGet();
        registerDoc(named, registered);
        var k = dev.nexus.service.db.CollectionKey.of(LATCHED);
        assertThat(this.<Integer>su(ctx -> ctx.fetchCount(d, d.TENANT_ID.eq(TENANT).and(d.TUMBLER.eq(named))
            .and(k.ne(TENANT, d.PHYSICAL_COLLECTION, d.PHYSICAL_COLLECTION_ID, registered)))).intValue()).isZero();
    }
}
