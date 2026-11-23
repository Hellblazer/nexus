// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * RDR-229 P3.5a (bead nexus-p2m1x.23), linda's ruling D1: the document routes are collection routes. A request that writes or
 * moves a document names the document's collection by label ({@code physical_collection}) or by registry id
 * ({@code physical_collection_id}), through the same registry the collection routes resolve through
 * ({@link CollectionRegistry#resolveId}).
 *
 * <p><b>Where the id is resolved, and why not at the route boundary.</b> {@code CollectionSelectorFilter} turns an id into a label
 * before the handler runs, and the repository then looks the label up again by name. For a document write that is the window
 * the review named (F4): a rename and a re-registration of the old label between the two reads hand the write to another
 * collection. Here the id is resolved inside the write's own transaction, to the registry row, and the row's id is what the
 * statement stamps into {@code catalog_documents.physical_collection_id}; the label of that row is what it writes beside it.
 * Nothing reads the name back.
 *
 * <p><b>The contract</b> (additive: a request that names a label only is answered as before):
 * <ul>
 *   <li>a label and an id for one collection is {@link IllegalArgumentException}, the ladder's 400 (an empty label counts as a
 *       label), and so is an id that is not a JSON integer, an explicit {@code null} included;</li>
 *   <li>an id no row has is {@link UnknownCollectionIdException}, the typed 404 {@code unknown_collection_id};</li>
 *   <li>a retired row's id is {@link CollectionRetiredIdException}, the typed 409 {@code collection_superseded}, the same refusal a
 *       retired id gets on the collection routes (ruling D3), naming the successor by {@code superseded_by_id}
 *       ({@link CollectionSuccessor}) in the body {@code CollectionSupersededBody} builds for every retired id;</li>
 *   <li>a label with no row keeps the typed 422 {@code unregistered_collection}, raised by {@link CollectionIds#require}.</li>
 * </ul>
 *
 * <p><b>The resolved id travels as a {@link Resolved}</b>, a Java object a JSON body cannot carry, stored in the request map under
 * the id field in place of the client's number. {@link #idOf} trusts only that, so a number that reached a write without passing
 * {@link #normalize} is ignored and the label decides, as before; it can never name a row nobody checked. Every public entry
 * point that takes a document map normalizes it first, in the transaction that writes.
 */
final class DocumentCollectionRef {

    /** The label field of a document request, and of a document row. */
    static final String LABEL = "physical_collection";

    /** The id field of a document request; additive beside {@link #LABEL} (RDR-229 P3.5a, ruling D1). */
    static final String ID = "physical_collection_id";

    private DocumentCollectionRef() {}

    /** The registry id {@link #normalize} resolved for a request, in place of the client's number. */
    record Resolved(long id) {}

    /**
     * The shape check of a request, with no database read: refuses a label and an id together, and an id that is not a JSON
     * integer. Run by the handlers that write a second row before the document (the legacy register writes the owner first), so
     * a refused request leaves nothing behind; {@link #normalize} runs it again in the transaction.
     */
    static void check(Map<String, Object> request) {
        if (!request.containsKey(ID) || request.get(ID) instanceof Resolved) {
            return;
        }
        Object id = request.get(ID);
        if (!(id instanceof Integer || id instanceof Long)) {
            throw new IllegalArgumentException("'" + ID + "' must be a JSON integer");
        }
        if (request.get(LABEL) != null) {
            throw new IllegalArgumentException("give either '" + LABEL + "' or '" + ID + "' for the collection, not both");
        }
    }

    /**
     * {@code request} with its id resolved to the registry row, in {@code ctx}'s transaction: the same map when it names no id
     * (a label-only request is not touched), else a copy whose {@link #LABEL} is the row's label and whose {@link #ID} is a
     * {@link Resolved}.
     *
     * @throws IllegalArgumentException      both fields, or an id that is not a JSON integer
     * @throws UnknownCollectionIdException  no row of the tenant has the id
     * @throws CollectionRetiredIdException  the row is retired
     */
    static Map<String, Object> normalize(DSLContext ctx, String tenant, Map<String, Object> request) {
        return normalize(ctx, tenant, request, null);
    }

    /** {@link #normalize} for a batch: each distinct id is resolved once, in {@code memo}. */
    static List<Map<String, Object>> normalizeAll(DSLContext ctx, String tenant, List<Map<String, Object>> requests) {
        Map<Long, CollectionRow> memo = new HashMap<>();
        List<Map<String, Object>> out = new ArrayList<>(requests.size());
        for (Map<String, Object> request : requests) {
            out.add(normalize(ctx, tenant, request, memo));
        }
        return out;
    }

    private static Map<String, Object> normalize(DSLContext ctx, String tenant, Map<String, Object> request,
                                                 Map<Long, CollectionRow> memo) {
        if (!request.containsKey(ID) || request.get(ID) instanceof Resolved) {
            return request;
        }
        check(request);
        long id = ((Number) request.get(ID)).longValue();
        CollectionRow row = memo == null ? resolve(ctx, tenant, id) : memo.computeIfAbsent(id, k -> resolve(ctx, tenant, k));
        Map<String, Object> out = new LinkedHashMap<>(request);
        out.put(LABEL, row.label());
        out.put(ID, new Resolved(row.collectionId()));
        return out;
    }

    private static CollectionRow resolve(DSLContext ctx, String tenant, long id) {
        CollectionRow row = CollectionRegistry.shared().resolveId(ctx, tenant, id);
        if (row.isSuperseded()) {
            throw new CollectionRetiredIdException(tenant, id, row.label(), CollectionSuccessor.of(ctx, tenant, id));
        }
        return row;
    }

    /**
     * The registry id a document write stamps beside its label: the resolved row's when the request named an id, else the id of
     * the row the label names. {@code null} for no collection ({@code ''} or absent), as {@link CollectionIds#require} gives.
     *
     * @throws UnregisteredCollectionException a label that no row has
     */
    static Long idOf(DSLContext ctx, String tenant, Map<String, Object> request) {
        return request.get(ID) instanceof Resolved r ? Long.valueOf(r.id())
            : CollectionIds.require(ctx, tenant, CatalogRepository.s(request, LABEL));
    }

    /** {@link #idOf} for a caller that has already read the label from the request (the update, which writes it under any JSON type). */
    static Long idOf(DSLContext ctx, String tenant, Map<String, Object> request, String label) {
        return request.get(ID) instanceof Resolved r ? Long.valueOf(r.id()) : CollectionIds.require(ctx, tenant, label);
    }

    /** {@link #idOf} for a batch that names the same collections many times: a label is looked up once, in {@code memo}. */
    static Long idOf(DSLContext ctx, String tenant, Map<String, Object> request, Map<String, Long> memo) {
        return request.get(ID) instanceof Resolved r ? Long.valueOf(r.id())
            : CatalogIdStamps.requireMemo(ctx, tenant, CatalogRepository.s(request, LABEL), memo);
    }
}
