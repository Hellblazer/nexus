// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.NullSource;
import org.junit.jupiter.params.provider.ValueSource;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-dngtz.1.4 (RDR-167 Step 3, residual 1): {@link CorrelationId}'s accept
 * rule and the {@code nxe-} marker on an id the engine generates.
 */
class CorrelationIdTest {

    private static final String GENERATED_SHAPE = "nxe-[0-9a-f]{32}";
    private static final String CLIENT_ID = "0123456789abcdef0123456789abcdef";

    @ParameterizedTest
    @ValueSource(strings = {CLIENT_ID, "abc.DEF_1-2", "x"})
    void aValidIdIsKept(String inbound) {
        assertThat(CorrelationId.acceptOrGenerate(inbound)).isEqualTo(inbound);
    }

    @Test
    void aSixtyFourCharacterIdIsKept() {
        String inbound = "a".repeat(64);
        assertThat(CorrelationId.acceptOrGenerate(inbound)).isEqualTo(inbound);
    }

    @ParameterizedTest
    @NullSource
    @ValueSource(strings = {"", "   ", "a b", "abc\n", "abc\r\n", "\nabc", "abc;def", "ab/c",
            "a[b", "a`b", "a^b", "caf\u00e9", "\uff11\uff12"})
    void anInvalidIdIsReplacedByAGeneratedOne(String inbound) {
        String id = CorrelationId.acceptOrGenerate(inbound);
        assertThat(id).isNotEqualTo(inbound).matches(GENERATED_SHAPE);
        assertThat(CorrelationId.isGenerated(id)).isTrue();
    }

    @Test
    void aSixtyFiveCharacterIdIsReplaced() {
        String inbound = "a".repeat(65);
        assertThat(CorrelationId.acceptOrGenerate(inbound)).matches(GENERATED_SHAPE);
    }

    @Test
    void aGeneratedIdHasTheMarkerAndPassesTheAcceptRule() {
        String id = CorrelationId.generate();
        assertThat(id).matches(GENERATED_SHAPE).startsWith(CorrelationId.GENERATED_PREFIX);
        assertThat(CorrelationId.acceptOrGenerate(id)).isEqualTo(id);
    }

    @Test
    void twoGeneratedIdsDiffer() {
        assertThat(CorrelationId.generate()).isNotEqualTo(CorrelationId.generate());
    }

    @Test
    void isGeneratedTellsAnEngineIdFromAClientId() {
        assertThat(CorrelationId.isGenerated(CorrelationId.generate())).isTrue();
        assertThat(CorrelationId.isGenerated(CLIENT_ID)).isFalse();
        assertThat(CorrelationId.isGenerated(null)).isFalse();
    }

    @Test
    void anInboundIdWithTheMarkerIsKeptLikeAnyValidId() {
        assertThat(CorrelationId.acceptOrGenerate("nxe-abc")).isEqualTo("nxe-abc");
    }

    @Test
    void theMarkerCountsOnlyAsAPrefix() {
        assertThat(CorrelationId.isGenerated("abc-nxe-def")).isFalse();
    }

    @Test
    void headerNameIsTheWireName() {
        assertThat(CorrelationId.HEADER).isEqualTo("X-Correlation-ID");
    }
}
