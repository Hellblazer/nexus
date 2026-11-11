// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.Field;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

/**
 * nexus-f3yqd: the state of the chash width CHECK constraints, read from {@code pg_constraint.convalidated} in ONE catalog
 * statement, for {@code GET /v1/admin/chash-conformance}. A width CHECK that is VALID says no row of its table is
 * non-conformant, so a reader may take that answer for the table's poison count instead of counting rows.
 *
 * <p><b>What it replaces, and what it does not.</b> It speaks to the two poison tables' width CHECKs only. The client's
 * legacy-debt legs ({@code topic_assignments}, {@code frecency}, {@code relevance_log}: anti-joins with no CHECK behind
 * them) are unchanged, and the migrations leave both CHECKs NOT VALID until the chash re-key rung validates them, so on a
 * fresh or never-validated store the answer is {@code all_validated: false} and nothing is saved. The payoff begins where a
 * store has been validated (the ladder's VALIDATE job of engine 0.1.190 is what makes that common).
 *
 * <p><b>Which constraints.</b> The two the client's gate list names as poison tables
 * ({@code nexus.db.chash_tables.POISON_CHASH_TABLES}: the tables that carry a width CHECK, so a non-conformant row
 * crash-loops an upgrade): {@code chunks_chash_octet_check} on {@code nexus.chunks} and
 * {@code catalog_document_chunks_chash_octet_check} on {@code nexus.catalog_document_chunks}, both
 * {@code CHECK (octet_length(chash) = 32)}, both added NOT VALID (rdr180-001, vectors-004) and validated by the chash
 * re-key rung's VALIDATE. The debt tables ({@code topic_assignments.doc_id}, {@code frecency.chunk_id},
 * {@code relevance_log.chunk_id}) carry no width CHECK and are NOT reported: a client that wants them counts them. A
 * same-named constraint on another relation (the retired {@code chunks_retired_225} carries its own
 * {@code chunks_chash_octet_check}) is not one of these and is never read: a constraint counts only through the table it
 * is declared on, or a partition of it. {@code tests/test_chash_conformance_route_targets.py} holds {@link #TARGETS} to
 * the client list.
 *
 * <p><b>A constraint that is not there is not valid.</b> Each target is reported whether or not the constraint exists;
 * an absent one is {@code present=false, validated=false}, so {@code all_validated} can never be a vacuous true.
 *
 * <p><b>Partitions.</b> {@code nexus.chunks} is partitioned, and a CHECK on a partitioned parent is copied to every
 * partition, at any depth, as a row of its own in {@code pg_constraint} (not linked by {@code conparentid}, which a
 * partition's inherited CHECK leaves 0: the link is the table's own place in {@code pg_inherits}).
 * {@code VALIDATE CONSTRAINT} on the parent validates the copies. This reports what PostgreSQL holds, folded to one entry
 * per target: {@code validated} is true only when the declaring table's own row is valid AND every copy below it is, so a
 * mix (a parent valid with a copy not valid, which PostgreSQL does not let ATTACH PARTITION create but which a hand edit
 * of a catalog or a future version might) reads NOT validated: the safe answer for a reader that skips its scans on true.
 * {@code partitions} and {@code partitions_not_validated} count the copies, so a mix is visible and a partitioned table
 * whose walk found no copy is distinguishable from a plain one.
 */
public final class ChashConformanceConstraints {

    /** A chash width constraint the route reports: the table it is declared on and its name. */
    public record Target(Table<?> table, String constraint) {
        /** The schema-qualified table, as the client's table list names it ({@code nexus.chunks}). */
        public String qualifiedTable() {
            return table.getSchema().getName() + "." + table.getName();
        }
    }

    /** The reported constraints, in report order. */
    public static final List<Target> TARGETS = List.of(
        new Target(CHUNKS, "chunks_chash_octet_check"),
        new Target(CATALOG_DOCUMENT_CHUNKS, "catalog_document_chunks_chash_octet_check"));

    /** One target's state. {@code partitions} counts the copies on partitions below the declaring table. */
    public record Entry(String table, String name, boolean present, boolean validated, int partitions,
                        int partitionsNotValidated) {}

    /** The answer: one entry per target; {@code allValidated} is true only when every target is present and validated. */
    public record Report(List<Entry> constraints) {
        public boolean allValidated() {
            return !constraints.isEmpty() && constraints.stream().allMatch(Entry::validated);
        }
    }

    /**
     * One {@code pg_constraint} row of a target's name. {@code relation} is the oid of the table it is declared on and
     * {@code relationParent} the oid of that table's parent in {@code pg_inherits} (0 for a table that is nobody's partition).
     * A CHECK a partition inherits is its own row with {@code conparentid} 0, so the partition tree is the table tree.
     */
    record Row(long relation, long relationParent, String relationName, String name, boolean validated) {}

    private final TenantScope scope;

    public ChashConformanceConstraints(TenantScope scope) {
        this.scope = scope;
    }

    /**
     * Folds the rows of {@link #query} into one entry per target. Pure, so the partition rules are testable without a
     * database: a row is a target's root when it is declared on the target table and that table is nobody's partition; every
     * row on a table whose {@code relationParent} chain leads to the root's table is one of its partition copies; a row on
     * any other table (a same-named constraint on a retired table) leads to no root and is not counted.
     */
    static Report aggregate(List<Row> rows) {
        Map<Long, Row> byRelation = new HashMap<>();
        for (Row r : rows) byRelation.put(r.relation(), r);
        List<Entry> out = new ArrayList<>(TARGETS.size());
        for (Target t : TARGETS) {
            Row root = rows.stream()
                .filter(r -> r.relationParent() == 0 && r.relationName().equals(t.table().getName())
                    && r.name().equals(t.constraint()))
                .findFirst().orElse(null);
            if (root == null) {
                out.add(new Entry(t.qualifiedTable(), t.constraint(), false, false, 0, 0));
                continue;
            }
            int copies = 0;
            int notValid = 0;
            for (Row r : rows) {
                if (r.relation() != root.relation() && r.name().equals(root.name()) && descendsFrom(r, root.relation(), byRelation)) {
                    copies++;
                    if (!r.validated()) notValid++;
                }
            }
            out.add(new Entry(t.qualifiedTable(), t.constraint(), true, root.validated() && notValid == 0, copies, notValid));
        }
        return new Report(List.copyOf(out));
    }

    /** True when the table of {@code r} is below table {@code ancestor} through {@code relationParent} links (bounded, so a cycle cannot loop). */
    private static boolean descendsFrom(Row r, long ancestor, Map<Long, Row> byRelation) {
        Row at = r;
        for (int depth = 0; depth < 32 && at.relationParent() != 0; depth++) {
            if (at.relationParent() == ancestor) return true;
            at = byRelation.get(at.relationParent());
            if (at == null) return false;
        }
        return false;
    }

    /**
     * The one catalog statement: every CHECK named like a target in the product schema, with its table, that table's parent
     * (a left join to {@code pg_inherits}) and its validity. Reads {@code pg_catalog} only; no table of the product is
     * scanned and no tenant predicate applies. Public so a test can run it on a session of its choosing.
     */
    public static org.jooq.Select<org.jooq.Record5<Long, Long, String, String, Boolean>> query(org.jooq.DSLContext ctx) {
        var con = DSL.table(DSL.name("pg_catalog", "pg_constraint")).as("con");
        var cls = DSL.table(DSL.name("pg_catalog", "pg_class")).as("cls");
        var ns = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("ns");
        var inh = DSL.table(DSL.name("pg_catalog", "pg_inherits")).as("inh");
        Field<Long> relation = DSL.field(DSL.name("con", "conrelid")).cast(SQLDataType.BIGINT);
        Field<Long> relationParent = DSL.coalesce(DSL.field(DSL.name("inh", "inhparent")).cast(SQLDataType.BIGINT), DSL.inline(0L));
        Field<String> relationName = DSL.field(DSL.name("cls", "relname"), String.class);
        Field<String> name = DSL.field(DSL.name("con", "conname"), String.class);
        Field<Boolean> validated = DSL.field(DSL.name("con", "convalidated"), Boolean.class);
        return ctx.select(relation, relationParent, relationName, name, validated)
            .from(con)
            .join(cls).on(DSL.field(DSL.name("cls", "oid")).eq(DSL.field(DSL.name("con", "conrelid"))))
            .join(ns).on(DSL.field(DSL.name("ns", "oid")).eq(DSL.field(DSL.name("cls", "relnamespace"))))
            .leftJoin(inh).on(DSL.field(DSL.name("inh", "inhrelid")).eq(DSL.field(DSL.name("con", "conrelid"))))
            .where(DSL.field(DSL.name("ns", "nspname"), String.class).eq(CHUNKS.getSchema().getName()))
            .and(DSL.field(DSL.name("con", "contype"), String.class).eq("c"))
            .and(name.in(TARGETS.stream().map(Target::constraint).toList()));
    }

    /**
     * Reads the report. The session is the caller's tenant only because every connection of the engine is scoped; no tenant
     * data is read and nothing here filters by tenant.
     */
    public Report read(String tenant) {
        return scope.withTenant(tenant, ctx -> {
            List<Row> rows = new ArrayList<>();
            for (var r : query(ctx).fetch()) {
                rows.add(new Row(r.value1(), r.value2(), r.value3(), r.value4(), Boolean.TRUE.equals(r.value5())));
            }
            return aggregate(rows);
        });
    }
}
