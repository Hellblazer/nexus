// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.IdKeyIndexProbe;
import dev.nexus.service.IdKeyIndexProbe.Idx;
import dev.nexus.service.PgContainerHelper;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;

import java.sql.Connection;
import java.time.Clock;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeUnit;
import java.util.stream.Collectors;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.3b (nexus-p2m1x.20): the pass's {@code boot_work} entry, what a pass cut at each step of the route leaves and how the next
 * pass resumes, and what a pass over a finished store does (nothing). Fixtures: {@link LeafIndexPassTestBase}.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Timeout(value = 300, unit = TimeUnit.SECONDS, threadMode = Timeout.ThreadMode.SEPARATE_THREAD)
class LeafIndexPassResumeIntegrationTest extends LeafIndexPassTestBase {

    // -- 8: boot_work, resume, a finished table ----------------------------------------------------------------------

    /** {@code boot_work}: the entry is present while the pass builds, its {@code done} counts the indexes built so far of a constant {@code total}, and it is gone after. */
    @Test
    void theBootWorkEntryCountsTheBuildsWhileRunning_andIsGoneAfter() throws Exception {
        String tenant = newTenant("bootwork");
        collection(tenant, name(tenant, "a"), 3);
        var registry = new BootWorkRegistry(Clock.systemUTC());
        var pass = new LeafIndexPass(admin, registry, () -> true, ENGINE_VERSION);
        List<String> seen = new ArrayList<>();
        pass.beforeDdl(statement -> {
            if (statement.kind() == LeafIndexPass.Kind.BUILD) {
                registry.snapshot().stream().filter(e -> LeafIndexPass.BOOT_WORK_ID.equals(e.id())).findFirst()
                    .ifPresentOrElse(e -> seen.add(e.done() + "/" + e.total()), () -> seen.add("absent"));
            }
        });
        assertThat(registry.snapshot()).as("none before").noneMatch(e -> LeafIndexPass.BOOT_WORK_ID.equals(e.id()));

        var result = pass.runOnce();

        int total = result.built();
        assertThat(total).as("every leaf of both tables had a build").isPositive();
        assertThat(seen).as("at the start of build i the entry says i of total")
            .containsExactlyElementsOf(java.util.stream.IntStream.range(0, total).mapToObj(i -> i + "/" + total).toList());
        assertThat(registry.snapshot()).as("gone once the pass ends").noneMatch(e -> LeafIndexPass.BOOT_WORK_ID.equals(e.id()));
    }

    /**
     * A pass cut at a chosen statement leaves the state before it, and the next pass resumes from there, completes the chain and registers
     * the swaps once. Three cuts: after the parent's ONLY constraint and before any model partition's; after a leaf's constraint and
     * before its attach; after every model partition's constraint and before the first model attach. In each state {@code chainComplete}
     * is false and no swap is registered (an unattached model index, a missing leaf constraint, a missing model constraint: no swap).
     */
    @Test
    void aPassCutAtEachStepOfTheRouteResumes_chainCompleteIsFalseUntilTheChainIsWhole_andTheSwapsRegisterOnce() throws Exception {
        String tenant = newTenant("resume");
        collection(tenant, name(tenant, "a"), 3);
        var chunks = LeafIndexPass.KEYS.get(0);
        var centroids = LeafIndexPass.KEYS.get(1);
        // the first statement that is not sent: the first model partition's constraint, the first leaf's attach, the first model's attach
        var stops = List.<java.util.function.Predicate<LeafIndexPass.Ddl>>of(
            st -> st.kind() == LeafIndexPass.Kind.PARENT_CONSTRAINT && st.target().startsWith("chunks_m"),
            st -> st.kind() == LeafIndexPass.Kind.ATTACH && st.target().startsWith("chunks_m"),
            st -> st.kind() == LeafIndexPass.Kind.ATTACH && st.target().equals("chunks_cid_uq"));
        for (int stop = 0; stop < stops.size(); stop++) {
            var at = stops.get(stop);
            int cutAt = stop;
            clean();
            var cut = openPass();
            cut.beforeDdl(st -> {
                if (at.test(st)) {
                    throw new StopHere();
                }
            });
            assertThatThrownBy(cut::runOnce).as("cut %s", cutAt).isInstanceOf(StopHere.class);
            assertThat(whole(chunks)).as("cut %s: the chain is not whole", cutAt).isFalse();
            assertThat(swapLabels()).as("cut %s: nothing registered", cutAt).isEmpty();

            var resumed = openPass().runOnce();

            assertThat(whole(chunks, centroids))
                .as("resumed from cut %s: both chains whole", cutAt).isTrue();
            assertThat(resumed.registered()).as("resumed from cut %s", cutAt).containsExactlyInAnyOrder(CHUNKS_SWAP, CENTROIDS_SWAP);
            assertThat(everyIdIndex()).as("resumed from cut %s: every index valid, and backing a constraint", cutAt)
                .allMatch(Idx::valid).allMatch(i -> i.constraint() != null);
            assertThat(swapRows()).as("one row per label").hasSize(2);
        }
    }

    /**
     * M3, the helper window: the parent has its id constraint and a model partition does not yet, so a leaf made now has no index (the
     * helper stands down for a constrained parent; the model has nothing to clone). The next pass builds that leaf's index and adopts it:
     * every leaf ends with one valid, constraint-backed, attached index, and the swap registers.
     */
    @Test
    void aLeafMadeWhileTheParentIsConstrainedAndTheModelIsNot_getsItsIndexFromTheNextPass() throws Exception {
        String old = newTenant("window-old");
        collection(old, name(old, "a"), 3);
        var cut = openPass();
        cut.beforeDdl(st -> {
            if (st.kind() == LeafIndexPass.Kind.PARENT_CONSTRAINT && st.target().startsWith("chunks_m")) {
                throw new StopHere();
            }
        });
        assertThatThrownBy(cut::runOnce).isInstanceOf(StopHere.class);
        assertThat(parentIndexes(IdKeyIndexProbe.CHUNKS)).as("the parent has its constraint").hasSize(1);

        String fresh = newTenant("window-new", false);
        collection(fresh, name(fresh, "a"), 2);
        String leaf = chunksLeaf(fresh);
        assertThat(chunksLeavesWithoutIndex()).as("the new leaf has no index: the window").contains(leaf);

        var result = openPass().runOnce();

        assertThat(chunksLeavesWithoutIndex()).isEmpty();
        assertThat(result.built()).as("the pass built the new leaf's index").isGreaterThanOrEqualTo(1);
        assertThat(asSuperuser(su -> IdKeyIndexProbe.indexes(su, IdKeyIndexProbe.CHUNKS)).stream().filter(i -> i.relation().equals(leaf)).toList())
            .as("exactly one index on the leaf: valid, constraint-backed, attached").hasSize(1)
            .allMatch(Idx::valid).allMatch(i -> i.constraint() != null).allMatch(i -> i.parentIndex() != null);
        assertThat(whole(LeafIndexPass.KEYS.get(0))).isTrue();
        assertThat(swapLabels()).containsExactly(CHUNKS_SWAP, CENTROIDS_SWAP);
    }

    /** A second pass over a finished store does nothing: no statement, nothing built or dropped or registered, and the same index oids. */
    @Test
    void aSecondPassOverAFinishedStoreSendsNothing() throws Exception {
        String tenant = newTenant("twice");
        collection(tenant, name(tenant, "a"), 3);
        var first = openPass().runOnce();
        assertThat(first.registered()).containsExactlyInAnyOrder(CHUNKS_SWAP, CENTROIDS_SWAP);
        List<Long> oids = everyIdIndex().stream().map(Idx::indexOid).sorted().toList();
        var registeredAt = swapRows().stream().collect(Collectors.toMap(r -> (String) r.get("label"), r -> r.get("registered_at")));
        List<LeafIndexPass.Ddl> sent = new ArrayList<>();
        var pass = openPass();
        pass.beforeDdl(sent::add);

        var second = pass.runOnce();

        assertThat(second.gateHeld()).isTrue();
        assertThat(second.built()).isZero();
        assertThat(second.dropped()).isZero();
        assertThat(second.registered()).isEmpty();
        assertThat(sent).as("no statement").isEmpty();
        assertThat(everyIdIndex().stream().map(Idx::indexOid).sorted().toList()).as("the same indexes, not rebuilt").isEqualTo(oids);
        assertThat(swapRows().stream().collect(Collectors.toMap(r -> (String) r.get("label"), r -> r.get("registered_at"))))
            .isEqualTo(registeredAt);
    }

    // -- 10: the chain's guards, a finished pass ---------------------------------------------------------------------

    /**
     * {@code chainComplete}'s validity guards, isolated (review r2, O1): with every leaf constraint-backed, valid and attached, an
     * INVALID index at the model level, or at the parent, still makes the chain incomplete, and no swap is registered for the table while
     * it is (the swap rows are removed first, so a pass that wrongly found the chain whole would register them again). Made valid again,
     * the next pass registers it.
     */
    @Test
    void anInvalidModelOrParentIndexMakesTheChainIncomplete_andTheSwapWaitsForIt() throws Exception {
        String tenant = newTenant("invalidlevel");
        collection(tenant, name(tenant, "a"), 3);
        var chunks = LeafIndexPass.KEYS.get(0);
        openPass().runOnce();
        assertThat(whole(chunks)).as("precondition: the pass finished the table").isTrue();

        for (int level = 0; level <= 1; level++) {   // 0 the parent, 1 a model partition
            int at = level;
            Idx upper = asSuperuser(su -> IdKeyIndexProbe.indexes(su, IdKeyIndexProbe.CHUNKS)).stream()
                .filter(i -> i.level() == at).findFirst().orElseThrow();
            String schema = upper.index().substring(0, upper.index().indexOf('.'));
            String index = upper.index().substring(upper.index().indexOf('.') + 1);
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.markIndexInvalid(su, schema, index);
            }
            asSuperuser(su -> su.deleteFrom(MOVES).where(LABEL.like("rdr229-swap-%")).execute());
            assertThat(asSuperuser(su -> IdKeyIndexProbe.indexes(su, IdKeyIndexProbe.CHUNKS)).stream().filter(i -> i.level() == at))
                .as("level %s: the index is INVALID and every leaf is still valid and attached", at).anyMatch(i -> !i.valid());
            assertThat(chunksLeavesWithoutIndex()).isEmpty();
            assertThat(whole(chunks)).as("level %s invalid: the chain is not complete", at).isFalse();

            var waiting = openPass().runOnce();

            assertThat(waiting.registered()).as("level %s invalid: no swap for chunks", at).doesNotContain(CHUNKS_SWAP);
            assertThat(swapLabels()).as("and the centroids, whose chain is whole, register").containsExactly(CENTROIDS_SWAP);

            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.markIndexValid(su, schema, index);
            }
            assertThat(whole(chunks)).as("level %s valid again", at).isTrue();
            assertThat(openPass().runOnce().registered()).as("level %s valid again: the swap registers", at).containsExactly(CHUNKS_SWAP);
        }
    }

    /**
     * A pass that finished (both swap rows exist, both chains whole) opens no connection on its later ticks (review r2, item 6): a peer
     * holding the pass's lock is not even met (a pass that looked would stand by, {@code gateHeld} false). A pass object made after a
     * restart does look.
     */
    @Test
    void aFinishedPassOpensNoConnectionOnLaterTicks_andANewPassLooksAgain() throws Exception {
        String tenant = newTenant("finished");
        collection(tenant, name(tenant, "a"), 3);
        var pass = openPass();
        assertThat(pass.runOnce().registered()).as("the first tick finishes the store").containsExactlyInAnyOrder(CHUNKS_SWAP, CENTROIDS_SWAP);

        try (HeldLock peer = new HeldLock(LeafIndexPass.LOCK_KEY)) {
            assertThat(pass.runOnce().gateHeld()).as("finished: it never reached for the lock").isTrue();
            assertThat(openPass().runOnce().gateHeld()).as("a pass made after a restart does look, and stands by").isFalse();
        }
    }

    /** A pass that left an index unbuilt (a duplicate under the id key) is not finished: its next tick does look. */
    @Test
    void aPassThatLeftALeafUnbuiltIsNotFinished() throws Exception {
        String tenant = newTenant("unfinished");
        collection(tenant, name(tenant, "a"), 3);
        duplicateUnderTheIdKey(tenant, name(tenant, "a"), name(tenant, "dup"));
        var pass = openPass();
        pass.runOnce();
        assertThat(chunksLeavesWithoutIndex()).as("precondition: a leaf could not be indexed").isNotEmpty();

        try (HeldLock peer = new HeldLock(LeafIndexPass.LOCK_KEY)) {
            assertThat(pass.runOnce().gateHeld()).as("not finished: the tick looked, met the peer and stood by").isFalse();
        }
    }
}
