// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.IdKeyIndexProbe;
import dev.nexus.service.IdKeyIndexProbe.Idx;
import dev.nexus.service.PgContainerHelper;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;

import java.sql.Connection;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.3b (nexus-p2m1x.20): the leftover re-read before a drop and the per-leaf backoff after a failed build. Fixtures:
 * {@link LeafIndexPassTestBase}.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Timeout(value = 300, unit = TimeUnit.SECONDS, threadMode = Timeout.ThreadMode.SEPARATE_THREAD)
class LeafIndexPassBackoffIntegrationTest extends LeafIndexPassTestBase {

    // -- 9: the leftover re-read, the backoff ------------------------------------------------------------------------

    /**
     * The leftover's {@code indisvalid} is read again immediately before the DROP (as {@code PciBuilderSession.Pass#drop} does): an index
     * that turned valid since the pass's first read (here, at the DROP statement) is not dropped, and the leaf is not built beside it.
     */
    @Test
    void aLeftoverThatTurnedValidBeforeTheDropIsNotDropped() throws Exception {
        String tenant = newTenant("stale");
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
            PgContainerHelper.markIndexInvalid(su, schema, leftover);
        }
        long oid = asSuperuser(su -> IdKeyIndexProbe.indexes(su, IdKeyIndexProbe.CHUNKS)).stream()
            .filter(i -> i.relation().equals(leaf)).findFirst().orElseThrow().indexOid();
        List<LeafIndexPass.Ddl> drops = new ArrayList<>();
        var pass = openPass();
        pass.beforeDdl(statement -> {
            if (statement.kind() == LeafIndexPass.Kind.DROP) {
                drops.add(statement);
                try (Connection su = pg.createConnection("")) {
                    PgContainerHelper.markIndexValid(su, schema, leftover);   // an operator's build finished after the pass's first read
                } catch (java.sql.SQLException e) {
                    throw new IllegalStateException(e);
                }
            }
        });

        var result = pass.runOnce();

        assertThat(drops).as("the pass was about to drop it").hasSize(1);
        assertThat(result.dropped()).as("the second read found it valid: no DROP was sent").isZero();
        List<Idx> after = asSuperuser(su -> IdKeyIndexProbe.indexes(su, IdKeyIndexProbe.CHUNKS)).stream().filter(i -> i.relation().equals(leaf)).toList();
        assertThat(after).as("the index is still there, once, valid, the same one").hasSize(1);
        assertThat(after.get(0).valid()).isTrue();
        assertThat(after.get(0).indexOid()).isEqualTo(oid);
    }

    /**
     * CR-S3: a leaf whose build fails (a duplicate under the id key) is not dropped and rebuilt on every tick: it waits 5 minutes after the
     * first failure, 10 after the second, 20 after the third, and the wait is in memory on a clock the test sets (no sleeps). {@code done}
     * of the boot_work entry counts the builds that succeeded only. Once the duplicate is gone the leaf builds and the table registers.
     */
    @Test
    void aLeafWhoseBuildFailedWaitsOutAnExponentialBackoff_andOnlySuccessesCountInBootWork() throws Exception {
        String tenant = newTenant("backoff");
        collection(tenant, name(tenant, "a"), 3);
        duplicateUnderTheIdKey(tenant, name(tenant, "a"), name(tenant, "dup"));
        String leaf = chunksLeaf(tenant);
        String relation = leaf.substring(leaf.indexOf('.') + 1);
        var clock = new MutableClock();
        var registry = new BootWorkRegistry(clock);
        var pass = new LeafIndexPass(admin, registry, () -> true, ENGINE_VERSION, clock);
        List<LeafIndexPass.Ddl> sent = new ArrayList<>();
        List<String> progress = new ArrayList<>();
        AtomicBoolean failedSeen = new AtomicBoolean();
        AtomicBoolean buildAfterTheFailure = new AtomicBoolean();
        AtomicInteger builds = new AtomicInteger();
        pass.beforeDdl(statement -> {
            sent.add(statement);
            if (statement.kind() == LeafIndexPass.Kind.BUILD) {
                if (failedSeen.get()) {
                    buildAfterTheFailure.set(true);
                }
                int succeeded = builds.getAndIncrement() - (failedSeen.get() ? 1 : 0);
                registry.snapshot().stream().filter(e -> LeafIndexPass.BOOT_WORK_ID.equals(e.id())).findFirst()
                    .ifPresent(e -> progress.add(e.done() + "/" + succeeded));
                if (statement.target().equals(relation)) {
                    failedSeen.set(true);
                }
            }
        });

        Runnable tick = () -> {
            builds.set(0);
            failedSeen.set(false);
            pass.runOnce();
        };
        java.util.function.Function<LeafIndexPass.Kind, Long> count = kind ->
            sent.stream().filter(q -> q.kind() == kind && (kind == LeafIndexPass.Kind.DROP
                ? q.target().equals(LeafIndexPass.leafIndexName(relation)) : q.target().equals(relation))).count();

        tick.run();   // t0: the first attempt fails
        assertThat(count.apply(LeafIndexPass.Kind.BUILD)).isEqualTo(1);
        assertThat(progress).as("the entry was read at the start of builds: the next assertion is not vacuous").isNotEmpty();
        assertThat(buildAfterTheFailure).as("the pass went on to build other leaves after the failed one (the centroids' come after the chunks')")
            .isTrue();
        assertThat(progress).as("done at the start of each build is the number of builds that succeeded before it, a failure not counted")
            .allSatisfy(p -> assertThat(p.substring(0, p.indexOf('/'))).isEqualTo(p.substring(p.indexOf('/') + 1)));

        clock.now = clock.now.plus(java.time.Duration.ofMinutes(1));
        tick.run();   // inside the first 5 minutes: not touched
        assertThat(count.apply(LeafIndexPass.Kind.BUILD)).as("no second attempt inside 5 minutes").isEqualTo(1);
        assertThat(count.apply(LeafIndexPass.Kind.DROP)).as("and no drop").isZero();

        clock.now = clock.now.plus(java.time.Duration.ofMinutes(5));   // t0 + 6
        tick.run();
        assertThat(count.apply(LeafIndexPass.Kind.DROP)).as("after 5 minutes the leftover is dropped").isEqualTo(1);
        assertThat(count.apply(LeafIndexPass.Kind.BUILD)).as("and rebuilt (and fails again)").isEqualTo(2);

        clock.now = clock.now.plus(java.time.Duration.ofMinutes(9));   // t0 + 15: the second wait is 10 minutes, from t0 + 6
        tick.run();
        assertThat(count.apply(LeafIndexPass.Kind.BUILD)).as("inside the second, doubled wait").isEqualTo(2);

        clock.now = clock.now.plus(java.time.Duration.ofMinutes(2));   // t0 + 17
        tick.run();
        assertThat(count.apply(LeafIndexPass.Kind.BUILD)).as("after it").isEqualTo(3);
        assertThat(swapLabels()).as("the duplicate holds the chunks table back").containsExactly(CENTROIDS_SWAP);

        removeDuplicate(tenant, name(tenant, "dup"));
        clock.now = clock.now.plus(java.time.Duration.ofMinutes(21));   // t0 + 38: past the third wait (20 minutes from t0 + 17)
        var last = pass.runOnce();
        assertThat(last.registered()).as("the leaf builds and the chunks swap registers").containsExactly(CHUNKS_SWAP);
        assertThat(chunksLeavesWithoutIndex()).isEmpty();
    }

    // -- 9b: a lock read that fails ----------------------------------------------------------------------------------

    private void migrationLockReadable(boolean readable) throws Exception {
        asSuperuser(su -> {
            var locks = DSL.table(DSL.name("pg_catalog", "pg_locks"));
            if (readable) {
                su.grant(DSL.privilege("SELECT")).on(locks).toPublic().execute();
            } else {
                su.revoke(DSL.privilege("SELECT")).on(locks).fromPublic().execute();
            }
            return null;
        });
    }

    /**
     * Review r2 (critic O3): the migration lock is read before every statement, and a read that FAILS (here pg_locks is made unreadable
     * to the pass's role at its first build, an error that is not a lost connection) is "unknown", never "not held": the pass ends there.
     * No statement was sent, so no leaf books a backoff: the next tick, the same object on a clock that has not moved, builds every leaf.
     */
    @Test
    void aFailedMigrationLockReadBeforeABuildEndsThePass_andBooksNoBackoff() throws Exception {
        String tenant = newTenant("lockread");
        collection(tenant, name(tenant, "a"), 3);
        var clock = new MutableClock();
        var pass = new LeafIndexPass(admin, new BootWorkRegistry(clock), () -> true, ENGINE_VERSION, clock);
        List<LeafIndexPass.Ddl> sent = new ArrayList<>();
        AtomicBoolean unreadable = new AtomicBoolean();
        pass.beforeDdl(statement -> {
            sent.add(statement);
            if (statement.kind() == LeafIndexPass.Kind.BUILD && !unreadable.getAndSet(true)) {
                try {
                    migrationLockReadable(false);
                } catch (Exception e) {
                    throw new IllegalStateException(e);
                }
            }
        });

        LeafIndexPass.Result cut;
        try {
            cut = pass.runOnce();
        } finally {
            migrationLockReadable(true);
        }

        assertThat(sent).as("the pass ended at the first build: the hook saw one statement and no later one").hasSize(1);
        assertThat(cut.built()).isZero();
        assertThat(everyIdIndex()).as("nothing was built").isEmpty();

        var next = pass.runOnce();   // same object, same clock: a booked backoff would hold every leaf out

        assertThat(next.built()).as("every leaf builds at once").isPositive();
        assertThat(chunksLeavesWithoutIndex()).isEmpty();
        assertThat(swapLabels()).containsExactly(CHUNKS_SWAP, CENTROIDS_SWAP);
    }
}
