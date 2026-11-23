// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.Condition;
import org.jooq.DSLContext;

import java.util.ArrayList;
import java.util.Collection;
import java.util.HashSet;
import java.util.List;
import java.util.Optional;
import java.util.OptionalLong;
import java.util.Set;
import java.util.TreeSet;
import java.util.function.Supplier;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23): the registry reads of the taxonomy writers that are keyed by a TOPIC (or by a label prefix) and not by a
 * collection the request names: update, rename, review, delete, merge and split of a topic, the purge of a document's assignments, the prune
 * of projections. Moved out of {@code TaxonomyRepository} (at its size ceiling) as the area this change rewrites.
 *
 * <p><b>The rule (linda's ruling, Sam's (b)).</b> A writer of a collection-scoped table reads the registry row of every collection it writes
 * under {@code FOR KEY SHARE}, in its own write transaction, before the first write, and writes under the locked row's identity: the id the
 * row carries, never the label a name column bears (after a one-row rename that label may be another collection's). Canonical order: the
 * sweep gate, the registry rows in {@code collection_id} order, then the child rows.
 *
 * <p><b>Which rows.</b> A topic is filed under one collection ({@code topics.collection_id}); its assignments are filed under their own
 * {@code source_collection_id}, which for a projection is ANOTHER collection. A write that changes or deletes a topic's assignments
 * (a delete cascades them, a merge moves and re-points them, a split deletes the parent's) reads the topic's collection and every source
 * collection of the assignments it reaches, all in one id-ordered statement. A row the P3.1 stamp has not reached (no id) is read by its
 * label, which a missing row does not refuse (the leftovers of a deleted collection are still the caller's to clear).
 *
 * <p>The sets are read WITHOUT a lock first, because the lock names what to lock. After the lock they are read again; a collection a
 * concurrent writer added in between is locked in a further statement ({@link #STABLE_PASSES} passes). The reads are TOLERANT
 * ({@link RegistryRowLocks#keyShareIfPresent}): a child row stamped to a collection that is retired or gone is not refused (no 409, no
 * 404, as before these reads existed), because supersede does not re-home children. The collection the REQUEST names stays refused where
 * it was.
 */
final class TaxonomyRegistryReads {

    /** How many times the lock set is re-read after locking before it is taken as stable. */
    private static final int STABLE_PASSES = 4;

    private TaxonomyRegistryReads() {
    }

    /** The collection ids and the labels (of rows no id has reached) a write must read. */
    record Names(Set<Long> ids, Set<String> labels) {
        static Names empty() {
            return new Names(new TreeSet<>(), new TreeSet<>());
        }

        void add(Long id, String label) {
            if (id != null) {
                ids.add(id);
            } else if (label != null && !label.isBlank()) {
                labels.add(label);
            }
        }
    }

    /**
     * The collection a topic is filed under and the registry row the transaction holds for it. {@code row} is empty for a topic filed
     * under no collection, or an unstamped one whose label has no row (a deleted collection's leftover).
     */
    record TopicHome(Long collectionId, String label, Optional<CollectionRow> row) {

        /** The {@code topics} rows of this home: the ones stamped with its id, else (unstamped) those bearing its label. */
        Condition ownsTopic() {
            return collectionId != null ? TOPICS.COLLECTION_ID.eq(collectionId) : TOPICS.COLLECTION_ID.isNull().and(TOPICS.COLLECTION.eq(label));
        }

        /**
         * {@code topicColumn} names {@code topicId}, a topic of THIS home: a write of assignment rows by topic is confined to a topic filed under
         * the collection whose row the transaction holds, so it never acts under a registry row another collection's topic did not hold.
         */
        Condition holdsTopic(org.jooq.Field<Long> topicColumn, long topicId) {
            return topicColumn.in(org.jooq.impl.DSL.select(TOPICS.ID).from(TOPICS).where(TOPICS.ID.eq(topicId).and(ownsTopic())));
        }

        /** The id a row of this home keeps: its stored one, else the id the locked row gives an unstamped one; null when there is none. */
        Long idToKeep() {
            return collectionId != null ? collectionId : row.map(CollectionRow::collectionId).orElse(null);
        }

        /**
         * What the topic's {@code collection} column takes in an UPDATE: the locked row's CURRENT label (a one-row rename leaves the column at
         * the old one), else the column as it is (a topic in no collection, or whose label no row holds).
         */
        org.jooq.Field<String> nameToWrite() {
            return liveRow().<org.jooq.Field<String>>map(r -> org.jooq.impl.DSL.val(r.label())).orElse(TOPICS.COLLECTION);
        }

        /** The home's CURRENT label: the locked row's, else the name the topic bears. */
        String currentLabel() {
            return liveRow().map(CollectionRow::label).orElse(label);
        }

        /** The locked row when it is live: a retired row's label is a tombstone's, never what a topic is restamped to. */
        private Optional<CollectionRow> liveRow() {
            return row.filter(r -> !r.isSuperseded());
        }
    }

    private static Optional<TopicHome> homeOf(DSLContext ctx, long topicId) {
        var r = ctx.select(TOPICS.COLLECTION_ID, TOPICS.COLLECTION).from(TOPICS).where(TOPICS.ID.eq(topicId)).fetchOne();
        return r == null ? Optional.empty() : Optional.of(new TopicHome(r.value1(), r.value2(), Optional.empty()));
    }

    private static Names namesOf(DSLContext ctx, String tenant, boolean withSources, List<Long> topicIds) {
        Names names = Names.empty();
        for (long topicId : topicIds) {
            homeOf(ctx, topicId).ifPresent(h -> names.add(h.collectionId(), h.label()));
        }
        if (withSources) {
            for (var s : ctx.selectDistinct(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION)
                    .from(TOPIC_ASSIGNMENTS)
                    .where(TOPIC_ASSIGNMENTS.TENANT_ID.eq(tenant).and(TOPIC_ASSIGNMENTS.TOPIC_ID.in(topicIds)))
                    .fetch()) {
                names.add(s.value1(), s.value2());
            }
        }
        return names;
    }

    /**
     * Reads, {@code FOR KEY SHARE} in one id-ordered statement, the registry rows a write keyed by topic reaches, before anything is written:
     * the collection of each topic in {@code topicIds}, and with {@code withSources} the source collections of every assignment of those
     * topics. Returns the home of the FIRST topic (empty when it does not exist), its row as locked.
     *
     * A row the topic or an assignment is filed under that is retired or gone is not refused.
     */
    static Optional<TopicHome> registryFirstOfTopics(DSLContext ctx, String tenant, boolean withSources, List<Long> topicIds) {
        lockStable(ctx, tenant, () -> namesOf(ctx, tenant, withSources, topicIds));
        return homeOf(ctx, topicIds.get(0)).map(h -> {
            if (h.collectionId() != null) {
                // tolerant: a topic stamped to a retired or gone collection is still written, as before this read existed
                return new TopicHome(h.collectionId(), h.label(),
                    RegistryRowLocks.keyShareIfPresent(ctx, tenant, List.of(h.collectionId()), List.of()).stream().findFirst());
            }
            return new TopicHome(null, h.label(), h.label().isBlank() ? Optional.empty()
                : CollectionRegistry.requireForWriteIfPresent(ctx, tenant, h.label()));
        });
    }

    /**
     * Refuses ({@link IllegalArgumentException}, a 400) a topic that exists but is filed under another collection than {@code locked}, the
     * row the transaction holds: a write that names a collection and a topic acts on the topic only when it is that collection's. A topic that
     * does not exist is left to the write's own foreign key.
     */
    static void requireTopicIn(DSLContext ctx, long topicId, CollectionRow locked) {
        var home = homeOf(ctx, topicId);
        if (home.isEmpty()) {
            return;
        }
        boolean mine = home.get().collectionId() != null ? home.get().collectionId() == locked.collectionId()
            : home.get().label().equals(locked.label());
        if (!mine) {
            throw new IllegalArgumentException("topic " + topicId + " is not a topic of collection '" + locked.label() + "'");
        }
    }

    /** {@link #registryFirstOfTopics} for one topic whose assignments the write leaves alone (a label or review change). */
    static Optional<TopicHome> registryFirstOfTopic(DSLContext ctx, String tenant, long topicId) {
        return registryFirstOfTopics(ctx, tenant, false, List.of(topicId));
    }

    /**
     * The source collections (id, or the label of an unstamped row) of the assignments {@code assignments} selects; for a write that
     * deletes or re-points those rows.
     */
    static Names sourcesOf(DSLContext ctx, String tenant, Condition assignments) {
        Names names = Names.empty();
        for (var s : ctx.selectDistinct(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION)
                .from(TOPIC_ASSIGNMENTS).where(TOPIC_ASSIGNMENTS.TENANT_ID.eq(tenant).and(assignments)).fetch()) {
            names.add(s.value1(), s.value2());
        }
        return names;
    }

    /**
     * The registry check of a write on {@code own} (a label, tolerant: a label no row holds is the leftovers of a deleted collection) that
     * also reaches {@code others}' collections: the rows are read in {@code collection_id} order, those below {@code own}'s id, then
     * {@code own}, then the rest. Returns the row of {@code own}, empty when no row has the label. {@code own} may be null for a write with
     * no collection of its own.
     */
    static Optional<CollectionRow> registryFirstAround(DSLContext ctx, String tenant, String own, Supplier<Names> others) {
        return registryFirstAround(ctx, tenant, own, others, locked -> others.get());
    }

    /**
     * {@link #registryFirstAround(DSLContext, String, String, Supplier)} for a write whose other collections are named through the rows of
     * {@code own} (the topics of the collection): {@code beforeOwn} judges them from an unlocked guess, for ordering only, and {@code afterOwn}
     * judges them again from the row as locked, which is what the write itself acts on. A rename in the gap between the guess and the lock
     * (it changes the name column, which a key share does not stop) cannot leave the locked row's topics' sources unread.
     */
    static Optional<CollectionRow> registryFirstAround(DSLContext ctx, String tenant, String own, Supplier<Names> beforeOwn,
                                                       java.util.function.Function<Optional<CollectionRow>, Names> afterOwn) {
        Long ownId = own == null ? null : unlockedId(ctx, tenant, own);
        Set<Long> heldIds = new HashSet<>();
        Set<String> heldLabels = new HashSet<>();
        lockBelow(ctx, tenant, ownId, beforeOwn, heldIds);
        Optional<CollectionRow> row = own == null ? Optional.empty() : CollectionRegistry.requireForWriteIfPresent(ctx, tenant, own);
        row.ifPresent(r -> heldIds.add(r.collectionId()));
        lockRest(ctx, tenant, () -> afterOwn.apply(row), heldIds, heldLabels);
        return row;
    }

    /**
     * The first half of a write that takes an advisory lock before its registry rows ({@link TaxonomyCollectionLock}, whose callback runs
     * between the lock and the collection's own row): reads, in ONE statement in {@code collection_id} order, the rows {@code others} names
     * and the collection's own row (by its label), so the writer holds no row while it waits for a lower one. A collection the request named
     * by id keeps its own by-id read (it consumes the request's id), so the rows below {@code ownId} are read first and the rest after it
     * ({@link #registryFirstRest}): the same order in two runs. Returns the ids held.
     */
    static Set<Long> registryFirstBeforeOwn(DSLContext ctx, String tenant, String own, Long ownId, Supplier<Names> others) {
        Set<Long> held = new HashSet<>();
        if (ExpectedCollectionIds.named(own)) {
            lockBelow(ctx, tenant, ownId, others, held);
            return held;
        }
        Names n = others.get();
        List<String> labels = new ArrayList<>(n.labels());
        labels.add(own);
        RegistryRowLocks.keyShareIfPresent(ctx, tenant, n.ids(), labels);   // tolerant for the children's ids; own is refused by the lock that follows
        held.addAll(n.ids());
        return held;
    }

    /**
     * For a caller that took its own row first (a transaction that takes an advisory lock, then the row: {@link TaxonomyCollectionLock}):
     * reads the registry rows {@code others} names, other than {@code own}, in one id-ordered statement (re-read until stable). The ids
     * below {@code own}'s are then read after it, which {@link #registryFirstAround} avoids.
     */
    static void registryFirstRest(DSLContext ctx, String tenant, Supplier<Names> others, Set<Long> held, CollectionRow own) {
        Set<Long> heldIds = new HashSet<>(held);
        heldIds.add(own.collectionId());
        lockRest(ctx, tenant, others, heldIds, new HashSet<>());
    }

    private static void lockBelow(DSLContext ctx, String tenant, Long ownId, Supplier<Names> others, Set<Long> heldIds) {
        Names n = others.get();
        List<Long> below = new ArrayList<>();
        for (long id : n.ids()) {
            if (ownId == null || id < ownId) {
                below.add(id);
            }
        }
        if (!below.isEmpty()) {
            RegistryRowLocks.keyShareIfPresent(ctx, tenant, below, List.of());
            heldIds.addAll(below);
        }
    }

    /**
     * Locks, in passes, the ids and labels {@code others} names that are not held yet. The first pass reads every id in ONE statement in
     * {@code collection_id} order. A later pass exists only for a collection a concurrent writer added in between, which the first statement
     * could not name; it may take an id BELOW one already held. That is the one order inversion left: it can close a cycle with a verb that
     * holds the lower id and waits for the higher, the database detects it (40P01) and the transaction is retried from the top, so the
     * cost is a retry, never a hang. {@link #STABLE_PASSES} bounds the passes.
     */
    private static void lockRest(DSLContext ctx, String tenant, Supplier<Names> others, Set<Long> heldIds, Set<String> heldLabels) {
        for (int pass = 0; pass < STABLE_PASSES; pass++) {
            Names n = others.get();
            List<Long> ids = n.ids().stream().filter(i -> !heldIds.contains(i)).toList();
            List<String> labels = n.labels().stream().filter(l -> !heldLabels.contains(l)).toList();
            if (ids.isEmpty() && labels.isEmpty()) {
                return;
            }
            RegistryRowLocks.keyShareIfPresent(ctx, tenant, ids, labels);
            heldIds.addAll(ids);
            heldLabels.addAll(labels);
        }
    }

    /** One id-ordered read of {@code names()} (re-read until stable), for a write with no collection of its own. */
    private static void lockStable(DSLContext ctx, String tenant, Supplier<Names> names) {
        lockRest(ctx, tenant, names, new HashSet<>(), new HashSet<>());
    }

    /**
     * The id a label resolves to now, read without a lock: the id the request named it by, else the live row's (a tombstone's when no row is
     * live). Null for a blank label or one no row holds. Only for ordering: the row is read again under the lock.
     */
    private static Long unlockedId(DSLContext ctx, String tenant, String label) {
        if (label.isBlank()) {
            return null;
        }
        OptionalLong named = ExpectedCollectionIds.of(label);
        if (named.isPresent()) {
            return named.getAsLong();
        }
        return ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(label))
            .orderBy(CollectionRegistry.labelResolutionOrder()).limit(1)
            .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
    }

    /**
     * The {@code topics} rows {@code label} names, judged BEFORE its registry row is read (the read of the rows those topics reach needs
     * them): the rows of the id the label resolves to now (read without a lock) and those the stamp has not reached that bear the label.
     * The write itself uses the row it then holds.
     */
    static Condition topicsNamedBy(DSLContext ctx, String tenant, String label) {
        Long id = unlockedId(ctx, tenant, label);
        Condition unstamped = TOPICS.COLLECTION_ID.isNull().and(TOPICS.COLLECTION.eq(label));
        return id == null ? unstamped : TOPICS.COLLECTION_ID.eq(id).or(unstamped);
    }

    /**
     * The labels of every registry row whose name starts with {@code prefix}, read without a lock: the rows a prefix prune reads
     * {@code FOR KEY SHARE} next ({@link #registryFirstOfPrefix}).
     */
    private static List<String> labelsStarting(DSLContext ctx, String tenant, String prefix) {
        return ctx.selectDistinct(CATALOG_COLLECTIONS.NAME).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.startsWith(prefix)))
            .orderBy(CATALOG_COLLECTIONS.NAME).fetch(CATALOG_COLLECTIONS.NAME);
    }

    /**
     * The registry rows of every collection whose label starts with {@code prefix}, live or retired, read {@code FOR KEY SHARE} in one
     * id-ordered statement (a collection registered meanwhile is not among the rows the write reaches: it deletes by the held ids). Retired
     * rows are included, so a child stamped to a tombstone that bears the label is pruned as it was when the prefix matched the name column
     * (a tombstone's label is its own, so a row is reached only by a label that starts with the prefix; until P4.1 a tombstone keeps its label taken). Returns each row.
     */
    static List<CollectionRow> registryFirstOfPrefix(DSLContext ctx, String tenant, String prefix) {
        List<String> labels = labelsStarting(ctx, tenant, prefix);
        if (labels.isEmpty()) {
            return List.of();
        }
        return RegistryRowLocks.keyShareIfPresent(ctx, tenant, List.of(), labels);
    }

    /** Every collection id of {@code rows}, for a statement that deletes by them. */
    static Collection<Long> idsOf(Collection<CollectionRow> rows) {
        return rows.stream().map(CollectionRow::collectionId).distinct().toList();
    }
}
