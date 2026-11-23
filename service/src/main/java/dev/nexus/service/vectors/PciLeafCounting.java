// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.PgSession;
import dev.nexus.service.vectors.PciCatalog.Leaf;
import org.jooq.DSLContext;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Duration;

/** The reads of a leaf's counting transaction that {@link PciReconciler} (near its size cap) shares with {@link PciIdBuilds}. */
final class PciLeafCounting {

    /** PciReconciler's logger: the counting transaction moved here from it, and its events keep their category (operators and tests read it). */
    private static final Logger log = LoggerFactory.getLogger(PciReconciler.class);

    private PciLeafCounting() { }

    /**
     * The first statements of every counting transaction: the per-statement timeout, a custom plan, the leaf's tenant,
     * and a read-back of it. False (and logged) when the tenant did not take, and the transaction must do nothing more.
     */
    static boolean begin(DSLContext tx, Leaf leaf, Duration countTimeout, PciReconciler.TenantBinder tenantBinder) {
        String tenant = leaf.tenant();
        // setLocal binds the matching network timeout with the statement timeout (nexus-u9zkn); the pass puts the
        // connection's own, longer one back when the transaction ends.
        PgSession.setLocal(tx, "statement_timeout", Long.toString(countTimeout.toMillis()));
        tx.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val("plan_cache_mode"),
            DSL.val("force_custom_plan"), DSL.inline(true))).fetch();
        tenantBinder.bind(tx, tenant);
        String bound = tx.select(DSL.function("current_setting", SQLDataType.VARCHAR, DSL.val("nexus.tenant"),
            DSL.inline(true))).fetchOne(0, String.class);
        if (!tenant.equals(bound)) {
            log.error("event=pci_count_tenant_mismatch leaf={} expected_tenant={} actual_tenant={} "
                + "detail=\"counts under the wrong tenant read other rows or none; the leaf is skipped\"",
                PciReconciler.leafId(leaf), tenant, bound);
            return false;
        }
        return true;
    }

    /**
     * Rows of {@code collection} on the leaf's (model, tenant) key, stopped at {@code cap + 1}. The probe's explicit
     * {@code tenant_id} predicate is REQUIRED for correctness: the admin role reads every tenant's chunks
     * ({@code chunks_gate_probe_owner_read}, vectors-029), so without it a same-named collection of another tenant would
     * be added to the count and a leaf would build an index its own rows do not warrant.
     */
    static int count(DSLContext tx, int dim, String collection, String model, String tenant, int cap) {
        Integer n = PgVectorRepository.probeSelectedRowsQuery(tx, dim, new String[] {collection}, model, tenant, cap)
            .fetchOne(0, Integer.class);
        return n == null ? 0 : n;
    }
}
