// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.IdKeyIndexProbe;
import dev.nexus.service.PgCatalogProbes;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.VectorSeeds;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.DrivingTableIsolationCheck;
import dev.nexus.service.vectors.LeafIndexPass;
import dev.nexus.service.vectors.Rdr229SwapMove;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.time.Clock;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;

/**
 * RDR-229 P3.3c (nexus-p2m1x.21), the failing tests of the swap moves: per table ({@code nexus.chunks},
 * {@code nexus.taxonomy_centroids}) the move that {@link LeafIndexPass} registers once every leaf has its id unique key, and
 * whose {@code on_complete} (a) refuses when the id unique key is absent, (b) adds the three child id keys {@code NOT VALID}
 * {@code ON UPDATE CASCADE} (chunks only), (c) registers the three validation moves (chunks only), (d) validates the RDR-225
 * composite from {@code nexus.chunks} to {@code catalog_collections} on the id directly (PostgreSQL takes no NOT VALID on a
 * partitioned referencing table). It drops nothing: the name primary keys, the three name child keys and
 * {@code chunks_collection_fk} stay (until P4.1 and P3.7).
 *
 * <p><b>Harness.</b> The real walk and the real worker with the production finisher and failure handler, in the pattern of
 * {@link CollectionIdStampMovesIntegrationTest}; the base empties the registry before each scenario. This class lives in the
 * {@code datamove} package, not beside {@code LeafIndexPassIntegrationTest}, because the worker's finisher and its seams are
 * package-private. The swap rows come from {@link LeafIndexPass#runOnce} (P3.3b). On a branch where that pass is not built yet,
 * {@link #registerSwaps} puts the id unique keys and the rows in itself (a row is {@link Rdr229SwapMove#spec} and the pass's
 * identity fields), so the scenarios run the swap P3.3c owns and not P3.3b's absence; with the pass built, the pass's rows
 * are the ones that run and the fallback never fires.
 *
 * <p><b>Names.</b> RDR-229 item 6 names the three child tables and the keys they reference but not the id keys' constraint
 * names, so every id key is found by what it keys and references, never by name. The three child validation moves are found by
 * their driving table. The registry is empty at the start of each scenario (the base's reset), and P3.3a is not on this branch,
 * so any row beyond the two swaps is a move the swap's {@code on_complete} registered.
 */
class Rdr229SwapMovesIntegrationTest extends Rdr229SwapMovesFixtures {

    // ── 1 ───────────────────────────────────────────────────────────────────────────────────────────────────

    /**
     * No child-validation move exists before its constraint does. Before the swap runs there is no id twin on any child and the
     * registry holds the two swaps and nothing else; once the chunks swap has completed, every validation move that exists names an id
     * twin that exists, in the registry in the same commit, and still not validated (the move has not run).
     */
    @Test
    void noChildValidationMoveExistsBeforeItsConstraintDoes() {
        seed("order");
        registerSwaps();

        assertThat(moves(LABEL_F.like("rdr229-swap-%")).stream().map(m -> (String) m.get("label")).toList())
            .as("precondition: the registry holds the two swaps").containsExactlyInAnyOrder(CHUNKS_SWAP, CENTROIDS_SWAP);
        assertThat(registeredBySwap()).as("before the swap: no validation move").isEmpty();
        for (String child : CHILD_ID_COLUMN.keySet()) {
            assertThat(idTwins(child)).as("before the swap: no id twin on " + child).isEmpty();
        }

        var w = productionWorker(new VerifyingFinisher());
        w.runCycle();

        assertThat(completedAt(CHUNKS_SWAP)).as("the chunks swap completed").isNotNull();
        var validations = registeredBySwap();
        assertThat(validations.stream().map(m -> (String) m.get("driving_table")).toList())
            .as("one validation move per child, registered by the chunks swap (the centroids swap registers none)")
            .containsExactlyInAnyOrder("catalog_document_chunks", "chunk_orphaned_at", "topic_assignments");
        for (var move : validations) {
            String child = (String) move.get("driving_table");
            var twins = idTwins(child);
            assertThat(twins).as("the constraint of the " + child + " validation move exists").hasSize(1);
            assertThat(twins.get(0).validated()).as("and is not validated: the move has not validated it yet").isFalse();
            assertThat((String) move.get("on_complete")).as("the move's on_complete validates that constraint")
                .contains("VALIDATE CONSTRAINT").contains(twins.get(0).name());
        }
    }

    // ── 2 ───────────────────────────────────────────────────────────────────────────────────────────────────

    /**
     * The swap refuses when the id unique key is absent: it does not complete, a refusal is recorded on the move (a blocked or failed
     * row, as the failure handler writes either), and nothing it adds exists: no id twin, no composite on the id, no validation
     * move. (A verification that returns the leaves with no valid id key would refuse first; the refusal is the move's either way.)
     */
    @Test
    void theSwapRefusesWhenTheIdUniqueKeyIsAbsent() {
        seed("nokey");
        registerSwaps();
        IdKeyIndexProbe.reset(ctx());
        assertThat(IdKeyIndexProbe.indexes(ctx(), IdKeyIndexProbe.CHUNKS)).as("precondition: the id key is gone").isEmpty();

        runSwaps(new VerifyingFinisher());

        for (String label : List.of(CHUNKS_SWAP, CENTROIDS_SWAP)) {
            var row = moves(LABEL_F.eq(label)).get(0);
            assertThat(row.get("completed_at")).as(label + " is not completed over a missing key").isNull();
            assertThat(row.get("failed_at") != null || row.get("last_transient") != null)
                .as(label + " records the refusal (failed or blocked)").isTrue();
        }
        for (String child : CHILD_ID_COLUMN.keySet()) {
            assertThat(idTwins(child)).as("no id twin on " + child).isEmpty();
        }
        assertThat(compositeOnId()).as("no composite on the id").isEmpty();
        assertThat(registeredBySwap()).as("no validation move").isEmpty();
        assertNameKeysStay();
    }

    // ── 3 ───────────────────────────────────────────────────────────────────────────────────────────────────

    /**
     * After the swap: the id keys exist (every leaf and level valid, each backing a UNIQUE constraint), the three child id keys exist
     * NOT VALID with ON UPDATE CASCADE (and keep their name key's ON DELETE action), the three validation moves are registered
     * exactly once (a second pass adds none; no worker cycle runs them, so they stay NOT VALID), the composite on the id is VALID, and the name keys all stay.
     */
    @Test
    void afterTheSwapTheChildIdKeysAreNotValidAndCascadeTheValidationMovesAreRegisteredOnceAndTheNameKeysStay() {
        seed("after");
        registerSwaps();

        runSwaps(new VerifyingFinisher());
        new LeafIndexPass(adminConnection, new BootWorkRegistry(Clock.systemUTC()), () -> true, ENGINE_VERSION).runOnce();

        assertThat(completedAt(CHUNKS_SWAP)).isNotNull();
        assertThat(completedAt(CENTROIDS_SWAP)).isNotNull();
        for (var key : IdKeyIndexProbe.KEYS) {
            assertThat(IdKeyIndexProbe.leavesWithoutValidIndex(ctx(), key)).as(key.table() + ": every leaf has a valid id key").isEmpty();
            assertThat(IdKeyIndexProbe.indexes(ctx(), key)).as(key.table() + ": every id index backs a constraint")
                .allMatch(i -> i.constraint() != null);
        }
        Map<String, String> nameDeleteAction = Map.of(
            "catalog_document_chunks", PgCatalogProbes.foreignKey(ctx(), "nexus", "fk_catalog_chunks_chunk").confdeltype(),
            "chunk_orphaned_at", PgCatalogProbes.foreignKey(ctx(), "nexus", "chunk_orphaned_at_chunk_fk").confdeltype(),
            "topic_assignments", PgCatalogProbes.foreignKey(ctx(), "nexus", "topic_assignments_chunk_fk").confdeltype());
        for (String child : CHILD_ID_COLUMN.keySet()) {
            var twins = idTwins(child);
            assertThat(twins).as("exactly one id twin on " + child).hasSize(1);
            Fk twin = twins.get(0);
            assertThat(twin.validated()).as(child + ": NOT VALID until its validation move runs").isFalse();
            assertThat(twin.onUpdate()).as(child + ": ON UPDATE CASCADE").isEqualTo("c");
            assertThat(twin.onDelete()).as(child + ": the name key's ON DELETE action").isEqualTo(nameDeleteAction.get(child));
        }
        var validations = registeredBySwap();
        assertThat(validations.stream().map(m -> (String) m.get("driving_table")).toList())
            .as("the three validation moves, once each, after a second pass")
            .containsExactlyInAnyOrder("catalog_document_chunks", "chunk_orphaned_at", "topic_assignments");

        assertThat(compositeOnId()).as("the swap no longer adds the composite (nexus-p2m1x.21): its adoption move does, after every leaf validates")
            .isEmpty();
        assertThat(adoptMoves()).as("the adoption move is registered once, after a second pass").hasSize(1);
        assertThat(leafMoves()).as("one validation move per leaf of chunks").hasSize(chunkLeaves().size());

        runUntilComposite(new VerifyingFinisher());
        var composite = compositeOnId();
        assertThat(composite).as("the composite from chunks to catalog_collections on the id, added by the adoption move").hasSize(1);
        assertThat(composite.get(0).validated()).as("adopted from the validated leaf keys: validated").isTrue();
        assertNameKeysStay();
    }

    // ── 4 ───────────────────────────────────────────────────────────────────────────────────────────────────

    /** After the swap a chunk upsert and a centroid upsert on the name conflict target still succeed (the name primary keys are the arbiters). */
    @Test
    void afterTheSwapAChunkUpsertAndACentroidUpsertOnTheNameTargetStillSucceed() {
        var t = seed("upsert");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        assertThat(completedAt(CHUNKS_SWAP)).as("precondition: the swap completed").isNotNull();
        String tenant = t.centroids();
        String collection = StampMoveSeeds.collection(tenant, 0);
        long id = StampMoveSeeds.id(ctx(), tenant, collection);
        byte[] chash = new byte[32];
        chash[0] = 33;

        for (String text : List.of("first", "second")) {
            ctx().insertInto(CHUNKS)
                .set(CHUNKS.TENANT_ID, tenant).set(CHUNKS.COLLECTION, collection).set(CHUNKS.COLLECTION_ID, id)
                .set(CHUNKS.CHASH, chash).set(CHUNKS.EMBEDDING_MODEL, BGE).set(CHUNKS.CHUNK_TEXT, text)
                .set(CHUNKS.EMBEDDING_768, VectorSeeds.vec(768, 0.3f)).set(CHUNKS.METADATA, JSONB.jsonb("{}"))
                .onConflict(CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL)
                .doUpdate().set(CHUNKS.CHUNK_TEXT, text)
                .execute();
            ctx().insertInto(TAXONOMY_CENTROIDS)
                .set(TAXONOMY_CENTROIDS.TENANT_ID, tenant).set(TAXONOMY_CENTROIDS.COLLECTION, collection)
                .set(TAXONOMY_CENTROIDS.COLLECTION_ID, id).set(TAXONOMY_CENTROIDS.TOPIC_ID, 7777L)
                .set(TAXONOMY_CENTROIDS.LABEL, text).set(TAXONOMY_CENTROIDS.EMBEDDING_MODEL, BGE)
                .set(TAXONOMY_CENTROIDS.EMBEDDING_768, VectorSeeds.vec(768, 0.4f))
                .onConflict(TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION, TAXONOMY_CENTROIDS.TOPIC_ID,
                    TAXONOMY_CENTROIDS.EMBEDDING_MODEL)
                .doUpdate().set(TAXONOMY_CENTROIDS.LABEL, text)
                .execute();
        }

        assertThat(ctx().select(CHUNKS.CHUNK_TEXT).from(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.CHASH.eq(chash))
            .fetch(CHUNKS.CHUNK_TEXT)).as("one chunk, upserted in place").containsExactly("second");
        assertThat(ctx().select(TAXONOMY_CENTROIDS.LABEL).from(TAXONOMY_CENTROIDS).where(TAXONOMY_CENTROIDS.TENANT_ID.eq(tenant))
            .and(TAXONOMY_CENTROIDS.TOPIC_ID.eq(7777L)).fetch(TAXONOMY_CENTROIDS.LABEL))
            .as("one centroid, upserted in place").containsExactly("second");
    }

    // ── 5 ───────────────────────────────────────────────────────────────────────────────────────────────────

    /**
     * A name-path rename leaves every child row with the new id. The rename is the engine's: a new registry row (so a new id), then
     * the chunk rows rewritten to the new name and id. The name keys cascade the name; from the swap the child id keys' ON UPDATE
     * CASCADE carry the id to the manifest, the orphan clock and the assignments. Before the swap the child id stays the old one.
     */
    @Test
    void aNameRenameLeavesEveryChildRowWithTheNewId() {
        var t = seed("rename");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        assertThat(completedAt(CHUNKS_SWAP)).as("precondition: the swap completed").isNotNull();

        for (String child : CHILD_ID_COLUMN.keySet()) {
            String tenant = t.of(child);
            String oldName = StampMoveSeeds.collection(tenant, 0);
            String newName = oldName.replace("__v1", "r__v1");
            PgContainerHelper.insertCollection(ctx(), tenant, newName, BGE);
            long oldId = StampMoveSeeds.id(ctx(), tenant, oldName);
            long newId = StampMoveSeeds.id(ctx(), tenant, newName);
            assertThat(newId).as("a rename inserts a new registry row, so a new id").isNotEqualTo(oldId);
            var childTable = table("nexus", child);
            assertThat(ctx().fetch(childTable, TENANT_F.eq(tenant))).as("non-vacuity: " + child + " holds a row").isNotEmpty();

            ctx().update(CHUNKS).set(CHUNKS.COLLECTION, newName).set(CHUNKS.COLLECTION_ID, newId)
                .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(oldName)).execute();

            Field<Long> idColumn = DSL.field(DSL.name(CHILD_ID_COLUMN.get(child)), Long.class);
            assertThat(ctx().select(idColumn).from(childTable).where(TENANT_F.eq(tenant)).fetch(idColumn))
                .as("every " + child + " row carries the new id").isNotEmpty().containsOnly(newId);
        }
    }

    // ── 6: the split bounds (Sam, 2026-11-21, amending decision C) ──────────────────────────────────────────

    /**
     * The swap rides the RDR-246 finisher's two bounds. The READ ONLY verification phase is the warm-up under
     * {@code verify_statement_timeout_ms} (about 60 s), the completion keeps {@code statement_timeout_ms} 1400 ms and a
     * {@code lock_timeout} of 700 ms, which {@code on_complete} inherits.
     *
     * <p>What the split exposes to observe: the registered row's two bounds, the verification text (which the registrant writes to read
     * what {@code on_complete} reads: the table and its leaf heaps, the id-key indexes, {@code catalog_collections}; there is no
     * pg_prewarm), and the session settings at the start of the completion transaction ({@code VerifyingFinisher#recheckHook}, which runs
     * after the settings are set and before the re-check and {@code on_complete}). Nothing exposes the relations actually read by the
     * first pass, so the warm set is asserted on the verification text, and the bounds {@code on_complete} sees are asserted at that
     * seam (they are set again, unchanged, after the re-check).
     */
    @Test
    void theSwapVerificationWarmsTheOnCompleteSetUnderTheLongBoundAndOnCompleteRunsUnder1400And700() {
        seed("bounds");
        registerSwaps();
        var settings = new ArrayList<List<String>>();
        var finisher = new VerifyingFinisher();
        finisher.recheckHook = tx -> settings.add(List.of(
            tx.select(DSL.function(DSL.name("current_setting"), String.class, DSL.inline("statement_timeout"))).fetchOne(0, String.class),
            tx.select(DSL.function(DSL.name("current_setting"), String.class, DSL.inline("lock_timeout"))).fetchOne(0, String.class)));

        for (String label : List.of(CHUNKS_SWAP, CENTROIDS_SWAP)) {
            var row = moves(LABEL_F.eq(label)).get(0);
            assertThat((Integer) row.get("statement_timeout_ms")).as(label + ": the completion's statement bound").isEqualTo(1400);
            assertThat((Integer) row.get("verify_statement_timeout_ms")).as(label + ": the warm-up's own, longer bound (about 60 s)")
                .isBetween(30_000, 120_000);
        }
        String chunksVerification = ((String) moves(LABEL_F.eq(CHUNKS_SWAP)).get(0).get("verification")).toLowerCase(java.util.Locale.ROOT);
        assertThat(chunksVerification).as("the chunks swap's verification reads the chunks leaves, the id-key indexes and the registry")
            .contains("chunks").contains("catalog_collections").containsAnyOf("pg_index", "indisvalid");

        runSwaps(finisher);

        assertThat(settings).as("the completion transaction of both swaps ran, after their verification phase").hasSizeGreaterThanOrEqualTo(2);
        // the moves the swap registered run in the same cycles: a validation that blocks no writer has no statement bound and a 5 s lock
        // wait ("0", "5s"); every other completion, the swaps and the adoption move included, runs under 1400 ms and 700 ms
        assertThat(settings).as("only the two bound pairs occur: 1400/700 ms (a step that blocks writers) and 0/5 s (a validation)")
            .allSatisfy(s -> assertThat(s).isIn(List.of("1400ms", "700ms"), List.of("0", "5s")));
        assertThat(settings.stream().filter(s -> s.equals(List.of("1400ms", "700ms"))).count())
            .as("both swaps completed under statement_timeout 1400 ms and lock_timeout 700 ms").isGreaterThanOrEqualTo(2);
    }

    // ── 7: a lock the swap cannot get ──────────────────────────────────────────────────────────────────────

    /**
     * RDR-246's fail-fast lock behaviour: a lock the on_complete cannot get within its 700 ms fails 55P03, which is a transient blocked
     * state, so the move is left retryable and not failed, and the whole on_complete is rolled back (nothing it adds exists, no
     * validation move). The lock is an open transaction that has written a row of {@code catalog_document_chunks}, which the id key
     * to chunks must lock SHARE ROW EXCLUSIVE to add. The retry after the release is the failure handler's backoff, not driven here.
     */
    @Test
    void aLockTheSwapCannotGetIsATransientBlockedStateAndTheMoveStaysRetryable() throws Exception {
        var t = seed("lock");
        registerSwaps();
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            Field<Integer> position = DSL.field(DSL.name("position"), Integer.class);
            DSL.using(holder, SQLDialect.POSTGRES).update(table("nexus", "catalog_document_chunks"))
                .set(position, position).where(TENANT_F.eq(t.catalogDocumentChunks())).execute();

            runSwaps(new VerifyingFinisher());

            holder.rollback();
        }

        var chunks = moves(LABEL_F.eq(CHUNKS_SWAP)).get(0);
        assertThat(chunks.get("completed_at")).as("not completed while the lock is held").isNull();
        assertThat(chunks.get("failed_at")).as("blocked, never failed, on 55P03").isNull();
        assertThat(chunks.get("last_transient")).as("the transient blocked state is recorded").isNotNull();
        assertThat((Integer) chunks.get("transient_retries")).as("and counted: the move retries").isGreaterThanOrEqualTo(1);
        for (String child : CHILD_ID_COLUMN.keySet()) {
            assertThat(idTwins(child)).as("the on_complete was rolled back: no id twin on " + child).isEmpty();
        }
        assertThat(compositeOnId()).as("nor the composite on the id").isEmpty();
        assertThat(registeredBySwap()).as("nor a validation move").isEmpty();
    }
}
