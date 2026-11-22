// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.CollectionDeletes;

import java.io.IOException;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * The delete of {@code POST /v1/catalog/collections/delete}, by label or by {@code collection_id} (RDR-229 P3.5a, bead
 * nexus-p2m1x.23), the contract agreed with bob for P3.12: the 200 carries {@code collection_id} and {@code name} (the label the
 * client's pipeline purge keys on); an id no row has is a typed 404 {@code unknown_collection_id} (the route boundary's, see
 * {@link CollectionSelectorFilter}); a retired row, asked for by its label or its id, is refused 409
 * {@code collection_superseded} naming the live successor's id and the tombstone stays; a label held by more than one live row
 * is refused 409 {@code collection_label_ambiguous} naming the ids, and the id path is how one of them is removed.
 *
 * <p>Moved out of {@code CatalogHandler} (over its size ceiling). The handler still reads the label and
 * {@code keep_quarantine} from the body itself, so its census of request-field reads is unchanged, and hands them here.
 */
final class CollectionDeleteRoute {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private CollectionDeleteRoute() {
    }

    /**
     * @param name           the label to delete (for an id request, the label the boundary resolved it to)
     * @param resolvedId     {@link CollectionSelectorFilter#RESOLVED_ID} from the body: the id the boundary resolved, or null
     */
    static void handle(HttpExchange exchange, String tenant, CatalogRepository repo, String name, boolean keepQuarantine,
                       Object resolvedId) throws IOException {
        Long id = resolvedId instanceof Number n ? n.longValue() : null;
        CollectionDeletes.Verdict verdict = repo.deleteVerdict(tenant, name, id);
        Map<String, Object> out = new LinkedHashMap<>();
        switch (verdict.kind()) {
            case UNKNOWN -> HttpUtil.send(exchange, 404, CollectionSelectorFilter.unknownBody(verdict.collectionId()));
            case AMBIGUOUS -> {
                out.put("error", "collection '" + name + "' is held by " + verdict.ids().length + " live rows (collection_ids "
                    + java.util.Arrays.toString(verdict.ids()) + "); delete one of them by its collection_id");
                out.put("reason", HttpUtil.COLLECTION_LABEL_AMBIGUOUS_REASON);
                out.put("collection", name);
                out.put("collection_ids", verdict.ids());
                HttpUtil.send(exchange, 409, MAPPER.writeValueAsString(out));
            }
            case SUPERSEDED -> {
                out.put("error", "collection '" + name + "' is retired: its live successor is collection_id "
                    + verdict.successorId() + "; delete that one (the retired row goes with it when it holds nothing)");
                out.put("reason", HttpUtil.COLLECTION_SUPERSEDED_REASON);
                out.put("collection", name);
                out.put("collection_id", verdict.collectionId());
                out.put("successor_collection_id", verdict.successorId());
                HttpUtil.send(exchange, 409, MAPPER.writeValueAsString(out));
            }
            case PROCEED -> {
                Map<String, Integer> counts = id != null
                    ? repo.deleteCollectionById(tenant, name, id, keepQuarantine)
                    : repo.deleteCollection(tenant, name, keepQuarantine);
                out.put("deleted", counts);
                if (verdict.collectionId() > 0) {
                    out.put("collection_id", verdict.collectionId());
                }
                out.put("name", name);
                HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(out));
            }
        }
    }
}
