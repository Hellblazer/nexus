// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.db.TenantExport.Column;
import dev.nexus.service.db.TenantExport.ExportClass;
import dev.nexus.service.db.TenantExport.ExportTable;
import dev.nexus.service.db.TenantExport.Reference;
import dev.nexus.service.db.TenantExport.Retired;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.regex.Pattern;

/**
 * What an import does with each table of a backup file, decided from the file's header and the target's live schema
 * alone (RDR-248 "Import session", Open; bead nexus-csayy.6). No database, no I/O: a unit-testable rule set.
 *
 * <p>The rule for a table of the file (N2, the compatibility rule):
 * <ul>
 * <li>a table the registry does not export (unknown, or class {@code excluded}) is REFUSED, unless the registry's
 *     retired list names it, in which case its data is skipped and reported;</li>
 * <li>a file column that is in the target with the same type loads; one the target lacks and the retired list names
 *     is skipped and reported; any other column, or one with another type, is a MISMATCH;</li>
 * <li>a target column the file lacks takes its default; one that is NOT NULL with no default (and is not
 *     {@code tenant_id}, which the load always supplies) is a MISMATCH, and so is a key or reference column the file
 *     lacks, because the load cannot rebuild a row's identity without it;</li>
 * <li>a MISMATCH on a {@code data} table REFUSES the open, naming the table and column; on a {@code derived} table
 *     it SKIPS the table (rebuildable data never refuses a restore), together with every table that refers to it.</li>
 * </ul>
 * A target table that the file does not carry is simply not loaded (an older file from before the table existed).
 */
public final class TenantImportPlan {

    private TenantImportPlan() {
    }

    /** A column of the live target table: its name, {@code format_type} text, and whether a row may omit it. */
    public record TargetColumn(String name, String type, boolean notNull, boolean hasDefault) {
        boolean mayBeOmitted() {
            return !notNull || hasDefault;
        }
    }

    /** A table as the file's header describes it. */
    public record FileTable(String name, List<Column> columns) {
        public FileTable {
            columns = List.copyOf(columns);
        }
    }

    /**
     * The decision for one file table. {@code loadColumns} are the file columns the load reads (present in the
     * target, same type), in file order; {@code retiredColumns} are staged but never loaded. {@code skipReason} is
     * null for a table that loads.
     */
    public record TablePlan(ExportTable entry, List<Column> fileColumns, List<String> loadColumns,
                            List<String> retiredColumns, List<String> defaultedColumns, String skipReason,
                            boolean tenantScoped) {
        public boolean loads() {
            return skipReason == null;
        }

        public String name() {
            return entry.name();
        }

        TablePlan skipped(String reason) {
            return new TablePlan(entry, fileColumns, loadColumns, retiredColumns, defaultedColumns, reason,
                tenantScoped);
        }
    }

    /** The outcome: the refusals (empty when the file may open) and a plan per file table, in registry order. */
    public record Plan(List<String> refusals, Map<String, TablePlan> tables, List<String> notes) {
        public boolean refused() {
            return !refusals.isEmpty();
        }
    }

    /**
     * A retired column's type is the only type text taken from the file (a known column's is the target's own). It
     * becomes the type of a stage column, so it must be a plain type name: words, an optional schema, an optional
     * length or precision in digits, and array brackets. Anything else, a quote or a semicolon included, is refused.
     */
    private static final Pattern PLAIN_TYPE = Pattern.compile(
        "[a-z_][a-z0-9_]*(\\.[a-z_][a-z0-9_]*)?( [a-z]+)*(\\([0-9]+(, ?[0-9]+)?\\))?( [a-z]+)*(\\[\\])*");

    public static boolean plainType(String type) {
        return type != null && type.length() <= 80 && PLAIN_TYPE.matcher(type).matches();
    }

    /**
     * @param file          the file's tables (header order is ignored: the plan is in registry order)
     * @param targetColumns the live columns of every registry table the engine can load, by table name; a table
     *                      absent from the map is one the target no longer has
     * @param retired       the registry's retired list ({@link TenantExport#RETIRED})
     */
    public static Plan analyze(List<FileTable> file, Map<String, List<TargetColumn>> targetColumns,
                               List<Retired> retired) {
        List<String> refusals = new ArrayList<>();
        List<String> notes = new ArrayList<>();
        Set<String> seen = new HashSet<>();
        Map<String, TablePlan> byName = new LinkedHashMap<>();
        Map<String, FileTable> fileByName = new LinkedHashMap<>();
        for (FileTable t : file) {
            if (!seen.add(t.name())) {
                refusals.add("the file lists table " + t.name() + " twice");
                continue;
            }
            fileByName.put(t.name(), t);
        }
        // tables the registry does not export
        for (FileTable t : file) {
            Optional<ExportTable> entry = TenantExport.exportable(t.name());
            if (entry.isEmpty() && !retiredTable(retired, t.name())) {
                refusals.add("table " + t.name() + " is not a table this engine restores, and no changeset retired it");
            }
        }
        // registry order, tables the file carries
        List<ExportTable> inOrder = new ArrayList<>();
        for (ExportTable e : TenantExport.TABLES) {
            if (e.exported() && fileByName.containsKey(e.name())) inOrder.add(e);
        }
        Map<String, String> skipped = new LinkedHashMap<>();
        for (ExportTable e : inOrder) {
            FileTable f = fileByName.get(e.name());
            List<TargetColumn> live = targetColumns.get(e.name());
            if (live == null) {
                if (retiredTable(retired, e.name())) {
                    skipped.put(e.name(), "retired table");
                } else {
                    mismatch(e, "the target has no table " + e.name(), refusals, skipped);
                }
                byName.put(e.name(), new TablePlan(e, f.columns(), List.of(), List.of(), List.of(),
                    skipped.get(e.name()), false));
                continue;
            }
            Map<String, TargetColumn> liveByName = new LinkedHashMap<>();
            live.forEach(c -> liveByName.put(c.name(), c));
            List<String> load = new ArrayList<>();
            List<String> retiredCols = new ArrayList<>();
            List<String> problems = new ArrayList<>();
            Set<String> fileNames = new LinkedHashSet<>();
            for (Column c : f.columns()) {
                fileNames.add(c.name());
                TargetColumn t = liveByName.get(c.name());
                if (t == null) {
                    if (retiredColumn(retired, e.name(), c.name())) {
                        if (!plainType(c.type())) {
                            problems.add("retired column " + e.name() + "." + c.name() + " has a type this engine will"
                                + " not stage: " + c.type());
                        } else {
                            retiredCols.add(c.name());
                        }
                    } else {
                        problems.add("column " + e.name() + "." + c.name() + " is not in the target and no changeset"
                            + " retired it");
                    }
                } else if (!t.type().equals(c.type())) {
                    problems.add("column " + e.name() + "." + c.name() + " is " + c.type() + " in the file and "
                        + t.type() + " in the target");
                } else {
                    load.add(c.name());
                }
            }
            List<String> defaulted = new ArrayList<>();
            Set<String> mustHave = identityColumns(e);
            for (TargetColumn t : live) {
                if (fileNames.contains(t.name()) || "tenant_id".equals(t.name())) continue;
                if (mustHave.contains(t.name())) {
                    problems.add("column " + e.name() + "." + t.name() + " is a key or reference column the file lacks");
                } else if (!t.mayBeOmitted()) {
                    problems.add("column " + e.name() + "." + t.name() + " is NOT NULL with no default in the target and"
                        + " the file lacks it");
                } else {
                    defaulted.add(t.name());
                }
            }
            if (!problems.isEmpty()) {
                mismatch(e, String.join("; ", problems), refusals, skipped);
            }
            byName.put(e.name(), new TablePlan(e, f.columns(), load, retiredCols, defaulted, skipped.get(e.name()),
                liveByName.containsKey("tenant_id")));
        }
        // a skipped table skips every table that refers to it (skipping nx_answer_runs skips nx_answer_steps)
        boolean grew = true;
        while (grew) {
            grew = false;
            for (ExportTable e : inOrder) {
                if (skipped.containsKey(e.name())) continue;
                for (Reference r : e.references()) {
                    if (skipped.containsKey(r.parentName()) && byName.containsKey(r.parentName())) {
                        if (e.exportClass() == ExportClass.DATA) {
                            refusals.add("table " + e.name() + " refers to " + r.parentName() + ", which is skipped: "
                                + skipped.get(r.parentName()));
                        } else {
                            skipped.put(e.name(), "refers to skipped table " + r.parentName());
                            byName.put(e.name(), byName.get(e.name()).skipped(skipped.get(e.name())));
                            grew = true;
                        }
                        break;
                    }
                }
            }
        }
        // a retired table of the file is reported, never staged
        for (FileTable t : file) {
            if (TenantExport.exportable(t.name()).isEmpty() && retiredTable(retired, t.name())) {
                notes.add("table " + t.name() + " is retired: its rows are skipped");
            }
        }
        return new Plan(List.copyOf(refusals), byName, List.copyOf(notes));
    }

    /** Columns whose absence from the file means the load cannot rebuild a row's identity or its references. */
    private static Set<String> identityColumns(ExportTable e) {
        Set<String> names = new LinkedHashSet<>();
        for (Column c : e.primaryKey()) names.add(c.name());
        if (e.reassignedId() != null) names.add(e.reassignedId());
        for (Reference r : e.references()) names.addAll(r.columns());
        names.remove("tenant_id");
        return names;
    }

    private static void mismatch(ExportTable e, String why, List<String> refusals, Map<String, String> skipped) {
        if (e.exportClass() == ExportClass.DATA) {
            refusals.add(why);
        } else {
            skipped.put(e.name(), why);
        }
    }

    private static boolean retiredTable(List<Retired> retired, String table) {
        return retired.stream().anyMatch(r -> r.column() == null && r.table().equals(table));
    }

    private static boolean retiredColumn(List<Retired> retired, String table, String column) {
        return retired.stream().anyMatch(r -> r.table().equals(table)
            && (r.column() == null || r.column().equals(column)));
    }
}
