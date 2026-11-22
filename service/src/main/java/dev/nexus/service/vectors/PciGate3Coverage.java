// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.datamove.CompletionCheck;
import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.vectors.PciCatalog.Index;
import dev.nexus.service.vectors.PciCatalog.Leaf;
import org.jooq.DSLContext;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.TreeSet;
import java.util.function.Supplier;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_MODELS;

/**
 * RDR-229 P3.4 (nexus-p2m1x.22): the PCI clause of gate 3, read from the database and nothing else (linda, 2026-11-22). Every engine
 * computes the same answer from the same catalog and registry, so a standby without the builder lock agrees with the lock holder.
 * (An earlier form read the reconciler's in-process plan, which a standby never makes.)
 *
 * <p><b>What it reads.</b> A {@link PciCatalog} snapshot over every leaf, and for each leaf that holds a per-collection index the
 * registry rows of the leaf's tenant and model. The registry read binds {@code nexus.tenant} on the check's session around the read (and
 * clears it), as the reconciler does: where the admin role has no owner-read policy on the row-secured registry, a read without the tenant
 * is empty. A leaf with a VALID name index whose registry read came back empty is UNKNOWN, never "nothing to cover": the clause then fails.
 *
 * <p><b>The clause.</b> Per leaf, a collection is <i>required</i> when it is live and not superseded in the registry and either has a
 * VALID name-predicate index (the router's index today) or has an id-predicate index of any validity. It is <i>covered</i> when its
 * id-predicate index is VALID. So:
 * <ul>
 *   <li>A name index with no VALID id index is uncovered; so is an id index that is INVALID (building, or killed).</li>
 *   <li><b>Dropping name indexes cannot flip gate 3</b> ({@code REPLACED_BY_ID}, after gate 3): a collection whose name index is gone but whose id
 *       index is VALID is required and covered, exactly as before the drop. Gate 3 reads {@code covered = required}, and a drop moves nothing from
 *       covered to uncovered.</li>
 *   <li>A name index of a collection that is not live, is superseded or has no registry row is not required: the planner never builds an
 *       id index for it and drops its name index by its own rules, so requiring it would hold gate 3 closed on an index that is going away or
 *       belongs to a quarantine.</li>
 * </ul>
 * A collection at the build threshold that has no index of either kind is NOT required by the clause above (the planner may be mid-build, or
 * capped out); it matters only in the empty case, below.
 *
 * <p><b>The empty case (RDR item 6: an empty plan is not a pass).</b> With nothing required (no live collection holds a VALID name index or an
 * id index of any validity), the clause passes only if nothing qualifies to be built: no live, unsuperseded collection has at least {@code B} rows
 * ({@code NX_SEARCH_PCI_BUILD_MIN_ROWS}, the planner's threshold) on its leaf. Otherwise a store that should have indexes and has none
 * (PCI just enabled, or the reconciler not yet run) fails. "Should have them" is therefore the planner's own
 * build rule, evaluated by the same bounded probe (rows counted up to {@code B + 1}, with the tenant predicate), and only in this state of
 * nothing required; the answer, either way, is kept for {@link #VACUITY_TTL}, so a store of many small collections is scanned once per ten
 * minutes and not once per poll. A store with a qualifying collection and an index on some other one is not the empty case: its qualifying but
 * unindexed collection is the reconciler's next build, not a veto.
 *
 * <p><b>PCI off.</b> {@code NX_SEARCH_PCI=0}: no per-collection index exists to replace, so the clause is
 * {@link CompletionCheck.IdIndexCoverage#pciOff()} and gate 3 rests on its stamps, the registry, gate 2 and the further conditions.
 *
 * <p><b>Known edge.</b> If {@code NX_SEARCH_PCI_MAX_PER_LEAF} is lowered below the number of name indexes a leaf holds, the planner builds
 * id indexes for at most the new cap, and a name-indexed collection beyond it stays uncovered until its name index goes by its own rules.
 */
final class PciGate3Coverage implements CompletionCheck.Gate3Source {

    private static final Logger log = LoggerFactory.getLogger(PciGate3Coverage.class);

    /** How long the empty-case scan's answer is kept. */
    static final Duration VACUITY_TTL = Duration.ofMinutes(10);

    /** The planned key of a store that should have indexes and has none: it is never covered. */
    static final String QUALIFYING_UNINDEXED = "qualifying-collection-without-any-index";

    private record Reg(long id, boolean live, boolean superseded) { }

    private record Vacuity(boolean qualifies, Instant at) { }

    private final Supplier<PciCatalog.Snapshot> catalog;
    private final PciSettings settings;
    private final Clock clock;
    private volatile Vacuity vacuity;

    PciGate3Coverage(Supplier<PciCatalog.Snapshot> catalog, PciSettings settings, Clock clock) {
        this.catalog = catalog;
        this.settings = settings;
        this.clock = clock;
    }

    @Override
    public Optional<CompletionCheck.IdIndexCoverage> read(DSLContext ctx) {
        if (!settings.enabled()) {
            return Optional.of(CompletionCheck.IdIndexCoverage.pciOff());
        }
        try {
            PciCatalog.Snapshot snapshot = catalog.get();
            if (snapshot.leaves().isEmpty()) {
                return Optional.empty();   // a failed read, never "there are no indexes"
            }
            return readLeaves(ctx, snapshot);
        } catch (RuntimeException e) {   // unknown is not "covered"
            log.warn("event=pci_gate3_coverage_unread cause=\"{}\"", e.toString());
            return Optional.empty();
        }
    }

    private Optional<CompletionCheck.IdIndexCoverage> readLeaves(DSLContext ctx, PciCatalog.Snapshot snapshot) {
        Set<String> required = new TreeSet<>();
        Set<String> covered = new TreeSet<>();
        Set<String> invalid = new TreeSet<>();
        for (Leaf leaf : snapshot.leaves()) {
            if (leaf.model() == null || leaf.tenant() == null) {
                continue;
            }
            Set<String> validNames = new HashSet<>();
            for (Index i : leaf.indexes()) {
                if (i.parsed() && i.valid()) {
                    validNames.add(i.collection());
                }
            }
            Set<Long> validIds = new HashSet<>();
            Set<Long> invalidIds = new HashSet<>();
            for (Index i : leaf.idIndexes()) {
                (i.valid() ? validIds : invalidIds).add(i.collectionId());
            }
            if (validNames.isEmpty() && validIds.isEmpty() && invalidIds.isEmpty()) {
                continue;
            }
            Map<String, Reg> registry = registry(ctx, leaf.model(), leaf.tenant());
            Map<Long, Reg> byId = new HashMap<>();
            registry.values().forEach(r -> byId.put(r.id(), r));
            if (!validNames.isEmpty() && registry.isEmpty()) {
                return Optional.empty();   // the registry was not visible: unknown, not "nothing to cover"
            }
            for (String name : validNames) {
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
        if (!required.isEmpty()) {
            return Optional.of(new CompletionCheck.IdIndexCoverage(required, covered, invalid, false, false, anyInvalid));
        }
        return Optional.of(qualifiesAnywhere(ctx, snapshot)
            ? new CompletionCheck.IdIndexCoverage(Set.of(QUALIFYING_UNINDEXED), Set.of(), Set.of(), false, false, anyInvalid)
            : new CompletionCheck.IdIndexCoverage(Set.of(), Set.of(), Set.of(), false, true, anyInvalid));
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
                        out.put(r.value1(), new Reg(r.value2(), "live".equals(r.value3()),
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

    /** Whether some live, unsuperseded collection has at least B rows on its leaf; cached for {@link #VACUITY_TTL}. */
    private boolean qualifiesAnywhere(DSLContext ctx, PciCatalog.Snapshot snapshot) {
        Vacuity kept = vacuity;
        Instant now = clock.instant();
        if (kept != null && Duration.between(kept.at(), now).compareTo(VACUITY_TTL) < 0) {
            return kept.qualifies();
        }
        boolean qualifies = false;
        String before = ctx.select(DSL.function("current_setting", SQLDataType.VARCHAR, DSL.val("statement_timeout")))
            .fetchOne(0, String.class);
        ctx.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val("statement_timeout"),
            DSL.val(Long.toString(PciReconciler.COUNT_TIMEOUT.toMillis())), DSL.inline(false))).fetch();
        try {
            int b = settings.buildMinRows();
            leaves:
            for (Leaf leaf : snapshot.leaves()) {
                if (leaf.model() == null || leaf.tenant() == null) {
                    continue;
                }
                Integer dim = ctx.select(EMBEDDING_MODELS.DIMENSION).from(EMBEDDING_MODELS)
                    .where(EMBEDDING_MODELS.EMBEDDING_MODEL.eq(leaf.model())).fetchOne(EMBEDDING_MODELS.DIMENSION);
                if (dim == null) {
                    continue;
                }
                for (String name : buildable(ctx, leaf)) {
                    Integer n = PgVectorRepository.probeSelectedRowsQuery(ctx, dim, new String[] {name},
                            leaf.model(), leaf.tenant(), b).fetchOne(0, Integer.class);
                    if (n != null && n >= b) {
                        qualifies = true;
                        break leaves;
                    }
                }
            }
        } finally {
            ctx.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val("statement_timeout"), DSL.val(before),
                DSL.inline(false))).fetch();
        }
        vacuity = new Vacuity(qualifies, now);
        return qualifies;
    }

    /** The live, unsuperseded collections of the leaf's tenant and model: the ones the planner may build. */
    private static List<String> buildable(DSLContext ctx, Leaf leaf) {
        bindTenant(ctx, leaf.tenant());
        try {
            return ctx.select(CATALOG_COLLECTIONS.NAME).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(leaf.tenant()))
                .and(CATALOG_COLLECTIONS.EMBEDDING_MODEL.eq(leaf.model()))
                .and(CATALOG_COLLECTIONS.LIFECYCLE_STATE.eq("live"))
                .and(DSL.coalesce(CATALOG_COLLECTIONS.SUPERSEDED_BY, "").eq(""))
                .fetch(CATALOG_COLLECTIONS.NAME);
        } finally {
            bindTenant(ctx, "");
        }
    }
}
