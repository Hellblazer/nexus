// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/** RDR-229 P3.4 (linda, 2026-11-22): the name family retires only when gate 3 has latched AND the id readers are present. */
class PciIdBuildsTest {

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
        b.gate3(() -> true);
        b.readers(() -> { throw new IllegalStateException("boom"); });
        assertThat(b.gate3Holds()).as("unknown readers is not holds").isFalse();
    }

    @Test
    void theProductionDefaultIsIdReadersPresent_trueSinceP35c_soTheRetireRunsOnceGate3Latches() {
        var b = new PciIdBuilds();
        b.gate3(() -> true);

        // The default supplier reads IdReaders.present(), a method, so a flip of the constant is never held inlined by a caller.
        assertThat(b.gate3Holds()).as("latched gate 3, production readers default").isEqualTo(IdReaders.present());
        // P3.5c flipped IdReaders.PRESENT to true; Rdr229P35cIdReadersPinTest holds it to what the readers do.
        assertThat(IdReaders.present()).as("true since P3.5c").isTrue();
        assertThat(b.gate3Holds()).isTrue();
    }
}
