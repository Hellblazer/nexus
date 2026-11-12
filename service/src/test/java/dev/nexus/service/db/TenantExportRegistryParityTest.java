// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.TenantExport.Column;
import dev.nexus.service.db.TenantExport.ExportTable;
import dev.nexus.service.db.TenantExport.Reference;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * The tenant export registry against a Liquibase-migrated database (RDR-248, bead nexus-csayy.1).
 *
 * <p>Every check reads the live catalog, never a hand count: the RLS parent tables, their primary keys and
 * types, which keys are global serials, their foreign keys, their triggers, and their id-shaped columns.
 * A schema change that the registry does not describe fails here with the live facts in the message.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TenantExportRegistryParityTest {

    /**
     * Column types the id census covers (gate S1): a reassigned id can hide in any of them. Every reassigned
     * id is a bigint today; {@link #everyReassignedIdIsACensusType} fails if one is not, so the census widens
     * with the ids it hunts.
     */
    static final Set<String> ID_SHAPED_TYPES = Set.of("bigint", "bigint[]", "jsonb");

    record LiveColumn(String name, String type, String defaultExpr, String identity, String generated) {
        boolean serial() {
            return (defaultExpr != null && defaultExpr.startsWith("nextval("))
                || "a".equals(identity) || "d".equals(identity);
        }
    }

    record LiveFk(List<String> columns, String parent, List<String> parentColumns) {
        @Override
        public String toString() {
            return String.join(",", columns) + " -> " + parent + "(" + String.join(",", parentColumns) + ")";
        }
    }

    record LiveTable(String name, Map<String, LiveColumn> columns, List<String> primaryKey,
                     List<LiveFk> foreignKeys, Set<String> triggers) {
    }

    PostgreSQLContainer<?> pg;
    Map<String, LiveTable> live;

    @BeforeAll
    void migrateAndRead() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            live = readLiveRlsTables(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    @AfterAll
    void stop() {
        if (pg != null) pg.stop();
    }

    // ── live catalog reader (jOOQ over pg_catalog, nexus-zrcj7) ───────────────────────────────────────

    private static <T> Field<T> f(String alias, String column, Class<T> type) {
        return DSL.field(DSL.name(alias, column), type);
    }

    static Map<String, LiveTable> readLiveRlsTables(DSLContext ctx) {
        Field<Long> oid = f("c", "oid", Long.class);
        Field<String> nsp = f("n", "nspname", String.class);
        Field<String> rel = f("c", "relname", String.class);
        Field<Boolean> rls = f("c", "relrowsecurity", Boolean.class);
        Field<Boolean> part = f("c", "relispartition", Boolean.class);
        Field<String> kind = f("c", "relkind", String.class);

        Map<Long, String> nameOf = new HashMap<>();
        Set<Long> rlsParents = new TreeSet<>();
        for (Record r : ctx.select(oid, nsp, rel, rls, part)
                .from(DSL.table(DSL.name("pg_class")).as("c"))
                .join(DSL.table(DSL.name("pg_namespace")).as("n"))
                .on(f("c", "relnamespace", Long.class).eq(f("n", "oid", Long.class)))
                .where(kind.in("r", "p"))
                .and(nsp.notIn("pg_catalog", "information_schema"))
                .fetch()) {
            nameOf.put(r.get(oid), TenantExport.qualify(r.get(nsp), r.get(rel)));
            if (r.get(rls) && !r.get(part)) rlsParents.add(r.get(oid));
        }

        Field<Long> attrelid = f("a", "attrelid", Long.class);
        Field<String> attname = f("a", "attname", String.class);
        Field<Short> attnum = f("a", "attnum", Short.class);
        Field<String> type = DSL.function("format_type", String.class,
            f("a", "atttypid", Long.class), f("a", "atttypmod", Integer.class));
        Field<String> identity = f("a", "attidentity", String.class);
        Field<String> generated = f("a", "attgenerated", String.class);
        Field<String> def = DSL.function("pg_get_expr", String.class,
            f("d", "adbin", Object.class), f("d", "adrelid", Long.class));

        Map<Long, Map<Short, LiveColumn>> columnsByNum = new HashMap<>();
        for (Record r : ctx.select(attrelid, attname, attnum, type, identity, generated, def)
                .from(DSL.table(DSL.name("pg_attribute")).as("a"))
                .leftJoin(DSL.table(DSL.name("pg_attrdef")).as("d"))
                .on(f("d", "adrelid", Long.class).eq(attrelid).and(f("d", "adnum", Short.class).eq(attnum)))
                .where(attrelid.in(rlsParents))
                .and(attnum.gt((short) 0))
                .and(f("a", "attisdropped", Boolean.class).isFalse())
                .orderBy(attrelid, attnum)
                .fetch()) {
            columnsByNum.computeIfAbsent(r.get(attrelid), k -> new LinkedHashMap<>())
                .put(r.get(attnum), new LiveColumn(r.get(attname), r.get(type), r.get(def),
                    blankToNull(r.get(identity)), blankToNull(r.get(generated))));
        }

        Field<Long> conrelid = f("k", "conrelid", Long.class);
        Field<String> contype = f("k", "contype", String.class);
        Field<Short[]> conkey = f("k", "conkey", Short[].class);
        Field<Long> confrelid = f("k", "confrelid", Long.class);
        Field<Short[]> confkey = f("k", "confkey", Short[].class);
        Map<Long, List<String>> pk = new HashMap<>();
        Map<Long, List<LiveFk>> fks = new HashMap<>();
        Map<Long, Map<Short, String>> parentCols = new HashMap<>();
        List<? extends Record> constraints = ctx.select(conrelid, contype, conkey, confrelid, confkey)
            .from(DSL.table(DSL.name("pg_constraint")).as("k"))
            .where(conrelid.in(rlsParents))
            .and(contype.in("p", "f"))
            // A foreign key into a partitioned table (chunks) is cloned once per partition, each clone
            // naming its parent constraint; only the declared constraint is the schema's fact.
            .and(f("k", "conparentid", Long.class).eq(0L))
            .orderBy(conrelid, f("k", "conname", String.class))
            .fetch();
        for (Record r : constraints) {
            Map<Short, LiveColumn> cols = columnsByNum.get(r.get(conrelid));
            List<String> names = new ArrayList<>();
            for (Short n : r.get(conkey)) names.add(cols.get(n).name());
            if ("p".equals(r.get(contype))) {
                pk.put(r.get(conrelid), names);
            } else {
                long parent = r.get(confrelid);
                Map<Short, String> pcols = parentCols.computeIfAbsent(parent, p -> columnNames(ctx, p));
                List<String> pnames = new ArrayList<>();
                for (Short n : r.get(confkey)) pnames.add(pcols.get(n));
                fks.computeIfAbsent(r.get(conrelid), k -> new ArrayList<>())
                    .add(new LiveFk(names, nameOf.get(parent), pnames));
            }
        }

        Field<Long> tgrelid = f("t", "tgrelid", Long.class);
        Field<String> tgname = f("t", "tgname", String.class);
        Map<Long, Set<String>> triggers = new HashMap<>();
        for (Record r : ctx.select(tgrelid, tgname)
                .from(DSL.table(DSL.name("pg_trigger")).as("t"))
                .where(tgrelid.in(rlsParents))
                .and(f("t", "tgisinternal", Boolean.class).isFalse())
                .fetch()) {
            triggers.computeIfAbsent(r.get(tgrelid), k -> new TreeSet<>()).add(r.get(tgname));
        }

        Map<String, LiveTable> out = new TreeMap<>();
        for (long t : rlsParents) {
            Map<String, LiveColumn> cols = new LinkedHashMap<>();
            columnsByNum.getOrDefault(t, Map.of()).values().forEach(c -> cols.put(c.name(), c));
            out.put(nameOf.get(t), new LiveTable(nameOf.get(t), cols, pk.getOrDefault(t, List.of()),
                fks.getOrDefault(t, List.of()), triggers.getOrDefault(t, Set.of())));
        }
        return out;
    }

    private static Map<Short, String> columnNames(DSLContext ctx, long relid) {
        Field<String> attname = f("a", "attname", String.class);
        Field<Short> attnum = f("a", "attnum", Short.class);
        Map<Short, String> m = new HashMap<>();
        ctx.select(attnum, attname)
            .from(DSL.table(DSL.name("pg_attribute")).as("a"))
            .where(f("a", "attrelid", Long.class).eq(relid))
            .and(attnum.gt((short) 0))
            .fetch()
            .forEach(r -> m.put(r.get(attnum), r.get(attname)));
        return m;
    }

    private static String blankToNull(String s) {
        return s == null || s.isBlank() ? null : s;
    }

    /** The live facts for one table, printed when the registry lacks or misdescribes it. */
    static String census(LiveTable t) {
        StringBuilder sb = new StringBuilder(t.name()).append('\n');
        sb.append("  pk: ");
        for (String c : t.primaryKey()) {
            LiveColumn col = t.columns().get(c);
            sb.append(c).append(' ').append(col.type()).append(col.serial() ? " SERIAL" : "").append("; ");
        }
        sb.append('\n');
        t.foreignKeys().forEach(fk -> sb.append("  fk: ").append(fk).append('\n'));
        if (!t.triggers().isEmpty()) sb.append("  triggers: ").append(t.triggers()).append('\n');
        List<String> idShaped = new ArrayList<>();
        t.columns().values().forEach(c -> {
            if (ID_SHAPED_TYPES.contains(c.type())) idShaped.add(c.name() + " " + c.type());
        });
        if (!idShaped.isEmpty()) sb.append("  id-shaped: ").append(idShaped).append('\n');
        return sb.toString();
    }

    // ── the checks: each returns one line per problem, so a planted registry proves the check bites ────

    static Map<String, ExportTable> byName(List<ExportTable> tables) {
        Map<String, ExportTable> m = new TreeMap<>();
        tables.forEach(t -> m.put(t.name(), t));
        return m;
    }

    static List<String> missingOrStaleTables(Map<String, LiveTable> live, List<ExportTable> tables) {
        Map<String, ExportTable> reg = byName(tables);
        List<String> problems = new ArrayList<>();
        live.values().forEach(t -> {
            if (!reg.containsKey(t.name())) problems.add("no entry: " + census(t));
        });
        reg.keySet().forEach(n -> {
            if (!live.containsKey(n)) problems.add("stale entry: " + n);
        });
        return problems;
    }

    static List<String> primaryKeyMismatches(Map<String, LiveTable> live, List<ExportTable> tables) {
        List<String> wrong = new ArrayList<>();
        byName(tables).forEach((name, e) -> {
            LiveTable t = live.get(name);
            if (t == null) return;
            List<Column> expected = new ArrayList<>();
            t.primaryKey().forEach(c -> expected.add(new Column(c, t.columns().get(c).type())));
            if (!expected.equals(e.primaryKey())) wrong.add(name + ": live " + expected + ", entry " + e.primaryKey());
        });
        return wrong;
    }

    static List<String> reassignedIdMismatches(Map<String, LiveTable> live, List<ExportTable> tables) {
        List<String> wrong = new ArrayList<>();
        byName(tables).forEach((name, e) -> {
            LiveTable t = live.get(name);
            if (t == null) return;
            // A serial column anywhere in the key is reassigned, composite or not (catalog_links keys on
            // (tenant_id, id)): RDR-248 reassigns always, which also keeps every sequence ahead of its rows.
            List<String> serials = new ArrayList<>();
            for (String c : t.primaryKey()) {
                if (t.columns().get(c).serial()) serials.add(c);
            }
            String serial = serials.isEmpty() ? null : serials.get(0);
            if (serials.size() > 1) {
                wrong.add(name + ": more than one serial key column " + serials);
            } else if (!java.util.Objects.equals(serial, e.reassignedId())) {
                wrong.add(name + ": live serial key " + serial + ", entry reassignedId " + e.reassignedId());
            } else if (serial != null && !ID_SHAPED_TYPES.contains(t.columns().get(serial).type())) {
                wrong.add(name + "." + serial + " is " + t.columns().get(serial).type()
                    + ", a type the id census does not cover; widen ID_SHAPED_TYPES");
            }
        });
        return wrong;
    }

    static List<String> foreignKeyMismatches(Map<String, LiveTable> live, List<ExportTable> tables) {
        List<String> wrong = new ArrayList<>();
        byName(tables).forEach((name, e) -> {
            LiveTable t = live.get(name);
            if (t == null) return;
            Set<String> liveFks = new TreeSet<>();
            t.foreignKeys().forEach(fk -> liveFks.add(fk.toString()));
            Set<String> entryFks = new TreeSet<>();
            for (Reference r : e.references()) {
                if (r.foreignKey()) {
                    entryFks.add(new LiveFk(r.columns(), r.parentName(), r.parentColumns()).toString());
                }
            }
            if (!liveFks.equals(entryFks)) wrong.add(name + ": live " + liveFks + ", entry " + entryFks);
        });
        return wrong;
    }

    static List<String> softReferenceProblems(Map<String, LiveTable> live, List<ExportTable> tables) {
        List<String> wrong = new ArrayList<>();
        tables.forEach(e -> e.references().forEach(r -> {
            if (r.foreignKey()) return;
            LiveTable parent = live.get(r.parentName());
            if (parent == null) {
                wrong.add(e.name() + ": refers to " + r.parentName() + ", not a live RLS table");
                return;
            }
            r.parentColumns().forEach(c -> {
                if (!parent.columns().containsKey(c)) wrong.add(e.name() + ": " + r.parentName() + "." + c);
            });
            LiveTable own = live.get(e.name());
            if (own != null && r.jsonPath() != null
                    && !"jsonb".equals(own.columns().get(r.columns().get(0)).type())) {
                wrong.add(e.name() + "." + r.columns().get(0) + ": a JSON path on a column that is not jsonb");
            }
            if (own != null && r.array() && !own.columns().get(r.columns().get(0)).type().endsWith("[]")) {
                wrong.add(e.name() + "." + r.columns().get(0) + ": an array reference on a column that is not an array");
            }
        }));
        return wrong;
    }

    static List<String> goneColumns(Map<String, LiveTable> live, List<ExportTable> tables) {
        List<String> gone = new ArrayList<>();
        byName(tables).forEach((name, e) -> {
            LiveTable t = live.get(name);
            if (t == null) return;
            e.namedColumns().forEach(c -> {
                if (!t.columns().containsKey(c)) gone.add(name + "." + c);
            });
        });
        return gone;
    }

    static List<String> triggerMismatches(Map<String, LiveTable> live, List<ExportTable> tables) {
        List<String> wrong = new ArrayList<>();
        byName(tables).forEach((name, e) -> {
            LiveTable t = live.get(name);
            if (t == null) return;
            if (!new TreeSet<>(t.triggers()).equals(new TreeSet<>(e.triggers()))) {
                wrong.add(name + ": live " + t.triggers() + ", entry " + new TreeSet<>(e.triggers()));
            }
        });
        return wrong;
    }

    static List<String> unclassifiedIdColumns(Map<String, LiveTable> live, List<ExportTable> tables) {
        List<String> unclassified = new ArrayList<>();
        byName(tables).forEach((name, e) -> {
            LiveTable t = live.get(name);
            if (t == null || !e.exported()) return;
            Set<String> classified = new TreeSet<>(e.notReferences());
            if (e.reassignedId() != null) classified.add(e.reassignedId());
            e.references().forEach(r -> classified.addAll(r.columns()));
            t.columns().values().forEach(c -> {
                if (ID_SHAPED_TYPES.contains(c.type()) && !classified.contains(c.name())) {
                    unclassified.add(name + "." + c.name() + " " + c.type());
                }
            });
        });
        return unclassified;
    }

    // ── the registry against the live schema ──────────────────────────────────────────────────────────

    @Test
    void theLiveSchemaHasRlsTablesToCheck() {
        // Non-vacuity: a reader that found nothing would pass every check below.
        assertThat(live).as("RLS parent tables in the migrated schema").hasSizeGreaterThan(40);
        assertThat(live).containsKeys("chunks", "memory", "catalog_documents", "t1.scratch");
        long fks = live.values().stream().mapToLong(t -> t.foreignKeys().size()).sum();
        long triggers = live.values().stream().mapToLong(t -> t.triggers().size()).sum();
        assertThat(fks).as("foreign keys read").isGreaterThan(20);
        assertThat(triggers).as("triggers read").isGreaterThan(5);
    }

    @Test
    void everyLiveRlsParentTableHasAnEntryAndNoEntryIsStale() {
        assertThat(missingOrStaleTables(live, TenantExport.TABLES)).isEmpty();
    }

    @Test
    void everyPrimaryKeyIsInItsEntryWithItsTypes() {
        assertThat(primaryKeyMismatches(live, TenantExport.TABLES)).isEmpty();
    }

    @Test
    void theReassignedIdIsExactlyTheSerialKey() {
        assertThat(reassignedIdMismatches(live, TenantExport.TABLES)).isEmpty();
    }

    @Test
    void everyForeignKeyIsAReferenceAndEveryForeignKeyReferenceExists() {
        assertThat(foreignKeyMismatches(live, TenantExport.TABLES)).isEmpty();
    }

    @Test
    void everySoftReferencePointsAtALiveColumnOfTheRightShape() {
        assertThat(softReferenceProblems(live, TenantExport.TABLES)).isEmpty();
    }

    @Test
    void everyColumnAnEntryNamesStillExists() {
        assertThat(goneColumns(live, TenantExport.TABLES)).isEmpty();
    }

    @Test
    void everyTriggerIsListed() {
        assertThat(triggerMismatches(live, TenantExport.TABLES)).isEmpty();
    }

    @Test
    void everyIdShapedColumnOfAnExportedTableIsClassified() {
        assertThat(unclassifiedIdColumns(live, TenantExport.TABLES))
            .as("id-shaped columns of exported tables that are neither the reassigned id, a reference, "
                + "nor listed as not a reference (gate S1 census)")
            .isEmpty();
    }

    @Test
    void theCensusListsSourceIdsAsAReference() {
        // Gate S1: memory_summaries.source_ids holds memory ids; the remap-or-provenance decision is Phase 2's.
        ExportTable ms = TenantExport.entry("memory_summaries").orElseThrow();
        assertThat(ms.references()).anySatisfy(r -> {
            assertThat(r.columns()).containsExactly("source_ids");
            assertThat(r.array()).isTrue();
            assertThat(r.parentName()).isEqualTo("memory");
        });
        assertThat(live.get("memory_summaries").columns().get("source_ids").type()).isEqualTo("bigint[]");
    }

    @Test
    void theRegistryLoadsInAnOrderTheLiveSchemaAccepts() {
        assertThat(TenantExport.loadOrderViolations(TenantExport.TABLES)).isEmpty();
    }

    // ── each check bites: a planted registry one step away from the real one fails it ────────────────

    private static List<ExportTable> replacing(String name, java.util.function.UnaryOperator<ExportTable> edit) {
        List<ExportTable> out = new ArrayList<>();
        for (ExportTable t : TenantExport.TABLES) out.add(t.name().equals(name) ? edit.apply(t) : t);
        return out;
    }

    private static ExportTable copy(ExportTable t, List<Column> pk, String reassigned, List<Reference> refs,
                                    Set<String> triggers, Set<String> maintained, Set<String> notRefs) {
        return new ExportTable(t.schema(), t.table(), t.exportClass(), t.reason(), pk, reassigned, refs,
            triggers, maintained, notRefs);
    }

    @Test
    void aTableWithNoEntryFails() {
        List<ExportTable> planted = new ArrayList<>(TenantExport.TABLES);
        planted.removeIf(t -> t.name().equals("memory"));
        assertThat(missingOrStaleTables(live, planted)).singleElement().asString().startsWith("no entry: memory");
    }

    @Test
    void aWrongKeyTypeFails() {
        List<ExportTable> planted = replacing("chunks", t -> copy(t,
            List.of(new Column("tenant_id", "text"), new Column("collection", "text"),
                new Column("chash", "text"), new Column("embedding_model", "text")),
            t.reassignedId(), t.references(), t.triggers(), t.triggerMaintained(), t.notReferences()));
        assertThat(primaryKeyMismatches(live, planted)).singleElement().asString().startsWith("chunks:");
    }

    @Test
    void aSerialKeyWithNoReassignedIdFails() {
        List<ExportTable> planted = replacing("catalog_links", t -> copy(t, t.primaryKey(), null,
            t.references(), t.triggers(), t.triggerMaintained(), t.notReferences()));
        assertThat(reassignedIdMismatches(live, planted)).singleElement().asString().startsWith("catalog_links:");
    }

    @Test
    void aForeignKeyMissingFromItsEntryFails() {
        List<ExportTable> planted = replacing("catalog_links", t -> copy(t, t.primaryKey(), t.reassignedId(),
            t.references().subList(0, 1), t.triggers(), t.triggerMaintained(), t.notReferences()));
        assertThat(foreignKeyMismatches(live, planted)).singleElement().asString().startsWith("catalog_links:");
    }

    @Test
    void aColumnThatNoLongerExistsFails() {
        List<ExportTable> planted = replacing("plans", t -> copy(t, t.primaryKey(), t.reassignedId(),
            t.references(), t.triggers(), t.triggerMaintained(), Set.of("plan_json", "default_bindings", "gone")));
        assertThat(goneColumns(live, planted)).containsExactly("plans.gone");
    }

    @Test
    void anUnlistedTriggerFails() {
        List<ExportTable> planted = replacing("topics", t -> copy(t, t.primaryKey(), t.reassignedId(),
            t.references(), Set.of(), t.triggerMaintained(), t.notReferences()));
        assertThat(triggerMismatches(live, planted)).singleElement().asString().startsWith("topics:");
    }

    @Test
    void anUnclassifiedIdColumnFails() {
        List<ExportTable> planted = replacing("nx_answer_runs", t -> copy(t, t.primaryKey(), t.reassignedId(),
            t.references().subList(0, 1), t.triggers(), t.triggerMaintained(), t.notReferences()));
        assertThat(unclassifiedIdColumns(live, planted)).containsExactly("nx_answer_runs.run_summary jsonb");
    }

    @Test
    void aSoftReferenceToAGoneParentColumnFails() {
        List<ExportTable> planted = replacing("taxonomy_centroids", t -> copy(t, t.primaryKey(),
            t.reassignedId(), List.of(Reference.soft("topic_id", "topics", "topic_pk")), t.triggers(),
            t.triggerMaintained(), t.notReferences()));
        assertThat(softReferenceProblems(live, planted)).containsExactly("taxonomy_centroids: topics.topic_pk");
    }
}
