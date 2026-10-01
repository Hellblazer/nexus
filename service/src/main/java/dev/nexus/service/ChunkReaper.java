// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.Rdr192BackfillGate;
import dev.nexus.service.db.Rdr192BackfillGate.BackfillIncompleteException;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.ReaperRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Function;
import java.util.function.Supplier;

/**
 * RDR-192 Step 9 (bead nexus-2x9xa): the state-derived reaper. A periodic pass, on {@code NexusService}'s
 * {@code sweepScheduler}, that moves chunks which nothing owns into the collection's quarantine sibling.
 *
 * <p><b>It recomputes from state, it keeps no drop set.</b> The post-commit sweep ({@code runSweepTransaction}) is
 * the fast path and keeps its lock and statement timeouts; when it fails open (a gate timeout, a statement
 * timeout, a permission error) the chunks it meant to drop are left behind, and nothing remembers them. The next
 * pass of this class finds them again from the manifest, because a chunk with no manifest row in any owner state,
 * past the grace window, is reapable by {@code nexus.chunk_is_reapable} (vectors-021), the one predicate every
 * engine consumer shares.
 *
 * <p><b>It quarantines, it never hard-deletes</b> (Sam, 2026-10-01). A moved chunk is restorable until
 * {@code gc_expire_quarantine}'s own 14 day clock and floors take it. <b>It covers every collection prefix</b>
 * ({@code knowledge__}, {@code docs__}, {@code code__}, {@code rdr__}): after RDR-223's ownerless-write refusal the
 * only producer of ownerless chunks is a crashed multi-request run, and those are mostly {@code docs__} and
 * {@code code__}.
 *
 * <p>The gates, in the order a pass meets them. Each refusal is counted and logged at WARN, never a quiet info line:
 * <ol>
 *   <li>per tenant, {@link Rdr192BackfillGate#requireComplete}: no verified backfill record, nothing is touched;</li>
 *   <li>per collection: a {@code quarantine-} name is skipped (a quarantine sibling is {@code gc_expire_quarantine}'s);
 *       a collection with no catalog row, or in any lifecycle state but {@code live}, is refused;</li>
 *   <li>per collection, a dry run counts what is reapable. Nothing reapable means nothing at risk and no census;</li>
 *   <li>per collection, the manifest-less census is re-run in the engine on every pass that would move something and
 *       must read {@code legacy-unmanifested == 0} and {@code unclassified == 0}. The stored ladder record is not
 *       enough: a tenant with an old record can later gain legacy-shaped chunks, and a live legacy note with no
 *       manifest row reads reapable once aged (matrix row R8);</li>
 *   <li>the move itself ({@code nexus.reaper_quarantine_chunks}, vectors-024): the exclusive sweep gate, the
 *       fraction floor ({@link Settings#floorFraction}, {@link Settings#floorMinChunks}), and the grace re-checked in
 *       the DELETE's own WHERE, so a client write that refreshes a chunk after it was chosen wins.</li>
 * </ol>
 *
 * <p>A pass moves at most {@link Settings#batchSize} (300) chunks per collection. The grace is the predicate's own
 * 30 days per chunk; this class has no setting for it. {@link #runOnce(Duration)} is package-private and takes the
 * grace so a test can pass zero.
 */
final class ChunkReaper {

    private static final Logger log = LoggerFactory.getLogger(ChunkReaper.class);

    /** The {@code actor} of the {@code gc_audit} row a pass that moves anything writes. */
    static final String ACTOR = "engine-reaper";

    static final String ENABLED_ENV = "NX_REAPER_ENABLED";
    static final String INTERVAL_SECONDS_ENV = "NX_REAPER_INTERVAL_SECONDS";
    static final String BATCH_SIZE_ENV = "NX_REAPER_BATCH_SIZE";
    static final String FLOOR_FRACTION_ENV = "NX_REAPER_FLOOR_FRACTION";
    static final String FLOOR_MIN_CHUNKS_ENV = "NX_REAPER_FLOOR_MIN_CHUNKS";
    static final String WALL_CLOCK_BUDGET_SECONDS_ENV = "NX_REAPER_WALL_CLOCK_BUDGET_SECONDS";

    /** Most chunks one pass moves from one collection (Sam, 2026-09-26: 300). */
    static final int MAX_BATCH_SIZE = 300;
    private static final String QUARANTINE_PREFIX = "quarantine-";
    private static final String LIVE = "live";
    /** Per-statement bound for the enumeration, the dry run and the move. */
    private static final int STATEMENT_TIMEOUT_MS = 25_000;
    private static final int LOCK_TIMEOUT_MS = 2_000;

    /**
     * The reaper's settings. Defaults are Sam's rulings (2026-09-26 and 2026-10-01): hourly, 300 chunks per
     * collection per pass, a floor of 0.25 from 100 reapable chunks up (the {@code NX_GC_FLOOR_FRACTION} semantics, applied
     * to the move). {@code enabled} is a kill switch, not a tuning knob. {@code wallClockBudget} bounds a whole run,
     * checked at tenant and collection boundaries only, because the reaper shares one scheduler thread with the T1
     * and tuple sweeps.
     */
    record Settings(boolean enabled, Duration interval, int batchSize, double floorFraction,
                    int floorMinChunks, Duration wallClockBudget) {

        static final Duration DEFAULT_INTERVAL = Duration.ofHours(1);
        static final double DEFAULT_FLOOR_FRACTION = 0.25;
        static final int DEFAULT_FLOOR_MIN_CHUNKS = 100;
        static final Duration DEFAULT_WALL_CLOCK_BUDGET = Duration.ofMinutes(10);

        static Settings defaults() {
            return new Settings(true, DEFAULT_INTERVAL, MAX_BATCH_SIZE, DEFAULT_FLOOR_FRACTION,
                                DEFAULT_FLOOR_MIN_CHUNKS, DEFAULT_WALL_CLOCK_BUDGET);
        }

        /**
         * Resolve from the environment. An invalid value falls back to the default with a WARN and never throws:
         * a typo must not stop the engine from booting, and for the floor in particular the default is the safe
         * direction (a nan or out-of-range value must not neutralize it).
         */
        static Settings fromEnv(Function<String, String> env) {
            boolean enabled = !isOff(env.apply(ENABLED_ENV));
            return new Settings(
                enabled,
                Duration.ofSeconds(positive(env, INTERVAL_SECONDS_ENV, DEFAULT_INTERVAL.toSeconds(), Long.MAX_VALUE / 4)),
                (int) positive(env, BATCH_SIZE_ENV, MAX_BATCH_SIZE, MAX_BATCH_SIZE),
                fraction(env, FLOOR_FRACTION_ENV, DEFAULT_FLOOR_FRACTION),
                (int) nonNegative(env, FLOOR_MIN_CHUNKS_ENV, DEFAULT_FLOOR_MIN_CHUNKS),
                Duration.ofSeconds(positive(env, WALL_CLOCK_BUDGET_SECONDS_ENV, DEFAULT_WALL_CLOCK_BUDGET.toSeconds(),
                                            Long.MAX_VALUE / 4)));
        }

        private static boolean isOff(String v) {
            return v != null && (v.trim().equalsIgnoreCase("false") || v.trim().equals("0")
                || v.trim().equalsIgnoreCase("off") || v.trim().equalsIgnoreCase("no"));
        }

        private static long positive(Function<String, String> env, String name, long dflt, long max) {
            String raw = env.apply(name);
            if (raw == null || raw.isBlank()) return dflt;
            try {
                long v = Long.parseLong(raw.trim());
                if (v >= 1 && v <= max) return v;
            } catch (NumberFormatException ignored) {
                // falls through to the warning
            }
            log.warn("event=reaper_setting_invalid name={} raw={} using={} expected=integer_in_1_{}",
                name, raw, dflt, max);
            return dflt;
        }

        private static long nonNegative(Function<String, String> env, String name, long dflt) {
            String raw = env.apply(name);
            if (raw == null || raw.isBlank()) return dflt;
            try {
                long v = Long.parseLong(raw.trim());
                if (v >= 0 && v <= Integer.MAX_VALUE) return v;
            } catch (NumberFormatException ignored) {
                // falls through to the warning
            }
            log.warn("event=reaper_setting_invalid name={} raw={} using={} expected=non_negative_integer",
                name, raw, dflt);
            return dflt;
        }

        private static double fraction(Function<String, String> env, String name, double dflt) {
            String raw = env.apply(name);
            if (raw == null || raw.isBlank()) return dflt;
            try {
                double v = Double.parseDouble(raw.trim());
                if (v >= 0.0 && v <= 1.0) return v;   // false for nan; excludes inf
            } catch (NumberFormatException ignored) {
                // falls through to the warning
            }
            log.warn("event=reaper_setting_invalid name={} raw={} using={} expected=fraction_in_0_1", name, raw, dflt);
            return dflt;
        }
    }

    /** Why a tenant or a collection was refused. Every one is counted and logged at WARN. */
    enum Refusal {
        BACKFILL_INCOMPLETE, UNREGISTERED_COLLECTION, COLLECTION_NOT_LIVE,
        CENSUS_LEGACY_UNMANIFESTED, CENSUS_UNCLASSIFIED, FLOOR_EXCEEDED, GATE_BUSY
    }

    /** One collection's outcome. {@code refusal} and {@code error} are null when absent. */
    record CollectionResult(String collection, long candidates, long total, long moved, Refusal refusal,
                            String error) {}

    record TenantResult(String tenant, List<CollectionResult> collections, Refusal tenantRefusal, String error) {
        CollectionResult collection(String name) {
            return collections.stream().filter(c -> c.collection().equals(name)).findFirst().orElse(null);
        }

        long candidates() {
            return collections.stream().mapToLong(CollectionResult::candidates).sum();
        }

        long moved() {
            return collections.stream().mapToLong(CollectionResult::moved).sum();
        }

        /** One gc_audit row per collection that moved anything. */
        int auditRows() {
            return (int) collections.stream().filter(c -> c.moved() > 0).count();
        }

        int refused() {
            return (int) collections.stream().filter(c -> c.refusal() != null).count()
                + (tenantRefusal != null ? 1 : 0);
        }

        int errors() {
            return (int) collections.stream().filter(c -> c.error() != null).count() + (error != null ? 1 : 0);
        }
    }

    record RunResult(List<TenantResult> tenants, boolean wallClockCut) {
        TenantResult tenant(String name) {
            return tenants.stream().filter(t -> t.tenant().equals(name)).findFirst().orElse(null);
        }
    }

    private final ReaperRepository store;
    private final PgVectorRepository vectors;
    private final Rdr192BackfillGate gate;
    private final Supplier<? extends Collection<String>> tenants;
    private final Settings settings;
    private final Clock clock;
    /** Refusals since boot: a counter a log reader can alert on, where a one-off line would scroll past. */
    private final AtomicLong refusedTotal = new AtomicLong();

    ChunkReaper(ReaperRepository store, PgVectorRepository vectors, Rdr192BackfillGate gate,
                Supplier<? extends Collection<String>> tenants, Settings settings, Clock clock) {
        this.store = store;
        this.vectors = vectors;
        this.gate = gate;
        this.tenants = tenants;
        this.settings = settings;
        this.clock = clock;
    }

    Settings settings() {
        return settings;
    }

    long refusedTotal() {
        return refusedTotal.get();
    }

    /** One scheduled pass: the predicate's own 30 day grace. */
    RunResult run() {
        return runOnce(null);
    }

    /**
     * One pass over every tenant, with {@code grace} injected: null is the predicate's default (what production
     * runs), anything else is for tests. Never throws: a failure is a counted, logged outcome.
     */
    RunResult runOnce(Duration grace) {
        long deadline = System.nanoTime() + settings.wallClockBudget().toNanos();
        List<TenantResult> results = new ArrayList<>();
        boolean cut = false;
        Collection<String> tenantIds;
        try {
            tenantIds = tenants.get();
        } catch (RuntimeException e) {
            log.warn("event=reaper_run tenants=0 candidates=0 moved=0 audit_rows=0 refused=0 errors=1 error={}",
                e.getMessage(), e);
            return new RunResult(List.of(), false);
        }
        for (String tenant : tenantIds) {
            if (System.nanoTime() >= deadline) {
                cut = true;
                break;
            }
            TenantResult r = passTenant(tenant, grace, deadline);
            results.add(r);
            if (r.error() == null && System.nanoTime() >= deadline) {
                cut = true;
            }
        }
        long candidates = results.stream().mapToLong(TenantResult::candidates).sum();
        long moved = results.stream().mapToLong(TenantResult::moved).sum();
        int auditRows = results.stream().mapToInt(TenantResult::auditRows).sum();
        int refused = results.stream().mapToInt(TenantResult::refused).sum();
        int errors = results.stream().mapToInt(TenantResult::errors).sum();
        log.info("event=reaper_run tenants={} candidates={} moved={} audit_rows={} refused={} errors={} wall_clock_cut={}",
            results.size(), candidates, moved, auditRows, refused, errors, cut);
        return new RunResult(results, cut);
    }

    private TenantResult passTenant(String tenant, Duration grace, long deadlineNanos) {
        try {
            gate.requireComplete(tenant);
        } catch (BackfillIncompleteException e) {
            long n = refusedTotal.incrementAndGet();
            log.warn("event=reaper_tenant_refused tenant={} reason={} refused_total={} detail={}",
                tenant, Refusal.BACKFILL_INCOMPLETE, n, e.getMessage());
            return logPass(new TenantResult(tenant, List.of(), Refusal.BACKFILL_INCOMPLETE, null));
        }

        List<CollectionResult> out = new ArrayList<>();
        String tenantError = null;
        try {
            List<String> names = store.collectionsWithChunks(tenant, Duration.ofMillis(STATEMENT_TIMEOUT_MS));
            Map<String, String> states = store.lifecycleStates(tenant);
            for (String name : names) {
                if (name.startsWith(QUARANTINE_PREFIX)) {
                    continue;   // gc_expire_quarantine's, with its own clock and floors
                }
                if (System.nanoTime() >= deadlineNanos) {
                    tenantError = "wall-clock budget spent before collection " + name + "; the rest wait for the next pass";
                    log.warn("event=reaper_wall_clock_cut tenant={} next_collection={}", tenant, name);
                    break;
                }
                out.add(passCollection(tenant, name, states, grace));
            }
        } catch (RuntimeException e) {
            tenantError = e.getMessage();
            log.warn("event=reaper_tenant_failed tenant={} error={}", tenant, e.getMessage(), e);
        }
        return logPass(new TenantResult(tenant, out, null, tenantError));
    }

    private CollectionResult passCollection(String tenant, String name, Map<String, String> states, Duration grace) {
        if (!states.containsKey(name)) {
            return refuse(tenant, name, Refusal.UNREGISTERED_COLLECTION, 0, 0, "no catalog_collections row");
        }
        String state = states.get(name);
        if (!LIVE.equals(state)) {
            return refuse(tenant, name, Refusal.COLLECTION_NOT_LIVE, 0, 0, "lifecycle_state=" + state);
        }
        try {
            ReaperRepository.Pass probe = store.probe(tenant, name, grace, STATEMENT_TIMEOUT_MS);
            if (probe.reapable() == 0) {
                return new CollectionResult(name, 0, probe.total(), 0, null, null);
            }

            // The census, in the engine, on every pass that would move something. The stored ladder record is not
            // enough: nothing revokes it, so a tenant that later gains legacy-shaped chunks keeps an open gate.
            var census = vectors.manifestLessCensus(tenant, name, 1, 0);
            long legacy = census.totals().getOrDefault("legacy-unmanifested", 0L);
            long unclassified = census.totals().getOrDefault("unclassified", 0L);
            if (legacy > 0) {
                return refuse(tenant, name, Refusal.CENSUS_LEGACY_UNMANIFESTED, probe.reapable(), probe.total(),
                    "legacy_unmanifested=" + legacy);
            }
            if (unclassified > 0) {
                return refuse(tenant, name, Refusal.CENSUS_UNCLASSIFIED, probe.reapable(), probe.total(),
                    "unclassified=" + unclassified);
            }

            ReaperRepository.Pass move = store.move(tenant, name, QUARANTINE_PREFIX + name,
                clock.instant().toString(), settings.batchSize(), grace, settings.floorFraction(),
                settings.floorMinChunks(), STATEMENT_TIMEOUT_MS, LOCK_TIMEOUT_MS);
            if (move.refused()) {
                return refuse(tenant, name, Refusal.FLOOR_EXCEEDED, move.reapable(), move.total(),
                    "floor_fraction=" + settings.floorFraction() + " floor_min_chunks=" + settings.floorMinChunks());
            }
            return new CollectionResult(name, move.reapable(), move.total(), move.moved(), null, null);
        } catch (RuntimeException e) {
            if ("55P03".equals(sqlState(e))) {
                return refuse(tenant, name, Refusal.GATE_BUSY, 0, 0, "a manifest writer holds the sweep gate");
            }
            log.warn("event=reaper_collection_failed tenant={} collection={} error={}", tenant, name, e.getMessage(), e);
            return new CollectionResult(name, 0, 0, 0, null, String.valueOf(e.getMessage()));
        }
    }

    private CollectionResult refuse(String tenant, String collection, Refusal reason, long candidates, long total,
                                    String detail) {
        long n = refusedTotal.incrementAndGet();
        log.warn("event=reaper_collection_refused tenant={} collection={} reason={} candidates={} total={} "
                + "refused_total={} detail={}", tenant, collection, reason, candidates, total, n, detail);
        return new CollectionResult(collection, candidates, total, 0, reason, null);
    }

    /** The event every pass logs, on success, refusal and failure alike, including {@code candidates=0}. */
    private TenantResult logPass(TenantResult r) {
        log.info("event=reaper_pass tenant={} collections={} candidates={} moved={} audit_rows={} refused={} errors={} error={}",
            r.tenant(), r.collections().size(), r.candidates(), r.moved(), r.auditRows(), r.refused(), r.errors(),
            r.error());
        return r;
    }

    private static String sqlState(Throwable t) {
        Throwable c = t;
        for (int depth = 0; c != null && depth < 32; depth++, c = c.getCause()) {
            if (c instanceof java.sql.SQLException se && se.getSQLState() != null) {
                return se.getSQLState();
            }
        }
        return null;
    }
}
