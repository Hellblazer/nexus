// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Record;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * The soft references of a restore (RDR-248 "S1, soft references"; bead nexus-csayy.6): a reference to a reassigned id
 * that has no foreign key. A value with a parent is remapped to the parent's new id; NULL, 0 and negative values are
 * sentinels and pass through; a positive value whose parent was gone before the export is negated, so it keeps naming
 * nothing, and the array keeps its length; a centroid whose topic is gone is dropped and counted. The reaped sources,
 * the deleted plan, the plan_id of 0 and the lost topic are built by hand here, because the export battery's seed has
 * none of them, and a second restore of a restored tenant must leave the negated values negative.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TenantImportSoftReferencesTest {

    private static final String SRC = "t-imp-soft-src";
    private static final long REAPED_A = 900_000_001L;
    private static final long REAPED_B = 900_000_002L;
    private static final long DELETED_PLAN = 900_000_003L;
    private static final long LOST_TOPIC = 900_000_004L;
    private static final String COLLECTION = "knowledge__softsrc768__bge-base-en-v15-768__v1";

    private ImportTestEnv env;
    private ImportTestEnv.BackupFile file;

    private static Field<Object> col(String name) {
        return DSL.field(DSL.name(name));
    }

    private static void insert(DSLContext ctx, String table, List<String> columns, Object... values) {
        ctx.insertInto(DSL.table(DSL.name("nexus", table)), columns.stream().map(TenantImportSoftReferencesTest::col).toList())
            .values(java.util.Arrays.stream(values).<Field<?>>map(v -> v instanceof Field<?> f ? f : DSL.val(v)).toList())
            .execute();
    }

    private static long id(DSLContext ctx, String table, String labelColumn, String label) {
        return ctx.select(DSL.field(DSL.name("id"), Long.class)).from(DSL.table(DSL.name("nexus", table)))
            .where(DSL.field(DSL.name("tenant_id"), String.class).eq(SRC), DSL.field(DSL.name(labelColumn), String.class).eq(label))
            .fetchOne(0, Long.class);
    }

    @BeforeAll
    void startAll() throws Exception {
        env = ImportTestEnv.start();
        String srcToken = env.newTenant(SRC);
        env.su(ctx -> {
            OffsetDateTime now = OffsetDateTime.now();
            PgContainerHelper.insertCollection(ctx, SRC, COLLECTION, "bge-base-en-v15-768");
            for (String t : List.of("a1", "a2", "a3")) {
                insert(ctx, "memory", List.of("tenant_id", "project", "title", "content", "timestamp"), SRC, "p", t, "content " + t, now);
            }
            long a1 = id(ctx, "memory", "title", "a1"), a2 = id(ctx, "memory", "title", "a2"), a3 = id(ctx, "memory", "title", "a3");
            List<String> summary = List.of("tenant_id", "project", "content", "source_ids", "model");
            insert(ctx, "memory_summaries", summary, SRC, "p", "partly reaped", new Long[] {a1, REAPED_A}, "m");
            insert(ctx, "memory_summaries", summary, SRC, "p", "all reaped", new Long[] {REAPED_B}, "m");
            insert(ctx, "memory_summaries", summary, SRC, "p", "after an earlier restore", new Long[] {-7L, a2}, "m");
            insert(ctx, "memory_summaries", summary, SRC, "p", "with a zero", new Long[] {0L, a3}, "m");
            for (String q : List.of("p1", "p2")) {
                insert(ctx, "plans", List.of("tenant_id", "query", "plan_json", "created_at", "verb"), SRC, q, JSONB.valueOf("{}"), now, "research");
            }
            long p1 = id(ctx, "plans", "query", "p1");
            List<String> run = List.of("tenant_id", "question", "created_at", "plan_id", "run_summary");
            Field<Long> nullPlan = DSL.castNull(org.jooq.impl.SQLDataType.BIGINT);
            Field<JSONB> nullSummary = DSL.castNull(org.jooq.impl.SQLDataType.JSONB);
            insert(ctx, "nx_answer_runs", run, SRC, "r-live", now, p1, JSONB.valueOf("{\"run\": {\"plan_id\": " + p1 + ", \"k\": \"v\"}}"));
            insert(ctx, "nx_answer_runs", run, SRC, "r-dead", now, DELETED_PLAN, JSONB.valueOf("{\"run\": {\"plan_id\": " + DELETED_PLAN + "}}"));
            insert(ctx, "nx_answer_runs", run, SRC, "r-zero", now, 0L, JSONB.valueOf("{\"run\": {\"plan_id\": 0}}"));
            insert(ctx, "nx_answer_runs", run, SRC, "r-null", now, nullPlan, nullSummary);
            insert(ctx, "nx_answer_runs", run, SRC, "r-negative", now, -9L, JSONB.valueOf("{\"run\": {\"plan_id\": -9}}"));
            insert(ctx, "nx_answer_runs", run, SRC, "r-other", now, nullPlan, JSONB.valueOf("{\"other\": 1}"));
            for (String t : List.of("t1", "t2")) {
                insert(ctx, "topics", List.of("tenant_id", "label", "collection", "created_at"), SRC, t, COLLECTION, now);
            }
            long t1 = id(ctx, "topics", "label", "t1");
            List<Field<?>> centroidColumns = List.of(col("tenant_id"), col("collection"), col("topic_id"), col("label"),
                col("embedding_model"), VectorSeeds.centroidEmbedding(768));
            for (long topic : new long[] {t1, LOST_TOPIC}) {
                ctx.insertInto(DSL.table(DSL.name("nexus", "taxonomy_centroids")), centroidColumns)
                    .values(DSL.val(SRC), DSL.val(COLLECTION), DSL.val(topic), DSL.val("centroid " + topic),
                        DSL.val("bge-base-en-v15-768"), DSL.val(TenantExportBatteryTest.Seed.vector(768, 3), VectorSeeds.centroidEmbedding(768)))
                    .execute();
            }
            return null;
        });
        file = env.export(srcToken);
    }

    @AfterAll
    void stopAll() {
        if (env != null) env.close();
    }

    // ── reads ───────────────────────────────────────────────────────────────────────────────────

    private Map<String, Long> ids(String tenant, String table, String labelColumn) {
        return env.su(ctx -> {
            Map<String, Long> m = new LinkedHashMap<>();
            for (Record r : ctx.select(DSL.field(DSL.name(labelColumn), String.class), DSL.field(DSL.name("id"), Long.class))
                    .from(DSL.table(DSL.name("nexus", table))).where(DSL.field(DSL.name("tenant_id"), String.class).eq(tenant)).fetch()) {
                m.put(r.get(0, String.class), r.get(1, Long.class));
            }
            return m;
        });
    }

    private Map<String, List<Long>> sources(String tenant) {
        return env.su(ctx -> {
            Map<String, List<Long>> m = new LinkedHashMap<>();
            for (Record r : ctx.select(DSL.field(DSL.name("content"), String.class), DSL.field(DSL.name("source_ids"), Long[].class))
                    .from(DSL.table(DSL.name("nexus", "memory_summaries"))).where(DSL.field(DSL.name("tenant_id"), String.class).eq(tenant)).fetch()) {
                m.put(r.get(0, String.class), List.of(r.get(1, Long[].class)));
            }
            return m;
        });
    }

    /** question to {plan_id, run_summary.run.plan_id}, each as text ("null" for NULL or absent). */
    private Map<String, List<String>> runs(String tenant) {
        return env.su(ctx -> {
            Map<String, List<String>> m = new LinkedHashMap<>();
            for (Record r : ctx.select(DSL.field(DSL.name("question"), String.class), DSL.field(DSL.name("plan_id"), Long.class),
                        DSL.field(DSL.name("run_summary"), JSONB.class))
                    .from(DSL.table(DSL.name("nexus", "nx_answer_runs"))).where(DSL.field(DSL.name("tenant_id"), String.class).eq(tenant)).fetch()) {
                String summary = r.get(2, JSONB.class) == null ? "null"
                    : r.get(2, JSONB.class).data().replaceAll("(?s).*\"plan_id\":\\s*(-?\\d+).*", "$1");
                m.put(r.get(0, String.class), List.of(String.valueOf(r.get(1, Long.class)), summary.startsWith("{") ? "no plan_id" : summary));
            }
            return m;
        });
    }

    private List<Long> centroidTopics(String tenant) {
        return env.su(ctx -> ctx.select(DSL.field(DSL.name("topic_id"), Long.class)).from(DSL.table(DSL.name("nexus", "taxonomy_centroids")))
            .where(DSL.field(DSL.name("tenant_id"), String.class).eq(tenant)).fetch(0, Long.class));
    }

    // ── the tests ───────────────────────────────────────────────────────────────────────────────

    @Test
    void remappedNegatedAndSentinelValuesEndUpWhereTheRuleSaysAndEachCountIsReported() throws Exception {
        String tenant = "t-imp-soft-a";
        JsonNode end = env.restore(env.newTenant(tenant), file);

        assertThat(end.get("status").asText()).isEqualTo("committed");
        Map<String, Long> memory = ids(tenant, "memory", "title");
        assertThat(memory.values()).as("the ids are new ones").doesNotContainAnyElementsOf(ids(SRC, "memory", "title").values());
        Map<String, List<Long>> s = sources(tenant);
        assertThat(s.get("partly reaped")).containsExactly(memory.get("a1"), -REAPED_A);
        assertThat(s.get("all reaped")).as("negated, so the array keeps its length and its CHECK").containsExactly(-REAPED_B);
        assertThat(s.get("after an earlier restore")).as("a negative value passes through").containsExactly(-7L, memory.get("a2"));
        assertThat(s.get("with a zero")).as("a zero passes through").containsExactly(0L, memory.get("a3"));

        Map<String, Long> plans = ids(tenant, "plans", "query");
        Map<String, List<String>> r = runs(tenant);
        assertThat(r.get("r-live")).containsExactly(String.valueOf(plans.get("p1")), String.valueOf(plans.get("p1")));
        assertThat(r.get("r-dead")).as("the deleted plan is negated, in the column and in the JSON, still a plan hit")
            .containsExactly(String.valueOf(-DELETED_PLAN), String.valueOf(-DELETED_PLAN));
        assertThat(r.get("r-zero")).as("0 is a plan miss and stays one").containsExactly("0", "0");
        assertThat(r.get("r-null")).containsExactly("null", "null");
        assertThat(r.get("r-negative")).containsExactly("-9", "-9");
        assertThat(r.get("r-other")).as("a summary with no plan_id is untouched").containsExactly("null", "no plan_id");

        Map<String, Long> topics = ids(tenant, "topics", "label");
        assertThat(centroidTopics(tenant)).as("the centroid whose topic is gone is dropped").containsExactly(topics.get("t1"));

        JsonNode report = end.get("report");
        Map<String, JsonNode> soft = new LinkedHashMap<>();
        report.get("soft_references").forEach(n -> soft.put(n.get("column").asText(), n));
        assertCounts(soft.get("memory_summaries.source_ids"), 3, 2, 2);
        assertCounts(soft.get("nx_answer_runs.plan_id"), 1, 2, 1);
        assertCounts(soft.get("nx_answer_runs.run_summary:run.plan_id"), 1, 2, 1);
        assertCounts(soft.get("taxonomy_centroids.topic_id"), 1, 0, 0);
        for (JsonNode t : report.get("tables")) {
            if (t.get("table").asText().equals("taxonomy_centroids")) {
                assertThat(t.get("staged").asLong()).isEqualTo(2);
                assertThat(t.get("inserted").asLong()).isEqualTo(1);
                assertThat(t.get("dropped").asLong()).isEqualTo(1);
            }
        }
        assertThat(report.get("rows_dropped").asLong()).isEqualTo(1);
    }

    private static void assertCounts(JsonNode soft, long remapped, long passed, long negated) {
        assertThat(soft).isNotNull();
        assertThat(soft.get("remapped").asLong()).as("remapped " + soft).isEqualTo(remapped);
        assertThat(soft.get("passed_through").asLong()).as("passed through " + soft).isEqualTo(passed);
        assertThat(soft.get("negated").asLong()).as("negated " + soft).isEqualTo(negated);
    }

    @Test
    void aSecondBackupAndRestoreLeavesTheNegatedValuesNegativeAndRemapsTheRest() throws Exception {
        String second = "t-imp-soft-b";
        String secondToken = env.newTenant(second);
        assertThat(env.restore(secondToken, file).get("status").asText()).isEqualTo("committed");
        ImportTestEnv.BackupFile again = env.export(secondToken);

        String third = "t-imp-soft-c";
        JsonNode end = env.restore(env.newTenant(third), again);

        assertThat(end.get("status").asText()).isEqualTo("committed");
        Map<String, Long> memory = ids(third, "memory", "title");
        Map<String, List<Long>> s = sources(third);
        assertThat(s.get("partly reaped")).as("the reaped source is still negative, the live one is the third tenant's own")
            .containsExactly(memory.get("a1"), -REAPED_A);
        assertThat(s.get("all reaped")).containsExactly(-REAPED_B);
        assertThat(runs(third).get("r-dead")).containsExactly(String.valueOf(-DELETED_PLAN), String.valueOf(-DELETED_PLAN));
        assertThat(runs(third).get("r-live")).containsExactly(String.valueOf(ids(third, "plans", "query").get("p1")),
            String.valueOf(ids(third, "plans", "query").get("p1")));
        // the tenants no longer share an id the other made
        assertThat(memory.values()).doesNotContainAnyElementsOf(ids(second, "memory", "title").values());
        JsonNode soft = end.get("report").get("soft_references");
        for (JsonNode n : soft) {
            if (n.get("column").asText().equals("memory_summaries.source_ids")) assertCounts(n, 3, 4, 0);   // the negatives are sentinels now
        }
    }

    @Test
    void aTopicWhoseParentHasALargerIdLoadsBecauseTheSelfReferenceIsOneStatement() throws Exception {
        // the child was created first, then pointed at a parent created after it: id order is not parent order
        String src = "t-imp-soft-topics-src";
        String srcToken = env.newTenant(src);
        env.su(ctx -> {
            OffsetDateTime now = OffsetDateTime.now();
            PgContainerHelper.insertCollection(ctx, src, "knowledge__topicsrc768__bge-base-en-v15-768__v1", "bge-base-en-v15-768");
            for (String t : List.of("child", "root")) {
                insert(ctx, "topics", List.of("tenant_id", "label", "collection", "created_at"), src,
                    t, "knowledge__topicsrc768__bge-base-en-v15-768__v1", now);
            }
            long root = ctx.select(DSL.field(DSL.name("id"), Long.class)).from(DSL.table(DSL.name("nexus", "topics")))
                .where(DSL.field(DSL.name("tenant_id"), String.class).eq(src), DSL.field(DSL.name("label"), String.class).eq("root"))
                .fetchOne(0, Long.class);
            ctx.update(DSL.table(DSL.name("nexus", "topics"))).set(DSL.field(DSL.name("parent_id")), root)
                .where(DSL.field(DSL.name("tenant_id"), String.class).eq(src), DSL.field(DSL.name("label"), String.class).eq("child"))
                .execute();
            return null;
        });
        ImportTestEnv.BackupFile f = env.export(srcToken);

        String dst = "t-imp-soft-topics-dst";
        assertThat(env.restore(env.newTenant(dst), f, 1).get("status").asText()).isEqualTo("committed");

        Map<String, Long> topics = ids(dst, "topics", "label");
        long childParent = env.su(ctx -> ctx.select(DSL.field(DSL.name("parent_id"), Long.class)).from(DSL.table(DSL.name("nexus", "topics")))
            .where(DSL.field(DSL.name("tenant_id"), String.class).eq(dst), DSL.field(DSL.name("label"), String.class).eq("child"))
            .fetchOne(0, Long.class));
        assertThat(childParent).isEqualTo(topics.get("root"));
    }
}
