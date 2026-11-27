// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.vectors.ReaperRepository;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.Duration;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27): the engine's two quarantine expiries after a one-row rename, for the rows the rename's retag did not reach.
 *
 * <p>A one-row rename A to B keeps the origin's {@code collection_id} and retags, in its own transaction, every quarantine row whose
 * {@code origin_collection_id} is that id ({@code CollectionOneRowRename} calls {@code QuarantineOrigin.retagRowsOf}), so the reaper then lists
 * those rows under B and {@code nexus.reaper_expire_quarantine} guards them by B's id: the control case below. A row the retag does not see
 * still carries the tag A: (1) a move into the sibling that commits after the rename's retag (the movers read the origin by name and take no
 * lock against the rename's {@code FOR UPDATE}), with the origin id key; (2) such a row without the key. The reaper lists it under A
 * ({@code ReaperRepository#taggedOrigins} reads the label tag), and the expiry resolves {@code v_origin_id} from the label A: NULL after the
 * rename, or the id of a new holder of A. The manifest guard, by that id (or by name only where the manifest row has no id), then misses the
 * manifest row that re-references the chunk under B, and the referenced row is deleted. {@code nexus.reaper_expire_client_quarantine} does the
 * same when A has been registered again as a live knowledge collection (otherwise {@code clientMovedOrigins} does not list A).
 *
 * <p>Each case writes the re-reference (the origin chunk and its manifest row, under B and the origin's id) after the rename, as a re-ingest
 * would; the quarantine rows are past retention. The assertion is that no referenced row is deleted. One booted engine for the class, through
 * {@link Rdr229RenameRestartPrecondition} (gate 3 latched, P3.5b applied), so the rename runs the one-row body.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229ReaperExpiryAfterRenameTest {

    private static final AtomicInteger SEQ = new AtomicInteger();
    private static final String MODEL = "minilm-l6-v2-384";
    private static final int ROW_LIMIT = 5_000;
    private static final int STATEMENT_MS = 25_000;
    private static final int LOCK_MS = 2_000;

    private Rdr229RenameEngine engine;
    private ReaperRepository reaper;

    @BeforeAll
    void boot() throws Exception {
        engine = new Rdr229RenameEngine();
        Rdr229RenameRestartPrecondition.reach(engine).reached().require();
        assertThat(engine.gate3Holds()).as("the engine's gate 3 signal is latched").isTrue();
        assertThat(engine.<Integer>su(ctx -> ctx.fetchCount(DSL.table(DSL.name("nexus", "rdr229_gate_latch")))))
            .as("the gate 3 latch row exists").isPositive();
        reaper = new ReaperRepository(engine.scope(), () -> true);
    }

    @AfterAll
    void stop() {
        if (engine != null) {
            engine.close();
        }
    }

    private static String coll(String owner, int version) {
        return "knowledge__" + owner + "__" + MODEL + "__v" + version;
    }

    private static String stamp(Duration age) {
        return Instant.now().minus(age).truncatedTo(ChronoUnit.SECONDS).toString();
    }

    /** The reaper's cutoff: 14 days, its default retention for both populations. */
    private static String cutoff() {
        return stamp(Duration.ofDays(14));
    }

    /** A tenant whose origin A was renamed to B by the one-row rename; the sibling is registered beside it. */
    private record World(String tenant, String a, String b, String sibling, long originId, String referenced, String unreferenced) { }

    private World renamed(String tag, boolean reRegisterA, boolean rowsBeforeRename, boolean engineMoved) throws Exception {
        int n = SEQ.incrementAndGet();
        String tenant = "p37rx-" + tag + n;
        String token = engine.newTenant(tenant);
        String ownerA = "p37rx" + tag + n + "a";
        String a = coll(ownerA, 1);
        String b = coll(ownerA + "x", 2);
        String sibling = "quarantine-" + a;
        String referenced = Chash.ofText(tenant + "/referenced").toHex();
        String unreferenced = Chash.ofText(tenant + "/unreferenced").toHex();
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, a);
            PgContainerHelper.insertCollection(ctx, tenant, sibling);
            return null;
        });
        long id = engine.su(ctx -> CollectionIds.require(ctx, tenant, a));
        if (rowsBeforeRename) {
            quarantineRows(tenant, sibling, a, id, true, engineMoved, referenced, unreferenced);
        }

        var r = engine.rename(token, a, b, Map.of("content_type", "knowledge", "owner_id", ownerA + "x"));
        assertThat(r.status()).as("the one-row rename A to B: " + r).isEqualTo(200);
        assertThat(engine.<Long>su(ctx -> CollectionIds.require(ctx, tenant, b))).as("B keeps A's id").isEqualTo(id);

        // the re-reference, written after the rename as a re-ingest under B writes it: the origin's chunk and a live manifest row naming it
        engine.su(ctx -> {
            PgContainerHelper.insertChunks(ctx, tenant, b, List.of(referenced), List.of("referenced"), List.of(new float[384]), List.of(Map.of()));
            String doc = tenant + ".doc.1";
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE,
                    CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID)
                .values(tenant, doc, "re-referenced", b, id).execute();
            ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                    CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION,
                    CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID)
                .values(tenant, doc, 0, Chash.fromHex(referenced).toBytes(), b, MODEL, id).execute();
            return null;
        });
        if (reRegisterA) {
            engine.su(ctx -> {
                PgContainerHelper.insertCollection(ctx, tenant, a);
                return null;
            });
            assertThat(engine.<Long>su(ctx -> CollectionIds.require(ctx, tenant, a))).as("A is registered again, under a new id").isNotEqualTo(id);
        }
        return new World(tenant, a, b, sibling, id, referenced, unreferenced);
    }

    /** One referenced and one unreferenced quarantine row of {@code sibling}, tagged for {@code origin}, 400 days old. */
    private void quarantineRows(String tenant, String sibling, String origin, long originId, boolean idKey, boolean engineMoved, String... hexes) {
        String old = stamp(Duration.ofDays(400));
        Map<String, Object> meta = new LinkedHashMap<>();
        meta.put("quarantined_at", old);
        meta.put("origin_collection", origin);
        if (idKey) {
            meta.put("origin_collection_id", originId);
        }
        if (engineMoved) {
            meta.put("quarantined_by", "engine-reaper");
            meta.put("reaper_quarantined_at", old);
        }
        engine.su(ctx -> {
            for (String hex : hexes) {
                PgContainerHelper.insertChunks(ctx, tenant, sibling, List.of(hex), List.of(hex), List.of(new float[384]), List.of(meta));
            }
            return null;
        });
    }

    /** The race: a move that commits after the rename's retag, so its rows still carry the label A. */
    private void movedAfterTheRetag(World w, boolean idKey, boolean engineMoved) {
        quarantineRows(w.tenant(), w.sibling(), w.a(), w.originId(), idKey, engineMoved, w.referenced(), w.unreferenced());
    }

    private boolean held(World w, String hex) {
        return engine.su(ctx -> ctx.fetchExists(CHUNKS, CHUNKS.TENANT_ID.eq(w.tenant()).and(CHUNKS.CHASH.eq(Chash.fromHex(hex).toBytes()))
            .and(CHUNKS.COLLECTION_ID.eq(CollectionIds.require(ctx, w.tenant(), w.sibling())))));
    }

    /** One engine-moved expiry pass over the sibling, as ChunkReaper.reaperMoved makes it: every listed origin, the 14-day cutoff. */
    private List<String> reapEngineMoved(World w) {
        List<String> origins = reaper.taggedOrigins(w.tenant(), w.sibling(), STATEMENT_MS);
        for (String origin : origins) {
            reaper.expire(w.tenant(), w.sibling(), origin, cutoff(), ROW_LIMIT, STATEMENT_MS, LOCK_MS);
        }
        return origins;
    }

    /** One client-moved expiry pass over the sibling, as ChunkReaper.clientMoved makes it (knowledge origins only). */
    private List<String> reapClientMoved(World w) {
        List<String> origins = reaper.clientMovedOrigins(w.tenant(), w.sibling(), "knowledge", STATEMENT_MS);
        for (String origin : origins) {
            reaper.expireClientMoved(w.tenant(), w.sibling(), origin, cutoff(), ROW_LIMIT, STATEMENT_MS, LOCK_MS);
        }
        return origins;
    }

    // ---- control: the rows the retag reached --------------------------------------------------------------------------------------

    /** Rows moved before the rename are retagged B by it; the reaper lists B, guards by B's id, and keeps the referenced row. */
    @Test
    void engineMoved_rowsTheRetagReached_referencedRowIsKept() throws Exception {
        World w = renamed("c", false, true, true);
        assertThat(reapEngineMoved(w)).as("the retag moved the tag to B").containsExactly(w.b());
        assertThat(held(w, w.unreferenced())).as("non-vacuity: the unreferenced row past retention is expired").isFalse();
        assertThat(held(w, w.referenced())).as("the referenced row is kept").isTrue();
    }

    // ---- (1) a row tagged A with the origin id, moved after the retag ---------------------------------------------------------------

    @Test
    void engineMoved_rowTaggedAWithTheOriginId_movedAfterTheRetag_referencedRowIsKept() throws Exception {
        World w = renamed("e1", false, false, true);
        movedAfterTheRetag(w, true, true);
        assertThat(reapEngineMoved(w)).as("the reaper lists the label tag A").containsExactly(w.a());
        assertThat(held(w, w.unreferenced())).as("non-vacuity: the unreferenced row past retention is expired").isFalse();
        assertThat(held(w, w.referenced())).as("the row a manifest row of the origin (by id, now B) names is never deleted").isTrue();
    }

    @Test
    void clientMoved_rowTaggedAWithTheOriginId_movedAfterTheRetag_AReRegistered_referencedRowIsKept() throws Exception {
        World w = renamed("k1", true, false, false);
        movedAfterTheRetag(w, true, false);
        assertThat(reapClientMoved(w)).as("A is live knowledge again, so the client lister names it").containsExactly(w.a());
        assertThat(held(w, w.unreferenced())).as("non-vacuity: the unreferenced row past retention is expired").isFalse();
        assertThat(held(w, w.referenced())).as("the row a manifest row of the origin (by id, now B) names is never deleted").isTrue();
    }

    // ---- (2) a row tagged A with no origin id key ------------------------------------------------------------------------------------

    @Test
    void engineMoved_rowTaggedAWithNoIdKey_referencedRowIsKept() throws Exception {
        World w = renamed("e2", false, false, true);
        movedAfterTheRetag(w, false, true);
        assertThat(reapEngineMoved(w)).as("the reaper lists the label tag A").containsExactly(w.a());
        assertThat(held(w, w.referenced())).as("a row with no id key is outside the call from the latch (it leaks, vectors-086); it is kept by the latch term, not by B's re-reference").isTrue();
    }

    @Test
    void clientMoved_rowTaggedAWithNoIdKey_AReRegistered_referencedRowIsKept() throws Exception {
        World w = renamed("k2", true, false, false);
        movedAfterTheRetag(w, false, false);
        assertThat(reapClientMoved(w)).as("A is live knowledge again, so the client lister names it").containsExactly(w.a());
        assertThat(held(w, w.referenced())).as("a row with no id key is outside the call from the latch (it leaks, vectors-086); it is kept by the latch term, not by B's re-reference").isTrue();
    }
}
