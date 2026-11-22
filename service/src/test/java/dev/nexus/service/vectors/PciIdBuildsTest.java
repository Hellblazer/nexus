// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/** RDR-229 P3.4 (linda, 2026-11-22): the name family retires only when gate 3 has latched AND the id readers are present. */
class PciIdBuildsTest {

    /** A class with the shape P3.5's {@code IdReaders} has, readers present. */
    static final class ReadersPresent {
        public static final boolean PRESENT = true;
    }

    /** The same, readers absent. */
    static final class ReadersAbsent {
        public static final boolean PRESENT = false;
    }

    /** No {@code PRESENT} field at all. */
    static final class NoField { }

    @Test
    void theReadersAreReadByReflection_andAbsenceOrAnyFailureIsFalse() {
        assertThat(PciIdBuilds.readersPresent(ReadersPresent.class.getName())).isTrue();
        assertThat(PciIdBuilds.readersPresent(ReadersAbsent.class.getName())).isFalse();
        assertThat(PciIdBuilds.readersPresent(NoField.class.getName())).as("no PRESENT field").isFalse();
        assertThat(PciIdBuilds.readersPresent("dev.nexus.service.vectors.NoSuchReaders")).as("no such class").isFalse();
        assertThat(PciIdBuilds.ID_READERS_CLASS).isEqualTo("dev.nexus.service.vectors.IdReaders");
    }

    @Test
    void theRetireNeedsTheLatchedGate3AndThePresentReaders_andAnInjectedSupplierControlsBoth() {
        var b = new PciIdBuilds();
        assertThat(b.gate3Holds()).as("nothing wired: closed").isFalse();

        b.gate3(() -> true);
        b.readers(() -> false);
        assertThat(b.gate3Holds()).as("latched gate 3, readers absent: no name index is dropped").isFalse();

        b.readers(() -> true);
        assertThat(b.gate3Holds()).as("latched gate 3, readers present").isTrue();

        b.gate3(() -> false);
        assertThat(b.gate3Holds()).as("readers present, gate 3 not latched").isFalse();

        b.gate3(() -> { throw new IllegalStateException("boom"); });
        assertThat(b.gate3Holds()).as("unknown is not holds").isFalse();
    }

    @Test
    void theDefaultReadersSupplierIsTheReflectiveOne_soABranchWithoutIdReadersDropsNothing() {
        var b = new PciIdBuilds();
        b.gate3(() -> true);
        // The default supplier reads dev.nexus.service.vectors.IdReaders.PRESENT: false until P3.5 defines the class.
        assertThat(b.gate3Holds()).isEqualTo(PciIdBuilds.idReadersPresent());
    }
}
