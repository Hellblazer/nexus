// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import dev.nexus.service.IdKeyIndexProbe;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.DeadlockRetry;
import dev.nexus.service.vectors.Rdr229ChunksFk;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.sql.Connection;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CyclicBarrier;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * RDR-229 P3.3c (nexus-p2m1x.21): the per-leaf route to {@code chunks_collection_id_fk}. The chunks swap's on_complete adds each leaf's key
 * NOT VALID and registers one validation move per leaf and the adoption move; each leaf validates in its own completion transaction (no statement
 * bound, a lock wait); the adoption move adds the parent key only when every leaf has a validated key that matches exactly, and PostgreSQL 17
 * adopts them with no scan. Real worker, real finisher, the real changelog (see {@link Rdr229SwapMovesFixtures}).
 *
 * <p><b>Reading the scan counts.</b> {@code pg_stat_user_tables} is flushed by an idle session after up to 10 s, so a count read right after
 * a worker's work can miss it. The scan tests wait 11 s after the leaf phase for the pooled sessions to flush, then run the step under test
 * on a session of the test's own, closed before the reading (a closing session flushes at once). The wait is the price of the number.
 */
class Rdr229ChunksFkIntegrationTest extends Rdr229SwapMovesFixtures {

    @org.junit.jupiter.api.AfterEach
    void releaseParked() throws Exception {
        releaseWhatTheTestParked();
    }

    // ── helpers ─────────────────────────────────────────────────────────────────────────────────────────────

    /** {@code seq_scan, seq_tup_read} of every leaf of chunks, by leaf, as the statistics views show them. */
    private Map<String, long[]> leafScans() {
        Map<String, long[]> out = new TreeMap<>();
        Field<String> relname = DSL.field(DSL.name("relname"), String.class);
        Field<Long> seq = DSL.field(DSL.name("seq_scan"), Long.class);
        Field<Long> tup = DSL.field(DSL.name("seq_tup_read"), Long.class);
        ctx().select(relname, seq, tup).from(DSL.table(DSL.name("pg_catalog", "pg_stat_user_tables")))
            .where(DSL.field(DSL.name("schemaname"), String.class).eq("nexus")).and(relname.in(chunkLeaves()))
            .fetch().forEach(r -> out.put(r.value1(), new long[] {r.value2(), r.value3()}));
        return out;
    }





    private void assertEveryLeafHasOneValidatedAdoptedKey() {
        var fks = leafFks();
        assertThat(chunkLeaves()).as("non-vacuity: chunks has leaves").isNotEmpty();
        for (String leaf : chunkLeaves()) {
            var ofLeaf = fks.stream().filter(f -> f.leaf().equals(leaf)).toList();
            assertThat(ofLeaf).as("%s: one key to the registry on the composite's columns", leaf).hasSize(1);
            assertThat(ofLeaf.get(0).validated()).as("%s: validated", leaf).isTrue();
            assertThat(ofLeaf.get(0).parentId()).as("%s: adopted by the parent key (conparentid set)", leaf).isNotZero();
        }
        assertThat(fks.stream().filter(f -> f.parentId() == 0)).as("no chunks leaf key is left standing alone (conparentid 0)").isEmpty();
        List<String> middle = chunkModelPartitions();
        var middleKeys = compositeKeysOn(middle);
        long middleWithKey = middleKeys.stream().filter(f -> f.parentId() != 0).map(LeafFk::leaf).distinct().count();
        assertThat(middle).as("non-vacuity: chunks has model partitions between the parent and its leaves").isNotEmpty();
        assertThat(middleWithKey).as("every model partition got the key from the parent ADD, attached to it").isEqualTo((long) middle.size());
    }

    // ── 1 ───────────────────────────────────────────────────────────────────────────────────────────────────

    /**
     * The swap adds a NOT VALID key to every leaf and registers one validation move per leaf and the adoption move, and not the composite. One
     * leaf validates, a worker restarts (a new worker has none of the old one's memory), the rest validate, and the adoption move adds the
     * composite: every leaf key is validated and adopted, none stands alone.
     */
    @Test
    void theSwapQueuesTheLeavesAndTheAdoptionMoveWhichAddsTheCompositeOnceEveryLeafIsValidated_acrossARestart() {
        seed("leaf");
        registerSwaps();
        runSwaps(new VerifyingFinisher());

        assertThat(completedAt(CHUNKS_SWAP)).as("the swap completed").isNotNull();
        assertThat(compositeOnId()).as("without the composite").isEmpty();
        List<String> leaves = chunkLeaves();
        assertThat(leaves).as("non-vacuity: several leaves").hasSizeGreaterThanOrEqualTo(2);
        var fks = leafFks();
        assertThat(fks.stream().map(LeafFk::leaf).toList()).as("each leaf has exactly one key to the registry").containsExactlyElementsOf(leaves);
        assertThat(fks).allSatisfy(f -> {
            assertThat(f.validated()).as(f.leaf() + ": NOT VALID until its move runs").isFalse();
            assertThat(f.parentId()).isZero();
            assertThat(f.name()).isEqualTo(f.leaf() + Rdr229ChunksFk.LEAF_CONSTRAINT_SUFFIX);
        });
        var leafMoves = leafMoves();
        assertThat(leafMoves.stream().map(m -> (String) m.get("label")).toList())
            .as("one validation move per leaf").containsExactlyInAnyOrderElementsOf(
                leaves.stream().map(l -> Rdr229ChunksFk.LEAF_LABEL_PREFIX + l).toList());
        assertThat(leafMoves).allSatisfy(m -> {
            assertThat((Integer) m.get("statement_timeout_ms")).as("a validation that blocks no writer has no statement bound")
                .isEqualTo(DataMove.NO_STATEMENT_BOUND_MS);
            assertThat((String) m.get("on_complete")).contains("VALIDATE CONSTRAINT").doesNotContain("ADD CONSTRAINT");
        });
        var adopt = adoptMoves();
        assertThat(adopt).as("one adoption move").hasSize(1);
        assertThat((Integer) adopt.get(0).get("statement_timeout_ms")).as("the step that adds the parent key keeps the tight bound")
            .isEqualTo(1400);
        assertThat((java.time.OffsetDateTime) adopt.get(0).get("registered_at")).as("registered after every leaf move, so the worker takes it last")
            .isAfterOrEqualTo(leafMoves.stream().map(m -> (java.time.OffsetDateTime) m.get("registered_at")).max(java.time.OffsetDateTime::compareTo).get());

        // one leaf validates; the adoption move and the other leaves are held. The "restart" is the next worker, which knows nothing of this one
        String first = leafMoves.get(0).get("label").toString();
        pause(Rdr229ChunksFk.LEAF_LABEL_PREFIX + "%");
        resume(first);
        pause(Rdr229ChunksFk.ADOPT_LABEL);
        productionWorker(new VerifyingFinisher()).runCycle();
        assertThat(completedAt(first)).as("the one leaf's move completed in its own transaction").isNotNull();
        assertThat(leafFks().stream().filter(LeafFk::validated).count()).as("and only its key is validated").isEqualTo(1);
        assertThat(compositeOnId()).isEmpty();

        resume(Rdr229ChunksFk.LEAF_LABEL_PREFIX + "%");
        productionWorker(new VerifyingFinisher()).runCycle();   // a worker that has none of the first one's memory
        assertThat(leafFks()).as("the rest validated after the restart").allSatisfy(f -> assertThat(f.validated()).isTrue());
        assertThat(compositeOnId()).as("the adoption move is still held").isEmpty();

        resume(Rdr229ChunksFk.ADOPT_LABEL);
        productionWorker(new VerifyingFinisher()).runCycle();
        assertThat(completedAt(Rdr229ChunksFk.ADOPT_LABEL)).as("the adoption move completed").isNotNull();
        assertThat(compositeOnId()).as("the composite is there").hasSize(1);
        assertThat(compositeOnId().get(0).validated()).isTrue();
        assertEveryLeafHasOneValidatedAdoptedKey();
        assertNameKeysStay();
    }

    // ── 2 ───────────────────────────────────────────────────────────────────────────────────────────────────

    /** The adoption step on validated leaves scans no leaf, and the composite's leaf keys are all adopted (conparentid set). */
    @Test
    void theParentStepAdoptsTheValidatedLeafKeysWithZeroScanDeltas() throws Exception {
        seed("scan");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        pause(Rdr229ChunksFk.ADOPT_LABEL);
        productionWorker(new VerifyingFinisher()).runCycle();   // the leaf moves validate every leaf; the adoption move is held
        assertThat(leafFks()).allSatisfy(f -> assertThat(f.validated()).as(f.leaf()).isTrue());
        assertThat(compositeOnId()).isEmpty();
        // the VALIDATEs scanned the leaves; let their counts land, then measure the step alone
        sleepMs(11_000);
        Map<String, long[]> before = leafScans();

        runOnCompleteOnOwnSession(onCompleteOf(Rdr229ChunksFk.ADOPT_LABEL));
        sleepMs(1_500);
        Map<String, long[]> after = leafScans();

        assertThat(compositeOnId()).as("the composite is there").hasSize(1);
        assertThat(before).as("non-vacuity: the leaves' counts were read").isNotEmpty();
        for (var e : before.entrySet()) {
            assertThat(after.get(e.getKey())[0] - e.getValue()[0]).as("%s: no sequential scan by the parent step", e.getKey()).isZero();
            assertThat(after.get(e.getKey())[1] - e.getValue()[1]).as("%s: no tuple read by the parent step", e.getKey()).isZero();
        }
        assertEveryLeafHasOneValidatedAdoptedKey();
    }

    // ── 3 ───────────────────────────────────────────────────────────────────────────────────────────────────

    /**
     * A leaf created after the validations: the parent step does NOT add the composite (it would scan the new leaf and build a second key
     * there). It adds the new leaf's key NOT VALID, queues the leaf's validation move and re-opens itself (no scan); the next call, with the
     * work queued, raises 55P03 and adds nothing; once the leaf validates, the step adds the composite.
     */
    @Test
    void aLeafAddedAfterTheValidationsMakesTheParentStepDeferWithNoScanThenComplete() throws Exception {
        seed("late");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        pause(Rdr229ChunksFk.ADOPT_LABEL);
        productionWorker(new VerifyingFinisher()).runCycle();
        assertThat(leafFks()).allSatisfy(f -> assertThat(f.validated()).isTrue());
        int leavesBefore = chunkLeaves().size();

        // the leaves come after the validations, the production way: the service role's create_tenant_partitions (SECURITY DEFINER, so they belong
        // to the migrating role and the grants changeset a later migration runs can touch them). One tenant gets a leaf under every model partition.
        String lateTenant = "p33c-late-leaf-" + System.nanoTime();
        tenantsUsed.add(lateTenant);
        createTenantLeaves(lateTenant);
        assertThat(chunkLeaves()).as("the late tenant's leaves are there").hasSizeGreaterThan(leavesBefore);
        List<String> lateLeaves = chunkLeaves().stream().filter(l -> leafFks().stream().noneMatch(f -> f.leaf().equals(l))).toList();
        assertThat(lateLeaves).as("non-vacuity: leaves with no key").isNotEmpty();

        sleepMs(11_000);
        Map<String, long[]> before = leafScans();

        // 1: the step defers: it queues the new leaf and does not add the composite
        runOnCompleteOnOwnSession(onCompleteOf(Rdr229ChunksFk.ADOPT_LABEL));
        sleepMs(1_500);
        Map<String, long[]> after = leafScans();
        assertThat(compositeOnId()).as("deferred: no composite while a leaf has no validated key").isEmpty();
        for (String late : lateLeaves) {
            assertThat(leafFks().stream().filter(f -> f.leaf().equals(late)).toList()).as("%s: the late leaf got its key, NOT VALID", late)
                .singleElement().satisfies(f -> assertThat(f.validated()).isFalse());
            assertThat(leafMoves().stream().map(m -> (String) m.get("label")).toList()).as("and its validation move")
                .contains(Rdr229ChunksFk.LEAF_LABEL_PREFIX + late);
        }
        for (var e : before.entrySet()) {
            assertThat(after.get(e.getKey())[0] - e.getValue()[0]).as("%s: the deferred step scanned nothing", e.getKey()).isZero();
        }
        for (String late : lateLeaves) {
            assertThat(after.getOrDefault(late, new long[] {0, 0})[0] - before.getOrDefault(late, new long[] {0, 0})[0])
                .as("%s: not the late leaf either", late).isZero();
        }

        // 2: the work is already queued and nothing is new: the step raises 55P03 (a backed-off transient, not a re-open every poll), takes no lock on
        // chunks and adds no composite
        assertThatThrownBy(() -> runOnCompleteOnOwnSession(onCompleteOf(Rdr229ChunksFk.ADOPT_LABEL)))
            .as("a leaf move is queued and has not run").satisfies(t -> assertThat(sqlState(t)).isEqualTo("55P03"));
        assertThat(compositeOnId()).as("still deferred while the new leaf's move has not run").isEmpty();

        // 3: the leaf validates and the step completes
        resume(Rdr229ChunksFk.ADOPT_LABEL);
        runUntilComposite(new VerifyingFinisher());
        assertThat(compositeOnId()).as("the composite arrived once the new leaf validated").hasSize(1);
        assertEveryLeafHasOneValidatedAdoptedKey();
    }

    // ── 4 ───────────────────────────────────────────────────────────────────────────────────────────────────

    /**
     * A writer keeps inserting while a leaf VALIDATE is held: the production on_complete of every leaf move runs in a transaction that stays open (SHARE UPDATE
     * EXCLUSIVE on each leaf and ROW SHARE on the registry, held to the commit), and chunk inserts run meanwhile, each under a 2 s lock wait, and
     * none waits. The writes go through {@link DeadlockRetry}: the test's own attempt counter and the retry counter both stay at the number of
     * writes and zero, so a deadlock the belt absorbed would show.
     */
    @Test
    void aWriterKeepsInsertingWhileEveryLeafValidateIsHeld_andNoDeadlockIsAbsorbed() throws Exception {
        var t = seed("writer");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        assertThat(leafFks()).as("non-vacuity: keys to validate").isNotEmpty().allSatisfy(f -> assertThat(f.validated()).isFalse());
        pause("rdr229-%");   // the worker takes none of them; the test holds the VALIDATEs
        String tenant = t.catalogDocumentChunks();
        String collection = StampMoveSeeds.collection(tenant, 0);

        try (Connection holder = pg.createConnection(""); Connection writer = pg.createConnection("")) {
            holder.setAutoCommit(false);
            {
                // the PRODUCTION leaf on_complete of every leaf, under the bounds the finisher gives a validation (no statement bound, 5 s lock wait),
                // in a transaction that stays open
                setLocal(holder, "statement_timeout", 0);
                setLocal(holder, "lock_timeout", "5s");
                for (var m : leafMoves()) {
                    MoveSql.runOnComplete(holder, (String) m.get("on_complete"));
                }
                int holderPid = pid(holder);
                long held = ctx().fetchCount(DSL.table(DSL.name("pg_catalog", "pg_locks")),
                    DSL.field(DSL.name("pid"), Integer.class).eq(holderPid),
                    DSL.field(DSL.name("locktype"), String.class).eq("relation"),
                    DSL.field(DSL.name("mode"), String.class).eq("ShareUpdateExclusiveLock"),
                    DSL.field(DSL.name("granted"), Boolean.class).isTrue());
                assertThat(held).as("non-vacuity: the VALIDATE's SHARE UPDATE EXCLUSIVE locks are held").isGreaterThanOrEqualTo(leafFks().size());

                long retriesBefore = DeadlockRetry.retryAttemptCount();
                AtomicInteger attempts = new AtomicInteger();
                int writes = 3;
                CompletableFuture<Void> done = CompletableFuture.runAsync(() -> {
                    for (int i = 0; i < writes; i++) {
                        String hex = String.format("%064x", System.nanoTime() + i);
                        DeadlockRetry.run("p33c-writer", () -> {
                            attempts.incrementAndGet();
                            try {
                                writer.setAutoCommit(false);
                                DSLContext w = DSL.using(writer, SQLDialect.POSTGRES);
                                setLocal(writer, "lock_timeout", "2s");
                                PgContainerHelper.insertChunks(w, tenant, collection, List.of(hex), List.of("w"), List.of(new float[768]), List.of(Map.of()));
                                writer.commit();
                            } catch (SQLException | RuntimeException e) {
                                try {
                                    writer.rollback();
                                } catch (SQLException ignored) {
                                    // the original failure is the one to report
                                }
                                throw e instanceof RuntimeException re ? re : new IllegalStateException(e);
                            }
                        });
                    }
                });
                done.get(20, TimeUnit.SECONDS);   // a blocked writer would hit its 2 s lock wait and fail, or this bound
                assertThat(attempts.get()).as("each write ran once: no deadlock retried it").isEqualTo(writes);
                assertThat(DeadlockRetry.retryAttemptCount() - retriesBefore).as("DeadlockRetry retried nothing").isZero();
                holder.commit();
            }
        }
        assertThat(leafFks()).as("the held VALIDATEs committed").allSatisfy(f -> assertThat(f.validated()).isTrue());
    }

    /**
     * The negative control of the test above: two sessions that take the same two locks in opposite order deadlock, DeadlockRetry absorbs the
     * 40P01 and the work ends clean, and the counters read it. It proves the "retried nothing" assertion above has teeth: this is what it sees
     * when there is a deadlock.
     */
    @Test
    void negativeControl_conflictingLockOrderIsSeenByTheRetryCounters() throws Exception {
        try (Connection a = pg.createConnection(""); Connection b = pg.createConnection(""); Connection setup = pg.createConnection("")) {
            for (String t : List.of("p33c_dl_a", "p33c_dl_b")) {
                DSL.using(setup, SQLDialect.POSTGRES).createTableIfNotExists(table("nexus", t)).column("x", SQLDataType.INTEGER).execute();
            }
            long retriesBefore = DeadlockRetry.retryAttemptCount();
            AtomicInteger attemptsA = new AtomicInteger();
            AtomicInteger attemptsB = new AtomicInteger();
            CyclicBarrier firstLockTaken = new CyclicBarrier(2);
            CompletableFuture<Void> fa = CompletableFuture.runAsync(() -> opposite(a, "p33c_dl_a", "p33c_dl_b", attemptsA, firstLockTaken));
            CompletableFuture<Void> fb = CompletableFuture.runAsync(() -> opposite(b, "p33c_dl_b", "p33c_dl_a", attemptsB, firstLockTaken));
            fa.get(30, TimeUnit.SECONDS);
            fb.get(30, TimeUnit.SECONDS);
            assertThat(attemptsA.get() + attemptsB.get()).as("one session was the deadlock victim and ran again").isEqualTo(3);
            assertThat(DeadlockRetry.retryAttemptCount() - retriesBefore).as("and DeadlockRetry counted its retry").isGreaterThanOrEqualTo(1);
            DSL.using(setup, SQLDialect.POSTGRES).dropTableIfExists(table("nexus", "p33c_dl_a")).execute();
            DSL.using(setup, SQLDialect.POSTGRES).dropTableIfExists(table("nexus", "p33c_dl_b")).execute();
        }
    }

    private static void opposite(Connection c, String first, String second, AtomicInteger attempts, CyclicBarrier firstLockTaken) {
        DeadlockRetry.run("p33c-negative-control", () -> {
            int attempt = attempts.incrementAndGet();
            try {
                c.setAutoCommit(false);
                lock(c, first, PgContainerHelper.LockMode.ACCESS_EXCLUSIVE);
                if (attempt == 1) {
                    firstLockTaken.await(10, TimeUnit.SECONDS);   // both hold their first lock before either asks for the second
                }
                lock(c, second, PgContainerHelper.LockMode.ACCESS_EXCLUSIVE);
                c.commit();
            } catch (SQLException | org.jooq.exception.DataAccessException | InterruptedException | java.util.concurrent.TimeoutException
                     | java.util.concurrent.BrokenBarrierException e) {
                // the deadlock victim's transaction is aborted: it ends here, before DeadlockRetry runs the work again on this connection
                try {
                    c.rollback();
                } catch (SQLException ignored) {
                    // the original failure is the one to report
                }
                throw new IllegalStateException(e);
            }
        });
    }

    // ── 5: predicate drift ──────────────────────────────────────────────────────────────────────────────────

    /**
     * A validated leaf key that differs from the composite in one thing PostgreSQL compares (the delete action, deferrability, the column
     * order) is NOT adopted: PostgreSQL would build a second key on that leaf and scan it under SHARE ROW EXCLUSIVE. The step must see the
     * difference, add no composite, and queue the leaf's own matching key.
     */
    @ParameterizedTest
    @ValueSource(strings = {"NO_ACTION", "DEFERRABLE", "COLUMN_ORDER"})
    void aValidatedLeafKeyThatDiffersIsNotAdopted_theStepQueuesTheLeafAndAddsNoComposite(String difference) throws Exception {
        seed("drift-" + difference.toLowerCase(java.util.Locale.ROOT).replace('_', '-'));
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        pause(Rdr229ChunksFk.ADOPT_LABEL);
        productionWorker(new VerifyingFinisher()).runCycle();
        assertThat(leafFks()).as("precondition: every leaf key validated").allSatisfy(f -> assertThat(f.validated()).isTrue());
        String leaf = chunkLeaves().get(0);
        String mismatch = "p33c_mismatch_" + difference.toLowerCase(java.util.Locale.ROOT);
        ctx().alterTable(table("nexus", leaf)).dropConstraint(DSL.name(leaf + Rdr229ChunksFk.LEAF_CONSTRAINT_SUFFIX)).execute();
        List<String> columns = difference.equals("COLUMN_ORDER") ? List.of("collection_id", "tenant_id", "embedding_model") : LEAF_KEY_COLUMNS;
        var action = difference.equals("NO_ACTION") ? PgContainerHelper.DeleteAction.NO_ACTION : PgContainerHelper.DeleteAction.RESTRICT;
        try {
            try (Connection c = pg.createConnection("")) {
                PgContainerHelper.addFkNotValidColumns(c, table("nexus", leaf), mismatch, columns, table("nexus", "catalog_collections"), columns, action,
                    difference.equals("DEFERRABLE"));
                PgContainerHelper.validateConstraint(c, table("nexus", leaf), mismatch);   // validated, as the leaf's own key was
            }
            resume(Rdr229ChunksFk.ADOPT_LABEL);

            runOnCompleteOnOwnSession(onCompleteOf(Rdr229ChunksFk.ADOPT_LABEL));

            assertThat(compositeOnId()).as("%s: the composite is not added over a key PostgreSQL would not adopt", difference).isEmpty();
            String ownName = leaf + Rdr229ChunksFk.LEAF_CONSTRAINT_SUFFIX;
            List<Boolean> own = compositeKeysOn(List.of(leaf)).stream().filter(f -> f.name().equals(ownName)).map(LeafFk::validated).toList();
            assertThat(own).as("%s: the leaf's own key was queued, NOT VALID", difference).containsExactly(false);
            assertThat(completedAt(Rdr229ChunksFk.LEAF_LABEL_PREFIX + leaf)).as("and its validation move is open").isNull();

            runUntilComposite(new VerifyingFinisher());
            assertThat(compositeOnId()).as("%s: the composite arrives once the leaf's own key validated", difference).hasSize(1);
            List<Long> parents = compositeKeysOn(List.of(leaf)).stream().filter(f -> f.name().equals(ownName)).map(LeafFk::parentId).toList();
            assertThat(parents).as("%s: the matching key was adopted", difference).singleElement().isNotEqualTo(0L);
        } finally {
            ctx().alterTable(table("nexus", leaf)).dropConstraintIfExists(DSL.name(mismatch)).execute();
        }
    }

    // ── 6: the adoption move's states ───────────────────────────────────────────────────────────────────────

    /**
     * A leaf with no validated key yet (here: leaves created after every existing leaf validated) makes the adoption move re-open itself in its
     * first pass: that is no completion (no event, no listener call), counts no retry, adds no composite, and bumps its registered_at. Every
     * other move is paused, so the listener can only hear the adoption move, and one cycle is run so the leaf moves it registers do not.
     */
    @Test
    void theAdoptionMoveThatReopensItselfIsNotACompletionAndCountsNoRetry() throws Exception {
        seed("reopen");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        validateEveryLeafByHand();
        pause("rdr229-%");
        resume(Rdr229ChunksFk.ADOPT_LABEL);
        var before = adoptMoves().get(0);
        String lateTenant = "p33c-reopen-leaf-" + System.nanoTime();
        tenantsUsed.add(lateTenant);
        createTenantLeaves(lateTenant);
        List<String> lateLeaves = chunkLeaves().stream().filter(l -> leafFks().stream().noneMatch(f -> f.leaf().equals(l))).toList();
        assertThat(lateLeaves).as("non-vacuity: leaves with no key").isNotEmpty();
        var completions = new AtomicInteger();
        var w = productionWorker(new VerifyingFinisher());
        w.onMoveCompleted(completions::incrementAndGet);

        w.runCycle();

        assertThat(completions.get()).as("nothing completed: the adoption move re-opened itself").isZero();
        var adopt = adoptMoves().get(0);
        assertThat(adopt.get("completed_at")).as("still open").isNull();
        assertThat(adopt.get("failed_at")).isNull();
        assertThat((Integer) adopt.get("transient_retries")).as("a wait for a leaf is no failure: no retry counted").isZero();
        assertThat((java.time.OffsetDateTime) adopt.get("registered_at")).as("it sorts after the leaf moves it just queued")
            .isAfter((java.time.OffsetDateTime) before.get("registered_at"));
        for (String late : lateLeaves) {
            assertThat(leafMoves().stream().map(m -> (String) m.get("label")).toList()).as("the move of late leaf %s was queued", late)
                .contains(Rdr229ChunksFk.LEAF_LABEL_PREFIX + late);
        }
        assertThat(compositeOnId()).isEmpty();
    }

    /** A leaf move that is failed is named by the adoption move, which fails loud (permanent) instead of waiting on it for ever. */
    @Test
    void aFailedLeafMoveFailsTheAdoptionMoveAndNamesIt() {
        seed("failedleaf");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        String failed = (String) leafMoves().get(0).get("label");
        ctx().update(table("nexus", "data_moves")).set(DSL.field(DSL.name("failed_at"), OffsetDateTime.class), OffsetDateTime.now())
            .set(DSL.field(DSL.name("failure"), String.class), "planted").where(LABEL_F.eq(failed)).execute();
        pause(Rdr229ChunksFk.LEAF_LABEL_PREFIX + "%");

        productionWorker(new VerifyingFinisher()).runCycle();

        var adopt = adoptMoves().get(0);
        assertThat(adopt.get("failed_at")).as("the adoption move failed, loud").isNotNull();
        assertThat((String) adopt.get("failure")).as("naming the leaf move, and saying the adoption move needs re-arming too").contains(failed).contains("failed")
            .contains("re-arm this move");
        assertThat(compositeOnId()).isEmpty();
    }

    /** A leaf move that is paused is named by a 55P03 (transient), so an operator reading the blocked state sees which leaf holds it. */
    @Test
    void aPausedLeafMoveBlocksTheAdoptionMoveAndNamesTheLeaf() {
        seed("pausedleaf");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        pause(Rdr229ChunksFk.LEAF_LABEL_PREFIX + "%");

        productionWorker(new VerifyingFinisher()).runCycle();

        var adopt = adoptMoves().get(0);
        assertThat(adopt.get("failed_at")).as("blocked, not failed").isNull();
        assertThat((String) adopt.get("last_transient")).as("the blocked state names the paused leaf").contains("paused").contains("55P03");
    }

    /** A validation move whose leaf is gone fails loud, with 42P01 naming it, and does not complete as a no-op. */
    @Test
    void aLeafValidationMoveWhoseLeafIsGoneFailsLoud() throws Exception {
        seed("goneleaf");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        String leaf = chunkLeaves().get(0);
        String text = onCompleteOf(Rdr229ChunksFk.LEAF_LABEL_PREFIX + leaf);
        assertThat(text).as("the text names the leaf by schema and name, quoted").contains("quote_ident('nexus')").contains("quote_ident('" + leaf + "')");
        String gone = text.replace("quote_ident('" + leaf + "')", "quote_ident('p33c_no_such_leaf')");

        assertThatThrownBy(() -> runOnCompleteOnOwnSession(gone)).as("a leaf that is not there is not 'validated'")
            .satisfies(t -> assertThat(sqlState(t)).isEqualTo("42P01"));
    }

    // ── 7: the lock a validation waits on, and what the status shows ────────────────────────────────────────





    /**
     * The production leaf on_complete that cannot get the leaf's lock waits the 5 s lock wait of a validation, not 700 ms, then fails 55P03
     * (transient: blocked, retried). While it waits its backend is known to the status (pid and start) and is a {@code nexus-data-move-}
     * session. The leaf key stays NOT VALID.
     */
    @Test
    void aLeafValidationBehindAHeldAccessExclusiveWaitsAboutFiveSecondsThenBlocksOn55P03() throws Exception {
        Held held = holdOneLeaf();
        try (Connection holder = held.connection()) {
            var w = held.worker();
            long started = System.nanoTime();
            var cycle = CompletableFuture.runAsync(w::runCycle);
            int pid = completionPid(w, held.label(), 3_000);
            assertThat(pid).as("the completion's backend is recorded for the status while it waits").isPositive();
            String application = ctx().select(DSL.field(DSL.name("application_name"), String.class))
                .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity"))).where(DSL.field(DSL.name("pid"), Integer.class).eq(pid)).fetchOne(0, String.class);
            assertThat(application).as("the session carries the data-move name, so a boot can reap it").startsWith("nexus-data-move-");
            cycle.get(30, TimeUnit.SECONDS);
            long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - started);
            holder.rollback();

            assertThat(elapsedMs).as("it waited the validation's 5 s lock wait, not the 700 ms of a step that blocks writers").isGreaterThanOrEqualTo(4_500L);
            var row = moves(LABEL_F.eq(held.label())).get(0);
            assertThat(row.get("completed_at")).as("not completed").isNull();
            assertThat(row.get("failed_at")).as("blocked, never failed").isNull();
            assertThat((String) row.get("last_transient")).as("the lock timeout is the recorded transient").contains("55P03");
            assertThat(leafFks().stream().filter(f -> f.leaf().equals(held.leaf())).toList()).singleElement()
                .satisfies(f -> assertThat(f.validated()).isFalse());
            assertThat(w.states().call(held.label())).as("and the in-flight record is gone").isEmpty();
        }
    }

    /** Stopping the worker cancels the completion statement in flight: the cycle ends at once, the move is not failed and no transient is counted. */
    @Test
    void stoppingTheWorkerCancelsAValidationWaitingOnALock() throws Exception {
        Held held = holdOneLeaf();
        try (Connection holder = held.connection()) {
            var w = held.worker();
            var cycle = CompletableFuture.runAsync(w::runCycle);
            assertThat(completionPid(w, held.label(), 3_000)).as("the validation is waiting").isPositive();
            long stopAt = System.nanoTime();

            w.stop();
            cycle.get(3, TimeUnit.SECONDS);

            assertThat(TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - stopAt)).as("the wait was cut short, not run out").isLessThan(3_000L);
            holder.rollback();
            var row = moves(LABEL_F.eq(held.label())).get(0);
            assertThat(row.get("failed_at")).as("a stop is not a failure").isNull();
            assertThat(row.get("last_transient")).as("nor a transient").isNull();
            assertThat(row.get("completed_at")).isNull();
        }
    }

    /** A boot ends the {@code nexus-data-move-} backends a stopped engine left, before the migration. */
    @Test
    void theBootReapsLeftoverDataMoveBackends() throws Exception {
        java.util.Properties props = new java.util.Properties();
        props.setProperty("user", adminConnection.user());
        props.setProperty("password", adminConnection.password());
        props.setProperty("ApplicationName", "nexus-data-move-leftover-test");
        try (Connection orphan = java.sql.DriverManager.getConnection(adminConnection.url(), props)) {
            assertThat(orphan.isValid(2)).as("precondition: the leftover backend is alive").isTrue();

            int ended = DataMoveOrphans.terminateLeftover(adminConnection);

            assertThat(ended).as("it ended at least the leftover").isGreaterThanOrEqualTo(1);
            assertThat(orphan.isValid(2)).as("and it is gone").isFalse();
        }
    }

    // ── 8: the role that validates ──────────────────────────────────────────────────────────────────────────

    /**
     * The role the data-move worker runs as owns the registry and every chunks leaf, including a leaf created after the migration through the
     * service path ({@code create_tenant_partitions} is SECURITY DEFINER, so the leaf belongs to its owner): PostgreSQL validates a key
     * set-based only when the validating role owns both tables (row security then does not apply to the check), and falls back to one probe
     * per row otherwise. And one production leaf VALIDATE, run as that role, probes the registry by index not once (it is set-based).
     */
    @Test
    void theWorkerRoleOwnsTheRegistryAndEveryLeafAndAProductionLeafValidateIsSetBased() throws Exception {
        seed("owner");
        registerSwaps();
        // a leaf created after the migration, the way the service creates it (nexus_svc calling the SECURITY DEFINER function)
        String lateTenant = "p33c-owner-leaf-" + System.nanoTime();
        tenantsUsed.add(lateTenant);
        createTenantLeaves(lateTenant);
        String worker = adminConnection.user();
        Field<String> relname = DSL.field(DSL.name("c", "relname"), String.class);
        Field<String> owner = DSL.function(DSL.name("pg_catalog", "pg_get_userbyid"), String.class, DSL.field(DSL.name("c", "relowner")));
        List<String> relations = new java.util.ArrayList<>(chunkLeaves());
        relations.add("catalog_collections");
        List<String> owners = ctx().select(relname, owner)
            .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
            .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n")).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus")).and(relname.in(relations)).and(owner.ne(worker))
            .fetch(r -> r.value1() + " (" + r.value2() + ")");
        assertThat(owners).as("tables not owned by %s, the role the data-move worker runs as", worker).isEmpty();
        assertThat(chunkLeaves()).as("non-vacuity: chunks has leaves").hasSizeGreaterThanOrEqualTo(2);

        runSwaps(new VerifyingFinisher());
        String leaf = chunkLeaves().get(0);
        long rows = ctx().fetchCount(table("nexus", leaf));
        assertThat(rows).as("non-vacuity: the leaf has rows, so a per-row fallback would probe the registry").isPositive();
        String text = onCompleteOf(Rdr229ChunksFk.LEAF_LABEL_PREFIX + leaf);
        java.util.Properties props = new java.util.Properties();
        props.setProperty("user", adminConnection.user());
        props.setProperty("password", adminConnection.password());
        try (Connection c = java.sql.DriverManager.getConnection(adminConnection.url(), props)) {
            c.setAutoCommit(false);
            MoveSql.runOnComplete(c, text);
            Field<Long> idxScan = DSL.field(DSL.name("idx_scan"), Long.class);
            Field<Long> seqScan = DSL.field(DSL.name("seq_scan"), Long.class);
            var scanned = DSL.using(c, SQLDialect.POSTGRES).select(seqScan, idxScan).from(DSL.table(DSL.name("pg_catalog", "pg_stat_xact_user_tables")))
                .where(DSL.field(DSL.name("schemaname"), String.class).eq("nexus")).and(DSL.field(DSL.name("relname"), String.class).eq("catalog_collections"))
                .fetchOne();
            long probes = scanned == null ? 0L : (scanned.value1() == null ? 0L : scanned.value1()) + (scanned.value2() == null ? 0L : scanned.value2());
            c.rollback();
            assertThat(probes).as("the VALIDATE as %s scanned the registry %d times for %d rows: a handful at most is the set-based check (the per-row fallback "
                + "scans it about once per row; the control in Rdr229ChunksFkHardeningIntegrationTest shows it)", worker, probes, rows)
                .isLessThanOrEqualTo(3L);
        }
    }


}
