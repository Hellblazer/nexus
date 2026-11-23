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
 * about what it costs with the triggers disabled.
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

    private long renameMillis(String tenant, String token, String oldName, String newName) throws Exception {
        long[] before = childCounts(tenant, oldName);
        assertThat(before).as("non-vacuity: children to cascade").containsExactly(N / 2L, N / 2L, N / 2L);
        long started = System.nanoTime();
        var r = engine.rename(token, oldName, newName);
        long ms = (System.nanoTime() - started) / 1_000_000;
        assertThat(r.status()).as("the name path before gate 3: " + r).isEqualTo(200);
        assertThat(childCounts(tenant, oldName)).as("no child is left under the old name").containsExactly(0L, 0L, 0L);
        assertThat(childCounts(tenant, newName)).as("every child follows its chunk to the new name").containsExactly(before);
        assertThat(Rdr229ChildNameWorld.childrenNotOnTheirChunksName(engine, tenant)).as("the children are consistent with the chunks").isZero();
        return ms;
    }

    @Test
    void aNamePathRenameCascadesEveryChildWithTheTriggersInstalled_atAboutTheCostOfNoTriggers() throws Exception {
        String tenant = "p37-casc";
        String token = engine.newTenant(tenant);
        String disabledOld = coll("p37cascb", 1);
        String enabledOld = coll("p37casca", 1);
        seed(tenant, disabledOld);
        seed(tenant, enabledOld);

        // control: the same rename with the three triggers disabled
        triggers(false);
        long withoutMs;
        try {
            withoutMs = renameMillis(tenant, token, disabledOld, coll("p37cascb", 2));
        } finally {
            triggers(true);
        }
        long withMs = renameMillis(tenant, token, enabledOld, coll("p37casca", 2));

        // the same rename with the three functions replaced by mutants WITHOUT the tombstone guard (what this commit's predecessor shipped):
        // the cascade runs step 2 for every row. The guard is what keeps the cascade at the cost above.
        String unguardedOld = coll("p37cascc", 1);
        seed(tenant, unguardedOld);
        long[] unguardedMs = new long[1];
        Vectors079Text.withMutant(engine, "r.superseded_by <> ''", "r.superseded_by = 'never'", () -> {
            try {
                unguardedMs[0] = renameMillis(tenant, token, unguardedOld, coll("p37cascc", 2));
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });

        System.out.println("TIMING name-path rename of " + (N + 1) + " chunks, " + N / 2 + " manifest, " + N / 2 + " orphaned, " + N / 2
            + " assignment rows: triggers disabled " + withoutMs + " ms, installed " + withMs + " ms, installed without the tombstone guard "
            + unguardedMs[0] + " ms");
        // wall-clock on a shared host is noisy: the guard is load-bearing if the unguarded cascade is slower in ANY of up to three paired rounds
        boolean loadBearing = unguardedMs[0] > withMs;
        long lastWith = withMs;
        long lastUnguarded = unguardedMs[0];
        for (int i = 1; i <= 2 && !loadBearing; i++) {
            final int round = i;
            String guardedOld = coll("p37cascg" + round, 1);
            String unguardedAgain = coll("p37cascu" + round, 1);
            seed(tenant, guardedOld);
            seed(tenant, unguardedAgain);
            lastWith = renameMillis(tenant, token, guardedOld, coll("p37cascg" + round, 2));
            long[] again = new long[1];
            Vectors079Text.withMutant(engine, "r.superseded_by <> ''", "r.superseded_by = 'never'", () -> {
                try {
                    again[0] = renameMillis(tenant, token, unguardedAgain, coll("p37cascu" + round, 2));
                } catch (Exception e) {
                    throw new IllegalStateException(e);
                }
            });
            lastUnguarded = again[0];
            System.out.println("TIMING round " + round + ": installed " + lastWith + " ms, installed without the tombstone guard " + lastUnguarded + " ms");
            loadBearing = lastUnguarded > lastWith;
        }
        assertThat(loadBearing).as("load-bearing: without the guard the same cascade is slower in at least one of three paired rounds (guarded " + withMs
            + " vs " + unguardedMs[0] + " ms, then " + lastWith + " vs " + lastUnguarded + " ms)").isTrue();
        assertThat(withMs).as("the cascade costs about what it costs without the triggers (disabled " + withoutMs + " ms): no by-id scan per row")
            .isLessThanOrEqualTo(withoutMs * 3 + 3000);
    }
}
