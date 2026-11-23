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
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.sql.Connection;
import java.sql.SQLException;
import java.sql.Statement;
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

    // ── helpers ─────────────────────────────────────────────────────────────────────────────────────────────

    private void pause(String labelLike) {
        ctx().execute("UPDATE nexus.data_moves SET paused_at = now(), paused_by = 'p33c-test' WHERE label LIKE ?", labelLike);
    }

    private void resume(String labelLike) {
        ctx().execute("UPDATE nexus.data_moves SET paused_at = NULL, paused_by = NULL WHERE label LIKE ?", labelLike);
    }

    private String onCompleteOf(String label) {
        return ctx().fetchOne("SELECT on_complete FROM nexus.data_moves WHERE label = ?", label).get(0, String.class);
    }

    /** {@code seq_scan, seq_tup_read} of every leaf of chunks, by leaf, as the statistics views show them. */
    private Map<String, long[]> leafScans() {
        Map<String, long[]> out = new TreeMap<>();
        ctx().resultQuery("SELECT s.relname, s.seq_scan, s.seq_tup_read FROM pg_catalog.pg_stat_user_tables s "
                + "WHERE s.schemaname = 'nexus' AND s.relname IN (SELECT c.relname FROM pg_catalog.pg_partition_tree('nexus.chunks'::pg_catalog.regclass) pt "
                + "JOIN pg_catalog.pg_class c ON c.oid = pt.relid WHERE pt.isleaf)")
            .fetch().forEach(r -> out.put(r.get(0, String.class), new long[] {r.get(1, Long.class), r.get(2, Long.class)}));
        return out;
    }

    private static void sleepMs(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException(e);
        }
    }

    /** Runs {@code sql} (a registry on_complete) in one transaction on a session of its own, closed on return so its statistics are flushed. */
    private void runOnCompleteOnOwnSession(String sql) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            try {
                MoveSql.runOnComplete(c, sql);
                c.commit();
            } catch (SQLException | RuntimeException e) {
                c.rollback();
                throw e;
            }
        }
    }

    private static String sqlState(Throwable t) {
        for (Throwable c = t; c != null; c = c.getCause()) {
            if (c instanceof SQLException se) {
                return se.getSQLState();
            }
        }
        return null;
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
        long middle = ctx().fetchOne("SELECT count(*) FROM pg_catalog.pg_partition_tree('nexus.chunks'::pg_catalog.regclass) pt "
            + "WHERE NOT pt.isleaf AND pt.level > 0").get(0, Long.class);
        long middleWithKey = ctx().fetchOne("SELECT count(*) FROM pg_catalog.pg_partition_tree('nexus.chunks'::pg_catalog.regclass) pt "
            + "WHERE NOT pt.isleaf AND pt.level > 0 AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint k WHERE k.conrelid = pt.relid "
            + "AND k.contype = 'f' AND k.conparentid <> 0 AND pg_catalog.pg_get_constraintdef(k.oid) LIKE 'FOREIGN KEY (tenant_id, collection_id, "
            + "embedding_model) REFERENCES nexus.catalog_collections%')").get(0, Long.class);
        assertThat(middle).as("non-vacuity: chunks has model partitions between the parent and its leaves").isPositive();
        assertThat(middleWithKey).as("every model partition got the key from the parent ADD, attached to it").isEqualTo(middle);
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
     * work queued, does the same and adds nothing; once the leaf validates, the step adds the composite.
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

        // the leaf comes after the validations, through the engine's own function
        String lateTenant = "p33c-late-leaf-" + System.nanoTime();
        tenantsUsed.add(lateTenant);
        List<String> modelPartitions = IdKeyIndexProbe.modelPartitions(ctx(), IdKeyIndexProbe.CHUNKS);
        assertThat(modelPartitions).as("non-vacuity: chunks has a model partition").isNotEmpty();
        String modelPartition = modelPartitions.get(0);
        var regclass = new org.jooq.impl.DefaultDataType<>(SQLDialect.POSTGRES, Object.class, "regclass");
        Boolean created = ctx().select(DSL.function(DSL.name("nexus", "partition_ensure_leaf"), org.jooq.impl.SQLDataType.BOOLEAN,
            DSL.val("nexus.chunks").cast(regclass), DSL.val(modelPartition).cast(regclass), DSL.val(lateTenant), DSL.val(true))).fetchOne(0, Boolean.class);
        assertThat(created).as("the late leaf was created").isTrue();
        assertThat(chunkLeaves()).as("one more leaf").hasSize(leavesBefore + 1);
        String lateLeaf = chunkLeaves().stream().filter(l -> leafFks().stream().noneMatch(f -> f.leaf().equals(l))).findFirst().orElseThrow();

        sleepMs(11_000);
        Map<String, long[]> before = leafScans();

        // 1: the step defers: it queues the new leaf and does not add the composite
        runOnCompleteOnOwnSession(onCompleteOf(Rdr229ChunksFk.ADOPT_LABEL));
        sleepMs(1_500);
        Map<String, long[]> after = leafScans();
        assertThat(compositeOnId()).as("deferred: no composite while a leaf has no validated key").isEmpty();
        assertThat(leafFks().stream().filter(f -> f.leaf().equals(lateLeaf)).toList()).as("the late leaf got its key, NOT VALID")
            .singleElement().satisfies(f -> assertThat(f.validated()).isFalse());
        assertThat(leafMoves().stream().map(m -> (String) m.get("label")).toList()).as("and its validation move")
            .contains(Rdr229ChunksFk.LEAF_LABEL_PREFIX + lateLeaf);
        for (var e : before.entrySet()) {
            assertThat(after.get(e.getKey())[0] - e.getValue()[0]).as("%s: the deferred step scanned nothing", e.getKey()).isZero();
        }
        assertThat(after.getOrDefault(lateLeaf, new long[] {0, 0})[0] - before.getOrDefault(lateLeaf, new long[] {0, 0})[0])
            .as("not the late leaf either").isZero();

        // 2: the work is queued and nothing changes: the step returns, writes nothing that matters (it re-opens itself) and adds no composite
        runOnCompleteOnOwnSession(onCompleteOf(Rdr229ChunksFk.ADOPT_LABEL));
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
            try (Statement st = holder.createStatement()) {
                // the PRODUCTION leaf on_complete of every leaf, under the bounds the finisher gives a validation (no statement bound, 5 s lock wait),
                // in a transaction that stays open
                st.execute("SET LOCAL statement_timeout = 0");
                st.execute("SET LOCAL lock_timeout = '5s'");
                for (var m : leafMoves()) {
                    MoveSql.runOnComplete(holder, (String) m.get("on_complete"));
                }
                long held = ctx().fetchOne("SELECT count(*) FROM pg_catalog.pg_locks l WHERE l.pid = " + pid(holder)
                    + " AND l.locktype = 'relation' AND l.mode = 'ShareUpdateExclusiveLock' AND l.granted").get(0, Long.class);
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
                                w.execute("SET LOCAL lock_timeout = '2s'");
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
            try (Statement st = setup.createStatement()) {
                st.execute("CREATE TABLE IF NOT EXISTS nexus.p33c_dl_a (x int)");
                st.execute("CREATE TABLE IF NOT EXISTS nexus.p33c_dl_b (x int)");
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
            try (Statement st = setup.createStatement()) {
                st.execute("DROP TABLE IF EXISTS nexus.p33c_dl_a, nexus.p33c_dl_b");
            }
        }
    }

    private static void opposite(Connection c, String first, String second, AtomicInteger attempts, CyclicBarrier firstLockTaken) {
        DeadlockRetry.run("p33c-negative-control", () -> {
            int attempt = attempts.incrementAndGet();
            try (Statement st = c.createStatement()) {
                c.setAutoCommit(false);
                st.execute("LOCK TABLE nexus." + first + " IN ACCESS EXCLUSIVE MODE");
                if (attempt == 1) {
                    firstLockTaken.await(10, TimeUnit.SECONDS);   // both hold their first lock before either asks for the second
                }
                st.execute("LOCK TABLE nexus." + second + " IN ACCESS EXCLUSIVE MODE");
                c.commit();
            } catch (SQLException | InterruptedException | java.util.concurrent.TimeoutException | java.util.concurrent.BrokenBarrierException e) {
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
        ctx().execute("ALTER TABLE nexus." + quote(leaf) + " DROP CONSTRAINT " + quote(leaf + Rdr229ChunksFk.LEAF_CONSTRAINT_SUFFIX));
        String columns = difference.equals("COLUMN_ORDER") ? "(collection_id, tenant_id, embedding_model)" : "(tenant_id, collection_id, embedding_model)";
        String actions = switch (difference) {
            case "NO_ACTION" -> "ON DELETE NO ACTION";
            case "DEFERRABLE" -> "ON DELETE RESTRICT DEFERRABLE";
            default -> "ON DELETE RESTRICT";
        };
        try {
            ctx().execute("ALTER TABLE nexus." + quote(leaf) + " ADD CONSTRAINT " + quote(mismatch) + " FOREIGN KEY " + columns
                + " REFERENCES nexus.catalog_collections " + columns + " " + actions);
            resume(Rdr229ChunksFk.ADOPT_LABEL);

            runOnCompleteOnOwnSession(onCompleteOf(Rdr229ChunksFk.ADOPT_LABEL));

            assertThat(compositeOnId()).as("%s: the composite is not added over a key PostgreSQL would not adopt", difference).isEmpty();
            var own = ctx().resultQuery("SELECT convalidated FROM pg_catalog.pg_constraint WHERE conrelid = ('nexus.' || quote_ident(?))::pg_catalog.regclass "
                + "AND conname = ?", leaf, leaf + Rdr229ChunksFk.LEAF_CONSTRAINT_SUFFIX).fetch(0, Boolean.class);
            assertThat(own).as("%s: the leaf's own key was queued, NOT VALID", difference).containsExactly(false);
            assertThat(completedAt(Rdr229ChunksFk.LEAF_LABEL_PREFIX + leaf)).as("and its validation move is open").isNull();

            runUntilComposite(new VerifyingFinisher());
            assertThat(compositeOnId()).as("%s: the composite arrives once the leaf's own key validated", difference).hasSize(1);
            long parent = ctx().fetchOne("SELECT conparentid::bigint FROM pg_catalog.pg_constraint WHERE conrelid = ('nexus.' || quote_ident(?))::pg_catalog.regclass "
                + "AND conname = ?", leaf, leaf + Rdr229ChunksFk.LEAF_CONSTRAINT_SUFFIX).get(0, Long.class);
            assertThat(parent).as("%s: the matching key was adopted", difference).isNotZero();
        } finally {
            ctx().execute("ALTER TABLE nexus." + quote(leaf) + " DROP CONSTRAINT IF EXISTS " + quote(mismatch));
        }
    }

    // ── 6: the adoption move's states ───────────────────────────────────────────────────────────────────────

    /** While leaves wait, the adoption move re-opens itself: no completion event, no listener call, no retry counted, and the composite is not there. */
    @Test
    void theAdoptionMoveThatReopensItselfIsNotACompletionAndCountsNoRetry() {
        seed("reopen");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        pause(Rdr229ChunksFk.LEAF_LABEL_PREFIX + "%");
        var completions = new AtomicInteger();
        var w = productionWorker(new VerifyingFinisher());
        w.onMoveCompleted(completions::incrementAndGet);

        for (int i = 0; i < 3; i++) {
            w.runCycle();
        }

        assertThat(completions.get()).as("nothing completed: the adoption move re-opened itself each time").isZero();
        var adopt = adoptMoves().get(0);
        assertThat(adopt.get("completed_at")).as("still open").isNull();
        assertThat(adopt.get("failed_at")).isNull();
        assertThat((Integer) adopt.get("transient_retries")).as("a wait for a leaf is no failure: no retry counted").isZero();
        assertThat(compositeOnId()).isEmpty();
    }

    /** A leaf move that is failed is named by the adoption move, which fails loud (permanent) instead of waiting on it for ever. */
    @Test
    void aFailedLeafMoveFailsTheAdoptionMoveAndNamesIt() {
        seed("failedleaf");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        String failed = (String) leafMoves().get(0).get("label");
        ctx().execute("UPDATE nexus.data_moves SET failed_at = now(), failure = 'planted' WHERE label = ?", failed);
        pause(Rdr229ChunksFk.LEAF_LABEL_PREFIX + "%");

        productionWorker(new VerifyingFinisher()).runCycle();

        var adopt = adoptMoves().get(0);
        assertThat(adopt.get("failed_at")).as("the adoption move failed, loud").isNotNull();
        assertThat((String) adopt.get("failure")).as("naming the leaf move").contains(failed).contains("failed");
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

    private record Held(Connection connection, String leaf, String label) { }

    /** Pauses every move but one leaf validation, and holds ACCESS EXCLUSIVE on that leaf from a session of the test's own. */
    private Held holdOneLeaf() throws Exception {
        seed("held");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        String leaf = chunkLeaves().get(0);
        String label = Rdr229ChunksFk.LEAF_LABEL_PREFIX + leaf;
        pause("rdr229-%");
        resume(label);
        Connection c = pg.createConnection("");
        c.setAutoCommit(false);
        try (Statement st = c.createStatement()) {
            st.execute("LOCK TABLE nexus." + quote(leaf) + " IN ACCESS EXCLUSIVE MODE");
        }
        return new Held(c, leaf, label);
    }

    private static int completionPid(DataMoveWorker w, String label, long timeoutMs) {
        long end = System.nanoTime() + timeoutMs * 1_000_000L;
        while (System.nanoTime() < end) {
            var call = w.states().call(label);
            if (call.isPresent() && VerifyingFinisher.COMPLETION_RUN.equals(call.get().runLabel())) {
                return call.get().pid();
            }
            sleepMs(50);
        }
        return -1;
    }

    /**
     * The production leaf on_complete that cannot get the leaf's lock waits the 5 s lock wait of a validation, not 700 ms, then fails 55P03
     * (transient: blocked, retried). While it waits its backend is known to the status (pid and start) and is a {@code nexus-data-move-}
     * session. The leaf key stays NOT VALID.
     */
    @Test
    void aLeafValidationBehindAHeldAccessExclusiveWaitsAboutFiveSecondsThenBlocksOn55P03() throws Exception {
        Held held = holdOneLeaf();
        try (Connection holder = held.connection()) {
            var w = productionWorker(new VerifyingFinisher());
            long started = System.nanoTime();
            var cycle = CompletableFuture.runAsync(w::runCycle);
            int pid = completionPid(w, held.label(), 3_000);
            assertThat(pid).as("the completion's backend is recorded for the status while it waits").isPositive();
            String application = ctx().fetchOne("SELECT application_name FROM pg_catalog.pg_stat_activity WHERE pid = ?", pid).get(0, String.class);
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
            var w = productionWorker(new VerifyingFinisher());
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
        // a leaf created after the migration, as the service role does it (when that role exists in this fixture; the owner otherwise)
        String lateTenant = "p33c-owner-leaf-" + System.nanoTime();
        tenantsUsed.add(lateTenant);
        try (Connection c = pg.createConnection("")) {
            boolean svc = DSL.using(c, SQLDialect.POSTGRES).fetchExists(DSL.table(DSL.name("pg_catalog", "pg_roles")),
                DSL.field(DSL.name("rolname"), String.class).eq("nexus_svc"));
            if (svc) {
                try (Statement st = c.createStatement()) {
                    st.execute("SET ROLE nexus_svc");
                }
            }
            DSL.using(c, SQLDialect.POSTGRES).execute("SELECT nexus.create_tenant_partitions('nexus.chunks'::pg_catalog.regclass, ?, true)", lateTenant);
        }
        String worker = adminConnection.user();
        var owners = ctx().resultQuery("SELECT c.relname || ' (' || pg_catalog.pg_get_userbyid(c.relowner) || ')' FROM pg_catalog.pg_class c "
                + "WHERE (c.oid = 'nexus.catalog_collections'::pg_catalog.regclass OR c.oid IN (SELECT pt.relid FROM "
                + "pg_catalog.pg_partition_tree('nexus.chunks'::pg_catalog.regclass) pt WHERE pt.isleaf)) AND pg_catalog.pg_get_userbyid(c.relowner) <> ?",
            worker).fetch(0, String.class);
        assertThat(owners).as("tables not owned by %s, the role the data-move worker runs as", worker).isEmpty();
        assertThat(chunkLeaves()).as("non-vacuity: chunks has leaves").hasSizeGreaterThanOrEqualTo(2);

        runSwaps(new VerifyingFinisher());
        String leaf = chunkLeaves().get(0);
        long rows = ctx().fetchOne("SELECT count(*) FROM nexus." + quote(leaf)).get(0, Long.class);
        assertThat(rows).as("non-vacuity: the leaf has rows, so a per-row fallback would probe the registry").isPositive();
        String text = onCompleteOf(Rdr229ChunksFk.LEAF_LABEL_PREFIX + leaf);
        java.util.Properties props = new java.util.Properties();
        props.setProperty("user", adminConnection.user());
        props.setProperty("password", adminConnection.password());
        try (Connection c = java.sql.DriverManager.getConnection(adminConnection.url(), props)) {
            c.setAutoCommit(false);
            MoveSql.runOnComplete(c, text);
            var stat = DSL.using(c, SQLDialect.POSTGRES).fetchOne("SELECT coalesce(idx_scan, 0) FROM pg_catalog.pg_stat_xact_user_tables "
                + "WHERE schemaname = 'nexus' AND relname = 'catalog_collections'");
            long probes = stat == null ? 0L : stat.get(0, Long.class);
            c.rollback();
            assertThat(probes).as("the VALIDATE as %s probed the registry by index %d times for %d rows: 0 is the set-based check", worker, probes, rows)
                .isZero();
        }
    }

    private static String quote(String ident) {
        return "\"" + ident.replace("\"", "\"\"") + "\"";
    }

    private static int pid(Connection c) throws SQLException {
        try (Statement st = c.createStatement(); var rs = st.executeQuery("SELECT pg_backend_pid()")) {
            rs.next();
            return rs.getInt(1);
        }
    }
}
