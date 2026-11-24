// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.core.JsonParser;
import com.fasterxml.jackson.core.JsonToken;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.CollectionRow;

import java.io.IOException;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

/**
 * RDR-229 R7 (bead nexus-p2m1x.25, linda's ruling 2026-11-23): a READ addressed by LABEL to a retired collection that names no live
 * successor is refused with the 409 {@code collection_superseded} the same request by id gets ({@link CollectionSupersededBody}: the
 * retired label and id, no successor field). Without it the label resolves to the tombstone ({@code CollectionRegistry#labelResolutionOrder}:
 * the live row first, else the newest tombstone) and the read runs against a collection that no longer answers, silently.
 *
 * <p><b>Why the route boundary and not the registry.</b> The registry's label read ({@code resolveLabel}, {@code lookup}) is also how the
 * rename and re-home bodies, the doctor's counts, {@code CompletionCheck}, the import and restore fidelity paths and the quarantine
 * restore find the tombstone they work on, and the reads of a tombstone's own row ({@code collections/get}). A refusal there would break
 * each of them. The route table already says which routes read and which write ({@link CollectionSelectors}), so the refusal is made once
 * per request, for the routes in {@link CollectionSelectors#RETIRED_LABEL_REFUSED}, and a reader that must see a tombstone is a route in
 * {@link CollectionSelectors#RETIRED_LABEL_READABLE}, with its reason. A writer is not here: it keeps its own refusal
 * ({@code CollectionSupersededException}, {@code SupersededCollectionWriteError} on the client).
 *
 * <p>What it leaves alone, so no behaviour moves for it: a label that resolves to a LIVE row (the cache never holds a tombstone, so a
 * cache hit is live and costs nothing), a label no row has (the handler's own answer), a request by id (the boundary already refused or
 * resolved it), and a tombstone WITH a live successor, which reads as it did. The successor is found as the by-id refusal finds it, by
 * {@code superseded_by_id} ({@code CollectionSuccessor}), never by the tombstone's label text.
 *
 * <p>The labels are read from the request the boundary has already rewritten, with the fields the route table names for it, so a request
 * the table does not describe (no such field) names nothing here.
 */
final class RetiredLabelReadGuard {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private RetiredLabelReadGuard() {
    }

    /**
     * The 409 body for the first retired, successor-less collection among {@code labels}, or empty. {@code byId} are the labels the boundary
     * resolved from ids in this request (live rows, already read), which are not read again.
     */
    static Optional<String> refusal(CollectionSelectorFilter.Rows rows, String tenant, Set<String> byId, List<String> labels) {
        Set<String> named = new LinkedHashSet<>(labels);
        named.removeAll(byId);
        if (named.isEmpty()) {
            return Optional.empty();
        }
        Map<String, CollectionRow> resolved = rows.byLabels(tenant, named);   // one registry read for the labels the cache lacks
        for (String label : named) {
            CollectionRow row = resolved.get(label);
            if (row != null && row.isSuperseded() && rows.successorOf(tenant, row.collectionId()).isEmpty()) {
                return Optional.of(CollectionSupersededBody.of(row.collectionId(), row.label(), Optional.empty()));
            }
        }
        return Optional.empty();
    }

    /** The collection labels a query string carries in the route's label fields (a repeated key counts each value). */
    static List<String> labelsInQuery(CollectionSelectors.Route route, String rawQuery) {
        List<String> out = new ArrayList<>();
        if (rawQuery == null || rawQuery.isEmpty()) {
            return out;
        }
        Set<String> fields = labelFields(route);
        for (String pair : rawQuery.split("&")) {
            String[] kv = pair.split("=", 2);
            if (kv.length == 2 && fields.contains(URLDecoder.decode(kv[0], StandardCharsets.UTF_8))) {
                out.add(URLDecoder.decode(kv[1], StandardCharsets.UTF_8));
            }
        }
        return out;
    }

    /**
     * The collection labels a JSON body carries at its top level in the route's label fields: a string, or an array of strings (the
     * {@code collections} list). A token scan, not a tree: every other value is skipped whole. A body that is not JSON names none; the
     * handler refuses it in its own words.
     */
    static List<String> labelsInBody(CollectionSelectors.Route route, byte[] body) {
        List<String> out = new ArrayList<>();
        Set<String> fields = labelFields(route);
        try (JsonParser p = MAPPER.getFactory().createParser(body)) {
            if (p.nextToken() != JsonToken.START_OBJECT) {
                return out;
            }
            while (p.nextToken() == JsonToken.FIELD_NAME) {
                String name = p.currentName();
                JsonToken value = p.nextToken();
                if (fields.contains(name) && value == JsonToken.VALUE_STRING) {
                    out.add(p.getText());
                } else if (fields.contains(name) && value == JsonToken.START_ARRAY) {
                    while (p.nextToken() != JsonToken.END_ARRAY) {
                        if (p.currentToken() == JsonToken.VALUE_STRING) {
                            out.add(p.getText());
                        } else {
                            p.skipChildren();
                        }
                    }
                } else {
                    p.skipChildren();
                }
            }
        } catch (IOException notJson) {
            return List.of();
        }
        return out;
    }

    /** The top-level label fields of a route (a nested slot, a field of each element of an array, is not read here). */
    private static Set<String> labelFields(CollectionSelectors.Route route) {
        Set<String> fields = new LinkedHashSet<>();
        for (CollectionSelectors.Slot slot : route.slots()) {
            if (!slot.nested()) {
                fields.addAll(slot.labelFields());
            }
        }
        return fields;
    }
}
