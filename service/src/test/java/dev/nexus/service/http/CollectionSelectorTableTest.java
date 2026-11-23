// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import dev.nexus.service.CollectionRouteCensusView;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23): the engine's route table ({@link CollectionSelectors}) is the route census of the test tree
 * ({@code CollectionRouteCensus}, which {@code CollectionRoutesByIdTest} runs and {@code CollectionRouteCensusTest} proves
 * complete), path for path and slot for slot. A route the census gains and the table does not would answer an id with the
 * handler's own "label required" 400; a table row with no census row would be a route nothing tests.
 */
class CollectionSelectorTableTest {

    private static String line(String path, CollectionSelectors.Route route) {
        StringBuilder line = new StringBuilder(route.where().name()).append(' ').append(path);
        for (CollectionSelectors.Slot slot : route.slots()) {
            line.append(' ').append(slot.labelField()).append(':').append(slot.idField()).append(':')
                .append(slot.list() ? "list" : "one").append(':').append(slot.nestedIn());
        }
        return line.toString();
    }

    @Test
    void theTableIsTheCensus_routeForRouteAndSlotForSlot() {
        List<String> census = CollectionRouteCensusView.lines();
        List<String> table = new ArrayList<>();
        CollectionSelectors.ROUTES.forEach((path, route) -> table.add(line(path, route)));

        assertThat(census).as("non-vacuity: the census has the routes").hasSizeGreaterThanOrEqualTo(80);
        assertThat(table).as("non-vacuity: the table has the routes").hasSizeGreaterThanOrEqualTo(80);
        assertThat(new TreeSet<>(table)).as("the table and the census name the same routes and slots")
            .isEqualTo(new TreeSet<>(census));
        assertThat(table).as("no route is listed twice").doesNotHaveDuplicates();
    }

    @Test
    void everyWriteRouteIsARouteOfTheTable_andOnlyBodyRoutesWrite() {
        assertThat(CollectionSelectors.WRITES).as("non-vacuity").hasSizeGreaterThanOrEqualTo(25);
        for (String path : CollectionSelectors.WRITES) {
            CollectionSelectors.Route route = CollectionSelectors.ROUTES.get(path);
            assertThat(route).as("a write route that is not in the table: %s", path).isNotNull();
            assertThat(route.writes()).as(path).isTrue();
            assertThat(route.where()).as(path).isEqualTo(CollectionSelectors.Where.BODY);
        }
        long marked = CollectionSelectors.ROUTES.values().stream().filter(CollectionSelectors.Route::writes).count();
        assertThat(marked).as("the table marks exactly the WRITES set").isEqualTo(CollectionSelectors.WRITES.size());
    }

    @Test
    void onlyTheDeleteHandsItsHandlerTheResolvedId() {
        List<String> passes = new ArrayList<>();
        for (Map.Entry<String, CollectionSelectors.Route> e : CollectionSelectors.ROUTES.entrySet()) {
            if (e.getValue().passesResolvedId()) {
                passes.add(e.getKey());
            }
        }
        assertThat(passes).containsExactly("/v1/catalog/collections/delete");
    }

    @Test
    void theAliasesAreTheOnesTheHandlersAccept() {
        // the delete reads "name" or "collection"; the rename reads "old_name" or "old"; no other route has an alias
        assertThat(CollectionSelectors.ROUTES.get("/v1/catalog/collections/delete").slots().get(0).labelFields())
            .containsExactly("name", "collection");
        assertThat(CollectionSelectors.ROUTES.get("/v1/catalog/collections/rename").slots().get(0).labelFields())
            .containsExactly("old_name", "old");
        long aliased = CollectionSelectors.ROUTES.values().stream()
            .flatMap(r -> r.slots().stream()).filter(s -> s.labelFields().size() > 1).count();
        assertThat(aliased).isEqualTo(2);
    }
}
