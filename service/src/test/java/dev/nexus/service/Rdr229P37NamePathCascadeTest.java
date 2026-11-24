// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.OffsetDateTime;
import java.util.List;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27), critic r2 item 2: a pre-gate-3 NAME-PATH rename with vectors-079 installed. The rename retires the old
 * registry row, gives the collection a new id and renames the chunks first; the chunk foreign keys' ON UPDATE CASCADE then renames each
 * manifest, orphaned-at and topic-assignment row by name while it still carries the old id. That cascade is not a writer: step 2 of the
 * trigger (a by-id scan of the tenant's leaf, no id key on chunks in this tree) would run once per cascaded row, N x N. The trigger skips
 * it for a child whose collection id belongs to a tombstone. The rename must complete, leave every child on its chunk's name, and cost
 * the work it costs with the triggers disabled: counted (step-2 lookups through a test-only counting seam), never timed.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P37NamePathCascadeTest {

    private static final int N = 6000;
    private static final List<String> TRIGGERS = List.of(
        "catalog_document_chunks:trg_catalog_document_chunks_name_from_chunk", "chunk_orphaned_at:trg_chunk_orphaned_at_name_from_chunk",
        "topic_assignments:trg_topic_assignments_name_from_chunk");

    private Rdr229RenameEngine engine;

    @BeforeAll
    void boot() throws Exception {
        engine = new Rdr229RenameEngine().gate3(false);   // never opens: the name path throughout
        engine.boot();
        Rdr229RenameRestartPrecondition.awaitStampMovesComplete(engine);
    }

    @AfterAll
    void stop() {
        if (engine != null) {
            engine.close();
        }
    }

    private static String coll(String owner, int version) {
        return "knowledge__" + owner + "__minilm-l6-v2-384__v" + version;
    }

    /**
     * One chunk, then N more copied from it with other chashes (N + 1 in all, in chash order). A manifest row for the first N/2, an orphaned-at
     * record for the next N/2 (a chunk with a manifest row has no record: the manifest UPDATE trigger clears it), a topic assignment for the first N/2.
     */
    private void seed(String tenant, String name) {
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, name);
            long id = Rdr229RenameChecks.registryId(ctx, tenant, name);
            String model = PgContainerHelper.collectionModel(ctx, tenant, name);
            Rdr229ChildNameWorld.chunk(ctx, tenant, name, id, model, 384, "seed");
            var c = CHUNKS.as("c");
            var g = DSL.generateSeries(1, N).as("g", "v");
            ctx.insertInto(CHUNKS).columns(CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL, CHUNKS.CHUNK_TEXT,
                    CHUNKS.EMBEDDING_384, CHUNKS.METADATA, CHUNKS.LAST_WRITTEN_AT, CHUNKS.COLLECTION_ID)
                .select(ctx.select(c.TENANT_ID, c.COLLECTION,
                        DSL.function("sha256", byte[].class, DSL.function("int4send", byte[].class, g.field("v", Integer.class))),
                        c.EMBEDDING_MODEL, c.CHUNK_TEXT, c.EMBEDDING_384, c.METADATA, c.LAST_WRITTEN_AT, c.COLLECTION_ID)
                    .from(c).crossJoin(g).where(c.TENANT_ID.eq(tenant)).and(c.COLLECTION.eq(name)))
                .execute();
            String doc = "casc.doc." + name;
            PgContainerHelper.insertCatalogDocument(ctx, tenant, doc);
            var first = ctx.select().from(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(name))
                .orderBy(CHUNKS.CHASH).limit(N / 2).asTable("f");
            ctx.insertInto(CATALOG_DOCUMENT_CHUNKS).columns(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                    CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION,
                    CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID)
                .select(ctx.select(first.field(CHUNKS.TENANT_ID), DSL.inline(doc), DSL.rowNumber().over(DSL.orderBy(first.field(CHUNKS.CHASH))).minus(1),
                        first.field(CHUNKS.CHASH), first.field(CHUNKS.COLLECTION), first.field(CHUNKS.EMBEDDING_MODEL),
                        first.field(CHUNKS.COLLECTION_ID)).from(first))
                .execute();
            var rest = ctx.select().from(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(name))
                .orderBy(CHUNKS.CHASH).offset(N / 2).limit(N / 2).asTable("r");
            ctx.insertInto(CHUNK_ORPHANED_AT).columns(CHUNK_ORPHANED_AT.TENANT_ID, CHUNK_ORPHANED_AT.COLLECTION, CHUNK_ORPHANED_AT.CHASH,
                    CHUNK_ORPHANED_AT.ORPHANED_AT, CHUNK_ORPHANED_AT.EMBEDDING_MODEL, CHUNK_ORPHANED_AT.COLLECTION_ID)
                .select(ctx.select(rest.field(CHUNKS.TENANT_ID), rest.field(CHUNKS.COLLECTION), rest.field(CHUNKS.CHASH),
                        DSL.currentOffsetDateTime(), rest.field(CHUNKS.EMBEDDING_MODEL), rest.field(CHUNKS.COLLECTION_ID)).from(rest))
                .execute();
            long topic = ctx.insertInto(TOPICS).set(TOPICS.TENANT_ID, tenant).set(TOPICS.LABEL, "t").set(TOPICS.COLLECTION, name)
                .set(TOPICS.COLLECTION_ID, id).set(TOPICS.CREATED_AT, OffsetDateTime.now()).returning(TOPICS.ID).fetchOne().getId();
            ctx.insertInto(TOPIC_ASSIGNMENTS).columns(TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID, TOPIC_ASSIGNMENTS.TOPIC_ID,
                    TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, TOPIC_ASSIGNMENTS.EMBEDDING_MODEL,
                    TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID)
                .select(ctx.select(first.field(CHUNKS.TENANT_ID), first.field(CHUNKS.CHASH), DSL.inline(topic), DSL.inline("seed"),
                        first.field(CHUNKS.COLLECTION), first.field(CHUNKS.EMBEDDING_MODEL), first.field(CHUNKS.COLLECTION_ID)).from(first))
                .execute();
            return null;
        });
    }

    private long[] childCounts(String tenant, String name) {
        return engine.su(ctx -> new long[] {
            ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(name))),
            ctx.fetchCount(CHUNK_ORPHANED_AT, CHUNK_ORPHANED_AT.TENANT_ID.eq(tenant).and(CHUNK_ORPHANED_AT.COLLECTION.eq(name))),
            ctx.fetchCount(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID.eq(tenant).and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.eq(name)))});
    }

    /** Disables or enables the three triggers. */
    private void triggers(boolean enabled) {
        engine.su(ctx -> ctx.connectionResult(c -> {
            try {
                for (String t : TRIGGERS) {
                    String table = t.split(":")[0];
                    String trigger = t.split(":")[1];
                    if (enabled) {
                        PgContainerHelper.enableTrigger(c, table, trigger);
                    } else {
                        PgContainerHelper.disableTrigger(c, table, trigger);
                    }
                }
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
            return null;
        }));
    }

    private static final org.jooq.Table<?> PROBE = DSL.table(DSL.name("nexus", "p37_step2_probe"));
    private static final String CHANGELOG_PROBE_CREATE = "db/changelog-test/db.changelog-test-step2-probe-create.xml";
    private static final String CHANGELOG_PROBE_DROP = "db/changelog-test/db.changelog-test-step2-probe-drop.xml";

    /** Each of the trigger bodies' step 2 (the by-id lookup of the chunk) advances the probe first: the instrumentation, the only change to the text. */
    private static final String STEP2 = "SELECT c.collection INTO v_stored";
    private static final String STEP2_COUNTED = "PERFORM pg_catalog.nextval('nexus.p37_step2_probe');\n    " + STEP2;
    private static final String GUARD = "r.superseded_by <> ''";
    private static final String GUARD_OFF = "r.superseded_by = 'never'";

    private void probe(String changelog, String bookkeeping) {
        engine.su(ctx -> ctx.connectionResult(c -> {
            try {
                PgContainerHelper.runSuperuserTestChangelog(c, changelog, bookkeeping, java.util.Map.of());
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
            return null;
        }));
    }

    /** How many times the instrumented bodies reached step 2 so far (the sequence's value, 0 before its first use). */
    private long step2Lookups() {
        var lastValue = DSL.field(DSL.name("last_value"), Long.class);
        var isCalled = DSL.field(DSL.name("is_called"), Boolean.class);
        return engine.su(ctx -> {
            var row = ctx.select(lastValue, isCalled).from(PROBE).fetchOne();
            return Boolean.TRUE.equals(row.value2()) ? row.value1() : 0L;
        });
    }

    /** What one rename did: how long it took (printed, never asserted) and how many step-2 lookups the instrumented trigger bodies made during it. */
    private record Cascade(long millis, long step2Lookups) { }

    private Cascade cascade(String tenant, String token, String oldName, String newName) throws Exception {
        long[] before = childCounts(tenant, oldName);
        assertThat(before).as("non-vacuity: children to cascade").containsExactly(N / 2L, N / 2L, N / 2L);
        long lookupsBefore = step2Lookups();
        long started = System.nanoTime();
        var r = engine.rename(token, oldName, newName);
        long ms = (System.nanoTime() - started) / 1_000_000;
        long lookups = step2Lookups() - lookupsBefore;
        assertThat(r.status()).as("the name path before gate 3: " + r).isEqualTo(200);
        assertThat(childCounts(tenant, oldName)).as("no child is left under the old name").containsExactly(0L, 0L, 0L);
        assertThat(childCounts(tenant, newName)).as("every child follows its chunk to the new name").containsExactly(before);
        assertThat(Rdr229ChildNameWorld.childrenNotOnTheirChunksName(engine, tenant)).as("the children are consistent with the chunks").isZero();
        return new Cascade(ms, lookups);
    }

    /**
     * The guard is load-bearing, and it is shown by COUNTING the work it skips, never by timing it. A cascaded child still carries the old collection
     * id, which belongs to the tombstone the rename just left; step 2 of the trigger bodies (a by-id scan of the tenant's leaf, no id key on chunks in this
     * tree) would run once per cascaded row. The test replays vectors-079 with the one change that step 2 first advances a sequence the test created on
     * this container (a counting seam in the test only; the shipped text is replayed back afterwards): the guarded cascade reaches step 2 for none of the
     * 3 x N/2 children, the same cascade with the guard mutated off for every one of them, and the control, the three triggers disabled, for none.
     * (The sequence is only advanced by step 2 and nothing else on this engine writes a child under a name other than its chunk's during the test.)
     */
    @Test
    void aNamePathRenameCascadesEveryChildWithTheTriggersInstalled_withoutTheByIdLookupPerRow() throws Exception {
        String tenant = "p37-casc";
        String token = engine.newTenant(tenant);
        long children = 3L * (N / 2);
        probe(CHANGELOG_PROBE_CREATE, "databasechangelog_test_step2_probe");
        try {
            String disabledOld = coll("p37cascb", 1);
            String enabledOld = coll("p37casca", 1);
            String unguardedOld = coll("p37cascc", 1);
            seed(tenant, disabledOld);
            seed(tenant, enabledOld);
            seed(tenant, unguardedOld);

            // control: the same rename with the three triggers disabled does no lookup at all (and the children still follow: the foreign keys' cascade)
            triggers(false);
            Cascade disabled;
            try {
                disabled = cascade(tenant, token, disabledOld, coll("p37cascb", 2));
            } finally {
                triggers(true);
            }

            // the guarded cascade, the shipped guard with the counting step 2
            var counted = new java.util.LinkedHashMap<String, String>();
            counted.put(STEP2, STEP2_COUNTED);
            Cascade[] guarded = new Cascade[1];
            Vectors079Text.withSubstitutions(engine, counted, () -> {
                try {
                    guarded[0] = cascade(tenant, token, enabledOld, coll("p37casca", 2));
                } catch (Exception e) {
                    throw new IllegalStateException(e);
                }
            });

            // the same rename with the tombstone guard mutated off (what the predecessor of this commit shipped): step 2 runs for every cascaded row
            var countedWithoutGuard = new java.util.LinkedHashMap<String, String>();
            countedWithoutGuard.put(STEP2, STEP2_COUNTED);
            countedWithoutGuard.put(GUARD, GUARD_OFF);
            Cascade[] unguarded = new Cascade[1];
            Vectors079Text.withSubstitutions(engine, countedWithoutGuard, () -> {
                try {
                    unguarded[0] = cascade(tenant, token, unguardedOld, coll("p37cascc", 2));
                } catch (Exception e) {
                    throw new IllegalStateException(e);
                }
            });

            System.out.println("COUNTS name-path rename of " + (N + 1) + " chunks, " + children + " cascaded child rows: step-2 lookups: triggers disabled "
                + disabled.step2Lookups() + ", guarded " + guarded[0].step2Lookups() + ", without the tombstone guard " + unguarded[0].step2Lookups()
                + "; TIMING (not asserted) " + disabled.millis() + " / " + guarded[0].millis() + " / " + unguarded[0].millis() + " ms");
            assertThat(disabled.step2Lookups()).as("control: with the triggers disabled nothing looks anything up").isZero();
            assertThat(guarded[0].step2Lookups()).as("the guard skips step 2 for every cascaded child: no by-id lookup per row, as with the triggers off").isZero();
            assertThat(unguarded[0].step2Lookups()).as("load-bearing: without the guard every one of the " + children + " cascaded children does the by-id lookup")
                .isGreaterThanOrEqualTo(children);
        } finally {
            probe(CHANGELOG_PROBE_DROP, "databasechangelog_test_step2_probe_drop");
        }
    }
}
