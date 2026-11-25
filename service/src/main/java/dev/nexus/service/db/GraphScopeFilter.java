// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;

import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * The scope predicate {@code POST /v1/catalog/traverse} applies to a walk BEFORE its
 * {@link CatalogRepository#graphBFS} node cut (RDR-232 section 3, nexus-jypmj.1.13).
 *
 * <p>Without it the engine keeps the 500 lowest {@code (min_depth, tumbler)} nodes of the whole reachable
 * set, and a scoped {@code nx_answer} then receives a response in which out-of-scope documents (code, mostly)
 * have already taken the slots in-scope evidence needed. The client used to drop the out-of-scope nodes AFTER
 * that cut, which cannot give the slots back.
 *
 * <p>The predicate is one of two shapes, both exact and neither a parse of a collection NAME (RDR-204: the
 * engine reads a document's {@code physical_collection} as a row value and compares it, never splits it):
 * <ul>
 *   <li>{@code {"collections": ["name", ...]}}: the allowed set. A document is inside when its
 *       {@code physical_collection} is a member. The client resolves a corpus prefix or a single collection
 *       to the registered names it admits and sends the set, so the prefix rule stays in one place (the
 *       client's {@code ScopeConstraint.admits}). An empty set is legal and admits no collection: the scope
 *       resolved to nothing, so only seeds survive;</li>
 *   <li>{@code {"subtree": "1.2"}}: the document's tumbler is the value or lies under it ({@code 1.2} admits
 *       {@code 1.2.9}, not {@code 1.20.9}).</li>
 * </ul>
 * "No scope" and {@code all} are not filters: the client sends none. A document with no collection is
 * outside a collection set ("cannot tell" is not "inside").
 *
 * <p><b>What a collection set matches (RDR-229 P3.5a, linda's follow-up to the census).</b> The labels on the wire stay labels. Before
 * gate 3 a document is inside when its stored {@code physical_collection} is one of them. From gate 3 the stored name is a
 * stale-name reader: after a one-row rename a document keeps the old name, and a filter naming the new label would miss it. So
 * {@link CatalogRepository#graphBFS} resolves each label to its registry id ({@link #idsOf}, which orders by
 * {@code CollectionRegistry.labelResolutionOrder}, a live row before a retired one) and matches the document's
 * {@code physical_collection_id}; a label no row has names no collection.
 */
public final class GraphScopeFilter {

    /** The most collection names one request may name; bounded like the other caller-supplied lists. */
    public static final int MAX_COLLECTIONS = 2000;

    // Sorted, so `admits` is a binary search. An array rather than a Set: this holds one request's predicate,
    // not tenant-keyed state, and TenantKeyedStateGateTest lists every Map/Set/List field in src/main.
    private final String[] collections;   // null for a subtree filter
    private final String subtree;         // null for a collection-set filter

    private GraphScopeFilter(String[] collections, String subtree) {
        this.collections = collections;
        this.subtree = subtree;
    }

    /** A collection-set filter; the set may be empty (admits no collection). */
    public static GraphScopeFilter ofCollections(Set<String> names) {
        if (names == null) throw new IllegalArgumentException("'scope_filter.collections' must be a list of strings");
        if (names.size() > MAX_COLLECTIONS) {
            throw new IllegalArgumentException("'scope_filter.collections' names " + names.size()
                + " collections (max " + MAX_COLLECTIONS + ")");
        }
        String[] sorted = names.toArray(String[]::new);
        java.util.Arrays.sort(sorted);
        return new GraphScopeFilter(sorted, null);
    }

    /** A tumbler-subtree filter; {@code prefix} must be digits and dots. */
    public static GraphScopeFilter ofSubtree(String prefix) {
        if (prefix == null || !prefix.strip().matches("\\d+(\\.\\d+)*")) {
            throw new IllegalArgumentException("'scope_filter.subtree' must be a tumbler prefix such as 1.2");
        }
        return new GraphScopeFilter(null, prefix.strip());
    }

    /**
     * The request's optional {@code scope_filter} object, or {@code null} when absent.
     *
     * @throws IllegalArgumentException (mapped to 400) when it is present and is not an object holding
     *                                  exactly one of {@code collections} (a list of non-blank strings) and
     *                                  {@code subtree}
     */
    public static GraphScopeFilter parse(Object raw) {
        if (raw == null) return null;
        if (!(raw instanceof Map<?, ?> m)) {
            throw new IllegalArgumentException("'scope_filter' must be an object {collections: [...]} or {subtree: \"1.2\"}");
        }
        boolean hasCollections = m.containsKey("collections");
        boolean hasSubtree = m.containsKey("subtree");
        if (hasCollections == hasSubtree) {
            throw new IllegalArgumentException("'scope_filter' must hold exactly one of 'collections' and 'subtree'");
        }
        if (hasSubtree) {
            return ofSubtree(m.get("subtree") instanceof String s ? s : null);
        }
        if (!(m.get("collections") instanceof List<?> list)) {
            throw new IllegalArgumentException("'scope_filter.collections' must be a list of strings");
        }
        Set<String> names = new LinkedHashSet<>();
        for (Object o : list) {
            if (!(o instanceof String s) || s.isBlank()) {
                throw new IllegalArgumentException("'scope_filter.collections' must be a list of non-blank strings");
            }
            names.add(s);
        }
        return ofCollections(names);
    }

    /**
     * The registry ids of a collection-set filter's labels, in {@code ctx}'s transaction; {@code null} for a subtree filter. A label
     * no row of the tenant has contributes none (it admits nothing), and a label held by several rows takes the first in
     * {@code labelResolutionOrder}.
     */
    Set<Long> idsOf(DSLContext ctx, String tenant) {
        if (subtree != null) {
            return null;
        }
        Set<Long> ids = new HashSet<>();
        for (String label : collections) {
            try {
                ids.add(CollectionRegistry.shared().resolveLabel(ctx, tenant, label).collectionId());
            } catch (UnregisteredCollectionException e) {
                // a label with no row names no collection
            }
        }
        return ids;
    }

    /**
     * {@link #admits(String, String)} from gate 3: a collection set matches the document's {@code collectionId} against
     * {@code ids} (from {@link #idsOf}), not its stored name; a subtree filter is as before. {@code ids == null} is the name match.
     */
    boolean admits(String collection, Long collectionId, Set<Long> ids, String tumbler) {
        if (subtree == null && ids != null) {
            return collectionId != null && ids.contains(collectionId);
        }
        return admits(collection, tumbler);
    }

    /** Whether a document in {@code collection} (catalog address {@code tumbler}) is inside the scope. */
    public boolean admits(String collection, String tumbler) {
        if (subtree != null) {
            return tumbler != null && (tumbler.equals(subtree) || tumbler.startsWith(subtree + "."));
        }
        return collection != null && java.util.Arrays.binarySearch(collections, collection) >= 0;
    }

    /** The scoped-result marker: what ran, and how many nodes it removed. */
    Map<String, Object> marker(int droppedOutOfScope) {
        Map<String, Object> marker = new LinkedHashMap<>();
        marker.put("applied", true);
        if (subtree != null) {
            marker.put("kind", "subtree");
            marker.put("subtree", subtree);
        } else {
            marker.put("kind", "collections");
            marker.put("collection_count", collections.length);
        }
        marker.put("dropped_out_of_scope", droppedOutOfScope);
        return marker;
    }
}
