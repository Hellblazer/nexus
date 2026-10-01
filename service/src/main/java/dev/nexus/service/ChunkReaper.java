// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Rdr192BackfillGate;
import dev.nexus.service.db.Rdr192BackfillGate.BackfillIncompleteException;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.PgVectorRepository.ManifestLessCensusResult;
import dev.nexus.service.vectors.ReaperRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Duration;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Function;
import java.util.function.LongSupplier;
import java.util.function.Supplier;

/**
 * RDR-192 Step 9 (bead nexus-2x9xa): the state-derived reaper. A periodic pass, on {@code NexusService}'s
 * {@code sweepScheduler}, that moves chunks which nothing owns into the collection's quarantine sibling, and
 * expires the quarantine it fills.
 *
 * <p><b>It recomputes from state, it keeps no drop set.</b> The post-commit sweep ({@code runSweepTransaction}) is
 * the fast path and keeps its lock and statement timeouts; when it fails open (a gate timeout, a statement
 * timeout, a permission error) the chunks it meant to drop are left behind, and nothing remembers them. The next
 * pass of this class finds them again from the manifest, because a chunk with no manifest row in any owner state,
 * past the grace window, is reapable by {@code nexus.chunk_is_reapable} (vectors-021), the one predicate every
 * engine consumer shares.
 *
 * <p><b>It quarantines, it never hard-deletes a chunk that something might still own</b> (Sam, 2026-10-01). A
 * moved chunk is held in the sibling for 14 days; then {@code gc_expire_quarantine}, run by the same pass, takes
 * it under that function's own floors and its refusal to delete a chunk a manifest row still names
 * ({@link #QUARANTINE_RETENTION}). A moved chunk comes back when a later index run or heal names it in a manifest
 * row of its origin collection ({@code gc_restore_rereferenced}); restoring by chash, for a wrongly moved chunk no
 * manifest row names, is a separate route this class does not provide. <b>It
 * covers every collection prefix</b> ({@code knowledge__}, {@code docs__}, {@code code__}, {@code rdr__}): after
 * RDR-223's ownerless-write refusal the only producer of ownerless chunks is a crashed multi-request run, and those
 * are mostly {@code docs__} and {@code code__}.
 *
 * <p>The gates, in the order a pass meets them. A refusal is counted, logged at WARN, and written once to
 * {@code gc_audit} per state change (operation {@value #AUDIT_REFUSED}); a cloud operator has no engine log:
 * <ol>
 *   <li>per tenant, {@link Rdr192BackfillGate#requireComplete}: no verified backfill record, nothing is touched;</li>
 *   <li>per tenant, the quarantine siblings are expired first (before this pass adds to them), each under
 *       {@code gc_expire_quarantine}'s own floor;</li>
 *   <li>per collection: a {@code quarantine-} name is skipped (a quarantine sibling is {@code gc_expire_quarantine}'s);
 *       a collection with no catalog row, or in any lifecycle state but {@code live}, is refused;</li>
 *   <li>per collection, a dry run counts what is reapable and how many chunks the collection holds. Nothing reapable
 *       means nothing at risk and no census;</li>
 *   <li>the floor, judged on those counts BEFORE the census: a collection whose whole reapable set is at least
 *       {@link Settings#floorMinChunks} chunks and more than {@link Settings#floorFraction} of the collection is
 *       refused without paying for a census, every hour. The move re-judges it under the gate;</li>
 *   <li>the manifest-less census, re-run in the engine and bounded by {@link Settings#censusTimeout}, on every pass
 *       that would move something. It must read {@code scope_chunk_total} equal to the dry run's total (else it
 *       read a different set than the one being judged, and a false zero is possible), and
 *       {@code legacy-unmanifested == 0} and {@code unclassified == 0}. The stored ladder record is not enough:
 *       a tenant with an old record can later gain legacy-shaped chunks, and a live legacy note with no manifest
 *       row reads reapable once aged (matrix row R8);</li>
 *   <li>the move itself ({@code nexus.reaper_quarantine_chunks}, vectors-024): the exclusive sweep gate, the floor,
 *       and the grace re-checked in the DELETE's own WHERE, so a client write that refreshes a chunk after it was
 *       chosen wins.</li>
 * </ol>
 *
 * <p><b>Known blind spot of the census.</b> It resolves a chunk's owner from the chunk's own metadata
 * ({@code catalog_doc_id} or {@code doc_id}) or a note-shaped reverse match. A {@code docs__} or {@code code__}
 * chunk written after RDR-108 carries no document id, so a live document whose manifest rows are missing reads
 * {@code no-owner}, not {@code legacy-unmanifested}, and the census passes. The backstops are the 30 day grace, the
 * floor, and the quarantine's 14 days. See the operator runbook.
 *
 * <p>A pass moves at most {@link Settings#batchSize} (300) chunks per collection. The grace is the predicate's own
 * 30 days per chunk; this class has no setting for it. {@link #runOnce(Duration)} is package-private and takes the
 * grace so a test can pass zero.
 */
final class ChunkReaper {

    private static final Logger log = LoggerFactory.getLogger(ChunkReaper.class);

    /** The {@code actor} of the {@code gc_audit} rows this class's passes write. */
    static final String ACTOR = "engine-reaper";
    /** {@code gc_audit.operation} of a durable refusal row (once per collection per state change). */
    static final String AUDIT_REFUSED = "reaper_refused";
    /** {@code gc_audit.operation} the move function writes ({@code vectors-024}). */
    static final String AUDIT_MOVED = "reaper_quarantine";

    static final String ENABLED_ENV = "NX_REAPER_ENABLED";
    static final String INTERVAL_SECONDS_ENV = "NX_REAPER_INTERVAL_SECONDS";
    static final String BATCH_SIZE_ENV = "NX_REAPER_BATCH_SIZE";
    static final String FLOOR_FRACTION_ENV = "NX_REAPER_FLOOR_FRACTION";
    static final String FLOOR_MIN_CHUNKS_ENV = "NX_REAPER_FLOOR_MIN_CHUNKS";
    static final String WALL_CLOCK_BUDGET_SECONDS_ENV = "NX_REAPER_WALL_CLOCK_BUDGET_SECONDS";
    static final String CENSUS_TIMEOUT_SECONDS_ENV = "NX_REAPER_CENSUS_TIMEOUT_SECONDS";

    /** Most chunks one pass moves from one collection (Sam, 2026-09-26: 300). */
    static final int MAX_BATCH_SIZE = 300;
    /** The interval never goes below this: a 1 second interval is a busy loop on the shared scheduler thread. */
    static final Duration MIN_INTERVAL = Duration.ofSeconds(60);
    /** First pass this long after boot, not one full interval: an engine that restarts hourly must still reap. */
    static final Duration INITIAL_DELAY = Duration.ofSeconds(60);
    /** How long a moved chunk waits in quarantine before {@code gc_expire_quarantine} may take it. */
    static final Duration QUARANTINE_RETENTION = Duration.ofDays(14);
    /** Most title/source_path rows a refusal's audit row carries. */
    static final int SAMPLE_SIZE = 5;
    /** The census page asked for: items beyond this are not itemised (the totals are collection-wide). */
    private static final int CENSUS_PAGE = 300;
    private static final String QUARANTINE_PREFIX = "quarantine-";
    private static final String LIVE = "live";
    /** Per-statement bound for the enumeration, the dry run, the move and the expiry. */
    private static final int STATEMENT_TIMEOUT_MS = 25_000;
    private static final int LOCK_TIMEOUT_MS = 2_000;
    /** Marker in the move function's own 55P03 message when the SWEEP GATE (not a row lock) timed out. */
    private static final String GATE_MESSAGE_MARKER = "RDR-191 sweep gate";

    /**
     * The reaper's settings. Defaults are Sam's rulings (2026-09-26 and 2026-10-01): hourly, 300 chunks per
     * collection per pass, a floor of 0.25 from 100 reapable chunks up (the {@code NX_GC_FLOOR_FRACTION} semantics,
     * applied to the move and to the expiry). {@code enabled} is a kill switch, not a tuning knob.
     * {@code wallClockBudget} bounds a whole run, checked at tenant and collection boundaries, because the reaper
     * shares one scheduler thread with the T1 and tuple sweeps. {@code censusTimeout} bounds one collection's census
     * statement.
     */
    record Settings(boolean enabled, Duration interval, int batchSize, double floorFraction,
                    int floorMinChunks, Duration wallClockBudget, Duration censusTimeout) {

        static final Duration DEFAULT_INTERVAL = Duration.ofHours(1);
        static final double DEFAULT_FLOOR_FRACTION = 0.25;
        static final int DEFAULT_FLOOR_MIN_CHUNKS = 100;
        static final Duration DEFAULT_WALL_CLOCK_BUDGET = Duration.ofMinutes(10);
        static final Duration DEFAULT_CENSUS_TIMEOUT = Duration.ofSeconds(60);

        static Settings defaults() {
            return new Settings(true, DEFAULT_INTERVAL, MAX_BATCH_SIZE, DEFAULT_FLOOR_FRACTION,
                                DEFAULT_FLOOR_MIN_CHUNKS, DEFAULT_WALL_CLOCK_BUDGET, DEFAULT_CENSUS_TIMEOUT);
        }

        /**
         * Resolve from the environment. An invalid value falls back to the default with a WARN and never throws:
         * a typo must not stop the engine from booting, and for the floor in particular the default is the safe
         * direction (a nan or out-of-range value must not neutralize it). The kill switch takes an explicit
         * true/false; anything else warns and leaves the reaper ENABLED (the default), so a typo in the off switch
         * is loud rather than silently inert.
         */
        static Settings fromEnv(Function<String, String> env) {
            long intervalSeconds = positive(env, INTERVAL_SECONDS_ENV, DEFAULT_INTERVAL.toSeconds(), Long.MAX_VALUE / 4);
            if (intervalSeconds < MIN_INTERVAL.toSeconds()) {
                log.warn("event=reaper_setting_clamped name={} raw={} using={}", INTERVAL_SECONDS_ENV, intervalSeconds,
                    MIN_INTERVAL.toSeconds());
                intervalSeconds = MIN_INTERVAL.toSeconds();
            }
            return new Settings(
                enabled(env.apply(ENABLED_ENV)),
                Duration.ofSeconds(intervalSeconds),
                (int) positive(env, BATCH_SIZE_ENV, MAX_BATCH_SIZE, MAX_BATCH_SIZE),
                fraction(env, FLOOR_FRACTION_ENV, DEFAULT_FLOOR_FRACTION),
                (int) nonNegative(env, FLOOR_MIN_CHUNKS_ENV, DEFAULT_FLOOR_MIN_CHUNKS),
                Duration.ofSeconds(positive(env, WALL_CLOCK_BUDGET_SECONDS_ENV, DEFAULT_WALL_CLOCK_BUDGET.toSeconds(),
                                            Long.MAX_VALUE / 4)),
                Duration.ofSeconds(positive(env, CENSUS_TIMEOUT_SECONDS_ENV, DEFAULT_CENSUS_TIMEOUT.toSeconds(), 3600)));
        }

        /** Unset or blank: on. An explicit true or false decides; anything else warns and stays on. */
        private static boolean enabled(String raw) {
            if (raw == null || raw.isBlank()) return true;
            String v = raw.trim().toLowerCase(java.util.Locale.ROOT);
            if (v.equals("true") || v.equals("1") || v.equals("on") || v.equals("yes")) return true;
            if (v.equals("false") || v.equals("0") || v.equals("off") || v.equals("no")) return false;
            log.warn("event=reaper_setting_invalid name={} raw={} using=true expected=true_or_false "
                + "(the reaper stays ENABLED; set false to turn it off)", ENABLED_ENV, raw);
            return true;
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

    /**
     * Why a tenant or a collection was held back. A REFUSAL ({@link #refusal} true) is a decision the reaper made
     * about the data: counted in {@code refused_total}, logged at WARN, written to {@code gc_audit}. A SKIP is a
     * collision with a live writer that clears itself next pass: counted separately, logged at INFO, never audited.
     */
    enum Refusal {
        BACKFILL_INCOMPLETE(true), UNREGISTERED_COLLECTION(true), COLLECTION_NOT_LIVE(true),
        CENSUS_LEGACY_UNMANIFESTED(true), CENSUS_UNCLASSIFIED(true), CENSUS_TIMED_OUT(true),
        CENSUS_SCOPE_MISMATCH(true), FLOOR_EXCEEDED(true), EXPIRY_REFUSED(true),
        /** A manifest writer holds the per-collection sweep gate SHARED, so the exclusive acquire timed out. */
        GATE_BUSY(false),
        /** A lock wait other than the gate timed out: a chunk row a client is refreshing, or the sibling registration. */
        LOCK_TIMEOUT(false);

        final boolean refusal;

        Refusal(boolean refusal) {
            this.refusal = refusal;
        }
    }

    /** One collection's outcome. {@code refusal} and {@code error} are null when absent. */
    record CollectionResult(String collection, long candidates, long total, long moved, Refusal refusal,
                            String error) {}

    /** One quarantine sibling's expiry outcome. {@code refusal} and {@code error} are null when absent. */
    record ExpiryResult(String quarantineCollection, long expired, long refused, Refusal refusal, String error) {}

    record TenantResult(String tenant, List<CollectionResult> collections, List<ExpiryResult> expiries,
                        Refusal tenantRefusal, String error, boolean wallClockCut) {
        CollectionResult collection(String name) {
            return collections.stream().filter(c -> c.collection().equals(name)).findFirst().orElse(null);
        }

        ExpiryResult expiry(String quarantineCollection) {
            return expiries.stream().filter(e -> e.quarantineCollection().equals(quarantineCollection))
                .findFirst().orElse(null);
        }

        long candidates() {
            return collections.stream().mapToLong(CollectionResult::candidates).sum();
        }

        long moved() {
            return collections.stream().mapToLong(CollectionResult::moved).sum();
        }

        long expired() {
            return expiries.stream().mapToLong(ExpiryResult::expired).sum();
        }

        /** One gc_audit row per collection that moved anything. */
        int auditRows() {
            return (int) collections.stream().filter(c -> c.moved() > 0).count();
        }

        /** Decisions the reaper made about the data. A gate or lock skip is not one. */
        int refused() {
            return (int) collections.stream().filter(c -> c.refusal() != null && c.refusal().refusal).count()
                + (int) expiries.stream().filter(e -> e.refusal() != null).count()
                + (tenantRefusal != null ? 1 : 0);
        }

        /** Collections that collided with a live writer and wait for the next pass. */
        int skipped() {
            return (int) collections.stream().filter(c -> c.refusal() != null && !c.refusal().refusal).count();
        }

        int errors() {
            return (int) collections.stream().filter(c -> c.error() != null).count()
                + (int) expiries.stream().filter(e -> e.error() != null).count() + (error != null ? 1 : 0);
        }
    }

    record RunResult(List<TenantResult> tenants, boolean wallClockCut) {
        TenantResult tenant(String name) {
            return tenants.stream().filter(t -> t.tenant().equals(name)).findFirst().orElse(null);
        }
    }

    /** The manifest-less census, one page, bounded. A seam so a test can supply a census the SQL cannot produce. */
    @FunctionalInterface
    interface Census {
        ManifestLessCensusResult run(String tenant, String collection, int limit, Duration statementTimeout);
    }

    private final ReaperRepository store;
    private final PgVectorRepository vectors;
    private final CatalogRepository catalog;
    private final Rdr192BackfillGate gate;
    private final Supplier<? extends Collection<String>> tenants;
    private final Settings settings;
    private final Clock clock;
    private final Census census;
    private final LongSupplier nanos;
    /** Refusals since boot: a counter a log reader can alert on, where a one-off line would scroll past. */
    private final AtomicLong refusedTotal = new AtomicLong();
    /** Collections skipped since boot because a manifest writer held the sweep gate. Not a refusal. */
    private final AtomicLong gateBusyTotal = new AtomicLong();
    /** Collections skipped since boot because a lock wait other than the sweep gate timed out. Not a refusal. */
    private final AtomicLong lockTimeoutTotal = new AtomicLong();
    private final AtomicReference<RunResult> lastRun = new AtomicReference<>();

    ChunkReaper(ReaperRepository store, PgVectorRepository vectors, CatalogRepository catalog,
                Rdr192BackfillGate gate, Supplier<? extends Collection<String>> tenants, Settings settings,
                Clock clock) {
        this(store, vectors, catalog, gate, tenants, settings, clock,
            (t, c, limit, timeout) -> vectors.manifestLessCensusBounded(t, c, limit, 0, timeout), System::nanoTime);
    }

    ChunkReaper(ReaperRepository store, PgVectorRepository vectors, CatalogRepository catalog,
                Rdr192BackfillGate gate, Supplier<? extends Collection<String>> tenants, Settings settings,
                Clock clock, Census census, LongSupplier nanos) {
        this.store = store;
        this.vectors = vectors;
        this.catalog = catalog;
        this.gate = gate;
        this.tenants = tenants;
        this.settings = settings;
        this.clock = clock;
        this.census = census;
        this.nanos = nanos;
    }

    Settings settings() {
        return settings;
    }

    /** Refusals since boot (decisions about the data); a gate or lock skip is not one. */
    long refusedTotal() {
        return refusedTotal.get();
    }

    /** Collections skipped since boot because a manifest writer held the sweep gate. */
    long gateBusyTotal() {
        return gateBusyTotal.get();
    }

    /** Collections skipped since boot because a lock wait other than the sweep gate timed out. */
    long lockTimeoutTotal() {
        return lockTimeoutTotal.get();
    }

    /** The last completed pass, or null before the first. */
    RunResult lastRun() {
        return lastRun.get();
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
        long deadline = nanos.getAsLong() + settings.wallClockBudget().toNanos();
        List<TenantResult> results = new ArrayList<>();
        boolean cut = false;
        Collection<String> tenantIds;
        try {
            tenantIds = tenants.get();
        } catch (RuntimeException e) {
            log.warn("event=reaper_run tenants=0 candidates=0 moved=0 audit_rows=0 expired=0 refused=0 skipped=0 "
                + "errors=1 wall_clock_cut=false error={}", e.getMessage(), e);
            RunResult failed = new RunResult(List.of(), false);
            lastRun.set(failed);
            return failed;
        }
        int unvisited = 0;
        for (String tenant : tenantIds) {
            if (cut || nanos.getAsLong() >= deadline) {
                cut = true;
                unvisited++;
                continue;
            }
            TenantResult r = passTenant(tenant, grace, deadline);
            results.add(r);
            if (r.wallClockCut()) {
                cut = true;
            }
        }
        if (unvisited > 0) {
            log.warn("event=reaper_wall_clock_cut tenants_unvisited={}", unvisited);
        }
        long candidates = results.stream().mapToLong(TenantResult::candidates).sum();
        long moved = results.stream().mapToLong(TenantResult::moved).sum();
        long expired = results.stream().mapToLong(TenantResult::expired).sum();
        int auditRows = results.stream().mapToInt(TenantResult::auditRows).sum();
        int refused = results.stream().mapToInt(TenantResult::refused).sum();
        int skipped = results.stream().mapToInt(TenantResult::skipped).sum();
        int errors = results.stream().mapToInt(TenantResult::errors).sum();
        log.info("event=reaper_run tenants={} candidates={} moved={} audit_rows={} expired={} refused={} skipped={} "
                + "errors={} wall_clock_cut={}",
            results.size(), candidates, moved, auditRows, expired, refused, skipped, errors, cut);
        RunResult out = new RunResult(results, cut);
        lastRun.set(out);
        return out;
    }

    private TenantResult passTenant(String tenant, Duration grace, long deadlineNanos) {
        try {
            gate.requireComplete(tenant);
        } catch (BackfillIncompleteException e) {
            long n = refusedTotal.incrementAndGet();
            log.warn("event=reaper_tenant_refused tenant={} reason={} refused_total={} detail={}",
                tenant, Refusal.BACKFILL_INCOMPLETE, n, e.getMessage());
            recordRefusal(tenant, "", Refusal.BACKFILL_INCOMPLETE, 0, 0, e.getMessage(), List.of());
            return logPass(new TenantResult(tenant, List.of(), List.of(), Refusal.BACKFILL_INCOMPLETE, null, false));
        }

        List<CollectionResult> out = new ArrayList<>();
        List<ExpiryResult> expiries = new ArrayList<>();
        String tenantError = null;
        boolean cut = false;
        try {
            List<String> names = store.collectionsWithChunks(tenant, Duration.ofMillis(STATEMENT_TIMEOUT_MS));
            Map<String, String> states = store.lifecycleStates(tenant);

            // Expire first, so the floor judges the quarantine as it stood before this pass adds to it.
            for (String name : names) {
                if (!name.startsWith(QUARANTINE_PREFIX)) continue;
                if (nanos.getAsLong() >= deadlineNanos) {
                    cut = true;
                    break;
                }
                expiries.add(expire(tenant, name, states));
            }
            for (String name : names) {
                if (name.startsWith(QUARANTINE_PREFIX)) continue;
                if (cut || nanos.getAsLong() >= deadlineNanos) {
                    cut = true;
                    break;
                }
                out.add(passCollection(tenant, name, states, grace));
            }
            if (cut) {
                tenantError = "wall-clock budget spent; the remaining collections wait for the next pass";
                log.warn("event=reaper_wall_clock_cut tenant={} collections_done={}", tenant, out.size());
            }
        } catch (RuntimeException e) {
            tenantError = e.getMessage();
            log.warn("event=reaper_tenant_failed tenant={} error={}", tenant, e.getMessage(), e);
        }
        return logPass(new TenantResult(tenant, out, expiries, null, tenantError, cut));
    }

    /** The move's and the census-gate's floor rule, one definition: at least the minimum, and MORE than the fraction. */
    static boolean floorTrips(long reapable, long total, double fraction, int minChunks) {
        return total > 0 && reapable >= minChunks && (double) reapable / (double) total > fraction;
    }

    private CollectionResult passCollection(String tenant, String name, Map<String, String> states, Duration grace) {
        if (!states.containsKey(name)) {
            return refuse(tenant, name, Refusal.UNREGISTERED_COLLECTION, 0, 0, "no catalog_collections row", List.of());
        }
        String state = states.get(name);
        if (!LIVE.equals(state)) {
            return refuse(tenant, name, Refusal.COLLECTION_NOT_LIVE, 0, 0, "lifecycle_state=" + state, List.of());
        }
        try {
            ReaperRepository.Pass probe = store.probe(tenant, name, grace, STATEMENT_TIMEOUT_MS);
            if (probe.reapable() == 0) {
                return new CollectionResult(name, 0, probe.total(), 0, null, null);
            }

            // The floor first, on the probe's own counts: a collection the floor will refuse anyway must not pay
            // for a census every hour. The move re-judges the same rule under the sweep gate.
            if (floorTrips(probe.reapable(), probe.total(), settings.floorFraction(), settings.floorMinChunks())) {
                return refuse(tenant, name, Refusal.FLOOR_EXCEEDED, probe.reapable(), probe.total(), floorDetail(),
                    sampleReapable(tenant, name, grace));
            }

            // The census, in the engine, on every pass that would move something. The stored ladder record is not
            // enough: nothing revokes it, so a tenant that later gains legacy-shaped chunks keeps an open gate.
            ManifestLessCensusResult c;
            try {
                c = census.run(tenant, name, CENSUS_PAGE, settings.censusTimeout());
            } catch (RuntimeException e) {
                if ("57014".equals(sqlState(e))) {
                    return refuse(tenant, name, Refusal.CENSUS_TIMED_OUT, probe.reapable(), probe.total(),
                        "census statement exceeded " + settings.censusTimeout().toSeconds() + "s; it was not read, "
                            + "so nothing is moved", List.of());
                }
                throw e;
            }
            // Non-vacuity: a census that read a different set than the one being judged (a wrong tenant or an
            // unset RLS GUC reads 0) proves nothing, and its zero must not be taken for a clean collection.
            if (c.scopeChunkTotal() != probe.total()) {
                return refuse(tenant, name, Refusal.CENSUS_SCOPE_MISMATCH, probe.reapable(), probe.total(),
                    "census saw scope_chunk_total=" + c.scopeChunkTotal() + " but the dry run counted " + probe.total()
                        + " chunks; chunks changed between the two reads, or the census read the wrong scope. "
                        + "Retried next pass", List.of());
            }
            long legacy = c.totals().getOrDefault("legacy-unmanifested", 0L);
            long unclassified = c.totals().getOrDefault("unclassified", 0L);
            if (legacy > 0) {
                return refuse(tenant, name, Refusal.CENSUS_LEGACY_UNMANIFESTED, probe.reapable(), probe.total(),
                    "legacy_unmanifested=" + legacy, sampleCensus(tenant, name, c, "legacy-unmanifested"));
            }
            if (unclassified > 0) {
                return refuse(tenant, name, Refusal.CENSUS_UNCLASSIFIED, probe.reapable(), probe.total(),
                    "unclassified=" + unclassified, sampleCensus(tenant, name, c, "unclassified"));
            }

            ReaperRepository.Pass move = store.move(tenant, name, QUARANTINE_PREFIX + name,
                quarantinedAt(), settings.batchSize(), grace, settings.floorFraction(),
                settings.floorMinChunks(), STATEMENT_TIMEOUT_MS, LOCK_TIMEOUT_MS);
            if (move.refused()) {
                return refuse(tenant, name, Refusal.FLOOR_EXCEEDED, move.reapable(), move.total(), floorDetail(),
                    sampleReapable(tenant, name, grace));
            }
            return new CollectionResult(name, move.reapable(), move.total(), move.moved(), null, null);
        } catch (RuntimeException e) {
            if ("55P03".equals(sqlState(e))) {
                return skip(tenant, name, chainMessage(e).contains(GATE_MESSAGE_MARKER)
                    ? Refusal.GATE_BUSY : Refusal.LOCK_TIMEOUT);
            }
            log.warn("event=reaper_collection_failed tenant={} collection={} error={}", tenant, name, e.getMessage(), e);
            return new CollectionResult(name, 0, 0, 0, null, String.valueOf(e.getMessage()));
        }
    }

    /**
     * The stamp {@code gc_expire_quarantine} compares against: whole seconds, {@code yyyy-MM-ddTHH:mm:ssZ}, the shape
     * the Python indexer writes. An {@code Instant.toString()} carries fractional seconds, which sort differently
     * from the cutoff's and expire a chunk early.
     */
    private String quarantinedAt() {
        return clock.instant().truncatedTo(ChronoUnit.SECONDS).toString();
    }

    private String floorDetail() {
        return "floor_fraction=" + settings.floorFraction() + " floor_min_chunks=" + settings.floorMinChunks();
    }

    /** Expire the siblings of this pass's own making: 14 days old, under {@code gc_expire_quarantine}'s own floor. */
    private ExpiryResult expire(String tenant, String quarantine, Map<String, String> states) {
        String origin = quarantine.substring(QUARANTINE_PREFIX.length());
        if (!states.containsKey(origin)) {
            // gc_expire_quarantine derives the dimension from the origin's registered row; with none there is
            // nothing safe to say about these chunks, so they are left (and nothing is audited as refused).
            log.info("event=reaper_expire_skipped tenant={} quarantine={} reason=origin_not_registered",
                tenant, quarantine);
            return new ExpiryResult(quarantine, 0, 0, null, null);
        }
        String cutoff = clock.instant().minus(QUARANTINE_RETENTION).truncatedTo(ChronoUnit.SECONDS).toString();
        try {
            PgVectorRepository.ExpireOutcome out = vectors.expireQuarantine(tenant, quarantine, origin, cutoff,
                settings.floorFraction(), settings.floorMinChunks(), false, STATEMENT_TIMEOUT_MS);
            // expired == 0 with refused > 0: the floor tripped, or every past-cutoff chunk is still named by a
            // manifest row (gc_expire_quarantine reports both as `refused`). Either way nothing was deleted, and
            // an operator must be told.
            Refusal refusal = out.expired() == 0 && out.refused() > 0 ? Refusal.EXPIRY_REFUSED : null;
            if (refusal != null) {
                long n = refusedTotal.incrementAndGet();
                log.warn("event=reaper_expire_refused tenant={} quarantine={} refused={} refused_total={} cutoff={} {}",
                    tenant, quarantine, out.refused(), n, cutoff, floorDetail());
                recordRefusal(tenant, quarantine, refusal, out.refused(), 0,
                    "gc_expire_quarantine deleted nothing: the floor tripped, or the past-cutoff chunks are still "
                        + "named by a manifest row; cutoff=" + cutoff + " " + floorDetail(), List.of());
            } else if (out.expired() > 0) {
                log.info("event=reaper_expired tenant={} quarantine={} expired={} refused={} cutoff={}",
                    tenant, quarantine, out.expired(), out.refused(), cutoff);
            }
            return new ExpiryResult(quarantine, out.expired(), out.refused(), refusal, null);
        } catch (RuntimeException e) {
            log.warn("event=reaper_expire_failed tenant={} quarantine={} error={}", tenant, quarantine,
                e.getMessage(), e);
            return new ExpiryResult(quarantine, 0, 0, null, String.valueOf(e.getMessage()));
        }
    }

    // ── refusals ─────────────────────────────────────────────────────────────

    private CollectionResult refuse(String tenant, String collection, Refusal reason, long candidates, long total,
                                    String detail, List<Map<String, Object>> sample) {
        long n = refusedTotal.incrementAndGet();
        log.warn("event=reaper_collection_refused tenant={} collection={} reason={} candidates={} total={} "
                + "refused_total={} detail={}", tenant, collection, reason, candidates, total, n, detail);
        recordRefusal(tenant, collection, reason, candidates, total, detail, sample);
        return new CollectionResult(collection, candidates, total, 0, reason, null);
    }

    /** A collision with a live writer: counted on its own, logged at INFO, never audited, retried next pass. */
    private CollectionResult skip(String tenant, String collection, Refusal reason) {
        long n = (reason == Refusal.GATE_BUSY ? gateBusyTotal : lockTimeoutTotal).incrementAndGet();
        log.info("event=reaper_collection_skipped tenant={} collection={} reason={} {}_total={} detail={}",
            tenant, collection, reason, reason == Refusal.GATE_BUSY ? "gate_busy" : "lock_timeout", n,
            reason == Refusal.GATE_BUSY
                ? "a manifest writer holds the sweep gate; retried next pass"
                : "a lock wait timed out (a chunk row a client is refreshing, or the sibling registration); retried next pass");
        return new CollectionResult(collection, 0, 0, 0, reason, null);
    }

    /**
     * Write the refusal to {@code gc_audit} unless the newest reaper row for this collection already says the same
     * reason: once per collection per state change, not once an hour. Never throws: the audit row is a visibility
     * aid, and a failure to write it must not turn a refusal (nothing moved) into a failed pass.
     */
    private void recordRefusal(String tenant, String collection, Refusal reason, long candidates, long total,
                               String detail, List<Map<String, Object>> sample) {
        try {
            if (newestReaperReason(tenant, collection).equals(reason.name())) {
                return;
            }
            Map<String, Object> details = new LinkedHashMap<>();
            details.put("reason", reason.name());
            details.put("detail", detail);
            details.put("candidates", candidates);
            details.put("total", total);
            details.put("floor_fraction", settings.floorFraction());
            details.put("floor_min_chunks", settings.floorMinChunks());
            details.put("sample", sample);
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("operation", AUDIT_REFUSED);
            row.put("collection", collection);
            row.put("actor", ACTOR);
            row.put("dry_run", false);
            row.put("details", details);
            catalog.recordGcAudit(tenant, row);
        } catch (RuntimeException e) {
            log.warn("event=reaper_refusal_audit_failed tenant={} collection={} reason={} error={}",
                tenant, collection, reason, e.getMessage(), e);
        }
    }

    /** The reason of the newest {@code reaper_*} audit row for the collection, or "" when it has none. */
    private String newestReaperReason(String tenant, String collection) {
        boolean tenantLevel = collection.isEmpty();
        var rows = catalog.listGcAudit(tenant, tenantLevel ? null : collection,
            tenantLevel ? AUDIT_REFUSED : null, 25, 0);
        for (var r : rows) {
            String op = String.valueOf(r.get("operation"));
            if (!collection.equals(String.valueOf(r.get("collection")))) continue;
            if (op.equals(AUDIT_MOVED)) return "";
            if (op.equals(AUDIT_REFUSED)) {
                return r.get("details") instanceof Map<?, ?> d ? String.valueOf(d.get("reason")) : "";
            }
        }
        return "";
    }

    /** Up to {@link #SAMPLE_SIZE} of what the floor refused to move, by title and source path. */
    private List<Map<String, Object>> sampleReapable(String tenant, String collection, Duration grace) {
        try {
            var rows = vectors.reapableChunks(tenant, collection, grace == null ? null : grace.toSeconds(), null,
                SAMPLE_SIZE, 0);
            List<Map<String, Object>> out = new ArrayList<>();
            for (var r : rows) {
                out.add(sampleRow(r.chash(), r.title(), r.sourcePath()));
            }
            return out;
        } catch (RuntimeException e) {
            log.warn("event=reaper_sample_failed tenant={} collection={} error={}", tenant, collection, e.getMessage());
            return List.of();
        }
    }

    /** Up to {@link #SAMPLE_SIZE} chunks the census put in {@code bucket}, by title and source path. */
    private List<Map<String, Object>> sampleCensus(String tenant, String collection, ManifestLessCensusResult c,
                                                   String bucket) {
        try {
            List<String> hexes = c.chashes().getOrDefault(bucket, List.of());
            if (hexes.size() > SAMPLE_SIZE) hexes = hexes.subList(0, SAMPLE_SIZE);
            List<Map<String, Object>> out = new ArrayList<>();
            for (var r : store.describe(tenant, collection, hexes)) {
                out.add(sampleRow(r.chash(), r.title(), r.sourcePath()));
            }
            return out;
        } catch (RuntimeException e) {
            log.warn("event=reaper_sample_failed tenant={} collection={} error={}", tenant, collection, e.getMessage());
            return List.of();
        }
    }

    private static Map<String, Object> sampleRow(String chash, String title, String sourcePath) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("chash", chash);
        m.put("title", title);
        m.put("source_path", sourcePath);
        return m;
    }

    /** The event every pass logs, on success, refusal and failure alike, including {@code candidates=0}. */
    private TenantResult logPass(TenantResult r) {
        log.info("event=reaper_pass tenant={} collections={} candidates={} moved={} audit_rows={} expired={} refused={} "
                + "skipped={} errors={} wall_clock_cut={} error={}",
            r.tenant(), r.collections().size(), r.candidates(), r.moved(), r.auditRows(), r.expired(), r.refused(),
            r.skipped(), r.errors(), r.wallClockCut(), r.error());
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

    private static String chainMessage(Throwable t) {
        StringBuilder sb = new StringBuilder();
        Throwable c = t;
        for (int depth = 0; c != null && depth < 32; depth++, c = c.getCause()) {
            if (c.getMessage() != null) sb.append(c.getMessage()).append(' ');
        }
        return sb.toString();
    }
}
