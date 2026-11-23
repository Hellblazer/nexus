// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.Map;
import java.util.OptionalLong;

/**
 * The collection ids a request named (RDR-229 P3.5a, bead nexus-p2m1x.23; linda's ruling on the route boundary's gap), carried
 * from the route boundary to the handler on the request's own thread. The boundary resolves a {@code collection_id} to a label
 * and the handler then works by that label, so between the two the id could be freed and its label taken by another row, and a
 * write by the id would land in that row. While an id is named here, {@link CollectionRegistry} resolves THAT id (inside the
 * handler's transaction, {@code FOR KEY SHARE}, which makes a concurrent delete of the row wait for the write) and never the
 * label: no row is {@link UnknownCollectionIdException}, a retired row is {@link CollectionRetiredIdException}, a live row
 * answers under its own label.
 *
 * <p>A {@link ThreadLocal}, not an exchange attribute: the HTTP server keeps exchange attributes on the shared context, where
 * two requests would see each other's (see {@code RequestContext}). The boundary filter sets it just before the handler runs on
 * the same thread and clears it in a {@code finally}. A handler that moves the work to another thread is not covered, and none
 * of the collection routes does.
 */
public final class ExpectedCollectionIds {

    private static final ThreadLocal<Map<String, Long>> CURRENT = new ThreadLocal<>();

    private ExpectedCollectionIds() {
    }

    /** Names the ids of this request, by the label the boundary resolved each to. An empty map names none. */
    public static void set(Map<String, Long> idsByLabel) {
        if (idsByLabel.isEmpty()) {
            CURRENT.remove();
        } else {
            CURRENT.set(Map.copyOf(idsByLabel));
        }
    }

    public static void clear() {
        CURRENT.remove();
    }

    /** The id this request named for {@code label}, if it named one. */
    static OptionalLong of(String label) {
        Map<String, Long> ids = CURRENT.get();
        Long id = ids == null ? null : ids.get(label);
        return id == null ? OptionalLong.empty() : OptionalLong.of(id);
    }
}
