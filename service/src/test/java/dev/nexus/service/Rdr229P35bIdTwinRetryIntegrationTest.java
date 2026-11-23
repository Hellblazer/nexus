// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CollectionIds;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Record;
import org.jooq.Sequence;
import org.jooq.Table;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.SQLException;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.function.Consumer;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;
import static dev.nexus.service.jooq.nexus.Tables.GC_QUARANTINE_ORPHANS;
import static dev.nexus.service.jooq.nexus.Tables.GC_QUARANTINE_ORPHANS_BOUNDED;
import static dev.nexus.service.jooq.nexus.Tables.REAPER_QUARANTINE_CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5b (nexus-p2m1x.24), the SQL functions that write a table with a name key and an id twin, and the one trigger whose
 * rename rule changed.
 *
 * <p>The retry. {@code nexus.chunks} (the primary key on the name, and each leaf's {@code <leaf>_cid_uq} on the id) and
 * {@code nexus.chunk_orphaned_at} (the primary key, and P3.4's id key) hold two unique keys until P4.1. An {@code ON CONFLICT}
 * arbiter names one of them, so a racing writer's row can trip the other as a plain 23505. Each of vectors-077-49, -56 and -87 (the
 * quarantine moves, which write the sibling's chunk row) and -60 and -61 (the manifest triggers, which write the orphaning record)
 * retries once, and only when the 23505 is that id twin: a unique index on the table's partition root over the id key's columns. The
 * match is made on the index's columns, so a wrong column array makes every real twin conflict look foreign and the call fail.
 *
 * <p>Each case holds a racer, a row of another name and the same id key, which the insert's name key does not match, so the
 * insert is refused by the id twin (a real unique violation on the real index). A BEFORE INSERT trigger
 * ({@code db.changelog-test-p35b-retry-gate.xml}) counts the attempts through a sequence a rolled-back attempt cannot undo,
 * and removes the racer on the attempt it is told to. Three modes: the racer is gone by the second attempt (one retry, success);
 * the racer is still there (a second 23505 is raised, after exactly two attempts); the conflict is on a unique index over other
 * columns (the first 23505 is raised, after one attempt).
 *
 * <p>The rename rule. {@code nexus.stamp_chunks_on_manifest_update} records an orphan for a key the statement dropped, the old keys
 * minus the new ones. B2 matches a key by id where both rows carry one, so a manifest row that keeps its id and changes only its
 * name (P3.7's rename) drops nothing.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P35bIdTwinRetryIntegrationTest {

    private static final int DIM = 768;
    private static final String QUARANTINE = "quarantine-";
    private static final String TS = "2026-11-23T00:00:00Z";
    private static final String GATE = "db/changelog-test/db.changelog-test-p35b-retry-gate.xml";
    private static final Table<?> GATE_CFG = DSL.table(DSL.name("nexus_test", "p35b_retry_gate_cfg"));
    private static final Field<String> CFG_TARGET = DSL.field(DSL.name("target"), String.class);
    private static final Field<String> CFG_RACER = DSL.field(DSL.name("racer"), String.class);
    private static final Field<Long> CFG_DELETE_ON = DSL.field(DSL.name("delete_on"), Long.class);
    private static final Sequence<Long> GATE_SEQ = DSL.sequence(DSL.name("nexus_test", "p35b_retry_gate_seq"), Long.class);
    /** The name P3.4 gives its id key of the orphaning record; the function matches the index by columns, never by this name. */
    private static final String ORPHAN_ID_KEY = "idx_chunk_orphaned_at_id_chash_uq";
    /** A unique index over other columns than the id key's: the same three and the model. */
    private static final String ORPHAN_OTHER_KEY = "idx_p35b_orphaned_at_other_uq";
    private static final String CHUNK_OTHER_KEY = "idx_p35b_chunks_other_uq";
    private static final Pattern VIOLATED = Pattern.compile("unique constraint \"([^\"]+)\"");

    private enum Mode { RETRY_SUCCEEDS, RACER_STAYS, OTHER_INDEX }

    private enum MoveFn { GC_BOUNDED, REAPER, GC }

    private enum Trigger { MANIFEST_DELETE, MANIFEST_UPDATE }

    private PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAndDropTheNameKeys() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        su(ctx -> assertThat(ByIdRestoreSupport.dropRegistryNameKeys(ctx))
            .as("non-vacuity: the registry's name keys exist and are dropped").isNotEmpty());
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.dropCatalogChunksFk(su);
            PgContainerHelper.runSuperuserTestChangelog(su, GATE, "databasechangelog_test_p35b_retry_gate", Map.of());
        }
    }

    @AfterAll
    void stop() {
        if (pg != null) {
            pg.stop();
        }
    }

    // ---- the quarantine moves: the sibling's chunk row -----------------------------------------------------------------------

    @Test
    void gcQuarantineOrphansBounded_retriesOnTheIdTwinOnce() throws Exception {
        chunkRace("bd-ok", MoveFn.GC_BOUNDED, Mode.RETRY_SUCCEEDS);
    }

    @Test
    void gcQuarantineOrphansBounded_aSecondIdTwinConflictIsRaised() throws Exception {
        chunkRace("bd-2x", MoveFn.GC_BOUNDED, Mode.RACER_STAYS);
    }

    @Test
    void gcQuarantineOrphansBounded_aConflictOnOtherColumnsIsRaisedAtOnce() throws Exception {
        chunkRace("bd-ot", MoveFn.GC_BOUNDED, Mode.OTHER_INDEX);
    }

    @Test
    void reaperQuarantineChunks_retriesOnTheIdTwinOnce() throws Exception {
        chunkRace("rp-ok", MoveFn.REAPER, Mode.RETRY_SUCCEEDS);
    }

    @Test
    void reaperQuarantineChunks_aSecondIdTwinConflictIsRaised() throws Exception {
        chunkRace("rp-2x", MoveFn.REAPER, Mode.RACER_STAYS);
    }

    @Test
    void reaperQuarantineChunks_aConflictOnOtherColumnsIsRaisedAtOnce() throws Exception {
        chunkRace("rp-ot", MoveFn.REAPER, Mode.OTHER_INDEX);
    }

    @Test
    void gcQuarantineOrphans_retriesOnTheIdTwinOnce() throws Exception {
        chunkRace("gc-ok", MoveFn.GC, Mode.RETRY_SUCCEEDS);
    }

    @Test
    void gcQuarantineOrphans_aSecondIdTwinConflictIsRaised() throws Exception {
        chunkRace("gc-2x", MoveFn.GC, Mode.RACER_STAYS);
    }

    @Test
    void gcQuarantineOrphans_aConflictOnOtherColumnsIsRaisedAtOnce() throws Exception {
        chunkRace("gc-ot", MoveFn.GC, Mode.OTHER_INDEX);
    }

    // ---- the manifest triggers: the orphaning record ------------------------------------------------------------------------

    @Test
    void stampOnManifestDelete_retriesOnTheIdTwinOnce() throws Exception {
        orphanRace("dl-ok", Trigger.MANIFEST_DELETE, Mode.RETRY_SUCCEEDS);
    }

    @Test
    void stampOnManifestDelete_aSecondIdTwinConflictIsRaised() throws Exception {
        orphanRace("dl-2x", Trigger.MANIFEST_DELETE, Mode.RACER_STAYS);
    }

    @Test
    void stampOnManifestDelete_aConflictOnOtherColumnsIsRaisedAtOnce() throws Exception {
        orphanRace("dl-ot", Trigger.MANIFEST_DELETE, Mode.OTHER_INDEX);
    }

    @Test
    void stampOnManifestUpdate_retriesOnTheIdTwinOnce() throws Exception {
        orphanRace("up-ok", Trigger.MANIFEST_UPDATE, Mode.RETRY_SUCCEEDS);
    }

    @Test
    void stampOnManifestUpdate_aSecondIdTwinConflictIsRaised() throws Exception {
        orphanRace("up-2x", Trigger.MANIFEST_UPDATE, Mode.RACER_STAYS);
    }

    @Test
    void stampOnManifestUpdate_aConflictOnOtherColumnsIsRaisedAtOnce() throws Exception {
        orphanRace("up-ot", Trigger.MANIFEST_UPDATE, Mode.OTHER_INDEX);
    }

    // ---- the rename rule of the manifest UPDATE trigger ---------------------------------------------------------------------

    /** A manifest row that keeps its id and changes only its name drops no key: no orphan is recorded for either chunk. */
    @Test
    void stampOnManifestUpdate_aSameIdRenameRecordsNoOrphan() throws Exception {
        OwnedStore s = ownedStore("rn-same");
        su(ctx -> {
            // vectors-079's trigger gives a manifest row the name of its chunk, so a name-only UPDATE would change nothing; the name-path
            // rename this test models (a row keeps its id and takes a new name) runs with that trigger off, as before P3.7
            int n = withTriggerOff(ctx, "catalog_document_chunks", "trg_catalog_document_chunks_name_from_chunk",
                () -> ctx.update(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, s.renamed)
                    .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(s.tenant)).and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(s.live)).execute());
            assertThat(n).as("non-vacuity: both manifest rows are renamed").isEqualTo(2);
            assertThat(ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(s.tenant)
                    .and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(s.renamed)).and(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID.eq(s.id))))
                .as("the rows carry the new name beside the unchanged id").isEqualTo(2);
            assertThat(orphans(ctx, s.tenant)).as("a rename that keeps the id orphans nothing").isEmpty();
        });
    }

    /** A real removal still records the orphan: the chunk loses its owner when its row's chash changes. */
    @Test
    void stampOnManifestUpdate_aRealRemovalRecordsTheOrphan() throws Exception {
        OwnedStore s = ownedStore("rn-real");
        su(ctx -> {
            int n = ctx.update(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.CHASH, bytes(s.third))
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(s.tenant)).and(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(bytes(s.first))).execute();
            assertThat(n).isEqualTo(1);
            assertThat(orphans(ctx, s.tenant)).as("the chunk that lost its owner, and no other").containsExactly(s.first);
            assertThat(ctx.fetchCount(CHUNK_ORPHANED_AT, CHUNK_ORPHANED_AT.TENANT_ID.eq(s.tenant).and(CHUNK_ORPHANED_AT.COLLECTION.eq(s.live))
                    .and(CHUNK_ORPHANED_AT.COLLECTION_ID.eq(s.id)).and(CHUNK_ORPHANED_AT.CHASH.eq(bytes(s.first)))))
                .as("the record carries the collection's name and id").isEqualTo(1);
        });
    }

    /** The two together in one statement: the row is renamed and moves to another chash. The rename hides nothing; the dropped chash is recorded. */
    @Test
    void stampOnManifestUpdate_aRenameThatAlsoDropsAChashRecordsTheDroppedChunk() throws Exception {
        OwnedStore s = ownedStore("rn-both");
        su(ctx -> {
            int n = ctx.update(CATALOG_DOCUMENT_CHUNKS)
                .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, s.renamed).set(CATALOG_DOCUMENT_CHUNKS.CHASH, bytes(s.third))
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(s.tenant)).and(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(bytes(s.first))).execute();
            assertThat(n).isEqualTo(1);
            assertThat(orphans(ctx, s.tenant)).as("the first chunk lost its owner; the second kept its own").containsExactly(s.first);
        });
    }

    // ---- fixtures -------------------------------------------------------------------------------------------------------------

    /** A collection whose two chunks each have a manifest row of one owner document, and a third chunk with none. */
    private record OwnedStore(String tenant, String live, String renamed, long id, String first, String second, String third) {}

    private OwnedStore ownedStore(String tag) throws Exception {
        String tenant = tenant(tag), live = collection(tag), renamed = collection(tag + "-renamed");
        List<String> h = hashes(tenant, 3);
        long[] id = new long[1];
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, live);
            seed(ctx, tenant, live, h);
            PgContainerHelper.ownChunks(ctx, tenant, live, h.get(0), h.get(1));
            id[0] = CollectionIds.require(ctx, tenant, live);
            assertThat(orphans(ctx, tenant)).as("precondition: no orphan is recorded yet").isEmpty();
        });
        return new OwnedStore(tenant, live, renamed, id[0], h.get(0), h.get(1), h.get(2));
    }

    /**
     * One chunk in {@code live}, ready to be moved to {@code quarantine}, and a racer: a chunk of another name that carries the
     * sibling's id and the same chash, so the move's insert meets the id twin and not the name key. Runs {@code fn} under the gate and
     * checks what {@code mode} says.
     */
    private void chunkRace(String tag, MoveFn fn, Mode mode) throws Exception {
        String tenant = tenant(tag), live = collection(tag), quarantine = QUARANTINE + live, racer = "stale-" + tag;
        String x = hashes(tenant, 1).get(0);
        String[] twin = new String[1];
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, live);
            PgContainerHelper.insertCollection(ctx, tenant, quarantine);
            seed(ctx, tenant, live, List.of(x));
            age(ctx, tenant);
            long sibling = CollectionIds.require(ctx, tenant, quarantine);
            insertChunk(ctx, tenant, racer, sibling, x, quarantine);
            twin[0] = violatedBy(() -> insertChunk(ctx, tenant, "probe-" + tag, sibling, x, quarantine));
            assertThat(twin[0]).as("non-vacuity: the racer conflicts on the leaf's id key and on nothing else").endsWith("_cid_uq");
        });
        if (mode == Mode.OTHER_INDEX) {
            // the leaf's id key is dropped and a unique key over three of its four columns stands in: the same conflict, a foreign key
            String leaf = twin[0].substring(0, twin[0].length() - "_cid_uq".length());
            try (Connection c = pg.createConnection("")) {
                c.setAutoCommit(true);
                PgContainerHelper.dropIndex(c, DSL.name("nexus", twin[0]));
                DSL.using(c, SQLDialect.POSTGRES).createUniqueIndex(DSL.name(CHUNK_OTHER_KEY))
                    .on(DSL.table(DSL.name("nexus", leaf)), DSL.field(DSL.name("tenant_id")), DSL.field(DSL.name("collection_id")), DSL.field(DSL.name("chash")))
                    .execute();
            }
        }
        armGate(quarantine, racer, mode == Mode.RETRY_SUCCEEDS ? 2 : 99);
        try {
            String refusal = refusal(() -> su(ctx -> {
                switch (fn) {
                    case GC_BOUNDED -> ctx.selectFrom(GC_QUARANTINE_ORPHANS_BOUNDED.call(DIM, tenant, live, quarantine, TS, 20, 100)).fetch();
                    case REAPER -> ctx.selectFrom(REAPER_QUARANTINE_CHUNKS.call(tenant, live, quarantine, TS, 100, null, 1.0, 0, false)).fetch();
                    case GC -> ctx.selectFrom(GC_QUARANTINE_ORPHANS.call(DIM, tenant, live, quarantine, TS, 20)).fetch();
                }
            }));
            su(ctx -> {
                long sibling = CollectionIds.require(ctx, tenant, quarantine);
                int movedRows = ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(quarantine)).and(CHUNKS.COLLECTION_ID.eq(sibling))
                    .and(CHUNKS.CHASH.eq(bytes(x))));
                int racerRows = ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(racer)));
                int liveRows = ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(live)).and(CHUNKS.CHASH.eq(bytes(x))));
                switch (mode) {
                    case RETRY_SUCCEEDS -> {
                        assertThat(refusal).as("one retry, then success").isNull();
                        assertThat(attempts(ctx)).as("exactly one retry: two inserts of the sibling's row").isEqualTo(2L);
                        assertThat(movedRows).as("the chunk moved to the sibling, under the sibling's id").isEqualTo(1);
                        assertThat(liveRows).as("and left the collection").isZero();
                        assertThat(racerRows).as("the racer's row is gone: the retry saw the committed state").isZero();
                    }
                    case RACER_STAYS -> {
                        assertThat(refusal).as("the second 23505 is raised").isNotNull().startsWith("23505").contains(twin[0]);
                        assertThat(attempts(ctx)).as("after exactly two attempts").isEqualTo(2L);
                        assertThat(movedRows).as("nothing moved: the call rolled back").isZero();
                        assertThat(liveRows).isEqualTo(1);
                    }
                    case OTHER_INDEX -> {
                        assertThat(refusal).as("a 23505 on another unique index is raised").isNotNull().startsWith("23505").contains(CHUNK_OTHER_KEY);
                        assertThat(attempts(ctx)).as("at the first attempt, with no retry").isEqualTo(1L);
                        assertThat(movedRows).isZero();
                        assertThat(liveRows).isEqualTo(1);
                    }
                }
            });
        } finally {
            disarmGate();
            dropIndexes(CHUNK_OTHER_KEY);
        }
    }

    /**
     * A chunk with an owner row, and a racer: an orphaning record of another name carrying the same id key, which the insert's name key
     * does not match. The trigger statement (a manifest DELETE or UPDATE) drops the owner, so the trigger inserts the chunk's record.
     */
    private void orphanRace(String tag, Trigger trigger, Mode mode) throws Exception {
        String tenant = tenant(tag), live = collection(tag), other = collection(tag + "-other"), racer = "stale-" + tag;
        List<String> h = hashes(tenant, 2);
        String x = h.get(0), y = h.get(1);
        long[] ids = new long[2];
        String indexName = mode == Mode.OTHER_INDEX ? ORPHAN_OTHER_KEY : ORPHAN_ID_KEY;
        dropIndexes(ORPHAN_ID_KEY, ORPHAN_OTHER_KEY);
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, live);
            PgContainerHelper.insertCollection(ctx, tenant, other);
            seed(ctx, tenant, live, h);
            PgContainerHelper.ownChunks(ctx, tenant, live, x);
            age(ctx, tenant);
            ids[0] = CollectionIds.require(ctx, tenant, live);
            ids[1] = CollectionIds.require(ctx, tenant, other);
            // the racer's chunk row (its record's foreign key names it) carries another collection's id, so the chunks' own id key is not met
            insertChunk(ctx, tenant, racer, ids[1], x, live);
            String model = PgContainerHelper.collectionModel(ctx, tenant, live);
            // vectors-079's trigger would store the racer's record under the chunk's own name (the live collection's), so the racer would
            // not hold the stale name the race needs; it is written with that trigger off, the state of a record written before P3.7
            withTriggerOff(ctx, "chunk_orphaned_at", "trg_chunk_orphaned_at_name_from_chunk", () -> ctx.insertInto(CHUNK_ORPHANED_AT)
                .set(CHUNK_ORPHANED_AT.TENANT_ID, tenant).set(CHUNK_ORPHANED_AT.COLLECTION, racer).set(CHUNK_ORPHANED_AT.COLLECTION_ID, ids[0])
                .set(CHUNK_ORPHANED_AT.CHASH, bytes(x)).set(CHUNK_ORPHANED_AT.ORPHANED_AT, OffsetDateTime.now().minusDays(2))
                .set(CHUNK_ORPHANED_AT.EMBEDDING_MODEL, model).execute());
            if (mode == Mode.OTHER_INDEX) {
                ctx.createUniqueIndexIfNotExists(DSL.name(indexName)).on(CHUNK_ORPHANED_AT, CHUNK_ORPHANED_AT.TENANT_ID,
                    CHUNK_ORPHANED_AT.COLLECTION_ID, CHUNK_ORPHANED_AT.CHASH, CHUNK_ORPHANED_AT.EMBEDDING_MODEL).execute();
            } else {
                // P3.4's id key, made here when P3.4 is not on this base
                ctx.createUniqueIndexIfNotExists(DSL.name(indexName)).on(CHUNK_ORPHANED_AT, CHUNK_ORPHANED_AT.TENANT_ID,
                    CHUNK_ORPHANED_AT.COLLECTION_ID, CHUNK_ORPHANED_AT.CHASH).execute();
            }
            String probe = violatedBy(() -> ctx.insertInto(CHUNK_ORPHANED_AT)
                .set(CHUNK_ORPHANED_AT.TENANT_ID, tenant).set(CHUNK_ORPHANED_AT.COLLECTION, live).set(CHUNK_ORPHANED_AT.COLLECTION_ID, ids[0])
                .set(CHUNK_ORPHANED_AT.CHASH, bytes(x)).set(CHUNK_ORPHANED_AT.ORPHANED_AT, OffsetDateTime.now())
                .set(CHUNK_ORPHANED_AT.EMBEDDING_MODEL, model).execute());
            assertThat(probe).as("non-vacuity: the trigger's own row conflicts with the racer on this key and on nothing else").isEqualTo(indexName);
        });
        armGate(live, racer, mode == Mode.RETRY_SUCCEEDS ? 2 : 99);
        try {
            String refusal = refusal(() -> su(ctx -> {
                switch (trigger) {
                    case MANIFEST_DELETE -> ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS)
                        .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(bytes(x))).execute();
                    case MANIFEST_UPDATE -> ctx.update(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.CHASH, bytes(y))
                        .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(bytes(x))).execute();
                }
            }));
            su(ctx -> {
                int own = ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(bytes(x))));
                int record = ctx.fetchCount(CHUNK_ORPHANED_AT, CHUNK_ORPHANED_AT.TENANT_ID.eq(tenant).and(CHUNK_ORPHANED_AT.COLLECTION.eq(live))
                    .and(CHUNK_ORPHANED_AT.COLLECTION_ID.eq(ids[0])).and(CHUNK_ORPHANED_AT.CHASH.eq(bytes(x))));
                int racerRows = ctx.fetchCount(CHUNK_ORPHANED_AT, CHUNK_ORPHANED_AT.TENANT_ID.eq(tenant).and(CHUNK_ORPHANED_AT.COLLECTION.eq(racer)));
                switch (mode) {
                    case RETRY_SUCCEEDS -> {
                        assertThat(refusal).as("one retry, then success").isNull();
                        assertThat(attempts(ctx)).as("exactly one retry: two inserts of the record").isEqualTo(2L);
                        assertThat(record).as("the chunk's orphaning is recorded under its name and id").isEqualTo(1);
                        assertThat(racerRows).as("the racer's record is gone").isZero();
                        assertThat(own).as("the statement took effect").isEqualTo(0);
                    }
                    case RACER_STAYS -> {
                        assertThat(refusal).as("the second 23505 is raised").isNotNull().startsWith("23505").contains(indexName);
                        assertThat(attempts(ctx)).as("after exactly two attempts").isEqualTo(2L);
                        assertThat(record).isZero();
                        assertThat(own).as("the statement rolled back: the owner row is still there").isEqualTo(1);
                    }
                    case OTHER_INDEX -> {
                        assertThat(refusal).as("a 23505 on another unique index is raised").isNotNull().startsWith("23505").contains(indexName);
                        assertThat(attempts(ctx)).as("at the first attempt, with no retry").isEqualTo(1L);
                        assertThat(record).isZero();
                        assertThat(own).isEqualTo(1);
                    }
                }
            });
        } finally {
            disarmGate();
            dropIndexes(ORPHAN_ID_KEY, ORPHAN_OTHER_KEY);
        }
    }

    /** Arms the gate for the next insert that names {@code target}: the attempt numbering starts again at 1. */
    private void armGate(String target, String racer, int deleteOnAttempt) {
        su(ctx -> {
            ctx.deleteFrom(GATE_CFG).execute();
            ctx.insertInto(GATE_CFG, CFG_TARGET, CFG_RACER, CFG_DELETE_ON).values(target, racer, (long) deleteOnAttempt).execute();
            ctx.alterSequence(GATE_SEQ).restart().execute();
        });
    }

    private void disarmGate() {
        su(ctx -> ctx.deleteFrom(GATE_CFG).execute());
    }

    private void dropIndexes(String... names) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            for (String name : names) {
                PgContainerHelper.dropIndexIfExists(c, DSL.name("nexus", name));
            }
        }
    }

    /** How many attempts the gate has seen: the sequence's last value, or 0 while it has not been called. */
    private static long attempts(DSLContext ctx) {
        Record r = ctx.select(DSL.field(DSL.name("last_value"), Long.class), DSL.field(DSL.name("is_called"), Boolean.class))
            .from(DSL.table(DSL.name("nexus_test", "p35b_retry_gate_seq"))).fetchOne();
        return Boolean.TRUE.equals(r.get(1, Boolean.class)) ? r.get(0, Long.class) : 0L;
    }

    private static void insertChunk(DSLContext ctx, String tenant, String collection, long id, String hex, String modelOf) {
        ctx.insertInto(CHUNKS)
            .set(CHUNKS.TENANT_ID, tenant).set(CHUNKS.COLLECTION, collection).set(CHUNKS.COLLECTION_ID, id).set(CHUNKS.CHASH, bytes(hex))
            .set(CHUNKS.EMBEDDING_MODEL, PgContainerHelper.collectionModel(ctx, tenant, modelOf))
            .set(CHUNKS.CHUNK_TEXT, "racer " + collection)
            .set(VectorSeeds.chunkEmbedding(DIM), VectorSeeds.vec(DIM, 0.1f))
            .set(CHUNKS.METADATA, JSONB.valueOf("{}"))
            .execute();
    }

    /** The name of the unique key {@code write} violates; the test fails when it does not violate one. */
    private static String violatedBy(Runnable write) {
        String message = refusal(write);
        assertThat(message).as("the write is refused").isNotNull().startsWith("23505");
        Matcher m = VIOLATED.matcher(message);
        assertThat(m.find()).as("the refusal names its unique key: %s", message).isTrue();
        return m.group(1);
    }

    /** The SQLSTATE and message of the failure, "state: message" (messages of the causes included); null when {@code body} succeeded. */
    private static String refusal(Runnable body) {
        try {
            body.run();
            return null;
        } catch (RuntimeException t) {
            String state = null;
            StringBuilder text = new StringBuilder();
            for (Throwable c = t; c != null; c = c.getCause() == c ? null : c.getCause()) {
                if (c instanceof SQLException se && se.getSQLState() != null && state == null) {
                    state = se.getSQLState();
                }
                text.append(' ').append(c.getMessage());
            }
            return state + ":" + text;
        }
    }

    private static List<String> orphans(DSLContext ctx, String tenant) {
        return ctx.select(CHUNK_ORPHANED_AT.CHASH).from(CHUNK_ORPHANED_AT).where(CHUNK_ORPHANED_AT.TENANT_ID.eq(tenant))
            .fetch(r -> HexFormat.of().formatHex(r.value1()));
    }

    private static void seed(DSLContext ctx, String tenant, String collection, List<String> hexes) {
        List<float[]> vectors = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (String hex : hexes) {
            float[] v = new float[DIM];
            java.util.Arrays.fill(v, 0.1f);
            vectors.add(v);
            texts.add("text " + hex.substring(0, 8));
            metas.add(Map.of());
        }
        PgContainerHelper.insertChunks(ctx, tenant, collection, hexes, texts, vectors, metas);
    }

    private static void age(DSLContext ctx, String tenant) {
        OffsetDateTime then = OffsetDateTime.now().minus(Duration.ofDays(90));
        ctx.update(CHUNKS).set(CHUNKS.CREATED_AT, then).set(CHUNKS.LAST_WRITTEN_AT, then).where(CHUNKS.TENANT_ID.eq(tenant)).execute();
    }

    private static String tenant(String tag) {
        return "p35b-rty-" + tag;
    }

    private static String collection(String tag) {
        return "docs__p35brty-" + tag + "__bge-base-en-v15-768__v1";
    }

    private static List<String> hashes(String tenant, int n) {
        List<String> out = new ArrayList<>();
        for (int i = 1; i <= n; i++) {
            out.add(Chash.ofText(tenant + "/chunk-" + i).toHex());
        }
        return out;
    }

    /** Runs {@code work} with {@code trigger} of {@code table} disabled ({@link PgContainerHelper#disableTrigger}), re-enabled after. */
    private static <T> T withTriggerOff(DSLContext ctx, String table, String trigger, java.util.function.Supplier<T> work) {
        ctx.connection(c -> {
            try {
                PgContainerHelper.disableTrigger(c, table, trigger);
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
        try {
            return work.get();
        } finally {
            ctx.connection(c -> {
                try {
                    PgContainerHelper.enableTrigger(c, table, trigger);
                } catch (Exception e) {
                    throw new IllegalStateException(e);
                }
            });
        }
    }

    private static byte[] bytes(String hex) {
        return Chash.fromHex(hex).toBytes();
    }

    private void su(Consumer<DSLContext> work) {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            work.accept(DSL.using(c, SQLDialect.POSTGRES));
        } catch (SQLException e) {
            throw new IllegalStateException(e);
        }
    }
}
