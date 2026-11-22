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
 * <p><b>Output.</b> An ordered list the executor runs front to back: every {@link Drop} (by index name; a
 * {@link DropReason#REINDEX_LEFTOVER} drop is one of them), then every
 * {@link SkipCountSuspect} (by collection), then every {@link Build} (largest count first, then collection name). Drops
 * come first because a drop frees a cap slot the same plan then spends. A drop that fails (the 5 s lock timeout) leaves
 * its index in place, so the leaf can briefly hold one more than the cap; the next pass drops it again.
 *
 * <p><b>Rules</b> (RDR Lifecycle, with Sam's decisions 1, 2 and 4, T2
 * {@code nexus_rdr/227-planner-decisions-confirmed}):
 * <ul>
 *   <li>Build a collection that is {@code lifecycle_state = 'live'} (exactly that string; null, empty and every other
 *       state never build), has an empty {@code superseded_by}, counts {@code >= B}, has no parsed {@code pci_} index
 *       on the leaf that survives this plan (a valid one, or the holder's in-flight build; a failed one is dropped by
 *       the same plan), and is not backed off.</li>
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
 *       whatever the registry says. Its collection is rebuilt in the same plan when the build rules allow it (live, not
 *       superseded, at least B rows, not backed off, a slot free), since the drop frees the name and the slot
 *       (nexus-f3yqd): waiting a sweep period for the rebuild lost that long after every restart that ended a build.
 *       The build is {@code CREATE INDEX CONCURRENTLY IF NOT EXISTS}, which would be a silent no-op against a failed
 *       index whose drop timed out, so the executor runs a build that follows such a drop only when the drop
 *       happened. The holder's own in-flight index is left alone whatever the rules say; the next pass judges it once
 *       it is valid.</li>
 *   <li>An unparsed index ({@link Index#parsed()} false) is never touched (decision 4), with ONE carve-out (Sam,
 *       2026-10-18, T2 {@code nexus_rdr/227-decision-4-amendment-reindex-leftovers}, nexus-rn1yd): a REINDEX
 *       CONCURRENTLY <i>leftover</i> ({@link Index#leftover()}: an INVALID {@code pci_<hash>_ccnew}/{@code _ccold[N]}
 *       that passed every other condition of the parsed rule) is dropped as {@link DropReason#REINDEX_LEFTOVER},
 *       whatever the registry and the counts say. It is still NOT parsed in the routing sense: it never serves, never
 *       blocks a build for its collection, and is never charged against the cap. A VALID {@code _ccnew}/{@code _ccold}
 *       is never touched.
 *       <p><b>No in-progress guard.</b> An invalid {@code _ccnew} is also what a REINDEX CONCURRENTLY that is still
 *       running looks like, and nothing here reads {@code pg_stat_progress_create_index}. None is needed: the running
 *       reindex holds a {@code ShareUpdateExclusiveLock} on the leaf for its whole run, and
 *       {@code DROP INDEX CONCURRENTLY} needs the same lock on the leaf first, so the drop waits and ends at the
 *       drop's {@code lock_timeout} (5 s), the index stays, and the next pass judges it again. A leftover whose
 *       reindex has ended takes the lock at once and drops.</li>
 *   <li>RDR-230 (nexus-4lnn1.1.10): the same carve-out for the leaf's own HNSW index. An invalid
 *       {@code <own index>_ccnew}/{@code _ccold[N]} in {@link Leaf#leafLeftovers()} (an unpredicated hnsw index of a
 *       leaf the builder resolved, named from the leaf's identified index; see {@link PciCatalog.LeafIndex}) is dropped
 *       as {@link DropReason#LEAF_REINDEX_LEFTOVER}, with a null collection, through the same executor and drop path
 *       as a {@code pci_} leftover. It is never charged against the cap and never blocks a build.</li>
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
     * @param collectionId   {@code catalog_collections.collection_id} (RDR-229 P3.1), or null when the caller did not read it:
     *                       the key of the id-predicate indexes ({@link #planWithIds}); a row with none is never built by id
     */
    public record RegistryRow(String name, String lifecycleState, String supersededBy, Long collectionId) {
        public RegistryRow {
            Objects.requireNonNull(name, "name");
        }

        /** A row read without its id: it plans name indexes only. */
        public RegistryRow(String name, String lifecycleState, String supersededBy) {
            this(name, lifecycleState, supersededBy, null);
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
        SUPERSEDED("superseded"),
        /**
         * An invalid {@code _ccnew}/{@code _ccold} left by a failed REINDEX CONCURRENTLY of a builder index
         * ({@link Index#leftover()}), or, RDR-229 P3.4, of an id-predicate index ({@link Index#leftoverOfId()}); dropped whatever the registry
         * and the counts say (nexus-rn1yd).
         */
        REINDEX_LEFTOVER("reindex_leftover"),
        /**
         * An invalid {@code _ccnew}/{@code _ccold} left by a failed REINDEX CONCURRENTLY of a leaf's own HNSW index
         * ({@link Index#leafLeftover()}); dropped through the same path as {@link #REINDEX_LEFTOVER} (RDR-230,
         * nexus-4lnn1.1.10).
         */
        LEAF_REINDEX_LEFTOVER("leaf_reindex_leftover"),
        /** RDR-229 P3.4: the registry has no row with this id (a rename minted a new one, or the row went); {@link #NOT_IN_REGISTRY}'s id counterpart. */
        ID_NOT_IN_REGISTRY("id_not_in_registry"),
        /** RDR-229 P3.4: the registry row of this id has {@code superseded_by} set; {@link #SUPERSEDED}'s id counterpart. */
        ID_SUPERSEDED("id_superseded"),
        /** RDR-229 P3.4: {@code count * 2 < B} for the collection of this id; {@link #BELOW_HALF_BUILD_THRESHOLD}'s id counterpart. */
        ID_BELOW_HALF_BUILD_THRESHOLD("id_below_half_build_threshold"),
        /**
         * RDR-229 P3.4 (linda, 2026-11-22): a name-predicate index whose collection's id-predicate index is VALID, dropped once gate 3 holds, so
         * the doubling the id budget allowed ends. Never dropped before gate 3, and never while the id index is INVALID or in flight.
         */
        REPLACED_BY_ID("replaced_by_id");

        private final String label;

        DropReason(String label) {
            this.label = label;
        }

        public String label() {
            return label;
        }
    }

    /** One step of a plan. */
    public sealed interface Action permits Build, Drop, SkipCountSuspect, Reindex { }

    /**
     * Why an index is reindexed (RDR-230 Technical Design 3); {@link #label()} is the value for the log line. The
     * thresholds are {@link PciReindexPolicy}'s.
     */
    public enum Trigger {
        /** Bytes per row grew to {@link PciReindexPolicy#SIZE_RATIO} of the anchor's (or of the install reference). */
        SIZE("size"),
        /** Deletes since build reached {@link PciReindexPolicy#DELETES_SINCE_BUILD_FRACTION} of the rows. */
        DELETES_SINCE_BUILD("deletes_since_build"),
        /** The row count fell by {@link PciReindexPolicy#BULK_DELETE_FRACTION} since the previous check: REINDEX, then VACUUM. */
        BULK_DELETE("bulk_delete"),
        /** The recall canary is red on at least {@link PciReindexPolicy#RED_CANARY_MIN_QUERIES} queries. */
        RED_CANARY("red_canary"),
        /** A leaf's own HNSW index grew to {@link PciReindexPolicy#LEAF_SIZE_RATIO} of the reference (RDR-230 P2.3). */
        LEAF_SIZE("leaf_size");

        private final String label;

        Trigger(String label) {
            this.label = label;
        }

        public String label() {
            return label;
        }
    }

    /**
     * {@code REINDEX INDEX CONCURRENTLY} of the existing index {@code name}, which serves {@code collection} (null for
     * a leaf's own HNSW index, which serves none). {@code bytes} is the index's size when it was read: the order of the
     * pass's REINDEXes is largest first. After the REINDEX the executor writes a fresh anchor and re-measures the
     * canary; after a {@link Trigger#BULK_DELETE} REINDEX it also runs {@code VACUUM (ANALYZE)} on the index's leaf
     * ({@link #vacuumAfter()}).
     */
    public record Reindex(String name, String collection, Trigger trigger, long bytes) implements Action {
        public Reindex {
            Objects.requireNonNull(name, "name");
            Objects.requireNonNull(trigger, "trigger");
        }

        /** True when the leaf is vacuumed after the REINDEX: only the bulk-delete trigger. */
        public boolean vacuumAfter() {
            return trigger == Trigger.BULK_DELETE;
        }
    }

    /**
     * Build {@code name} on the leaf for {@code collection}; {@code name} is {@link PciCatalog#indexName}, or, when
     * {@code collectionId} is set (RDR-229 P3.4), {@link PciCatalog#indexNameById} and the index is on
     * {@code WHERE collection_id = <collectionId>}.
     */
    public record Build(String name, String collection, Long collectionId) implements Action {
        /** A build on the name predicate. */
        public Build(String name, String collection) {
            this(name, collection, null);
        }

        /** True for a build on the id predicate. */
        public boolean byId() {
            return collectionId != null;
        }
    }

    /**
     * Drop the existing index {@code name}, which serves (or, for a leftover, was made from) {@code collection};
     * {@code collection} is null for a {@link DropReason#LEAF_REINDEX_LEFTOVER}, which belongs to the leaf's own index.
     */
    public record Drop(String name, String collection, DropReason reason) implements Action { }

    /**
     * The count for this populated, live, unsuperseded collection was zero: a counting failure, never acted on. The
     * caller logs {@code event=pci_count_zero_indexed}.
     */
    public record SkipCountSuspect(String collection) implements Action { }

    /**
     * The reads the id half makes against the database, as the reconciler's counting transaction owns them: injected so this class stays a
     * function of its arguments.
     */
    public interface IdReads {
        /**
         * Of the {@code candidates} (the ranked collections within the id budget, so never more than the leaf's cap), those that still have
         * a row on this leaf, for this tenant and model, whose {@code collection_id} is null: stamp-before-build, so none of them gets an id
         * build yet. A read that fails or times out answers every candidate (fail closed): it holds back the id builds and nothing else.
         */
        Set<String> nullIdCollections(Collection<RegistryRow> candidates);

        /**
         * The rows of {@code collections} counted up to a higher cap than {@code count} was, for ranking: the name planner's recount, so that
         * when more candidates qualify than the id budget admits the largest are kept and not the alphabetically first of a tie at B. A name
         * the answer omits keeps its capped count.
         */
        default Map<String, Integer> recount(Collection<String> collections) {
            return Map.of();
        }

        /** Reads that find no null id and recount nothing. */
        IdReads NONE = candidates -> Set.of();
    }

    /**
     * What the id half of a pass needs beyond the name half (RDR-229 P3.4).
     *
     * @param gate2   gate 2 of the completion check holds: both swaps and every validation move are complete, so a chunk-family
     *                {@code on_complete} cannot end an id build. Until it does no id build is planned.
     * @param reads   the database reads of the id half ({@link IdReads}); asked only once gate 2 holds
     * @param gate3   gate 3 of the completion check holds: the name family retires. No name index is built, and each VALID name index whose
     *                collection's id index is VALID is dropped as {@link DropReason#REPLACED_BY_ID}. Gate 3 is computed from the database and
     *                counts a valid id index covered whether or not its name index is gone, so these drops cannot make it false again.
     */
    public record IdPlanInputs(boolean gate2, IdReads reads, boolean gate3) {
        public IdPlanInputs {
            Objects.requireNonNull(reads, "reads");
        }

        /** Fixed null-id collections and no recount; gate 3 does not hold. */
        public IdPlanInputs(boolean gate2, Set<String> nullIdCollections) {
            this(gate2, nullIdCollections, false);
        }

        /** Fixed null-id collections and no recount. */
        public IdPlanInputs(boolean gate2, Set<String> nullIdCollections, boolean gate3) {
            this(gate2, (IdReads) candidates -> {
                Set<String> out = new HashSet<>();
                candidates.forEach(c -> { if (nullIdCollections.contains(c.name())) out.add(c.name()); });
                return out;
            }, gate3);
        }

        /** No id build: gate 2 does not hold. */
        public static final IdPlanInputs NONE = new IdPlanInputs(false, Set.of());
    }

    /** A leaf's plan with its id half: every {@link Drop} (name, id, then replaced), every {@link SkipCountSuspect}, every {@link Build} (name, then id). */
    public record Plan(List<Action> actions) { }

    /** The backoff key of an id build, beside the collection names the name builds use: no name can start with {@code #}-then-{@code id:}. */
    public static String idBackoffKey(long collectionId) {
        return "#id:" + collectionId;
    }

    /**
     * {@link #plan} for the name half plus, for the id predicate (RDR-229 P3.4, residual S-2): the same rules keyed by
     * {@code collection_id}. {@link #plan} itself never reads an id index.
     *
     * <p><b>Transition.</b> The id index is built beside the name index and the name index is kept until the family retires. Before
     * the retire a name index leaves only by its own rules above. The retire ({@code ids.gate3()}, which the reconciler sets only when gate 3
     * has LATCHED and the engine's id readers are present, {@code IdReaders.PRESENT}) builds no name index and drops each VALID name index
     * whose live collection's id index is VALID, as {@link DropReason#REPLACED_BY_ID}; one whose id index is missing or INVALID keeps it. The id
     * indexes get a budget of their own, the same {@code NX_SEARCH_PCI_MAX_PER_LEAF}: until the retire ends it, a leaf at the cap holds the cap's
     * worth of name indexes and the cap's worth of id indexes (a stated exception to the cap, S-2's second option), with one build in flight at a
     * time because the builder is serial.
     *
     * <p><b>Id rules.</b> Build a collection whose registry row has an id, is live, not superseded, has no valid or in-flight id index, is not
     * backed off ({@link #idBackoffKey}) and is within the budget (largest first, then name), only when {@code ids.gate2()} and the collection has
     * no null-id row. It qualifies at B rows; or, while it still has a name index that this plan keeps, at any count the name plan keeps that
     * index for (B/2 and up, and zero, the counting-failure guard): gate 3 requires an id index for every collection with a VALID name index, so
     * an empty or half-full one must get its own. A valid id index drops with {@link DropReason#ID_NOT_IN_REGISTRY},
     * {@link DropReason#ID_SUPERSEDED} or {@link DropReason#ID_BELOW_HALF_BUILD_THRESHOLD}; an invalid one that is not in flight, as
     * {@link DropReason#FAILED_BUILD}, with the rebuild in the same plan. A live indexed collection that counts zero is a
     * {@link SkipCountSuspect} (when it has no name index to raise it), never a drop.
     */
    public static Plan planWithIds(Leaf leaf, Map<String, Integer> counts, Collection<RegistryRow> registry,
                                   Set<String> backedOff, String inFlightIndex, PciSettings settings, IdPlanInputs ids) {
        Objects.requireNonNull(ids, "ids");
        List<Action> names = plan(leaf, counts, registry, backedOff, inFlightIndex, settings);
        if (ids.gate3()) {
            // The name family is retired: nothing builds a name index again (the REPLACED_BY_ID drop below would otherwise be undone next pass).
            names = names.stream().filter(a -> !(a instanceof Build)).toList();
        }
        if (!settings.enabled() || leaf.model() == null || leaf.tenant() == null) {
            return new Plan(names);
        }
        long b = settings.buildMinRows();
        Map<Long, RegistryRow> byId = new HashMap<>();
        for (RegistryRow row : registry) {
            if (row.collectionId() != null) {
                byId.put(row.collectionId(), row);
            }
        }
        Set<String> nameIndexed = new HashSet<>();
        for (Index index : leaf.indexes()) {
            if (index.parsed()) {
                nameIndexed.add(index.collection());
            }
        }
        TreeMap<String, Drop> drops = new TreeMap<>();
        TreeSet<String> suspects = new TreeSet<>();
        Set<Long> served = new HashSet<>();
        Set<Long> validKept = new HashSet<>();
        int kept = 0;
        for (Index index : leaf.indexes()) {
            if (!index.idParsed()) {
                continue;
            }
            long id = index.collectionId();
            RegistryRow row = byId.get(id);
            String collection = row == null ? null : row.name();
            if (!index.valid()) {
                if (index.name().equals(inFlightIndex)) {
                    served.add(id);
                    kept++;
                } else {
                    drops.put(index.name(), new Drop(index.name(), collection, DropReason.FAILED_BUILD));
                }
                continue;
            }
            long count = collection == null ? 0 : counts.getOrDefault(collection, 0);
            if (row == null) {
                drops.put(index.name(), new Drop(index.name(), null, DropReason.ID_NOT_IN_REGISTRY));
                continue;
            }
            if (row.superseded()) {
                drops.put(index.name(), new Drop(index.name(), collection, DropReason.ID_SUPERSEDED));
                continue;
            }
            if (row.live() && count == 0) {
                if (!nameIndexed.contains(collection)) {
                    suspects.add(collection);
                }
            } else if (count * 2 < b) {
                drops.put(index.name(), new Drop(index.name(), collection, DropReason.ID_BELOW_HALF_BUILD_THRESHOLD));
                continue;
            }
            served.add(id);
            validKept.add(id);
            kept++;
        }
        TreeMap<String, Drop> replaced = new TreeMap<>();
        if (ids.gate3()) {
            Set<String> alreadyDropped = new HashSet<>();
            names.stream().filter(a -> a instanceof Drop).forEach(a -> alreadyDropped.add(((Drop) a).name()));
            Map<String, RegistryRow> byName = new HashMap<>();
            registry.forEach(r -> byName.put(r.name(), r));
            for (Index index : leaf.indexes()) {
                if (!index.parsed() || !index.valid() || alreadyDropped.contains(index.name())) {
                    continue;
                }
                RegistryRow row = byName.get(index.collection());
                if (row != null && row.collectionId() != null && row.live() && !row.superseded()
                    && validKept.contains(row.collectionId())) {
                    replaced.put(index.name(), new Drop(index.name(), index.collection(), DropReason.REPLACED_BY_ID));
                }
            }
        }
        List<Build> builds = new ArrayList<>();
        int slots = settings.maxPerLeaf() - kept;
        if (slots > 0) {
            // A candidate is a live, unsuperseded collection with an id, no valid or in-flight id index and no backoff (a backed-off one is held
            // back from BUILDING only: gate 3 reads the database and still requires its id index). It qualifies at B rows, or, while it has a name
            // index the name plan keeps (count at least B/2, or zero: a live collection that counts zero is a counting failure, never a drop), so
            // that gate 3, which needs an id index for every collection with a VALID name index, is reachable. An empty partial index is a few pages.
            List<RegistryRow> candidates = registry.stream()
                .filter(row -> row.collectionId() != null && row.live() && !row.superseded())
                .filter(row -> !served.contains(row.collectionId()) && !backedOff.contains(idBackoffKey(row.collectionId())))
                .filter(row -> {
                    int n = counts.getOrDefault(row.name(), 0);
                    return n >= b || (nameIndexed.contains(row.name()) && (n == 0 || (long) n * 2 >= b));
                })
                .toList();
            Map<String, Integer> ranked = counts;
            if (ids.gate2() && candidates.size() > slots) {
                // The counts stop at B + 1, so every big collection ties; recount the candidates higher, as the name planner does.
                Map<String, Integer> more = ids.reads().recount(candidates.stream().map(RegistryRow::name).toList());
                if (!more.isEmpty()) {
                    ranked = new HashMap<>(counts);
                    ranked.putAll(more);
                }
            }
            Map<String, Integer> byCount = ranked;
            List<RegistryRow> within = candidates.stream()
                .sorted(Comparator.<RegistryRow>comparingInt(row -> byCount.getOrDefault(row.name(), 0)).reversed()
                    .thenComparing(RegistryRow::name))
                .limit(slots)
                .toList();
            if (ids.gate2() && !within.isEmpty()) {
                Set<String> unstamped = ids.reads().nullIdCollections(within);
                for (RegistryRow row : within) {
                    if (!unstamped.contains(row.name())) {
                        builds.add(new Build(PciCatalog.indexNameById(leaf.model(), leaf.tenant(), row.collectionId()),
                            row.name(), row.collectionId()));
                    }
                }
            }
        }
        List<Action> merged = new ArrayList<>();
        names.stream().filter(a -> a instanceof Drop).forEach(merged::add);
        merged.addAll(drops.values());
        merged.addAll(replaced.values());
        names.stream().filter(a -> a instanceof SkipCountSuspect).forEach(merged::add);
        suspects.forEach(c -> merged.add(new SkipCountSuspect(c)));
        names.stream().filter(a -> a instanceof Build).forEach(merged::add);
        merged.addAll(builds);
        return new Plan(List.copyOf(merged));
    }

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
            // A leftover is invalid by construction (PciCatalog.Index refuses a valid one); the check is the second
            // line, so that a valid index can never be planned for a drop on the strength of a leftover claim.
            if (index.leftover() && !index.valid()) {
                // Not parsed in the routing sense: no served.add, no kept++, no cap slot, no build block.
                drops.put(index.name(), new Drop(index.name(), index.leftoverOf(), DropReason.REINDEX_LEFTOVER));
                continue;
            }
            if (!index.parsed()) {
                continue;
            }
            String collection = index.collection();
            if (!index.valid()) {
                if (index.name().equals(inFlightIndex)) {
                    served.add(collection);
                    kept++;
                } else {
                    // nexus-f3yqd (F): not served. The drop frees the name and the slot, so this plan may build the
                    // collection again; the executor skips that build when the drop did not happen.
                    drops.put(index.name(), new Drop(index.name(), collection, DropReason.FAILED_BUILD));
                }
                continue;
            }
            served.add(collection);
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

        // RDR-230 (nexus-4lnn1.1.10): a leftover of the leaf's own HNSW index. Like a pci_ leftover it is no cap slot, no
        // served collection and no build block, so it only adds a drop. Its collection is null: it serves none.
        for (Index leftover : leaf.leafLeftovers()) {
            if (leftover.leafLeftover() && !leftover.valid()) {
                drops.put(leftover.name(), new Drop(leftover.name(), null, DropReason.LEAF_REINDEX_LEFTOVER));
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

    /**
     * The REINDEXes the triggers call for, largest index first (then name): one per candidate whose
     * {@link PciReindexPolicy#trigger} fires. Not limited: the caller adds any leaf-index REINDEX and then
     * {@link #firstPerPass}. A pure function of its arguments.
     */
    static List<Reindex> reindexes(Collection<PciReindexPolicy.Candidate> candidates, java.time.Instant now) {
        List<Reindex> out = new ArrayList<>();
        for (PciReindexPolicy.Candidate c : candidates) {
            PciReindexPolicy.trigger(c, now)
                .ifPresent(t -> out.add(new Reindex(c.name(), c.collection(), t, c.bytes())));
        }
        out.sort(BY_SIZE_THEN_NAME);
        return List.copyOf(out);
    }

    /** Largest first, then name: the order a pass runs its REINDEXes in. */
    static final Comparator<Reindex> BY_SIZE_THEN_NAME =
        Comparator.comparingLong(Reindex::bytes).reversed().thenComparing(Reindex::name);

    /** The first {@link PciReindexPolicy#MAX_PER_PASS} of {@code ordered}, which is already in pass order. */
    static List<Reindex> firstPerPass(List<Reindex> ordered) {
        return List.copyOf(ordered.subList(0, Math.min(ordered.size(), PciReindexPolicy.MAX_PER_PASS)));
    }
}
