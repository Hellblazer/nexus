// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.datamove.CompletionCheck;
import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.vectors.PciCatalog.Index;
import dev.nexus.service.vectors.PciCatalog.Leaf;
import dev.nexus.service.db.PgSession;
import org.jooq.DSLContext;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.TreeSet;
import java.util.function.Supplier;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_MODELS;

/**
 * RDR-229 P3.4 (nexus-p2m1x.22): the PCI clause of gate 3, read from the database and nothing else (linda, 2026-11-22). Every engine
 * computes the same answer from the same catalog and registry, so a standby without the builder lock agrees with the lock holder.
 * (An earlier form read the reconciler's in-process plan, which a standby never makes.)
 *
 * <p><b>What it reads.</b> A {@link PciCatalog} snapshot over every leaf, and for each leaf the registry rows of the leaf's tenant and model.
 * The registry read binds {@code nexus.tenant} on the check's session around the read (and clears it), as the reconciler does: where the admin
 * role has no owner-read policy on the row-secured registry, a read without the tenant is empty. A leaf with a name index whose registry read came
 * back empty is UNKNOWN, never "nothing to cover": the clause then answers nothing and gate 3 fails closed.
 *
 * <p><b>The clause</b> (RDR item 6: every collection the planner would index has a VALID id-predicate index). A live, unsuperseded collection that
 * has an id is <i>required</i> when the planner would index it:
 * <ul>
 *   <li>it holds a name-predicate index, of any validity, or an id-predicate index of any validity; or</li>
 *   <li>it has at least {@code B} rows ({@code NX_SEARCH_PCI_BUILD_MIN_ROWS}, the planner's threshold) and no index of either kind: a collection that
 *       was renamed (a new id) or has just crossed {@code B} is required before its index exists, so gate 3 cannot latch ahead of it. A leaf
 *       builds at most {@code NX_SEARCH_PCI_MAX_PER_LEAF} id indexes, so of the collections that qualify only as many as the leaf has free
 *       slots (the cap less the collections that already hold an index and are required) are required, taken in the planner's order: the
 *       largest by a recount at {@code 10 * B}, then by name.</li>
 * </ul>
 * It is <i>covered</i> when its id-predicate index is VALID. So a name index or an INVALID id index with no VALID id index is uncovered, and
 * <b>dropping name indexes cannot flip gate 3</b> ({@code REPLACED_BY_ID}, after the latch): a collection whose name index is gone but whose id
 * index is VALID is required and covered, exactly as before the drop. A name index of a collection that is not live, is superseded or has no
 * registry row is not required: the planner never builds an id index for it and drops its name index by its own rules.
 *
 * <p><b>Cost, and the near-latch scan.</b> The second bullet needs a bounded row count per live collection with no index. It is the expensive
 * part, and it only matters when everything else is covered (gate 3 is false anyway while an indexed collection lacks its id index), so it
 * runs only then, once the indexed collections are all covered: in one short transaction, with {@code statement_timeout} bound, a total budget of
 * {@link #SCAN_BUDGET} and the planner's own probe (rows counted up to {@code B + 1}, tenant predicate required). A scan that runs out of budget, or
 * fails, makes the clause unknown (fail closed). A positive answer is kept for {@link #SCAN_TTL}, less the collections that have since got an index;
 * a negative one is never kept, so the read that lets gate 3 latch is always a fresh scan. Once gate 3 has latched, the engine repeats this
 * read only every {@code CompletionBootWork.LATCHED_COVERAGE_PERIOD}.
 *
 * <p><b>The empty case (RDR item 6: an empty plan is not a pass).</b> With nothing required after that scan the clause passes
 * ({@code nothingToIndex}): no live collection has an index or reaches the threshold. A snapshot with no leaf at all is not "verified": it
 * answers an empty plan without the flag, and the completion check then reads the registry itself (an empty plan over a populated registry
 * fails; over an empty one, a fresh install, it passes).
 *
 * <p><b>PCI off.</b> {@code NX_SEARCH_PCI=0}: no per-collection index exists to replace, so the clause is
 * {@link CompletionCheck.IdIndexCoverage#pciOff()} and gate 3 rests on its stamps, the registry, gate 2 and the further conditions.
 *
 * <p><b>Known edge.</b> If {@code NX_SEARCH_PCI_MAX_PER_LEAF} is lowered below the number of name indexes a leaf holds, the planner builds
 * id indexes for at most the new cap, and a name-indexed collection beyond it stays uncovered until its name index goes by its own rules.
 */
final class PciGate3Coverage implements CompletionCheck.Gate3Source {

    private static final Logger log = LoggerFactory.getLogger(PciGate3Coverage.class);

    /** How long a positive scan answer (unindexed collections at the threshold) is kept. */
    static final Duration SCAN_TTL = Duration.ofMinutes(10);

    /** The most one scan may take in total, over every leaf; past it the clause is unknown. */
    static final Duration SCAN_BUDGET = Duration.ofSeconds(60);

    private record Reg(String name, long id, boolean live, boolean superseded) { }

    private record Found(Set<String> keys, Instant at) { }

    private final Supplier<PciCatalog.Snapshot> catalog;
    private final PciSettings settings;
    private final Clock clock;
    /** The last scan's unindexed-collection keys (model, tenant, id), while any: kept {@link #SCAN_TTL}; never an empty or unknown answer. */
    private volatile Found unindexed;

    /** The one latched gate-3 signal the near-latch scan's probe and recount read the collection by id from (RDR-229 P3.5c). */
    private final Gate3 readGate;

    /** Every caller names its gate: there is no overload that stands for {@code Gate3.never()} (a gate-less scan reads by name, RDR-229 P3.5c). */
    PciGate3Coverage(Supplier<PciCatalog.Snapshot> catalog, PciSettings settings, Clock clock, Gate3 readGate) {
        this.catalog = catalog;
        this.settings = settings;
        this.clock = clock;
        this.readGate = java.util.Objects.requireNonNull(readGate, "readGate");
    }

    @Override
    public Optional<CompletionCheck.IdIndexCoverage> read(DSLContext ctx) {
        if (!settings.enabled()) {
            return Optional.of(CompletionCheck.IdIndexCoverage.pciOff());
        }
        try {
            return readLeaves(ctx, catalog.get());
        } catch (RuntimeException e) {   // unknown is not "covered"
            log.warn("event=pci_gate3_coverage_unread cause=\"{}\"", e.toString());
            return Optional.empty();
        }
    }

    private Optional<CompletionCheck.IdIndexCoverage> readLeaves(DSLContext ctx, PciCatalog.Snapshot snapshot) {
        Set<String> required = new TreeSet<>();
        Set<String> covered = new TreeSet<>();
        Set<String> invalid = new TreeSet<>();
        Map<Leaf, Map<String, Reg>> registries = new HashMap<>();
        for (Leaf leaf : snapshot.leaves()) {
            if (leaf.model() == null || leaf.tenant() == null) {
                continue;
            }
            Set<String> names = new HashSet<>();
            for (Index i : leaf.indexes()) {
                if (i.parsed()) {
                    names.add(i.collection());
                }
            }
            Set<Long> validIds = new HashSet<>();
            Set<Long> invalidIds = new HashSet<>();
            for (Index i : leaf.idIndexes()) {
                (i.valid() ? validIds : invalidIds).add(i.collectionId());
            }
            if (names.isEmpty() && validIds.isEmpty() && invalidIds.isEmpty()) {
                continue;
            }
            Map<String, Reg> registry = registry(ctx, leaf.model(), leaf.tenant());
            registries.put(leaf, registry);
            if (!names.isEmpty() && registry.isEmpty()) {
                return Optional.empty();   // the registry was not visible: unknown, not "nothing to cover"
            }
            Map<Long, Reg> byId = new HashMap<>();
            registry.values().forEach(r -> byId.put(r.id(), r));
            for (String name : names) {
                Reg r = registry.get(name);
                if (r == null || !r.live() || r.superseded()) {
                    continue;   // going away by the planner's own rules, or not the planner's to index
                }
                String key = PciCatalog.idKey(leaf.model(), leaf.tenant(), r.id());
                required.add(key);
                if (validIds.contains(r.id())) {
                    covered.add(key);
                }
            }
            for (long id : validIds) {
                Reg r = byId.get(id);
                if (r != null && r.live() && !r.superseded()) {   // an id index the planner keeps: required, and covered
                    String key = PciCatalog.idKey(leaf.model(), leaf.tenant(), id);
                    required.add(key);
                    covered.add(key);
                }
            }
            for (long id : invalidIds) {
                Reg r = byId.get(id);
                if (r != null && r.live() && !r.superseded()) {
                    String key = PciCatalog.idKey(leaf.model(), leaf.tenant(), id);
                    required.add(key);
                    invalid.add(key);
                }
            }
        }
        boolean anyInvalid = snapshot.invalidIdCount() > 0;   // any INVALID id-predicate index, a dropped collection's included
        if (snapshot.leaves().isEmpty()) {
            // Not "verified empty": the completion check reads the registry itself and fails an empty plan over a populated one.
            return Optional.of(new CompletionCheck.IdIndexCoverage(Set.of(), Set.of(), Set.of(), false, false, anyInvalid));
        }
        if (covered.containsAll(required)) {
            // Everything that holds an index is covered: only now does a collection with none of either kind matter (the near-latch scan).
            required.addAll(unindexedAtThreshold(ctx, snapshot, registries, required));
        }
        if (required.isEmpty()) {
            return Optional.of(new CompletionCheck.IdIndexCoverage(Set.of(), Set.of(), Set.of(), false, true, anyInvalid));
        }
        return Optional.of(new CompletionCheck.IdIndexCoverage(required, covered, invalid, false, false, anyInvalid));
    }

    /** The registry rows of (tenant, model) by name, read with the tenant bound on the check's session and cleared after. */
    private static Map<String, Reg> registry(DSLContext ctx, String model, String tenant) {
        bindTenant(ctx, tenant);
        try {
            Map<String, Reg> out = new HashMap<>();
            ctx.select(CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.COLLECTION_ID, CATALOG_COLLECTIONS.LIFECYCLE_STATE,
                    CATALOG_COLLECTIONS.SUPERSEDED_BY)
                .from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant))   // required: the admin role may read every tenant's rows
                .and(CATALOG_COLLECTIONS.EMBEDDING_MODEL.eq(model))
                .fetch()
                .forEach(r -> {
                    if (r.value2() != null) {
                        out.put(r.value1(), new Reg(r.value1(), r.value2(), "live".equals(r.value3()),
                            r.value4() != null && !r.value4().isEmpty()));
                    }
                });
            return out;
        } finally {
            bindTenant(ctx, "");
        }
    }

    private static void bindTenant(DSLContext ctx, String tenant) {
        ctx.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val("nexus.tenant"), DSL.val(tenant), DSL.inline(false)))
            .fetch();
    }

    /**
     * The keys of the live, unsuperseded collections with an id and at least {@code B} rows on their leaf that hold no index of either kind
     * ({@code held}). A kept positive answer is reused until {@link #SCAN_TTL} or until every collection in it has got an index; a negative or
     * unknown answer is never kept. Throws (the clause is unknown) when a leaf has rows the registry read cannot see, or the budget runs out.
     */
    private Set<String> unindexedAtThreshold(DSLContext ctx, PciCatalog.Snapshot snapshot, Map<Leaf, Map<String, Reg>> known, Set<String> held) {
        Found kept = unindexed;
        Instant now = clock.instant();
        if (kept != null && Duration.between(kept.at(), now).compareTo(SCAN_TTL) < 0) {
            Set<String> still = new TreeSet<>(kept.keys());
            still.removeAll(held);
            if (!still.isEmpty()) {
                return still;
            }
        }
        Set<String> found = new TreeSet<>();
        // One short transaction bounds the probes: PgSession.setLocal sets the statement bound and its matching network timeout, and the
        // transaction's end resets both, so nothing is left on the check's session (NetworkBoundStatementTimeoutGateTest).
        ctx.transaction(cfg -> {
            DSLContext tx = DSL.using(cfg);
            PgSession.setLocal(tx, "statement_timeout", Long.toString(PciReconciler.COUNT_TIMEOUT.toMillis()));
            Instant until = now.plus(SCAN_BUDGET);
            int b = settings.buildMinRows();
            for (Leaf leaf : snapshot.leaves()) {
                if (leaf.model() == null || leaf.tenant() == null) {
                    continue;
                }
                Integer dim = tx.select(EMBEDDING_MODELS.DIMENSION).from(EMBEDDING_MODELS)
                    .where(EMBEDDING_MODELS.EMBEDDING_MODEL.eq(leaf.model())).fetchOne(EMBEDDING_MODELS.DIMENSION);
                if (dim == null) {
                    continue;
                }
                Map<String, Reg> registry = known.containsKey(leaf) ? known.get(leaf) : registry(tx, leaf.model(), leaf.tenant());
                if (registry.isEmpty()) {
                    if (leafHasRows(tx, leaf)) {
                        throw new IllegalStateException("a leaf holds rows and the registry shows none of its tenant: not visible, so unknown");
                    }
                    continue;
                }
                // The planner builds at most NX_SEARCH_PCI_MAX_PER_LEAF id indexes on a leaf, the ones that already stand (of any validity) first,
                // then the largest of those that qualify. A leaf with more qualifying collections than free slots can never be covered whole, so
                // the clause asks for as many as the planner would build, in the planner's own order, and no more (critic S-a).
                int occupied = 0;
                List<Reg> qualifying = new ArrayList<>();
                Map<String, Integer> counted = new HashMap<>();
                for (Reg r : registry.values()) {
                    String key = PciCatalog.idKey(leaf.model(), leaf.tenant(), r.id());
                    if (!r.live() || r.superseded()) {
                        continue;
                    }
                    if (held.contains(key)) {
                        occupied++;
                        continue;
                    }
                    if (!clock.instant().isBefore(until)) {
                        throw new IllegalStateException("the near-latch scan ran out of its budget of " + SCAN_BUDGET + ": unknown");
                    }
                    // the probe by the collection's id from the latch (the selection resolves the label live-first), by name before it
                    Integer n = PgVectorRepository.probeSelectedRowsQuery(tx, dim, CollectionSelector.of(readGate, tx, leaf.tenant(), r.name()),
                            leaf.model(), leaf.tenant(), b).fetchOne(0, Integer.class);
                    if (n != null && n >= b) {
                        qualifying.add(r);
                        counted.put(r.name(), n);
                    }
                }
                int free = Math.max(0, settings.maxPerLeaf() - occupied);
                if (qualifying.size() > free) {
                    // Every count stops at B, so they tie: recount higher, as the planner does, and take the largest first, then by name.
                    int rankCap = PciReconciler.RANK_CAP_FACTOR * b;
                    for (Reg r : qualifying) {
                        if (!clock.instant().isBefore(until)) {
                            throw new IllegalStateException("the near-latch scan ran out of its budget of " + SCAN_BUDGET + ": unknown");
                        }
                        counted.put(r.name(), PciLeafCounting.count(tx, dim, r.name(), leaf.model(), leaf.tenant(), rankCap, readGate));
                    }
                    qualifying.sort(java.util.Comparator.<Reg>comparingInt(r -> counted.get(r.name())).reversed()
                        .thenComparing(Reg::name));
                }
                for (Reg r : qualifying.subList(0, Math.min(free, qualifying.size()))) {
                    found.add(PciCatalog.idKey(leaf.model(), leaf.tenant(), r.id()));
                }
            }
        });
        unindexed = found.isEmpty() ? null : new Found(Set.copyOf(found), now);
        return found;
    }

    /** Whether the leaf of (model, tenant) holds any chunk row; the tenant predicate is required. */
    private static boolean leafHasRows(DSLContext tx, Leaf leaf) {
        return tx.fetchExists(tx.selectOne().from(CHUNKS).where(CHUNKS.EMBEDDING_MODEL.eq(leaf.model()))
            .and(CHUNKS.TENANT_ID.eq(leaf.tenant())));
    }
}
