// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.db.TenantExport.Column;
import dev.nexus.service.db.TenantExport.ExportClass;
import dev.nexus.service.db.TenantExport.Retired;
import dev.nexus.service.db.TenantImportPlan.FileTable;
import dev.nexus.service.db.TenantImportPlan.Plan;
import dev.nexus.service.db.TenantImportPlan.TargetColumn;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * The compatibility rule of a tenant import (RDR-248 "Import session", Open and "Derived tables and cross-release";
 * bead nexus-csayy.6), without a database: which tables of a file load, which are skipped and why, and which refuse
 * the open naming the table and column. The target here is built from the file itself, then one thing is changed.
 */
class TenantImportPlanTest {

    private static final List<Column> MEMORY = List.of(new Column("id", "bigint"), new Column("tenant_id", "text"),
        new Column("project", "text"), new Column("title", "text"), new Column("note", "text"));
    private static final List<Column> RUNS = List.of(new Column("id", "bigint"), new Column("tenant_id", "text"),
        new Column("question", "text"), new Column("plan_id", "bigint"));
    private static final List<Column> STEPS = List.of(new Column("run_id", "bigint"), new Column("tenant_id", "text"),
        new Column("step_index", "integer"));

    private static List<TargetColumn> target(List<Column> columns) {
        List<TargetColumn> out = new ArrayList<>();
        for (Column c : columns) {
            boolean key = c.name().equals("id") || c.name().equals("tenant_id") || c.name().equals("run_id");
            out.add(new TargetColumn(c.name(), c.type(), key, key && c.name().equals("id")));
        }
        return out;
    }

    private static Map<String, List<TargetColumn>> targets() {
        Map<String, List<TargetColumn>> t = new LinkedHashMap<>();
        t.put("memory", target(MEMORY));
        t.put("nx_answer_runs", target(RUNS));
        t.put("nx_answer_steps", target(STEPS));
        return t;
    }

    private static List<FileTable> file() {
        return List.of(new FileTable("memory", MEMORY), new FileTable("nx_answer_runs", RUNS),
            new FileTable("nx_answer_steps", STEPS));
    }

    private static List<FileTable> with(List<FileTable> file, String table, List<Column> columns) {
        List<FileTable> out = new ArrayList<>();
        for (FileTable t : file) out.add(t.name().equals(table) ? new FileTable(table, columns) : t);
        return out;
    }

    @Test
    void aFileWhoseColumnsAllMatchLoadsEveryTable() {
        Plan plan = TenantImportPlan.analyze(file(), targets(), List.of());
        assertThat(plan.refusals()).isEmpty();
        assertThat(plan.tables().values()).allMatch(TenantImportPlan.TablePlan::loads);
        assertThat(plan.tables().get("memory").loadColumns()).containsExactly("id", "tenant_id", "project", "title", "note");
    }

    @Test
    void aDataColumnTheTargetLacksAndNoChangesetRetiredIsRefusedByName() {
        List<Column> newer = new ArrayList<>(MEMORY);
        newer.add(new Column("from_a_newer_engine", "text"));
        Plan plan = TenantImportPlan.analyze(with(file(), "memory", newer), targets(), List.of());
        assertThat(plan.refusals()).singleElement().asString()
            .contains("memory.from_a_newer_engine").contains("not in the target");
    }

    @Test
    void aDataColumnWithAnotherTypeIsRefusedNamingBothTypes() {
        List<Column> retyped = new ArrayList<>(MEMORY);
        retyped.set(2, new Column("project", "integer"));
        Plan plan = TenantImportPlan.analyze(with(file(), "memory", retyped), targets(), List.of());
        assertThat(plan.refusals()).singleElement().asString().contains("memory.project").contains("integer").contains("text");
    }

    @Test
    void aTargetColumnTheFileLacksTakesItsDefaultWhenItMayBeOmitted() {
        List<Column> older = new ArrayList<>(MEMORY);
        older.removeIf(c -> c.name().equals("note"));   // nullable in the target
        Plan plan = TenantImportPlan.analyze(with(file(), "memory", older), targets(), List.of());
        assertThat(plan.refusals()).isEmpty();
        assertThat(plan.tables().get("memory").defaultedColumns()).containsExactly("note");
        assertThat(plan.tables().get("memory").loadColumns()).doesNotContain("note");
    }

    @Test
    void aNotNullColumnWithNoDefaultTheFileLacksIsRefused() {
        Map<String, List<TargetColumn>> t = targets();
        List<TargetColumn> strict = new ArrayList<>(t.get("memory"));
        strict.set(4, new TargetColumn("note", "text", true, false));
        t.put("memory", strict);
        List<Column> older = new ArrayList<>(MEMORY);
        older.removeIf(c -> c.name().equals("note"));
        Plan plan = TenantImportPlan.analyze(with(file(), "memory", older), t, List.of());
        assertThat(plan.refusals()).singleElement().asString().contains("memory.note").contains("NOT NULL");
    }

    @Test
    void aKeyColumnTheFileLacksIsRefusedEvenWhenTheTargetWouldDefaultIt() {
        List<Column> noId = new ArrayList<>(MEMORY);
        noId.removeIf(c -> c.name().equals("id"));
        Plan plan = TenantImportPlan.analyze(with(file(), "memory", noId), targets(), List.of());
        assertThat(plan.refusals()).singleElement().asString().contains("memory.id").contains("key or reference");
    }

    @Test
    void aRetiredColumnIsSkippedAndReportedNotRefused() {
        List<Column> older = new ArrayList<>(MEMORY);
        older.add(new Column("legacy_flag", "boolean"));
        Plan plan = TenantImportPlan.analyze(with(file(), "memory", older), targets(),
            List.of(new Retired("memory", "legacy_flag", "memory-099")));
        assertThat(plan.refusals()).isEmpty();
        assertThat(plan.tables().get("memory").retiredColumns()).containsExactly("legacy_flag");
        assertThat(plan.tables().get("memory").loadColumns()).doesNotContain("legacy_flag");
    }

    @Test
    void aRetiredColumnWhoseTypeIsNotAPlainTypeNameIsRefusedBecauseItBecomesAStageColumnType() {
        List<Column> hostile = new ArrayList<>(MEMORY);
        hostile.add(new Column("legacy_flag", "boolean); drop table nexus.memory; --"));
        Plan plan = TenantImportPlan.analyze(with(file(), "memory", hostile), targets(),
            List.of(new Retired("memory", "legacy_flag", "memory-099")));
        assertThat(plan.refusals()).singleElement().asString().contains("legacy_flag").contains("will not stage");
    }

    @Test
    void plainTypeNamesAreAcceptedAndNothingElse() {
        for (String ok : List.of("bigint", "text", "boolean", "timestamp with time zone", "character varying(255)",
                "numeric(10,2)", "bigint[]", "double precision", "vector(768)", "public.vector(768)")) {
            assertThat(TenantImportPlan.plainType(ok)).as(ok).isTrue();
        }
        for (String bad : List.of("", "text; drop", "text'", "\"x\"", "text --", "a(b)", "int, int", "x".repeat(81))) {
            assertThat(TenantImportPlan.plainType(bad)).as(bad).isFalse();
        }
    }

    @Test
    void aTableTheRegistryDoesNotExportIsRefusedUnlessRetired() {
        List<FileTable> f = new ArrayList<>(file());
        f.add(new FileTable("gc_audit", List.of(new Column("id", "bigint"))));   // an excluded table
        assertThat(TenantImportPlan.analyze(f, targets(), List.of()).refusals()).singleElement().asString()
            .contains("gc_audit").contains("no changeset retired it");
        Plan retired = TenantImportPlan.analyze(f, targets(), List.of(new Retired("gc_audit", null, "x-1")));
        assertThat(retired.refusals()).isEmpty();
        assertThat(retired.notes()).singleElement().asString().contains("gc_audit");
    }

    @Test
    void aDerivedTableWithAMismatchedColumnIsSkippedWithTheTablesThatReferToIt() {
        List<Column> retyped = new ArrayList<>(RUNS);
        retyped.set(2, new Column("question", "integer"));
        Plan plan = TenantImportPlan.analyze(with(file(), "nx_answer_runs", retyped), targets(), List.of());
        assertThat(plan.refusals()).isEmpty();
        assertThat(plan.tables().get("nx_answer_runs").skipReason()).contains("nx_answer_runs.question");
        assertThat(plan.tables().get("nx_answer_steps").skipReason()).contains("refers to skipped table nx_answer_runs");
        assertThat(plan.tables().get("memory").loads()).isTrue();
        assertThat(TenantExport.entry("nx_answer_runs").orElseThrow().exportClass()).isEqualTo(ExportClass.DERIVED);
    }

    @Test
    void aTableTheTargetNoLongerHasIsRefusedForDataAndSkippedForDerived() {
        Map<String, List<TargetColumn>> t = targets();
        t.remove("memory");
        t.remove("nx_answer_runs");
        Plan plan = TenantImportPlan.analyze(file(), t, List.of());
        assertThat(plan.refusals()).singleElement().asString().contains("no table memory");
        assertThat(plan.tables().get("nx_answer_runs").skipReason()).contains("no table nx_answer_runs");
        assertThat(plan.tables().get("nx_answer_steps").skipReason()).contains("refers to skipped table");
    }

    @Test
    void aTableListedTwiceIsRefused() {
        List<FileTable> twice = new ArrayList<>(file());
        twice.add(file().get(0));
        assertThat(TenantImportPlan.analyze(twice, targets(), List.of()).refusals()).singleElement().asString()
            .contains("memory twice");
    }

    @Test
    void thePlanIsInRegistryOrderWhateverTheFilesOrder() {
        List<FileTable> reversed = new ArrayList<>(file());
        java.util.Collections.reverse(reversed);
        assertThat(TenantImportPlan.analyze(reversed, targets(), List.of()).tables().keySet())
            .containsExactly("memory", "nx_answer_runs", "nx_answer_steps");
    }
}
