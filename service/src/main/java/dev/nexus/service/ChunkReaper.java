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
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.concurrent.ConcurrentHashMap;
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
 * moved chunk is tagged ({@code quarantined_by = engine-reaper}) and held in the sibling for
 * {@link Settings#quarantineRetention} (14 days by default); then {@code reaper_expire_quarantine}, run by the same
 * pass, deletes it, refusing only to delete a chunk a manifest row still names. <b>There is no fraction floor on
 * expiry</b> (Sam, 2026-10-01): the floor on the move bounds what one pass quarantines, and what protects a chunk
 * wrongly moved is the manifest recheck, the retention window, the audit row that names every deleted chash and the
 * restore verb (nexus-wbfpw.49). <b>The split is symmetric, with one engine-side exception</b> (Sam, 2026-10-01 and
 * 2026-10-04): the engine's expiry covers the chunks this class moved, and the client's expiry
 * ({@code gc_expire_quarantine}, run by {@code nx index repo}) skips them and deletes only what the client moved, at its
 * own {@code NX_GC_QUARANTINE_DAYS} retention. The exception is {@code quarantine-knowledge__*}: no client sweep runs
 * on a knowledge collection routinely, so the engine also expires the rows a client moved there
 * ({@link #expireClientMoved}, {@link Settings#clientQuarantineRetention}, audited as
 * {@value #AUDIT_EXPIRED_CLIENT}); code, docs and rdr quarantine stays with the client. A moved chunk a
 * manifest row of its origin names again is never deleted; for a repo collection a later index run or heal moves it
 * back ({@code gc_restore_rereferenced}), and for a {@code knowledge__} collection (where a re-put embeds a fresh
 * origin chunk) nothing does and the quarantine copy stays, protected (storage only; the copy is redundant because
 * the manifest row requires the origin chunk). <b>It covers every collection prefix</b> ({@code knowledge__}, {@code docs__}, {@code code__}, {@code rdr__}): after
 * RDR-223's ownerless-write refusal the only producer of ownerless chunks is a crashed multi-request run, and those
 * are mostly {@code docs__} and {@code code__}.
 *
 * <p>The gates, in the order a pass meets them. A refusal is counted, logged at WARN, and written once to
 * {@code gc_audit} per state change (operation {@value #AUDIT_REFUSED}); a cloud operator has no engine log:
 * <ol>
 *   <li>per tenant, {@link Rdr192BackfillGate#requireComplete}: no verified backfill record, nothing is touched
 *       (unless the tenant holds no chunk and no manifest row at all: an empty tenant passes, nexus-wbfpw.73);</li>
 *   <li>per tenant, the quarantine siblings are expired first (before this pass adds to them), only the chunks
 *       this class tagged, never one a manifest row names, at most {@link #MAX_EXPIRY_ROWS} per sibling and pass;</li>
 *   <li>per collection: a {@code quarantine-} name is skipped (a quarantine sibling is expired, never reaped);
 *       a collection with no catalog row, or in any lifecycle state but {@code live}, is refused;</li>
 *   <li>per collection, a dry run counts what is reapable and how many chunks the collection holds. Nothing reapable
 *       means nothing at risk and no census;</li>
 *   <li>the floor, judged on those counts BEFORE the census: a collection whose whole reapable set is at least
 *       {@link Settings#floorMinChunks} chunks and more than {@link Settings#floorFraction} of the collection is
 *       refused without paying for a census, every hour. The move re-judges it under the gate;</li>
 *   <li>the manifest-less census, re-run in the engine and bounded by {@link Settings#censusTimeout}, on every pass
 *       that would move something. It must read {@code scope_chunk_total} not below the dry run's total (below it,
 *       it read a different set than the one being judged, and a false zero is possible; above it, a chunk was written
 *       between the two reads, which proceeds and is logged), and
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
 * <p><b>No collection starves another.</b> A run that spends its wall-clock budget resumes, on the next pass, at the
 * tenant and the collection where it was cut. A collection whose census times out three passes running backs off
 * for 2^k passes (at most 24 hours), so a pathological collection stops spending the shared thread every hour. Any
 * other statement of a pass that hits its bound (SQLSTATE 57014: the dry run, the move, one sibling's expiry) is a
 * counted refusal, audited once per state change, with the same streak and rest.
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
    /** {@code gc_audit.operation} of the expiry of chunks the reaper moved ({@code vectors-024-2}). */
    static final String AUDIT_EXPIRED = "reaper_expire_quarantine";
    /**
     * {@code gc_audit.operation} of the expiry of chunks a CLIENT moved ({@code vectors-028}, nexus-wbfpw.75): its own
     * name, so a reader can tell which population was deleted.
     */
    static final String AUDIT_EXPIRED_CLIENT = "reaper_expire_client_quarantine";

    static final String ENABLED_ENV = "NX_REAPER_ENABLED";
    static final String INTERVAL_SECONDS_ENV = "NX_REAPER_INTERVAL_SECONDS";
    static final String BATCH_SIZE_ENV = "NX_REAPER_BATCH_SIZE";
    static final String FLOOR_FRACTION_ENV = "NX_REAPER_FLOOR_FRACTION";
    static final String FLOOR_MIN_CHUNKS_ENV = "NX_REAPER_FLOOR_MIN_CHUNKS";
    static final String WALL_CLOCK_BUDGET_SECONDS_ENV = "NX_REAPER_WALL_CLOCK_BUDGET_SECONDS";
    static final String CENSUS_TIMEOUT_SECONDS_ENV = "NX_REAPER_CENSUS_TIMEOUT_SECONDS";
    static final String QUARANTINE_RETENTION_DAYS_ENV = "NX_REAPER_QUARANTINE_RETENTION_DAYS";
    static final String CLIENT_QUARANTINE_RETENTION_DAYS_ENV = "NX_REAPER_CLIENT_QUARANTINE_RETENTION_DAYS";
    static final String FLOOR_EXEMPT_COLLECTIONS_ENV = "NX_REAPER_FLOOR_EXEMPT_COLLECTIONS";

    /** Most chunks one pass moves from one collection (Sam, 2026-09-26: 300). */
    static final int MAX_BATCH_SIZE = 300;
    /** The interval never goes below this: a 1 second interval is a busy loop on the shared scheduler thread. */
    static final Duration MIN_INTERVAL = Duration.ofSeconds(60);
    /** First pass this long after boot, not one full interval: an engine that restarts hourly must still reap. */
    static final Duration INITIAL_DELAY = Duration.ofSeconds(60);
    /** Consecutive census timeouts on one collection before its census backs off. */
    static final int CENSUS_BACKOFF_AFTER = 3;
    /** The longest a timing-out collection's census is left alone. */
    static final Duration MAX_CENSUS_BACKOFF = Duration.ofHours(24);
    /** Most chunks one expiry call deletes from one sibling (the audit row's own chash ceiling); the rest wait a pass. */
    static final int MAX_EXPIRY_ROWS = 5000;
    /**
     * The only origins whose CLIENT-moved quarantine rows the engine expires (Sam, 2026-10-04, nexus-wbfpw.75): those
     * the catalog registry says are knowledge collections, so their siblings are {@code quarantine-knowledge__*}. No
     * client sweep runs on a knowledge collection routinely; code, docs and rdr quarantine is expired by the client
     * on every {@code nx index repo}, at the user's own {@code NX_GC_QUARANTINE_DAYS}, which the engine cannot see.
     * The content type is the registry row's, never parsed from a name (RDR-204, {@code CollectionParseGateTest}).
     * {@code nexus.reaper_expire_client_quarantine} enforces the same boundary itself: the sibling must be a
     * registered quarantine collection of this content type, and the origin a registered LIVE collection of it.
     */
    static final String CLIENT_EXPIRY_CONTENT_TYPE = "knowledge";
    /** Consecutive timeouts of one statement before it backs off (the same rule for every bounded statement). */
    static final int TIMEOUT_BACKOFF_AFTER = CENSUS_BACKOFF_AFTER;
    /** Most title/source_path rows a refusal's audit row carries. */
    static final int SAMPLE_SIZE = 5;
    /** The census page asked for: items beyond this are not itemised (the totals are collection-wide). */
    private static final int CENSUS_PAGE = 300;
    /** A census that read this many TIMES the dry run's total, and at least {@link #CENSUS_GROWTH_WARN_MIN_EXTRA} more, is warned about. */
    private static final int CENSUS_GROWTH_WARN_FACTOR = 2;
    private static final long CENSUS_GROWTH_WARN_MIN_EXTRA = 100;
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
     * applied to the MOVE only; expiry has no floor). {@code enabled} is a kill switch, not a tuning knob.
     * {@code wallClockBudget} bounds a whole run, checked at tenant and collection boundaries, because the reaper
     * shares one scheduler thread with the T1 and tuple sweeps. {@code censusTimeout} bounds one collection's census
     * statement.
     */
    record Settings(boolean enabled, Duration interval, int batchSize, double floorFraction,
                    int floorMinChunks, Duration wallClockBudget, Duration censusTimeout,
                    Duration quarantineRetention, Set<String> floorExemptCollections,
                    Duration clientQuarantineRetention) {

        Settings {
            floorExemptCollections = Set.copyOf(floorExemptCollections);
        }

        /**
         * Whether the MOVE floor is waived for {@code collection} of {@code tenant}: an entry {@code tenant/collection}
         * waives it for that tenant only; a bare {@code collection} entry waives it for every tenant that has a
         * collection of that name (collection names are per-tenant, so {@code code__1-1__...} exists in many).
         */
        boolean isFloorExempt(String tenant, String collection) {
            return floorExemptCollections.contains(tenant + "/" + collection)
                || floorExemptCollections.contains(collection);
        }

        /** The settings without a retention override or a floor exemption: the 14 day defaults for both populations, no exemptions. */
        Settings(boolean enabled, Duration interval, int batchSize, double floorFraction, int floorMinChunks,
                 Duration wallClockBudget, Duration censusTimeout) {
            this(enabled, interval, batchSize, floorFraction, floorMinChunks, wallClockBudget, censusTimeout,
                DEFAULT_QUARANTINE_RETENTION, Set.of(), DEFAULT_CLIENT_QUARANTINE_RETENTION);
        }

        static final Duration DEFAULT_INTERVAL = Duration.ofHours(1);
        /** How long a chunk the reaper moved waits in quarantine before the engine expires it (Sam: 14 days). */
        static final Duration DEFAULT_QUARANTINE_RETENTION = Duration.ofDays(14);
        /**
         * How long a chunk a client moved into a {@code quarantine-knowledge__*} sibling waits before the engine
         * expires it (Sam, 2026-10-04): its own setting, because the client's {@code NX_GC_QUARANTINE_DAYS} is not
         * the engine's to read.
         */
        static final Duration DEFAULT_CLIENT_QUARANTINE_RETENTION = Duration.ofDays(14);
        static final long MAX_QUARANTINE_RETENTION_DAYS = 3650;
        static final double DEFAULT_FLOOR_FRACTION = 0.25;
        static final int DEFAULT_FLOOR_MIN_CHUNKS = 100;
        static final Duration DEFAULT_WALL_CLOCK_BUDGET = Duration.ofMinutes(10);
        static final Duration DEFAULT_CENSUS_TIMEOUT = Duration.ofSeconds(60);

        static Settings defaults() {
            return new Settings(true, DEFAULT_INTERVAL, MAX_BATCH_SIZE, DEFAULT_FLOOR_FRACTION,
                                DEFAULT_FLOOR_MIN_CHUNKS, DEFAULT_WALL_CLOCK_BUDGET, DEFAULT_CENSUS_TIMEOUT,
                                DEFAULT_QUARANTINE_RETENTION, Set.of(), DEFAULT_CLIENT_QUARANTINE_RETENTION);
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
                Duration.ofSeconds(positive(env, CENSUS_TIMEOUT_SECONDS_ENV, DEFAULT_CENSUS_TIMEOUT.toSeconds(), 3600)),
                Duration.ofDays(positive(env, QUARANTINE_RETENTION_DAYS_ENV, DEFAULT_QUARANTINE_RETENTION.toDays(),
                                         MAX_QUARANTINE_RETENTION_DAYS)),
                names(env.apply(FLOOR_EXEMPT_COLLECTIONS_ENV)),
                Duration.ofDays(positive(env, CLIENT_QUARANTINE_RETENTION_DAYS_ENV,
                                         DEFAULT_CLIENT_QUARANTINE_RETENTION.toDays(), MAX_QUARANTINE_RETENTION_DAYS)));
        }

        /**
         * A comma-separated list of {@code tenant/collection} entries (exempting that tenant's collection only) or
         * bare collection names (every tenant with a collection of that name; WARN at boot); blank entries are
         * dropped. A {@code quarantine-} collection is dropped with a warning: the reaper never visits a quarantine
         * sibling, so exempting one means nothing.
         */
        private static Set<String> names(String raw) {
            Set<String> out = new TreeSet<>();
            if (raw == null) return out;
            for (String part : raw.split(",")) {
                String entry = part.trim();
                if (entry.isEmpty()) continue;
                int slash = entry.lastIndexOf('/');
                String collection = slash < 0 ? entry : entry.substring(slash + 1);
                if (collection.isEmpty() || (slash == 0)) {
                    log.warn("event=reaper_setting_invalid name={} raw={} using=ignored expected=tenant/collection_or_collection",
                        FLOOR_EXEMPT_COLLECTIONS_ENV, entry);
                    continue;
                }
                if (collection.startsWith(QUARANTINE_PREFIX)) {
                    log.warn("event=reaper_setting_invalid name={} raw={} using=ignored expected=a_live_collection_name",
                        FLOOR_EXEMPT_COLLECTIONS_ENV, entry);
                    continue;
                }
                if (slash < 0) {
                    log.warn("event=reaper_floor_exempt_all_tenants name={} collection={} (a bare collection name waives "
                        + "the move floor in EVERY tenant that has it; write tenant/collection to scope it)",
                        FLOOR_EXEMPT_COLLECTIONS_ENV, entry);
                }
                out.add(entry);
            }
            return out;
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
        CENSUS_SCOPE_MISMATCH(true), FLOOR_EXCEEDED(true),
        /** A statement other than the census (the dry run, the move, one sibling's expiry) hit its bound (57014). */
        STATEMENT_TIMED_OUT(true),
        /** A manifest writer holds the per-collection sweep gate SHARED, so the exclusive acquire timed out. */
        GATE_BUSY(false),
        /** A lock wait other than the gate timed out: a chunk row a client is refreshing, or the sibling registration. */
        LOCK_TIMEOUT(false),
        /** The collection's census timed out {@value ChunkReaper#CENSUS_BACKOFF_AFTER} passes running and is resting. */
        CENSUS_BACKOFF(false),
        /** A statement other than the census timed out {@value ChunkReaper#TIMEOUT_BACKOFF_AFTER} passes running and is resting. */
        STATEMENT_BACKOFF(false);

        final boolean refusal;

        Refusal(boolean refusal) {
            this.refusal = refusal;
        }
    }

    /** One collection's outcome. {@code refusal} and {@code error} are null when absent. */
    record CollectionResult(String collection, long candidates, long total, long moved, Refusal refusal,
                            String error) {}

    /**
     * One quarantine sibling's expiry outcome. {@code protectedCount} counts chunks past the cutoff that a manifest
     * row of the origin still names (benign, labelled {@code expiry_protected}, never a refusal). {@code refusal}
     * and {@code error} are null when absent; {@code refusal} is a skip (a lock wait timed out, or the sibling is
     * resting after repeated timeouts) or {@link Refusal#STATEMENT_TIMED_OUT}. Expiry has no floor, so it has no
     * floor refusal. {@code expired} and {@code protectedCount} are what the sibling's earlier origins already
     * deleted or protected, kept when a later origin ends the pass with a refusal or an error (nexus-wbfpw.53).
     */
    record ExpiryResult(String quarantineCollection, long expired, long protectedCount,
                        Refusal refusal, String error) {}

    record TenantResult(String tenant, List<CollectionResult> collections, List<ExpiryResult> expiries,
                        List<ExpiryResult> clientExpiries, Refusal tenantRefusal, String error,
                        boolean wallClockCut) {
        CollectionResult collection(String name) {
            return collections.stream().filter(c -> c.collection().equals(name)).findFirst().orElse(null);
        }

        ExpiryResult expiry(String quarantineCollection) {
            return expiries.stream().filter(e -> e.quarantineCollection().equals(quarantineCollection))
                .findFirst().orElse(null);
        }

        /** The expiry of what a client moved (nexus-wbfpw.75), for a {@code quarantine-knowledge__*} sibling; null when none ran. */
        ExpiryResult clientExpiry(String quarantineCollection) {
            return clientExpiries.stream().filter(e -> e.quarantineCollection().equals(quarantineCollection))
                .findFirst().orElse(null);
        }

        long candidates() {
            return collections.stream().mapToLong(CollectionResult::candidates).sum();
        }

        long moved() {
            return collections.stream().mapToLong(CollectionResult::moved).sum();
        }

        /** Chunks the reaper moved that this pass expired. The client-moved ones are {@link #clientExpired()}. */
        long expired() {
            return expiries.stream().mapToLong(ExpiryResult::expired).sum();
        }

        /** Chunks a client moved that this pass expired from {@code quarantine-knowledge__*} siblings (nexus-wbfpw.75). */
        long clientExpired() {
            return clientExpiries.stream().mapToLong(ExpiryResult::expired).sum();
        }

        /** One gc_audit row per collection that moved anything. */
        int auditRows() {
            return (int) collections.stream().filter(c -> c.moved() > 0).count();
        }

        /** Decisions the reaper made about the data. A gate or lock skip is not one. */
        int refused() {
            return (int) collections.stream().filter(c -> c.refusal() != null && c.refusal().refusal).count()
                + (int) expiries.stream().filter(e -> e.refusal() != null && e.refusal().refusal).count()
                + (int) clientExpiries.stream().filter(e -> e.refusal() != null && e.refusal().refusal).count()
                + (tenantRefusal != null ? 1 : 0);
        }

        /** Collections and siblings that collided with a live writer, or are resting, and wait for a later pass. */
        int skipped() {
            return (int) collections.stream().filter(c -> c.refusal() != null && !c.refusal().refusal).count()
                + (int) expiries.stream().filter(e -> e.refusal() != null && !e.refusal().refusal).count()
                + (int) clientExpiries.stream().filter(e -> e.refusal() != null && !e.refusal().refusal).count();
        }

        /** Chunks past the retention window that a manifest row still names, summed over the siblings: benign. */
        long expiryProtected() {
            return expiries.stream().mapToLong(ExpiryResult::protectedCount).sum();
        }

        /** The same count for the client-moved population (nexus-wbfpw.75). */
        long clientExpiryProtected() {
            return clientExpiries.stream().mapToLong(ExpiryResult::protectedCount).sum();
        }

        /**
         * The tenant, one of its collections or one of its siblings threw. A wall-clock cut sets {@code error} to
         * explain itself and is not a failure; a tenant the gate refused has no error at all.
         */
        boolean failed() {
            return (error != null && !wallClockCut)
                || collections.stream().anyMatch(c -> c.error() != null)
                || expiries.stream().anyMatch(e -> e.error() != null)
                || clientExpiries.stream().anyMatch(e -> e.error() != null);
        }

        /**
         * The tenant passed the gate cleanly and held nothing to look at: no collection and no quarantine sibling
         * with chunks (nexus-wbfpw.73). Refused, errored and wall-clock-cut tenants are never empty.
         */
        boolean empty() {
            return tenantRefusal == null && error == null && collections.isEmpty() && expiries.isEmpty();
        }

        int errors() {
            return (int) collections.stream().filter(c -> c.error() != null).count()
                + (int) expiries.stream().filter(e -> e.error() != null).count()
                + (int) clientExpiries.stream().filter(e -> e.error() != null).count() + (error != null ? 1 : 0);
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
    /** When the last pass that ran to the end finished; null before the first. Read by the status route's thread. */
    private volatile Instant lastCompletedPassAt;
    /** That pass's tenant counts, set with {@link #lastCompletedPassAt} (the status route reads the two together). */
    private volatile LastPass lastPass;
    /** Passes since boot that died, or that could not list their tenants. */
    private final AtomicLong failedPassesTotal = new AtomicLong();
    /** Refusals since boot: a counter a log reader can alert on, where a one-off line would scroll past. */
    private final AtomicLong refusedTotal = new AtomicLong();
    /** Collections skipped since boot because a manifest writer held the sweep gate. Not a refusal. */
    private final AtomicLong gateBusyTotal = new AtomicLong();
    /** Collections skipped since boot because a lock wait other than the sweep gate timed out. Not a refusal. */
    private final AtomicLong lockTimeoutTotal = new AtomicLong();
    /** Census statements that hit their bound since boot (each also a refusal). */
    private final AtomicLong censusTimedOutTotal = new AtomicLong();
    /** Collections skipped since boot because their census is resting after repeated timeouts. Not a refusal. */
    private final AtomicLong censusBackoffTotal = new AtomicLong();
    /** Non-census statements (dry run, move, expiry) that hit their bound since boot (each also a refusal). */
    private final AtomicLong statementTimedOutTotal = new AtomicLong();
    /** Collections and siblings skipped since boot because a non-census statement is resting after timeouts. */
    private final AtomicLong statementBackoffTotal = new AtomicLong();
    private final AtomicReference<RunResult> lastRun = new AtomicReference<>();
    private final AtomicLong passNumber = new AtomicLong();
    /**
     * {@code tenant/collection#stage} to its timeout streak, for the stages {@code census}, {@code probe},
     * {@code move} and {@code expire} (the key is the sibling's name for expiry); removed when that statement
     * completes or its reason is gone (nothing reapable).
     */
    private final Map<String, TimeoutStreak> timeoutStreaks = new ConcurrentHashMap<>();
    /** Where the last wall-clock cut stopped, so the next pass resumes there and nothing is starved. */
    private volatile String resumeTenant;
    private final Map<String, String> resumeCollection = new ConcurrentHashMap<>();
    private final Map<String, String> resumeExpiry = new ConcurrentHashMap<>();

    /** Consecutive timeouts of one statement, and the last pass it is skipped through. */
    private static final class TimeoutStreak {
        int consecutive;
        long skipThroughPass;
    }

    private static final String CENSUS_STAGE = "#census";
    private static final String PROBE_STAGE = "#probe";
    private static final String MOVE_STAGE = "#move";
    private static final String EXPIRE_STAGE = "#expire";
    /** The client-moved population's own timeout streak, so its rest never silences the reaper's own expiry. */
    private static final String EXPIRE_CLIENT_STAGE = "#expire-client";

    /** Passes left (counting {@code pass}) that the statement under {@code key} rests; 0 when it is not resting. */
    private long restLeft(String key, long pass) {
        TimeoutStreak streak = timeoutStreaks.get(key);
        return streak != null && pass <= streak.skipThroughPass ? streak.skipThroughPass - pass + 1 : 0;
    }

    /**
     * Record one more timeout of the statement under {@code key} on pass {@code pass}. From the
     * {@value #TIMEOUT_BACKOFF_AFTER}rd in a row it rests for 2^k passes, at most 24 hours of passes.
     * Returns the passes it now rests (0 before the third).
     */
    private long noteTimeout(String key, long pass) {
        TimeoutStreak streak = timeoutStreaks.computeIfAbsent(key, k -> new TimeoutStreak());
        streak.consecutive++;
        long rest = 0;
        if (streak.consecutive >= TIMEOUT_BACKOFF_AFTER) {
            long k = streak.consecutive - TIMEOUT_BACKOFF_AFTER + 1L;
            long cap = Math.max(1L, MAX_CENSUS_BACKOFF.toSeconds() / Math.max(1L, settings.interval().toSeconds()));
            rest = Math.min(1L << Math.min(k, 30L), cap);
            streak.skipThroughPass = pass + rest;
        }
        return rest;
    }

    private static String streakDetail(long consecutive, long rest) {
        return " consecutive=" + consecutive + (rest > 0 ? " it rests for the next " + rest + " pass(es)" : "");
    }

    /**
     * The empty-tenant test the production gate is built with (nexus-wbfpw.73): no chunk row in {@code nexus.chunks},
     * read under the tenant's RLS context with the same statement bound as the reaper's own enumeration. The
     * manifest is not read: a manifest row cannot outlive its chunk (see {@code ReaperRepository#holdsNothing}).
     */
    static java.util.function.Predicate<String> emptyTenantProbe(ReaperRepository store) {
        return tenant -> store.holdsNothing(tenant, Duration.ofMillis(STATEMENT_TIMEOUT_MS));
    }

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

    /** Census statements that hit their bound since boot. */
    long censusTimedOutTotal() {
        return censusTimedOutTotal.get();
    }

    /** Collections skipped since boot because their census was resting after repeated timeouts. */
    long censusBackoffTotal() {
        return censusBackoffTotal.get();
    }

    /** Non-census statements (dry run, move, expiry) that hit their bound since boot. */
    long statementTimedOutTotal() {
        return statementTimedOutTotal.get();
    }

    /** Collections and siblings skipped since boot because a non-census statement was resting. */
    long statementBackoffTotal() {
        return statementBackoffTotal.get();
    }

    /** The last completed pass, or null before the first. */
    RunResult lastRun() {
        return lastRun.get();
    }

    /**
     * When the last pass that RAN TO THE END finished (the reaper's clock), or null before the first. A pass that
     * died (an {@link Error} out of a statement, the tenant list failing) is not a completed pass and does not move
     * this: it is the one trace of the reaper being alive that a {@code candidates=0} pass leaves, since such a
     * pass writes no {@code gc_audit} row (nexus-wbfpw.56). Served as {@code reaper.last_completed_pass_at} on
     * {@code GET /v1/status}.
     */
    Instant lastCompletedPassAt() {
        return lastCompletedPassAt;
    }

    /**
     * What the last pass that ran to the end did with the tenants it visited (nexus-wbfpw.55 round 2, critique S4a):
     * {@code lastCompletedPassAt} moves for ANY pass that reaches the end, so a reaper whose every tenant is refused
     * (the RDR-192 backfill rung not run) or whose every collection errors (a grants regression) still reads alive.
     * These counts tell a working pass from one that did nothing useful. A tenant is refused when the backfill gate
     * kept the whole tenant out, and errored when it, one of its collections or one of its quarantine siblings threw
     * (a wall-clock cut is neither). A tenant that is neither is one the pass worked on, even if it found nothing to
     * move. {@code tenantsEmpty} (nexus-wbfpw.73, appended) counts the visited tenants that were neither refused nor
     * errored and held no chunk in any collection or quarantine sibling: the default tenant is always visited and is
     * empty in cloud, so a client judging "did this pass work on anything" subtracts it from the visited count.
     * Served as {@code reaper.last_pass} on {@code GET /v1/status}.
     */
    record LastPass(int tenantsVisited, int tenantsErrored, int tenantsRefused, int tenantsEmpty) {
        int tenantsOk() {
            return tenantsVisited - tenantsErrored - tenantsRefused;
        }
    }

    /** The last pass that ran to the end, summarised; null before the first. */
    LastPass lastPass() {
        return lastPass;
    }

    /** Passes since boot that died or could not list their tenants; a pass whose collections failed one by one is not one. */
    long failedPassesTotal() {
        return failedPassesTotal.get();
    }

    /** One scheduled pass: the predicate's own 30 day grace. */
    RunResult run() {
        return runOnce(null);
    }

    /**
     * One pass over every tenant, with {@code grace} injected: null is the predicate's default (what production
     * runs), anything else is for tests. Never throws, not even an {@link Error}: a failure is a counted, logged
     * outcome (nexus-wbfpw.56; this said "never throws" while catching RuntimeException only, and an Error that got
     * out of the scheduled task ended every later run of the schedule, silently, on the thread the T1 and tuple
     * sweeps share).
     */
    RunResult runOnce(Duration grace) {
        try {
            return runPass(grace);
        } catch (Throwable t) {
            long n = failedPassesTotal.incrementAndGet();
            log.error("event=reaper_pass_failed error_class={} error={} failed_passes_total={}",
                t.getClass().getName(), t.getMessage(), n, t);
            RunResult failed = new RunResult(List.of(), false);
            lastRun.set(failed);
            return failed;
        }
    }

    private RunResult runPass(Duration grace) {
        long deadline = nanos.getAsLong() + settings.wallClockBudget().toNanos();
        long pass = passNumber.incrementAndGet();
        List<TenantResult> results = new ArrayList<>();
        boolean cut = false;
        String cutAt = null;
        Collection<String> tenantIds;
        try {
            // Sorted here, in Java, with the same ordering the resume rotation compares by: the supplier's order
            // is whatever the database returned (SELECT DISTINCT has no ORDER BY).
            List<String> ordered = new ArrayList<>(new TreeSet<>(tenants.get()));
            tenantIds = rotated(ordered, resumeTenant);
        } catch (RuntimeException e) {
            // A pass that could not list its tenants is a failed pass, logged as one (nexus-wbfpw.67, conexus-lv6t):
            // it used to log a WARN event=reaper_run with errors=1, which an alert on reaper_pass_failed missed and a
            // heartbeat on reaper_run read as alive. Same fields as runOnce's line for an escaped Throwable, in the same order,
            // with stage= appended last so a filter written against that line matches this one too.
            long n = failedPassesTotal.incrementAndGet();
            log.error("event=reaper_pass_failed error_class={} error={} failed_passes_total={} stage=tenant_list",
                e.getClass().getName(), e.getMessage(), n, e);
            RunResult failed = new RunResult(List.of(), false);
            lastRun.set(failed);
            return failed;
        }
        int unvisited = 0;
        for (String tenant : tenantIds) {
            if (cut || nanos.getAsLong() >= deadline) {
                if (!cut) cutAt = tenant;
                cut = true;
                unvisited++;
                continue;
            }
            TenantResult r = passTenant(tenant, grace, deadline, pass);
            results.add(r);
            if (r.wallClockCut()) {
                cut = true;
                cutAt = tenant;
            }
        }
        // A cut resumes where it stopped, not at the front: otherwise whatever sorts first is visited every hour
        // and whatever sorts last is starved for as long as the first ones are slow.
        resumeTenant = cut ? cutAt : null;
        if (unvisited > 0) {
            log.warn("event=reaper_wall_clock_cut tenants_unvisited={}", unvisited);
        }
        long candidates = results.stream().mapToLong(TenantResult::candidates).sum();
        long moved = results.stream().mapToLong(TenantResult::moved).sum();
        long expired = results.stream().mapToLong(TenantResult::expired).sum();
        long expiryProtected = results.stream().mapToLong(TenantResult::expiryProtected).sum();
        long clientExpired = results.stream().mapToLong(TenantResult::clientExpired).sum();
        long clientExpiryProtected = results.stream().mapToLong(TenantResult::clientExpiryProtected).sum();
        int auditRows = results.stream().mapToInt(TenantResult::auditRows).sum();
        int refused = results.stream().mapToInt(TenantResult::refused).sum();
        int skipped = results.stream().mapToInt(TenantResult::skipped).sum();
        int errors = results.stream().mapToInt(TenantResult::errors).sum();
        log.info("event=reaper_run tenants={} candidates={} moved={} audit_rows={} expired={} expiry_protected={} "
                + "client_expired={} client_expiry_protected={} "
                + "refused={} skipped={} errors={} wall_clock_cut={} refused_total={} census_timed_out_total={} "
                + "statement_timed_out_total={}",
            results.size(), candidates, moved, auditRows, expired, expiryProtected, clientExpired,
            clientExpiryProtected, refused, skipped, errors, cut,
            refusedTotal.get(), censusTimedOutTotal.get(), statementTimedOutTotal.get());
        RunResult out = new RunResult(results, cut);
        lastRun.set(out);
        lastPass = new LastPass(results.size(), (int) results.stream().filter(TenantResult::failed).count(),
            (int) results.stream().filter(r -> r.tenantRefusal() != null).count(),
            (int) results.stream().filter(TenantResult::empty).count());
        lastCompletedPassAt = clock.instant().truncatedTo(ChronoUnit.SECONDS);
        return out;
    }

    /** {@code items} starting at the first element equal to {@code resume} (all of them when it is null or absent). */
    private static List<String> rotated(List<String> items, String resume) {
        int at = resume == null ? -1 : items.indexOf(resume);
        if (at <= 0) return items;
        List<String> out = new ArrayList<>(items.subList(at, items.size()));
        out.addAll(items.subList(0, at));
        return out;
    }

    /** The sorted {@code names} rotated to start at the first name not before {@code resume} (a name since deleted). */
    private static List<String> rotatedFrom(List<String> names, String resume) {
        if (resume == null) return names;
        int at = 0;
        while (at < names.size() && names.get(at).compareTo(resume) < 0) at++;
        if (at == 0 || at >= names.size()) return names;
        List<String> out = new ArrayList<>(names.subList(at, names.size()));
        out.addAll(names.subList(0, at));
        return out;
    }

    private TenantResult passTenant(String tenant, Duration grace, long deadlineNanos, long pass) {
        try {
            gate.requireComplete(tenant);
        } catch (BackfillIncompleteException e) {
            long n = refusedTotal.incrementAndGet();
            log.warn("event=reaper_tenant_refused tenant={} reason={} refused_total={} detail={}",
                tenant, Refusal.BACKFILL_INCOMPLETE, n, e.getMessage());
            recordRefusal(tenant, "", Refusal.BACKFILL_INCOMPLETE, 0, 0, e.getMessage(), List.of());
            return logPass(new TenantResult(tenant, List.of(), List.of(), List.of(), Refusal.BACKFILL_INCOMPLETE, null,
                false));
        }

        List<CollectionResult> out = new ArrayList<>();
        List<ExpiryResult> expiries = new ArrayList<>();
        List<ExpiryResult> clientExpiries = new ArrayList<>();
        String tenantError = null;
        boolean cut = false;
        try {
            // Sorted in Java (the SQL's ORDER BY uses the database collation, the resume rotation compares by
            // String.compareTo, and the two can disagree on where "resume at X" falls).
            List<String> names = store.collectionsWithChunks(tenant, Duration.ofMillis(STATEMENT_TIMEOUT_MS)).stream()
                .sorted().toList();
            Map<String, String> states = store.lifecycleStates(tenant);
            List<String> quarantines = rotatedFrom(
                names.stream().filter(n -> n.startsWith(QUARANTINE_PREFIX)).toList(), resumeExpiry.get(tenant));
            List<String> live = rotatedFrom(
                names.stream().filter(n -> !n.startsWith(QUARANTINE_PREFIX)).toList(), resumeCollection.get(tenant));
            String firstUnvisited = null;

            // Expire first, so the floor judges the quarantine as it stood before this pass adds to it.
            resumeExpiry.remove(tenant);
            for (String name : quarantines) {
                if (nanos.getAsLong() >= deadlineNanos) {
                    cut = true;
                    resumeExpiry.put(tenant, name);
                    break;
                }
                expiries.add(expire(tenant, name, states, pass));
                // The same sibling, the other population: what a client moved, for knowledge origins only
                // (nexus-wbfpw.75). A sibling with no such origin yields nothing (null) and is not listed.
                ExpiryResult clientResult = expireClientMoved(tenant, name, states, pass);
                if (clientResult != null) {
                    clientExpiries.add(clientResult);
                }
            }
            for (String name : live) {
                if (cut || nanos.getAsLong() >= deadlineNanos) {
                    cut = true;
                    firstUnvisited = name;
                    break;
                }
                out.add(passCollection(tenant, name, states, grace, pass));
            }
            if (firstUnvisited != null) {
                resumeCollection.put(tenant, firstUnvisited);
            } else {
                resumeCollection.remove(tenant);
            }
            if (cut) {
                tenantError = "wall-clock budget spent; the remaining collections wait for the next pass";
                log.warn("event=reaper_wall_clock_cut tenant={} collections_done={}", tenant, out.size());
            }
        } catch (RuntimeException e) {
            tenantError = e.getMessage();
            log.warn("event=reaper_tenant_failed tenant={} error={}", tenant, e.getMessage(), e);
        }
        return logPass(new TenantResult(tenant, out, expiries, clientExpiries, null, tenantError, cut));
    }

    /** The move's and the census-gate's floor rule, one definition: at least the minimum, and MORE than the fraction. */
    static boolean floorTrips(long reapable, long total, double fraction, int minChunks) {
        return total > 0 && reapable >= minChunks && (double) reapable / (double) total > fraction;
    }

    private CollectionResult passCollection(String tenant, String name, Map<String, String> states, Duration grace,
                                            long pass) {
        if (!states.containsKey(name)) {
            return refuse(tenant, name, Refusal.UNREGISTERED_COLLECTION, 0, 0, "no catalog_collections row", List.of());
        }
        String state = states.get(name);
        if (!LIVE.equals(state)) {
            return refuse(tenant, name, Refusal.COLLECTION_NOT_LIVE, 0, 0, "lifecycle_state=" + state, List.of());
        }
        String base = tenant + "/" + name;
        // A dry run or a move that timed out three passes running rests too: the same streak and cap as the census.
        long resting = Math.max(restLeft(base + PROBE_STAGE, pass), restLeft(base + MOVE_STAGE, pass));
        if (resting > 0) {
            return skipStatementBackoff(tenant, name, resting);
        }
        String stage = PROBE_STAGE;
        try {
            ReaperRepository.Pass probe = store.probe(tenant, name, grace, STATEMENT_TIMEOUT_MS);
            timeoutStreaks.remove(base + PROBE_STAGE);   // the dry run completed
            String streakKey = base + CENSUS_STAGE;
            if (probe.reapable() == 0) {
                timeoutStreaks.remove(streakKey);   // nothing to census: the reason for the streak is gone
                timeoutStreaks.remove(base + MOVE_STAGE);
                return new CollectionResult(name, 0, probe.total(), 0, null, null);
            }

            // The move floor, with one named exemption (NX_REAPER_FLOOR_EXEMPT_COLLECTIONS): a collection that is
            // legitimately mostly garbage is drained through it without turning the floor off for everything else.
            // Fraction 1.0 never trips (a ratio is never above 1), here and in the move function. The exemption is
            // logged on every pass that uses it, and the move's gc_audit row records floor_fraction 1.0.
            boolean exempt = settings.isFloorExempt(tenant, name);
            double fraction = exempt ? 1.0 : settings.floorFraction();
            if (exempt) {
                log.warn("event=reaper_floor_exempt tenant={} collection={} reapable={} total={} "
                        + "would_have_refused={} (NX_REAPER_FLOOR_EXEMPT_COLLECTIONS names this collection)",
                    tenant, name, probe.reapable(), probe.total(),
                    floorTrips(probe.reapable(), probe.total(), settings.floorFraction(), settings.floorMinChunks()));
            }

            // The floor first, on the probe's own counts: a collection the floor will refuse anyway must not pay
            // for a census every hour. The move re-judges the same rule under the sweep gate.
            if (floorTrips(probe.reapable(), probe.total(), fraction, settings.floorMinChunks())) {
                return refuse(tenant, name, Refusal.FLOOR_EXCEEDED, probe.reapable(), probe.total(), floorDetail(),
                    sampleReapable(tenant, name, grace));
            }

            // A collection whose census timed out three passes running rests for 2^k passes: the census is the
            // expensive step and a pathological collection would otherwise spend the shared thread every pass.
            long censusRest = restLeft(streakKey, pass);
            if (censusRest > 0) {
                return skipBackoff(tenant, name, probe, censusRest);
            }

            // The census, in the engine, on every pass that would move something. The stored ladder record is not
            // enough: nothing revokes it, so a tenant that later gains legacy-shaped chunks keeps an open gate.
            ManifestLessCensusResult c;
            try {
                c = census.run(tenant, name, CENSUS_PAGE, settings.censusTimeout());
            } catch (RuntimeException e) {
                if ("57014".equals(sqlState(e))) {
                    return censusTimedOut(tenant, name, probe, streakKey, pass);
                }
                throw e;
            }
            timeoutStreaks.remove(streakKey);   // it completed, whatever it said: the streak is over
            // Non-vacuity: a census that read a different set than the one being judged (a wrong tenant or an
            // unset RLS GUC reads 0) proves nothing, and its zero must not be taken for a clean collection.
            // Growth is not a mismatch (nexus-wbfpw.56, gate M6): a chunk written between the dry run and the census,
            // up to the census bound apart, makes the census read MORE, and refusing for it made every pass on a busy
            // collection refuse and write an audit row per state change. Growth is safe: the census classifies every
            // chunk it reads (a new legacy-shaped one refuses below) and the move re-judges the rule under the gate.
            // A census that read FEWER chunks than the dry run (a wrong tenant or an unset RLS GUC reads 0, a
            // concurrent delete reads fewer) is still refused: its buckets describe a different set.
            if (c.scopeChunkTotal() < probe.total()) {
                return refuse(tenant, name, Refusal.CENSUS_SCOPE_MISMATCH, probe.reapable(), probe.total(),
                    "census saw scope_chunk_total=" + c.scopeChunkTotal() + " but the dry run counted " + probe.total()
                        + " chunks; chunks were removed between the two reads, or the census read the wrong scope. "
                        + "Retried next pass", List.of());
            }
            if (c.scopeChunkTotal() > probe.total()) {
                // Growth proceeds, but a census that read MANY times what the dry run counted is the shape of a
                // different, larger set (a wrong scope that happens to be bigger), which this shrink-only check
                // cannot tell from a busy collection. So it is said out loud, not silently accepted.
                boolean muchLarger = c.scopeChunkTotal() >= CENSUS_GROWTH_WARN_FACTOR * probe.total()
                    && c.scopeChunkTotal() - probe.total() >= CENSUS_GROWTH_WARN_MIN_EXTRA;
                if (muchLarger) {
                    log.warn("event=reaper_census_scope_much_larger tenant={} collection={} dry_run_total={} "
                            + "census_total={} (the census read at least {}x the chunks the dry run counted and {} "
                            + "more; the move proceeds, but check the census is reading this collection)",
                        tenant, name, probe.total(), c.scopeChunkTotal(), CENSUS_GROWTH_WARN_FACTOR,
                        CENSUS_GROWTH_WARN_MIN_EXTRA);
                } else {
                    log.info("event=reaper_census_scope_grew tenant={} collection={} dry_run_total={} census_total={}",
                        tenant, name, probe.total(), c.scopeChunkTotal());
                }
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

            stage = MOVE_STAGE;
            ReaperRepository.Pass move = store.move(tenant, name, QUARANTINE_PREFIX + name,
                quarantinedAt(), settings.batchSize(), grace, fraction,
                settings.floorMinChunks(), STATEMENT_TIMEOUT_MS, LOCK_TIMEOUT_MS);
            timeoutStreaks.remove(base + MOVE_STAGE);   // the move completed (a floor refusal is still a completed statement)
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
            if ("57014".equals(sqlState(e))) {
                // The dry run or the move hit its 25 s bound: a counted refusal, audited once, resting after the
                // third in a row, never a stack trace every hour.
                return statementTimedOut(tenant, name, stage, base + stage, pass);
            }
            log.warn("event=reaper_collection_failed tenant={} collection={} error={}", tenant, name, e.getMessage(), e);
            return new CollectionResult(name, 0, 0, 0, null, String.valueOf(e.getMessage()));
        }
    }

    /** A census that hit its bound: a counted refusal, and after the third in a row a rest of 2^k passes. */
    private CollectionResult censusTimedOut(String tenant, String name, ReaperRepository.Pass probe, String streakKey,
                                            long pass) {
        long timedOut = censusTimedOutTotal.incrementAndGet();
        long rest = noteTimeout(streakKey, pass);
        return refuse(tenant, name, Refusal.CENSUS_TIMED_OUT, probe.reapable(), probe.total(),
            "census statement exceeded " + settings.censusTimeout().toSeconds() + "s; it was not read, so nothing is "
                + "moved; census_timed_out_total=" + timedOut + streakDetail(timeoutStreaks.get(streakKey).consecutive, rest),
            List.of());
    }

    /**
     * The dry run or the move hit its statement bound: a counted refusal (nothing was moved, or the move rolled
     * back), audited once per state change, resting after the third in a row like the census.
     */
    private CollectionResult statementTimedOut(String tenant, String name, String stage, String key, long pass) {
        long timedOut = statementTimedOutTotal.incrementAndGet();
        long rest = noteTimeout(key, pass);
        return refuse(tenant, name, Refusal.STATEMENT_TIMED_OUT, 0, 0,
            "the " + stage.substring(1) + " statement exceeded its " + (STATEMENT_TIMEOUT_MS / 1000) + "s bound; "
                + "nothing was moved by it; statement_timed_out_total=" + timedOut
                + streakDetail(timeoutStreaks.get(key).consecutive, rest),
            List.of());
    }

    /** A collection whose dry run or move is resting after repeated timeouts: counted on its own, INFO, never audited. */
    private CollectionResult skipStatementBackoff(String tenant, String name, long passesLeft) {
        long n = statementBackoffTotal.incrementAndGet();
        log.info("event=reaper_collection_skipped tenant={} collection={} reason={} statement_backoff_total={} "
                + "passes_left={} detail={}",
            tenant, name, Refusal.STATEMENT_BACKOFF, n, passesLeft,
            "its dry run or move timed out " + TIMEOUT_BACKOFF_AFTER + " or more passes running; it rests, then is retried");
        return new CollectionResult(name, 0, 0, 0, Refusal.STATEMENT_BACKOFF, null);
    }

    /** A collection resting after repeated census timeouts: counted on its own, INFO, never audited. */
    private CollectionResult skipBackoff(String tenant, String name, ReaperRepository.Pass probe, long passesLeft) {
        long n = censusBackoffTotal.incrementAndGet();
        log.info("event=reaper_collection_skipped tenant={} collection={} reason={} census_backoff_total={} "
                + "passes_left={} detail={}",
            tenant, name, Refusal.CENSUS_BACKOFF, n, passesLeft,
            "the census timed out " + CENSUS_BACKOFF_AFTER + " or more passes running; it rests, then is retried "
                + "(raise NX_REAPER_CENSUS_TIMEOUT_SECONDS to give it room)");
        return new CollectionResult(name, probe.reapable(), probe.total(), 0, Refusal.CENSUS_BACKOFF, null);
    }

    /**
     * The stamp the expiry compares against: whole seconds, {@code yyyy-MM-ddTHH:mm:ssZ}, the shape
     * the Python indexer writes. An {@code Instant.toString()} carries fractional seconds, which sort differently
     * from the cutoff's and expire a chunk early.
     */
    private String quarantinedAt() {
        return clock.instant().truncatedTo(ChronoUnit.SECONDS).toString();
    }

    private String floorDetail() {
        return "floor_fraction=" + settings.floorFraction() + " floor_min_chunks=" + settings.floorMinChunks();
    }

    /** The origins of the rows one expiry population owns in one sibling. */
    @FunctionalInterface
    private interface OriginLister {
        List<String> list(String tenant, String quarantine);
    }

    /** One expiry call for one origin of a sibling: the cutoff in, what it deleted and protected out. */
    @FunctionalInterface
    private interface OriginExpirer {
        ReaperRepository.Expiry expire(String tenant, String quarantine, String origin, String cutoff);
    }

    /**
     * One population of quarantine rows the engine expires, so the two share one body: the statement streak, the
     * lock and timeout classification, the resume and the registered-origin check are the same code. {@code stage}
     * keys the timeout streak ({@link #EXPIRE_STAGE} or {@link #EXPIRE_CLIENT_STAGE}), {@code variant} is spliced
     * into the log event names ("" keeps the reaper's own, {@code client_} names the client-moved population),
     * and {@code keepFor} is that population's own setting.
     */
    private record Population(String stage, String variant, Duration keepFor,
                              OriginLister origins, OriginExpirer expirer) {
        /** The client-moved population is looked for in every sibling and reported only where it has an origin. */
        boolean quietWhenNoOrigins() {
            return !variant.isEmpty();
        }
    }

    /** The rows the reaper moved: tagged, {@link Settings#quarantineRetention}, {@code reaper_expire_quarantine}. */
    private Population reaperMoved() {
        return new Population(EXPIRE_STAGE, "", settings.quarantineRetention(),
            (t, q) -> store.taggedOrigins(t, q, STATEMENT_TIMEOUT_MS),
            (t, q, origin, cutoff) -> store.expire(t, q, origin, cutoff, MAX_EXPIRY_ROWS, STATEMENT_TIMEOUT_MS,
                LOCK_TIMEOUT_MS));
    }

    /**
     * The rows a client moved, knowledge origins only: {@link Settings#clientQuarantineRetention},
     * {@code reaper_expire_client_quarantine} (nexus-wbfpw.75).
     */
    private Population clientMoved() {
        return new Population(EXPIRE_CLIENT_STAGE, "client_", settings.clientQuarantineRetention(),
            (t, q) -> store.clientMovedOrigins(t, q, CLIENT_EXPIRY_CONTENT_TYPE, STATEMENT_TIMEOUT_MS),
            (t, q, origin, cutoff) -> store.expireClientMoved(t, q, origin, cutoff, MAX_EXPIRY_ROWS,
                STATEMENT_TIMEOUT_MS, LOCK_TIMEOUT_MS));
    }

    /**
     * Expire the chunks this class moved into one quarantine sibling: tagged chunks older than
     * {@link Settings#quarantineRetention}, never one a manifest row of their origin names, at most
     * {@link #MAX_EXPIRY_ROWS} per origin per pass. There is no floor (Sam, 2026-10-01). The origin of a chunk is
     * read from the chunk's own {@code origin_collection} tag, never parsed out of the sibling's name. Quarantine a
     * client filled carries no tag and is not touched here; see {@link #expireClientMoved}.
     */
    private ExpiryResult expire(String tenant, String quarantine, Map<String, String> states, long pass) {
        return expirePopulation(reaperMoved(), tenant, quarantine, states, pass);
    }

    /**
     * Expire the chunks a client moved into one quarantine sibling (Sam, 2026-10-04, nexus-wbfpw.75), so a
     * {@code quarantine-knowledge__*} sibling no longer waits for a hand-run {@code nx t3 gc}: rows the reaper
     * does not own whose {@code origin_collection} tag names a registered origin with catalog content type
     * knowledge, older than
     * {@link Settings#clientQuarantineRetention} by their own {@code quarantined_at} stamp, never one a manifest
     * row of the origin names, at most {@link #MAX_EXPIRY_ROWS} per origin per pass, no floor, audited as
     * {@value #AUDIT_EXPIRED_CLIENT}. Code, docs and rdr siblings have no such origin and yield null: the client
     * expires those on every {@code nx index repo} at the user's own {@code NX_GC_QUARANTINE_DAYS}, which the
     * engine cannot see. Returns null when the sibling holds no client-moved row of a knowledge origin.
     */
    private ExpiryResult expireClientMoved(String tenant, String quarantine, Map<String, String> states, long pass) {
        return expirePopulation(clientMoved(), tenant, quarantine, states, pass);
    }

    private ExpiryResult expirePopulation(Population pop, String tenant, String quarantine,
                                          Map<String, String> states, long pass) {
        String key = tenant + "/" + quarantine + pop.stage();
        String v = pop.variant();
        long resting = restLeft(key, pass);
        if (resting > 0) {
            long n = statementBackoffTotal.incrementAndGet();
            log.info("event=reaper_{}expire_skipped tenant={} quarantine={} reason={} statement_backoff_total={} "
                    + "passes_left={} detail={}", v, tenant, quarantine, Refusal.STATEMENT_BACKOFF, n, resting,
                "its expiry timed out " + TIMEOUT_BACKOFF_AFTER + " or more passes running; it rests, then is retried");
            return new ExpiryResult(quarantine, 0, 0, Refusal.STATEMENT_BACKOFF, null);
        }
        String cutoff = clock.instant().minus(pop.keepFor()).truncatedTo(ChronoUnit.SECONDS).toString();
        // Kept outside the try: a refusal or a failure on a LATER origin must report what the earlier origins of the
        // same sibling already deleted (their rows are gone and audited; nexus-wbfpw.53 fixed the result and the log
        // dropping them).
        long expired = 0;
        long protectedCount = 0;
        List<String> origins = null;
        try {
            origins = pop.origins().list(tenant, quarantine);
            if (origins.isEmpty() && pop.quietWhenNoOrigins()) {
                timeoutStreaks.remove(key);
                return null;
            }
            for (String origin : origins) {
                if (!states.containsKey(origin)) {
                    // An origin with no catalog row is a catalog anomaly or a retired collection; nothing is expired
                    // for it (an operator should look at it first), and nothing is audited as refused.
                    log.info("event=reaper_{}expire_skipped tenant={} quarantine={} origin={} reason=origin_not_registered",
                        v, tenant, quarantine, origin);
                    continue;
                }
                ReaperRepository.Expiry out = pop.expirer().expire(tenant, quarantine, origin, cutoff);
                expired += out.expired();
                protectedCount += out.protectedCount();
            }
            timeoutStreaks.remove(key);
            // protected > 0 is benign (a chunk a manifest row of the origin names again is never deleted) and is
            // labelled on its own: not a refusal, not counted in refused_total, not a WARN every hour.
            if (expired > 0) {
                log.info("event=reaper_{}expired tenant={} quarantine={} expired={} expiry_protected={} cutoff={}",
                    v, tenant, quarantine, expired, protectedCount, cutoff);
            }
            return new ExpiryResult(quarantine, expired, protectedCount, null, null);
        } catch (RuntimeException e) {
            if ("55P03".equals(sqlState(e))) {
                // A row lock (a client re-referencing or refreshing a chunk) timed out: routine contention that
                // clears itself, counted with the other lock skips, never an error and never a refusal.
                long n = lockTimeoutTotal.incrementAndGet();
                log.info("event=reaper_{}expire_skipped tenant={} quarantine={} reason={} lock_timeout_total={} "
                        + "expired_before_refusal={} expiry_protected_before_refusal={} detail={}", v, tenant,
                    quarantine, Refusal.LOCK_TIMEOUT, n, expired, protectedCount,
                    "a lock wait timed out during expiry; retried next pass");
                return new ExpiryResult(quarantine, expired, protectedCount, Refusal.LOCK_TIMEOUT, null);
            }
            if ("57014".equals(sqlState(e))) {
                // The expiry (or the listing of the sibling's origins) hit its bound: a counted refusal, audited
                // once under the sibling's name, resting after the third in a row.
                long timedOut = statementTimedOutTotal.incrementAndGet();
                long rest = noteTimeout(key, pass);
                if (origins == null && pop.quietWhenNoOrigins()) {
                    // The client-moved population looks in EVERY sibling, code, docs and rdr ones included, and the
                    // listing is where it timed out, so nothing says this sibling is a knowledge one: log only. No
                    // refusal, no gc_audit row (a refusal row would be filed against a sibling the engine never
                    // expires from), and it rests like any timed-out statement.
                    log.warn("event=reaper_{}expire_timed_out tenant={} quarantine={} statement_timed_out_total={} "
                            + "detail={}", v, tenant, quarantine, timedOut,
                        "listing the origins of the client-moved rows exceeded its " + (STATEMENT_TIMEOUT_MS / 1000)
                            + "s bound" + streakDetail(timeoutStreaks.get(key).consecutive, rest));
                    return null;
                }
                long n = refusedTotal.incrementAndGet();
                String detail = "the " + (v.isEmpty() ? "" : "client-moved ") + "expiry statement exceeded its "
                    + (STATEMENT_TIMEOUT_MS / 1000) + "s bound and "
                    + (expired > 0 ? "deleted " + expired + " chunk(s) of the origins it reached first, none after"
                        : "deleted nothing")
                    + "; statement_timed_out_total=" + timedOut
                    + streakDetail(timeoutStreaks.get(key).consecutive, rest);
                log.warn("event=reaper_{}expire_refused tenant={} quarantine={} reason={} refused_total={} "
                        + "expired_before_refusal={} expiry_protected_before_refusal={} detail={}", v, tenant,
                    quarantine, Refusal.STATEMENT_TIMED_OUT, n, expired, protectedCount, detail);
                recordRefusal(tenant, quarantine, Refusal.STATEMENT_TIMED_OUT, 0, 0, detail, List.of());
                return new ExpiryResult(quarantine, expired, protectedCount, Refusal.STATEMENT_TIMED_OUT, null);
            }
            log.warn("event=reaper_{}expire_failed tenant={} quarantine={} expired_before_refusal={} "
                    + "expiry_protected_before_refusal={} error={}", v, tenant, quarantine, expired, protectedCount,
                e.getMessage(), e);
            return new ExpiryResult(quarantine, expired, protectedCount, null, String.valueOf(e.getMessage()));
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
            if (op.equals(AUDIT_REFUSED)) {
                return r.get("details") instanceof Map<?, ?> d ? String.valueOf(d.get("reason")) : "";
            }
            // Any other reaper row (a move, an expiry) is a state change: a refusal after it is news again.
            if (op.startsWith("reaper_")) return "";
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
        log.info("event=reaper_pass tenant={} collections={} candidates={} moved={} audit_rows={} expired={} "
                + "expiry_protected={} client_expired={} client_expiry_protected={} refused={} skipped={} "
                + "errors={} wall_clock_cut={} error={}",
            r.tenant(), r.collections().size(), r.candidates(), r.moved(), r.auditRows(), r.expired(),
            r.expiryProtected(), r.clientExpired(), r.clientExpiryProtected(), r.refused(), r.skipped(),
            r.errors(), r.wallClockCut(), r.error());
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
