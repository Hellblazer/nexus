// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * The route census as one text line per route, for {@code dev.nexus.service.http.CollectionSelectorTableTest}, which holds the
 * engine's route table equal to it. {@link CollectionRouteCensus} is package-private to this package and the table is
 * package-private to {@code http}, so neither test can see both; this view is the only thing that crosses.
 *
 * <p>Only the rows the route boundary resolves ({@link CollectionRouteCensus.Resolver#BOUNDARY}) are in the view: the document
 * routes resolve their id inside the write, so the table has no entry for them. A path the census lists twice, once with its
 * collection in the query (GET) and once in the body (POST), is one line, {@code EITHER}, as the table lists it.
 *
 * <p>Format: {@code <QUERY|BODY|EITHER> <path> <labelField>:<idField>:<list|one>:<nestedIn> ...}, one group per slot in request order.
 */
public final class CollectionRouteCensusView {

    private CollectionRouteCensusView() {
    }

    /** One line per census route the boundary resolves, in census order. */
    public static List<String> lines() {
        Map<String, String> byPath = new LinkedHashMap<>();
        Map<String, String> where = new LinkedHashMap<>();
        for (CollectionRouteCensus.Route route : CollectionRouteCensus.ROUTES) {
            if (route.resolver() != CollectionRouteCensus.Resolver.BOUNDARY) {
                continue;
            }
            StringBuilder slots = new StringBuilder();
            for (CollectionRouteCensus.Slot slot : route.slots()) {
                slots.append(' ').append(slot.labelField()).append(':').append(slot.idField()).append(':')
                    .append(slot.list() ? "list" : "one").append(':').append(slot.nestedIn());
            }
            String seen = where.get(route.path());
            String now = route.where().name();
            where.put(route.path(), seen == null || seen.equals(now) ? now : "EITHER");
            byPath.put(route.path(), slots.toString());
        }
        List<String> out = new ArrayList<>();
        byPath.forEach((path, slots) -> out.add(where.get(path) + " " + path + slots));
        return out;
    }
}
