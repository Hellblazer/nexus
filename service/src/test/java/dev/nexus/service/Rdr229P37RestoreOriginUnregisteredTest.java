// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.catchThrowable;

import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.db.RestoreOriginUnregisteredException;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Function;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27; linda 2026-11-23, conditions 1 and 2 on the restore by origin id), at the repository level on the test's own container:
 * after the gate 3 latch a quarantined chunk is the origin's by the origin's registry id (vectors-083), and a chunk whose {@code origin_collection_id} is the
 * JSON null marker (the origin had no registry row when it was quarantined) is not restorable by id. The restores do not answer a silent zero: they throw
 * {@link RestoreOriginUnregisteredException} naming the label and the number of rows, before anything moves, and the chunks stay quarantined. The refusal is for
 * the whole call: a call that also names id-matched rows restores none of them. The gc restores (unbounded, bounded) and the quarantine restore (by chash and by
 * quarantined_at window) are each run after the latch; one case before the latch shows the label restore unchanged. The window form also selects a renamed
 * collection's chunks by the origin's id (the Java twin of vectors-083's SQL rule). The 409 the exception becomes is {@code HttpUtilPartitionErrorsTest}'s.
 */
class Rdr229P37RestoreOriginUnregisteredTest extends AtomicWriteTestBase {

    private static final String MODEL = "minilm-l6-v2-384";
    private static final String TS = "2026-11-23T00:00:00Z";
    private static final org.jooq.Table<?> LATCH = DSL.table(DSL.name("nexus", "rdr229_gate_latch"));
    private static final org.jooq.Field<String> LATCH_GATE = DSL.field(DSL.name("nexus", "rdr229_gate_latch", "gate"), String.class);

    /** The repository of an install past gate 3: its reads go by id (the latch row the SQL reads is inserted by {@link #whileGate3Latched}). */
    private PgVectorRepository latched() {
        return new PgVectorRepository(tenantScope, embedder, embedder, () -> true);
    }

    private PgVectorRepository beforeTheLatch() {
        return new PgVectorRepository(tenantScope, embedder, embedder);
    }

    private <T> T su(Function<DSLContext, T> work) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    /** The latch is install-wide and the class shares one database, so the row is there for {@code work} only. */
    private void whileGate3Latched(Runnable work) throws Exception {
        su(ctx -> ctx.insertInto(LATCH).columns(LATCH_GATE, DSL.field(DSL.name("latched_at"), OffsetDateTime.class), DSL.field(DSL.name("engine_version"), String.class))
            .values("gate3", OffsetDateTime.now(), "p37-test").onConflictDoNothing().execute());
        try {
            work.run();
        } finally {
            su(ctx -> ctx.deleteFrom(LATCH).where(LATCH_GATE.eq("gate3")).execute());
        }
    }

    /**
     * A live origin and its quarantine sibling holding one id-tagged chunk and, when asked, one marker chunk, both owned by the origin's manifest. The origin
     * holds a stub chunk row for each (a manifest row needs its chunk; the shape the restore sweeps meet), counted in {@code originChunks}.
     */
    private record World(String tenant, String origin, String quarantine, long originId, long quarantineId, String idTagged, String marker, long originChunks) { }

    private World world(String tag, boolean withMarker) throws Exception {
        int n = seq.incrementAndGet();
        String tenant = "p37ro" + tag + n;
        String origin = "knowledge__p37ro" + tag + n + "__" + MODEL + "__v1";
        String quarantine = "quarantine-" + origin;
        String idTagged = Chash.ofText("p37 restore origin id " + tag + n).toHex();
        String marker = Chash.ofText("p37 restore origin marker " + tag + n).toHex();
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, origin);
            PgContainerHelper.insertCollection(ctx, tenant, quarantine);
            return null;
        });
        long originId = su(ctx -> CollectionIds.require(ctx, tenant, origin));
        long quarantineId = su(ctx -> CollectionIds.require(ctx, tenant, quarantine));
        su(ctx -> {
            PgContainerHelper.insertChunks(ctx, tenant, quarantine, List.of(idTagged), List.of("id tagged"), List.of(new float[384]), List.of(tag(origin, originId, false)));
            PgContainerHelper.insertChunks(ctx, tenant, origin, List.of(idTagged), List.of("stub"), List.of(new float[384]), List.of(Map.of()));
            PgContainerHelper.ownChunks(ctx, tenant, origin, idTagged);
            if (withMarker) {
                PgContainerHelper.insertChunks(ctx, tenant, quarantine, List.of(marker), List.of("marker"), List.of(new float[384]), List.of(tag(origin, originId, true)));
                PgContainerHelper.insertChunks(ctx, tenant, origin, List.of(marker), List.of("stub"), List.of(new float[384]), List.of(Map.of()));
                PgContainerHelper.ownChunks(ctx, tenant, origin, marker);
            }
            return null;
        });
        long originChunks = su(ctx -> (long) ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION_ID.eq(originId))));
        return new World(tenant, origin, quarantine, originId, quarantineId, idTagged, marker, originChunks);
    }

    /** The quarantine metadata of a chunk of {@code origin}: the label tag, and the id key as a number, or as the JSON null marker. */
    private static Map<String, Object> tag(String origin, long originId, boolean marker) {
        Map<String, Object> meta = new LinkedHashMap<>();
        meta.put("origin_collection", origin);
        meta.put("quarantined_at", TS);
        meta.put("origin_collection_id", marker ? null : originId);
        return meta;
    }

    /** What the quarantine sibling holds: chash to (collection id, metadata), its lifecycle_state, and the origin's chunk count. */
    private record Snapshot(Map<String, String> rows, String lifecycle, long originChunks) { }

    private Snapshot snapshot(World w) {
        try {
            return su(ctx -> {
                Map<String, String> rows = new java.util.TreeMap<>();
                for (var r : ctx.select(CHUNKS.CHASH, CHUNKS.COLLECTION_ID, CHUNKS.METADATA).from(CHUNKS)
                        .where(CHUNKS.TENANT_ID.eq(w.tenant()).and(CHUNKS.COLLECTION_ID.eq(w.quarantineId()))).fetch()) {
                    JSONB meta = r.value3();
                    rows.put(java.util.HexFormat.of().formatHex(r.value1()), r.value2() + " " + meta.data());
                }
                String lifecycle = ctx.select(CATALOG_COLLECTIONS.LIFECYCLE_STATE).from(CATALOG_COLLECTIONS)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(w.tenant()).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(w.quarantineId()))).fetchOne(0, String.class);
                long originChunks = ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(w.tenant()).and(CHUNKS.COLLECTION_ID.eq(w.originId())));
                return new Snapshot(rows, lifecycle, originChunks);
            });
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /** The refusal names the label and the one marker row, and nothing moved: same collection, lifecycle_state and metadata, same counts. */
    private void assertRefusedAndUntouched(World w, Snapshot before, Throwable thrown) {
        assertThat(thrown).isInstanceOfSatisfying(RestoreOriginUnregisteredException.class, e -> {
            assertThat(e.label()).as("it names the origin label").isEqualTo(w.origin());
            assertThat(e.rows()).as("and how many chunks carry the marker").isEqualTo(1L);
        });
        Snapshot after = snapshot(w);
        assertThat(before.rows()).as("the sibling holds both chunks, tagged as they were").hasSize(2);
        assertThat(after).as("nothing moved or was deleted or rewritten, the id-matched chunk included").isEqualTo(before);
        assertThat(after.lifecycle()).isEqualTo("quarantine");
        assertThat(after.originChunks()).isEqualTo(w.originChunks());
    }

    @Test
    void gcRestoreRereferenced_afterTheLatch_refusesAMarkerRow_andMovesNothing() throws Exception {
        var w = world("gc", true);
        var before = snapshot(w);
        whileGate3Latched(() ->
            assertRefusedAndUntouched(w, before, catchThrowable(() -> latched().restoreRereferenced(w.tenant(), w.quarantine(), w.origin()))));
    }

    @Test
    void gcRestoreRereferencedBounded_afterTheLatch_refusesAMarkerRow_andMovesNothing() throws Exception {
        var w = world("gb", true);
        var before = snapshot(w);
        whileGate3Latched(() ->
            assertRefusedAndUntouched(w, before, catchThrowable(() -> latched().restoreRereferencedBounded(w.tenant(), w.quarantine(), w.origin(), 100))));
    }

    @Test
    void quarantineRestore_afterTheLatch_refusesARequestedMarkerRow_andMovesNothing() throws Exception {
        var w = world("qr", true);
        var before = snapshot(w);
        whileGate3Latched(() -> assertRefusedAndUntouched(w, before, catchThrowable(() ->
            latched().quarantineRestore(w.tenant(), w.origin(), null, List.of(w.idTagged(), w.marker()), "p37", false, false))));
    }

    /** The window form selects the marker row by its label (nothing else names the origin of a row with no id), and the same refusal follows. */
    @Test
    void quarantineRestoreSelected_afterTheLatch_refusesASelectedMarkerRow_andMovesNothing() throws Exception {
        var w = world("qs", true);
        var before = snapshot(w);
        whileGate3Latched(() -> assertRefusedAndUntouched(w, before, catchThrowable(() -> latched().quarantineRestoreSelected(w.tenant(), w.origin(), null,
            Instant.parse("2026-11-22T00:00:00Z"), Instant.parse("2026-11-24T00:00:00Z"), null, 100, "p37", false, false))));
    }

    /** A call that names only the id-matched chunk is not refused: the marker row it does not name is not its business. */
    @Test
    void quarantineRestore_namingOnlyTheIdMatchedRow_isNotRefused() throws Exception {
        var w = world("qo", true);
        whileGate3Latched(() -> {
            var out = latched().quarantineRestore(w.tenant(), w.origin(), null, List.of(w.idTagged()), "p37", false, false);
            assertThat(out.rows()).hasSize(1);
            assertThat(out.rows().get(0).outcome()).as("the origin already holds the chunk's stub row: present, found by the origin's id").isEqualTo("present");
        });
    }

    /** Control: with no marker row, the same gc restore by id runs, and takes the chunk out of quarantine. */
    @Test
    void gcRestoreRereferenced_afterTheLatch_withNoMarkerRow_restoresById() throws Exception {
        var w = world("gn", false);
        whileGate3Latched(() -> {
            assertThat(latched().restoreRereferenced(w.tenant(), w.quarantine(), w.origin())).isEqualTo(1L);
            assertThat(snapshot(w).rows()).as("the chunk left quarantine").isEmpty();
        });
    }

    /** Before the latch the label match is the rule and the marker is not read: the label restore is unchanged, marker row included. */
    @Test
    void gcRestoreRereferenced_beforeTheLatch_restoresByLabel_aMarkerRowIncluded() throws Exception {
        var w = world("gl", true);

        long restored = beforeTheLatch().restoreRereferenced(w.tenant(), w.quarantine(), w.origin());

        assertThat(restored).as("both chunks carry the label tag: restored by it").isEqualTo(2L);
        assertThat(snapshot(w).rows()).as("both left quarantine").isEmpty();
    }

    /**
     * The window form after a one-row rename: the chunks are tagged with the label the origin had when they were quarantined, so the selection goes by the id
     * from the latch. Named by the NEW label it selects and restores them; selected by the label text it found none.
     */
    @Test
    void quarantineRestoreSelected_afterARename_namedByTheNewLabel_restoresTheRenamedCollectionsChunks() throws Exception {
        int n = seq.incrementAndGet();
        String tenant = "p37rosel" + n;
        String origin = "knowledge__p37rosel" + n + "__" + MODEL + "__v1";
        String renamed = "knowledge__p37roselr" + n + "__" + MODEL + "__v1";
        String quarantine = "quarantine-" + origin;
        String hex = Chash.ofText("p37 restore selected after rename " + n).toHex();
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, origin);
            PgContainerHelper.insertCollection(ctx, tenant, quarantine);
            return null;
        });
        long originId = su(ctx -> CollectionIds.require(ctx, tenant, origin));
        su(ctx -> {
            PgContainerHelper.insertChunks(ctx, tenant, quarantine, List.of(hex), List.of("renamed origin"), List.of(new float[384]), List.of(tag(origin, originId, false)));
            assertThat(ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, renamed)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(origin)).execute()).as("the registry row is renamed in place").isEqualTo(1);
            return null;
        });
        whileGate3Latched(() -> {
            var out = latched().quarantineRestoreSelected(tenant, renamed, null,
                Instant.parse("2026-11-22T00:00:00Z"), Instant.parse("2026-11-24T00:00:00Z"), null, 100, "p37", false, false);

            assertThat(out.rows()).as("the renamed collection's quarantined chunk is selected by the origin's id").hasSize(1);
            assertThat(out.rows().get(0).outcome()).isEqualTo("restored");
            long atOrigin;
            try {
                atOrigin = su(ctx -> (long) ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION_ID.eq(originId))
                    .and(CHUNKS.CHASH.eq(Chash.fromHex(hex).toBytes()))));
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
            assertThat(atOrigin).as("it is at the origin, under the origin's id").isEqualTo(1L);
        });
    }
}
