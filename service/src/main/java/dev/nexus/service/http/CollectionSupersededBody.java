// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.CollectionSuccessor;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Optional;

/**
 * The body of the 409 {@code collection_superseded} for a request that names a RETIRED row by {@code collection_id} (RDR-229
 * P3.5a, bead nexus-p2m1x.23; linda's D3). One builder for every place that finds the retired row: the route boundary
 * ({@link CollectionSelectorFilter}), the delete's verdict, and a registry read inside a handler's transaction
 * ({@code CollectionRetiredIdException}), so all three answer the same bytes.
 *
 * <p>The shape is that of the engine's other {@code collection_superseded} bodies ({@code HttpUtil#sendTypedDbError}):
 * {@code error} and {@code reason} the same value, {@code message}, {@code collection} (the retired label) and
 * {@code superseded_by} (the successor's label, the key the client's {@code superseded_write_error_from_body} reads), plus the ids,
 * {@code collection_id} and {@code successor_collection_id}. The successor fields are present only when the walk by
 * {@code superseded_by_id} ({@link CollectionSuccessor}) found a live successor; its label is the row's CURRENT label.
 */
final class CollectionSupersededBody {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private CollectionSupersededBody() {
    }

    static String of(long retiredId, String retiredLabel, Optional<CollectionSuccessor.Successor> successor) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("error", HttpUtil.COLLECTION_SUPERSEDED_REASON);
        body.put("reason", HttpUtil.COLLECTION_SUPERSEDED_REASON);
        body.put("message", successor
            .map(s -> "collection_id " + retiredId + " ('" + retiredLabel + "') was superseded by '" + s.label()
                + "' (collection_id " + s.id() + "), so a request that names it is refused")
            .orElse("collection_id " + retiredId + " ('" + retiredLabel
                + "') is retired and names no live successor, so a request that names it is refused"));
        body.put("collection", retiredLabel);
        body.put("collection_id", retiredId);
        successor.ifPresent(s -> {
            body.put("superseded_by", s.label());
            body.put("successor_collection_id", s.id());
        });
        try {
            return MAPPER.writeValueAsString(body);
        } catch (java.io.IOException e) {
            throw new IllegalStateException(e);
        }
    }
}
