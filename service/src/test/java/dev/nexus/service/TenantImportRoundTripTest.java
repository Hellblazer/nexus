// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.db.TenantExport;
import dev.nexus.service.db.TenantExport.ExportClass;
import dev.nexus.service.db.TenantExport.ExportTable;
import dev.nexus.service.db.TenantExport.Reference;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Query;
import org.jooq.Record;
import org.jooq.Table;
import org.jooq.impl.SQLDataType;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Export, then import into an empty tenant (RDR-248 Test Plan, "export then import into an empty tenant"; bead
 * nexus-csayy.6): every exported table of the registry is seeded for the source tenant (the export battery's seed:
 * every column filled in one row, NULL in another), the tenant is exported over the routes and the file restored into
 * a fresh tenant of the same database. Per-table counts and per-table row contents (every column except the ones a
 * restore changes by design: the tenant, reassigned ids and the columns that refer to them, trigger-maintained
 * columns) are equal, and every reference to a reassigned id points at the same row it did in the source. Then the
 * next engine insert into each reassigned table succeeds (the sequences are right), the source is untouched, and the
 * load is reported.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TenantImportRoundTripTest {

    private static final String SRC = "t-imp-rt-src";

    private ImportTestEnv env;
    private String srcToken;
    private ImportTestEnv.BackupFile file;

    @BeforeAll
    void startAll() throws Exception {
        env = ImportTestEnv.start();
        srcToken = env.newTenant(SRC);
        List<String> failures = new ArrayList<>();
        env.su(ctx -> {
            TenantExportBatteryTest.Seed.rows(ctx, SRC, 3, failures);
            TenantExportBatteryTest.Seed.fillNulls(ctx, SRC, failures);
            return null;
        });
        assertThat(failures).as("the seed").isEmpty();
        file = env.export(srcToken);
    }

    @AfterAll
    void stopAll() {
        if (env != null) env.close();
    }

    // ── helpers ─────────────────────────────────────────────────────────────────────────────────

    private static Condition tenantIs(String tenant) {
        return DSL.field(DSL.name("tenant_id"), String.class).eq(tenant);
    }

    private static Field<Object> col(String name) {
        return DSL.field(DSL.name(name));
    }

    private static Object normalize(Object v) {
        if (v == null) return "∅";
        if (v instanceof byte[] b) return java.util.HexFormat.of().formatHex(b);
        if (v instanceof Object[] a) return Arrays.deepToString(a);
        if (v instanceof JSONB j) return j.data();
        return v.toString();
    }

    /** Columns whose values a restore changes by design: they are compared through the references instead. */
    private static Set<String> changedByDesign(ExportTable t) {
        Set<String> out = new HashSet<>(Set.of("tenant_id"));
        if (t.reassignedId() != null) out.add(t.reassignedId());
        out.addAll(t.triggerMaintained());
        for (Reference r : t.references()) {
            var parent = TenantExport.entry(r.parentTable());
            if (parent.isPresent() && parent.get().reassignedId() != null && r.parentColumns().contains(parent.get().reassignedId())) {
                out.add(r.columns().get(r.parentColumns().indexOf(parent.get().reassignedId())));
            }
        }
        return out;
    }

    private List<String> rowsOf(String table, String tenant, Set<String> skip) {
        return env.su(ctx -> {
            List<String> live = new ArrayList<>();
            for (JsonNode c : file.table(table).withArray("columns")) {
                if (!skip.contains(c.get("name").asText())) live.add(c.get("name").asText());
            }
            List<String> rows = new ArrayList<>();
            for (Record r : ctx.select(live.stream().map(TenantImportRoundTripTest::col).toList())
                    .from(DSL.table(DSL.name("nexus", table))).where(DSL.field(DSL.name("tenant_id"), String.class).eq(tenant)).fetch()) {
                List<Object> values = new ArrayList<>();
                for (int i = 0; i < live.size(); i++) values.add(normalize(r.get(i)));
                rows.add(values.toString());
            }
            Collections.sort(rows);
            return rows;
        });
    }

    /** Maps a reassigned id to a label the test can compare across tenants. */
    private Map<Long, String> labels(String table, String labelColumn, String tenant) {
        return env.su(ctx -> {
            Map<Long, String> m = new LinkedHashMap<>();
            for (Record r : ctx.select(DSL.field(DSL.name("id"), Long.class), DSL.field(DSL.name(labelColumn), String.class))
                    .from(DSL.table(DSL.name("nexus", table))).where(DSL.field(DSL.name("tenant_id"), String.class).eq(tenant)).fetch()) {
                m.put(r.get(0, Long.class), r.get(1, String.class));
            }
            return m;
        });
    }

    // ── the tests ───────────────────────────────────────────────────────────────────────────────

    @Test
    void everyTableRestoresWithEqualCountsAndContents() throws Exception {
        String tenant = "t-imp-rt-a";
        String token = env.newTenant(tenant);
        long before = env.count("memory", SRC);

        JsonNode end = env.restore(token, file);

        assertThat(end.get("status").asText()).as(end.toString()).isEqualTo("committed");
        for (ExportTable t : TenantExport.TABLES) {
            if (!t.exported()) continue;
            long expected = env.count(t.table(), SRC);
            assertThat(expected).as("the seed filled " + t.name()).isPositive();
            long actual = env.count(t.table(), tenant);
            if (t.name().equals("catalog_meta")) {
                assertThat(actual).as("catalog_meta: the source's rows, its own ghost-sweep row kept, and the marker").isEqualTo(expected + 1);
                continue;
            }
            assertThat(actual).as("rows of " + t.name()).isEqualTo(expected);
            assertThat(rowsOf(t.table(), tenant, changedByDesign(t))).as("contents of " + t.name())
                .isEqualTo(rowsOf(t.table(), SRC, changedByDesign(t)));
        }
        assertThat(env.count("memory", SRC)).as("the source is untouched").isEqualTo(before);
        JsonNode report = end.get("report");
        assertThat(report.get("rows_dropped").asLong()).isZero();
        assertThat(report.get("skipped_tables").size()).isZero();
        for (JsonNode t : report.get("tables")) {
            assertThat(t.get("inserted").asLong() + t.get("seeded_skipped").asLong() + t.get("dropped").asLong())
                .as("staged = inserted + seeded + dropped for " + t.get("table")).isEqualTo(t.get("staged").asLong());
        }
    }

    @Test
    void referencesPointAtTheSameRowsAfterTheIdsAreReassigned() throws Exception {
        String tenant = "t-imp-rt-b";
        String token = env.newTenant(tenant);
        assertThat(env.restore(token, file).get("status").asText()).isEqualTo("committed");

        // the ids really changed (a restore that kept them would pass every comparison below)
        Map<Long, String> srcMemory = labels("memory", "title", SRC);
        Map<Long, String> dstMemory = labels("memory", "title", tenant);
        assertThat(dstMemory.keySet()).as("memory ids are reassigned").doesNotContainAnyElementsOf(srcMemory.keySet());

        // topics: every topic's parent has the same label as in the source (the self foreign key, in one statement)
        assertThat(parentLabels(tenant)).isEqualTo(parentLabels(SRC)).containsValue("topic 3");
        // memory_summaries.source_ids name the same memories
        assertThat(summarySources(tenant, dstMemory)).isEqualTo(summarySources(SRC, srcMemory));
        // nx_answer_runs.plan_id and run_summary.run.plan_id name the same plan
        assertThat(runPlans(tenant)).isEqualTo(runPlans(SRC)).isNotEmpty();
        // topic_links, topic_assignments and taxonomy_centroids name the same topics
        assertThat(topicRefs(tenant)).isEqualTo(topicRefs(SRC)).isNotEmpty();
    }

    private Map<String, String> parentLabels(String tenant) {
        Map<Long, String> labels = labels("topics", "label", tenant);
        return env.su(ctx -> {
            Map<String, String> m = new LinkedHashMap<>();
            for (Record r : ctx.select(DSL.field(DSL.name("label"), String.class), DSL.field(DSL.name("parent_id"), Long.class))
                    .from(DSL.table(DSL.name("nexus", "topics"))).where(DSL.field(DSL.name("tenant_id"), String.class).eq(tenant)).fetch()) {
                Long parent = r.get(1, Long.class);
                if (parent != null) m.put(r.get(0, String.class), labels.get(parent));
            }
            return m;
        });
    }

    private Map<String, List<String>> summarySources(String tenant, Map<Long, String> memoryTitles) {
        return env.su(ctx -> {
            Map<String, List<String>> m = new LinkedHashMap<>();
            for (Record r : ctx.select(DSL.field(DSL.name("content"), String.class), DSL.field(DSL.name("source_ids"), Long[].class))
                    .from(DSL.table(DSL.name("nexus", "memory_summaries"))).where(DSL.field(DSL.name("tenant_id"), String.class).eq(tenant)).fetch()) {
                List<String> titles = new ArrayList<>();
                for (Long id : r.get(1, Long[].class)) titles.add(memoryTitles.get(id));
                m.put(r.get(0, String.class), titles);
            }
            return m;
        });
    }

    private Map<String, String> runPlans(String tenant) {
        Map<Long, String> plans = labels("plans", "query", tenant);
        return env.su(ctx -> {
            Map<String, String> m = new LinkedHashMap<>();
            for (Record r : ctx.select(DSL.field(DSL.name("question"), String.class), DSL.field(DSL.name("plan_id"), Long.class),
                        DSL.field(DSL.name("run_summary"), JSONB.class))
                    .from(DSL.table(DSL.name("nexus", "nx_answer_runs"))).where(DSL.field(DSL.name("tenant_id"), String.class).eq(tenant)).fetch()) {
                Long plan = r.get(1, Long.class);
                String summary = r.get(2, JSONB.class) == null ? "∅" : plans.get(Long.parseLong(
                    r.get(2, JSONB.class).data().replaceAll("(?s).*\"plan_id\":\\s*(-?\\d+).*", "$1")));
                if (plan != null) m.put(r.get(0, String.class), plans.get(plan) + "/" + summary);
            }
            return m;
        });
    }

    private Set<String> topicRefs(String tenant) {
        Map<Long, String> topics = labels("topics", "label", tenant);
        return env.su(ctx -> {
            Set<String> refs = new java.util.TreeSet<>();
            for (Record r : ctx.select(DSL.field(DSL.name("from_topic_id"), Long.class), DSL.field(DSL.name("to_topic_id"), Long.class))
                    .from(DSL.table(DSL.name("nexus", "topic_links"))).where(DSL.field(DSL.name("tenant_id"), String.class).eq(tenant)).fetch()) {
                refs.add("link " + topics.get(r.get(0, Long.class)) + ">" + topics.get(r.get(1, Long.class)));
            }
            for (Record r : ctx.select(DSL.field(DSL.name("topic_id"), Long.class), DSL.field(DSL.name("embedding_model"), String.class))
                    .from(DSL.table(DSL.name("nexus", "topic_assignments"))).where(DSL.field(DSL.name("tenant_id"), String.class).eq(tenant)).fetch()) {
                refs.add("assignment " + topics.get(r.get(0, Long.class)));
            }
            for (Record r : ctx.select(DSL.field(DSL.name("topic_id"), Long.class), DSL.field(DSL.name("label"), String.class))
                    .from(DSL.table(DSL.name("nexus", "taxonomy_centroids"))).where(DSL.field(DSL.name("tenant_id"), String.class).eq(tenant)).fetch()) {
                refs.add("centroid " + r.get(1, String.class) + "@" + topics.get(r.get(0, Long.class)));
            }
            return refs;
        });
    }

    // ── references resolve, and the checks above can fail ───────────────────────────────────────

    /** What {@link #resolution} found for one reference column: rows it looked at, and positive values that name no row of the tenant. */
    private record Resolution(String reference, long examined, long unresolved) {
    }

    /**
     * Every reference of every exported table whose parent is exported, resolved inside {@code tenant}: a foreign key
     * must find its parent row in the tenant, and a soft reference's positive values (a NULL, 0 or negative value names
     * no row by design) must find theirs, as a restored tenant must hold no pointer into another tenant's ids.
     */
    private List<Resolution> resolution(String tenant) {
        return env.su(ctx -> {
            List<Resolution> out = new ArrayList<>();
            Field<String> childTenant = DSL.field(DSL.name("c", "tenant_id"), String.class);
            for (ExportTable t : TenantExport.TABLES) {
                if (!t.exported()) continue;
                for (Reference r : t.references()) {
                    var parent = TenantExport.entry(r.parentTable());
                    if (parent.isEmpty() || !parent.get().exported()) continue;
                    Table<?> child = DSL.table(DSL.name("nexus", t.table())).as("c");
                    Table<?> par = DSL.table(DSL.name("nexus", parent.get().table())).as("p");
                    Field<String> parentTenant = DSL.field(DSL.name("p", "tenant_id"), String.class);
                    Table<?> from = child;
                    Condition present = DSL.trueCondition();
                    Condition parentMatch;
                    if (r.array()) {
                        // each element of the array is a value to resolve
                        Field<Long[]> array = DSL.field(DSL.name("c", r.columns().get(0)), SQLDataType.BIGINT.getArrayDataType());
                        from = child.crossJoin(DSL.unnest(array).as("u", "v"));
                        Field<Long> v = DSL.field(DSL.name("u", "v"), Long.class);
                        present = v.gt(0L);
                        parentMatch = DSL.field(DSL.name("p", r.parentColumns().get(0)), Long.class).eq(v);
                    } else if (r.jsonPath() != null) {
                        List<Field<?>> args = new ArrayList<>();
                        args.add(DSL.field(DSL.name("c", r.columns().get(0)), SQLDataType.JSONB));
                        for (String step : r.jsonPath().split("\\.")) args.add(DSL.val(step));
                        Field<Long> v = DSL.function("jsonb_extract_path_text", SQLDataType.VARCHAR, args.toArray(new Field<?>[0]))
                            .cast(SQLDataType.BIGINT);
                        present = v.gt(0L);
                        parentMatch = DSL.field(DSL.name("p", r.parentColumns().get(0)), Long.class).eq(v);
                    } else {
                        parentMatch = DSL.trueCondition();
                        for (int i = 0; i < r.columns().size(); i++) {
                            String c = r.columns().get(i), pc = r.parentColumns().get(i);
                            if (c.equals("tenant_id")) continue;
                            Field<Object> cv = DSL.field(DSL.name("c", c));
                            present = present.and(r.foreignKey() ? cv.isNotNull() : DSL.field(DSL.name("c", c), Long.class).gt(0L));
                            parentMatch = parentMatch.and(DSL.field(DSL.name("p", pc)).eq(cv));
                        }
                    }
                    Condition unresolved = present.and(DSL.notExists(DSL.selectOne().from(par).where(parentTenant.eq(tenant), parentMatch)));
                    Record row = ctx.select(DSL.count().filterWhere(present), DSL.count().filterWhere(unresolved))
                        .from(from).where(childTenant.eq(tenant)).fetchOne();
                    out.add(new Resolution(t.name() + "." + String.join(",", r.columns()) + " -> " + parent.get().name(),
                        row.get(0, Long.class), row.get(1, Long.class)));
                }
            }
            return out;
        });
    }

    @Test
    void everyForeignKeyAndSoftReferenceOfTheRestoredTenantResolvesToItsOwnNewRows() throws Exception {
        String tenant = "t-imp-rt-d";
        assertThat(env.restore(env.newTenant(tenant), file).get("status").asText()).isEqualTo("committed");

        List<Resolution> found = resolution(tenant);

        assertThat(found).as("the check looked at the references of the registry").hasSizeGreaterThanOrEqualTo(20);
        assertThat(found.stream().filter(r -> r.unresolved() > 0).toList()).as("references that name no row of the tenant").isEmpty();
        // a check that examined nothing proves nothing: every reference must have held a value to resolve
        assertThat(found.stream().filter(r -> r.examined() == 0).map(Resolution::reference).toList())
            .as("references with no value to resolve (the seed must reach every one)").isEmpty();
        // and the source tenant resolves the same way, so the check is not tuned to the restore
        assertThat(resolution(SRC).stream().filter(r -> r.unresolved() > 0).toList()).isEmpty();
    }

    @Test
    void theChecksFailWhenAnyKindOfReferenceKeepsTheSourcesIdOrAValueIsDamaged() throws Exception {
        // A restore that kept the source's ids (the regression the reassignment exists to prevent) is simulated, one reference
        // kind at a time on a tenant of its own, by pointing that reference back at the SOURCE tenant's id. The resolution check
        // must name exactly that reference. Kinds: a bigint[] soft reference, a scalar soft reference, a soft reference inside a
        // jsonb path, a soft reference of a derived table (the centroid's topic), and a foreign key with no tenant column
        // (a step's run). The composite foreign keys cannot be pointed at another tenant: the database refuses it.
        long srcMemoryId = labels("memory", "title", SRC).keySet().iterator().next();
        long srcPlanId = labels("plans", "query", SRC).keySet().iterator().next();
        long srcTopicId = labels("topics", "label", SRC).keySet().iterator().next();
        long srcRunId = labels("nx_answer_runs", "question", SRC).keySet().iterator().next();
        // each damage is a typed update of ONE tenant's rows, built from the tenant it damages
        Map<String, java.util.function.BiFunction<DSLContext, String, Query>> damage = new LinkedHashMap<>();
        damage.put("memory_summaries.source_ids", (ctx, tn) -> ctx.update(DSL.table(DSL.name("nexus", "memory_summaries")))
            .set(DSL.field(DSL.name("source_ids"), Long[].class), new Long[] {srcMemoryId}).where(tenantIs(tn)));
        damage.put("nx_answer_runs.plan_id", (ctx, tn) -> ctx.update(DSL.table(DSL.name("nexus", "nx_answer_runs")))
            .set(DSL.field(DSL.name("plan_id"), Long.class), srcPlanId).where(tenantIs(tn)));
        damage.put("nx_answer_runs.run_summary", (ctx, tn) -> ctx.update(DSL.table(DSL.name("nexus", "nx_answer_runs")))
            .set(DSL.field(DSL.name("run_summary"), JSONB.class), JSONB.valueOf("{\"run\": {\"plan_id\": " + srcPlanId + "}}"))
            .where(tenantIs(tn), DSL.field(DSL.name("run_summary")).isNotNull()));
        damage.put("taxonomy_centroids.topic_id", (ctx, tn) -> ctx.update(DSL.table(DSL.name("nexus", "taxonomy_centroids")))
            .set(DSL.field(DSL.name("topic_id"), Long.class), srcTopicId).where(tenantIs(tn)));
        // One step only, with a step_index no seeded step holds: (run_id, step_index) is the primary key over the whole table,
        // so moving every step, or a step whose index the source run already has, would collide instead of damaging.
        damage.put("nx_answer_steps.run_id", (ctx, tn) -> {
            Table<?> steps = DSL.table(DSL.name("nexus", "nx_answer_steps"));
            Field<Long> runId = DSL.field(DSL.name("run_id"), Long.class);
            Field<Integer> stepIndex = DSL.field(DSL.name("step_index"), Integer.class);
            Record one = ctx.select(runId, stepIndex).from(steps).where(tenantIs(tn)).orderBy(runId, stepIndex).limit(1).fetchOne();
            return ctx.update(steps).set(runId, srcRunId).set(stepIndex, 1_000_000)
                .where(tenantIs(tn), runId.eq(one.get(runId)), stepIndex.eq(one.get(stepIndex)));
        });
        int n = 0;
        for (var d : damage.entrySet()) {
            String tenant = "t-imp-rt-e" + n++;
            assertThat(env.restore(env.newTenant(tenant), file).get("status").asText()).isEqualTo("committed");
            assertThat(resolution(tenant).stream().filter(r -> r.unresolved() > 0)).as("sound before the damage to " + d.getKey()).isEmpty();

            env.su(ctx -> d.getValue().apply(ctx, tenant).execute());

            assertThat(resolution(tenant).stream().filter(r -> r.unresolved() > 0).map(Resolution::reference).toList())
                .as("the resolution check names " + d.getKey() + " once it holds the source's id")
                .anyMatch(r -> r.startsWith(d.getKey()));
        }

        // the join comparisons see the same damage in the references they read
        String tenant = "t-imp-rt-e-joins";
        assertThat(env.restore(env.newTenant(tenant), file).get("status").asText()).isEqualTo("committed");
        Map<Long, String> srcMemory = labels("memory", "title", SRC);
        Map<Long, String> dstMemory = labels("memory", "title", tenant);
        assertThat(summarySources(tenant, dstMemory)).as("sound before the damage").isEqualTo(summarySources(SRC, srcMemory));
        env.su(ctx -> {
            damage.get("memory_summaries.source_ids").apply(ctx, tenant).execute();
            damage.get("nx_answer_runs.plan_id").apply(ctx, tenant).execute();
            return null;
        });
        assertThat(summarySources(tenant, dstMemory)).as("the join comparison sees it too").isNotEqualTo(summarySources(SRC, srcMemory));
        assertThat(runPlans(tenant)).isNotEqualTo(runPlans(SRC));

        // and the contents comparison sees a value changed in a column no restore touches
        ExportTable memory = TenantExport.entry("memory").orElseThrow();
        env.su(ctx -> {
            Table<?> mem = DSL.table(DSL.name("nexus", "memory"));
            Field<Long> id = DSL.field(DSL.name("id"), Long.class);
            Field<String> content = DSL.field(DSL.name("content"), String.class);
            Long first = ctx.select(DSL.min(id)).from(mem).where(tenantIs(tenant)).fetchOne(0, Long.class);
            return ctx.update(mem).set(content, content.concat("!")).where(tenantIs(tenant), id.eq(first)).execute();
        });
        assertThat(rowsOf("memory", tenant, changedByDesign(memory))).isNotEqualTo(rowsOf("memory", SRC, changedByDesign(memory)));
    }

    @Test
    void aSecondGenerationRestoreOfTheSeededRowsIsEqualToTheSourceOnEveryTableAndEveryReference() throws Exception {
        // restore A -> C, export C, restore that file -> E: every comparison of the first generation holds against the ORIGINAL source.
        // This seed holds no reaped or deleted parent, so it does not exercise the negation rule across generations: that row of the
        // Test Plan is TenantImportSoftReferencesTest.aSecondBackupAndRestoreLeavesTheNegatedValuesNegativeAndRemapsTheRest.
        String first = "t-imp-gen-1", second = "t-imp-gen-2";
        String firstToken = env.newTenant(first);
        assertThat(env.restore(firstToken, file).get("status").asText()).isEqualTo("committed");
        ImportTestEnv.BackupFile again = env.export(firstToken);

        JsonNode end = env.restore(env.newTenant(second), again);

        assertThat(end.get("status").asText()).as(end.toString()).isEqualTo("committed");
        for (ExportTable t : TenantExport.TABLES) {
            if (!t.exported()) continue;
            if (t.name().equals("catalog_meta")) {
                // the source's rows, the tenant's own ghost-sweep row, and the marker of THIS restore (the first marker came in the file and is replaced)
                assertThat(env.count(t.table(), second)).as("catalog_meta").isEqualTo(env.count(t.table(), SRC) + 1);
                continue;
            }
            assertThat(env.count(t.table(), second)).as("rows of " + t.name()).isEqualTo(env.count(t.table(), SRC));
            assertThat(rowsOf(t.table(), second, changedByDesign(t))).as("contents of " + t.name())
                .isEqualTo(rowsOf(t.table(), SRC, changedByDesign(t)));
        }
        Map<Long, String> srcMemory = labels("memory", "title", SRC);
        Map<Long, String> dstMemory = labels("memory", "title", second);
        assertThat(dstMemory.keySet()).as("a second generation has ids of its own").doesNotContainAnyElementsOf(srcMemory.keySet())
            .doesNotContainAnyElementsOf(labels("memory", "title", first).keySet());
        assertThat(parentLabels(second)).isEqualTo(parentLabels(SRC));
        assertThat(summarySources(second, dstMemory)).isEqualTo(summarySources(SRC, srcMemory));
        assertThat(runPlans(second)).isEqualTo(runPlans(SRC)).isNotEmpty();
        assertThat(topicRefs(second)).isEqualTo(topicRefs(SRC)).isNotEmpty();
        assertThat(resolution(second).stream().filter(r -> r.unresolved() > 0)).isEmpty();
    }

    @Test
    void theNextEngineInsertAfterTheLoadSucceeds() throws Exception {
        String tenant = "t-imp-rt-c";
        String token = env.newTenant(tenant);
        assertThat(env.restore(token, file).get("status").asText()).isEqualTo("committed");
        Set<Long> restored = new HashSet<>(labels("memory", "title", tenant).keySet());
        restored.addAll(labels("plans", "query", tenant).keySet());

        // the insert the engine does for a new memory and a new plan: no id is given, the sequence supplies it
        env.su(ctx -> {
            ctx.insertInto(DSL.table(DSL.name("nexus", "memory")),
                    col("tenant_id"), col("project"), col("title"), col("content"), col("timestamp"))
                .values(tenant, "p", "after the restore", "c", OffsetDateTime.now()).execute();
            ctx.insertInto(DSL.table(DSL.name("nexus", "plans")),
                    col("tenant_id"), col("query"), col("plan_json"), col("created_at"), col("verb"))
                .values(DSL.val(tenant), DSL.val("after the restore"), DSL.val(JSONB.valueOf("{}")), DSL.val(OffsetDateTime.now()),
                    DSL.val("research")).execute();
            ctx.insertInto(DSL.table(DSL.name("nexus", "topics")),
                    col("tenant_id"), col("label"), col("collection"), col("created_at"))
                .values(DSL.val(tenant), DSL.val("after the restore"), DSL.val(TenantExportBatteryTest.Seed.collection(SRC, 768)),
                    DSL.val(OffsetDateTime.now()))
                .execute();
            return null;
        });
        Set<Long> after = new HashSet<>(labels("memory", "title", tenant).keySet());
        after.addAll(labels("plans", "query", tenant).keySet());
        assertThat(after).hasSize(restored.size() + 2).containsAll(restored);
    }
}
