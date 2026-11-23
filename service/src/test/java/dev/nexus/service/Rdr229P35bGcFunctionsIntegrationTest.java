// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.jooq.nexus.Routines;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Record;
import org.jooq.Result;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Consumer;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.GC_QUARANTINE_ORPHANS;
import static dev.nexus.service.jooq.nexus.Tables.GC_QUARANTINE_ORPHANS_BOUNDED;
import static dev.nexus.service.jooq.nexus.Tables.GC_QUARANTINE_ORPHANS_FLOORED;
import static dev.nexus.service.jooq.nexus.Tables.GC_RESTORE_REREFERENCED_BOUNDED;
import static dev.nexus.service.jooq.nexus.Tables.QUARANTINE_RESTORE_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.REAPER_QUARANTINE_CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5b (nexus-p2m1x.24), the quarantine and restore functions that the form B rewrite changed, run on the two
 * stores the readers must hold at once.
 *
 * <p>A renamed registry row. P3.7's rename rewrites no name column: the registry row takes the new label and every chunk and
 * manifest row keeps the old one beside the unchanged id. A caller names the collection by its live label, and
 * {@code nexus.chunk_is_reapable} resolves a label to the registry id. The functions must pass that label, never the chunk's own
 * (stale) name column, or a chunk with a live owner is judged reapable and quarantined. Each case below owns three chunks, leaves
 * one with no owner, renames the registry row only, and runs one function past the grace: the owned chunks must stay and the
 * unowned one must move.
 *
 * <p>A half-stamped store. While a stamp is unfinished some chunks and some manifest rows carry the id and some do not; the same
 * four combinations of chunk and manifest, and two unowned chunks, one of each, go through every function.
 *
 * <p>Fixture. One database, walked to HEAD, with P3.7's name-key drop (the registry's name keys and the manifest's key to the chunk)
 * done once for the class. Every case has a tenant of its own. A NULL-id row is written the way a stamp in progress leaves it: the
 * P3.1 CHECKs that refuse it are dropped around the update and put back NOT VALID by the operator's re-add.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P35bGcFunctionsIntegrationTest {

    private static final String TS = "2026-11-23T00:00:00Z";
    private static final int DIM = 768;
    private static final Duration AGE = Duration.ofDays(90);
    private static final String QUARANTINE = "quarantine-";

    private PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAndDropTheNameKeys() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        su(ctx -> {
            assertThat(ByIdRestoreSupport.dropRegistryNameKeys(ctx))
                .as("non-vacuity: the registry's name keys exist and are dropped").isNotEmpty();
        });
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.dropCatalogChunksFk(su);
        }
    }

    @AfterAll
    void stop() {
        if (pg != null) {
            pg.stop();
        }
    }

    // ---- a renamed registry row: nothing with a live owner moves ---------------------------------------------------------

    @Test
    void renamed_gcQuarantineOrphans() throws Exception {
        renamedMovesOnlyTheUnowned("r-gc", (ctx, s) ->
            first(ctx.selectFrom(GC_QUARANTINE_ORPHANS.call(DIM, s.tenant, s.live, s.quarantine, TS, 20)).fetch()).get("moved", Long.class));
    }

    @Test
    void renamed_gcQuarantineOrphansBounded() throws Exception {
        renamedMovesOnlyTheUnowned("r-bd", (ctx, s) ->
            first(ctx.selectFrom(GC_QUARANTINE_ORPHANS_BOUNDED.call(DIM, s.tenant, s.live, s.quarantine, TS, 20, 100)).fetch()).get("moved", Long.class));
    }

    @Test
    void renamed_gcQuarantineOrphansFloored_noFloor() throws Exception {
        renamedMovesOnlyTheUnowned("r-fn", (ctx, s) ->
            first(ctx.selectFrom(GC_QUARANTINE_ORPHANS_FLOORED.call(DIM, s.tenant, s.live, s.quarantine, TS, 20, null, null, null, false, null)).fetch())
                .get("moved", Long.class));
    }

    /** The floor reads the reapable share of the whole collection: three owned and one not is 25%, under 90%. A name column read as the label makes it 100%. */
    @Test
    void renamed_gcQuarantineOrphansFloored_judgedBounded() throws Exception {
        renamedMovesOnlyTheUnowned("r-fb", (ctx, s) -> {
            Record row = first(ctx.selectFrom(GC_QUARANTINE_ORPHANS_FLOORED.call(DIM, s.tenant, s.live, s.quarantine, TS, 20, 100, 0.9, 0, false, null)).fetch());
            assertThat(row.get("refused", Boolean.class)).as("the floor is not crossed: one of four is reapable").isFalse();
            assertThat(row.get("reapable_count", Long.class)).isEqualTo(1L);
            assertThat(row.get("total_count", Long.class)).isEqualTo(4L);
            return row.get("moved", Long.class);
        });
    }

    @Test
    void renamed_gcQuarantineOrphansFloored_judgedUnbounded() throws Exception {
        renamedMovesOnlyTheUnowned("r-fu", (ctx, s) -> {
            Record row = first(ctx.selectFrom(GC_QUARANTINE_ORPHANS_FLOORED.call(DIM, s.tenant, s.live, s.quarantine, TS, 20, null, 0.9, 0, false, null)).fetch());
            assertThat(row.get("refused", Boolean.class)).as("the floor is not crossed: one of four is reapable").isFalse();
            assertThat(row.get("reapable_count", Long.class)).isEqualTo(1L);
            assertThat(row.get("total_count", Long.class)).isEqualTo(4L);
            return row.get("moved", Long.class);
        });
    }

    @Test
    void renamed_reaperQuarantineChunks_dryRunCountsOnlyTheUnowned() throws Exception {
        Store s = renamedStore("r-rd");
        su(ctx -> {
            Record row = first(ctx.selectFrom(REAPER_QUARANTINE_CHUNKS.call(s.tenant, s.live, s.quarantine, TS, 100, null, 0.5, 0, true)).fetch());
            assertThat(row.get("reapable_count", Long.class)).as("the unowned chunk alone is reapable").isEqualTo(1L);
            assertThat(row.get("total_count", Long.class)).isEqualTo(4L);
            assertThat(row.get("moved", Long.class)).as("a dry run moves nothing").isZero();
            assertNothingMoved(ctx, s);
        });
    }

    @Test
    void renamed_reaperQuarantineChunks() throws Exception {
        renamedMovesOnlyTheUnowned("r-rp", (ctx, s) -> {
            Record row = first(ctx.selectFrom(REAPER_QUARANTINE_CHUNKS.call(s.tenant, s.live, s.quarantine, TS, 100, null, 0.5, 0, false)).fetch());
            assertThat(row.get("refused", Boolean.class)).as("the floor is not crossed: one of four is reapable").isFalse();
            return row.get("moved", Long.class);
        });
    }

    // ---- a half-stamped store: the four chunk and manifest combinations, and two unowned chunks ------------------------------

    @Test
    void halfStamped_gcQuarantineOrphans() throws Exception {
        halfStampedMovesOnlyTheUnowned("h-gc", (ctx, s) ->
            first(ctx.selectFrom(GC_QUARANTINE_ORPHANS.call(DIM, s.tenant, s.live, s.quarantine, TS, 20)).fetch()).get("moved", Long.class));
    }

    @Test
    void halfStamped_gcQuarantineOrphansBounded() throws Exception {
        halfStampedMovesOnlyTheUnowned("h-bd", (ctx, s) ->
            first(ctx.selectFrom(GC_QUARANTINE_ORPHANS_BOUNDED.call(DIM, s.tenant, s.live, s.quarantine, TS, 20, 100)).fetch()).get("moved", Long.class));
    }

    @Test
    void halfStamped_gcQuarantineOrphansFloored() throws Exception {
        halfStampedMovesOnlyTheUnowned("h-fl", (ctx, s) -> {
            Record row = first(ctx.selectFrom(GC_QUARANTINE_ORPHANS_FLOORED.call(DIM, s.tenant, s.live, s.quarantine, TS, 20, 100, 0.9, 0, false, null)).fetch());
            assertThat(row.get("refused", Boolean.class)).as("two of six are reapable: under the floor").isFalse();
            assertThat(row.get("reapable_count", Long.class)).isEqualTo(2L);
            assertThat(row.get("total_count", Long.class)).isEqualTo(6L);
            return row.get("moved", Long.class);
        });
    }

    @Test
    void halfStamped_reaperQuarantineChunks() throws Exception {
        halfStampedMovesOnlyTheUnowned("h-rp", (ctx, s) -> {
            Record row = first(ctx.selectFrom(REAPER_QUARANTINE_CHUNKS.call(s.tenant, s.live, s.quarantine, TS, 100, null, 0.5, 0, false)).fetch());
            assertThat(row.get("refused", Boolean.class)).isFalse();
            return row.get("moved", Long.class);
        });
    }

    // ---- a half-stamped quarantine: the restores ------------------------------------------------------------------------------

    /**
     * Four chunks in quarantine, each owned again by a manifest row of the origin: chunk and manifest row stamped, only the manifest
     * row, only the chunk, neither. All four come back, each on the origin's id, and a fifth with no owner stays.
     */
    @Test
    void halfStamped_gcRestoreRereferenced() throws Exception {
        RestoreStore s = restoreStore("h-r1");
        su(ctx -> {
            Long restored = Routines.gcRestoreRereferenced(ctx.configuration(), DIM, s.tenant, s.quarantine, s.live);
            assertThat(restored).as("the four re-owned chunks are restored whatever their stamp").isEqualTo(4L);
            assertRestored(ctx, s);
        });
    }

    @Test
    void halfStamped_gcRestoreRereferencedBounded() throws Exception {
        RestoreStore s = restoreStore("h-r2");
        su(ctx -> {
            Record row = first(ctx.selectFrom(GC_RESTORE_REREFERENCED_BOUNDED.call(DIM, s.tenant, s.quarantine, s.live, 100)).fetch());
            assertThat(row.get("restored", Long.class)).isEqualTo(4L);
            assertThat(row.get("remaining", Long.class)).isZero();
            assertRestored(ctx, s);
        });
    }

    /**
     * Four named chunks, two present at the origin already (one of each stamp) and two not. The present ones stay in quarantine
     * whatever their stamp; the others come back on the origin's id. The dry run says the same.
     */
    @Test
    void halfStamped_quarantineRestoreChunks() throws Exception {
        String tenant = tenant("h-qr"), live = collection("h-qr"), quarantine = QUARANTINE + live;
        List<String> hexes = hashes(tenant, "qr", 4);
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, live);
            PgContainerHelper.insertCollection(ctx, tenant, quarantine);
            // quarantine copies of all four; the origin already holds the third (stamped) and the fourth (to be unstamped)
            seed(ctx, tenant, quarantine, hexes, Map.of("origin_collection", live, "quarantined_at", TS));
            seed(ctx, tenant, live, hexes.subList(2, 4), Map.of());
            unstamp(tenant,
                CHUNKS.COLLECTION.eq(quarantine).and(CHUNKS.CHASH.in(bytes(List.of(hexes.get(1), hexes.get(3)))))
                    .or(CHUNKS.COLLECTION.eq(live).and(CHUNKS.CHASH.eq(bytes(hexes.get(3))))),
                null);
        });
        String[] named = hexes.toArray(String[]::new);
        su(ctx -> {
            Map<String, String> dry = outcomes(ctx.selectFrom(QUARANTINE_RESTORE_CHUNKS.call(tenant, live, quarantine, named, "p35b", null, true, false)).fetch());
            assertThat(dry).as("the dry run's verdicts").containsEntry(hexes.get(0), "would_restore").containsEntry(hexes.get(1), "would_restore")
                .containsEntry(hexes.get(2), "present").containsEntry(hexes.get(3), "present");
            Map<String, String> done = outcomes(ctx.selectFrom(QUARANTINE_RESTORE_CHUNKS.call(tenant, live, quarantine, named, "p35b", null, false, false)).fetch());
            assertThat(done).as("the restore's verdicts").containsEntry(hexes.get(0), "restored").containsEntry(hexes.get(1), "restored")
                .containsEntry(hexes.get(2), "present").containsEntry(hexes.get(3), "present");
            long liveId = CollectionIds.require(ctx, tenant, live);
            long quarId = CollectionIds.require(ctx, tenant, quarantine);
            assertThat(inCollection(ctx, tenant, liveId, live, hexes)).as("the origin now holds all four").containsExactlyInAnyOrderElementsOf(hexes);
            assertThat(inCollection(ctx, tenant, quarId, quarantine, hexes)).as("quarantine keeps the two that were already present")
                .containsExactlyInAnyOrder(hexes.get(2), hexes.get(3));
            assertThat(ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(live)).and(CHUNKS.COLLECTION_ID.eq(liveId))
                    .and(CHUNKS.CHASH.in(bytes(hexes.subList(0, 2))))))
                .as("a restored row carries the origin's id").isEqualTo(2);
        });
    }

    // ---- a renamed origin: the restores called by its NEW label (P3.7's one-row rename: the children keep the old one) ---------------

    /** The registry row of {@code live} takes {@code live + "-renamed"} in place (the id stays); every chunk and manifest row keeps the old label. */
    private String renameOrigin(DSLContext ctx, String tenant, String live) {
        String neu = live + "-renamed";
        int n = ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, neu)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(live)).execute();
        assertThat(n).as("the origin's registry row is renamed in place").isEqualTo(1);
        return neu;
    }

    private static long rowsOfAChash(DSLContext ctx, String tenant, long id, String hex) {
        return ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION_ID.eq(id)).and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(hex))));
    }

    /** How a quarantined chunk's metadata names its origin besides the label: no id key (written before the id key), the id, or the JSON null marker of a dangling label. */
    private enum IdTag { NONE, ID, MARKER }

    /** The metadata of a quarantined chunk of {@code live} (registered, so it has an id), the id key as {@code idTag} says. */
    private static Map<String, Object> quarantineTag(DSLContext ctx, String tenant, String live, IdTag idTag) {
        Map<String, Object> meta = new LinkedHashMap<>();
        meta.put("origin_collection", live);
        meta.put("quarantined_at", TS);
        if (idTag == IdTag.ID) {
            meta.put("origin_collection_id", CollectionIds.require(ctx, tenant, live));
        } else if (idTag == IdTag.MARKER) {
            meta.put("origin_collection_id", null);
        }
        return meta;
    }

    /** Gate 3 is latched for the duration of {@code work} (the latch is install-wide and this class shares one database, so it is removed after). */
    private void whileGate3Latched(Consumer<DSLContext> work) throws Exception {
        su(ctx -> ctx.execute("INSERT INTO nexus.rdr229_gate_latch (gate, latched_at, engine_version) VALUES ('gate3', now(), 'p37-test') ON CONFLICT DO NOTHING"));
        try {
            su(work);
        } finally {
            su(ctx -> ctx.execute("DELETE FROM nexus.rdr229_gate_latch WHERE gate = 'gate3'"));
        }
    }

    /** Quarantined chunks r1..r4, each owned again by a manifest row of the origin, all stamped; r5 has no owner. Nothing is unstamped here. */
    private RestoreStore stampedRestoreStore(String tag, IdTag idTag) throws Exception {
        String tenant = tenant(tag), live = collection(tag), quarantine = QUARANTINE + live;
        List<String> r = hashes(tenant, "sr", 5);
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, live);
            PgContainerHelper.insertCollection(ctx, tenant, quarantine);
            seed(ctx, tenant, quarantine, r, quarantineTag(ctx, tenant, live, idTag));
            PgContainerHelper.ownChunks(ctx, tenant, live, r.subList(0, 4).toArray(String[]::new));
        });
        return new RestoreStore(tenant, live, quarantine, r.subList(0, 4), r.get(4));
    }

    /**
     * Before the latch the origin is still matched by the label tag, so a restore named by the origin's NEW label restores nothing: the one-row rename
     * only happens from gate 3, so this is the pre-latch rule held as it was, not a gap.
     */
    @Test
    void renamed_gcRestoreRereferenced_beforeTheLatch_namedByTheNewLabel_restoresNothing() throws Exception {
        RestoreStore s = stampedRestoreStore("rn-r1", IdTag.ID);
        su(ctx -> {
            String neu = renameOrigin(ctx, s.tenant, s.live);
            Long restored = Routines.gcRestoreRereferenced(ctx.configuration(), DIM, s.tenant, s.quarantine, neu);
            assertThat(restored).as("nothing is restored before the latch: the chunks' origin_collection is %s, not %s", s.live, neu).isZero();
            assertThat(inCollection(ctx, s.tenant, CollectionIds.require(ctx, s.tenant, s.quarantine), s.quarantine, concat(s.reowned, List.of(s.lone))))
                .as("every quarantined chunk stays").containsExactlyInAnyOrderElementsOf(concat(s.reowned, List.of(s.lone)));
        });
    }

    @Test
    void renamed_gcRestoreRereferenced_namedByTheNewLabel_restoresOneRowEach() throws Exception {
        RestoreStore s = stampedRestoreStore("rn-r2", IdTag.ID);
        whileGate3Latched(ctx -> {
            String neu = renameOrigin(ctx, s.tenant, s.live);
            long liveId = CollectionIds.require(ctx, s.tenant, neu);
            Long restored = Routines.gcRestoreRereferenced(ctx.configuration(), DIM, s.tenant, s.quarantine, neu);
            assertThat(restored).as("the four re-owned chunks come back under the origin's new label").isEqualTo(4L);
            for (String h : s.reowned) {
                assertThat(rowsOfAChash(ctx, s.tenant, liveId, h)).as("one row for %s on the origin's id", h).isEqualTo(1L);
            }
            assertThat(inCollection(ctx, s.tenant, CollectionIds.require(ctx, s.tenant, s.quarantine), s.quarantine, concat(s.reowned, List.of(s.lone))))
                .as("quarantine keeps the chunk with no owner").containsExactly(s.lone);
        });
    }

    @Test
    void renamed_gcRestoreRereferencedBounded_namedByTheNewLabel_restoresOneRowEach() throws Exception {
        RestoreStore s = stampedRestoreStore("rn-r3", IdTag.ID);
        whileGate3Latched(ctx -> {
            String neu = renameOrigin(ctx, s.tenant, s.live);
            long liveId = CollectionIds.require(ctx, s.tenant, neu);
            Record row = first(ctx.selectFrom(GC_RESTORE_REREFERENCED_BOUNDED.call(DIM, s.tenant, s.quarantine, neu, 100)).fetch());
            assertThat(row.get("restored", Long.class)).as("the four re-owned chunks come back under the origin's new label").isEqualTo(4L);
            assertThat(row.get("remaining", Long.class)).as("nothing restorable is left").isZero();
            for (String h : s.reowned) {
                assertThat(rowsOfAChash(ctx, s.tenant, liveId, h)).as("one row for %s on the origin's id", h).isEqualTo(1L);
            }
        });
    }

    /** A marker row (JSON null id: the origin had no registry row) is not restorable by id after the latch, and does not fall back to the label. */
    @Test
    void latched_gcRestoreRereferenced_aMarkerRowIsNotRestored() throws Exception {
        RestoreStore s = stampedRestoreStore("rn-r4", IdTag.MARKER);
        whileGate3Latched(ctx -> {
            assertThat(Routines.gcRestoreRereferenced(ctx.configuration(), DIM, s.tenant, s.quarantine, s.live))
                .as("the label matches the tag, but the id key is the null marker: nothing is restored after the latch").isZero();
            assertThat(inCollection(ctx, s.tenant, CollectionIds.require(ctx, s.tenant, s.quarantine), s.quarantine, concat(s.reowned, List.of(s.lone))))
                .as("every quarantined chunk stays").containsExactlyInAnyOrderElementsOf(concat(s.reowned, List.of(s.lone)));
            Record row = first(ctx.selectFrom(GC_RESTORE_REREFERENCED_BOUNDED.call(DIM, s.tenant, s.quarantine, s.live, 100)).fetch());
            assertThat(row.get("restored", Long.class)).as("the bounded restore agrees").isZero();
        });
    }

    /** Four quarantined chunks of {@code live}, the origin already holding the third and the fourth. */
    private List<String> quarantinedWithTwoAtTheOrigin(String tag, String tenant, String live, String quarantine, IdTag idTag) throws Exception {
        List<String> hexes = hashes(tenant, tag, 4);
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, live);
            PgContainerHelper.insertCollection(ctx, tenant, quarantine);
            seed(ctx, tenant, quarantine, hexes, quarantineTag(ctx, tenant, live, idTag));
            seed(ctx, tenant, live, hexes.subList(2, 4), Map.of());
        });
        return hexes;
    }

    private void assertRestoreVerdictsOnARenamedOrigin(DSLContext ctx, String tenant, String named, String quarantine, List<String> hexes, long liveId) {
        String[] chashes = hexes.toArray(String[]::new);
        Map<String, String> done = outcomes(ctx.selectFrom(QUARANTINE_RESTORE_CHUNKS.call(tenant, named, quarantine, chashes, "p37", null, false, false)).fetch());
        assertThat(done).as("the restore's verdicts, the origin named %s", named)
            .containsEntry(hexes.get(0), "restored").containsEntry(hexes.get(1), "restored")
            .containsEntry(hexes.get(2), "present").containsEntry(hexes.get(3), "present");
        for (String h : hexes) {
            assertThat(rowsOfAChash(ctx, tenant, liveId, h)).as("one row for %s on the origin's id, no duplicate", h).isEqualTo(1L);
        }
    }

    /** Before the latch, as above: by the new label the chunks the origin holds are "present" (found by id) and the quarantined ones are "missing". */
    @Test
    void renamed_quarantineRestoreChunks_beforeTheLatch_namedByTheNewLabel_restoresNothing() throws Exception {
        String tenant = tenant("rn-q1"), live = collection("rn-q1"), quarantine = QUARANTINE + live;
        List<String> hexes = quarantinedWithTwoAtTheOrigin("rnq1", tenant, live, quarantine, IdTag.ID);
        String[] chashes = hexes.toArray(String[]::new);
        su(ctx -> {
            String neu = renameOrigin(ctx, tenant, live);
            Map<String, String> done = outcomes(ctx.selectFrom(QUARANTINE_RESTORE_CHUNKS.call(tenant, neu, quarantine, chashes, "p37", null, false, false)).fetch());
            assertThat(done).as("before the latch the origin is found by id, the quarantined rows are not: their origin_collection is %s", live)
                .containsEntry(hexes.get(0), "missing").containsEntry(hexes.get(1), "missing")
                .containsEntry(hexes.get(2), "present").containsEntry(hexes.get(3), "present");
        });
    }

    @Test
    void renamed_quarantineRestoreChunks_namedByTheNewLabel_restoresAndSeesWhatTheOriginHolds() throws Exception {
        String tenant = tenant("rn-q2"), live = collection("rn-q2"), quarantine = QUARANTINE + live;
        List<String> hexes = quarantinedWithTwoAtTheOrigin("rnq2", tenant, live, quarantine, IdTag.ID);
        whileGate3Latched(ctx -> {
            String neu = renameOrigin(ctx, tenant, live);
            assertRestoreVerdictsOnARenamedOrigin(ctx, tenant, neu, quarantine, hexes, CollectionIds.require(ctx, tenant, neu));
        });
    }

    /** The reattach plan finds the quarantined chunk the same way: a dry run by the new label predicts the restore, where before it saw "missing". */
    @Test
    void renamed_quarantineRestoreChunks_dryRunByTheNewLabelPredictsTheRestore() throws Exception {
        String tenant = tenant("rn-q3"), live = collection("rn-q3"), quarantine = QUARANTINE + live;
        List<String> hexes = quarantinedWithTwoAtTheOrigin("rnq3", tenant, live, quarantine, IdTag.ID);
        String[] chashes = hexes.toArray(String[]::new);
        whileGate3Latched(ctx -> {
            String neu = renameOrigin(ctx, tenant, live);
            Map<String, String> dry = outcomes(ctx.selectFrom(QUARANTINE_RESTORE_CHUNKS.call(tenant, neu, quarantine, chashes, "p37", null, true, false)).fetch());
            assertThat(dry).containsEntry(hexes.get(0), "would_restore").containsEntry(hexes.get(1), "would_restore")
                .containsEntry(hexes.get(2), "present").containsEntry(hexes.get(3), "present");
        });
    }

    /** A marker row (JSON null id) is not restorable by id after the latch, named by the label it carries or not. */
    @Test
    void latched_quarantineRestoreChunks_aMarkerRowIsNotRestored() throws Exception {
        String tenant = tenant("rn-q4"), live = collection("rn-q4"), quarantine = QUARANTINE + live;
        List<String> hexes = quarantinedWithTwoAtTheOrigin("rnq4", tenant, live, quarantine, IdTag.MARKER);
        String[] chashes = hexes.toArray(String[]::new);
        whileGate3Latched(ctx -> {
            Map<String, String> done = outcomes(ctx.selectFrom(QUARANTINE_RESTORE_CHUNKS.call(tenant, live, quarantine, chashes, "p37", null, false, false)).fetch());
            assertThat(done).as("the label matches the tag, but the id key is the null marker: nothing moves after the latch")
                .containsEntry(hexes.get(0), "missing").containsEntry(hexes.get(1), "missing")
                .containsEntry(hexes.get(2), "present").containsEntry(hexes.get(3), "present");
            long quarId = CollectionIds.require(ctx, tenant, quarantine);
            assertThat(inCollection(ctx, tenant, quarId, quarantine, hexes)).as("every quarantined chunk stays").containsExactlyInAnyOrderElementsOf(hexes);
        });
    }

    // ---- fixtures ---------------------------------------------------------------------------------------------------------------

    /** A collection of one case: the label the caller names it by, and the sibling the moves write to. */
    private record Store(String tenant, String live, String quarantine, List<String> owned, List<String> unowned) {}

    private record RestoreStore(String tenant, String live, String quarantine, List<String> reowned, String lone) {}

    private interface Move {
        long run(DSLContext ctx, Store store);
    }

    /** Three owned chunks and one unowned, all 90 days old; the registry row renamed after, nothing else. */
    private Store renamedStore(String tag) throws Exception {
        String tenant = tenant(tag), old = collection(tag), live = collection(tag + "-renamed");
        List<String> owned = hashes(tenant, "own", 3), unowned = hashes(tenant, "orphan", 1);
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, old);
            seed(ctx, tenant, old, concat(owned, unowned), Map.of());
            PgContainerHelper.ownChunks(ctx, tenant, old, owned.toArray(String[]::new));
            age(ctx, tenant);
            long id = CollectionIds.require(ctx, tenant, old);
            int renamed = ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, live)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(old)).execute();
            assertThat(renamed).as("the registry row is renamed in place").isEqualTo(1);
            assertThat(CollectionIds.require(ctx, tenant, live)).as("a rename keeps the id").isEqualTo(id);
            assertThat(ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(old)).and(CHUNKS.COLLECTION_ID.eq(id))))
                .as("precondition: the four chunks still carry the old label beside the id").isEqualTo(4);
            assertThat(ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant)
                    .and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(old)).and(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID.eq(id))))
                .as("precondition: so do the three manifest rows").isEqualTo(3);
            assertThat(ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(live))))
                .as("precondition: no chunk carries the new label").isZero();
        });
        return new Store(tenant, live, QUARANTINE + live, owned, unowned);
    }

    /**
     * Chunks a..f of one collection: a, b, c and d owned (a: chunk and manifest row stamped, b: only the manifest row, c: only the
     * chunk, d: neither), e and f unowned (e stamped, f not). All 90 days old.
     */
    private Store halfStampedStore(String tag) throws Exception {
        String tenant = tenant(tag), live = collection(tag);
        List<String> h = hashes(tenant, "hs", 6);
        List<String> owned = h.subList(0, 4), unowned = h.subList(4, 6);
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, live);
            seed(ctx, tenant, live, h, Map.of());
            PgContainerHelper.ownChunks(ctx, tenant, live, owned.toArray(String[]::new));
            age(ctx, tenant);
            unstamp(tenant,
                CHUNKS.COLLECTION.eq(live).and(CHUNKS.CHASH.in(bytes(List.of(h.get(1), h.get(3), h.get(5))))),
                CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(live).and(CATALOG_DOCUMENT_CHUNKS.CHASH.in(bytes(List.of(h.get(2), h.get(3))))));
            assertThat(ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION_ID.isNull())))
                .as("non-vacuity: three chunks are unstamped").isEqualTo(3);
            assertThat(ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID.isNull())))
                .as("non-vacuity: two manifest rows are unstamped, three are stamped").isEqualTo(2);
        });
        return new Store(tenant, live, QUARANTINE + live, owned, unowned);
    }

    /** Quarantined chunks r1..r5 (r2 and r4 unstamped); manifest rows of the origin for r1..r4 (r3 and r4 unstamped); r5 has no owner. */
    private RestoreStore restoreStore(String tag) throws Exception {
        String tenant = tenant(tag), live = collection(tag), quarantine = QUARANTINE + live;
        List<String> r = hashes(tenant, "rs", 5);
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, live);
            PgContainerHelper.insertCollection(ctx, tenant, quarantine);
            seed(ctx, tenant, quarantine, r, Map.of("origin_collection", live, "quarantined_at", TS));
            PgContainerHelper.ownChunks(ctx, tenant, live, r.subList(0, 4).toArray(String[]::new));
            unstamp(tenant,
                CHUNKS.COLLECTION.eq(quarantine).and(CHUNKS.CHASH.in(bytes(List.of(r.get(1), r.get(3))))),
                CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(live).and(CATALOG_DOCUMENT_CHUNKS.CHASH.in(bytes(List.of(r.get(2), r.get(3))))));
            assertThat(ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION_ID.isNull())))
                .as("non-vacuity: two quarantined chunks are unstamped").isEqualTo(2);
        });
        return new RestoreStore(tenant, live, quarantine, r.subList(0, 4), r.get(4));
    }

    private void renamedMovesOnlyTheUnowned(String tag, Move move) throws Exception {
        Store s = renamedStore(tag);
        su(ctx -> {
            long moved = move.run(ctx, s);
            assertOnlyTheUnownedMoved(ctx, s, moved);
        });
    }

    private void halfStampedMovesOnlyTheUnowned(String tag, Move move) throws Exception {
        Store s = halfStampedStore(tag);
        su(ctx -> {
            long moved = move.run(ctx, s);
            assertOnlyTheUnownedMoved(ctx, s, moved);
        });
    }

    /** The call reported one move per unowned chunk, and the rows say the same: the owned chunks stay, the unowned ones are the sibling's. */
    private static void assertOnlyTheUnownedMoved(DSLContext ctx, Store s, long moved) {
        List<String> all = concat(s.owned, s.unowned);
        long liveId = CollectionIds.require(ctx, s.tenant, s.live);
        List<String> stayed = inCollection(ctx, s.tenant, liveId, s.live, all);
        assertThat(stayed).as("every owned chunk stays on the collection").containsAll(s.owned);
        assertThat(moved).as("the call reports one move per unowned chunk").isEqualTo(s.unowned.size());
        assertThat(stayed).as("and the unowned ones left it").doesNotContainAnyElementsOf(s.unowned);
        Long quarId = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(s.tenant)).and(CATALOG_COLLECTIONS.NAME.eq(s.quarantine)).fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
        assertThat(quarId).as("the move registered its sibling").isNotNull();
        assertThat(inCollection(ctx, s.tenant, quarId, s.quarantine, all)).as("the sibling holds the unowned chunks and no owned one")
            .containsExactlyInAnyOrderElementsOf(s.unowned);
    }

    private static void assertNothingMoved(DSLContext ctx, Store s) {
        List<String> all = concat(s.owned, s.unowned);
        assertThat(inCollection(ctx, s.tenant, CollectionIds.require(ctx, s.tenant, s.live), s.live, all))
            .as("a dry run leaves every chunk where it is").containsExactlyInAnyOrderElementsOf(all);
        assertThat(ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(s.tenant).and(CHUNKS.COLLECTION.eq(s.quarantine)))).as("and writes no sibling row").isZero();
    }

    private static void assertRestored(DSLContext ctx, RestoreStore s) {
        long liveId = CollectionIds.require(ctx, s.tenant, s.live);
        long quarId = CollectionIds.require(ctx, s.tenant, s.quarantine);
        List<String> all = concat(s.reowned, List.of(s.lone));
        assertThat(inCollection(ctx, s.tenant, liveId, s.live, all)).as("the origin holds the four re-owned chunks")
            .containsExactlyInAnyOrderElementsOf(s.reowned);
        assertThat(ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(s.tenant).and(CHUNKS.COLLECTION.eq(s.live)).and(CHUNKS.COLLECTION_ID.eq(liveId))))
            .as("each of them carries the origin's id").isEqualTo(4);
        assertThat(inCollection(ctx, s.tenant, quarId, s.quarantine, all)).as("quarantine keeps the chunk with no owner")
            .containsExactly(s.lone);
    }

    /**
     * The chashes among {@code candidates} that a collection holds by its id, or by name where the row has no id. {@code name} may be
     * null for a label the caller does not know the rows by (the owned chunks of a renamed collection carry the old one): then by id only.
     */
    private static List<String> inCollection(DSLContext ctx, String tenant, long id, String name, List<String> candidates) {
        var byId = CHUNKS.COLLECTION_ID.eq(id);
        var where = name == null ? byId : byId.or(CHUNKS.COLLECTION_ID.isNull().and(CHUNKS.COLLECTION.eq(name)));
        return ctx.select(CHUNKS.CHASH).from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(tenant)).and(where).and(CHUNKS.CHASH.in(bytes(candidates)))
            .fetch(r -> HexFormat.of().formatHex(r.value1()));
    }

    private static Map<String, String> outcomes(Result<? extends Record> rows) {
        Map<String, String> out = new LinkedHashMap<>();
        for (Record r : rows) {
            out.put(r.get("r_chash", String.class), r.get("r_outcome", String.class));
        }
        return out;
    }

    /**
     * Writes the unstamped shape of a stamp in progress: the id of the chunk rows of {@code tenant} matching {@code chunkWhere}, and of
     * its manifest rows matching {@code manifestWhere} (when given), is set NULL. The CHECKs that refuse such a row are dropped around
     * the update and put back by the operator's re-add (NOT VALID: no row is scanned).
     */
    private void unstamp(String tenant, Condition chunkWhere, Condition manifestWhere) {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.dropConstraint(su, CHUNKS, "chunks_collection_id_check");
            PgContainerHelper.dropConstraint(su, CHUNKS, "chunks_collection_id_not_null");
            PgContainerHelper.dropConstraint(su, CATALOG_DOCUMENT_CHUNKS, "catalog_document_chunks_collection_id_check");
            ctx.update(CHUNKS).setNull(CHUNKS.COLLECTION_ID).where(CHUNKS.TENANT_ID.eq(tenant)).and(chunkWhere).execute();
            if (manifestWhere != null) {
                ctx.update(CATALOG_DOCUMENT_CHUNKS).setNull(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID)
                    .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant)).and(manifestWhere).execute();
            }
            PgContainerHelper.runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-p31-readd.xml",
                "databasechangelog_test_p31_readd", Map.of());
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private static void seed(DSLContext ctx, String tenant, String collection, List<String> hexes, Map<String, Object> metadata) {
        List<float[]> vectors = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (String hex : hexes) {
            float[] v = new float[DIM];
            java.util.Arrays.fill(v, 0.1f);
            vectors.add(v);
            texts.add("text " + hex.substring(0, 8));
            metas.add(metadata);
        }
        PgContainerHelper.insertChunks(ctx, tenant, collection, hexes, texts, vectors, metas);
    }

    private static void age(DSLContext ctx, String tenant) {
        OffsetDateTime then = OffsetDateTime.now().minus(AGE);
        ctx.update(CHUNKS).set(CHUNKS.CREATED_AT, then).set(CHUNKS.LAST_WRITTEN_AT, then).where(CHUNKS.TENANT_ID.eq(tenant)).execute();
    }

    private static Record first(Result<? extends Record> rows) {
        assertThat(rows).as("the function returns one row").hasSize(1);
        return rows.get(0);
    }

    private static String tenant(String tag) {
        return "p35b-gcf-" + tag;
    }

    private static String collection(String tag) {
        return "docs__p35bgcf-" + tag + "__bge-base-en-v15-768__v1";
    }

    private static List<String> hashes(String tenant, String label, int n) {
        List<String> out = new ArrayList<>();
        for (int i = 1; i <= n; i++) {
            out.add(Chash.ofText(tenant + "/" + label + "-" + i).toHex());
        }
        return out;
    }

    private static List<byte[]> bytes(List<String> hexes) {
        return hexes.stream().map(h -> Chash.fromHex(h).toBytes()).toList();
    }

    private static byte[] bytes(String hex) {
        return Chash.fromHex(hex).toBytes();
    }

    private static List<String> concat(List<String> a, List<String> b) {
        List<String> out = new ArrayList<>(a);
        out.addAll(b);
        return out;
    }

    private void su(Consumer<DSLContext> work) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            work.accept(DSL.using(c, SQLDialect.POSTGRES));
        }
    }
}
