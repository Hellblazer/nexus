// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.Field;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

/**
 * RDR-229 P3.11 (nexus-p2m1x.31): one restore's map from the backup's {@code collection_id} to the id the restore gave that
 * registry row, source id to target id, held as a session temp table so a map of tens of thousands of collections is joined in
 * SQL and never copied into the heap. It is built once, by {@link RestoreIdFill#fill} of {@code catalog_collections}, from the
 * registry stage's own pair of ids (the file's, saved before the engine's sequence overwrites them, and the drawn ones), and
 * read by every later table's fill, the tombstones' second pass and the quarantine tag. {@link #empty()} is the map before the
 * registry has loaded: it maps nothing. The temp table is {@code ON COMMIT DROP}, like the stages.
 *
 * <p>A source id held by more than one registry row of the file is ambiguous and is left out: no row can follow it. Whether a
 * file's id maps is therefore decided by this table alone.
 *
 * @param table the temp table's name; {@code null} for the empty map
 */
record CollectionIdMap(String table) {

    /** The temp table's name: one map per load, on the load's one session. */
    static final String TABLE = "nx_restore_cid_map";
    static final String SOURCE = "src_id";
    static final String TARGET = "dst_id";

    static CollectionIdMap empty() {
        return new CollectionIdMap(null);
    }

    boolean isEmpty() {
        return table == null;
    }

    /** A scalar subselect: the target id of {@code source}, NULL when it is NULL or the map holds no such source id. */
    Field<Long> target(Field<Long> source) {
        if (isEmpty()) return DSL.castNull(SQLDataType.BIGINT);
        Table<?> m = DSL.table(DSL.name(table)).as("cidmap");
        return DSL.field(DSL.select(DSL.field(DSL.name("cidmap", TARGET), SQLDataType.BIGINT)).from(m)
            .where(DSL.field(DSL.name("cidmap", SOURCE), SQLDataType.BIGINT).eq(source)));
    }
}
