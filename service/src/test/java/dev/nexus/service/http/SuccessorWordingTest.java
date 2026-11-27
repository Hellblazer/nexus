// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import org.junit.jupiter.api.Test;

import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 R7 (bead nexus-p2m1x.25): the one wording of what a retired collection was superseded by, shared by the supersede, rename
 * and concurrent-supersede refusals of {@code CatalogHandler}. The concurrent-supersede race cannot be driven through HTTP, so its
 * sentence is pinned here.
 */
class SuccessorWordingTest {

    @Test
    void aLiveSuccessorIsNamed() {
        assertThat(SuccessorWording.supersededBy(Optional.of("new"))).isEqualTo("superseded by new");
        assertThat(SuccessorWording.supersededTo(Optional.of("new"))).isEqualTo("superseded to new");
    }

    @Test
    void noSuccessorSaysSoAndNamesNothing() {
        assertThat(SuccessorWording.supersededBy(Optional.empty())).isEqualTo("retired and names no live successor");
        assertThat(SuccessorWording.supersededTo(Optional.empty())).isEqualTo("retired and names no live successor");
    }

    @Test
    void theParentheticalForARetiredRowReadsRightEitherWay() {
        assertThat(SuccessorWording.retiredBecause(Optional.of("new"))).isEqualTo("superseded by new");
        assertThat(SuccessorWording.retiredBecause(Optional.empty())).isEqualTo("it names no live successor");
    }

    @Test
    void theConcurrentSupersedeRefusalReadsRightEitherWay() {
        assertThat(SuccessorWording.concurrentSupersede("a", Optional.of("b"), "c"))
            .isEqualTo("collection a was superseded to b concurrently; refusing to chain a second supersede to c");
        assertThat(SuccessorWording.concurrentSupersede("a", Optional.empty(), "c"))
            .isEqualTo("collection a was retired and names no live successor concurrently; refusing to chain a second supersede to c");
    }
}
