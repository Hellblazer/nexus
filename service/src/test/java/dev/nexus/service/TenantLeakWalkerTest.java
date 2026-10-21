// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Supplier;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Bead nexus-vr8wk: the leak walker finds a tenant name wherever the canary needs it found, and says nothing where it
 * is absent. Without these, a walker that quietly stopped descending into maps or lambdas would turn the canary
 * green over a leak (a pass over input it never examined).
 */
class TenantLeakWalkerTest {

    private static final String X = "leak-tenant-x1";

    static final class StaticHolder {
        static final Map<String, Object> BY_TENANT = new ConcurrentHashMap<>();
        static final Set<String> NAMES = ConcurrentHashMap.newKeySet();
    }

    record Entry(String owner, int count) { }

    static final class Holder {
        final List<Object> list = new ArrayList<>();
        final AtomicReference<Object> last = new AtomicReference<>();
        final Map<String, Entry> entries = new HashMap<>();
        Optional<String> maybe = Optional.empty();
        Object[] array = new Object[2];
        Supplier<String> captured;
        Holder self;
    }

    private static List<String> holderPaths(TenantLeakWalker.Result r) {
        return r.hits().stream().map(TenantLeakWalker.Hit::path).toList();
    }

    @Test
    void findsTheNeedleInEveryShapeOfHolder() {
        var h = new Holder();
        h.list.add(List.of("a", "row-" + X));
        h.last.set(Map.of("k", X));
        h.entries.put("e1", new Entry(X, 1));
        h.maybe = Optional.of(X + "-opt");
        h.array[1] = X;
        String captured = X + "-captured";
        h.captured = () -> captured;   // the string lives only in the lambda's capture
        h.self = h;                    // a cycle must terminate

        var r = TenantLeakWalker.walk(X, Map.of("root", h), List.of());

        assertThat(holderPaths(r)).containsExactlyInAnyOrder(
            "root.list[0][1]", "root.last.get[k]", "root.entries[e1].owner", "root.maybe.get", "root.array[1]",
            "root.captured.arg$1");
    }

    @Test
    void findsAMapKeyAndAStaticField() {
        StaticHolder.BY_TENANT.put(X + "/coll", 1);
        StaticHolder.NAMES.add("unrelated");
        StaticHolder.NAMES.add("tenant:" + X);
        try {
            var r = TenantLeakWalker.walk(X, Map.of(), List.of(StaticHolder.class));

            assertThat(holderPaths(r)).hasSize(2).contains("TenantLeakWalkerTest$StaticHolder.BY_TENANT{key}")
                .anyMatch(path -> path.startsWith("TenantLeakWalkerTest$StaticHolder.NAMES["));
            assertThat(r.staticOwners()).isEqualTo(1);
            assertThat(r.staticSkipped()).isEmpty();
        } finally {
            StaticHolder.BY_TENANT.clear();
            StaticHolder.NAMES.clear();
        }
    }

    @Test
    void reportsNothingWhenTheNeedleIsAbsent_andCountsWhatItWalked() {
        var h = new Holder();
        h.list.add("something else");
        h.entries.put("e", new Entry("someone-else", 2));
        h.captured = () -> "nope";

        var r = TenantLeakWalker.walk(X, Map.of("root", h), List.of());

        assertThat(r.hits()).isEmpty();
        assertThat(r.objects()).as("it did walk the graph, so an empty result is not a walk of nothing")
            .isGreaterThan(4);
    }

    @Test
    void doesNotDescendIntoForeignObjects_butCountsThem() {
        var h = new Holder();
        h.list.add(new java.util.concurrent.atomic.AtomicLong(7));
        h.list.add(new StringBuilder(X));   // a CharSequence is not a String: out of scope, stated
        h.list.add(new java.util.Random(1));

        var r = TenantLeakWalker.walk(X, Map.of("root", h), List.of());

        assertThat(r.hits()).isEmpty();
        assertThat(r.opaque()).containsKey("java.util.Random");
    }

    @Test
    void aNeedleTooShortToBeATenantIsRefused() {
        assertThatThrownBy(() -> TenantLeakWalker.walk("ab", Map.of(), List.of()))
            .isInstanceOf(IllegalArgumentException.class);
    }

    // ── a walk that did not finish is a failure, not a green result (nexus-vr8wk fix round, critic S2) ──

    @Test
    void aWalkPastTheDepthCapFailsInsteadOfTruncating() {
        Object chain = X;   // the needle sits below the cap: a silent cut-off would hide it
        for (int i = 0; i < TenantLeakWalker.MAX_DEPTH + 6; i++) {
            chain = List.of(chain);
        }
        Object root = chain;

        assertThatThrownBy(() -> TenantLeakWalker.walk(X, Map.of("root", root), List.of()))
            .isInstanceOf(TenantLeakWalker.WalkTruncatedException.class)
            .hasMessageContaining("deeper than " + TenantLeakWalker.MAX_DEPTH);
    }

    @Test
    void aChainJustWithinTheDepthCapStillWalksAndFindsTheNeedle() {
        Object chain = X;
        for (int i = 0; i < TenantLeakWalker.MAX_DEPTH - 2; i++) {
            chain = List.of(chain);
        }

        var r = TenantLeakWalker.walk(X, Map.of("root", chain), List.of());

        assertThat(r.hits()).hasSize(1);
    }

    @Test
    void aWalkPastTheObjectCapFailsInsteadOfTruncating() {
        var big = new ArrayList<Object>();
        for (int i = 0; i < 50; i++) {
            big.add(new ArrayList<>(List.of("filler-" + i)));
        }

        assertThatThrownBy(() -> TenantLeakWalker.walk(X, Map.of("root", big), List.of(), 20))
            .isInstanceOf(TenantLeakWalker.WalkTruncatedException.class)
            .hasMessageContaining("cap of 20 objects");
        assertThat(TenantLeakWalker.walk(X, Map.of("root", big), List.of(), 1_000).objects())
            .as("the same graph under a roomy cap completes").isGreaterThan(50);
    }

    /** A list whose iterator throws {@link java.util.ConcurrentModificationException} the first {@code failures}
     *  times it is asked for one: an engine thread changing it under the walk. */
    static final class FlakyList extends ArrayList<Object> {
        int failures;
        int iterators;

        @Override
        public java.util.Iterator<Object> iterator() {
            iterators++;
            if (failures-- > 0) {
                throw new java.util.ConcurrentModificationException("simulated");
            }
            return super.iterator();
        }
    }

    @Test
    void aConcurrentModificationRestartsTheWalk_andThenSucceeds() {
        var flaky = new FlakyList();
        flaky.add("row-" + X);
        flaky.failures = TenantLeakWalker.MAX_ATTEMPTS - 1;

        var r = TenantLeakWalker.walk(X, Map.of("root", flaky), List.of());

        assertThat(flaky.iterators).as("it retried").isEqualTo(TenantLeakWalker.MAX_ATTEMPTS);
        assertThat(holderPaths(r)).containsExactly("root[0]");
    }

    @Test
    void aConcurrentModificationThatNeverClearsFailsTheWalkAfterTheBound() {
        var flaky = new FlakyList();
        flaky.add("row");
        flaky.failures = Integer.MAX_VALUE;

        assertThatThrownBy(() -> TenantLeakWalker.walk(X, Map.of("root", flaky), List.of()))
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("all " + TenantLeakWalker.MAX_ATTEMPTS + " attempts")
            .hasCauseInstanceOf(java.util.ConcurrentModificationException.class);
        assertThat(flaky.iterators).isEqualTo(TenantLeakWalker.MAX_ATTEMPTS);
    }
}
