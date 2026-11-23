// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.vectors.PciBuilderSession.BuilderState;
import dev.nexus.service.vectors.PciBuilderSession.DdlOutcome;
import dev.nexus.service.vectors.PciCatalog.Index;
import dev.nexus.service.vectors.PciCatalog.Leaf;
import dev.nexus.service.vectors.PciReconcilePlanner.Action;
import dev.nexus.service.vectors.PciReconcilePlanner.Build;
import dev.nexus.service.vectors.PciReconcilePlanner.Drop;
import dev.nexus.service.vectors.PciReconcilePlanner.Reindex;
import dev.nexus.service.vectors.PciReconcilePlanner.RegistryRow;
import dev.nexus.service.vectors.PciReconcilePlanner.SkipCountSuspect;
import org.jooq.DSLContext;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.sql.DataSource;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.Collection;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Supplier;
import java.util.function.ToIntFunction;
import java.util.random.RandomGenerator;

import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_MODELS;

/**
 * RDR-227 Step 2 (nexus-43ulx.19): the reconciler's DDL half. Every {@code NX_SEARCH_PCI_SWEEP_SECONDS} it opens a
 * {@link PciBuilderSession.Pass} (which takes the builder lock; an engine that does not get it reports standby and
 * does nothing), reads the catalog, and for each tenant leaf counts, asks {@link PciReconcilePlanner} what to do, and
 * runs the plan: drops first, then builds one at a time. It acts only when {@code NX_SEARCH_PCI=1}
 * ({@link #start()} schedules nothing otherwise) and only on the engine that holds the lock. It runs on a thread of
 * its own with {@code scheduleWithFixedDelay}, so passes never overlap and a long build never delays the read half
 * ({@link PciIndexSweep}), which has its own task.
 *
 * <p><b>Counting</b> (one explicit transaction per leaf, on the pass's admin session; autocommit is off for it only):
 * <ol>
 *   <li>{@code set_config('nexus.tenant', <leaf tenant>, true)}, a {@code statement_timeout} of
 *       {@link #COUNT_TIMEOUT} and {@code plan_cache_mode = force_custom_plan} (a generic plan would not prune to the
 *       leaf), all transaction-local. The 30 s is per STATEMENT, not per leaf (nexus-43ulx.30, T2
 *       nexus_rdr/227-research-11, on local NVMe): the worst single count took 0.55 s cold, and a whole leaf's
 *       counting took 11 to 20 s cold, projected at about 45 s for 200 saturating collections. The leaf's
 *       transaction as a whole is therefore NOT bounded at 30 s. No {@code transaction_timeout} is set: it is a
 *       PostgreSQL 17 setting, setting it from inside the open transaction was not verified, and a per-leaf bound needs
 *       a value of its own (300 s was proposed, about 6.7 times the NVMe projection). A leaf with a statement past 30 s
 *       is skipped.</li>
 *   <li>Read {@code current_setting('nexus.tenant')} back. {@code catalog_collections} is FORCE ROW LEVEL SECURITY.
 *       Where the migrating role has no owner-read policy on it, a registry read under the wrong tenant is an EMPTY
 *       list, which the planner reads as "everything was deleted". Where {@code vectors-065} gave it one (RDR-229
 *       D2, two-role installs), the read's own {@code tenant_id} predicate selects the tenant's rows. Either way the
 *       check stays: on a mismatch the leaf is skipped ({@code event=pci_count_tenant_mismatch})
 *       and nothing is dropped or built. This check does NOT protect the chunk counts: {@code vectors-029} gave the
 *       migrating role ({@code nexus_admin} in production, the builder's role) a permissive
 *       {@code chunks_gate_probe_owner_read} policy, {@code USING (true)}, on {@code nexus.chunks}, so the admin
 *       session reads every tenant's chunks whatever {@code nexus.tenant} says.</li>
 *   <li>Read the registry ({@code catalog_collections}: name, lifecycle_state, superseded_by) for that tenant and
 *       the leaf's model, and the model's dimension. A failed read skips the leaf; it is never an empty registry.</li>
 *   <li>Count rows per collection, capped at B+1, with {@link PgVectorRepository#probeSelectedRowsQuery} (the router's
 *       probe: a count over a {@code LIMIT B+1} subquery on the leaf's (model, tenant, collection) key). Its explicit
 *       {@code tenant_id} predicate is what makes a count per-tenant (see the policy above), and the registry read's
 *       own {@code tenant_id} predicate is what keeps a same-named collection of another tenant out of a leaf's
 *       registry when the admin role bypasses row-level security: both are required for correctness, and each has a
 *       test that fails without it. Only
 *       collections that can build (live, unsuperseded) or that hold a {@code pci_} index are counted; a collection
 *       that is neither can neither build nor drop, so its count would decide nothing.</li>
 *   <li>Commit. A statement timeout ({@code 57014}) skips the leaf for this pass
 *       ({@code event=pci_count_timeout leaf=<name>}); a partial count is never acted on. The timeout is
 *       per statement (PostgreSQL restarts its clock at each), so a leaf with many collections is bounded at
 *       N times it, not once.</li>
 * </ol>
 * The plan is computed inside the transaction (it is a pure function of what was read), so the executor never sees
 * the registry or the counts, only actions.
 *
 * <p><b>Cap ranking.</b> Counts capped at B+1 make "keep the largest collections" vacuous: every candidate counts B or
 * B+1. When a leaf has more build candidates (count at least B, live, no index, not backed off) than free cap slots,
 * just those candidates are recounted with a higher cap, {@link #RANK_CAP_FACTOR} times B, inside the same
 * transaction and under the same timeout, and the planner ranks by those counts. Collections above that cap tie and
 * are ordered by name. The candidates and the free slots come from a preliminary plan made with no cap: the planner's
 * own drops and builds, and {@code kept = parsed indexes - drops}. Otherwise the B+1 cap stands.
 *
 * <p><b>Execution</b> (outside the transaction, in the session's autocommit): {@link PciBuilderSession.Pass#drop}
 * for every {@link Drop} (a 5 s lock timeout that expires leaves the index and the next pass retries), then
 * {@link PciBuilderSession.Pass#build} for every {@link Build} in plan order. The pass re-checks the migrator's lock
 * before each statement ({@code PciBuilderSession}) and this class checks it before each leaf's counting. After
 * every successful BUILD and every successful DROP it calls {@link PciIndexSweep#refresh()}: a router set that still
 * lists a dropped index sends its collection to the serving {@code ef_search} with no graph behind it. A
 * {@code refresh()} that returns false (a lock-wait skip or a failed read) is not fatal, since the read half's next
 * tick repairs the set, but after a DROP it is tried once more because that is the unsafe direction. When the pass
 * ends (a migration, a lost connection, a privilege error) the remaining actions are not sent, and a build that never
 * started, or whose backend was terminated, is not charged to backoff.
 *
 * <p><b>The anchor</b> (RDR-230 Technical Design 1 and 2, nexus-4lnn1.1.1). An index's anchor is its PostgreSQL
 * comment ({@link PciCatalog.Anchor}). After each successful BUILD the pass counts the collection's rows (uncapped, in
 * a counting transaction of its own) and writes {@code origin=built}; a failed count or comment leaves the index
 * unanchored and the next pass adopts it. Every pass, inside the leaf's counting transaction, reads each valid parsed
 * index's comment and {@code pg_relation_size} and the leaf's {@code pg_stat_user_tables} row. An index with no
 * parseable anchor is adopted once ({@code origin=adopted}, the current rows and bytes, the adoption time as
 * {@code built_at}; {@code event=pci_anchor_adopted reason=missing|unparsable}); an anchored one is not written
 * again, except that one whose relfilenode is not the index's (a hand REINDEX CONCURRENTLY keeps the comment) is
 * re-anchored as built now and one with no relfilenode is stamped once ({@link PciAnchorLedger}). The uncapped row count and {@code written_since} are taken only when the leaf's deletes or non-HOT updates
 * advanced since this engine last looked, a statistics reset moved them backward, the index's last count is older
 * than {@link #RECOUNT_AFTER}, the anchor is not the one the last count was taken against, or the index's size changed
 * and its bytes over the stale row count (an upper bound on bytes per row, since ingest only adds rows) reaches the
 * REINDEX size trigger ({@link PciReindexPolicy#sizeFires}): then a count could change the verdict. What was read is kept
 * in memory ({@link IndexReading}) and, like the retry state, is lost on restart: the first look after a boot counts.
 * A leaf whose pass is skipped keeps its previous readings, and a leaf's baseline moves only when its transaction
 * commits.
 *
 * <p><b>REINDEX</b> (RDR-230 Phase 2, nexus-4lnn1.1.10; {@code NX_SEARCH_PCI_REINDEX}, default on). After the pass's
 * drops and builds, {@link #runReindexes} turns what the leaves' counting transactions read into
 * {@link PciReindexPolicy.Candidate}s, asks {@link PciReconcilePlanner#reindexes} (a pure function; the thresholds are
 * {@link PciReindexPolicy}'s) which fire, and runs at most {@link PciReindexPolicy#MAX_PER_PASS}, one at a time, largest
 * first: {@code Pass#reindex}, then a fresh anchor ({@link #anchorAfterBuild}), then, after a bulk delete,
 * {@code Pass#vacuum} of the leaf, then the canary again (with a red run's own queries when there are some). What the
 * rules need between passes ({@link PciReindexState}: the previous row count, the once-a-day limit of a red-canary
 * REINDEX, {@code red_after_reindex}, the failure backoff) is in memory like the build backoff. A failed REINDEX is
 * charged to RDR-227's backoff per index; one cut off by a migration or a lost connection is not. A red-canary
 * REINDEX whose re-measure is still red is written into the anchor as {@code red_unfixed}, which stops the daily
 * red-canary trigger while the index and the query ring are unchanged. A size change of an
 * index makes the next look recount its rows, because the size rule divides bytes by rows.
 *
 * <p><b>Retry state is in memory, per engine, and is lost on restart.</b> After a failed build of
 * (leaf, collection) the first retry comes no sooner than {@link #FIRST_RETRY} later, doubling to
 * {@link #MAX_RETRY}; the collection is handed to the planner as backed off until then. After
 * {@link #FAILING_AFTER} consecutive failures it counts as <i>failing</i>. A restart forgets all of it, so a build
 * that always fails is retried at once after each boot and then backs off again. A success clears the entry, and so
 * does a collection leaving the registry, being superseded or quarantined, or gaining a valid index. A build ended
 * by the connection being terminated (a schema migration) or lost is not a failure of the
 * build and is not charged.
 *
 * <p><b>With more than one engine the backoff is bounded, not exponential.</b> The builder lock serialises passes,
 * but it is held per pass, so the holder alternates between engines, while the backoff and the failure count live in
 * the memory of the engine that ran the build. A failed concurrent build leaves an INVALID index that must be
 * dropped before the name can be built again, and a peer with no entry for the collection drops it and rebuilds on
 * its first pass after the drop. So across N engines a failing build is retried at most once per sweep period
 * ({@code NX_SEARCH_PCI_SWEEP_SECONDS}) by whichever engine holds the lock, which bounds the cost but does not back
 * it off to {@link #MAX_RETRY}; each engine's own retries do back off, and its {@code failing} count is its own, so
 * an operator reads {@code failing} on every engine. A shared, catalog-backed backoff is deliberately not built
 * (the retry state is a session decision: in memory, lost on restart).
 *
 * <p><b>Status for the status object</b> (nexus-43ulx.23 builds the object; {@link #status()} is its source).
 * {@code builder_state} is the builder's own current state, so a privilege or authentication failure shows up while
 * it holds. Only the lock holder reports {@code building} (0 or 1), {@code failing} and the progress of a pass in
 * flight ({@code pass_started_at}, {@code pass_in_progress}): other engines cannot see another role's
 * {@code pg_stat_progress_create_index} and hold none of this state. The last DDL pass time is this engine's own,
 * reported whether or not it holds the lock now (null only before its first completed pass). A non-holder reports
 * {@code builder_state = standby} (or {@code off}, {@code auth_failed}) and nulls for the holder-only fields.
 */
public final class PciReconciler {

    private static final Logger log = LoggerFactory.getLogger(PciReconciler.class);

    /** Name of the scheduler thread; the stop test and a thread dump both look for it. */
    static final String THREAD_NAME = "pci-reconcile-ddl";

    /** Statement timeout of each counting statement; Step 3 measures it. */
    static final Duration COUNT_TIMEOUT = Duration.ofSeconds(30);

    /** The ranking recount's cap, as a multiple of B. */
    static final int RANK_CAP_FACTOR = 10;

    /** The first retry after a failed build, then doubling. */
    static final Duration FIRST_RETRY = Duration.ofMinutes(10);
    static final Duration MAX_RETRY = Duration.ofHours(24);

    /** Consecutive failed builds after which a collection counts as failing. */
    static final int FAILING_AFTER = 3;

    /** How long an index's row count and {@code written_since} stand with no delete or update to move them. */
    static final Duration RECOUNT_AFTER = Duration.ofHours(24);

    /** How long the counting transaction waits for a lock on an index it sizes; a longer wait skips the leaf. */
    static final String SIZE_LOCK_TIMEOUT = "5s";

    /** SQLSTATE: query_canceled (a statement timeout). */
    private static final String QUERY_CANCELED = "57014";

    /** Sets the transaction-local tenant. A seam: the production binder is {@link #SET_LOCAL_TENANT}. */
    @FunctionalInterface
    interface TenantBinder {
        void bind(DSLContext tx, String tenant);
    }

    static final TenantBinder SET_LOCAL_TENANT = (tx, tenant) -> tx.select(DSL.function("set_config",
        SQLDataType.VARCHAR, DSL.val("nexus.tenant"), DSL.val(tenant), DSL.inline(true))).fetch();

    /**
     * What the status object reads from the DDL half.
     *
     * @param builderState {@code ok}, {@code no_privilege}, {@code auth_failed}, {@code off} or {@code standby}
     * @param building     1 while this holder's build runs, else 0; {@code null} unless this engine holds the lock
     * @param failing      collections with {@value #FAILING_AFTER} or more consecutive failed builds; {@code null}
     *                     unless this engine holds the lock
     * @param lastDdlPassAt when this engine's last pass that held the lock, ran to its end and processed at least one
     *                     leaf finished; kept when the engine is standby now, {@code null} only before its first
     * @param passStartedAt when the pass in flight started; {@code null} when none is, and always on a non-holder
     * @param passInProgress whether a lock-holding pass is in flight (the long first pass shows here, not in
     *                     {@code lastDdlPassAt})
     * @param sweepSeconds the period this engine's passes run at ({@code NX_SEARCH_PCI_SWEEP_SECONDS}); a client reads
     *                     it to judge how stale {@code lastDdlPassAt} may be
     */
    public record DdlStatus(BuilderState builderState, Integer building, Integer failing, Instant lastDdlPassAt,
                            Instant passStartedAt, boolean passInProgress, long sweepSeconds) {
        /** The setting's default period, for a status built without one (tests). */
        static final long DEFAULT_SWEEP_SECONDS = 600;

        /** A status with the default period. */
        public DdlStatus(BuilderState builderState, Integer building, Integer failing, Instant lastDdlPassAt,
                         Instant passStartedAt, boolean passInProgress) {
            this(builderState, building, failing, lastDdlPassAt, passStartedAt, passInProgress,
                DEFAULT_SWEEP_SECONDS);
        }

        /** A status with no pass in flight. */
        public DdlStatus(BuilderState builderState, Integer building, Integer failing, Instant lastDdlPassAt) {
            this(builderState, building, failing, lastDdlPassAt, null, false);
        }
    }

    /** What one pass did; for tests and the pass log line. */
    record PassReport(BuilderState state, boolean ranToEnd, int leaves, int drops, int builds, int failedBuilds,
                      int skippedLeaves) {
        static PassReport inactive(BuilderState state) {
            return new PassReport(state, false, 0, 0, 0, 0, 0);
        }
    }

    /** The longest start jitter: a pass at boot waits a random time up to this, or a tenth of the period. */
    static final Duration MAX_START_JITTER = Duration.ofSeconds(60);

    private record LeafKey(String leaf, String collection) { }

    /**
     * What the DDL half last read of one valid parsed index (RDR-230 Technical Design 2).
     *
     * @param anchor       the anchor on the index when read (adopted or written since)
     * @param bytes        {@code pg_relation_size} when read
     * @param rows         the collection's uncapped row count at {@code countedAt}
     * @param writtenSince rows whose {@code last_written_at} is later than {@code anchor.builtAt()}, at {@code countedAt}
     * @param countedAt    when {@code rows} and {@code writtenSince} were taken, on this engine's clock
     * @param countedBytes {@code pg_relation_size} when {@code rows} was counted: with {@code rows}, a coherent pair (after
     *                     a skipped recount {@code bytes} is fresh and {@code rows} is not)
     */
    record IndexReading(PciCatalog.Anchor anchor, long bytes, long rows, long writtenSince, Instant countedAt,
                        long countedBytes) {
        /** A reading whose count was taken now, at {@code bytes}. */
        IndexReading(PciCatalog.Anchor anchor, long bytes, long rows, long writtenSince, Instant countedAt) {
            this(anchor, bytes, rows, writtenSince, countedAt, bytes);
        }
    }

    private record Failure(int consecutive, Instant nextRetryAt) { }

    private final Supplier<PciCatalog.Snapshot> catalog;
    private final PciBuilderSession builder;
    private final PciIndexSweep sweep;
    private final PciSettings settings;
    private final Clock clock;
    private final TenantBinder tenantBinder;
    private final Duration countTimeout;
    private final Duration period;
    private final RandomGenerator random;
    /** nexus-4lnn1.1.3: deletes the recall canary's query-ring rows older than 30 days; returns the row count. */
    private final ToIntFunction<Collection<String>> ringExpiry;
    /** nexus-4lnn1.1.4: when the recall canary runs and what it last found; {@code null} means no canary (tests). */
    private final PciCanarySchedule canary;

    private final Map<LeafKey, Failure> failures = new ConcurrentHashMap<>();
    /** What the DDL half last read of each index's anchor, and the anchors it writes on what it finds (nexus-4lnn1.1.10). */
    private final PciAnchorLedger ledger;
    /** RDR-230 Phase 2: what the REINDEX half remembers (previous counts, 24 h limits, backoff, counters). */
    private final PciReindexState reindex = new PciReindexState();
    /** The REINDEX's execution (statement, anchor and its retry, VACUUM, canary again); built with the canary. */
    private final PciReindexPhase phase;
    /**
     * TEST SEAM: run at the start of the REINDEX phase, inside the guard that keeps a failing phase from failing the
     * pass. A test sets it to throw, or to take the migration lock at that moment.
     */
    volatile Runnable reindexPhaseHook;
    /** TEST SEAM: run after a leaf's own HNSW index is reindexed, before its anchor is written. */
    volatile Runnable afterLeafReindexHook;

    /** TEST SEAM: the REINDEX half's memory, for tests that set its backoff. */
    PciReindexState reindexState() {
        return reindex;
    }

    /** The anchors still owed after a REINDEX, by index name, for tests. */
    Map<String, PciReindexState.Owed> owedAnchors() {
        return reindex.owedAnchors();
    }
    private final AtomicReference<String> building = new AtomicReference<>();
    private final AtomicInteger passes = new AtomicInteger();
    /** nexus-f3yqd.3: the {@code boot_work} entry; null until {@link #bootWork} binds a registry. */
    private volatile PciBootWork bootWork;
    /**
     * Leaf HNSW indexes whose size rule is inactive for want of an install reference at their dimension (RDR-230 P2.3
     * sets it each pass through {@link #noteLeafUnanchored}); the status reports it as {@code unanchored}.
     */
    private volatile int leafUnanchored;
    private volatile boolean holder;
    private volatile Instant lastDdlPassAt;
    private volatile Instant passStartedAt;

    private final Object lifecycle = new Object();
    private ScheduledExecutorService scheduler;   // guarded by lifecycle
    private boolean stopped;                      // guarded by lifecycle

    public PciReconciler(PciCatalog catalog, PciBuilderSession builder, PciIndexSweep sweep, PciSettings settings) {
        this(catalog, builder, sweep, settings, Clock.systemUTC(), SET_LOCAL_TENANT, COUNT_TIMEOUT,
            Duration.ofSeconds(Objects.requireNonNull(settings, "settings").sweepSeconds()));
    }

    PciReconciler(PciCatalog catalog, PciBuilderSession builder, PciIndexSweep sweep, PciSettings settings,
                  Clock clock, TenantBinder tenantBinder, Duration countTimeout, Duration period) {
        this(Objects.requireNonNull(catalog, "catalog")::read, builder, sweep, settings, clock, tenantBinder,
            countTimeout, period, RandomGenerator.getDefault());
    }

    /** As above with the catalog read and the start jitter's source injected (tests). */
    PciReconciler(Supplier<PciCatalog.Snapshot> catalog, PciBuilderSession builder, PciIndexSweep sweep,
                  PciSettings settings, Clock clock, TenantBinder tenantBinder, Duration countTimeout,
                  Duration period, RandomGenerator random) {
        this(catalog, builder, sweep, settings, clock, tenantBinder, countTimeout, period, random, leafTenants -> 0);
    }

    /**
     * As above with the query-ring age bound (nexus-4lnn1.1.3): {@code ringExpiry} runs at the start of every pass
     * this engine runs as the builder-lock holder, whatever the pass then plans, and returns the rows it deleted. It is
     * handed the distinct tenants of the pass's catalog snapshot (every tenant leaf of {@code nexus.chunks}, with or
     * without indexes; empty when the read failed), so a tenant that holds ring rows is visited whether or not it still
     * has a token or a ledger row.
     */
    PciReconciler(Supplier<PciCatalog.Snapshot> catalog, PciBuilderSession builder, PciIndexSweep sweep,
                  PciSettings settings, Clock clock, TenantBinder tenantBinder, Duration countTimeout,
                  Duration period, RandomGenerator random, ToIntFunction<Collection<String>> ringExpiry) {
        this(catalog, builder, sweep, settings, clock, tenantBinder, countTimeout, period, random, ringExpiry, null);
    }

    /**
     * As above with the recall canary's schedule (nexus-4lnn1.1.4): on each pass this engine runs as the builder-lock
     * holder, after the drops and builds, the schedule runs what is due, within its pass budget. {@code null} runs none.
     */
    PciReconciler(Supplier<PciCatalog.Snapshot> catalog, PciBuilderSession builder, PciIndexSweep sweep,
                  PciSettings settings, Clock clock, TenantBinder tenantBinder, Duration countTimeout,
                  Duration period, RandomGenerator random, ToIntFunction<Collection<String>> ringExpiry,
                  PciCanarySchedule canary) {
        this.canary = canary;
        this.ledger = new PciAnchorLedger(Objects.requireNonNull(clock, "clock"), reindex::reference,
            name -> reindex.owedAnchors().containsKey(name));
        this.phase = new PciReindexPhase(reindex, canary, clock, this::writeAnchor, ledger::markRedUnfixed);
        this.phase.idCanaryWhen(idBuilds::gate3Holds);
        this.ringExpiry = Objects.requireNonNull(ringExpiry, "ringExpiry");
        this.random = Objects.requireNonNull(random, "random");
        this.catalog = Objects.requireNonNull(catalog, "catalog");
        this.builder = Objects.requireNonNull(builder, "builder");
        this.sweep = Objects.requireNonNull(sweep, "sweep");
        this.settings = Objects.requireNonNull(settings, "settings");
        this.clock = Objects.requireNonNull(clock, "clock");
        this.tenantBinder = Objects.requireNonNull(tenantBinder, "tenantBinder");
        this.countTimeout = Objects.requireNonNull(countTimeout, "countTimeout");
        this.period = Objects.requireNonNull(period, "period");
        if (period.isZero() || period.isNegative()) {
            throw new IllegalArgumentException("period must be positive, got " + period);
        }
    }

    /**
     * The production reconciler: reads the catalog through {@code ds}, builds and drops on a fresh connection with the
     * engine's {@code NX_DB_ADMIN_*} values, and refreshes {@code sweep} after each change.
     */
    public static PciReconciler create(DataSource ds, AdminConnection admin, String bootNonce, PciIndexSweep sweep,
                                       PciSettings settings) {
        return create(ds, admin, bootNonce, sweep, settings, leafTenants -> 0);
    }

    /** As above, with the query ring's age bound run at the start of each pass this engine holds the lock for. */
    public static PciReconciler create(DataSource ds, AdminConnection admin, String bootNonce, PciIndexSweep sweep,
                                       PciSettings settings, ToIntFunction<Collection<String>> ringExpiry) {
        return new PciReconciler(new PciCatalog(ds)::read,
            new PciBuilderSession(admin.url(), admin.user(), admin.password(), bootNonce, settings), sweep, settings,
            Clock.systemUTC(), SET_LOCAL_TENANT, COUNT_TIMEOUT,
            Duration.ofSeconds(Objects.requireNonNull(settings, "settings").sweepSeconds()),
            RandomGenerator.getDefault(), ringExpiry);
    }

    /**
     * As above, with the recall canary (nexus-4lnn1.1.4, RDR-230): its statements go through {@code repository}'s
     * serving path, its queries come from the ring in {@code scope}'s database.
     */
    public static PciReconciler create(DataSource ds, AdminConnection admin, String bootNonce, PciIndexSweep sweep,
                                       PciSettings settings, ToIntFunction<Collection<String>> ringExpiry,
                                       PgVectorRepository repository,
                                       dev.nexus.service.db.TenantScope scope) {
        // The canary's id statements and its ring reader follow the latch this reconciler's retire reads (gate 3 latched, the id readers present), bound below.
        var latch = new PciCanaryLatch();
        var ring = new PciQueryRingReader(Objects.requireNonNull(scope, "scope"), latch);
        var runner = new PciCanary(ring::load, Objects.requireNonNull(repository, "repository").canaryStatements(latch));
        var reconciler = new PciReconciler(new PciCatalog(ds)::read,
            new PciBuilderSession(admin.url(), admin.user(), admin.password(), bootNonce, settings), sweep, settings,
            Clock.systemUTC(), SET_LOCAL_TENANT, COUNT_TIMEOUT,
            Duration.ofSeconds(Objects.requireNonNull(settings, "settings").sweepSeconds()),
            RandomGenerator.getDefault(), ringExpiry, new PciCanarySchedule(runner));
        latch.follow(reconciler.idBuilds::gate3Holds);
        reconciler.canaryLatch = latch;
        return reconciler;
    }

    /** The latch the canary, its ring reader and the query ring's admission follow: false until this reconciler binds it ({@link #create} with a canary). */
    public dev.nexus.service.datamove.Gate3 canaryLatch() {
        return canaryLatch;
    }

    /** The canary's latest result for every index it has run on, by index name (empty without a canary). */
    Map<String, PciCanary.Result> canaryResults() {
        return canary == null ? Map.of() : canary.results();
    }

    // -- status ----------------------------------------------------------------------------------------------

    /**
     * The {@code maintenance} object of the status (RDR-230 Technical Design 7, nexus-4lnn1.1.5), from values this
     * engine holds in memory: the canary's latest result per index and what the last counting look read of it. No query
     * runs. The canary's results are this engine's own history and stay after it becomes a standby, like
     * {@code last_ddl_pass_at}; an engine that never held the lock has none and reports zeros and an empty list.
     *
     * @return null when no canary is wired (a reconciler built without one): the status then omits the key
     */
    public PciMaintenanceStatus maintenance() {
        if (canary == null) {
            return null;
        }
        var entries = new java.util.ArrayList<PciMaintenanceStatus.Entry>();
        for (PciCanary.Result r : canary.results().values()) {
            PciCanary.Target t = r.target();
            IndexReading reading = ledger.reading(t.leaf(), t.index());
            java.util.Optional<String> sweepOrigin = reading != null ? java.util.Optional.empty()
                : sweep.anchor(t.model(), t.tenant(), t.collection()).map(PciCatalog.Anchor::origin);
            entries.add(PciMaintenanceStatus.entry(r, reading, sweepOrigin,
                reindex.redAfterReindex(t.index(), clock.instant())));
        }
        return PciMaintenanceStatus.of(entries, settings.enabled() && settings.reindex(), reindex.reindexing(),
            reindex.lastReindexAt(), reindex.reindexes(), leafUnanchored);
    }

    /** The DDL half's status; see the class comment for who reports what. */
    public DdlStatus status() {
        Instant lastPass = lastDdlPassAt;
        if (!settings.enabled()) {
            return new DdlStatus(BuilderState.OFF, null, null, lastPass, null, false, period.toSeconds());
        }
        // The builder's own current state, not a copy taken at the start of a pass: a privilege error in the middle
        // of a pass (and an authentication failure at its start) is what this field exists to show.
        BuilderState state = builder.state();
        if (!holder) {
            return new DdlStatus(state, null, null, lastPass, null, false, period.toSeconds());
        }
        Instant startedAt = passStartedAt;
        return new DdlStatus(state, building.get() != null ? 1 : 0, failingCount(), lastPass, startedAt,
            startedAt != null, period.toSeconds());
    }

    /** Collections with {@value #FAILING_AFTER} or more consecutive failed builds. */
    private int failingCount() {
        return (int) failures.values().stream().filter(f -> f.consecutive() >= FAILING_AFTER).count();
    }

    /** The leaf-index rule's count of indexes it cannot judge for want of a reference; the status's {@code unanchored}. */
    void noteLeafUnanchored(int count) {
        leafUnanchored = Math.max(0, count);
    }

    /** REINDEXes this engine has finished since boot, for tests. */
    int reindexes() {
        return reindex.reindexes();
    }

    /** Whether the index's last red-canary REINDEX re-measured red and a day has not passed since; for tests. */
    boolean redAfterReindex(String indexName) {
        return reindex.redAfterReindex(indexName, clock.instant());
    }

    /** Passes started since boot, for tests. */
    int passes() {
        return passes.get();
    }

    /** What the last committed look read of {@code indexName} on {@code leaf}, or {@code null}; for tests. */
    IndexReading reading(Leaf leaf, String indexName) {
        return ledger.reading(leafId(leaf), indexName);
    }

    /**
     * Report this reconciler's pass to {@code GET /v1/status}'s {@code boot_work} (nexus-f3yqd.3): a
     * {@code pci-reconcile} entry while a pass that plans builds runs, with a {@code step} sampled from the builder
     * backend's {@code pg_stat_progress_create_index} (see {@link PciBootWork}). Call before {@link #start()}; without
     * it the reconciler reports nothing. Only the engine that holds the builder lock plans builds, so a standby
     * reports none. {@code GET /v1/status} is unauthenticated, and by Sam's rulings of 2026-10-19 and 2026-11-06 it
     * names no collection, tenant or index: the step carries the build phase only, and no argument changes that.
     */
    public PciReconciler bootWork(dev.nexus.service.BootWorkRegistry registry) {
        return bootWork(registry, PciBootWork.SAMPLE_EVERY);
    }

    /** As above with the sampling period injected (tests). */
    PciReconciler bootWork(dev.nexus.service.BootWorkRegistry registry, Duration sampleEvery) {
        this.bootWork = new PciBootWork(registry, builder::sampleBuildProgress, sampleEvery);
        return this;
    }

    /** Collections with a failure entry (any count), for tests; {@link #status()} reports only the failing ones. */
    int trackedFailures() {
        return failures.size();
    }

    // -- schedule --------------------------------------------------------------------------------------------

    /**
     * Start the DDL task: one pass after a short random delay ({@link #startJitterMillis}), then one {@code period}
     * after each pass ends. Idempotent while running.
     * Does nothing when {@code NX_SEARCH_PCI=0}. Call after the service and the read half have started. A reconciler
     * that has been stopped does not restart.
     */
    public void start() {
        if (!settings.enabled()) {
            log.info("event=pci_reconciler_off reason=NX_SEARCH_PCI_0");
            return;
        }
        long jitterMillis;
        synchronized (lifecycle) {
            if (stopped) {
                throw new IllegalStateException("the PCI reconciler was stopped and does not restart");
            }
            if (scheduler != null) {
                return;
            }
            jitterMillis = startJitterMillis(period, random);
            scheduler = Executors.newSingleThreadScheduledExecutor(r -> {
                Thread t = new Thread(r, THREAD_NAME);
                t.setDaemon(true);
                return t;
            });
            // A jittered first run: engines restarted together (a rolling deploy, a node reboot) would otherwise all
            // open their counting transactions at once. The builder lock still lets one build; this spreads the rest.
            scheduler.scheduleWithFixedDelay(this::scheduledPass, jitterMillis, period.toMillis(),
                TimeUnit.MILLISECONDS);
        }
        log.info("event=pci_reconciler_started period_seconds={} start_delay_ms={}", period.toSeconds(), jitterMillis);
    }

    /**
     * A random delay in {@code [0, min(MAX_START_JITTER, period / 10)]} milliseconds, drawn from {@code random}.
     */
    static long startJitterMillis(Duration period, RandomGenerator random) {
        long max = Math.min(MAX_START_JITTER.toMillis(), period.toMillis() / 10);
        return max <= 0 ? 0 : random.nextLong(max + 1);
    }

    /** A thrown task is never rescheduled by the executor, so nothing may escape this method. */
    private void scheduledPass() {
        try {
            reconcileOnce();
        } catch (Throwable t) {
            log.error("event=pci_reconcile_task_error error=\"{}\"", t.toString(), t);
        }
    }

    /**
     * End the task. A pass in flight is interrupted, and this does not wait for it: an in-flight
     * {@code CREATE INDEX CONCURRENTLY} ignores the interrupt, and the shutdown hook leaves the builder's backend
     * running so the build finishes in Postgres (nexus-f3yqd). Harmless when never started or already stopped.
     */
    public void stop() {
        ScheduledExecutorService toStop;
        synchronized (lifecycle) {
            stopped = true;
            toStop = scheduler;
        }
        if (toStop != null) {
            toStop.shutdownNow();
        }
    }

    /** True between {@link #start()} and {@link #stop()}. */
    public boolean isRunning() {
        synchronized (lifecycle) {
            return scheduler != null && !stopped;
        }
    }

    // -- one pass --------------------------------------------------------------------------------------------

    /**
     * Runs when a pass ends, however it ends (the completion check re-reads then, RDR-229 P3.1). Set once at boot, on the pass's thread; it must
     * not block, and a throw is logged and goes no further.
     */
    private volatile Runnable passEnded = () -> { };

    private final PciIdBuilds idBuilds = new PciIdBuilds();
    private volatile dev.nexus.service.datamove.Gate3 canaryLatch = dev.nexus.service.datamove.Gate3.never();

    /** RDR-229 P3.4: gate 2 of the completion check, read at the start of each pass; until it holds no id build is planned. */
    public void idBuildGate(java.util.function.BooleanSupplier gate2) {
        idBuilds.gate(gate2);
    }

    /**
     * RDR-229 P3.4: the PCI clause of gate 3, for {@code CompletionBootWork}. Read from the database ({@link PciGate3Coverage}), so any engine
     * answers it the same, a standby included; it needs this reconciler only for the catalog, the settings and the clock.
     */
    public dev.nexus.service.datamove.CompletionCheck.Gate3Source gate3Source() {
        return new PciGate3Coverage(catalog, settings, clock);
    }

    /**
     * RDR-229 P3.4 (linda, 2026-11-22): gate 3 as it LATCHED ({@code CompletionBootWork#gate3Latched}, never the live gate 3), read at the start of each
     * pass. Once it holds, and the id readers are present ({@link IdReaders#PRESENT}), the name family retires: no name index is built, and each
     * name index whose collection's id index is VALID is dropped ({@code REPLACED_BY_ID}).
     */
    public void retireNameIndexesWhen(java.util.function.BooleanSupplier gate3) {
        idBuilds.gate3(gate3);
    }

    /**
     * RDR-229 P3.4 (linda, 2026-11-23): the gate 3 latch row says it latched while {@code NX_SEARCH_PCI} was off ({@code pci_off}), so no id index was
     * required then. With PCI on now, the id builds go ahead as if gate 2 held; the readers are on the id either way.
     */
    public void buildIdIndexesFirstWhen(java.util.function.BooleanSupplier pciOffLatch) {
        idBuilds.pciOffLatch(pciOffLatch);
    }

    /**
     * Whether this engine's readers select by id ({@link IdReaders#PRESENT}, which P3.5c flips; the default). The name family retires only
     * when this is true as well as the latched gate 3: the retire drops the indexes the name path serves from.
     */
    public void idReadersPresent(java.util.function.BooleanSupplier present) {
        idBuilds.readers(present);
    }

    /** Sets what runs at the end of each pass; see the field. A null clears it. */
    public void onPassEnded(Runnable listener) {
        this.passEnded = listener == null ? () -> { } : listener;
    }

    /** Run one pass now, on the calling thread. Returns what it did. */
    PassReport reconcileOnce() {
        try {
            return runPass();
        } finally {
            try {
                passEnded.run();
            } catch (RuntimeException e) {   // a listener is the engine's, never the pass's failure
                log.warn("event=pci_pass_ended_listener_failed cause=\"{}\"", e.toString());
            }
        }
    }

    private PassReport runPass() {
        passes.incrementAndGet();
        if (!settings.enabled()) {
            holder = false;
            return PassReport.inactive(BuilderState.OFF);
        }
        long startedNanos = System.nanoTime();
        PciBuilderSession.Pass pass;
        try {
            pass = builder.open();
        } catch (IllegalStateException e) {
            // The database is unreachable: this engine does not hold the lock now, and nothing is building.
            holder = false;
            building.set(null);
            passStartedAt = null;
            log.warn("event=pci_reconcile_pass_failed reason=builder_open cause=\"{}\"", e.getMessage());
            return PassReport.inactive(builder.state());
        }
        try (pass) {
            BuilderState state = builder.state();
            holder = state == BuilderState.OK || state == BuilderState.NO_PRIVILEGE;
            if (!holder || !pass.live()) {
                return PassReport.inactive(state);
            }
            passStartedAt = clock.instant();
            PciCatalog.Snapshot snapshot = null;
            try {
                snapshot = catalog.get();
            } catch (RuntimeException e) {
                log.warn("event=pci_reconcile_pass_failed reason=catalog_read cause=\"{}\"", e.getMessage());
            }
            // After the read, so the ring's age bound can visit the snapshot's tenants; before any build, so it still
            // runs on a pass that then fails or plans nothing.
            expireRings(snapshot == null ? List.of() : leafTenants(snapshot));
            if (snapshot == null) {
                return PassReport.inactive(state);
            }
            if (snapshot.leaves().isEmpty()) {
                // No installed schema produces this: a failed read, never "there are no indexes".
                log.warn("event=pci_reconcile_pass_failed reason=no_partition_leaves");
                return PassReport.inactive(state);
            }
            pass.useSnapshot(snapshot);   // the only leaves VACUUM may touch
            Tally tally = new Tally();
            tally.gate2 = idBuilds.gate2Holds();
            tally.gate3 = idBuilds.gate3Holds();
            tally.lastPlannable = lastPlannable(snapshot.leaves());
            for (Leaf leaf : snapshot.leaves()) {
                if (!pass.live() || Thread.currentThread().isInterrupted()) {
                    break;
                }
                tally.leaves++;
                if (!reconcileLeaf(pass, leaf, tally)) {
                    break;
                }
            }
            boolean ranToEnd = pass.live() && !Thread.currentThread().isInterrupted();
            // A pass whose every leaf was skipped (a count timeout, a failed read) did no reconciling: reporting it
            // as the last completed pass would hide that nothing has been checked.
            if (ranToEnd && tally.processedLeaves > 0) {
                lastDdlPassAt = clock.instant();
            }
            // After the pass's drops and builds, before the canary: a REINDEX is re-measured by it (RDR-230 TD3).
            if (ranToEnd && settings.reindex()) {
                try {
                    runReindexes(pass, snapshot, tally);
                } catch (RuntimeException e) {
                    // Like the canary below: maintenance never fails the pass, and what it spared still runs.
                    log.warn("event=pci_reindex_phase_failed error={}", PciQueryRing.sanitize(e));
                }
            }
            log.info("event=pci_reconcile_pass ran_to_end={} leaves={} processed_leaves={} drops={} builds={} "
                + "failed_builds={} skipped_leaves={} took_ms={} sum_count_ms={} max_count_ms={} builder_state={} "
                + "failing={} reindexes={}", ranToEnd, tally.leaves, tally.processedLeaves, tally.drops, tally.builds,
                tally.failedBuilds, tally.skippedLeaves, (System.nanoTime() - startedNanos) / 1_000_000,
                tally.sumCountMillis, tally.maxCountMillis, builder.state().wire(), failingCount(), tally.reindexes);
            if (ranToEnd) {
                runCanaries(snapshot, tally);
            }
            return new PassReport(builder.state(), ranToEnd, tally.leaves, tally.drops, tally.builds,
                tally.failedBuilds, tally.skippedLeaves);
        } finally {
            building.set(null);
            passStartedAt = null;
            PciBootWork work = bootWork;
            if (work != null) {
                work.endPass();
            }
        }
    }

    /**
     * The recall canary (nexus-4lnn1.1.4), after the pass's drops and builds: the family the serving statement selects by. Before gate 3 has latched
     * that is every valid name index the snapshot lists that the pass did not drop, and every name index it built; once it has (and the id readers are
     * present), the valid id indexes and every id index it built. It runs on the lock holder only and never fails the pass; a result is recorded and
     * logged, and a red one can start a REINDEX (RDR-230).
     */
    private void runCanaries(PciCatalog.Snapshot snapshot, Tally tally) {
        if (canary == null) {
            return;
        }
        try {
            Map<String, PciCanary.Target> targets = new java.util.LinkedHashMap<>();
            for (Leaf leaf : snapshot.leaves()) {
                if (leaf.model() == null || leaf.tenant() == null) {
                    continue;
                }
                for (Index index : leaf.indexes()) {
                    if (!index.valid() || tally.droppedIndexes.contains(index.name())) {
                        continue;
                    }
                    if (!tally.gate3 && index.parsed()) {
                        targets.put(index.name(), new PciCanary.Target(leafId(leaf), leaf.tenant(), leaf.model(),
                            index.collection(), index.name()));
                    } else if (tally.gate3 && index.idParsed()) {
                        // Latched: the name family retires, the serving statement selects by id, so the canary follows it. The label is the
                        // registry's current one for the id, read by this pass's counting transaction; an id it did not read is not measured.
                        String label = tally.idLabels.get(PciCatalog.idKey(leaf.model(), leaf.tenant(), index.collectionId()));
                        if (label != null) {
                            targets.put(index.name(), new PciCanary.Target(leafId(leaf), leaf.tenant(), leaf.model(), label,
                                index.name(), index.collectionId()));
                        }
                    }
                }
            }
            for (PciCanary.Target built : tally.builtTargets) {
                targets.put(built.index(), built);
            }
            canary.runPass(List.copyOf(targets.values()));
        } catch (RuntimeException e) {
            log.warn("event=pci_canary_pass_failed error=\"{}\"", PciQueryRing.sanitize(e));
        }
    }

    /**
     * The leaf's statistics, just committed, to the canary's vacuum trigger: the leaf's valid indexes the plan is not
     * dropping.
     */
    private void observeForCanary(Leaf leaf, Planned planned) {
        PciAnchorReads.LeafStats stats = planned.anchors().stats();
        if (canary == null || stats == null) {
            return;
        }
        Set<String> dropping = new HashSet<>();
        for (Action action : planned.actions()) {
            if (action instanceof Drop d) {
                dropping.add(d.name());
            }
        }
        List<String> names = leaf.indexes().stream()
            .filter(i -> i.parsed() && i.valid() && !dropping.contains(i.name())).map(Index::name).toList();
        canary.observeLeaf(leafId(leaf), names, stats);
    }

    /**
     * The query ring's 30-day age bound (nexus-4lnn1.1.3). It is not part of the pass's account: a failure here must
     * not stop the builds, and the expiry logs its own counts.
     */
    private void expireRings(Collection<String> leafTenants) {
        try {
            ringExpiry.applyAsInt(leafTenants);
        } catch (RuntimeException e) {
            log.warn("event=pci_ring_expire_failed reason=hook error_class={}", e.getClass().getSimpleName());
        }
    }

    /**
     * The distinct tenants of the snapshot's leaves, in leaf order; a leaf whose tenant bound did not parse names none.
     * A ring row needs a registry row, and admission needs a valid per-collection index, which lives on a leaf, so a
     * tenant that holds ring rows has a leaf. The exception is a tenant whose leaf was dropped while its registry rows
     * stayed (the rows then go with the collection, not with the 30-day bound), or whose leaf bound this engine cannot
     * parse.
     */
    static List<String> leafTenants(PciCatalog.Snapshot snapshot) {
        var out = new java.util.LinkedHashSet<String>();
        for (Leaf leaf : snapshot.leaves()) {
            if (leaf.tenant() != null && !leaf.tenant().isBlank()) {
                out.add(leaf.tenant());
            }
        }
        return List.copyOf(out);
    }

    static final class Tally {
        int leaves;
        /** RDR-229 P3.4: gates 2 and 3 of the completion check as read at the start of the pass. */
        boolean gate2;
        boolean gate3;
        /**
         * The last leaf of the snapshot that can plan a build (its model and tenant parsed), or null when none can:
         * the {@code boot_work} total is not known before the pass has reached it (nexus-f3yqd.3).
         */
        Leaf lastPlannable;
        /** Leaves that were counted and planned (not skipped); the pass counts as done only if there was one. */
        int processedLeaves;
        int drops;
        int builds;
        int failedBuilds;
        int skippedLeaves;
        /** REINDEXes this pass finished. */
        int reindexes;
        /** What each processed leaf's counting transaction read, for the REINDEX rules (RDR-230 Technical Design 3). */
        final List<PciReindexState.Look> looks = new java.util.ArrayList<>();
        /** The counting transactions' wall time over the leaves processed: their sum and their longest. */
        long sumCountMillis;
        long maxCountMillis;
        /** Indexes this pass dropped, and those it built (for the canary, which runs after both). */
        final Set<String> droppedIndexes = new HashSet<>();
        final List<PciCanary.Target> builtTargets = new java.util.ArrayList<>();
        /** The registry's label of each live, unsuperseded collection with an id, by (model, tenant, id): the id canary's ring key. */
        final Map<String, String> idLabels = new java.util.HashMap<>();
    }

    /** The last leaf with a parsed model and tenant: the one whose plan completes the pass's count of builds. */
    static Leaf lastPlannable(List<Leaf> leaves) {
        Leaf last = null;
        for (Leaf leaf : leaves) {
            if (leaf.model() != null && leaf.tenant() != null) {
                last = leaf;
            }
        }
        return last;
    }

    /**
     * The pass has dealt with {@code leaf}, which planned {@code builds} builds (0 for a leaf that was skipped or has
     * nothing to build): tell {@code boot_work}, which fixes its total once the last plannable leaf is in.
     */
    private void noteLeaf(Leaf leaf, Tally tally, int builds) {
        PciBootWork work = bootWork;
        if (work != null) {
            work.planned(builds, leaf != tally.lastPlannable);
        }
    }

    static String leafId(Leaf leaf) {
        return leaf.schema() + "." + leaf.name();
    }

    /** @return false when the pass cannot go on (its connection is gone); the caller ends the loop */
    private boolean reconcileLeaf(PciBuilderSession.Pass pass, Leaf leaf, Tally tally) {
        if (leaf.model() == null || leaf.tenant() == null) {
            // Its indexes are unparsed (never touched) and a name cannot be made without both.
            return true;
        }
        try {
            if (pass.migrationInProgress()) {
                return false;
            }
        } catch (DataAccessException e) {
            // The idle builder session was ended between statements (the migrator of a rolling deploy does this), or
            // pg_locks could not be read, and unknown is not "free". One line, no stack: it is routine, and the next
            // pass starts clean.
            log.warn("event=pci_reconcile_pass_failed reason=migration_check sqlstate={} cause=\"{}\"",
                e.sqlState(), e.getMessage());
            pass.abandon(e);
            return false;
        }
        Planned planned;
        try {
            planned = pass.transaction(tx -> countAndPlan(tx, leaf, tally.gate2, tally.gate3));
        } catch (DataAccessException e) {
            tally.skippedLeaves++;
            noteLeaf(leaf, tally, 0);
            if (QUERY_CANCELED.equals(e.sqlState())) {
                log.warn("event=pci_count_timeout leaf={} timeout_ms={}", leafId(leaf), countTimeout.toMillis());
            } else {
                log.warn("event=pci_count_failed leaf={} sqlstate={} cause=\"{}\"", leafId(leaf), e.sqlState(),
                    e.getMessage());
            }
            return true;
        }
        if (planned == null) {
            tally.skippedLeaves++;
            noteLeaf(leaf, tally, 0);
            return true;
        }
        tally.processedLeaves++;
        tally.sumCountMillis += planned.countMillis();
        tally.maxCountMillis = Math.max(tally.maxCountMillis, planned.countMillis());
        ledger.apply(leaf, planned.anchors());
        planned.idLabels().forEach((id, name) -> tally.idLabels.put(PciCatalog.idKey(leaf.model(), leaf.tenant(), id), name));
        tally.looks.add(new PciReindexState.Look(leaf, planned.dimension(), planned.anchors().readings()));
        observeForCanary(leaf, planned);
        noteLeaf(leaf, tally, (int) planned.actions().stream().filter(a -> a instanceof Build).count());
        execute(pass, leaf, planned.dimension(), planned.anchors().writes(), planned.actions(), tally);
        return true;
    }

    // -- counting and planning, inside the transaction ---------------------------------------------------------

    private record Planned(List<Action> actions, int dimension, long countMillis, PciAnchorLedger.Reads anchors,
                           Map<Long, String> idLabels) { }

    /** The body of the counting transaction for one leaf; {@code null} means skip the leaf. */
    private Planned countAndPlan(DSLContext tx, Leaf leaf, boolean gate2, boolean gate3) {
        long startedNanos = System.nanoTime();
        String tenant = leaf.tenant();
        String model = leaf.model();
        if (!PciLeafCounting.begin(tx, leaf, countTimeout, tenantBinder)) {
            return null;
        }

        Integer dimension = tx.select(EMBEDDING_MODELS.DIMENSION).from(EMBEDDING_MODELS)
            .where(EMBEDDING_MODELS.EMBEDDING_MODEL.eq(model)).fetchOne(EMBEDDING_MODELS.DIMENSION);
        if (dimension == null) {
            log.warn("event=pci_reconcile_leaf_skipped leaf={} reason=unknown_model model={}", leafId(leaf), model);
            return null;
        }
        List<RegistryRow> registry = PciIdBuilds.registry(tx, tenant, model);
        forgetStaleBackoff(leaf, PciIdBuilds.buildable(registry));

        Set<String> indexed = new LinkedHashSet<>();
        for (Index index : leaf.indexes()) {
            if (index.parsed()) {
                indexed.add(index.collection());
                if (index.valid()) {
                    failures.remove(new LeafKey(leafId(leaf), index.collection()));
                }
            }
        }
        Set<String> toCount = PciIdBuilds.countTargets(leaf, indexed, registry);

        int b = settings.buildMinRows();
        Map<String, Integer> counts = new HashMap<>();
        for (String collection : toCount) {
            counts.put(collection, PciLeafCounting.count(tx, dimension, collection, model, tenant, b));
        }

        Instant now = clock.instant();
        Set<String> backedOff = backedOff(leaf, now);
        String inFlight = building.get();

        // Preliminary plan with no cap: its builds are the candidates, and the free slots follow from its drops.
        PciSettings uncapped = new PciSettings(settings.enabled(), b, settings.sweepSeconds(), Integer.MAX_VALUE);
        List<Action> preliminary = PciReconcilePlanner.plan(leaf, counts, registry, backedOff, inFlight, uncapped);
        long parsed = leaf.indexes().stream().filter(Index::parsed).count();
        // A leftover is not a parsed index (it is not in `parsed`), so its drop frees no cap slot either; nor is a
        // leftover of the leaf's own HNSW index.
        long drops = preliminary.stream()
            .filter(a -> a instanceof Drop d && d.reason() != PciReconcilePlanner.DropReason.REINDEX_LEFTOVER
                && d.reason() != PciReconcilePlanner.DropReason.LEAF_REINDEX_LEFTOVER)
            .count();
        long slots = settings.maxPerLeaf() - (parsed - drops);
        List<String> candidates = preliminary.stream().filter(a -> a instanceof Build)
            .map(a -> ((Build) a).collection()).toList();
        boolean recounted = slots > 0 && candidates.size() > slots;
        if (recounted) {
            int rankCap = Math.multiplyExact(b, RANK_CAP_FACTOR);
            for (String collection : candidates) {
                counts.put(collection, PciLeafCounting.count(tx, dimension, collection, model, tenant, rankCap));
            }
        }
        // RDR-229 P3.4: the id half. The null-id read runs only once gate 2 holds, for the collections that could build.
        var plan = PciIdBuilds.plan(tx, leaf, dimension, counts, registry, backedOff, inFlight, settings, gate2, gate3,
            Math.multiplyExact(b, RANK_CAP_FACTOR));
        List<Action> actions = plan.actions();
        PciAnchorLedger.Reads anchors = readAnchors(tx, leaf, dimension, actions, now);
        // One reading, logged and carried: the pass line's sum and max are over exactly these figures. The per-leaf
        // line stays DEBUG (one per leaf per pass is a lot of lines at 20,000 collections); the pass line is INFO.
        long tookMs = (System.nanoTime() - startedNanos) / 1_000_000;
        log.debug("event=pci_count_done leaf={} counted={} recounted={} actions={} took_ms={}", leafId(leaf),
            counts.size(), recounted ? candidates.size() : 0, actions.size(), tookMs);
        Map<Long, String> labels = new java.util.HashMap<>();
        for (RegistryRow row : registry) {
            if (row.collectionId() != null && row.live() && !row.superseded()) {
                labels.put(row.collectionId(), row.name());
            }
        }
        return new Planned(actions, dimension, tookMs, anchors, labels);
    }

    /** The anchor reads of {@link PciAnchorLedger#read} for the valid parsed indexes the plan does not drop. */
    private PciAnchorLedger.Reads readAnchors(DSLContext tx, Leaf leaf, int dim, List<Action> actions, Instant now) {
        Set<String> dropping = new HashSet<>();
        for (Action action : actions) {
            if (action instanceof Drop d) {
                dropping.add(d.name());
            }
        }
        List<Index> subjects = leaf.indexes().stream()
            .filter(i -> (i.parsed() || i.idParsed()) && i.valid() && !dropping.contains(i.name())).toList();
        return subjects.isEmpty() ? PciAnchorLedger.Reads.NONE : ledger.read(tx, leaf, dim, subjects, now);
    }

    // -- execution, in autocommit ------------------------------------------------------------------------------

    private void execute(PciBuilderSession.Pass pass, Leaf leaf, int dim, List<PciAnchorLedger.Write> writes,
                         List<Action> actions, Tally tally) {
        // Anchor writes (adoptions, rebuilds, stamps) are quick and independent of the plan; a build can hold the pass
        // for half an hour.
        for (PciAnchorLedger.Write w : writes) {
            if (!pass.live() || Thread.currentThread().isInterrupted()) {
                return;
            }
            // A rebuilt index's rows are a new baseline: the next look must not read the fall since the old build as a
            // bulk delete.
            if (ledger.write(pass, leaf, w) && w.kind() == PciAnchorLedger.Kind.REBUILT) {
                reindex.rebaseline(w.index().name(), w.rows());
            }
        }
        // nexus-f3yqd (F): a build that follows the drop of its failed index runs only if that drop happened.
        Set<String> failedDrops = new HashSet<>();
        for (Action action : actions) {
            if (action instanceof Drop d && d.reason() == PciReconcilePlanner.DropReason.FAILED_BUILD) {
                failedDrops.add(d.name());
            }
        }
        for (Action action : actions) {
            if (!pass.live() || Thread.currentThread().isInterrupted()) {
                return;
            }
            switch (action) {
                case SkipCountSuspect s -> log.warn("event=pci_count_zero_indexed leaf={} collection={} "
                    + "detail=\"a live, indexed collection counted zero rows: a counting failure, never acted on\"",
                    leafId(leaf), s.collection());
                case Drop d -> drop(pass, leaf, d, tally);
                case Build b when failedDrops.contains(b.name()) && !tally.droppedIndexes.contains(b.name()) -> {
                    log.info("event=pci_build_skipped reason=failed_index_not_dropped leaf={} index={} "
                        + "detail=\"the next pass drops it again and builds\"", leafId(leaf), b.name());
                    // nexus-f3yqd.3: noteLeaf counted this build when the plan was made; it will not run, so it is
                    // no longer pending for boot_work's done (the total, counted from the plan, does not move).
                    PciBootWork work = bootWork;
                    if (work != null) {
                        work.buildSkipped();
                    }
                }
                case Build b -> build(pass, leaf, dim, b, tally);
                // plan() never returns one: a REINDEX is chosen across the whole pass and runs after its drops and builds.
                case Reindex r -> log.warn("event=pci_reindex_in_leaf_plan leaf={} index={}", leafId(leaf), r.name());
            }
        }
    }

    /**
     * After a successful build: count the collection's rows (uncapped) and the index's size in a counting transaction
     * and write {@code origin=built}. {@code built_at} is taken before the count, so a row written during it counts in
     * {@code written_since} as well as in {@code rows}: the figure errs toward more work. Anything that goes wrong
     * leaves the index unanchored, which the next pass adopts; the build itself stands.
     */
    private void anchorAfterBuild(PciBuilderSession.Pass pass, Leaf leaf, int dim, Build build) {
        writeAnchor(pass, leaf, dim, build, false);
    }

    /**
     * The anchor write of {@link #anchorAfterBuild}, for a built index (after a build) or a REINDEXed one
     * ({@code afterReindex}; {@link PciReindexPhase}). After a build a failure leaves the index with no anchor and the
     * next pass adopts it; after a REINDEX the old anchor is still there, so nothing adopts it, and the phase retries the
     * write at the start of the next pass and does not judge the index in between.
     *
     * @return whether the anchor was written
     */
    boolean writeAnchor(PciBuilderSession.Pass pass, Leaf leaf, int dim, Build build, boolean afterReindex) {
        if (build.collection() == null) {   // a leaf's own HNSW index (P2.3): its own anchor, the same retry
            return PciLeafIndexReads.anchorAfterReindex(pass, leaf, build.name(), clock, countTimeout);
        }
        String next = afterReindex ? "the next pass retries it" : "the next pass adopts it";
        Instant builtAt = clock.instant();
        Index index = build.byId() ? Index.byId(build.name(), true, build.collectionId(), null)
            : new Index(build.name(), true, build.collection());
        long[] figures;
        try {
            figures = pass.transaction(tx -> {
                if (!PciLeafCounting.begin(tx, leaf, countTimeout, tenantBinder)) {
                    return null;
                }
                PgSession.setLocal(tx, "lock_timeout", SIZE_LOCK_TIMEOUT);
                PciAnchorReads.IndexFacts facts = PciAnchorReads.indexFacts(tx, leaf.schema(), build.name());
                if (facts == null) {
                    return null;
                }
                return new long[] {
                    build.byId() ? PciAnchorReads.countRowsById(tx, leaf.model(), leaf.tenant(), build.collectionId())
                        : PciAnchorReads.countRows(tx, dim, leaf.model(), leaf.tenant(), build.collection()),
                    facts.bytes()};
            });
        } catch (DataAccessException e) {
            log.warn("event=pci_anchor_not_written origin=built index={} leaf={} reason=count_failed sqlstate={} "
                + "detail=\"the index stands; {}\"", build.name(), leafId(leaf), e.sqlState(), next);
            return false;
        }
        if (figures == null) {
            log.warn("event=pci_anchor_not_written origin=built index={} leaf={} reason=count_skipped "
                + "detail=\"the tenant did not bind or the index is gone; {} if it exists\"", build.name(),
                leafId(leaf), next);
            return false;
        }
        var written = pass.write(leaf, index,
            new PciCatalog.Anchor(builtAt, figures[0], figures[1], PciCatalog.ORIGIN_BUILT));
        DdlOutcome outcome = written.outcome();
        if (outcome == DdlOutcome.DONE) {
            ledger.put(leafId(leaf), build.name(), new IndexReading(written.anchor(), figures[1], figures[0], 0, builtAt));
            log.info("event=pci_anchor_written origin=built index={} leaf={} rows={} bytes={}", build.name(),
                leafId(leaf), figures[0], figures[1]);
            refreshSweep("anchor", false);
            return true;
        }
        log.info("event=pci_anchor_not_written origin=built index={} leaf={} outcome={} detail=\"{}\"", build.name(),
            leafId(leaf), outcome, next);
        return false;
    }

    // -- REINDEX (RDR-230 Technical Design 3 and 4) -----------------------------------------------------------

    /**
     * After the pass's drops and builds: choose the REINDEXes from what the leaves' counting transactions read, and run
     * at most {@link PciReindexPolicy#MAX_PER_PASS}, one at a time, largest first. The triggers are
     * {@link PciReconcilePlanner#reindexes} (a pure function); the memory they read is {@link PciReindexState}.
     */
    private void runReindexes(PciBuilderSession.Pass pass, PciCatalog.Snapshot snapshot, Tally tally) {
        Runnable hook = reindexPhaseHook;
        if (hook != null) {
            hook.run();
        }
        Instant now = clock.instant();
        Map<String, PciReindexState.Subject> subjects = new HashMap<>();
        Set<String> snapshotIndexes = new HashSet<>();
        for (Leaf leaf : snapshot.leaves()) {
            leaf.indexes().forEach(i -> snapshotIndexes.add(i.name()));
        }
        // A leaf's own HNSW index keeps its failure backoff like a pci_ index: its name is in the snapshot.
        for (Leaf leaf : snapshot.leaves()) {
            leaf.hnswIndexes().forEach(i -> snapshotIndexes.add(i.name()));
        }
        // A fresh anchor a REINDEX could not write last pass. This pass's looks still carry the OLD anchor, so an index
        // whose write just landed is judged on the next pass, from its new one.
        Set<String> justAnchored = phase.retryOwedAnchors(pass, snapshotIndexes);
        java.util.function.Function<String, PciCanary.Result> latest = name -> canary == null ? null : canary.latest(name);
        List<PciReindexPolicy.Candidate> candidates = reindex.candidates(tally.looks, tally.droppedIndexes,
            snapshotIndexes, justAnchored, latest, now, subjects);
        // A green canary run ends a red_unfixed in the anchor (nexus-4lnn1.1.10).
        for (PciReindexState.Subject s : subjects.values()) {
            PciCanary.Result r = latest.apply(s.index().name());
            if (pass.live() && s.reading().anchor().redUnfixed() != null && r != null
                && r.state() == PciCanary.State.GREEN) {
                ledger.clearRedUnfixed(pass, s.leaf(), s.index());
            }
        }
        List<Reindex> triggered = withLeafSizeCandidates(pass, tally, PciReconcilePlanner.reindexes(candidates, now),
            now);
        for (Reindex r : triggered) {
            if (r.trigger() == PciReconcilePlanner.Trigger.BULK_DELETE) {
                reindex.bulkDeleteOwed(r.name());   // the limit may defer it, and the next check sees no fall
            }
        }
        for (Reindex r : PciReconcilePlanner.firstPerPass(triggered)) {
            if (!pass.live() || Thread.currentThread().isInterrupted()) {
                return;
            }
            if (r.collection() == null) {
                reindexLeafIndex(pass, snapshot, r, tally);   // a leaf's own HNSW index serves no collection
                continue;
            }
            PciReindexState.Subject subject = subjects.get(r.name());
            if (subject == null) {
                log.warn("event=pci_reindex_skipped reason=no_subject index={}", r.name());
                continue;
            }
            if (phase.reindexOne(pass, subject, r)) {
                tally.reindexes++;
            }
        }
    }

    /**
     * RDR-230 Technical Design 3, the leaf size rule: add the {@link PciReconcilePlanner.Trigger#LEAF_SIZE} REINDEXes
     * of the leaves' own HNSW indexes to the pci_ ones and sort the lot largest first ({@code BY_SIZE_THEN_NAME}), so
     * they share the per-pass limit. The decision is {@link PciLeafReindexRule}; what it reads, and how a lock on an
     * index is bounded, is {@link PciLeafIndexReads}. The count of indexes it cannot judge for want of a reference is
     * reported every pass, so it falls back to 0 once an anchor exists. (Only runs with {@code NX_SEARCH_PCI_REINDEX}
     * on: with it off nothing is read, no candidate is added, and {@code unanchored} stays 0, since the rule is off,
     * not inactive for want of a reference.)
     */
    private List<Reindex> withLeafSizeCandidates(PciBuilderSession.Pass pass, Tally tally, List<Reindex> triggered,
                                                 Instant now) {
        if (tally.looks.isEmpty()) {
            noteLeafUnanchored(0);
            return triggered;
        }
        Set<String> schemas = new HashSet<>();
        tally.looks.forEach(look -> schemas.add(look.leaf().schema()));
        Map<String, Long> estimates = PciLeafIndexReads.rowEstimates(pass, schemas, countTimeout);
        if (estimates == null) {
            return triggered;
        }
        Map<String, ?> owed = reindex.owedAnchors();   // a leaf index owed its anchor is no candidate until it lands
        PciLeafReindexRule.Scan scan = PciLeafReindexRule.scan(tally.looks, estimates::get,
            (leaf, own) -> PciLeafIndexReads.facts(pass, leaf, own, countTimeout),
            name -> reindex.backedOff(name, now) || owed.containsKey(name), now);
        noteLeafUnanchored(scan.unanchored());
        scan.rebuilt().forEach(r -> PciLeafIndexReads.reanchorRebuilt(pass, r, clock, countTimeout));
        scan.stamps().forEach(st -> PciLeafIndexReads.stamp(pass, st));
        if (scan.candidates().isEmpty()) {
            return triggered;
        }
        List<Reindex> all = new java.util.ArrayList<>(triggered);
        all.addAll(scan.candidates());
        all.sort(PciReconcilePlanner.BY_SIZE_THEN_NAME);
        return List.copyOf(all);
    }

    /**
     * A {@link PciReconcilePlanner.Trigger#LEAF_SIZE} REINDEX of a leaf's own HNSW index ({@code r.collection()} is
     * null). The leaf and index are resolved from {@code snapshot} by name; one that is not there is logged and skipped,
     * with no statement, as a pci_ target with no subject is. The REINDEX goes through
     * {@link PciBuilderSession.Pass#reindex(Leaf, PciCatalog.LeafIndex)}, and the outcome is recorded in
     * {@link PciReindexState} exactly as {@link #reindexOne} does (started/finished, failure backoff, no backoff for a
     * pass that ended). On DONE the index is anchored ({@link PciLeafIndexReads#anchorAfterReindex}, through
     * {@link #writeAnchor}); an anchor that is not written is owed and retried like a pci_ index's. There is no canary
     * re-measure (the leaf index serves no one collection) and no VACUUM.
     */
    void reindexLeafIndex(PciBuilderSession.Pass pass, PciCatalog.Snapshot snapshot, Reindex r, Tally tally) {
        Leaf leaf = null;
        PciCatalog.LeafIndex own = null;
        for (Leaf candidate : snapshot.leaves()) {
            for (PciCatalog.LeafIndex i : candidate.hnswIndexes()) {
                if (i.name().equals(r.name())) {
                    leaf = candidate;
                    own = i;
                }
            }
        }
        if (leaf == null) {
            log.warn("event=pci_reindex_skipped reason=no_leaf_index index={}", r.name());
            return;
        }
        String name = r.name();
        log.info("event=pci_reindex_started leaf={} index={} collection={} trigger={} bytes={}", leafId(leaf), name,
            r.collection(), r.trigger().label(), r.bytes());
        reindex.running(name);
        DdlOutcome outcome;
        try {
            outcome = pass.reindex(leaf, own);
        } finally {
            reindex.running(null);
        }
        switch (outcome) {
            case DONE -> {
                reindex.finished(name, r.trigger(), clock.instant());
                tally.reindexes++;
                log.info("event=pci_index_reindexed leaf={} index={} collection={} trigger={} bytes_before={}",
                    leafId(leaf), name, r.collection(), r.trigger().label(), r.bytes());
                Runnable hook = afterLeafReindexHook;
                if (hook != null) {
                    hook.run();
                }
                // Owed like a pci_ index's: retried at the start of the next pass, and not a candidate until it lands.
                if (!(pass.live() && writeAnchor(pass, leaf, own.dimension(), new Build(name, null), true))) {
                    reindex.anchorOwed(name, leaf, own.dimension(), null);
                }
            }
            case FAILED, REJECTED -> {
                int n = reindex.failed(name, clock.instant());
                log.warn("event=pci_reindex_failed leaf={} index={} collection={} trigger={} consecutive_failures={} "
                    + "next_retry_at={}", leafId(leaf), name, r.collection(), r.trigger().label(), n,
                    reindex.nextRetryAt(name));
            }
            default -> log.info("event=pci_reindex_not_done leaf={} index={} outcome={}", leafId(leaf), name, outcome);
        }
    }

    private void drop(PciBuilderSession.Pass pass, Leaf leaf, Drop drop, Tally tally) {
        Index index = java.util.stream.Stream.concat(leaf.indexes().stream(), leaf.leafLeftovers().stream())
            .filter(i -> i.name().equals(drop.name())).findFirst().orElse(null);
        if (index == null) {
            return;
        }
        DdlOutcome outcome = pass.drop(leaf, index);
        if (outcome == DdlOutcome.DONE) {
            tally.drops++;
            tally.droppedIndexes.add(drop.name());
            if (canary != null) {
                canary.dropped(drop.name());
            }
            log.info("event=pci_index_dropped leaf={} index={} collection={} reason={}", leafId(leaf), drop.name(),
                drop.collection(), drop.reason().label());
            // A router set that still lists the dropped index sends its collection to the serving ef_search with no
            // graph behind it: the unsafe direction. A refresh that failed or was skipped is tried once more.
            refreshSweep("drop", true);
        } else if (outcome == DdlOutcome.STALE) {
            // The re-read before the DROP found the leftover valid now or gone: nothing was dropped, so nothing is
            // counted or logged as a drop. pci_drop_skipped_stale (the session) says which.
            log.info("event=pci_index_drop_stale leaf={} index={} reason={} detail=\"not dropped; the next pass "
                + "judges what is there\"", leafId(leaf), drop.name(), drop.reason().label());
        } else {
            log.info("event=pci_index_drop_not_done leaf={} index={} outcome={} detail=\"the next pass retries\"",
                leafId(leaf), drop.name(), outcome);
        }
    }

    private void build(PciBuilderSession.Pass pass, Leaf leaf, int dim, Build build, Tally tally) {
        LeafKey key = new LeafKey(leafId(leaf),
            build.byId() ? PciReconcilePlanner.idBackoffKey(build.collectionId()) : build.collection());
        building.set(build.name());
        PciBootWork work = bootWork;
        PciBootWork.Watch watch = work == null ? null : work.watchBuild();
        DdlOutcome outcome;
        try {
            outcome = build.byId() ? pass.buildById(leaf, dim, build.collectionId())
                : pass.build(leaf, dim, build.collection());
        } finally {
            building.set(null);
            if (watch != null) {
                watch.close();
            }
        }
        switch (outcome) {
            case DONE -> {
                tally.builds++;
                failures.remove(key);
                // The canary measures the family the serving statement selects by: the name indexes before the latch, the id indexes after it.
                if (canary != null && build.byId() == tally.gate3) {
                    canary.built(build.name());
                    tally.builtTargets.add(new PciCanary.Target(leafId(leaf), leaf.tenant(), leaf.model(),
                        build.collection(), build.name(), build.byId() ? build.collectionId() : null));
                }
                log.info("event=pci_index_built leaf={} index={} collection={}", leafId(leaf), build.name(),
                    build.collection());
                refreshSweep("build", false);
                if (pass.live()) {
                    anchorAfterBuild(pass, leaf, dim, build);
                }
            }
            case FAILED, REJECTED -> {
                tally.failedBuilds++;
                recordFailure(key, build.name());
            }
            // The pass ended before the statement went out (a migration, a privilege error), or the backend was ended
            // under it (a migration walk terminated it; the network dropped): not this collection's fault.
            default -> log.info("event=pci_index_build_not_done leaf={} index={} outcome={}", leafId(leaf),
                build.name(), outcome);
        }
    }

    private void refreshSweep(String after, boolean retryOnce) {
        if (sweep.refresh()) {
            return;
        }
        if (retryOnce && sweep.refresh()) {
            log.info("event=pci_reconcile_refresh_retried after={} applied=true", after);
            return;
        }
        if (retryOnce) {
            log.warn("event=pci_reconcile_refresh_not_applied after={} retried=true detail=\"the router set may still "
                + "list a dropped index until the read half's next tick\"", after);
        } else {
            log.debug("event=pci_reconcile_refresh_not_applied after={} detail=\"the read half repairs the set on "
                + "its next tick\"", after);
        }
    }

    // -- backoff ---------------------------------------------------------------------------------------------

    private void recordFailure(LeafKey key, String indexName) {
        Instant now = clock.instant();
        Failure stamped = failures.compute(key, (k, old) -> {
            int n = old == null ? 1 : old.consecutive() + 1;
            return new Failure(n, now.plus(retryDelay(n)));
        });
        log.warn("event=pci_build_failed leaf={} collection={} index={} consecutive_failures={} next_retry_at={}",
            key.leaf(), key.collection(), indexName, stamped.consecutive(), stamped.nextRetryAt());
        if (stamped.consecutive() == FAILING_AFTER) {
            log.warn("event=pci_build_failing leaf={} collection={} index={} consecutive_failures={}", key.leaf(),
                key.collection(), indexName, stamped.consecutive());
        }
    }

    /** {@link #FIRST_RETRY} after the first failure, doubling, capped at {@link #MAX_RETRY}. */
    static Duration retryDelay(int consecutiveFailures) {
        Duration delay = FIRST_RETRY;
        for (int i = 1; i < consecutiveFailures && delay.compareTo(MAX_RETRY) < 0; i++) {
            delay = delay.multipliedBy(2);
        }
        return delay.compareTo(MAX_RETRY) > 0 ? MAX_RETRY : delay;
    }

    private Set<String> backedOff(Leaf leaf, Instant now) {
        String id = leafId(leaf);
        Set<String> out = new HashSet<>();
        failures.forEach((key, failure) -> {
            if (key.leaf().equals(id) && failure.nextRetryAt() != null && failure.nextRetryAt().isAfter(now)) {
                out.add(key.collection());
            }
        });
        return out;
    }

    /**
     * A collection that left the registry, was superseded or is no longer live can no longer build, so it has nothing
     * to retry and must not hold {@code failing} above zero.
     */
    private void forgetStaleBackoff(Leaf leaf, Set<String> buildable) {
        String id = leafId(leaf);
        failures.keySet().removeIf(k -> k.leaf().equals(id) && !buildable.contains(k.collection()));
    }
}
