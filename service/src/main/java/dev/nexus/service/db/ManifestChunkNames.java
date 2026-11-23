// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

import org.jooq.DSLContext;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

/**
 * RDR-229 P3.5c, G17 (linda 2026-11-22/23): a manifest row names its chunk through {@code fk_catalog_chunks_chunk}, whose columns
 * are the chunk's own stored {@code (tenant, collection, chash, model)}. After a one-row rename (P3.7) a chunk keeps the name it
 * was written under, while the caller's collection label is the registry's new one. A child-row writer therefore takes the name
 * column from the referenced chunk row, found by the collection's id, never from the request's label. A chash with no chunk row
 * of that collection (a manifest row written ahead of its chunk) keeps the caller's label.
 *
 * <p>A chash can have two chunk rows of one collection id only across embedding models (P3.3c's id key on chunks is
 * {@code (collection_id, chash, model)}); the choice between them is deterministic (linda 2026-11-23): the row whose stored name is the
 * collection's current registry label, else the lexicographically smallest name. Two rows of one chash AND one model are a defect
 * the id key forbids; they are logged at ERROR, never silently resolved.
 */
final class ManifestChunkNames {

    private static final Logger log = LoggerFactory.getLogger(ManifestChunkNames.class);
    private static final int BATCH = 500;

    private ManifestChunkNames() {
    }

    /** Lower-case chash hex to the stored name of the chunk row of {@code collectionId} (empty for a null id). */
    static Map<String, String> of(DSLContext ctx, String tenant, Long collectionId, List<String> chashHexes) {
        Map<String, String> out = new HashMap<>();
        if (collectionId == null) {
            return out;
        }
        var hex = ChashHex.hex(CHUNKS.CHASH);
        String current = ctx.select(CATALOG_COLLECTIONS.NAME).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(collectionId))
            .fetchOne(CATALOG_COLLECTIONS.NAME);
        List<String> all = chashHexes.stream().filter(h -> h != null && !h.isBlank()).map(h -> h.toLowerCase(Locale.ROOT)).distinct().toList();
        Set<String> seenWithModel = new HashSet<>();
        for (int i = 0; i < all.size(); i += BATCH) {
            for (var r : ctx.select(hex, CHUNKS.COLLECTION, CHUNKS.EMBEDDING_MODEL).from(CHUNKS)
                    .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION_ID.eq(collectionId))
                        .and(hex.in(all.subList(i, Math.min(i + BATCH, all.size())))))
                    .orderBy(hex, CHUNKS.COLLECTION, CHUNKS.EMBEDDING_MODEL).fetch()) {
                if (!seenWithModel.add(r.value1() + "|" + r.value3())) {
                    log.error("event=manifest_chunk_names_duplicate_within_model tenant={} collection_id={} chash={} model={} "
                        + "(the id key (collection_id, chash, model) forbids two rows)", tenant, collectionId, r.value1(), r.value3());
                }
                out.merge(r.value1(), r.value2(), (kept, next) -> prefer(current, kept, next));
            }
        }
        return out;
    }

    /** The registry's current label wins; otherwise the lexicographically smaller name (a total order, so the choice never depends on row order). */
    private static String prefer(String current, String a, String b) {
        if (a.equals(current)) return a;
        if (b.equals(current)) return b;
        return a.compareTo(b) <= 0 ? a : b;
    }

    /** The name column of the manifest row for {@code chashHex}: the chunk's stored name when {@code names} has it, else {@code label}. */
    static String nameFor(Map<String, String> names, String chashHex, String label) {
        String stored = chashHex == null ? null : names.get(chashHex.toLowerCase(Locale.ROOT));
        return stored != null ? stored : label;
    }
}
