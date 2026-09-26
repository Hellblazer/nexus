// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-192 Step 2 (bead nexus-wbfpw.4): {@code POST /v1/vectors/manifest-less-census}
 * refuses a {@code quarantine-*} collection with 400 — quarantine rows are out of
 * the census by construction (RDR-192 MVV (a)). The repository test cannot prove
 * the handler's refusal (it never reaches the repository), so this is a static
 * method with its own pin, mirroring {@link VectorHandlerRowLimitRoutingTest}'s
 * precedent for {@code row_limit} routing.
 */
class VectorHandlerManifestLessCensusRoutingTest {

    @Test
    void quarantinePrefixedCollection_isRefused() {
        assertThatThrownBy(() -> VectorHandler.requireNotQuarantineCollection("quarantine-code__x__voyage-code-3__v1"))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("quarantine");
    }

    @Test
    void ordinaryCollection_isAccepted() {
        assertThatCode(() -> VectorHandler.requireNotQuarantineCollection("knowledge__x__minilm-l6-v2-384__v1"))
            .doesNotThrowAnyException();
    }

    @Test
    void collectionNamedQuarantineButWithoutTheTrailingHyphen_isNotRefused() {
        // "quarantine" alone (no trailing "-") is not the quarantine-collection
        // grammar (docs/collections.md) -- this pin exists so a future edit
        // cannot accidentally widen the prefix check to a bare substring match.
        assertThatCode(() -> VectorHandler.requireNotQuarantineCollection("quarantine"))
            .doesNotThrowAnyException();
    }
}
