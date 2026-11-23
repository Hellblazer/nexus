// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

import org.jooq.DSLContext;

import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * RDR-229 P3.5c, G17 (linda 2026-11-22/23): a manifest row names its chunk through {@code fk_catalog_chunks_chunk}, whose columns
 * are the chunk's own stored {@code (tenant, collection, chash, model)}. After a one-row rename (P3.7) a chunk keeps the name it
 * was written under, while the caller's collection label is the registry's new one. A child-row writer therefore takes the name
 * column from the referenced chunk row, found by the collection's id, never from the request's label. A chash with no chunk row
 * of that collection (a manifest row written ahead of its chunk) keeps the caller's label.
 */
final class ManifestChunkNames {

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
        List<String> all = chashHexes.stream().filter(h -> h != null && !h.isBlank()).map(h -> h.toLowerCase(Locale.ROOT)).distinct().toList();
        for (int i = 0; i < all.size(); i += BATCH) {
            ctx.select(hex, CHUNKS.COLLECTION).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION_ID.eq(collectionId))
                    .and(hex.in(all.subList(i, Math.min(i + BATCH, all.size())))))
                .fetch().forEach(r -> out.putIfAbsent(r.value1(), r.value2()));
        }
        return out;
    }

    /** The name column of the manifest row for {@code chashHex}: the chunk's stored name when {@code names} has it, else {@code label}. */
    static String nameFor(Map<String, String> names, String chashHex, String label) {
        String stored = chashHex == null ? null : names.get(chashHex.toLowerCase(Locale.ROOT));
        return stored != null ? stored : label;
    }
}
