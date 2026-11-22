// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import org.jooq.DSLContext;

import java.util.List;

/**
 * RDR-229 P3.4 (nexus-p2m1x.22, linda's ruling 2026-11-22): the census of the supporting indexes of the id keys. An id key is a
 * top-level foreign key in schema {@code nexus} whose referencing columns include {@code collection_id} or
 * {@code source_collection_id}: P3.3a's seven twins to the registry (vectors-072), P3.3c's three child keys into the chunks id key
 * and its composite from chunks to the registry ({@link dev.nexus.service.vectors.Rdr229SwapMove}). The census is read from
 * {@code pg_constraint} and {@code pg_index}, never from a list, so a key added later is in it.
 *
 * <p><b>The rule.</b> A key is supported when its referencing table has a VALID, non-partial index whose first two columns are both
 * columns of the key and one of them is {@code tenant_id}. The registry keys pass through (tenant_id, &lt;id column&gt;)
 * (IdUniqueIndexPass's supporting indexes, or an arbiter that leads with those columns). The three keys into chunks pass through (tenant_id, chash) or (tenant_id, doc_id): their second column is not the
 * id, and that is enough because chash (doc_id holds the chash) is near-unique within a tenant, so the lookup a chunk delete or
 * update makes reads a handful of rows (Rdr229SwapMovesAfterTheSwapIntegrationTest shows each lookup on its index). The composite
 * passes through the chunks id unique key, which leads with (tenant_id, collection_id) (vectors-073). A partial index is not
 * counted: it cannot serve every lookup. Without a supporting index a registry-row delete scans the referencing table.
 *
 * <p>P4.1 refuses to drop the name keys while {@link #unsupported} is not empty (nexus-p2m1x.35): the name keys' own indexes
 * go with the name columns, and the id keys would be left with none.
 */
public final class IdKeyIndexCensus {

    /** The id columns that make a foreign key an id key. */
    public static final List<String> ID_COLUMNS = List.of("collection_id", "source_collection_id");

    /** One id key: its table, its name, and whether an index supports it. */
    public record Key(String table, String constraint, boolean supported) { }

    private static final String SQL = """
        SELECT r.relname AS tbl, c.conname AS con,
               EXISTS (SELECT 1 FROM pg_catalog.pg_index i
                        WHERE i.indrelid = c.conrelid AND i.indisvalid AND i.indpred IS NULL AND i.indnkeyatts >= 2
                          AND i.indkey[0] = ANY (c.conkey) AND i.indkey[1] = ANY (c.conkey)
                          AND (i.indkey[0] = t.attnum OR i.indkey[1] = t.attnum)) AS supported
          FROM pg_catalog.pg_constraint c
          JOIN pg_catalog.pg_class r ON r.oid = c.conrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = r.relnamespace
          LEFT JOIN pg_catalog.pg_attribute t ON t.attrelid = c.conrelid AND t.attname = 'tenant_id' AND NOT t.attisdropped
         WHERE n.nspname = 'nexus' AND c.contype = 'f' AND c.conparentid = 0
           AND EXISTS (SELECT 1 FROM pg_catalog.pg_attribute a
                        WHERE a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey) AND a.attname = ANY (?::text[]))
         ORDER BY r.relname, c.conname
        """;

    private IdKeyIndexCensus() { /* static */ }

    /** Every id key, in table then name order. A key on a table with no tenant_id column is listed and never supported. */
    public static List<Key> keys(DSLContext ctx) {
        return ctx.resultQuery(SQL, (Object) ID_COLUMNS.toArray(String[]::new))
            .fetch(r -> new Key(r.get("tbl", String.class), r.get("con", String.class), Boolean.TRUE.equals(r.get("supported", Boolean.class))));
    }

    /** The id keys no index supports: empty is the census passing. */
    public static List<Key> unsupported(DSLContext ctx) {
        return keys(ctx).stream().filter(k -> !k.supported()).toList();
    }
}
