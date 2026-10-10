// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.vectors.PciCatalog.Index;
import dev.nexus.service.vectors.PciCatalog.Leaf;

import java.util.ArrayList;
import java.util.Collection;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;

/**
 * RDR-227 Step 2 (nexus-43ulx.18): what the DDL half of the reconciler does to ONE tenant leaf, as a pure function of
 * what it saw. It reads nothing and writes nothing: no connection, no clock, no environment, and no logging. Counting
 * (the bounded per-tenant counts) and execution (the drops and the one-at-a-time builds) are nexus-43ulx.19.
 *
 * <p><b>Logging.</b> The planner has no sink. A zero count for a populated collection comes back as a
 * {@link SkipCountSuspect}, and the caller logs {@code event=pci_count_zero_indexed} for each one; nothing here can
 * fail to log, and nothing the planner decides depends on whether it was logged.
 *
 * <p><b>Inputs the caller owes.</b>
 * <ul>
 *   <li>{@code leaf}: one element of {@link PciCatalog.Snapshot#leaves()}. A leaf whose model or tenant did not parse
 *       gets an empty plan (its indexes are unparsed anyway, and a name cannot be made without both).</li>
 *   <li>{@code counts}: rows per collection on this leaf, counted under {@code SET LOCAL nexus.tenant} and stopped at
 *       B+1 rows. A collection with no row is absent; absent means zero. Both {@code count >= B} and
 *       {@code count * 2 < B} are decided identically by B+1 and by any larger number, so the cap loses nothing. A
 *       negative count is refused.</li>
 *   <li>{@code registry}: the collection registry's rows for this leaf's tenant AND model, nothing else. A name not in
 *       it is "no longer listed". If the registry read failed, run no plan: an empty list would read as "everything
 *       was deleted" and drop every index (the same contract as an empty {@link PciCatalog.Snapshot#leaves()}).</li>
 *   <li>{@code backedOff}: the collections whose retry time has not arrived. The planner does not hold backoff state.</li>
 *   <li>{@code inFlightIndex}: the name of the build this holder is running now, or {@code null}.</li>
 * </ul>
 *
 * <p><b>Output.</b> An ordered list the executor runs front to back: every {@link Drop} (by index name), then every
 * {@link SkipCountSuspect} (by collection), then every {@link Build} (largest count first, then collection name). Drops
 * come first because a drop frees a cap slot the same plan then spends. A drop that fails (the 5 s lock timeout) leaves
 * its index in place, so the leaf can briefly hold one more than the cap; the next pass drops it again.
 *
 * <p><b>Rules</b> (RDR Lifecycle, with Sam's decisions 1, 2 and 4, T2
 * {@code nexus_rdr/227-planner-decisions-confirmed}):
 * <ul>
 *   <li>Build a collection that is {@code lifecycle_state = 'live'} (exactly that string; null, empty and every other
 *       state never build), has an empty {@code superseded_by}, counts {@code >= B}, has no parsed {@code pci_} index
 *       on the leaf under ANY name or validity, and is not backed off.</li>
 *   <li>Drop a valid index when the registry does not list its collection, or its {@code superseded_by} is set, or
 *       {@code count * 2 < B}. Between B/2 and B the index stays and nothing builds. The state alone never drops an
 *       index: an existing index on a quarantine, dormant or disputed collection drops only by these rules.</li>
 *   <li><b>Zero-count guard.</b> "Populated" means the collection holds a parsed {@code pci_} index on this leaf, so an
 *       earlier pass counted it at or above B. A count of zero for a populated collection that is live and not
 *       superseded is a counting failure (a count taken without the tenant set reads zero): {@link SkipCountSuspect},
 *       never a drop. Consequence: a live collection that really was emptied keeps its index (an empty partial index,
 *       a few pages) until the registry removes or supersedes it. A zero for a non-live collection is not suspect
 *       (quarantine moves rows, so a zero is expected) and the count rule drops it.</li>
 *   <li>An invalid index that is not this holder's in-flight build is a failed build: {@link DropReason#FAILED_BUILD},
 *       whatever the registry says. The collection is NOT rebuilt in the same plan: the build is
 *       {@code CREATE INDEX CONCURRENTLY IF NOT EXISTS}, which would be a silent no-op against a failed index whose
 *       drop timed out. The rebuild is the next pass, once the name is free. The holder's own in-flight index is left
 *       alone whatever the rules say; the next pass judges it once it is valid.</li>
 *   <li>An unparsed index ({@link Index#parsed()} false) is never touched (decision 4).</li>
 *   <li>{@code NX_SEARCH_PCI=0}: an empty plan, so no builds and no drops.</li>
 * </ul>
 *
 * <p><b>Cap, and what counts against it.</b> {@code NX_SEARCH_PCI_MAX_PER_LEAF} bounds the "valid or building" indexes
 * per leaf. This planner counts exactly: each valid parsed index that survives this plan, plus this holder's in-flight
 * build. It does NOT count an invalid index that is being dropped this pass (it is going away and was never serving),
 * and it does NOT count an unparsed index (the builder did not make it and cannot drop it, so charging it would let an
 * operator-made {@code pci_} index starve real collections of their slots). Free slots are the cap minus that count;
 * a maximum of 0 builds none but still drops. An existing index is never dropped for being over the cap (the RDR gives
 * no such reason): lowering the cap below what exists builds nothing until drops bring the leaf under it.
 *
 * <p><b>Ranking, and a limit of capped counts.</b> Candidates are ranked by the count they arrive with, largest first,
 * then by collection name. Every candidate counts at least B, so with counts stopped at B+1 only two values occur and
 * the rank is effectively B+1 first, then name. To keep the genuinely largest collections when more qualify than the
 * cap admits, the caller must hand the candidates' real (or higher-capped) counts. The planner ranks by whatever it is
 * given.
 */
public final class PciReconcilePlanner {

    private PciReconcilePlanner() { }

    /**
     * The registry fields the planner reads, one row per (tenant, collection) the caller already narrowed to the leaf's
     * tenant and model.
     *
     * @param name           the collection name
     * @param lifecycleState {@code catalog_collections.lifecycle_state}; nullable column, and only {@code "live"} is live
     * @param supersededBy   {@code catalog_collections.superseded_by}; null and {@code ""} both mean not superseded
     */
    public record RegistryRow(String name, String lifecycleState, String supersededBy) {
        public RegistryRow {
            Objects.requireNonNull(name, "name");
        }

        boolean live() {
            return "live".equals(lifecycleState);
        }

        boolean superseded() {
            return supersededBy != null && !supersededBy.isEmpty();
        }
    }

    /** Why an index is dropped; {@link #label()} is the value for the log line. */
    public enum DropReason {
        /** Invalid and not this holder's in-flight build. */
        FAILED_BUILD("failed_build"),
        /** {@code count * 2 < B}. */
        BELOW_HALF_BUILD_THRESHOLD("below_half_build_threshold"),
        /** The registry no longer lists the collection for this tenant and model. */
        NOT_IN_REGISTRY("not_in_registry"),
        /** The registry row has {@code superseded_by} set. */
        SUPERSEDED("superseded");

        private final String label;

        DropReason(String label) {
            this.label = label;
        }

        public String label() {
            return label;
        }
    }

    /** One step of a plan. */
    public sealed interface Action permits Build, Drop, SkipCountSuspect { }

    /** Build {@code name} on the leaf for {@code collection}; {@code name} is {@link PciCatalog#indexName}. */
    public record Build(String name, String collection) implements Action { }

    /** Drop the existing index {@code name}, which serves {@code collection}. */
    public record Drop(String name, String collection, DropReason reason) implements Action { }

    /**
     * The count for this populated, live, unsuperseded collection was zero: a counting failure, never acted on. The
     * caller logs {@code event=pci_count_zero_indexed}.
     */
    public record SkipCountSuspect(String collection) implements Action { }

    /** The plan for one leaf; see the class comment for the rules and the inputs. */
    public static List<Action> plan(Leaf leaf, Map<String, Integer> counts, Collection<RegistryRow> registry,
                                    Set<String> backedOff, String inFlightIndex, PciSettings settings) {
        Objects.requireNonNull(leaf, "leaf");
        Objects.requireNonNull(counts, "counts");
        Objects.requireNonNull(registry, "registry");
        Objects.requireNonNull(backedOff, "backedOff");
        Objects.requireNonNull(settings, "settings");
        for (Map.Entry<String, Integer> e : counts.entrySet()) {
            if (e.getValue() == null || e.getValue() < 0) {
                throw new IllegalArgumentException("count for " + e.getKey() + " must be >= 0, got " + e.getValue());
            }
        }
        if (!settings.enabled() || leaf.model() == null || leaf.tenant() == null) {
            return List.of();
        }
        long b = settings.buildMinRows();
        Map<String, RegistryRow> rows = new HashMap<>();
        for (RegistryRow row : registry) {
            rows.put(row.name(), row);
        }

        TreeMap<String, Drop> drops = new TreeMap<>();
        TreeSet<String> suspects = new TreeSet<>();
        Set<String> served = new HashSet<>();
        int kept = 0;
        for (Index index : leaf.indexes()) {
            if (!index.parsed()) {
                continue;
            }
            String collection = index.collection();
            served.add(collection);
            if (!index.valid()) {
                if (index.name().equals(inFlightIndex)) {
                    kept++;
                } else {
                    drops.put(index.name(), new Drop(index.name(), collection, DropReason.FAILED_BUILD));
                }
                continue;
            }
            RegistryRow row = rows.get(collection);
            long count = counts.getOrDefault(collection, 0);
            if (row == null) {
                drops.put(index.name(), new Drop(index.name(), collection, DropReason.NOT_IN_REGISTRY));
            } else if (row.superseded()) {
                drops.put(index.name(), new Drop(index.name(), collection, DropReason.SUPERSEDED));
            } else if (row.live() && count == 0) {
                suspects.add(collection);
                kept++;
            } else if (count * 2 < b) {
                drops.put(index.name(), new Drop(index.name(), collection, DropReason.BELOW_HALF_BUILD_THRESHOLD));
            } else {
                kept++;
            }
        }

        List<Action> plan = new ArrayList<>(drops.values());
        for (String collection : suspects) {
            plan.add(new SkipCountSuspect(collection));
        }
        int slots = settings.maxPerLeaf() - kept;
        if (slots > 0) {
            rows.values().stream()
                .filter(row -> row.live() && !row.superseded())
                .filter(row -> counts.getOrDefault(row.name(), 0) >= b)
                .filter(row -> !served.contains(row.name()) && !backedOff.contains(row.name()))
                .sorted(Comparator.<RegistryRow>comparingInt(row -> counts.get(row.name())).reversed()
                    .thenComparing(RegistryRow::name))
                .limit(slots)
                .forEach(row -> plan.add(new Build(PciCatalog.indexName(leaf.model(), leaf.tenant(), row.name()),
                    row.name())));
        }
        return List.copyOf(plan);
    }
}
