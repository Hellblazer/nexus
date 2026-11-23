// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.vectors.PciCatalog.Leaf;
import dev.nexus.service.vectors.PciReconcilePlanner.RegistryRow;
import org.jooq.DSLContext;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.Collection;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.BooleanSupplier;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

/**
 * RDR-229 P3.4 (nexus-p2m1x.22): what {@link PciReconciler} keeps and reads for the id-predicate indexes, apart from the
 * reconciler (which is near its size cap): the gate-2 and gate-3 reads and the null-id read.
 *
 * <p><b>Gate 2</b> is read once at the start of a pass ({@link #gate2Holds}). Until it holds no id build is planned, so a
 * chunk-family {@code on_complete} (which ends builds in flight, {@code VerifyingFinisher.java:40-41}) cannot end one. The
 * default is false: an engine with no completion check builds no id index. The exception: a gate 3 latch row that says
 * {@code pci_off} (gate 3 latched while per-collection indexing was off, so no id index was required then) makes the id builds go ahead as if
 * gate 2 held ({@link #pciOffLatch}), because the readers are on the id regardless and need those indexes now that PCI is on.
 *
 * <p><b>Null ids.</b> A collection is built by id only once none of its rows on the leaf, for the leaf's tenant and model, has a
 * null {@code collection_id} (stamp-before-build, RDR-247 Scope boundary). {@link #nullIdCollections} reads that for the
 * collections that could be built, in the leaf's counting transaction, with the tenant predicate: the admin role reads every
 * tenant's chunks, so without it another tenant's unstamped row of the same name would hold this tenant's build back.
 *
 * <p><b>The retire</b> ({@link #gate3Holds}, read once at the start of a pass): once gate 3 has LATCHED and the id readers are present
 * ({@link IdReaders#PRESENT}) the planner retires the name family (no name builds, {@code REPLACED_BY_ID} drops). Gate 3's own PCI clause is
 * {@link PciGate3Coverage}, read from the database. {@code REPLACED_BY_ID} is safe when {@code EngineSingletonLock} (nexus-5tykh) allows one
 * engine per database: the engine that drops a name index is the one that serves, so {@code IdReaders.PRESENT} is the serving engine's own value,
 * never a peer's. That lock is skipped under {@code NX_ENGINE_SINGLETON_LOCK=off} and behind a connection pooler, where a peer could still serve
 * name-predicate reads: so {@code Main} wires the retire only for an engine that took the lock.
 */
final class PciIdBuilds {

    private volatile BooleanSupplier gate2 = () -> false;
    private volatile BooleanSupplier gate3 = () -> false;

    void gate(BooleanSupplier gate2) {
        this.gate2 = gate2 == null ? () -> false : gate2;
    }

    private volatile BooleanSupplier pciOffLatch = () -> false;

    /** Whether the gate 3 latch row says it latched while PCI was off: the id builds then do not wait for gate 2. */
    void pciOffLatch(BooleanSupplier pciOffLatch) {
        this.pciOffLatch = pciOffLatch == null ? () -> false : pciOffLatch;
    }

    boolean gate2Holds() {
        try {
            return gate2.getAsBoolean() || pciOffLatch.getAsBoolean();
        } catch (RuntimeException e) {   // unknown is not "holds"
            return false;
        }
    }

    void gate3(BooleanSupplier gate3) {
        this.gate3 = gate3 == null ? () -> false : gate3;
    }

    private volatile BooleanSupplier readers = IdReaders::present;

    void readers(BooleanSupplier readers) {
        this.readers = readers == null ? () -> false : readers;
    }

    /**
     * Whether the name family retires this pass: gate 3 has LATCHED (the engine's cached read of {@code nexus.rdr229_gate_latch}, never the live
     * gate 3) AND the id readers are present in this engine ({@link IdReaders#PRESENT}). Dropping a name index while the readers still select by
     * name would leave search without its index. Unknown is not "holds".
     */
    boolean gate3Holds() {
        try {
            return gate3.getAsBoolean() && readers.getAsBoolean();
        } catch (RuntimeException e) {
            return false;
        }
    }

    /** The P3.1 CHECKs of {@code nexus.chunks} whose validation means no row has a null id beside a name (gate 2 needs both). */
    static final List<String> CHUNK_ID_CHECKS = List.of("chunks_collection_id_check", "chunks_collection_id_not_null");

    private static final Logger log = LoggerFactory.getLogger(PciIdBuilds.class);

    /**
     * Whether both P3.1 CHECKs of {@code nexus.chunks} are present and validated ({@code pg_constraint.convalidated}). Then no row of any
     * collection can have a null {@code collection_id}, and the null-id scan is skipped: gate 2, which an id build waits for, requires
     * exactly these.
     */
    static boolean checksValidated(DSLContext tx) {
        var k = DSL.table(DSL.name("pg_catalog", "pg_constraint")).as("k");
        var r = DSL.table(DSL.name("pg_catalog", "pg_class")).as("r");
        var n = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
        List<Boolean> validated = tx.select(DSL.field(DSL.name("k", "convalidated"), Boolean.class))
            .from(k)
            .join(r).on(DSL.field(DSL.name("r", "oid")).eq(DSL.field(DSL.name("k", "conrelid"))))
            .join(n).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("r", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(DSL.field(DSL.name("r", "relname"), String.class).eq("chunks"))
            .and(DSL.field(DSL.name("k", "conname"), String.class).in(CHUNK_ID_CHECKS))
            .fetch(0, Boolean.class);
        return validated.size() == CHUNK_ID_CHECKS.size() && validated.stream().allMatch(Boolean.TRUE::equals);
    }

    /**
     * Of the ranked {@code candidates} (within the id budget, so at most the leaf's cap), the collections with a null-id row, or all of them
     * when a probe fails. Skipped when the CHECKs are validated ({@link #checksValidated}). Each probe is {@code EXISTS ... LIMIT 1} on the
     * (tenant, model, collection) primary-key prefix, inside a savepoint: a probe that times out (57014) or fails is rolled back alone and
     * answers "held back" for that collection, so it costs an id build this pass and never the leaf's name reconcile.
     */
    static Set<String> nullIdCollections(DSLContext tx, Leaf leaf, Collection<RegistryRow> candidates) {
        if (checksValidated(tx)) {
            return Set.of();
        }
        Set<String> out = new HashSet<>();
        for (RegistryRow row : candidates) {
            try {
                if (probe(tx, leaf.model(), leaf.tenant(), row.name())) {
                    out.add(row.name());
                }
            } catch (DataAccessException | java.sql.SQLException e) {
                log.warn("event=pci_null_id_probe_failed leaf={}.{} detail=\"the id build waits a pass; the name reconcile goes on\" cause=\"{}\"",
                    leaf.schema(), leaf.name(), e.getMessage());
                out.add(row.name());
            }
        }
        return out;
    }

    private static boolean probe(DSLContext tx, String model, String tenant, String collection) throws java.sql.SQLException {
        java.sql.Savepoint sp = tx.connectionResult(java.sql.Connection::setSavepoint);
        try {
            boolean found = hasNullId(tx, model, tenant, collection);
            tx.connection(c -> c.releaseSavepoint(sp));
            return found;
        } catch (DataAccessException e) {
            tx.connection(c -> c.rollback(sp));   // the transaction survives: only this probe is undone
            throw e;
        }
    }

    /** Whether a row of {@code collection} on (model, tenant) has a null {@code collection_id}; the tenant predicate is required. */
    static boolean hasNullId(DSLContext tx, String model, String tenant, String collection) {
        return tx.fetchExists(tx.selectOne().from(CHUNKS)
            .where(CHUNKS.EMBEDDING_MODEL.eq(model))
            .and(CHUNKS.TENANT_ID.eq(tenant))
            .and(CHUNKS.COLLECTION.eq(collection))
            .and(CHUNKS.COLLECTION_ID.isNull())
            .limit(1));
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

    /**
     * The registry rows of the leaf's (tenant, model). The tenant predicate is REQUIRED, not belt and braces: a superuser or BYPASSRLS admin role is
     * not filtered by row-level security, and another tenant's live row of the same name would mask this leaf's NOT_IN_REGISTRY drop.
     */
    static List<RegistryRow> registry(DSLContext tx, String tenant, String model) {
        return tx.select(CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.LIFECYCLE_STATE,
                CATALOG_COLLECTIONS.SUPERSEDED_BY, CATALOG_COLLECTIONS.COLLECTION_ID)
            .from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant))
            .and(CATALOG_COLLECTIONS.EMBEDDING_MODEL.eq(model))
            .fetch(r -> new RegistryRow(r.value1(), r.value2(), r.value3(), r.value4()));
    }

    /**
     * The backoff keys of the registry rows that can still build, so {@code forgetStaleBackoff} keeps their failures: the name of each, and the id
     * backoff key of each that has an id.
     */
    static Set<String> buildable(Collection<RegistryRow> registry) {
        Set<String> out = new HashSet<>();
        for (RegistryRow row : registry) {
            if (row.live() && !row.superseded()) {
                out.add(row.name());
                if (row.collectionId() != null) {
                    out.add(PciReconcilePlanner.idBackoffKey(row.collectionId()));
                }
            }
        }
        return out;
    }

    /**
     * The collections the leaf's counting transaction counts: those with a name index ({@code indexed}), those with an id index (an id index judges
     * its collection by the count too), and the registry's buildable ones. Only a collection that can build counts for anything; a quarantined one
     * without an index decides nothing.
     */
    static Set<String> countTargets(Leaf leaf, Set<String> indexed, Collection<RegistryRow> registry) {
        Set<String> out = new LinkedHashSet<>(indexed);
        out.addAll(idIndexedNames(leaf, registry));
        for (RegistryRow row : registry) {
            if (row.live() && !row.superseded()) {
                out.add(row.name());
            }
        }
        return out;
    }

    /**
     * The leaf's plan with the id half ({@link PciReconcilePlanner#planWithIds}): the null-id read is {@link #nullIdCollections} and the recount of
     * the ids' candidates is {@link PciLeafCounting#count} capped at {@code rankCap}, both in the counting transaction {@code tx}.
     */
    static PciReconcilePlanner.Plan plan(DSLContext tx, Leaf leaf, int dimension, Map<String, Integer> counts,
                                         Collection<RegistryRow> registry, Set<String> backedOff, String inFlight,
                                         PciSettings settings, boolean gate2, boolean gate3, int rankCap) {
        return plan(tx, leaf, dimension, counts, registry, backedOff, inFlight, settings, gate2, gate3, rankCap, Gate3.never());
    }

    /**
     * {@link #plan} whose recount of the ids' candidates reads each candidate's rows by {@code collection_id} once {@code readGate} holds
     * (RDR-229 P3.5c, nexus-p2m1x.25; {@link PciLeafCounting#count(DSLContext, int, String, String, String, int, Gate3)}): an id build's
     * candidate is a registry row, and a registry row renamed in place has chunk rows under its old label. {@code gate3} (the boolean) is
     * still P3.4's retire switch and is unchanged; {@code readGate} is the engine's Gate3 read at each count.
     */
    static PciReconcilePlanner.Plan plan(DSLContext tx, Leaf leaf, int dimension, Map<String, Integer> counts,
                                         Collection<RegistryRow> registry, Set<String> backedOff, String inFlight,
                                         PciSettings settings, boolean gate2, boolean gate3, int rankCap, Gate3 readGate) {
        var inputs = new PciReconcilePlanner.IdPlanInputs(gate2, new PciReconcilePlanner.IdReads() {
            @Override
            public Set<String> nullIdCollections(Collection<RegistryRow> candidates) {
                return PciIdBuilds.nullIdCollections(tx, leaf, candidates);
            }

            @Override
            public Map<String, Integer> recount(Collection<String> collections) {
                Map<String, Integer> out = new HashMap<>();
                collections.forEach(c ->
                    out.put(c, PciLeafCounting.count(tx, dimension, c, leaf.model(), leaf.tenant(), rankCap, readGate)));
                return out;
            }
        }, gate3);
        return PciReconcilePlanner.planWithIds(leaf, counts, registry, backedOff, inFlight, settings, inputs);
    }
}
