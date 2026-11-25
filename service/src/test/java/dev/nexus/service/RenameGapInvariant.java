// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23): the invariant the rename-in-the-gap tests read across the whole nexus schema, shared by
 * {@code CollectionRoutesByIdTest} (the generic routes) and {@code HeavyRoutesRenameGapTest} (the routes that need a document, a chunk, a
 * job or a pipeline run first). Every (name column, id column) pair of the schema ({@code collection}/{@code collection_id},
 * {@code source_collection}/{@code source_collection_id}, ...) is found from the catalog's own metadata, so a writer that stamps the label
 * it was given is found wherever it writes, not per route.
 */
final class RenameGapInvariant {

    private RenameGapInvariant() {
    }

    /** The (table, name column, id column) triples of the schema, found from the catalog's metadata. */
    static List<String[]> nameIdPairs(CollectionRouteFixture f) {
        return f.asSuperuser(ctx -> {
            List<String[]> pairs = new ArrayList<>();
            ctx.meta().filterSchemas(sc -> sc.getName().equals("nexus")).getTables().forEach(t -> {
                for (var field : t.fields()) {
                    if (field.getType() == String.class && t.field(field.getName() + "_id") != null
                        && t.field(field.getName() + "_id").getType() == Long.class
                        && (field.getName().equals("collection") || field.getName().endsWith("_collection") || field.getName().equals("name"))) {
                        pairs.add(new String[] {t.getName(), field.getName(), field.getName() + "_id"});
                    }
                }
            });
            return pairs;
        });
    }

    /**
     * Rows anywhere in the schema carrying one of the renamed ids beside a label that is not that row's current label, and (when
     * {@code checkNewHolders}) rows carrying the id of a new holder of an old label. Returns {@code [rows that carry a renamed id]}: the
     * writes that landed somewhere. {@code skipTables} names tables whose rows the test itself stored under the old label by construction
     * (a topic seeded before the rename), so their stale name is the setup and not a finding.
     */
    static int[] findings(CollectionRouteFixture f, Map<Long, String> currentLabelOf, Set<Long> newHolders, List<String> violations,
                          Set<String> skipTables, boolean checkNewHolders) {
        int[] counts = new int[1];
        f.asSuperuser(ctx -> {
            for (String[] pair : nameIdPairs(f)) {
                if (pair[0].equals("catalog_collections") || skipTables.contains(pair[0])) {
                    continue;   // the registry itself: the rename in place is the setup
                }
                var table = org.jooq.impl.DSL.table(org.jooq.impl.DSL.name("nexus", pair[0]));
                var name = org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("nexus", pair[0], pair[1]), String.class);
                var id = org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("nexus", pair[0], pair[2]), Long.class);
                if (checkNewHolders) {
                    int holders = ctx.fetchCount(table, id.in(newHolders));
                    if (holders > 0) {
                        violations.add(pair[0] + "." + pair[2] + ": " + holders + " row(s) carry the id of the NEW holder of the old label");
                    }
                }
                for (var e : currentLabelOf.entrySet()) {
                    counts[0] += ctx.fetchCount(table, id.eq(e.getKey()));
                    int stale = ctx.fetchCount(table, id.eq(e.getKey()).and(name.isDistinctFrom(e.getValue())));
                    if (stale > 0) {
                        violations.add(pair[0] + "." + pair[1] + ": " + stale + " row(s) carry id " + e.getKey() + " beside a label that is not "
                            + e.getValue() + ", its current one");
                    }
                }
            }
            return null;
        });
        return counts;
    }
}
