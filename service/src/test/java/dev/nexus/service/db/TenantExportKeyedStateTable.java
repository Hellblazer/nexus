// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.List;
import java.util.Map;

/**
 * The tenant export's rows of {@link TenantKeyedStateGateTest}'s checked tables (RDR-248, bead nexus-csayy.2), in
 * their own file because the gate test is at its size ceiling. One tenant export session is held per tenant, so a
 * tenant reset closes it; everything else the export builds is one request's header or page.
 */
final class TenantExportKeyedStateTable {

    private TenantExportKeyedStateTable() {
    }

    /** The evictor name, as {@code NexusService.tenantKeyedState()} registers it. */
    static final String EXPORT_SESSIONS = "TenantExportSessions";

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
    }
}
