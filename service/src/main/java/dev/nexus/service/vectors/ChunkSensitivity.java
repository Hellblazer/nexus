// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.impl.DSL;

import java.util.Collection;
import java.util.HashSet;
import java.util.HexFormat;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_SENSITIVE_MANY;

/**
 * The one spelling, for the jOOQ reads, of RDR-243's chunk sensitivity (nexus-rfvyy.7.4): a chunk is sensitive when ANY
 * LIVE owner of it is (a tombstoned owner counts for nothing), the rule {@code nexus.chunk_sensitive} runs and the
 * search functions of {@code vectors-055} write out inline.
 *
 * <p>Three things live here so that the get-by-id reads, the by-address catalog reads and the search hits share them:
 * the request value ({@link #excludeFlag}), the exclusion predicate ({@link #notSensitive}) and the batched flag
 * lookup ({@link #sensitiveChashes}).
 */
public final class ChunkSensitivity {

    private ChunkSensitivity() { }

    /**
     * The value of a request's optional {@code sensitivity}: {@code "exclude"} is true; absent or JSON null is false
     * (the read as it always was); anything else, another string, a number, a boolean or an array, is an {@link
     * IllegalArgumentException} naming the field (a 400): a typo must never silently include the rows the caller
     * meant to keep out.
     */
    public static boolean excludeFlag(Object val) {
        if (val == null) return false;
        if (val instanceof String s && s.equals("exclude")) return true;
        throw new IllegalArgumentException(
            "field 'sensitivity' must be \"exclude\" or absent, got " + (val instanceof String ? "\"" + val + "\"" : val));
    }

    /**
     * {@code NOT EXISTS} a live sensitive owner of the chunk at {@code (tenantId, collection, chash)}, to sit in the
     * WHERE of the read itself, so a page, its offset and its count are all taken over the rows that survive. The
     * join is the one {@code nexus.chunk_sensitive} runs, written out because the planner cannot inline a scalar SQL
     * function whose body holds a subquery.
     *
     * @param chash the chunk row's raw {@code bytea} chash column (not the hex-converted field)
     */
    public static Condition notSensitive(Field<String> tenantId, Field<String> collection, Field<byte[]> chash) {
        var m = CATALOG_DOCUMENT_CHUNKS;
        var d = CATALOG_DOCUMENTS;
        return DSL.notExists(DSL.selectOne().from(m)
            .join(d).on(d.TENANT_ID.eq(m.TENANT_ID).and(d.TUMBLER.eq(m.DOC_ID)))
            .where(m.TENANT_ID.eq(tenantId))
            .and(m.COLLECTION.eq(collection))
            .and(m.CHASH.eq(chash))
            .and(d.DELETED_AT.isNull())
            .and(d.SENSITIVE));
    }

    /**
     * The chunks among {@code chashHexes} of {@code collection} that have a live sensitive owner, as lowercase hex:
     * ONE {@code nexus.chunk_sensitive_many} call, whatever the number of chashes, in the caller's tenant-scoped
     * transaction.
     */
    public static Set<String> sensitiveChashes(DSLContext ctx, String tenant, String collection,
                                               Collection<String> chashHexes) {
        Set<String> out = new HashSet<>();
        if (chashHexes.isEmpty()) return out;
        byte[][] chashes = chashHexes.stream().map(h -> HexFormat.of().parseHex(h)).toArray(byte[][]::new);
        var many = CHUNK_SENSITIVE_MANY.call(tenant, collection, chashes);
        for (Record r : ctx.selectFrom(many).fetch()) {
            if (Boolean.TRUE.equals(r.get(many.SENSITIVE))) {
                out.add(HexFormat.of().formatHex(r.get(many.CHASH)));
            }
        }
        return out;
    }
}
