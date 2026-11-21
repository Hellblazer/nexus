// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.db.TenantExport.ExportTable;
import dev.nexus.service.db.TenantExport.Reference;
import dev.nexus.service.db.TenantImportLoad.LoadFailedException;
import dev.nexus.service.db.TenantImportPlan.TablePlan;
import dev.nexus.service.db.TenantImportStaging.Stage;
import org.jooq.DSLContext;
import org.jooq.impl.DSL;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17): refuses a backup in which any id twin ({@code collection_id} and its siblings,
 * {@code superseded_by_id}) is non-null. Until P3.11 (nexus-p2m1x.31) remaps a backup's ids through the restored registry, such
 * a value would be written as the source installation's id: wrong data and no error. A backup written before the twins
 * existed carries none and is filled by name ({@link RestoreIdFill}).
 *
 * <p>The refusal runs on the staged rows after the upload and before the first insert into a real table, so it writes
 * nothing. The registry's own {@code collection_id} is not a twin: the restore never takes it from the file.
 *
 * <p>P3.11 deletes this class, its one call in {@link TenantImportLoad#run} and its test in the commit that adds the remap.
 */
final class RestoreTwinGuard {

    /** The stable code at the head of the failure message. */
    static final String CODE = "restore_twin_ids_unsupported";

    private RestoreTwinGuard() {
    }

    static void check(DSLContext ctx, Map<String, TablePlan> plan, Map<String, Stage> stages) {
        List<String> found = new ArrayList<>();
        for (ExportTable e : TenantExport.TABLES) {
            TablePlan tp = plan.get(e.name());
            Stage st = stages.get(e.name());
            if (tp == null || st == null || !tp.loads()) continue;
            Set<String> inFile = new java.util.HashSet<>();
            tp.fileColumns().forEach(c -> inFile.add(c.name()));
            for (Reference r : e.references()) {
                if (!TenantExport.refersToRemappedColumn(r)) continue;
                for (String column : r.columns()) {
                    if (!inFile.contains(column)) continue;
                    if (ctx.fetchExists(ctx.selectOne().from(TenantImportStaging.stageTable(st))
                            .where(DSL.field(DSL.name(column)).isNotNull()))) {
                        found.add(e.name() + "." + column);
                    }
                }
            }
        }
        if (!found.isEmpty()) {
            throw new LoadFailedException(CODE + ": the backup carries collection ids (" + String.join(", ", found)
                + "), and restoring ids taken from another installation is not supported until RDR-229 P3.11 "
                + "(nexus-p2m1x.31) remaps them; restore a backup taken before RDR-229 P3.1, or wait for P3.11");
        }
    }
}
