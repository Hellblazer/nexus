// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.DSLContext;

import java.util.List;

/**
 * RDR-229 P3.3b (nexus-p2m1x.20): reads the unique id-key indexes of the two partitioned tables straight from the catalog, by
 * what they index and never by name, so a test of the pass and a test of the leaf helper agree on what "the id unique index"
 * is. The keys are the RDR's (item 6, "Switch"): {@code nexus.chunks (tenant_id, collection_id, chash, embedding_model)} and
 * {@code nexus.taxonomy_centroids (tenant_id, collection_id, topic_id, embedding_model)}, beside the name primary keys
 * ({@code collection} in place of {@code collection_id}), which are not matched. Both columns exist since P3.1
 * ({@code vectors-068}); P3.3a (nexus-p2m1x.19) adds foreign-key twins, not these key columns.
 */
public final class IdKeyIndexProbe {

    private IdKeyIndexProbe() { /* static */ }

    /** A table and the columns of its id key. */
    public record Key(String table, String columns) {
        String indexdefSuffix() {
            return "(" + columns + ")";
        }
    }

    public static final Key CHUNKS = new Key("chunks", "tenant_id, collection_id, chash, embedding_model");
    public static final Key CENTROIDS = new Key("taxonomy_centroids", "tenant_id, collection_id, topic_id, embedding_model");
    public static final List<Key> KEYS = List.of(CHUNKS, CENTROIDS);

    /**
     * One unique index on the key. {@code level} is the relation's depth in the partition tree: 0 the parent, 1 a model
     * partition, 2 a tenant leaf. {@code constraint} is the UNIQUE constraint the index backs, or null for a bare index.
     * {@code parentIndex} is the index this one is attached to ({@code pg_inherits}), or null.
     */
    public record Idx(String relation, int level, boolean leaf, String index, long indexOid, boolean valid,
                      String constraint, String parentIndex) { }

    /** Every unique index on {@code key} in the table's partition tree, parent first. */
    public static List<Idx> indexes(DSLContext ctx, Key key) {
        return ctx.fetch("""
                SELECT pt.relid::regclass::text AS rel, pt.level AS lvl, pt.isleaf AS leaf,
                       i.indexrelid::regclass::text AS idx, i.indexrelid::bigint AS idx_oid, i.indisvalid AS valid,
                       k.conname AS con, pidx.indexrelid::regclass::text AS parent_idx
                  FROM pg_catalog.pg_partition_tree(?::regclass) pt
                  JOIN pg_catalog.pg_index i ON i.indrelid = pt.relid AND i.indisunique
                   AND pg_catalog.pg_get_indexdef(i.indexrelid) LIKE ?
                  LEFT JOIN pg_catalog.pg_constraint k ON k.conindid = i.indexrelid AND k.contype = 'u'
                  LEFT JOIN pg_catalog.pg_inherits ih ON ih.inhrelid = i.indexrelid
                  LEFT JOIN pg_catalog.pg_index pidx ON pidx.indexrelid = ih.inhparent
                 ORDER BY pt.level, pt.relid::regclass::text
                """, "nexus." + key.table(), "%" + key.indexdefSuffix())
            .map(r -> new Idx(r.get("rel", String.class), r.get("lvl", Integer.class), r.get("leaf", Boolean.class),
                r.get("idx", String.class), r.get("idx_oid", Long.class), r.get("valid", Boolean.class),
                r.get("con", String.class), r.get("parent_idx", String.class)));
    }

    /** The leaves of the table's tree, as {@code regclass} text, ordered. */
    public static List<String> leaves(DSLContext ctx, Key key) {
        return ctx.fetch("SELECT pt.relid::regclass::text AS rel FROM pg_catalog.pg_partition_tree(?::regclass) pt "
                + "WHERE pt.isleaf ORDER BY 1", "nexus." + key.table())
            .map(r -> r.get("rel", String.class));
    }

    /** The model partitions (level 1) of the table's tree, as {@code regclass} text, ordered. */
    public static List<String> modelPartitions(DSLContext ctx, Key key) {
        return ctx.fetch("SELECT pt.relid::regclass::text AS rel FROM pg_catalog.pg_partition_tree(?::regclass) pt "
                + "WHERE pt.level = 1 ORDER BY 1", "nexus." + key.table())
            .map(r -> r.get("rel", String.class));
    }

    /** The leaves of {@code key}'s table with no VALID unique index on the key. */
    public static List<String> leavesWithoutValidIndex(DSLContext ctx, Key key) {
        List<String> valid = indexes(ctx, key).stream().filter(i -> i.leaf() && i.valid()).map(Idx::relation).toList();
        return leaves(ctx, key).stream().filter(l -> !valid.contains(l)).toList();
    }

    /**
     * Removes every unique index on the keys from the trees, so each test starts from the state before P3.3b: the top-level
     * constraints first (dropping one drops the attached ones under it), then every bare index that is not attached to another.
     * A test that leaves a constraint on a parent would make every later leaf get an index from PostgreSQL itself.
     */
    public static void reset(DSLContext su) {
        for (Key key : KEYS) {
            for (var r : su.fetch("""
                    SELECT pt.relid::regclass::text AS rel, k.conname AS con
                      FROM pg_catalog.pg_partition_tree(?::regclass) pt
                      JOIN pg_catalog.pg_constraint k ON k.conrelid = pt.relid AND k.contype = 'u' AND k.conparentid = 0
                      JOIN pg_catalog.pg_index i ON i.indexrelid = k.conindid
                       AND pg_catalog.pg_get_indexdef(i.indexrelid) LIKE ?
                     ORDER BY pt.level
                    """, "nexus." + key.table(), "%" + key.indexdefSuffix())) {
                su.execute("ALTER TABLE " + r.get("rel", String.class) + " DROP CONSTRAINT IF EXISTS \""
                    + r.get("con", String.class) + "\"");
            }
            for (var r : su.fetch("""
                    SELECT i.indexrelid::regclass::text AS idx
                      FROM pg_catalog.pg_partition_tree(?::regclass) pt
                      JOIN pg_catalog.pg_index i ON i.indrelid = pt.relid AND i.indisunique
                       AND pg_catalog.pg_get_indexdef(i.indexrelid) LIKE ?
                     WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_inherits ih WHERE ih.inhrelid = i.indexrelid)
                     ORDER BY pt.level
                    """, "nexus." + key.table(), "%" + key.indexdefSuffix())) {
                su.execute("DROP INDEX IF EXISTS " + r.get("idx", String.class));
            }
        }
    }
}
