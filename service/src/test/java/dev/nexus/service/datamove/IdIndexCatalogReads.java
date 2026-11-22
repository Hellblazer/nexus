// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * RDR-229 P3.4 (nexus-p2m1x.22): the catalog reads of the P3.4 census tests, typed jOOQ over pg_index, pg_class, pg_namespace and
 * pg_attribute. The array operations the old statements used ({@code indkey[n]}, {@code = ANY (indkey)}) have no typed form, so the
 * rows carry each index's key as its text and the table's attributes, and the columns are worked out here.
 */
final class IdIndexCatalogReads {

    private IdIndexCatalogReads() { /* static */ }

    /**
     * One VALID unique index of schema nexus on a top-level table: its table, its key columns in order as the catalog lists them
     * ({@code a, b, c}), its partial predicate as {@code pg_get_expr} deparses it without the outer parentheses (or null), and every
     * column it covers (key and included) by name.
     */
    record UniqueIndex(String table, String keyColumns, String predicate, List<String> coveredColumns) { }

    private static final Field<Long> INDEXRELID = DSL.field(DSL.name("i", "indexrelid"), Long.class);
    private static final Field<String> TABLE = DSL.field(DSL.name("t", "relname"), String.class);
    private static final Field<String> INDKEY = DSL.cast(DSL.field(DSL.name("i", "indkey")), SQLDataType.VARCHAR);
    private static final Field<Integer> NKEYATTS = DSL.field(DSL.name("i", "indnkeyatts"), Integer.class);
    private static final Field<String> PRED = DSL.function(DSL.name("pg_catalog", "pg_get_expr"), String.class,
        DSL.field(DSL.name("i", "indpred")), DSL.field(DSL.name("i", "indrelid")));
    private static final Field<Short> ATTNUM = DSL.field(DSL.name("a", "attnum"), Short.class);
    private static final Field<String> ATTNAME = DSL.field(DSL.name("a", "attname"), String.class);

    /** Every VALID unique index in schema nexus on a top-level table (not a partition), in index then column order. */
    static List<UniqueIndex> uniqueIndexes(DSLContext ctx) {
        var rows = ctx.select(INDEXRELID, TABLE, INDKEY, NKEYATTS, PRED, ATTNUM, ATTNAME)
            .from(DSL.table(DSL.name("pg_catalog", "pg_index")).as("i"))
            .join(DSL.table(DSL.name("pg_catalog", "pg_class")).as("t")).on(DSL.field(DSL.name("t", "oid")).eq(DSL.field(DSL.name("i", "indrelid"))))
            .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n")).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("t", "relnamespace"))))
            .join(DSL.table(DSL.name("pg_catalog", "pg_attribute")).as("a")).on(DSL.field(DSL.name("a", "attrelid")).eq(DSL.field(DSL.name("i", "indrelid"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(DSL.field(DSL.name("i", "indisunique"), Boolean.class))
            .and(DSL.field(DSL.name("i", "indisvalid"), Boolean.class))
            .and(DSL.field(DSL.name("t", "relispartition"), Boolean.class).isFalse())
            .and(ATTNUM.gt((short) 0))
            .orderBy(INDEXRELID, ATTNUM)
            .fetch();
        Map<Long, List<org.jooq.Record>> byIndex = new LinkedHashMap<>();
        rows.forEach(r -> byIndex.computeIfAbsent(r.get(INDEXRELID), k -> new ArrayList<>()).add(r));
        List<UniqueIndex> out = new ArrayList<>();
        for (List<org.jooq.Record> index : byIndex.values()) {
            var first = index.get(0);
            Map<Short, String> names = new LinkedHashMap<>();
            index.forEach(r -> names.put(r.get(ATTNUM), r.get(ATTNAME)));
            String[] key = first.get(INDKEY).trim().split("\\s+");
            List<String> keyColumns = new ArrayList<>();
            List<String> covered = new ArrayList<>();
            for (int k = 0; k < key.length; k++) {
                String name = names.get(Short.valueOf(key[k]));   // null for an expression (attnum 0)
                if (name != null) {
                    covered.add(name);
                }
                if (k < first.get(NKEYATTS)) {
                    keyColumns.add(name);
                }
            }
            String pred = first.get(PRED);
            if (pred != null && pred.startsWith("(") && pred.endsWith(")")) {
                pred = pred.substring(1, pred.length() - 1);
            }
            out.add(new UniqueIndex(first.get(TABLE), String.join(", ", keyColumns), pred, List.copyOf(covered)));
        }
        return out;
    }

    /** True when {@code nexus.<table>} exists as a relation of any kind (what {@code to_regclass} resolved). */
    static boolean relationExists(DSLContext ctx, String table) {
        return ctx.fetchExists(DSL.selectOne().from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
            .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n")).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(DSL.field(DSL.name("c", "relname"), String.class).eq(table)));
    }
}
