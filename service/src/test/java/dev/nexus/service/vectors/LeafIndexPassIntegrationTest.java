// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.IdKeyIndexProbe;
import dev.nexus.service.IdKeyIndexProbe.Idx;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.PciBuilders;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.datamove.CompletionCheck;
import dev.nexus.service.vectors.PciBuilderSession.BuilderState;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;

import java.sql.Connection;
import java.time.Clock;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.stream.Collectors;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;


/**
 * RDR-229 P3.3b (nexus-p2m1x.20), the failing tests of the per-leaf unique-index pass: the route, the registration and the migration walk's view of it (fixtures: {@link LeafIndexPassTestBase}; resume and a finished store: {@link LeafIndexPassResumeIntegrationTest}; the backoff: {@link LeafIndexPassBackoffIntegrationTest}) ({@link LeafIndexPass}), against the real
 * two-level partitioned layout in the shape {@link PciReconcilerIntegrationTest} uses: a database migrated by the non-superuser
 * owner (production's {@code nexus_admin}), the pass connecting as the owner. The pass builds the id unique indexes of
 * {@code nexus.chunks} and {@code nexus.taxonomy_centroids} and, last, registers each table's swap move
 * ({@link LeafIndexPass#swapLabel}).
 *
 * <p><b>Gate 1.</b> Every test but {@link #withMovesBlockedThePassBuildsNoIndexAndRegistersNoSwap} opens the gate with a lambda
 * that returns true: the gate is the completion check's ({@code CompletionCheckIntegrationTest} and its siblings test it) and
 * this class tests what the pass does when it holds. The blocked test reads the REAL gate over a store whose stamp moves are
 * registered and cannot run.
 *
 * <p><b>The key columns.</b> P3.3a (nexus-p2m1x.19) is not on this branch. The id key's columns are not among what it adds
 * ({@code collection_id} is on both tables since P3.1, {@code vectors-068}; P3.3a adds the seven foreign-key twins and their
 * validation moves), so no fixture here adds a column. What the pass waits for from P3.3a is its labels in the gate's read
 * set, which a lambda stands in for.
 *
 * <p><b>Hermetic.</b> Every test starts and ends with no id unique index anywhere in the two trees
 * ({@link IdKeyIndexProbe#reset}): a constraint left on a parent would make PostgreSQL itself index each later leaf.
 */
@Tag("integration")
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Timeout(value = 300, unit = TimeUnit.SECONDS, threadMode = Timeout.ThreadMode.SEPARATE_THREAD)
class LeafIndexPassIntegrationTest extends LeafIndexPassTestBase {

    // -- 1 ---------------------------------------------------------------------------------------------------------

    /**
     * A pass ended mid-build leaves an INVALID leaf index; the next pass drops it and rebuilds it (RDR-229 P3.3b test; T2
     * nexus_rdr/229-research-33, the pattern of {@code PciBootWork.ON_STOP}). The leftover is an index named as the pass names its
     * leaf index, marked invalid the way {@code PgContainerHelper.markIndexInvalid} does for the other interrupted-build tests (the
     * test tree holds no raw SQL, and {@code CREATE INDEX CONCURRENTLY} has no typed form; the failing concurrent build itself is
     * what test 2 does through the pass). What the next pass must do is the drop and the rebuild, not build beside it: one unique id
     * index on the leaf, valid, and not the leftover's oid.
     */
    @Test
    void aPassEndedMidBuildLeavesAnInvalidIndexAndTheNextPassDropsAndRebuildsIt() throws Exception {
        String tenant = newTenant("midbuild");
        collection(tenant, name(tenant, "a"), 3);
        String leaf = chunksLeaf(tenant);
        String schema = leaf.substring(0, leaf.indexOf('.'));
        String relation = leaf.substring(leaf.indexOf('.') + 1);
        String leftover = LeafIndexPass.leafIndexName(relation);
        asSuperuser(su -> {
            var cols = java.util.Arrays.stream(IdKeyIndexProbe.CHUNKS.columns().split(", ")).map(c -> DSL.field(DSL.name(c))).toList();
            su.createUniqueIndex(DSL.name(leftover)).on(DSL.table(DSL.name(schema, relation)), cols).execute();
            return null;
        });
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.markIndexInvalid(su, schema, leftover);   // what a build ended mid-way leaves behind
        }
        List<Idx> before = asSuperuser(su -> IdKeyIndexProbe.indexes(su, IdKeyIndexProbe.CHUNKS)).stream()
            .filter(i -> i.relation().equals(leaf)).toList();
        assertThat(before).as("precondition: one INVALID index on the leaf, under the pass's name").hasSize(1);
        assertThat(before.get(0).valid()).isFalse();
        long leftoverOid = before.get(0).indexOid();

        var result = openPass().runOnce();

        List<Idx> after = asSuperuser(su -> IdKeyIndexProbe.indexes(su, IdKeyIndexProbe.CHUNKS)).stream()
            .filter(i -> i.relation().equals(leaf)).toList();
        assertThat(result.dropped()).as("the pass dropped the leftover").isEqualTo(1);
        assertThat(after).as("one unique id index on the leaf: the leftover dropped, not built beside").hasSize(1);
        assertThat(after.get(0).valid()).as("and it is valid").isTrue();
        assertThat(after.get(0).indexOid()).as("and it is a rebuild, not the leftover").isNotEqualTo(leftoverOid);
        assertThat(asSuperuser(su -> IdKeyIndexProbe.indexes(su, IdKeyIndexProbe.CHUNKS)).stream().filter(i -> !i.valid()).toList())
            .as("no INVALID id index is left anywhere in the tree").isEmpty();
    }

    // -- 2 ---------------------------------------------------------------------------------------------------------

    /**
     * The swap is never registered while any leaf's unique index is missing or INVALID. One chunks leaf cannot be indexed (a
     * duplicate under the id key), so the chunks table has an INVALID leaf and no swap row, however many passes run; the
     * centroids, whose leaves all index, do get theirs (so the test is not a pass that registers nothing at all). Removing the
     * duplicate lets the next pass finish the chunks and register it. The swap is per table: a table's swap waits for its own
     * leaves only.
     */
    @Test
    void theSwapIsNeverRegisteredWhileAnyLeafHasNoValidUniqueIndex() throws Exception {
        String tenant = newTenant("noswap");
        collection(tenant, name(tenant, "a"), 3);
        duplicateUnderTheIdKey(tenant, name(tenant, "a"), name(tenant, "dup"));
        String leaf = chunksLeaf(tenant);

        var first = openPass().runOnce();
        var second = openPass().runOnce();

        assertThat(chunksLeavesWithoutIndex())
            .as("the duplicate keeps exactly its own leaf without a valid index; every other chunks leaf has one")
            .containsExactly(leaf);
        assertThat(swapLabels()).as("chunks: no swap while a leaf has no valid index (two passes); centroids: registered")
            .containsExactly(CENTROIDS_SWAP);
        assertThat(first.registered()).containsExactly(CENTROIDS_SWAP);
        assertThat(second.registered()).as("and the second pass registers nothing more").isEmpty();

        removeDuplicate(tenant, name(tenant, "dup"));
        var third = openPass().runOnce();

        assertThat(chunksLeavesWithoutIndex()).isEmpty();
        assertThat(third.registered()).as("the pass that finishes the last leaf registers the chunks swap").containsExactly(CHUNKS_SWAP);
        assertThat(swapLabels()).containsExactly(CHUNKS_SWAP, CENTROIDS_SWAP);
    }

    // -- 2b --------------------------------------------------------------------------------------------------------

    /**
     * Linda's seam ruling 2026-11-22, item 1: the pass adds a table's PARENT id constraint only once every leaf of that table has
     * a valid index. One chunks leaf cannot be indexed, so the chunks parent has no id constraint (and no index of any kind at the
     * parent) after the pass, while the centroids, whose leaves all index, have theirs: the second assertion is what shows the pass
     * ran and made the parent constraint for the table that was ready. Once the duplicate is gone the next pass adds the chunks one.
     */
    @Test
    void theParentConstraintIsNotAddedWhileAnyLeafOfThatTableLacksAValidIndex() throws Exception {
        String tenant = newTenant("noparent");
        collection(tenant, name(tenant, "a"), 3);
        duplicateUnderTheIdKey(tenant, name(tenant, "a"), name(tenant, "dup"));

        openPass().runOnce();

        List<Idx> centroidsParent = parentIndexes(IdKeyIndexProbe.CENTROIDS);
        assertThat(centroidsParent).as("centroids: every leaf valid, so the parent constraint is there").hasSize(1);
        assertThat(centroidsParent.get(0).constraint()).isNotNull();
        assertThat(parentIndexes(IdKeyIndexProbe.CHUNKS)).as("chunks: a leaf has no valid index, so no parent constraint and no parent index")
            .isEmpty();

        removeDuplicate(tenant, name(tenant, "dup"));
        openPass().runOnce();

        List<Idx> chunksParent = parentIndexes(IdKeyIndexProbe.CHUNKS);
        assertThat(chunksParent).as("chunks: the pass that finds every leaf valid adds the parent constraint").hasSize(1);
        assertThat(chunksParent.get(0).constraint()).isNotNull();
        assertThat(chunksParent.get(0).valid()).isTrue();
    }

    // -- 3 ---------------------------------------------------------------------------------------------------------

    /**
     * Gate 1 governs (residual S-5, nexus-p2m1x.43): on an install whose moves are blocked (BYO or --no-pg-bundle PostgreSQL with
     * no pg_background) the stamp never completes, so the pass starts nothing: no index, no swap. This store is such an install:
     * the migration registered the stamp moves and nothing can run them, so the REAL gate does not hold. A pass on that gate
     * builds no index and registers no swap. The same pass on an open gate then builds and registers on the same store, which is
     * what makes the first half mean "the gate held it back" and not "the pass cannot build here".
     */
    @Test
    void withMovesBlockedThePassBuildsNoIndexAndRegistersNoSwap() throws Exception {
        String tenant = newTenant("blocked");
        collection(tenant, name(tenant, "a"), 3);
        assertThat(stampMoves(false)).as("precondition: the stamp moves are registered").isPositive();
        assertThat(stampMoves(true)).as("precondition: no stamp move can run here, so none completed").isZero();
    LeafIndexPass.Gate1 real = () -> {
            try {
                return asOwner(ctx -> CompletionCheck.evaluate(ctx).gate1());
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        };
        Boolean realGate = real.holds();
        assertThat(realGate).as("precondition: the real gate 1 does not hold").isFalse();

        var blocked = pass(real).runOnce();

        assertThat(blocked.gateHeld()).isFalse();
        assertThat(blocked.built()).isZero();
        assertThat(everyIdIndex())
            .as("no index of any kind on either table's tree").isEmpty();
        assertThat(swapLabels()).as("no swap registered").isEmpty();

        var open = openPass().runOnce();   // control: the same store, the gate open

        assertThat(open.gateHeld()).isTrue();
        assertThat(chunksLeavesWithoutIndex())
            .as("with the gate open the pass builds on this very store").isEmpty();
        assertThat(swapLabels()).containsExactly(CHUNKS_SWAP, CENTROIDS_SWAP);
    }

    // -- 5 ---------------------------------------------------------------------------------------------------------

    /**
     * Registration is idempotent: no second insert when the swap label row exists. Two passes leave one row each and the second
     * reports no registration. A row that is already there (here a hand row with its own changeset_id, as a restored or
     * hand-registered store could carry) is neither inserted over nor replaced, and the pass does not fail on it.
     */
    @Test
    void registrationIsIdempotentAndNeverInsertsASecondRowForALabel() throws Exception {
        String tenant = newTenant("idem");
        collection(tenant, name(tenant, "a"), 3);

        var first = openPass().runOnce();
        var registeredAt = swapRows().stream().collect(Collectors.toMap(r -> (String) r.get("label"), r -> r.get("registered_at")));
        var second = openPass().runOnce();

        assertThat(first.registered()).containsExactlyInAnyOrder(CHUNKS_SWAP, CENTROIDS_SWAP);
        assertThat(second.registered()).as("nothing to register the second time").isEmpty();
        var rows = swapRows();
        assertThat(rows).as("one row per label").hasSize(2);
        assertThat(rows.stream().collect(Collectors.toMap(r -> (String) r.get("label"), r -> r.get("registered_at"))))
            .as("the rows are the first pass's, untouched").isEqualTo(registeredAt);

        asSuperuser(su -> su.deleteFrom(MOVES).where(LABEL.eq(CHUNKS_SWAP)).execute());
        asSuperuser(su -> su.insertInto(MOVES, LABEL, DSL.field(DSL.name("template")), DSL.field(DSL.name("driving_schema")),
                DSL.field(DSL.name("driving_table")), DSL.field(DSL.name("chunk_size")), DSL.field(DSL.name("sleep_ms")),
                DSL.field(DSL.name("statement_timeout_ms")), DSL.field(DSL.name("verification")), DSL.field(DSL.name("changeset_id")))
            .values(CHUNKS_SWAP, "hand", "nexus", "chunks", 1, 0, 1000, "hand", "hand-registered").execute());
        var third = openPass().runOnce();

        assertThat(third.registered()).as("the label has a row: no insert").isEmpty();
        assertThat(swapRows().stream().filter(r -> CHUNKS_SWAP.equals(r.get("label"))).map(r -> r.get("changeset_id")).toList())
            .as("the existing row is not replaced").containsExactly("hand-registered");
    }

    // -- 6 ---------------------------------------------------------------------------------------------------------

    /**
     * With NX_SEARCH_PCI=0 the pass builds the indexes on every leaf and both partition levels and registers the swap (S-3 points
     * 1 and 2, nexus-p2m1x.41). The reconciler is built with PCI off, as NX_SEARCH_PCI=0 makes it, and does nothing; the pass,
     * which takes no PciSettings, does the S-3 route on two tenants: per leaf a valid unique index backing a UNIQUE constraint,
     * attached to its model partition's, which is attached to the parent's, each level valid. The layout must have two model
     * partitions at least, or the model level is not exercised. The registered rows name the table, carry the run-time
     * changeset_id {@value LeafIndexPass#CHANGESET_PREFIX}{@code <engine version>} (S-3 point 3, rule 2: the engine inserts the row,
     * not another move's on_complete; the row's own on_complete is {@link Rdr229SwapMove#spec}'s) and a template the data-move registry accepts ({@code <driving_table>} and
     * {@code <chunking_clause>} once each, assert_chunking_template.sql).
     */
    @Test
    void withPciOffThePassBuildsEveryLeafAndBothPartitionLevelsAndRegistersTheSwap() throws Exception {
        String one = newTenant("pcioff-1");
        String two = newTenant("pcioff-2");
        collection(one, name(one, "a"), 3);
        collection(two, name(two, "a"), 3);
        PciSettings off = new PciSettings(false, 200, 600, 16);
        var reconciler = new PciReconciler(new PciCatalog(svcDs),
            new PciBuilderSession(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS, NONCE, off), PciIndexSweep.create(svcDs, off), off,
            Clock.systemUTC(), PciReconciler.SET_LOCAL_TENANT, PciReconciler.COUNT_TIMEOUT, Duration.ofSeconds(60));
        var pciReport = reconciler.reconcileOnce();
        assertThat(pciReport.state()).as("precondition: PCI is off").isEqualTo(BuilderState.OFF);
        assertThat(pciReport.builds()).isZero();

        var result = openPass().runOnce();

        assertThat(result.gateHeld()).isTrue();
        for (var key : IdKeyIndexProbe.KEYS) {
            List<Idx> all = asSuperuser(su -> IdKeyIndexProbe.indexes(su, key));
            List<String> leaves = asSuperuser(su -> IdKeyIndexProbe.leaves(su, key));
            List<String> models = asSuperuser(su -> IdKeyIndexProbe.modelPartitions(su, key));
            assertThat(models).as("%s: two model partitions at least, so the model level is a level", key.table()).hasSizeGreaterThanOrEqualTo(2);

            List<Idx> top = all.stream().filter(i -> i.level() == 0).toList();
            List<Idx> mid = all.stream().filter(i -> i.level() == 1).toList();
            List<Idx> bottom = all.stream().filter(Idx::leaf).toList();
            assertThat(top).as("%s: the parent has its unique index", key.table()).hasSize(1);
            assertThat(mid.stream().map(Idx::relation).toList()).as("%s: every model partition has one", key.table())
                .containsExactlyInAnyOrderElementsOf(models);
            assertThat(bottom.stream().map(Idx::relation).toList()).as("%s: every leaf has one", key.table())
                .containsExactlyInAnyOrderElementsOf(leaves);
            assertThat(all).as("%s: every one valid", key.table()).allMatch(Idx::valid);
            assertThat(all).as("%s: every one backs a UNIQUE constraint (ADD ... USING INDEX at the leaf, ONLY above)", key.table())
                .allMatch(i -> i.constraint() != null);
            assertThat(mid).as("%s: each model index is attached to the parent's", key.table())
                .allMatch(i -> top.get(0).index().equals(i.parentIndex()));
            List<String> modelIndexes = mid.stream().map(Idx::index).toList();
            assertThat(bottom).as("%s: each leaf index is attached to a model partition's", key.table())
                .allMatch(i -> modelIndexes.contains(i.parentIndex()));
        }

        assertThat(result.registered()).containsExactlyInAnyOrder(CHUNKS_SWAP, CENTROIDS_SWAP);
        for (var row : swapRows()) {
            String table = ((String) row.get("label")).substring("rdr229-swap-".length());
            assertThat(row.get("driving_schema")).isEqualTo("nexus");
            assertThat(row.get("driving_table")).isEqualTo(table);
            assertThat(row.get("changeset_id")).as("the run-time changeset_id convention, rule 2")
                .isEqualTo(LeafIndexPass.CHANGESET_PREFIX + ENGINE_VERSION);
            assertThat(row.get("completed_at")).as("registered open, for the worker").isNull();
            String template = (String) row.get("template");
            assertThat(template.split("<driving_table>", -1)).as("<driving_table> exactly once").hasSize(2);
            assertThat(template.split("<chunking_clause>", -1)).as("<chunking_clause> exactly once").hasSize(2);
            assertThat((String) row.get("verification")).isNotBlank();
        }
    }

    // -- 7: visible to the migration walk ----------------------------------------------------------------------------

    /** The pass's backend carries the builders' {@code application_name} prefix, the one the walk's termination and the on_complete's match. */
    @Test
    void thePassNamesItsBackendLikeAPciBuilder_soTheWalkAndTheOnCompleteEndIt() {
        assertThat(LeafIndexPass.APPLICATION_NAME).startsWith(PciBuilderSession.APPLICATION_NAME_PREFIX);
    }

    /**
     * CR-S1: with the migration advisory lock held by another session, a pass sends no statement and builds nothing, whether the lock is
     * held when the pass starts or is taken between two of its statements (the hook takes it at the first statement the pass is about
     * to send); released, the next pass does the whole job.
     */
    @Test
    void withTheMigrationLockHeld_theRunSendsNoDdl_atTheStartAndBetweenStatements() throws Exception {
        String tenant = newTenant("migration");
        collection(tenant, name(tenant, "a"), 3);
        List<LeafIndexPass.Ddl> sent = new ArrayList<>();

        try (HeldLock migration = new HeldLock(SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY)) {
            var pass = openPass();
            pass.beforeDdl(sent::add);
            var result = pass.runOnce();
            assertThat(result.gateHeld()).as("a migration walks: the pass stands down").isFalse();
            assertThat(result.built()).isZero();
            assertThat(sent).as("no statement was even prepared to send").isEmpty();
            assertThat(everyIdIndex()).isEmpty();
            assertThat(swapLabels()).isEmpty();
        }

        HeldLock[] late = new HeldLock[1];
        try {
            var pass = openPass();
            pass.beforeDdl(statement -> {
                if (late[0] == null) {
                    try {
                        late[0] = new HeldLock(SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY);   // the walk starts after the pass did
                    } catch (Exception e) {
                        throw new IllegalStateException(e);
                    }
                }
                sent.add(statement);
            });
            var cut = pass.runOnce();
            assertThat(late[0]).as("the hook ran: the pass was about to send its first statement").isNotNull();
            assertThat(cut.built()).as("and the statement was not sent").isZero();
            assertThat(sent).as("the hook saw one statement and the pass ended there").hasSize(1);
            assertThat(everyIdIndex()).as("no index, valid or not").isEmpty();
            assertThat(swapLabels()).isEmpty();
        } finally {
            if (late[0] != null) {
                late[0].close();
            }
        }

        var after = openPass().runOnce();
        assertThat(after.gateHeld()).isTrue();
        assertThat(chunksLeavesWithoutIndex()).isEmpty();
        assertThat(swapLabels()).containsExactly(CHUNKS_SWAP, CENTROIDS_SWAP);
    }

    /** CR-S5: a second session holding the pass's own lock (another engine's pass) makes {@code runOnce} send nothing. */
    @Test
    void aSecondSessionHoldingThePassLockMakesRunOnceSendNothing() throws Exception {
        String tenant = newTenant("peer");
        collection(tenant, name(tenant, "a"), 3);
        List<LeafIndexPass.Ddl> sent = new ArrayList<>();
        try (HeldLock peer = new HeldLock(LeafIndexPass.LOCK_KEY)) {
            var pass = openPass();
            pass.beforeDdl(sent::add);
            var result = pass.runOnce();
            assertThat(result.gateHeld()).as("standby: not a pass").isFalse();
            assertThat(result.built() + result.dropped()).isZero();
            assertThat(result.registered()).isEmpty();
            assertThat(sent).isEmpty();
            assertThat(everyIdIndex()).isEmpty();
            assertThat(swapLabels()).isEmpty();
        }
        assertThat(openPass().runOnce().registered()).as("the peer gone, the pass runs").containsExactlyInAnyOrder(CHUNKS_SWAP, CENTROIDS_SWAP);
    }

    /**
     * Linda's requirement (b), and the walk's termination finding the pass: a build in flight is held on a lock of the test's, the pass's
     * backend is ended with the one call the migration walk and the on_complete make ({@code PciBuilders.terminate}; it finds the pass by
     * the builders' name prefix), the CREATE INDEX CONCURRENTLY dies leaving an INVALID index and the pass ends without throwing; the next
     * pass drops that index and rebuilds, and no INVALID index remains.
     *
     * <p>The next pass is the SAME object on a clock that never moves, and the pass that was killed sent nothing after its build: a lost
     * connection ends the pass ({@code endIfConnectionLost}) and is not a failed build, which would put the leaf in a five-minute backoff
     * and leave the leftover where it is. The next tick must rebuild at once.
     */
    @Test
    void aBuildKilledByTheWalksTerminationLeavesAnInvalidIndexTheNextPassDropsAndRebuilds() throws Exception {
        String tenant = newTenant("killed");
        collection(tenant, name(tenant, "a"), 3);
        var clock = new MutableClock();
        var pass = new LeafIndexPass(admin, new BootWorkRegistry(clock), () -> true, ENGINE_VERSION, clock);
        List<LeafIndexPass.Ddl> sent = new java.util.concurrent.CopyOnWriteArrayList<>();
        pass.beforeDdl(sent::add);
        long leftoverOid;
        try (Connection blocker = pg.createConnection("")) {
            blocker.setAutoCommit(false);
            try {
                // a write in flight on the tenant's rows: its ROW EXCLUSIVE lock on every leaf of the tenant makes a CREATE INDEX
                // CONCURRENTLY there wait (after it has made its INVALID catalog entry) for the transaction to end
                DSL.using(blocker, SQLDialect.POSTGRES).update(CHUNKS).set(CHUNKS.CHUNK_TEXT, "held").where(CHUNKS.TENANT_ID.eq(tenant)).execute();
                CompletableFuture<LeafIndexPass.Result> running = CompletableFuture.supplyAsync(pass::runOnce);
                long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(120);
                List<Idx> invalid = List.of();
                while (invalid.isEmpty() && System.nanoTime() < deadline) {
                    Thread.sleep(100);
                    invalid = asSuperuser(su -> IdKeyIndexProbe.indexes(su, IdKeyIndexProbe.CHUNKS)).stream().filter(i -> !i.valid()).toList();
                }
                assertThat(invalid).as("a build is in flight: its index is INVALID and the pass waits on the test's lock").isNotEmpty();
                leftoverOid = invalid.get(0).indexOid();

                int ended = asSuperuser(su -> PciBuilders.terminate(su, "reason=test_walk", org.slf4j.LoggerFactory.getLogger("leaf-index-test")));
                assertThat(ended).as("the walk's termination found the pass's backend by its name").isEqualTo(1);
                running.get(60, TimeUnit.SECONDS);   // the pass ends; it does not throw into its caller
            } finally {
                blocker.rollback();
            }
        }
        List<Idx> killed = asSuperuser(su -> IdKeyIndexProbe.indexes(su, IdKeyIndexProbe.CHUNKS)).stream().filter(i -> !i.valid()).toList();
        assertThat(killed).as("the killed build left its INVALID index").isNotEmpty();
        assertThat(killed.stream().map(Idx::indexOid)).contains(leftoverOid);
        assertThat(swapLabels()).as("and the table is not registered").isEmpty();
        assertThat(sent).as("the killed pass ended at its build: the hook saw no statement after it").isNotEmpty();
        assertThat(sent.get(sent.size() - 1).kind()).isEqualTo(LeafIndexPass.Kind.BUILD);
        int sentByTheKilledPass = sent.size();

        var next = pass.runOnce();   // the same object, the same clock: no backoff was booked

        assertThat(next.dropped()).as("the next pass drops the leftover").isGreaterThanOrEqualTo(1);
        assertThat(asSuperuser(su -> IdKeyIndexProbe.indexes(su, IdKeyIndexProbe.CHUNKS)).stream().map(Idx::indexOid))
            .as("and rebuilds: the leftover's oid is gone").doesNotContain(leftoverOid);
        assertThat(asSuperuser(su -> IdKeyIndexProbe.indexes(su, IdKeyIndexProbe.CHUNKS)).stream().filter(i -> !i.valid()).toList())
            .as("no INVALID id index is left").isEmpty();
        assertThat(swapLabels()).containsExactly(CHUNKS_SWAP, CENTROIDS_SWAP);
        assertThat(sent.subList(sentByTheKilledPass, sent.size())).as("the next tick dropped the leftover and rebuilt its leaf at once")
            .anyMatch(q -> q.kind() == LeafIndexPass.Kind.DROP).anyMatch(q -> q.kind() == LeafIndexPass.Kind.BUILD);
    }
}
