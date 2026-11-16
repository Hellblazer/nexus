// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.List;
import java.util.Map;

/**
 * What a tenant import's load did (RDR-248 "Import session", Commit step 4; bead nexus-csayy.6): the counts the
 * verification used and the status route reports. {@code staged} is the rows the upload put in the table's stage,
 * {@code inserted} the rows the load wrote, and the difference is {@code seededSkipped} (rows the engine itself had
 * already written for the tenant, kept) plus {@code dropped} (a soft-reference rule dropped the row).
 */
public record TenantImportReport(List<TableResult> tables, List<SoftResult> softReferences,
                                 Map<String, String> skippedTables, List<String> skippedColumns) {

    public TenantImportReport {
        tables = List.copyOf(tables);
        softReferences = List.copyOf(softReferences);
        skippedTables = Map.copyOf(skippedTables);
        skippedColumns = List.copyOf(skippedColumns);
    }

    /** One table's counts. */
    public record TableResult(String table, long staged, long inserted, long seededSkipped, long dropped) {
    }

    /** The values of one soft reference column: remapped to a new id, passed through as a sentinel, or negated. */
    public record SoftResult(String column, long remapped, long passedThrough, long negated) {
    }

    /** Rows written, summed over every table. */
    public long insertedRows() {
        return tables.stream().mapToLong(TableResult::inserted).sum();
    }

    /** Rows dropped by a soft-reference rule, summed over every table. */
    public long droppedRows() {
        return tables.stream().mapToLong(TableResult::dropped).sum();
    }
}
