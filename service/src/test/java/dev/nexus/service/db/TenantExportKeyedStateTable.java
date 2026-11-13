// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.List;
import java.util.Map;

/**
 * The tenant export's and import's rows of {@link TenantKeyedStateGateTest}'s checked tables (RDR-248, beads
 * nexus-csayy.2 and nexus-csayy.6), in their own file because the gate test is at its size ceiling. One tenant export
 * session is held per tenant, so a tenant reset closes it; the same goes for an import session and the outcome the
 * engine keeps of a finished one. Everything else the export and import build is one request's header, page, plan or
 * report.
 */
final class TenantExportKeyedStateTable {

    private TenantExportKeyedStateTable() {
    }

    /** The evictor name, as {@code NexusService.tenantKeyedState()} registers it. */
    static final String EXPORT_SESSIONS = "TenantExportSessions";

    /** The import sessions' evictor name. */
    static final String IMPORT_SESSIONS = "TenantImportSessions";

    /**
     * Adds the export's entries to {@code keyed} (field to evictor) and {@code notKeyed} (field to reason);
     * {@code result} is the gate's standing reason for a component of one request's result record.
     */
    static void fill(Map<String, String> keyed, Map<String, String> notKeyed, String result) {
        // the held snapshot is the one thing the engine holds for a tenant outside any table; a reset closes it
        keyed.put("NexusService.exportSessions", EXPORT_SESSIONS);
        keyed.put("TenantExportSessions.byOwner", EXPORT_SESSIONS);
        notKeyed.put("TenantExportSessions.opening", "the tenants whose session is being opened right now, one "
            + "open at a time per tenant; an entry lives only for the duration of that POST and is removed in its "
            + "finally, so no tenant's state is kept");
        for (String f : List.of("TenantExportReads.TableHeader.primaryKey", "TenantExportReads.TableHeader.columns",
            "TenantExportReads.Header.tables", "TenantExportReads.Header.embeddingModels",
            "BinaryCopyPage.Result.lastKeyFields")) {
            notKeyed.put(f, result);   // one export header or page, built for that request and dropped with its response
        }
        // the import session (nexus-csayy.6): a held connection and its staged rows, one per tenant; a reset ends it
        keyed.put("NexusService.importSessions", IMPORT_SESSIONS);
        keyed.put("TenantImportSessions.byTenant", IMPORT_SESSIONS);
        keyed.put("TenantImportSessions.outcomes", IMPORT_SESSIONS);   // the 24-hour record of a finished session; a reset drops it
        keyed.put("TenantExportSessions.importSlots", IMPORT_SESSIONS);   // the cap's and the one-per-tenant rule's view of the same sessions
        notKeyed.put("TenantExportSessions.Slot.tenant", "the key of an importSlots entry, which the import evictor frees");
        notKeyed.put("TenantImportSessions.Session.tenant", "the owner of one live import session, immutable; the session "
            + "is the keyed entry (byTenant)");
        notKeyed.put("TenantImportSessions.Outcome.tenant", "the owner of one recorded outcome, immutable; the outcome is "
            + "the keyed entry (outcomes)");
        for (String f : List.of("TenantImportPlan.FileTable.columns", "TenantImportPlan.TablePlan.fileColumns",
            "TenantImportPlan.TablePlan.loadColumns", "TenantImportPlan.TablePlan.retiredColumns",
            "TenantImportPlan.TablePlan.defaultedColumns", "TenantImportPlan.TablePlan.tenantScoped",
            "TenantImportPlan.Plan.refusals", "TenantImportPlan.Plan.tables", "TenantImportPlan.Plan.notes",
            "TenantImportReport.tables", "TenantImportReport.softReferences", "TenantImportReport.skippedTables",
            "TenantImportReport.skippedColumns", "TenantImportSessions.RefusedException.extra",
            "TenantImportSessions.OpenRequest.sourceTenant", "TenantImportSessions.OpenRequest.tables",
            "TenantImportSessions.OpenRequest.embeddingModels", "TenantImportSessions.Session.stages",
            "TenantImportSessions.Session.tables", "TenantImportSessions.Session.ignored",
            "TenantImportSessions.Session.notes", "TenantImportSessions.Opened.skippedTables",
            "TenantImportSessions.Opened.notes", "TenantImportSessions.Opened.loadOrder",
            "TenantImportSessions.StatusView.notes", "TenantImportSessions.Outcome.notes",
            "TenantImportLoad.SoftExpr.joins")) {
            notKeyed.put(f, result);   // one open request, plan, load or answer, or a part of a live session's own record
        }
        notKeyed.put("TenantImportSessions.retired", "the registry's retired list, set at construction and immutable; the same "
            + "for every tenant");
        for (String f : List.of("TenantImportSessions.partHookForTests", "TenantImportSessions.loadHookForTests")) {
            notKeyed.put(f, "a test-only hook, null in production");
        }
        for (String f : List.of("TenantExport.SeededKey.values", "TenantExport.SeededKey.defaultTenantOnly",
            "TenantExport.ExportTable.seededKeys", "TenantExport.Entry.seeded")) {
            notKeyed.put(f, "the tenant export's schema registry (nexus-csayy.6): rows the engine writes for every "
                + "tenant, fixed at class load; not keyed by tenant");
        }
    }
}
