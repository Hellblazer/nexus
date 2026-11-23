// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * RDR-229 P3.5a (bead nexus-p2m1x.23): which request fields of which routes name a collection, and the field that carries
 * the same collection by {@code collection_id}. {@link CollectionSelectorFilter} reads this table to resolve an id to its
 * label at the route boundary, once, before the handler runs.
 *
 * <p>The id spelling is the convention linda confirmed on 2026-11-22: the id twin of a single-valued {@code collection},
 * {@code name} or {@code old_name} is {@code collection_id}; of the list {@code collections} it is {@code collection_ids}; of
 * every other field {@code x} it is {@code x_id}. In a query string the id is a decimal number, in a body a JSON integer.
 * The route set is the census of {@code CollectionRouteCensus} (the test tree), and {@code CollectionSelectorTableTest} holds
 * this table equal to it, so a route cannot gain a collection field in one and not the other.
 *
 * <p>A handler never reads an id field: by the time it runs the id has been replaced by the label it names, so every handler,
 * and its own typed refusals (422 {@code unregistered_collection} for a label with no row), is unchanged.
 */
final class CollectionSelectors {

    private CollectionSelectors() {
    }

    /** Where the collection fields of a route travel. */
    enum Where { QUERY, BODY }

    /**
     * One collection-valued field and its id twin.
     *
     * @param labelFields the names the handler reads the label under; the first is the one the resolved label is written to,
     *                    a later one is an alias of it ({@code collection} for {@code name} on a delete)
     * @param idField     the id twin
     * @param list        the field carries an array of collections ({@code collections})
     * @param nestedIn    the request array whose every element carries the field, or {@code ""} for a top-level field
     */
    record Slot(List<String> labelFields, String idField, boolean list, String nestedIn) {

        String labelField() {
            return labelFields.get(0);
        }

        boolean nested() {
            return !nestedIn.isEmpty();
        }
    }

    /**
     * @param where       where the fields travel
     * @param slots       the collection fields, in request order
     * @param passesResolvedId the route's handler reads the id the boundary resolved ({@link CollectionSelectorFilter#RESOLVED_ID}),
     *                    not only the label: the delete, which must remove the one row of an id when a label is held by several.
     *                    Every route answers a retired row's id 409 {@code collection_superseded}, this one included
     */
    record Route(Where where, List<Slot> slots, boolean passesResolvedId) {
    }

    /** The routes by full path. */
    static final Map<String, Route> ROUTES = Map.copyOf(table());

    private static Slot one(String labelField, String idField) {
        return new Slot(List.of(labelField), idField, false, "");
    }

    private static Slot nested(String array, String labelField, String idField) {
        return new Slot(List.of(labelField), idField, false, array);
    }

    private static Slot coll() {
        return one("collection", "collection_id");
    }

    private static Slot name() {
        return one("name", "collection_id");
    }

    private static Slot many() {
        return new Slot(List.of("collections"), "collection_ids", true, "");
    }

    private static Slot aliased(Slot slot, String alias) {
        List<String> names = new ArrayList<>(slot.labelFields());
        names.add(alias);
        return new Slot(List.copyOf(names), slot.idField(), slot.list(), slot.nestedIn());
    }

    private static void get(Map<String, Route> t, String path, Slot... slots) {
        t.put(path, new Route(Where.QUERY, List.of(slots), false));
    }

    private static void post(Map<String, Route> t, String path, Slot... slots) {
        t.put(path, new Route(Where.BODY, List.of(slots), false));
    }

    private static void delete(Map<String, Route> t, String path, Slot... slots) {
        t.put(path, new Route(Where.BODY, List.of(slots), true));
    }

    private static Map<String, Route> table() {
        Map<String, Route> t = new LinkedHashMap<>();
        get(t, "/v1/catalog/collections/get", name());
        get(t, "/v1/catalog/collections/health", coll());
        get(t, "/v1/catalog/collections/owner-root", name());
        post(t, "/v1/catalog/collections/set_aspects_enabled", name());
        post(t, "/v1/catalog/collections/supersede", name(), one("superseded_by", "superseded_by_id"));
        post(t, "/v1/catalog/collections/rehome", one("source", "source_id"), one("target", "target_id"));
        post(t, "/v1/catalog/collections/rehome/status", one("source", "source_id"), one("target", "target_id"));
        post(t, "/v1/catalog/collections/rename", aliased(one("old_name", "collection_id"), "old"));
        delete(t, "/v1/catalog/collections/delete", aliased(name(), "collection"));
        get(t, "/v1/catalog/list", coll());
        get(t, "/v1/catalog/resolve", coll());
        get(t, "/v1/catalog/manifest/chashes", coll());
        get(t, "/v1/catalog/resolve_span", coll());
        get(t, "/v1/catalog/resolve_chash", one("prefer_collection", "prefer_collection_id"));
        get(t, "/v1/catalog/gc_audit/list", coll());
        post(t, "/v1/catalog/index-run/begin", coll());
        post(t, "/v1/catalog/index-run/begin-many", coll());
        post(t, "/v1/catalog/manifest/write", coll());
        post(t, "/v1/catalog/manifest/append", coll());
        post(t, "/v1/catalog/manifest/append_many", coll());
        post(t, "/v1/catalog/manifest/write_many", coll());
        post(t, "/v1/catalog/import/chunk", coll());
        post(t, "/v1/vectors/upsert-chunks", coll());
        post(t, "/v1/vectors/search", many());
        post(t, "/v1/vectors/query", many());
        post(t, "/v1/vectors/search-per-collection", many());
        post(t, "/v1/vectors/hybrid-search", many());
        post(t, "/v1/vectors/search-metadata-scoped", many());
        post(t, "/v1/vectors/search-topic-scoped", coll());
        post(t, "/v1/vectors/search-graph-hop", many());
        post(t, "/v1/vectors/search-aspect-scoped", many());
        post(t, "/v1/vectors/get", coll());
        post(t, "/v1/vectors/get-all-metadata", coll());
        post(t, "/v1/vectors/store-get", coll());
        post(t, "/v1/vectors/get-embeddings", coll());
        post(t, "/v1/vectors/store-list", coll());
        post(t, "/v1/vectors/store-delete", coll());
        post(t, "/v1/vectors/update-metadata", coll());
        get(t, "/v1/vectors/count", coll());
        post(t, "/v1/vectors/embed", coll());
        post(t, "/v1/vectors/gc/quarantine-orphans", coll(), one("quarantine_collection", "quarantine_collection_id"));
        post(t, "/v1/vectors/gc/restore-rereferenced", one("origin_collection", "origin_collection_id"), one("quarantine_collection", "quarantine_collection_id"));
        post(t, "/v1/vectors/gc/expire-quarantine", one("origin_collection", "origin_collection_id"), one("quarantine_collection", "quarantine_collection_id"));
        post(t, "/v1/vectors/gc/quarantine-restore", one("origin_collection", "origin_collection_id"));
        post(t, "/v1/vectors/manifest-less-census", coll());
        post(t, "/v1/vectors/reapable", coll());
        post(t, "/v1/vectors/reembed/plan", one("source_collection", "source_collection_id"), one("target_collection", "target_collection_id"));
        post(t, "/v1/vectors/reembed/jobs", one("source_collection", "source_collection_id"), one("target_collection", "target_collection_id"));
        get(t, "/v1/taxonomy/topics", coll());
        get(t, "/v1/taxonomy/topics/unreviewed", coll());
        get(t, "/v1/taxonomy/topics/resolve", coll());
        post(t, "/v1/taxonomy/topics/insert", coll());
        post(t, "/v1/taxonomy/assignments/assign", one("source_collection", "source_collection_id"));
        post(t, "/v1/taxonomy/assignments/assign_from_chashes", coll());
        post(t, "/v1/taxonomy/assignments/cross-preview", coll());
        post(t, "/v1/taxonomy/assignments/unassigned", coll());
        post(t, "/v1/taxonomy/purge_collection", coll());
        post(t, "/v1/taxonomy/meta/record", coll());
        get(t, "/v1/taxonomy/meta/last_count", coll());
        post(t, "/v1/taxonomy/meta/last_discover_batch", many());
        get(t, "/v1/taxonomy/top_topics", coll());
        get(t, "/v1/taxonomy/chunk_grounded", one("source_collection", "source_collection_id"));
        get(t, "/v1/taxonomy/audit", coll());
        post(t, "/v1/taxonomy/topics/persist_split", one("collection_name", "collection_id"));
        get(t, "/v1/taxonomy/rebuild/old_state", coll());
        post(t, "/v1/taxonomy/topics/persist_rebuild", coll());
        post(t, "/v1/taxonomy/topics/persist_discovered", coll());
        post(t, "/v1/taxonomy/centroids/upsert", nested("records", "collection", "collection_id"));
        post(t, "/v1/taxonomy/centroids/query", coll());
        get(t, "/v1/taxonomy/centroids/count", coll());
        get(t, "/v1/taxonomy/centroids/by_collection", coll());
        get(t, "/v1/taxonomy/centroids/foreign", coll());
        post(t, "/v1/taxonomy/centroids/delete", coll());
        post(t, "/v1/taxonomy/centroids/purge", coll());
        get(t, "/v1/aspects/get", coll());
        get(t, "/v1/aspects/list_by_collection", coll());
        post(t, "/v1/aspects/delete", coll());
        post(t, "/v1/aspects/salient_sentences/set_by_key", coll());
        post(t, "/v1/aspects/upsert", coll());
        post(t, "/v1/aspects/highlights/upsert", coll());
        get(t, "/v1/chash/count_for_collection", coll());
        get(t, "/v1/chash/registered_chashes", coll());
        get(t, "/v1/remap/entries", one("source_collection", "source_collection_id"));
        get(t, "/v1/remap/count", one("source_collection", "source_collection_id"));
        post(t, "/v1/remap/clear_leg", one("source_collection", "source_collection_id"), one("target_collection", "target_collection_id"));
        post(t, "/v1/remap/record_batch", one("source_collection", "source_collection_id"));
        post(t, "/v1/pipeline/create", coll());
        post(t, "/v1/pipeline/delete_collection", coll());
        post(t, "/v1/telemetry/relevance/log", coll());
        get(t, "/v1/telemetry/search/stats", coll());
        post(t, "/v1/telemetry/hook_failures/record", coll());
        return t;
    }
}
