// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record5;
import org.jooq.Result;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

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

    private static final Table<?> CONSTRAINT = DSL.table(DSL.name("pg_catalog", "pg_constraint"));
    private static final Table<?> CLASS = DSL.table(DSL.name("pg_catalog", "pg_class"));
    private static final Table<?> NAMESPACE = DSL.table(DSL.name("pg_catalog", "pg_namespace"));
    private static final Table<?> ATTRIBUTE = DSL.table(DSL.name("pg_catalog", "pg_attribute"));
    private static final Table<?> INDEX = DSL.table(DSL.name("pg_catalog", "pg_index"));

    private static final Field<String> REL = DSL.field(DSL.name("r", "relname"), String.class);
    private static final Field<String> CON = DSL.field(DSL.name("c", "conname"), String.class);
    private static final Field<Short[]> CONKEY = DSL.field(DSL.name("c", "conkey"), SQLDataType.SMALLINT.getArrayDataType());
    private static final Field<Short> ATTNUM = DSL.field(DSL.name("a", "attnum"), Short.class);
    private static final Field<String> ATTNAME = DSL.field(DSL.name("a", "attname"), String.class);

    private IdKeyIndexCensus() { /* static */ }

    /** The top-level foreign keys of schema nexus, each with its columns' attributes, in table then name order; one row per attribute of the table. */
    private static Result<Record5<String, String, Short[], Short, String>> keyAttributes(DSLContext ctx) {
        return ctx.select(REL, CON, CONKEY, ATTNUM, ATTNAME)
            .from(CONSTRAINT.as("c"))
            .join(CLASS.as("r")).on(DSL.field(DSL.name("r", "oid")).eq(DSL.field(DSL.name("c", "conrelid"))))
            .join(NAMESPACE.as("n")).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("r", "relnamespace"))))
            .join(ATTRIBUTE.as("a")).on(DSL.field(DSL.name("a", "attrelid")).eq(DSL.field(DSL.name("c", "conrelid"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(DSL.field(DSL.name("c", "contype"), String.class).eq(DSL.inline("f")))
            .and(DSL.field(DSL.name("c", "conparentid"), Long.class).eq(DSL.inline(0L)))
            .and(ATTNUM.gt((short) 0))
            .and(DSL.field(DSL.name("a", "attisdropped"), Boolean.class).isFalse())
            .orderBy(REL, CON, ATTNUM)
            .fetch();
    }

    /** The VALID, non-partial indexes of schema nexus with two or more key columns, by table: each one's key columns (attnums) in order. */
    private static Map<String, List<List<Short>>> candidateIndexes(DSLContext ctx) {
        Field<String> indkey = DSL.cast(DSL.field(DSL.name("i", "indkey")), SQLDataType.VARCHAR);
        Map<String, List<List<Short>>> byTable = new HashMap<>();
        ctx.select(REL, indkey, DSL.field(DSL.name("i", "indnkeyatts"), Integer.class))
            .from(INDEX.as("i"))
            .join(CLASS.as("r")).on(DSL.field(DSL.name("r", "oid")).eq(DSL.field(DSL.name("i", "indrelid"))))
            .join(NAMESPACE.as("n")).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("r", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(DSL.field(DSL.name("i", "indisvalid"), Boolean.class))
            .and(DSL.field(DSL.name("i", "indpred")).isNull())
            .and(DSL.field(DSL.name("i", "indnkeyatts"), Integer.class).ge(2))
            .fetch()
            .forEach(row -> {
                String[] parts = row.get(indkey).trim().split("\\s+");   // int2vector as text: "1 2 3"
                List<Short> cols = new ArrayList<>(2);
                for (int k = 0; k < 2; k++) {   // only the first two key columns are read by the rule
                    cols.add(Short.valueOf(parts[k]));
                }
                byTable.computeIfAbsent(row.get(REL), t -> new ArrayList<>()).add(cols);
            });
        return byTable;
    }

    /**
     * Every id key, in table then name order. A key on a table with no tenant_id column is listed and never supported. The array
     * operations of the rule ({@code indkey[n] = ANY (conkey)}) have no typed jOOQ form, so the catalog's columns are read and the rule
     * is applied here.
     */
    public static List<Key> keys(DSLContext ctx) {
        Map<String, List<List<Short>>> indexes = candidateIndexes(ctx);
        List<Key> out = new ArrayList<>();
        String table = null;
        String con = null;
        Set<Short> conkey = null;
        boolean idColumn = false;
        Short tenantAttnum = null;
        for (var row : keyAttributes(ctx)) {
            String t = row.get(REL);
            String c = row.get(CON);
            if (!t.equals(table) || !c.equals(con)) {
                if (con != null && idColumn) {
                    out.add(key(table, con, conkey, tenantAttnum, indexes));
                }
                table = t;
                con = c;
                conkey = new HashSet<>(List.of(row.get(CONKEY)));
                idColumn = false;
                tenantAttnum = null;
            }
            short attnum = row.get(ATTNUM);
            if (conkey.contains(attnum) && ID_COLUMNS.contains(row.get(ATTNAME))) {
                idColumn = true;
            }
            if ("tenant_id".equals(row.get(ATTNAME))) {
                tenantAttnum = attnum;
            }
        }
        if (con != null && idColumn) {
            out.add(key(table, con, conkey, tenantAttnum, indexes));
        }
        return out;
    }

    /** The rule for one key: an index whose first two columns are both columns of the key and one of them is tenant_id. */
    private static Key key(String table, String con, Set<Short> conkey, Short tenantAttnum, Map<String, List<List<Short>>> indexes) {
        boolean supported = tenantAttnum != null && indexes.getOrDefault(table, List.of()).stream()
            .anyMatch(cols -> conkey.contains(cols.get(0)) && conkey.contains(cols.get(1))
                && (cols.get(0).equals(tenantAttnum) || cols.get(1).equals(tenantAttnum)));
        return new Key(table, con, supported);
    }

    /** The id keys no index supports: empty is the census passing. */
    public static List<Key> unsupported(DSLContext ctx) {
        return keys(ctx).stream().filter(k -> !k.supported()).toList();
    }
}
