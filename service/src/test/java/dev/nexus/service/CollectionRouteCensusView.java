// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import java.util.ArrayList;
import java.util.List;

/**
 * The route census as one text line per route, for {@code dev.nexus.service.http.CollectionSelectorTableTest}, which holds the
 * engine's route table equal to it. {@link CollectionRouteCensus} is package-private to this package and the table is
 * package-private to {@code http}, so neither test can see both; this view is the only thing that crosses.
 *
 * <p>Format: {@code <QUERY|BODY> <path> <labelField>:<idField>:<list|one>:<nestedIn> ...}, one group per slot in request order.
 */
public final class CollectionRouteCensusView {

    private CollectionRouteCensusView() {
    }

    /** One line per census route, in census order. */
    public static List<String> lines() {
        List<String> out = new ArrayList<>();
        for (CollectionRouteCensus.Route route : CollectionRouteCensus.ROUTES) {
            StringBuilder line = new StringBuilder(route.where().name()).append(' ').append(route.path());
            for (CollectionRouteCensus.Slot slot : route.slots()) {
                line.append(' ').append(slot.labelField()).append(':').append(slot.idField()).append(':')
                    .append(slot.list() ? "list" : "one").append(':').append(slot.nestedIn());
            }
            out.add(line.toString());
        }
        return out;
    }
}
