// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-v3w9n Amendment 2 — pure unit coverage for the tumbler-grammar
 * segment-count predicates, no DB/HTTP involved.
 */
class TumblerGrammarTest {

    @Test
    void isOwnerPrefix_exactlyTwoSegments_true() {
        assertThat(TumblerGrammar.isOwnerPrefix("1.7")).isTrue();
        assertThat(TumblerGrammar.isOwnerPrefix("bt.1")).isTrue();
        assertThat(TumblerGrammar.isOwnerPrefix("vfef0.race")).isTrue();
    }

    @Test
    void isOwnerPrefix_oneSegment_false() {
        assertThat(TumblerGrammar.isOwnerPrefix("1")).isFalse();
        assertThat(TumblerGrammar.isOwnerPrefix("bo1")).isFalse();
        assertThat(TumblerGrammar.isOwnerPrefix("regcf-1")).isFalse();
    }

    @Test
    void isOwnerPrefix_threeOrMoreSegments_false() {
        assertThat(TumblerGrammar.isOwnerPrefix("1.7.1")).isFalse();
    }

    @Test
    void isOwnerPrefix_emptySegmentsOrNull_false() {
        assertThat(TumblerGrammar.isOwnerPrefix("")).isFalse();
        assertThat(TumblerGrammar.isOwnerPrefix(".")).isFalse();
        assertThat(TumblerGrammar.isOwnerPrefix("1.")).isFalse();
        assertThat(TumblerGrammar.isOwnerPrefix(".1")).isFalse();
        assertThat(TumblerGrammar.isOwnerPrefix(null)).isFalse();
    }

    /**
     * Fix round 1 (code-review-expert Important finding): a bare
     * {@code isEmpty()} check let a whitespace-only segment through —
     * {@code "1. "} split on '.' produces a second segment {@code " "},
     * which is non-empty but blank.
     */
    @Test
    void isOwnerPrefix_whitespaceOnlySegment_false() {
        assertThat(TumblerGrammar.isOwnerPrefix("1. ")).isFalse();
        assertThat(TumblerGrammar.isOwnerPrefix(" .1")).isFalse();
        assertThat(TumblerGrammar.isOwnerPrefix("1.\t")).isFalse();
    }

    @Test
    void isDocumentTumbler_threeOrMoreSegments_true() {
        assertThat(TumblerGrammar.isDocumentTumbler("1.7.1")).isTrue();
        assertThat(TumblerGrammar.isDocumentTumbler("bt.1.5")).isTrue();
        assertThat(TumblerGrammar.isDocumentTumbler("cov2.1.1.2")).isTrue();
    }

    @Test
    void isDocumentTumbler_fewerThanThreeSegments_false() {
        assertThat(TumblerGrammar.isDocumentTumbler("1.7")).isFalse();
        assertThat(TumblerGrammar.isDocumentTumbler("1")).isFalse();
    }

    @Test
    void isDocumentTumbler_emptySegmentsOrNull_false() {
        assertThat(TumblerGrammar.isDocumentTumbler("")).isFalse();
        assertThat(TumblerGrammar.isDocumentTumbler("1..1")).isFalse();
        assertThat(TumblerGrammar.isDocumentTumbler("1.7.")).isFalse();
        assertThat(TumblerGrammar.isDocumentTumbler(null)).isFalse();
    }

    @Test
    void isDocumentTumbler_whitespaceOnlySegment_false() {
        assertThat(TumblerGrammar.isDocumentTumbler("1.7. ")).isFalse();
        assertThat(TumblerGrammar.isDocumentTumbler(" .7.1")).isFalse();
        assertThat(TumblerGrammar.isDocumentTumbler("1.\t.1")).isFalse();
    }
}
