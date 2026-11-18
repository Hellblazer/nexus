// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.Table;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;

import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_COVERAGE_COUNTS_1024;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_COVERAGE_COUNTS_384;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_COVERAGE_COUNTS_768;

/**
 * RDR-134 Step 2 (bead nexus-7fku2.1.2): the per-collection topic coverage the routed search gates on, read once
 * from {@code nexus.taxonomy_coverage_counts_<dim>} (taxonomy-022; set-based bodies in taxonomy-023) and cached.
 *
 * <p><b>Coverage</b> is {@code 1 - unassigned / chunks} over the collection's live chunks. A collection is routed only
 * when {@link Counts#routable} holds: it has centroids, it has chunks, and its coverage is at least
 * {@link #ROUTE_MIN_COVERAGE} (the default; a request may set another, {@code topic_route.min_coverage}). A collection
 * with no chunks has no coverage (never 1) and an orphaned taxonomy (topics, no chunks) is therefore never routed.
 *
 * <p>The count ignores {@code embedding_model} and counts sensitive chunks, so its ratio can read lower than what the
 * search sees (a sensitive chunk the search excludes, a chunk of another model that the search does not reach): a
 * collection can be refused or fall back that a search-side ratio would have routed, never routed wrongly.
 *
 * <p><b>Invalidation: option (a), the engine invalidates on a store.</b> The engine cannot see which assignments
 * the client deferred or lost ({@code taxonomy_deferral()} and the lost-chunk tripwire are client state), so it does
 * not try. A route that stores chunks, or changes an input of the count (the manifest that makes a chunk live, a
 * document's tombstone, an assignment, a topic, a centroid), drops the affected entries through {@code
 * CoverageInvalidation}, and the next read recounts. A store whose assignment never happened or failed is then counted
 * as what it is, unassigned chunks, with no signalling and no failure path. Option (b), the client telling the engine,
 * would add a call that can itself be lost, and a lost signal is a stale high reading that hides chunks.
 *
 * <p><b>Which writers drop, and which do not.</b> Dropping: {@code VectorHandler}'s {@code /upsert-chunks} and the two
 * restore routes ({@code /gc/restore-rereferenced}, {@code /gc/quarantine-restore}); the {@code TaxonomyHandler} routes
 * that write topics, assignments or centroids; the {@code CatalogHandler} routes that write manifest rows, documents or
 * collections; and the two catalog background jobs (reconcile commit, manifest fill), which drop the tenant when their
 * body ends. {@code /store-delete} is not one of them: it deletes only chunks no manifest row names, which are in
 * neither count. NOT dropping, and so covered by the ten-minute ceiling alone: {@code ChashHandler}'s
 * {@code /rename_collection}, the tenant import commit ({@code TenantImportHandler}), the re-embed jobs, the engine's
 * own writers (the reaper, gc expiry). The test-only tenant reset evicts the tenant ({@link #evictTenant}). A caller that does not know the collection a
 * write touched drops the whole tenant: over-invalidating costs one recount, under-invalidating hides chunks.
 *
 * <p><b>Ceiling.</b> An entry older than {@link #DEFAULT_TTL} (ten minutes) is recounted whatever invalidation did,
 * which bounds the staleness a missed hook or an engine-internal writer can cause.
 *
 * <p><b>Invalidate versus an in-flight fill.</b> A read that started before a commit and finishes after its
 * invalidation must not cache what it counted. Each key's {@link Slot} carries a version that every invalidation
 * bumps; a fill notes the version before it counts and stores only if it is unchanged, both under the slot's monitor.
 * The drop runs when the route's repository call has returned (see {@code CoverageInvalidation} for the exact moment),
 * so a count that began after the commit sees the write, and one that began before the drop is discarded.
 *
 * <p><b>A failed lookup is not an error.</b> {@link #lookup} answers empty on any failure (an unregistered
 * collection, a pool or statement timeout) and caches nothing, so the caller searches unscoped instead of failing
 * the request, and the next request tries again.
 *
 * <p><b>One engine per database</b>, as {@link CollectionRegistry}: an invalidation reaches only this JVM, and
 * {@link EngineSingletonLock} keeps a second engine off the database. <b>Bounds:</b> one small entry per
 * (tenant, collection) read through here, capped at {@link #MAX_ENTRIES} (the map is cleared on overflow; a clear
 * never serves a stale value, it only costs recounts). The count itself runs on one pooled connection under
 * {@link PgSession#setTaxonomyAssignBounds} and scans the collection once; it is the read to avoid repeating,
 * which is what the cache is for.
 */
public final class TaxonomyCoverage {

    private static final Logger log = LoggerFactory.getLogger(TaxonomyCoverage.class);

    /** A collection is routed at or above this coverage (RDR-134; Step 5a checks the value). */
    public static final double ROUTE_MIN_COVERAGE = 0.9;
    /** The ceiling on how long a counted value is served, whatever invalidation did. */
    public static final Duration DEFAULT_TTL = Duration.ofMinutes(10);
    /** Entries kept; on overflow the map is cleared. */
    public static final int MAX_ENTRIES = 10_000;

    /**
     * One reading. {@code chunks} and {@code unassigned} are 0 when {@code hasTaxonomy} is false: the function does not
     * scan a collection that has no centroids, so they mean "not measured" there, not "empty".
     */
    public record Counts(boolean hasTaxonomy, long chunks, long unassigned) {

        /** {@code 1 - unassigned / chunks}; 0 when there are no chunks (no coverage, never 1). */
        public double coverage() {
            return chunks <= 0 ? 0.0 : (double) (chunks - unassigned) / chunks;
        }

        /** Centroids exist, chunks exist, and the coverage is at least {@code minCoverage}. */
        public boolean routable(double minCoverage) {
            return hasTaxonomy && chunks > 0 && coverage() >= minCoverage;
        }

        /** {@link #routable(double)} at {@link #ROUTE_MIN_COVERAGE}. */
        public boolean routable() {
            return routable(ROUTE_MIN_COVERAGE);
        }
    }

    private record Key(String tenant, String collection) {}

    private record Entry(Counts counts, Instant computedAt) {}

    /** One key's cached value and its invalidation version, both guarded by the slot's own monitor. */
    private static final class Slot {
        Entry entry;
        long version;
    }

    private final TenantScope tenantScope;
    private final Clock clock;
    private final Duration ttl;
    private final int maxEntries;
    private final ConcurrentHashMap<Key, Slot> slots = new ConcurrentHashMap<>();

    private volatile Runnable afterCountForTests;

    /** Test seam: runs between a fill's count and its store, so a test can land an invalidation in that window. Null clears it. */
    public void setAfterCountHookForTests(Runnable hook) {
        this.afterCountForTests = hook;
    }

    public TaxonomyCoverage(TenantScope tenantScope) {
        this(tenantScope, Clock.systemUTC(), DEFAULT_TTL, MAX_ENTRIES);
    }

    public TaxonomyCoverage(TenantScope tenantScope, Clock clock, Duration ttl, int maxEntries) {
        this.tenantScope = tenantScope;
        this.clock = clock;
        this.ttl = ttl;
        this.maxEntries = maxEntries;
    }

    /**
     * The collection's counts, from the cache while the entry is younger than the ceiling and no write has invalidated
     * it, else recounted. Empty when the count could not be taken (nothing is cached then): the caller searches the
     * collection unscoped.
     */
    public Optional<Counts> lookup(String tenant, String collection) {
        return lookup(tenant, collection, 0);
    }

    /**
     * As {@link #lookup(String, String)} with the count statement, if one has to run, bounded to at most
     * {@code maxStatementMs} ({@code 0}: the default bound). A cached reading is served whatever the bound. A count cut
     * short by it answers empty and caches nothing, like any other failed count, so a collection whose count never
     * fits the bound is never read from the cache (RDR-134 Step 3: the routed arm passes its share of the arm's
     * remaining budget, and degrades to the unscoped search on empty).
     */
    public Optional<Counts> lookup(String tenant, String collection, int maxStatementMs) {
        Key key = new Key(tenant, collection);
        Slot slot = slots.get(key);
        if (slot == null) {
            if (slots.size() >= maxEntries) {
                slots.clear();
            }
            slot = slots.computeIfAbsent(key, k -> new Slot());
        }
        long seen;
        synchronized (slot) {
            Entry hit = slot.entry;
            if (hit != null && clock.instant().isBefore(hit.computedAt().plus(ttl))) {
                return Optional.of(hit.counts());
            }
            seen = slot.version;
        }
        Counts counted;
        try {
            counted = count(tenant, collection, maxStatementMs);
        } catch (RuntimeException e) {
            log.warn("event=taxonomy_coverage_lookup_failed tenant={} collection={} error={}", tenant, collection,
                e.toString());
            return Optional.empty();
        }
        Runnable seam = afterCountForTests;
        if (seam != null) {
            seam.run();
        }
        synchronized (slot) {
            if (slot.version == seen) {
                slot.entry = new Entry(counted, clock.instant());
            }
        }
        return Optional.of(counted);
    }

    /**
     * A write to {@code collection} committed: its next {@link #lookup} recounts, and a count already in flight does
     * not cache. Other collections are untouched. Call it AFTER the write's transaction commits.
     */
    public void invalidate(String tenant, String collection) {
        Slot slot = slots.get(new Key(tenant, collection));
        if (slot != null) {
            synchronized (slot) {
                slot.version++;
                slot.entry = null;
            }
        }
    }

    /** {@link #invalidate} for every collection of {@code tenant}: for a write that does not name its collection. */
    public void invalidateTenant(String tenant) {
        slots.forEach((key, slot) -> {
            if (key.tenant().equals(tenant)) {
                synchronized (slot) {
                    slot.version++;
                    slot.entry = null;
                }
            }
        });
    }

    /**
     * Forget every entry of {@code tenant} (a tenant reset or delete, through {@code NexusService.tenantKeyedState()}):
     * the slots go, not just their values, so the cache holds nothing that names the tenant. A fill in flight keeps its
     * detached slot and stores into it, where nothing reads it. Returns the entries removed.
     */
    public int evictTenant(String tenant) {
        int before = slots.size();
        slots.keySet().removeIf(key -> key.tenant().equals(tenant));
        return Math.max(0, before - slots.size());
    }

    private Counts count(String tenant, String collection, int maxStatementMs) {
        int dim = CollectionRegistry.lookup(tenantScope, tenant, collection).dimension();
        return tenantScope.withTenant(tenant, ctx -> {
            // The function takes no limit, so the read is bounded here, as TaxonomyRepository#unassignedChashes does.
            if (maxStatementMs > 0) {
                PgSession.setTaxonomyReadBounds(ctx, maxStatementMs);
            } else {
                PgSession.setTaxonomyAssignBounds(ctx);
            }
            // Not named `fn`: HnswServingGucParityTest anchors on that name for a vector-ranked fetch, and this is a
            // plain count with no distance operator.
            Table<?> countsFn = switch (dim) {
                case 384 -> TAXONOMY_COVERAGE_COUNTS_384.call(collection);
                case 768 -> TAXONOMY_COVERAGE_COUNTS_768.call(collection);
                case 1024 -> TAXONOMY_COVERAGE_COUNTS_1024.call(collection);
                default -> throw new IllegalArgumentException("unsupported dim " + dim);
            };
            var rec = ctx.selectFrom(countsFn).fetchOne();
            if (rec == null) {
                throw new IllegalStateException("taxonomy_coverage_counts returned no row");
            }
            return new Counts(Boolean.TRUE.equals(rec.get("has_taxonomy", Boolean.class)),
                orZero(rec.get("chunks", Long.class)), orZero(rec.get("unassigned", Long.class)));
        });
    }

    private static long orZero(Long v) {
        return v == null ? 0L : v;
    }
}
