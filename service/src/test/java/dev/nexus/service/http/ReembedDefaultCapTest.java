// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-8zzjh: the default token cap fails closed. A model is uncapped only when it is of a family this engine
 * embeds on this machine (bge, MiniLM); every other model token, and none, gets the default. The route test cannot
 * reach the unknown case: {@code embedding_profile.embedding_model} is a foreign key to the known models.
 */
class ReembedDefaultCapTest {

    @Test
    void localFamiliesAreUncapped() {
        assertThat(ReembedHandler.defaultTokenCap("bge-base-en-v15-768")).isNull();
        assertThat(ReembedHandler.defaultTokenCap("minilm-l6-v2-384")).isNull();
    }

    @Test
    void voyageAnUnknownFamilyAndNoModelAreCapped() {
        assertThat(ReembedHandler.defaultTokenCap("voyage-context-3")).isEqualTo(ReembedHandler.DEFAULT_TOKEN_CAP);
        assertThat(ReembedHandler.defaultTokenCap("voyage-code-3")).isEqualTo(10_000_000L);
        assertThat(ReembedHandler.defaultTokenCap("acme-embed-9000")).as("unknown family: fail closed")
            .isEqualTo(10_000_000L);
        assertThat(ReembedHandler.defaultTokenCap("bge")).as("a prefix needs its separator").isEqualTo(10_000_000L);
        assertThat(ReembedHandler.defaultTokenCap("")).isEqualTo(10_000_000L);
        assertThat(ReembedHandler.defaultTokenCap(null)).isEqualTo(10_000_000L);
    }
}
