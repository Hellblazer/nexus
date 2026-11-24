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

    /**
     * Where the collection fields of a route travel. {@link #EITHER}: the route answers a GET, whose fields are in the query string,
     * and a POST, whose fields are in the body (the pipeline's {@code /pages} and {@code /chunks}); the method says which.
     */
    enum Where { QUERY, BODY, EITHER }

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
     * @param passesResolvedId the route's handler reads the id the boundary resolved for its FIRST slot ({@link CollectionSelectorFilter#RESOLVED_ID}),
     *                    not only the label: the retiring verbs, which read and lock the source by that id in their own transaction
     *                    ({@link dev.nexus.service.db.CollectionRefusals#requireSourceById}), the delete also to remove the one row of
     *                    an id when a label is held by several. A supersede's SECOND slot, its successor, is passed the same way
     *                    ({@link CollectionSelectorFilter#RESOLVED_SUCCESSOR_ID}) and read and locked by that id too. Every route answers a
     *                    retired row's id 409 {@code collection_superseded}, these included
     * @param writes      the route writes under the collection it names, so a request by id must read that id in the write's own
     *                    transaction ({@link dev.nexus.service.db.ExpectedCollectionIds}); the filter reports a 2xx answer that did not
     */
    record Route(Where where, List<Slot> slots, boolean passesResolvedId, boolean writes) {
    }

    /**
     * The routes that write under the collection they name, and must read a named id in their own transaction. The retiring verbs
     * (supersede, rename, delete) are here: their read is {@link dev.nexus.service.db.CollectionRefusals#requireSourceById}, a
     * {@code FOR UPDATE} by id that counts as the read ({@link dev.nexus.service.db.ExpectedCollectionIds}), so a handler that stops
     * passing the id it was given answers 2xx with the id unconsumed and the filter reports it. A re-home is here too: it retires
     * nothing, it moves rows between two live collections, so it reads both ends FOR KEY SHARE like any writer.
     */
    static final java.util.Set<String> WRITES = java.util.Set.of(
        "/v1/catalog/collections/supersede", "/v1/catalog/collections/rename", "/v1/catalog/collections/delete",
        "/v1/catalog/collections/rehome",
        "/v1/catalog/manifest/write", "/v1/catalog/manifest/append", "/v1/catalog/manifest/append_many",
        "/v1/catalog/manifest/write_many", "/v1/catalog/import/chunk",
        "/v1/vectors/upsert-chunks", "/v1/vectors/store-delete", "/v1/vectors/update-metadata",
        "/v1/taxonomy/topics/insert", "/v1/taxonomy/assignments/assign", "/v1/taxonomy/assignments/assign_from_chashes",
        "/v1/taxonomy/purge_collection", "/v1/taxonomy/meta/record", "/v1/taxonomy/topics/persist_split",
        "/v1/taxonomy/topics/persist_rebuild", "/v1/taxonomy/topics/persist_discovered", "/v1/taxonomy/centroids/upsert",
        "/v1/taxonomy/centroids/delete", "/v1/taxonomy/centroids/purge",
        "/v1/aspects/delete", "/v1/aspects/salient_sentences/set_by_key", "/v1/aspects/upsert", "/v1/aspects/highlights/upsert",
        "/v1/remap/clear_leg", "/v1/remap/record_batch", "/v1/pipeline/create", "/v1/pipeline/delete_collection",
        "/v1/telemetry/relevance/log", "/v1/telemetry/hook_failures/record",
        // RDR-229 P3.5a W2 (nexus-p2m1x.23): routes the hand-kept list had missed. Each reads the named id in its own write transaction
        "/v1/catalog/collections/set_aspects_enabled", "/v1/vectors/reembed/jobs",
        "/v1/vectors/gc/quarantine-orphans", "/v1/vectors/gc/restore-rereferenced", "/v1/vectors/gc/expire-quarantine",
        "/v1/vectors/gc/quarantine-restore");

    /**
     * The POST routes that name a collection and only read: nothing is written under the collection or anywhere, so a request by id
     * has no write to guard. {@code CollectionRouteClassificationTest} holds every POST and DELETE route of the table to be in exactly
     * one of {@link #WRITES}, this set and {@link #EXEMPT}.
     */
    static final java.util.Set<String> READS = java.util.Set.of(
        "/v1/catalog/collections/rehome/status",
        "/v1/vectors/search", "/v1/vectors/query", "/v1/vectors/search-per-collection", "/v1/vectors/hybrid-search",
        "/v1/vectors/search-metadata-scoped", "/v1/vectors/search-topic-scoped", "/v1/vectors/search-graph-hop",
        "/v1/vectors/search-aspect-scoped", "/v1/vectors/get", "/v1/vectors/get-all-metadata", "/v1/vectors/store-get",
        "/v1/vectors/get-embeddings", "/v1/vectors/store-list", "/v1/vectors/embed", "/v1/vectors/manifest-less-census",
        "/v1/vectors/reapable", "/v1/vectors/reembed/plan",
        "/v1/taxonomy/assignments/cross-preview", "/v1/taxonomy/assignments/unassigned",
        "/v1/taxonomy/meta/last_discover_batch", "/v1/taxonomy/centroids/query");

    /**
     * The read routes that refuse a request by LABEL whose collection is retired and names no live successor (RDR-229 R7, bead
     * nexus-p2m1x.25): the routes that return the collection's content, a search, a get, a list, the topics, the aspects. They answer the 409
     * {@code collection_superseded} the same request by id gets ({@link RetiredLabelReadGuard}).
     */
    static final java.util.Set<String> RETIRED_LABEL_REFUSED = java.util.Set.of(
        "/v1/vectors/search", "/v1/vectors/query", "/v1/vectors/search-per-collection", "/v1/vectors/hybrid-search",
        "/v1/vectors/search-metadata-scoped", "/v1/vectors/search-topic-scoped", "/v1/vectors/search-graph-hop",
        "/v1/vectors/search-aspect-scoped", "/v1/vectors/get", "/v1/vectors/get-all-metadata", "/v1/vectors/store-get",
        "/v1/vectors/get-embeddings", "/v1/vectors/store-list",
        "/v1/taxonomy/topics", "/v1/taxonomy/topics/unreviewed", "/v1/taxonomy/topics/resolve", "/v1/taxonomy/top_topics",
        "/v1/taxonomy/chunk_grounded", "/v1/taxonomy/audit", "/v1/taxonomy/rebuild/old_state",
        "/v1/taxonomy/assignments/cross-preview", "/v1/taxonomy/assignments/unassigned",
        "/v1/taxonomy/centroids/query", "/v1/taxonomy/centroids/by_collection",
        "/v1/aspects/get", "/v1/aspects/list_by_collection");

    private static final String REGISTRY_ROW = "it answers the registry row itself or its state (the way a client learns a collection is retired), "
        + "which a doctor, a rename, a re-home and a restore read for a tombstone";
    private static final String CATALOG_BOOKKEEPING = "a catalog document, manifest or audit read that the delete, re-home, restore and doctor flows run over "
        + "a retired collection; not one of the reader classes R7 names (vector search and get, store list, topics, aspects). A candidate for the next cut";
    private static final String COUNT = "it counts or bookkeeps, and a doctor counts a tombstone's rows";

    /**
     * Every other read route of the table, with the reason a request by label may still read a retired collection. Together with
     * {@link #RETIRED_LABEL_REFUSED} it is every route that is neither a {@link #WRITES} route nor {@link #EXEMPT}
     * ({@code CollectionRouteClassificationTest}), so a new read route must say which it is.
     */
    static final Map<String, String> RETIRED_LABEL_READABLE = readable();

    private static Map<String, String> readable() {
        Map<String, String> m = new LinkedHashMap<>();
        for (String route : List.of("/v1/catalog/collections/get", "/v1/catalog/collections/health", "/v1/catalog/collections/owner-root")) {
            m.put(route, REGISTRY_ROW);
        }
        m.put("/v1/catalog/collections/rehome/status", "the status of a re-home, which a later retire of either end must not hide");
        for (String route : List.of("/v1/catalog/list", "/v1/catalog/resolve", "/v1/catalog/manifest/chashes", "/v1/catalog/resolve_span",
                "/v1/catalog/resolve_chash", "/v1/catalog/gc_audit/list", "/v1/chash/registered_chashes")) {
            m.put(route, CATALOG_BOOKKEEPING);
        }
        for (String route : List.of("/v1/vectors/count", "/v1/taxonomy/meta/last_count", "/v1/taxonomy/meta/last_discover_batch",
                "/v1/taxonomy/centroids/count", "/v1/chash/count_for_collection", "/v1/remap/count", "/v1/pipeline/counts",
                "/v1/telemetry/search/stats")) {
            m.put(route, COUNT);
        }
        m.put("/v1/vectors/embed", "it embeds the request's own texts with the collection's model and reads none of its rows");
        m.put("/v1/vectors/manifest-less-census", "a maintenance census an operator runs over a collection of any state");
        m.put("/v1/vectors/reapable", "a maintenance census an operator runs over a collection of any state");
        m.put("/v1/taxonomy/centroids/foreign", "a maintenance census an operator runs over a collection of any state");
        m.put("/v1/vectors/reembed/plan", "the handler refuses a retired source or target in plain words, identically with the job start "
            + "(ReembedHandler, TargetSelection), so the plan and the start answer the same");
        m.put("/v1/remap/entries", "a remap's source is a retired collection by design: the entries are how a reader maps its old ids");
        m.put("/v1/pipeline/state", "the pdf pipeline's own work queue row, not the collection's content");
        return Map.copyOf(m);
    }

    private static final String HISTORY = "a history-table write (T1 852f4c0f): the id is stamped with an unlocked read and a row that "
        + "resolves to nothing is written with a NULL id, never refused";
    private static final String PIPELINE = "a pipeline-run write: it lands on the pdf_pipeline row it resolves by pipeline id or content hash "
        + "and on that row's own pages and chunks (a work queue); it writes no chunk, manifest or registry row of the collection";

    /**
     * The POST and DELETE routes that name a collection and are neither a {@link #WRITES} nor a {@link #READS} route, each with the
     * reason a request by id has nothing to guard there.
     */
    static final Map<String, String> EXEMPT = exempt();

    private static Map<String, String> exempt() {
        Map<String, String> e = new LinkedHashMap<>();
        e.put("/v1/catalog/gc_audit/record", HISTORY);
        e.put("/v1/catalog/index-run/begin", "it stamps the document row it names by tumbler; the collection field is only logged");
        e.put("/v1/catalog/index-run/begin-many", "it stamps the document rows it names by tumbler; the collection field is only logged");
        for (String op : List.of("progress", "extraction_meta", "complete", "fail", "pages", "chunks", "mark_uploaded",
                "reset_uploaded", "clear_wal", "delete")) {
            e.put("/v1/pipeline/" + op, PIPELINE);
        }
        return Map.copyOf(e);
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
        t.put(path, new Route(Where.QUERY, List.of(slots), false, false));
    }

    private static void post(Map<String, Route> t, String path, Slot... slots) {
        t.put(path, new Route(Where.BODY, List.of(slots), false, WRITES.contains(path)));
    }

    /** A retiring verb that reads its source by the id the boundary resolved (supersede, rename); the delete is {@link #delete}. */
    private static void retire(Map<String, Route> t, String path, Slot... slots) {
        t.put(path, new Route(Where.BODY, List.of(slots), true, WRITES.contains(path)));
    }

    private static void either(Map<String, Route> t, String path, Slot... slots) {
        t.put(path, new Route(Where.EITHER, List.of(slots), false, WRITES.contains(path)));
    }

    private static void delete(Map<String, Route> t, String path, Slot... slots) {
        t.put(path, new Route(Where.BODY, List.of(slots), true, WRITES.contains(path)));
    }

    private static Map<String, Route> table() {
        Map<String, Route> t = new LinkedHashMap<>();
        get(t, "/v1/catalog/collections/get", name());
        get(t, "/v1/catalog/collections/health", coll());
        get(t, "/v1/catalog/collections/owner-root", name());
        post(t, "/v1/catalog/collections/set_aspects_enabled", name());
        retire(t, "/v1/catalog/collections/supersede", name(), one("superseded_by", "superseded_by_id"));
        post(t, "/v1/catalog/collections/rehome", one("source", "source_id"), one("target", "target_id"));
        post(t, "/v1/catalog/collections/rehome/status", one("source", "source_id"), one("target", "target_id"));
        retire(t, "/v1/catalog/collections/rename", aliased(one("old_name", "collection_id"), "old"));
        delete(t, "/v1/catalog/collections/delete", aliased(name(), "collection"));
        get(t, "/v1/catalog/list", coll());
        get(t, "/v1/catalog/resolve", coll());
        get(t, "/v1/catalog/manifest/chashes", coll());
        get(t, "/v1/catalog/resolve_span", coll());
        get(t, "/v1/catalog/resolve_chash", one("prefer_collection", "prefer_collection_id"));
        get(t, "/v1/catalog/gc_audit/list", coll());
        post(t, "/v1/catalog/gc_audit/record", coll());
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
        post(t, "/v1/remap/record_batch", one("source_collection", "source_collection_id"), nested("entries", "target_collection", "target_collection_id"));
        post(t, "/v1/pipeline/create", coll());
        post(t, "/v1/pipeline/delete_collection", coll());
        get(t, "/v1/pipeline/state", coll());
        post(t, "/v1/pipeline/progress", coll());
        post(t, "/v1/pipeline/extraction_meta", coll());
        post(t, "/v1/pipeline/complete", coll());
        post(t, "/v1/pipeline/fail", coll());
        either(t, "/v1/pipeline/pages", coll());
        either(t, "/v1/pipeline/chunks", coll());
        post(t, "/v1/pipeline/mark_uploaded", coll());
        post(t, "/v1/pipeline/reset_uploaded", coll());
        get(t, "/v1/pipeline/counts", coll());
        post(t, "/v1/pipeline/clear_wal", coll());
        post(t, "/v1/pipeline/delete", coll());
        post(t, "/v1/telemetry/relevance/log", coll());
        get(t, "/v1/telemetry/search/stats", coll());
        post(t, "/v1/telemetry/hook_failures/record", coll());
        return t;
    }
}
