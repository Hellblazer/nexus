// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CollectionIds;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ArrayNode;
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

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;

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

    private static final org.slf4j.Logger LOG = org.slf4j.LoggerFactory.getLogger(TenantImportRoundTripTest.class);

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
        // RDR-229 P3.11: collection_id and its kin are referenced columns, not reassigned keys; a restore maps them to the
        // target's registry ids, so they differ from the source's by design and are compared through the registry instead
        // (aBackupWithCollectionIdRestoresEveryChunkAndChildRowOnTheTargetsNewId). Absent before P3.1: the file has no such column.
        out.addAll(CollectionIdSites.idColumnsOf(t.table()));
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
                    col("tenant_id"), col("label"), col("collection"), col("collection_id"), col("created_at"))
                .values(DSL.val(tenant), DSL.val("after the restore"), DSL.val(TenantExportBatteryTest.Seed.collection(SRC, 768)),
                    DSL.val(CollectionIds.require(ctx, tenant, TenantExportBatteryTest.Seed.collection(SRC, 768))),
                    DSL.val(OffsetDateTime.now()))
                .execute();
            return null;
        });
        Set<Long> after = new HashSet<>(labels("memory", "title", tenant).keySet());
        after.addAll(labels("plans", "query", tenant).keySet());
        assertThat(after).hasSize(restored.size() + 2).containsAll(restored);
    }

    // ── RDR-229 P3.11: the backup carries collection_id, and a restore maps it to the target's ──────────────────

    /** The registry's ids of {@code tenant}, by collection name. */
    private Map<String, Long> registryIds(String tenant) {
        return env.su(ctx -> {
            Map<String, Long> m = new LinkedHashMap<>();
            for (Record r : ctx.select(CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).fetch()) {
                m.put(r.get(CATALOG_COLLECTIONS.NAME), r.get(CATALOG_COLLECTIONS.COLLECTION_ID));
            }
            return m;
        });
    }

    /**
     * A source tenant as an engine at P3.1 holds it: the battery seed (which stamps every id, and retires the 1024-wide
     * collection under the 768-wide one so {@code superseded_by_id} holds a value to restore) plus one row in each table the
     * backup does not carry. Returns the tenant's token. No two live registry rows share a (content_type, owner_id,
     * embedding_model, model_version) tuple: the three collections differ by model.
     */
    private String stampedSource(String tenant, String tombstone, String successor) throws Exception {
        String token = batterySource(tenant, tombstone, successor);
        // the battery seed reaches no table the backup excludes; the audit covers them too, so they are seeded here
        env.su(ctx -> {
            CollectionIdSites.seedUnexported(ctx, tenant, successor, TenantExportBatteryTest.Seed.collection(tenant, 384));
            return null;
        });
        return token;
    }

    /** The battery seed alone, for a test that renames a collection (an unfinished re-embed job refuses a rename of its target). */
    private String batterySource(String tenant, String tombstone, String successor) throws Exception {
        String token = env.newTenant(tenant);
        assertThat(tombstone).as("the battery seed retires the 1024-wide collection").isEqualTo(TenantExportBatteryTest.Seed.collection(tenant, 1024));
        assertThat(successor).as("the battery seed retires it under the 768-wide one").isEqualTo(TenantExportBatteryTest.Seed.collection(tenant, 768));
        List<String> failures = new ArrayList<>();
        env.su(ctx -> {
            TenantExportBatteryTest.Seed.rows(ctx, tenant, 1, failures);
            TenantExportBatteryTest.Seed.fillNulls(ctx, tenant, failures);
            return null;
        });
        assertThat(failures).as("the seed").isEmpty();
        // the battery's history rows name a collection that is no registry row: add two that do, carrying the source's ids
        env.su(ctx -> {
            CollectionIdSites.seedHistory(ctx, tenant, successor, TenantExportBatteryTest.Seed.collection(tenant, 384));
            return null;
        });
        return token;
    }

    /**
     * The strict audit of the target's exported history tables, with the per-table counts in the message: each row whose name
     * names a restored registry row carries exactly that row's id, a row naming none carries none, and each table was seeded
     * (a count of zero is a setup failure).
     */
    private void assertHistoryRefilledByName(String tenant) {
        List<CollectionIdSites.HistoryFinding> found = env.su(ctx -> CollectionIdSites.auditHistoryStrict(ctx, tenant));
        LOG.info("history_strict tenant={} found={}", tenant, found);
        assertThat(CollectionIdSites.historyProblems(found, true))
            .as("history rows not on the restored registry row's id for their name; rows examined per table: " + found).isEmpty();
    }

    @Test
    void aBackupWithCollectionIdRestoresEveryChunkAndChildRowOnTheTargetsNewId() throws Exception {
        String src = "t-imp-cid-src", dst = "t-imp-cid-dst";
        String successor = TenantExportBatteryTest.Seed.collection(src, 768), tombstone = TenantExportBatteryTest.Seed.collection(src, 1024);
        String token = stampedSource(src, tombstone, successor);
        // the source is sound, and the check that follows can fail: it holds ids at every exported site, none null
        CollectionIdSites.Finding srcRegistry = env.su(ctx -> CollectionIdSites.registryAudit(ctx, src));
        assertThat(srcRegistry.clean() && srcRegistry.examined() == 3).as("the source registry: " + srcRegistry).isTrue();
        // every site of item 6, excluded tables too: a table the test did not seed is a setup failure, never a pass
        // (the battery's fill puts an arbitrary id in the two history tables, which the restore refills by name, so the SOURCE audit
        // leaves those two out; the restored audit below covers them)
        List<CollectionIdSites.Finding> srcFindings = env.su(ctx -> CollectionIdSites.audit(ctx, src,
            CollectionIdSites.SITES.stream().filter(x -> !x.history() || !x.exported()).toList()));
        assertThat(CollectionIdSites.unexamined(srcFindings)).as("source sites with no row; rows examined per site: " + CollectionIdSites.counts(srcFindings)).isEmpty();
        assertThat(srcFindings.stream().filter(f -> !f.clean()).toList()).as("the stamped source").isEmpty();
        Map<String, Long> srcIds = registryIds(src);
        assertThat(srcIds.values()).doesNotContainNull().hasSize(3);

        // the backup carries the id: the registry's section names the column
        ImportTestEnv.BackupFile backup = env.export(token);
        assertThat(backup.table("catalog_collections").withArray("columns").toString())
            .as("the file's catalog_collections columns").contains("\"collection_id\"").contains("\"superseded_by_id\"");
        long highestBefore = env.su(ctx -> ctx.select(DSL.max(CATALOG_COLLECTIONS.COLLECTION_ID)).from(CATALOG_COLLECTIONS).fetchOne(0, Long.class));

        String dstToken = env.newTenant(dst);
        JsonNode end = env.restore(dstToken, backup);

        assertThat(end.get("status").asText()).as(end.toString()).isEqualTo("committed");
        Map<String, Long> dstIds = registryIds(dst);
        assertThat(dstIds.keySet()).as("the registry's names are kept").isEqualTo(srcIds.keySet());
        assertThat(dstIds.values()).as("every restored registry row has an id").doesNotContainNull();
        // Sam's P3.8 ruling: a restore never writes the source's id; the engine mints the target's. Both tenants share this
        // database and its one sequence here, so a restore that kept an id would repeat the source's, and a restore that
        // minted would draw above every id issued before it.
        assertThat(dstIds.values()).as("the restore writes no id of the source").doesNotContainAnyElementsOf(srcIds.values());
        assertThat(dstIds.values()).as("a restore never restarts the id sequence: every restored id is above every id issued before")
            .allMatch(id -> id > highestBefore);
        // every chunk and child row is on its collection's NEW id, and none is on the source's
        List<CollectionIdSites.Finding> found = env.su(ctx -> CollectionIdSites.audit(ctx, dst, CollectionIdSites.exportedSites()));
        assertThat(found.stream().filter(f -> !f.clean()).toList()).as("child rows whose id is null or is not the target registry's").isEmpty();
        // the restore refilled the exported history tables BY NAME: each row carries exactly the restored registry's id for its name
        assertHistoryRefilledByName(dst);
        assertThat(CollectionIdSites.unexamined(found)).as("restored sites with no row; rows examined per site: " + CollectionIdSites.counts(found)).isEmpty();
        // the tables the backup excludes come back empty: the restore carries none of the source's rows, so none of its ids
        List<String> carried = new ArrayList<>();
        env.su(ctx -> {
            for (String table : CollectionIdSites.SITES.stream().filter(x -> !x.exported()).map(CollectionIdSites.Site::table).distinct().toList()) {
                long n = ctx.fetchCount(DSL.table(DSL.name("nexus", table)), DSL.field(DSL.name("tenant_id"), String.class).eq(dst));
                if (n != 0) carried.add(table + "=" + n);
            }
            return null;
        });
        assertThat(carried).as("excluded tables that hold rows after the restore").isEmpty();
        Set<Long> childIds = new TreeSet<>();
        env.su(ctx -> {
            for (CollectionIdSites.Site s : CollectionIdSites.exportedSites()) {
                String id = CollectionIdSites.idColumn(ctx, s);
                Field<Long> idField = DSL.field(DSL.name(id), Long.class);
                childIds.addAll(ctx.selectDistinct(idField).from(DSL.table(DSL.name("nexus", s.table())))
                    .where(DSL.field(DSL.name("tenant_id"), String.class).eq(dst)).and(idField.isNotNull()).fetch(idField));
            }
            return null;
        });
        assertThat(childIds).as("ids the target's child rows hold").isNotEmpty().doesNotContainAnyElementsOf(srcIds.values())
            .isSubsetOf(dstIds.values());
        // the tombstone's successor id is mapped as well: it names the target's row, not the source's
        CollectionIdSites.Finding dstRegistry = env.su(ctx -> CollectionIdSites.registryAudit(ctx, dst));
        assertThat(dstRegistry.clean() && dstRegistry.examined() == 3).as("the target registry's tombstone fields: " + dstRegistry).isTrue();
        Long successorId = env.su(ctx -> ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(dst)).and(CATALOG_COLLECTIONS.NAME.eq(tombstone))
            .fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID));
        assertThat(successorId).as("the tombstone points at the target's id of its successor").isEqualTo(dstIds.get(successor));
    }

    /**
     * Linda's ruling (RDR-229 P3.11 round 2): restore a set of ids, mint exactly one new collection afterwards, and the new id is
     * greater than every restored id. P3.1's id is GENERATED BY DEFAULT, so an import that wrote explicit ids without moving the
     * sequence would hand a later mint an id a restored row already holds (a 23505 on UNIQUE (tenant_id, collection_id), or a
     * collection that shares an id); an import that takes ids from the sequence cannot.
     */
    @Test
    void aCollectionMintedAfterARestoreHasAnIdAboveEveryRestoredId() throws Exception {
        String src = "t-imp-mint-src", dst = "t-imp-mint-dst";
        String successor = TenantExportBatteryTest.Seed.collection(src, 768), tombstone = TenantExportBatteryTest.Seed.collection(src, 1024);
        ImportTestEnv.BackupFile backup = env.export(stampedSource(src, tombstone, successor));
        assertThat(env.restore(env.newTenant(dst), backup).get("status").asText()).isEqualTo("committed");
        Map<String, Long> restored = registryIds(dst);
        assertThat(restored).as("the restored set of ids").hasSize(3);
        assertThat(restored.values()).as("every restored registry row has an id").doesNotContainNull();

        String fresh = TenantExportBatteryTest.Seed.collection(dst + "-fresh", 768);
        long minted = env.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, dst, fresh);
            return CollectionIds.require(ctx, dst, fresh);
        });

        assertThat(registryIds(dst)).as("exactly one collection was minted").hasSize(restored.size() + 1);
        assertThat(restored.values()).as("the minted id " + minted + " is not one a restored row holds").doesNotContain(minted);
        assertThat(minted).as("the minted id is above every restored id " + restored.values())
            .isGreaterThan(java.util.Collections.max(restored.values()));
    }

    /**
     * After P3.1 a row whose collection has no registry row is refused (422, UnregisteredCollectionException, from the NULLIF
     * CHECK's guard), so a restore must load {@code catalog_collections} before every table that names a collection, whatever order
     * the backup file holds them in. Today the plan is in registry order and ignores the header's order
     * ({@code TenantImportPlan.java:115} and {@code :141-145}), the load walks {@code TenantExport.TABLES}
     * ({@code TenantImportLoad.java:97}), and the open answer lists the tables in that order for the client to upload
     * ({@code TenantImportSessions.java:436-437}; {@code requireOrder}, {@code :557-565}, refuses a table that arrives before an
     * earlier one closed). The file format therefore does not fix the order, and such a file can exist: this one is the export with
     * its tables in the reverse order, {@code catalog_collections} last.
     */
    @Test
    void aBackupWhoseReferencingTablesPrecedeCatalogCollectionsStillRestoresOnTheTargetsNewIds() throws Exception {
        String src = "t-imp-ord-src", dst = "t-imp-ord-dst";
        String successor = TenantExportBatteryTest.Seed.collection(src, 768), tombstone = TenantExportBatteryTest.Seed.collection(src, 1024);
        ImportTestEnv.BackupFile backup = env.export(stampedSource(src, tombstone, successor));
        ImportTestEnv.BackupFile reversed = new ImportTestEnv.BackupFile(backup.header.deepCopy());
        ArrayNode tables = reversed.header.withArray("tables");
        List<JsonNode> held = new ArrayList<>();
        tables.forEach(held::add);
        Collections.reverse(held);
        tables.removeAll();
        held.forEach(tables::add);
        for (String name : reversed.tableNames()) reversed.pages.put(name, new ArrayList<>(backup.pages.get(name)));
        reversed.seal();
        List<String> inFile = reversed.tableNames();
        List<String> exportedSites = CollectionIdSites.exportedSites().stream().map(CollectionIdSites.Site::table).distinct().toList();
        for (String t : exportedSites) {
            assertThat(inFile.indexOf(t)).as(t + " precedes catalog_collections in the file").isNotNegative()
                .isLessThan(inFile.indexOf("catalog_collections"));
        }
        // the property that matters, whatever the file order: the load order has the registry before every table that names a collection
        List<String> loadOrder = TenantExport.TABLES.stream().map(ExportTable::name).toList();
        for (String t : exportedSites) {
            assertThat(loadOrder.indexOf("catalog_collections")).as("catalog_collections loads before " + t)
                .isLessThan(loadOrder.indexOf(t));
        }
        Map<String, Long> srcIds = registryIds(src);

        JsonNode end = env.restore(env.newTenant(dst), reversed);

        assertThat(end.get("status").asText()).as(end.toString()).isEqualTo("committed");
        Map<String, Long> dstIds = registryIds(dst);
        assertThat(dstIds.keySet()).isEqualTo(srcIds.keySet());
        assertThat(dstIds.values()).as("the restore writes no id of the source").doesNotContainNull().doesNotContainAnyElementsOf(srcIds.values());
        List<CollectionIdSites.Finding> found = env.su(ctx -> CollectionIdSites.audit(ctx, dst, CollectionIdSites.exportedSites()));
        assertThat(CollectionIdSites.unexamined(found)).as("restored sites with no row; rows examined per site: " + CollectionIdSites.counts(found)).isEmpty();
        assertThat(found.stream().filter(f -> !f.clean()).toList()).as("rows on a collection's id that is not the target registry's").isEmpty();
        // the restore refilled the exported history tables BY NAME: each row carries exactly the restored registry's id for its name
        assertHistoryRefilledByName(dst);
    }

    /**
     * RDR-229 P3.11's own scope: an export is one consistent snapshot, so a rename in the source while the export is being read does
     * not tear the file. The session holds a {@code REPEATABLE READ READ ONLY} transaction that calls {@code pg_export_snapshot()}
     * and nothing else ({@code TenantExportSessions.java:34-35}, {@code :799-808}), and every header and page read opens its own
     * transaction whose first statement is {@code SET TRANSACTION SNAPSHOT} ({@code TenantExportReads.java:36-37}). So a collection
     * renamed between the header read and the page reads is still under its old name in every table of the file, the registry's
     * rows and every twin id agree, and the restore into an empty tenant refuses no orphan and lands every twin on the restored
     * registry row's id. (A torn file would hold the new registry row's id on a child page and no such row in the registry page.)
     */
    @Test
    void anExportReadAcrossARenameIsOneSnapshotAndRestoresOnTheRestoredIds() throws Exception {
        String src = "t-imp-snap-src", dst = "t-imp-snap-dst";
        String successor = TenantExportBatteryTest.Seed.collection(src, 768), tombstone = TenantExportBatteryTest.Seed.collection(src, 1024);
        String renamed = TenantExportBatteryTest.Seed.collection(src, 384), renamedTo = "knowledge__timpsnapsrcrenamed__minilm-l6-v2-384__v1";
        String token = batterySource(src, tombstone, successor);
        Map<String, Long> before = registryIds(src);
        assertThat(before).as("the source registry before the rename").containsKey(renamed).doesNotContainKey(renamedTo).hasSize(3);
        Integer chunksBefore = env.su(ctx -> ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(src).and(CHUNKS.COLLECTION.eq(renamed))));
        assertThat(chunksBefore).as("chunks under the collection to be renamed").isPositive();

        ImportTestEnv.BackupFile file = env.export(token, () -> {
            var r = env.callJson("POST", "/v1/catalog/collections/rename", token,
                "{\"old_name\":\"" + renamed + "\",\"new_name\":\"" + renamedTo + "\"}");
            assertThat(r.statusCode()).as("the rename in the source, after the session opened: " + r.body()).isEqualTo(200);
        });

        // the rename really happened in the source, so the file's agreement is not vacuous
        Map<String, Long> after = registryIds(src);
        assertThat(after).as("the source registry after the rename").containsKey(renamedTo);
        Integer chunksLeft = env.su(ctx -> ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(src).and(CHUNKS.COLLECTION.eq(renamed))));
        assertThat(chunksLeft).as("the source's chunks left the old name").isZero();

        JsonNode end = env.restore(env.newTenant(dst), file);

        assertThat(end.get("status").asText()).as("no orphan refusal: " + end).isEqualTo("committed");
        Map<String, Long> restored = registryIds(dst);
        assertThat(restored.keySet()).as("the restored names are the pre-rename names").isEqualTo(before.keySet());
        assertThat(restored).as("the renamed name is not in the restore").doesNotContainKey(renamedTo);
        assertThat(restored.values()).as("ids are the target's, none the source's").doesNotContainNull()
            .doesNotContainAnyElementsOf(before.values()).doesNotContainAnyElementsOf(after.values());
        Integer chunksRestored = env.su(ctx -> ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(dst).and(CHUNKS.COLLECTION.eq(renamed))));
        assertThat(chunksRestored).as("the restored chunks sit under the pre-rename name").isEqualTo(chunksBefore);
        List<CollectionIdSites.Finding> found = env.su(ctx -> CollectionIdSites.audit(ctx, dst, CollectionIdSites.exportedSites()));
        assertThat(CollectionIdSites.unexamined(found)).as("restored sites with no row; rows examined per site: " + CollectionIdSites.counts(found)).isEmpty();
        assertThat(found.stream().filter(f -> !f.clean()).toList()).as("twins not on the restored registry row's id").isEmpty();
        // the restore refilled the exported history tables BY NAME: each row carries exactly the restored registry's id for its name
        assertHistoryRefilledByName(dst);
        CollectionIdSites.Finding registry = env.su(ctx -> CollectionIdSites.registryAudit(ctx, dst));
        assertThat(registry.clean() && registry.examined() == 3).as("the restored registry: " + registry).isTrue();
    }
}
