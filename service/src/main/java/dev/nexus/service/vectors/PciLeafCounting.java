// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.datamove.Gate3;
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
     * be added to the count and a leaf would build an index its own rows do not warrant. The name path ({@link Gate3#never()}).
     */
    /**
     * {@link #count(DSLContext, int, String, String, String, int)} that reads the collection's rows by {@code collection_id} once
     * {@code gate3} holds (RDR-229 P3.5c, nexus-p2m1x.25): the label resolves to the registry's id in the same statement, so a collection
     * renamed in place is counted under its new label (its chunk rows keep the old one) and the old label counts nothing. A NULL id is no
     * collection: an empty or unregistered label counts zero and never falls back to the name. {@code gate3} is read on each call. The
     * statement keeps the probe's shape ({@link PgVectorRepository#probeSelectedRowsQuery}): the model and the tenant on the leaf, the
     * collection key, {@code LIMIT cap + 1} under a count.
     */
    static int count(DSLContext tx, int dim, String collection, String model, String tenant, int cap, Gate3 gate3) {
        if (!gate3.holds()) {
            Integer n = PgVectorRepository.probeSelectedRowsQuery(tx, dim, CollectionSelector.byName(java.util.List.of(collection)), model, tenant, cap)
                .fetchOne(0, Integer.class);
            return n == null ? 0 : n;
        }
        DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);
        Integer n = tx.selectCount()
            .from(tx.selectOne()
                .from(ch.table())
                .where(ch.embeddingModel().eq(model))
                .and(ch.tenantId().eq(tenant))
                .and(CollectionSelector.collectionIs(gate3, tenant, ch.collection(), ch.collectionId(), collection))
                .limit(cap + 1)
                .asTable("probe"))
            .fetchOne(0, Integer.class);
        return n == null ? 0 : n;
    }
}
