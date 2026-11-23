// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27; linda 2026-11-23, condition 1 and 2 on the restore by origin id): after the gate 3 latch a quarantined chunk is the origin's
 * by the origin's registry id (vectors-083), and a chunk whose {@code origin_collection_id} is the JSON null marker (the origin had no registry row when it was
 * quarantined) is not restorable by id. The restore routes do not answer a silent zero: they answer a typed 409 {@code restore_origin_unregistered} naming the
 * label and the number of rows, before anything moves, and the chunks stay quarantined. The refusal is for the whole call: a call that also names id-matched rows
 * restores none of them. Each of the two restore entry points ({@code /v1/vectors/gc/restore-rereferenced}, bounded and not, and
 * {@code /v1/vectors/gc/quarantine-restore}) is run after the latch on the real service; a control shows an id-matched restore is not refused.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P37RestoreOriginUnregisteredTest {

    private static final String MODEL = "minilm-l6-v2-384";
    private static final String REASON = "restore_origin_unregistered";
    private static final AtomicInteger SEQ = new AtomicInteger();

    private Rdr229RenameEngine engine;

    @BeforeAll
    void boot() throws Exception {
        engine = new Rdr229RenameEngine();
        Rdr229RenameRestartPrecondition.reach(engine).reached().require();
        try (Connection su = engine.pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.dropCatalogChunksFk(su);   // the manifest key onto the chunk name, as the name-key drop leaves it
        }
    }

    @AfterAll
    void stop() {
        if (engine != null) {
            engine.close();
        }
    }

    /**
     * A tenant with a live origin and its quarantine sibling holding one id-tagged chunk and one marker chunk, both owned by the origin's manifest. The origin
     * holds a stub chunk row for each (the shape the restore sweeps meet in production: a manifest row needs its chunk, so the row the owner wrote is
     * already there), counted in {@code originChunks}.
     */
    private record World(String tenant, String token, String origin, String quarantine, long originId, long quarantineId, String idTagged, String marker,
                         long originChunks) { }

    private World world(String tag, boolean withMarker) throws Exception {
        int n = SEQ.incrementAndGet();
        String tenant = "p37-ro-" + tag + n;
        String token = engine.newTenant(tenant);
        String origin = "knowledge__p37ro" + tag + n + "__" + MODEL + "__v1";
        String quarantine = "quarantine-" + origin;
        String idTagged = dev.nexus.service.db.Chash.ofText("p37 restore origin id " + tag + n).toHex();
        String marker = dev.nexus.service.db.Chash.ofText("p37 restore origin marker " + tag + n).toHex();
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, origin);
            PgContainerHelper.insertCollection(ctx, tenant, quarantine);
            return null;
        });
        long originId = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, origin));
        long quarantineId = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, quarantine));
        Map<String, Object> tagById = new LinkedHashMap<>();
        tagById.put("origin_collection", origin);
        tagById.put("quarantined_at", "2026-11-23T00:00:00Z");
        tagById.put("origin_collection_id", originId);
        Map<String, Object> tagMarker = new LinkedHashMap<>(tagById);
        tagMarker.put("origin_collection_id", null);   // JSON null: the origin had no registry row when the chunk was quarantined
        engine.su(ctx -> {
            PgContainerHelper.insertChunks(ctx, tenant, quarantine, List.of(idTagged), List.of("id tagged"), List.of(new float[384]), List.of(tagById));
            PgContainerHelper.insertChunks(ctx, tenant, origin, List.of(idTagged), List.of("stub"), List.of(new float[384]), List.of(Map.of()));
            PgContainerHelper.ownChunks(ctx, tenant, origin, idTagged);
            if (withMarker) {
                PgContainerHelper.insertChunks(ctx, tenant, quarantine, List.of(marker), List.of("marker"), List.of(new float[384]), List.of(tagMarker));
                PgContainerHelper.insertChunks(ctx, tenant, origin, List.of(marker), List.of("stub"), List.of(new float[384]), List.of(Map.of()));
                PgContainerHelper.ownChunks(ctx, tenant, origin, marker);
            }
            return null;
        });
        long originChunks = engine.su(ctx -> (long) ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION_ID.eq(originId))));
        return new World(tenant, token, origin, quarantine, originId, quarantineId, idTagged, marker, originChunks);
    }

    private long chunksIn(World w, long collectionId) {
        return engine.su(ctx -> (long) ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(w.tenant()).and(CHUNKS.COLLECTION_ID.eq(collectionId))));
    }

    private long manifestRows(World w) {
        return engine.su(ctx -> (long) ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(w.tenant())));
    }

    /** The 409's typed body, and that nothing moved: both chunks still in the quarantine sibling, none at the origin, the manifest as it was. */
    private void assertRefusedAndUntouched(World w, java.net.http.HttpResponse<String> r, long manifestBefore) throws Exception {
        assertThat(r.statusCode()).as(r.body()).isEqualTo(409);
        var body = Rdr229RenameEngine.MAPPER.readTree(r.body());
        assertThat(body.path("reason").asText()).as("a typed refusal").isEqualTo(REASON);
        assertThat(body.path("label").asText()).as("it names the label").isEqualTo(w.origin());
        assertThat(body.path("rows").asLong()).as("and how many rows carry the marker").isEqualTo(1L);
        assertThat(chunksIn(w, w.quarantineId())).as("both chunks stay quarantined, none was deleted").isEqualTo(2L);
        assertThat(chunksIn(w, w.originId())).as("the origin holds what it held, the id-matched chunk's restore included").isEqualTo(w.originChunks());
        assertThat(manifestRows(w)).as("the manifest is as it was").isEqualTo(manifestBefore);
    }

    @Test
    void gcRestoreRereferenced_afterTheLatch_refusesAMarkerRow_andMovesNothing() throws Exception {
        var w = world("gc", true);
        long manifest = manifestRows(w);

        var r = engine.call("POST", "/v1/vectors/gc/restore-rereferenced", w.token(),
            "{\"quarantine_collection\":\"" + w.quarantine() + "\",\"origin_collection\":\"" + w.origin() + "\"}");

        assertRefusedAndUntouched(w, r, manifest);
    }

    @Test
    void gcRestoreRereferencedBounded_afterTheLatch_refusesAMarkerRow_andMovesNothing() throws Exception {
        var w = world("gb", true);
        long manifest = manifestRows(w);

        var r = engine.call("POST", "/v1/vectors/gc/restore-rereferenced", w.token(),
            "{\"quarantine_collection\":\"" + w.quarantine() + "\",\"origin_collection\":\"" + w.origin() + "\",\"row_limit\":100}");

        assertRefusedAndUntouched(w, r, manifest);
    }

    @Test
    void quarantineRestore_afterTheLatch_refusesARequestedMarkerRow_andMovesNothing() throws Exception {
        var w = world("qr", true);
        long manifest = manifestRows(w);

        var r = engine.call("POST", "/v1/vectors/gc/quarantine-restore", w.token(),
            "{\"origin_collection\":\"" + w.origin() + "\",\"chashes\":[\"" + w.idTagged() + "\",\"" + w.marker() + "\"],\"reattach\":false}");

        assertRefusedAndUntouched(w, r, manifest);
    }

    /** A call that names only the id-matched chunk is not refused: the marker row it does not name is not its business. */
    @Test
    void quarantineRestore_namingOnlyTheIdMatchedRow_isNotRefused() throws Exception {
        var w = world("qo", true);

        var r = engine.call("POST", "/v1/vectors/gc/quarantine-restore", w.token(),
            "{\"origin_collection\":\"" + w.origin() + "\",\"chashes\":[\"" + w.idTagged() + "\"],\"reattach\":false}");

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        assertThat(Rdr229RenameEngine.MAPPER.readTree(r.body()).path("rows").get(0).path("outcome").asText())
            .as("the origin already holds the chunk's stub row: present, found by the origin's id").isEqualTo("present");
    }

    /** Control: with no marker row, the same gc restore by id runs, and takes the chunk out of quarantine. */
    @Test
    void gcRestoreRereferenced_withNoMarkerRow_restoresById() throws Exception {
        var w = world("gn", false);

        var r = engine.call("POST", "/v1/vectors/gc/restore-rereferenced", w.token(),
            "{\"quarantine_collection\":\"" + w.quarantine() + "\",\"origin_collection\":\"" + w.origin() + "\"}");

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        assertThat(Rdr229RenameEngine.MAPPER.readTree(r.body()).path("restored").asLong()).isEqualTo(1L);
        assertThat(chunksIn(w, w.quarantineId())).as("the chunk left quarantine").isZero();
    }
}
