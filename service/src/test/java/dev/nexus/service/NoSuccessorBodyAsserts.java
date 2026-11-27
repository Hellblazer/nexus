// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5c (bead nexus-p2m1x.25): the body of today's 409 {@code collection_superseded} for a tombstone that names no successor
 * ({@code CollectionSupersededBody}), asserted in one place for every test that reads it: the retired label and id, and none of the
 * successor's fields.
 */
public final class NoSuccessorBodyAsserts {

    private NoSuccessorBodyAsserts() {
    }

    /** {@code body} (status already 409) is the bare marker of the retired collection {@code label} / {@code id}; {@code what} names the request. */
    public static void assertBareMarker(JsonNode body, String what, String label, long id) {
        assertThat(body.path("reason").asText()).as(what + ": " + body).isEqualTo("retired_no_successor");
        assertThat(body.path("error").asText()).as(what).isEqualTo("collection_superseded");
        assertThat(body.path("collection").asText()).as(what + ": " + body).isEqualTo(label);
        assertThat(body.path("collection_id").asLong(-1)).as(what).isEqualTo(id);
        assertThat(body.has("superseded_by")).as("%s names no successor, so no superseded_by: %s", what, body).isFalse();
        assertThat(body.has("successor_collection_id")).as("%s: no successor_collection_id: %s", what, body).isFalse();
        assertThat(body.path("message").asText()).as("%s: the message names none: %s", what, body).contains("names no live successor");
    }
}
