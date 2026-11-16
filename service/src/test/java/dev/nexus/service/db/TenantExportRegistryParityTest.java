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
     * id is a bigint today; {@link #reassignedIdMismatches} fails if one is not, so the census widens with
     * the ids it hunts.
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
                     List<LiveFk> foreignKeys, Map<String, String> triggers) {
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
        Field<String> tgdef = DSL.function("pg_get_triggerdef", String.class, f("t", "oid", Long.class));
        Map<Long, Map<String, String>> triggers = new HashMap<>();
        for (Record r : ctx.select(tgrelid, tgname, tgdef)
                .from(DSL.table(DSL.name("pg_trigger")).as("t"))
                .where(tgrelid.in(rlsParents))
                .and(f("t", "tgisinternal", Boolean.class).isFalse())
                .fetch()) {
            triggers.computeIfAbsent(r.get(tgrelid), k -> new TreeMap<>())
                .put(r.get(tgname), firing(r.get(tgdef)));
        }

        Map<String, LiveTable> out = new TreeMap<>();
        for (long t : rlsParents) {
            Map<String, LiveColumn> cols = new LinkedHashMap<>();
            columnsByNum.getOrDefault(t, Map.of()).values().forEach(c -> cols.put(c.name(), c));
            out.put(nameOf.get(t), new LiveTable(nameOf.get(t), cols, pk.getOrDefault(t, List.of()),
                fks.getOrDefault(t, List.of()), triggers.getOrDefault(t, Map.of())));
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

    private static final java.util.regex.Pattern TRIGGER_DEF = java.util.regex.Pattern.compile(
        "^CREATE (?:CONSTRAINT )?TRIGGER \\S+ (.+?) ON \\S+ (.+?) EXECUTE (?:FUNCTION|PROCEDURE) ");

    /**
     * When a trigger fires, from its {@code pg_get_triggerdef} text: everything between the trigger's name
     * and its function except the table, so timing, events, any FROM, deferral, REFERENCING, the level and
     * any WHEN clause ({@code BEFORE INSERT OR UPDATE OF owner_id FOR EACH ROW}). The function body is not
     * compared; a trigger that keeps its name and firing but changes what it writes passes.
     */
    static String firing(String def) {
        java.util.regex.Matcher m = TRIGGER_DEF.matcher(def);
        if (!m.find()) throw new IllegalStateException("unparsed trigger definition: " + def);
        return m.group(1) + " " + m.group(2);
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
            t.columns().values().forEach(c -> {
                if (c.serial() && !t.primaryKey().contains(c.name())) {
                    wrong.add(name + "." + c.name() + ": a serial column outside the primary key; decide whether "
                        + "a restore reassigns it before classing it");
                }
            });
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
            LiveColumn col = own == null ? null : own.columns().get(r.columns().get(0));
            if (col == null) return; // a gone column is goneColumns' finding
            if (r.jsonPath() != null && !"jsonb".equals(col.type())) {
                wrong.add(e.name() + "." + col.name() + ": a JSON path on a column that is not jsonb");
            }
            if (r.array() && !col.type().endsWith("[]")) {
                wrong.add(e.name() + "." + col.name() + ": an array reference on a column that is not an array");
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
            if (!new TreeMap<>(t.triggers()).equals(new TreeMap<>(e.triggers()))) {
                wrong.add(name + ": live " + new TreeMap<>(t.triggers()) + ", entry " + new TreeMap<>(e.triggers()));
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

    /** An exported table refers only to exported tables (or global ones): otherwise its rows cannot load. */
    static List<String> referencesOutsideTheExport(List<ExportTable> tables) {
        Map<String, ExportTable> reg = byName(tables);
        List<String> wrong = new ArrayList<>();
        tables.forEach(e -> {
            if (!e.exported()) return;
            e.references().forEach(r -> {
                ExportTable parent = reg.get(r.parentName());
                if (parent != null && !parent.exported()) {
                    wrong.add(e.name() + " refers to excluded " + parent.name());
                }
                if (parent != null && e.exportClass() == TenantExport.ExportClass.DATA
                        && parent.exportClass() == TenantExport.ExportClass.DERIVED) {
                    // RDR-248 gate N2 may skip derived tables on a schema mismatch; data must stand alone.
                    wrong.add("data " + e.name() + " refers to derived " + parent.name());
                }
            });
        });
        return wrong;
    }

    /** Every exported table carries {@code tenant_id text}: the export pages by it and the restore rewrites it. */
    static List<String> exportedWithoutTenantColumn(Map<String, LiveTable> live, List<ExportTable> tables) {
        List<String> wrong = new ArrayList<>();
        tables.forEach(e -> {
            LiveTable t = live.get(e.name());
            if (t == null || !e.exported()) return;
            LiveColumn c = t.columns().get("tenant_id");
            if (c == null || !"text".equals(c.type())) wrong.add(e.name());
        });
        return wrong;
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
    void exportedTablesReferOnlyToExportedTablesAndDataNeverToDerived() {
        assertThat(referencesOutsideTheExport(TenantExport.TABLES)).isEmpty();
    }

    @Test
    void everyExportedTableHasATenantColumn() {
        assertThat(exportedWithoutTenantColumn(live, TenantExport.TABLES)).isEmpty();
    }

    @Test
    void aReferenceToAnExcludedOrDerivedTableFails() {
        List<ExportTable> toExcluded = replacing("memory", t -> copy(t, t.primaryKey(), t.reassignedId(),
            List.of(Reference.soft("id", "tuples", "id")), t.triggers(), t.triggerMaintained(), t.notReferences()));
        assertThat(referencesOutsideTheExport(toExcluded)).containsExactly("memory refers to excluded tuples");
        List<ExportTable> toDerived = replacing("memory", t -> copy(t, t.primaryKey(), t.reassignedId(),
            List.of(Reference.soft("id", "relevance_log", "id")), t.triggers(), t.triggerMaintained(),
            t.notReferences()));
        assertThat(referencesOutsideTheExport(toDerived))
            .containsExactly("data memory refers to derived relevance_log");
    }

    @Test
    void anExportedTableWithNoTenantColumnFails() {
        Map<String, LiveTable> planted = new TreeMap<>(live);
        LiveTable memory = live.get("memory");
        Map<String, LiveColumn> cols = new LinkedHashMap<>(memory.columns());
        cols.remove("tenant_id");
        planted.put("memory", new LiveTable("memory", cols, memory.primaryKey(), memory.foreignKeys(),
            memory.triggers()));
        assertThat(exportedWithoutTenantColumn(planted, TenantExport.TABLES)).containsExactly("memory");
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
    void theOnlySelfReferenceIsTopicsParent() {
        // Table load order cannot satisfy a self-reference (ExportTable.selfReferences); pin the live set so
        // a new one reaches the Phase 2 design.
        Set<String> liveSelf = new TreeSet<>();
        live.values().forEach(t -> t.foreignKeys().forEach(fk -> {
            if (fk.parent().equals(t.name())) liveSelf.add(t.name() + "." + fk);
        }));
        Set<String> registrySelf = new TreeSet<>();
        TenantExport.TABLES.forEach(e -> e.selfReferences().forEach(r -> registrySelf.add(e.name() + "."
            + new LiveFk(r.columns(), r.parentName(), r.parentColumns()))));
        assertThat(registrySelf).isEqualTo(liveSelf)
            .containsExactly("topics.tenant_id,parent_id -> topics(tenant_id,id)");
    }

    /**
     * Tables that carry a tenant_id but are global by design, so they have no RLS and no registry entry. A
     * tenant export never carries them (RDR-248: credentials live only in global tables).
     */
    static final Map<String, String> GLOBAL_TENANT_KEYED = new TreeMap<>(Map.of(
        "session_tokens", "credentials: a session token, never in a tenant's export",
        "service_tokens", "credentials: a tenant's service token, never in a tenant's export",
        "tuple_tenants", "engine registry of tenants that hold tuples",
        "request_ledger_tenants", "engine registry of tenants with request-ledger rows"));

    /**
     * Membership is RLS-only, so a tenant-scoped table that lost or never got its policy would drop out of
     * backups with no failure here. Every table in the product schemas with a {@code tenant_id} column must
     * be an RLS parent table, apart from the global ones named in {@link #GLOBAL_TENANT_KEYED}.
     */
    @Test
    void everyTenantScopedTableIsAnRlsTable() {
        Field<String> nsp = f("n", "nspname", String.class);
        Field<String> rel = f("c", "relname", String.class);
        List<String> unprotected = new ArrayList<>();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (Record r : ctx.select(nsp, rel)
                    .from(DSL.table(DSL.name("pg_class")).as("c"))
                    .join(DSL.table(DSL.name("pg_namespace")).as("n"))
                    .on(f("c", "relnamespace", Long.class).eq(f("n", "oid", Long.class)))
                    .join(DSL.table(DSL.name("pg_attribute")).as("a"))
                    .on(f("a", "attrelid", Long.class).eq(f("c", "oid", Long.class)))
                    .where(f("c", "relkind", String.class).in("r", "p"))
                    .and(f("c", "relispartition", Boolean.class).isFalse())
                    .and(f("c", "relrowsecurity", Boolean.class).isFalse())
                    .and(nsp.notIn("pg_catalog", "information_schema"))
                    .and(f("a", "attname", String.class).eq("tenant_id"))
                    .and(f("a", "attisdropped", Boolean.class).isFalse())
                    .fetch()) {
                unprotected.add(TenantExport.qualify(r.get(nsp), r.get(rel)));
            }
        } catch (java.sql.SQLException ex) {
            throw new IllegalStateException(ex);
        }
        assertThat(new TreeSet<>(unprotected)).as("tables with a tenant_id column and no row-level security, "
            + "other than the global ones named in GLOBAL_TENANT_KEYED").isEqualTo(GLOBAL_TENANT_KEYED.keySet());
    }

    // ── each check bites: a planted registry one step away from the real one fails it ────────────────

    private static List<ExportTable> replacing(String name, java.util.function.UnaryOperator<ExportTable> edit) {
        List<ExportTable> out = new ArrayList<>();
        for (ExportTable t : TenantExport.TABLES) out.add(t.name().equals(name) ? edit.apply(t) : t);
        return out;
    }

    private static ExportTable copy(ExportTable t, List<Column> pk, String reassigned, List<Reference> refs,
                                    Map<String, String> triggers, Set<String> maintained, Set<String> notRefs) {
        return new ExportTable(t.schema(), t.table(), t.exportClass(), t.reason(), pk, reassigned, refs,
            triggers, t.loadsAfter(), maintained, notRefs);
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
            t.references(), Map.of(), t.triggerMaintained(), t.notReferences()));
        assertThat(triggerMismatches(live, planted)).singleElement().asString().startsWith("topics:");
    }

    @Test
    void aTriggerThatChangesWhenItFiresFails() {
        List<ExportTable> planted = replacing("topics", t -> copy(t, t.primaryKey(), t.reassignedId(),
            t.references(), Map.of("trg_topics_updated_at", "BEFORE INSERT OR UPDATE FOR EACH ROW"),
            t.triggerMaintained(), t.notReferences()));
        assertThat(triggerMismatches(live, planted)).singleElement().asString().startsWith("topics:");
    }

    @Test
    void anUnclassifiedIdColumnFails() {
        List<ExportTable> planted = replacing("nx_answer_runs", t -> copy(t, t.primaryKey(), t.reassignedId(),
            t.references().subList(0, 1), t.triggers(), t.triggerMaintained(), t.notReferences()));
        assertThat(unclassifiedIdColumns(live, planted)).containsExactly("nx_answer_runs.run_summary jsonb");
    }

    @Test
    void firingKeepsEverythingButTheNameTableAndFunction() {
        assertThat(firing("CREATE TRIGGER t BEFORE UPDATE ON nexus.topics FOR EACH ROW "
            + "EXECUTE FUNCTION nexus.stamp_updated_at()")).isEqualTo("BEFORE UPDATE FOR EACH ROW");
        assertThat(firing("CREATE TRIGGER t BEFORE INSERT OR UPDATE OF owner_id, content_type ON "
            + "nexus.catalog_collections FOR EACH ROW WHEN ((new.owner_id IS NULL)) EXECUTE FUNCTION nexus.f()"))
            .isEqualTo("BEFORE INSERT OR UPDATE OF owner_id, content_type FOR EACH ROW WHEN ((new.owner_id IS NULL))");
        assertThat(firing("CREATE TRIGGER t AFTER INSERT ON nexus.topic_assignments REFERENCING NEW TABLE AS "
            + "new_rows FOR EACH STATEMENT EXECUTE FUNCTION nexus.f()"))
            .isEqualTo("AFTER INSERT REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT");
        assertThat(firing("CREATE CONSTRAINT TRIGGER t AFTER INSERT ON nexus.x DEFERRABLE INITIALLY DEFERRED "
            + "FOR EACH ROW EXECUTE PROCEDURE nexus.f()"))
            .isEqualTo("AFTER INSERT DEFERRABLE INITIALLY DEFERRED FOR EACH ROW");
    }

    @Test
    void aSerialColumnOutsideThePrimaryKeyFails() {
        Map<String, LiveTable> planted = new TreeMap<>(live);
        LiveTable plans = live.get("plans");
        planted.put("plans", new LiveTable("plans", plans.columns(), List.of(), plans.foreignKeys(),
            plans.triggers()));
        assertThat(reassignedIdMismatches(planted, TenantExport.TABLES))
            .anySatisfy(s -> assertThat(s).startsWith("plans.id: a serial column outside the primary key"));
    }

    @Test
    void anEntryWithNoLiveTableFails() {
        List<ExportTable> planted = new ArrayList<>(TenantExport.TABLES);
        planted.add(new ExportTable("nexus", "ghost", TenantExport.ExportClass.DATA, null, List.of(), null,
            List.of(), Map.of(), Set.of(), Set.of(), Set.of()));
        assertThat(missingOrStaleTables(live, planted)).containsExactly("stale entry: ghost");
    }

    @Test
    void aReassignedIdOnAKeyThatIsNotSerialFails() {
        List<ExportTable> planted = replacing("catalog_meta", t -> copy(t, t.primaryKey(), "key",
            t.references(), t.triggers(), t.triggerMaintained(), t.notReferences()));
        assertThat(reassignedIdMismatches(live, planted)).singleElement().asString().startsWith("catalog_meta:");
    }

    @Test
    void anUnclassifiedBigintFails() {
        List<ExportTable> planted = replacing("catalog_owners", t -> copy(t, t.primaryKey(), t.reassignedId(),
            t.references(), t.triggers(), t.triggerMaintained(), Set.of()));
        assertThat(unclassifiedIdColumns(live, planted)).containsExactly("catalog_owners.next_seq bigint");
    }

    @Test
    void anUnclassifiedBigintArrayFails() {
        List<ExportTable> planted = replacing("memory_summaries", t -> copy(t, t.primaryKey(), t.reassignedId(),
            List.of(), t.triggers(), t.triggerMaintained(), t.notReferences()));
        assertThat(unclassifiedIdColumns(live, planted)).containsExactly("memory_summaries.source_ids bigint[]");
    }

    @Test
    void aJsonPathOnAColumnThatIsNotJsonbFails() {
        List<ExportTable> planted = replacing("nx_answer_runs", t -> copy(t, t.primaryKey(), t.reassignedId(),
            List.of(Reference.softJson("plan_id", "run.plan_id", "plans", "id")), t.triggers(),
            t.triggerMaintained(), Set.of("duration_ms", "run_summary")));
        assertThat(softReferenceProblems(live, planted))
            .containsExactly("nx_answer_runs.plan_id: a JSON path on a column that is not jsonb");
    }

    @Test
    void anArrayReferenceOnAColumnThatIsNotAnArrayFails() {
        List<ExportTable> planted = replacing("taxonomy_centroids", t -> copy(t, t.primaryKey(),
            t.reassignedId(), List.of(Reference.softArray("topic_id", "topics", "id")), t.triggers(),
            t.triggerMaintained(), t.notReferences()));
        assertThat(softReferenceProblems(live, planted))
            .containsExactly("taxonomy_centroids.topic_id: an array reference on a column that is not an array");
    }

    @Test
    void aSoftReferenceToAGoneParentColumnFails() {
        List<ExportTable> planted = replacing("taxonomy_centroids", t -> copy(t, t.primaryKey(),
            t.reassignedId(), List.of(Reference.soft("topic_id", "topics", "topic_pk")), t.triggers(),
            t.triggerMaintained(), t.notReferences()));
        assertThat(softReferenceProblems(live, planted)).containsExactly("taxonomy_centroids: topics.topic_pk");
    }

    // ── the import's rows of the registry (RDR-248 Step 5, bead nexus-csayy.6) ───────────────────────

    /** A soft reference with no S1 rule: the load would not know what a value with no map entry becomes. */
    static List<String> softReferencesWithoutARule(List<ExportTable> tables) {
        List<String> wrong = new ArrayList<>();
        tables.forEach(e -> e.references().forEach(r -> {
            if (!r.foreignKey() && r.softRule() == null) wrong.add(e.name() + "." + r.columns().get(0));
        }));
        return wrong;
    }

    /** A seeded key on a table that is not data, or with a value count that is not the key's columns after tenant_id, or on a non-text key. */
    static List<String> seededKeyProblems(Map<String, LiveTable> live, List<ExportTable> tables) {
        List<String> wrong = new ArrayList<>();
        tables.forEach(e -> {
            if (e.seededKeys().isEmpty()) return;
            if (e.exportClass() != TenantExport.ExportClass.DATA) {
                wrong.add(e.name() + ": seeded keys on a table that is not data");
                return;
            }
            List<String> keyColumns = e.primaryKey().stream().map(TenantExport.Column::name).filter(n -> !n.equals("tenant_id")).toList();
            LiveTable t = live.get(e.name());
            e.seededKeys().forEach(k -> {
                if (k.values().size() != keyColumns.size()) {
                    wrong.add(e.name() + ": a seeded key has " + k.values().size() + " values, the key has " + keyColumns);
                }
            });
            if (t != null) {
                keyColumns.forEach(c -> {
                    LiveColumn col = t.columns().get(c);
                    if (col == null || !"text".equals(col.type())) wrong.add(e.name() + "." + c + ": a seeded key column that is not text");
                });
            }
        });
        return wrong;
    }

    /** A retired entry that names a table or column the live schema still has: it is not retired. */
    static List<String> retiredButStillLive(Map<String, LiveTable> live, List<TenantExport.Retired> retired) {
        List<String> wrong = new ArrayList<>();
        retired.forEach(r -> {
            LiveTable t = live.get(r.table());
            if (t == null) return;
            if (r.column() == null) {
                wrong.add(r.table() + ": retired by " + r.changeset() + " and still a live RLS table");
            } else if (t.columns().containsKey(r.column())) {
                wrong.add(r.table() + "." + r.column() + ": retired by " + r.changeset() + " and still a live column");
            }
        });
        return wrong;
    }

    @Test
    void everySoftReferenceHasARuleForAValueWithNoMapEntry() {
        assertThat(softReferencesWithoutARule(TenantExport.TABLES)).isEmpty();
        assertThat(TenantExport.TABLES.stream().flatMap(t -> t.references().stream()).filter(r -> !r.foreignKey()).count())
            .as("non-vacuity: the four soft references of RDR-248 S1").isEqualTo(4);
    }

    @Test
    void aSoftReferenceWithNoRuleFails() {
        List<ExportTable> planted = replacing("nx_answer_runs", t -> copy(t, t.primaryKey(), t.reassignedId(),
            List.of(Reference.soft("plan_id", "plans", "id")), t.triggers(), t.triggerMaintained(), t.notReferences()));
        assertThat(softReferencesWithoutARule(planted)).containsExactly("nx_answer_runs.plan_id");
    }

    @Test
    void everySeededKeyFitsItsTable() {
        assertThat(seededKeyProblems(live, TenantExport.TABLES)).isEmpty();
        assertThat(TenantExport.TABLES.stream().filter(t -> !t.seededKeys().isEmpty()).map(ExportTable::name))
            .as("non-vacuity: the two kinds of row the engine writes for a tenant").containsExactlyInAnyOrder("catalog_meta", "embedding_profile");
    }

    @Test
    void aSeededKeyWithTheWrongNumberOfValuesFails() {
        List<ExportTable> planted = replacing("catalog_meta", t -> new ExportTable(t.schema(), t.table(), t.exportClass(), t.reason(),
            t.primaryKey(), t.reassignedId(), t.references(), t.triggers(), t.loadsAfter(), t.triggerMaintained(), t.notReferences(),
            List.of(new TenantExport.SeededKey(List.of("a", "b"), false, false))));
        assertThat(seededKeyProblems(live, planted)).singleElement().asString().contains("catalog_meta").contains("2 values");
    }

    @Test
    void theRetiredListNamesNothingTheLiveSchemaStillHas() {
        assertThat(retiredButStillLive(live, TenantExport.RETIRED)).isEmpty();
    }

    @Test
    void aRetiredEntryForALiveColumnOrTableFails() {
        assertThat(retiredButStillLive(live, List.of(new TenantExport.Retired("memory", "title", "memory-099"))))
            .containsExactly("memory.title: retired by memory-099 and still a live column");
        assertThat(retiredButStillLive(live, List.of(new TenantExport.Retired("chunks", null, "chunks-099"))))
            .containsExactly("chunks: retired by chunks-099 and still a live RLS table");
        assertThat(retiredButStillLive(live, List.of(new TenantExport.Retired("long_gone", null, "gone-1"),
            new TenantExport.Retired("memory", "long_gone_column", "memory-098")))).isEmpty();
    }
}
