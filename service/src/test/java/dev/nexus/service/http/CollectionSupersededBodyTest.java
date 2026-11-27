// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.CollectionSuccessor;
import org.junit.jupiter.api.Test;

import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 R7 (bead nexus-p2m1x.25): the 409 body of a retired collection. With a live successor it is the {@code collection_superseded}
 * body of every released engine; with none, {@code error} stays {@code collection_superseded} and {@code reason} is
 * {@code retired_no_successor}, with no successor field.
 */
class CollectionSupersededBodyTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    @Test
    void aTombstoneWithNoSuccessorCarriesTheRetiredNoSuccessorReason() throws Exception {
        JsonNode body = MAPPER.readTree(CollectionSupersededBody.of(7L, "old", Optional.empty()));
        assertThat(body.path("error").asText()).isEqualTo("collection_superseded");
        assertThat(body.path("reason").asText()).isEqualTo("retired_no_successor");
        assertThat(body.path("collection").asText()).isEqualTo("old");
        assertThat(body.path("collection_id").asLong()).isEqualTo(7L);
        assertThat(body.has("superseded_by")).isFalse();
        assertThat(body.has("successor_collection_id")).isFalse();
        assertThat(body.path("message").asText()).contains("names no live successor");
    }

    @Test
    void aTombstoneWithALiveSuccessorKeepsTheCollectionSupersededReason() throws Exception {
        JsonNode body = MAPPER.readTree(CollectionSupersededBody.of(7L, "old",
            Optional.of(new CollectionSuccessor.Successor(9L, "new"))));
        assertThat(body.path("error").asText()).isEqualTo("collection_superseded");
        assertThat(body.path("reason").asText()).isEqualTo("collection_superseded");
        assertThat(body.path("superseded_by").asText()).isEqualTo("new");
        assertThat(body.path("successor_collection_id").asLong()).isEqualTo(9L);
    }
}
