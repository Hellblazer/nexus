// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.datamove.CompletionCheck;
import dev.nexus.service.vectors.PciCatalog.Leaf;
import dev.nexus.service.vectors.PciReconcilePlanner.RegistryRow;
import org.jooq.DSLContext;

import java.util.Collection;
import java.util.HashSet;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.function.BooleanSupplier;
import java.util.function.Supplier;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

/**
 * RDR-229 P3.4 (nexus-p2m1x.22): what {@link PciReconciler} keeps and reads for the id-predicate indexes, apart from the
 * reconciler (which is near its size cap): the gate-2 read, the null-id read, and the coverage gate 3 reads.
 *
 * <p><b>Gate 2</b> is read once at the start of a pass ({@link #gate2Holds}). Until it holds no id build is planned, so a
 * chunk-family {@code on_complete} (which ends builds in flight, {@code VerifyingFinisher.java:40-41}) cannot end one. The
 * default is false: an engine with no completion check builds no id index.
 *
 * <p><b>Null ids.</b> A collection is built by id only once none of its rows on the leaf, for the leaf's tenant and model, has a
 * null {@code collection_id} (stamp-before-build, RDR-247 Scope boundary). {@link #nullIdCollections} reads that for the
 * collections that could be built, in the leaf's counting transaction, with the tenant predicate: the admin role reads every
 * tenant's chunks, so without it another tenant's unstamped row of the same name would hold this tenant's build back.
 *
 * <p><b>Gate 3</b> ({@link #coverage}): P3.4 is complete when every collection the planner would index has a VALID id-predicate
 * index (residual S-2). The planned set is what the last complete pass's plan said ({@link PciReconcilePlanner.Plan#plannedIdKeys});
 * the valid and invalid sets are read fresh from a catalog snapshot, so an index a pass just built counts without waiting a pass.
 * A pass that skipped a leaf, or did not run to its end, leaves no planned set (unknown), and gate 3 then does not hold. An engine
 * that holds no builder lock, or has {@code NX_SEARCH_PCI} off, plans nothing and so reports no coverage.
 */
final class PciIdBuilds {

    private volatile BooleanSupplier gate2 = () -> false;
    /** The planned id keys of the last pass that saw every leaf; null before one and after a pass that did not. */
    private volatile Set<String> planned;

    void gate(BooleanSupplier gate2) {
        this.gate2 = gate2 == null ? () -> false : gate2;
    }

    boolean gate2Holds() {
        try {
            return gate2.getAsBoolean();
        } catch (RuntimeException e) {   // unknown is not "holds"
            return false;
        }
    }

    /** Records this pass's planned keys when it saw every leaf, else forgets the last (unknown). */
    void passEnded(boolean sawEveryLeaf, Set<String> plannedKeys) {
        planned = sawEveryLeaf ? Set.copyOf(plannedKeys) : null;
    }

    /** The collection names whose id index is held back by a null-id row: probed only for collections that could build now. */
    static Set<String> nullIdCollections(DSLContext tx, Leaf leaf, Collection<RegistryRow> registry,
                                         Map<String, Integer> counts, int buildMinRows) {
        Set<Long> haveId = new HashSet<>();
        leaf.idIndexes().forEach(i -> haveId.add(i.collectionId()));
        Set<String> out = new HashSet<>();
        for (RegistryRow row : registry) {
            if (row.collectionId() == null || !row.live() || row.superseded() || haveId.contains(row.collectionId())
                || counts.getOrDefault(row.name(), 0) < buildMinRows) {
                continue;
            }
            if (hasNullId(tx, leaf.model(), leaf.tenant(), row.name())) {
                out.add(row.name());
            }
        }
        return out;
    }

    /** Whether a row of {@code collection} on (model, tenant) has a null {@code collection_id}; the tenant predicate is required. */
    static boolean hasNullId(DSLContext tx, String model, String tenant, String collection) {
        return tx.fetchExists(tx.selectOne().from(CHUNKS)
            .where(CHUNKS.EMBEDDING_MODEL.eq(model))
            .and(CHUNKS.TENANT_ID.eq(tenant))
            .and(CHUNKS.COLLECTION.eq(collection))
            .and(CHUNKS.COLLECTION_ID.isNull()));
    }

    /** The names of the registry rows whose id has an id index on the leaf: counted like a name-indexed collection. */
    static Set<String> idIndexedNames(Leaf leaf, Collection<RegistryRow> registry) {
        Set<Long> ids = new HashSet<>();
        leaf.idIndexes().forEach(i -> ids.add(i.collectionId()));
        Set<String> out = new HashSet<>();
        for (RegistryRow row : registry) {
            if (row.collectionId() != null && ids.contains(row.collectionId())) {
                out.add(row.name());
            }
        }
        return out;
    }

    /** The id backoff keys of the registry rows that can still build, so {@code forgetStaleBackoff} keeps their failures. */
    static Set<String> buildableKeys(Collection<RegistryRow> registry) {
        Set<String> out = new HashSet<>();
        for (RegistryRow row : registry) {
            if (row.collectionId() != null && row.live() && !row.superseded()) {
                out.add(PciReconcilePlanner.idBackoffKey(row.collectionId()));
            }
        }
        return out;
    }

    /** What gate 3 reads, or empty when no complete pass has planned or the catalog read failed. */
    Optional<CompletionCheck.IdIndexCoverage> coverage(Supplier<PciCatalog.Snapshot> catalog) {
        Set<String> plannedKeys = planned;
        if (plannedKeys == null) {
            return Optional.empty();
        }
        try {
            PciCatalog.Snapshot snapshot = catalog.get();
            if (snapshot.leaves().isEmpty()) {
                return Optional.empty();   // a failed read, never "there are no indexes"
            }
            return Optional.of(new CompletionCheck.IdIndexCoverage(plannedKeys, snapshot.validIdKeys(), snapshot.invalidIdKeys()));
        } catch (RuntimeException e) {
            return Optional.empty();
        }
    }
}
