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

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
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
 *       {@link #COUNT_TIMEOUT} (30 s to start; Step 3 measures it) and {@code plan_cache_mode = force_custom_plan}
 *       (a generic plan would not prune to the leaf), all transaction-local.</li>
 *   <li>Read {@code current_setting('nexus.tenant')} back. {@code nexus.chunks} is FORCE ROW LEVEL SECURITY, so the
 *       owner role reads nothing for any other tenant, and a registry read under the wrong tenant is an EMPTY list,
 *       which the planner reads as "everything was deleted". On a mismatch the leaf is skipped
 *       ({@code event=pci_count_tenant_mismatch}) and nothing is dropped or built.</li>
 *   <li>Read the registry ({@code catalog_collections}: name, lifecycle_state, superseded_by) for that tenant and
 *       the leaf's model, and the model's dimension. A failed read skips the leaf; it is never an empty registry.</li>
 *   <li>Count rows per collection, capped at B+1, with {@link PgVectorRepository#probeSelectedRowsQuery} (the router's
 *       probe: a count over a {@code LIMIT B+1} subquery on the leaf's (model, tenant, collection) key). Only
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
 * {@code refresh()} that returns false (a lock-wait skip or a failed read) is not fatal; the read half's next tick
 * repairs the set. When the pass ends (a migration, a lost connection, a privilege error) the remaining actions
 * are not sent, and a build that never started is not charged to backoff.
 *
 * <p><b>Retry state is in memory, on the lock holder, and is lost on restart.</b> After a failed build of
 * (leaf, collection) the first retry comes no sooner than {@link #FIRST_RETRY} later, doubling to
 * {@link #MAX_RETRY}; the collection is handed to the planner as backed off until then. After
 * {@link #FAILING_AFTER} consecutive failures it counts as <i>failing</i>. A restart forgets all of it, so a build
 * that always fails is retried at once after each boot and then backs off again. A success clears the entry, and so
 * does a collection leaving the registry or gaining a valid index.
 *
 * <p><b>Status for the status object</b> (nexus-43ulx.23 builds the object; {@link #status()} is its source). Only
 * the lock holder reports {@code building} (0 or 1), {@code failing} and the last DDL pass time: other engines cannot
 * see another role's {@code pg_stat_progress_create_index} and hold none of this state. A non-holder reports
 * {@code builder_state = standby} (or {@code off}, {@code auth_failed}) and nulls.
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
     * @param lastDdlPassAt when the last pass that held the lock and ran to its end finished; {@code null} unless
     *                     this engine holds the lock, or before the first such pass
     */
    public record DdlStatus(BuilderState builderState, Integer building, Integer failing, Instant lastDdlPassAt) { }

    /** What one pass did; for tests and the pass log line. */
    record PassReport(BuilderState state, boolean ranToEnd, int leaves, int drops, int builds, int failedBuilds,
                      int skippedLeaves) {
        static PassReport inactive(BuilderState state) {
            return new PassReport(state, false, 0, 0, 0, 0, 0);
        }
    }

    private record LeafKey(String leaf, String collection) { }

    private record Failure(int consecutive, Instant nextRetryAt) { }

    private final PciCatalog catalog;
    private final PciBuilderSession builder;
    private final PciIndexSweep sweep;
    private final PciSettings settings;
    private final Clock clock;
    private final TenantBinder tenantBinder;
    private final Duration countTimeout;
    private final Duration period;

    private final Map<LeafKey, Failure> failures = new ConcurrentHashMap<>();
    private final AtomicReference<String> building = new AtomicReference<>();
    private final AtomicInteger passes = new AtomicInteger();
    private volatile boolean holder;
    private volatile Instant lastDdlPassAt;
    private volatile BuilderState lastState;

    private final Object lifecycle = new Object();
    private ScheduledExecutorService scheduler;   // guarded by lifecycle
    private boolean stopped;                      // guarded by lifecycle

    public PciReconciler(PciCatalog catalog, PciBuilderSession builder, PciIndexSweep sweep, PciSettings settings) {
        this(catalog, builder, sweep, settings, Clock.systemUTC(), SET_LOCAL_TENANT, COUNT_TIMEOUT,
            Duration.ofSeconds(Objects.requireNonNull(settings, "settings").sweepSeconds()));
    }

    PciReconciler(PciCatalog catalog, PciBuilderSession builder, PciIndexSweep sweep, PciSettings settings,
                  Clock clock, TenantBinder tenantBinder, Duration countTimeout, Duration period) {
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
        return new PciReconciler(new PciCatalog(ds),
            new PciBuilderSession(admin.url(), admin.user(), admin.password(), bootNonce, settings), sweep, settings);
    }

    // -- status ----------------------------------------------------------------------------------------------

    /** The DDL half's status; see the class comment for who reports what. */
    public DdlStatus status() {
        if (!settings.enabled()) {
            return new DdlStatus(BuilderState.OFF, null, null, null);
        }
        BuilderState state = lastState != null ? lastState : builder.state();
        if (!holder) {
            return new DdlStatus(state, null, null, null);
        }
        int failing = (int) failures.values().stream().filter(f -> f.consecutive() >= FAILING_AFTER).count();
        return new DdlStatus(state, building.get() != null ? 1 : 0, failing, lastDdlPassAt);
    }

    /** Passes started since boot, for tests. */
    int passes() {
        return passes.get();
    }

    // -- schedule --------------------------------------------------------------------------------------------

    /**
     * Start the DDL task: one pass at once, then one {@code period} after each pass ends. Idempotent while running.
     * Does nothing when {@code NX_SEARCH_PCI=0}. Call after the service and the read half have started. A reconciler
     * that has been stopped does not restart.
     */
    public void start() {
        if (!settings.enabled()) {
            log.info("event=pci_reconciler_off reason=NX_SEARCH_PCI_0");
            return;
        }
        synchronized (lifecycle) {
            if (stopped) {
                throw new IllegalStateException("the PCI reconciler was stopped and does not restart");
            }
            if (scheduler != null) {
                return;
            }
            scheduler = Executors.newSingleThreadScheduledExecutor(r -> {
                Thread t = new Thread(r, THREAD_NAME);
                t.setDaemon(true);
                return t;
            });
            scheduler.scheduleWithFixedDelay(this::scheduledPass, 0, period.toMillis(), TimeUnit.MILLISECONDS);
        }
        log.info("event=pci_reconciler_started period_seconds={}", period.toSeconds());
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
     * {@code CREATE INDEX CONCURRENTLY} ignores the interrupt, and the shutdown hook ends the builder's backend with
     * {@code BackendReaper.terminateAtShutdown} instead. Harmless when never started or already stopped.
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

    /** Run one pass now, on the calling thread. Returns what it did. */
    PassReport reconcileOnce() {
        passes.incrementAndGet();
        if (!settings.enabled()) {
            lastState = BuilderState.OFF;
            return PassReport.inactive(BuilderState.OFF);
        }
        long startedNanos = System.nanoTime();
        PciBuilderSession.Pass pass;
        try {
            pass = builder.open();
        } catch (IllegalStateException e) {
            log.warn("event=pci_reconcile_pass_failed reason=builder_open cause=\"{}\"", e.getMessage());
            return PassReport.inactive(builder.state());
        }
        try (pass) {
            BuilderState state = pass.state();
            lastState = state;
            holder = state == BuilderState.OK || state == BuilderState.NO_PRIVILEGE;
            if (!holder || !pass.live()) {
                return PassReport.inactive(state);
            }
            PciCatalog.Snapshot snapshot;
            try {
                snapshot = catalog.read();
            } catch (RuntimeException e) {
                log.warn("event=pci_reconcile_pass_failed reason=catalog_read cause=\"{}\"", e.getMessage());
                return PassReport.inactive(state);
            }
            if (snapshot.leaves().isEmpty()) {
                // No installed schema produces this: a failed read, never "there are no indexes".
                log.warn("event=pci_reconcile_pass_failed reason=no_partition_leaves");
                return PassReport.inactive(state);
            }
            Tally tally = new Tally();
            for (Leaf leaf : snapshot.leaves()) {
                if (!pass.live() || Thread.currentThread().isInterrupted()) {
                    break;
                }
                tally.leaves++;
                reconcileLeaf(pass, leaf, tally);
            }
            boolean ranToEnd = pass.live() && !Thread.currentThread().isInterrupted();
            if (ranToEnd) {
                lastDdlPassAt = clock.instant();
            }
            log.info("event=pci_reconcile_pass ran_to_end={} leaves={} drops={} builds={} failed_builds={} "
                + "skipped_leaves={} took_ms={}", ranToEnd, tally.leaves, tally.drops, tally.builds,
                tally.failedBuilds, tally.skippedLeaves, (System.nanoTime() - startedNanos) / 1_000_000);
            return new PassReport(pass.state(), ranToEnd, tally.leaves, tally.drops, tally.builds,
                tally.failedBuilds, tally.skippedLeaves);
        } finally {
            building.set(null);
        }
    }

    private static final class Tally {
        int leaves;
        int drops;
        int builds;
        int failedBuilds;
        int skippedLeaves;
    }

    private static String leafId(Leaf leaf) {
        return leaf.schema() + "." + leaf.name();
    }

    private void reconcileLeaf(PciBuilderSession.Pass pass, Leaf leaf, Tally tally) {
        if (leaf.model() == null || leaf.tenant() == null) {
            // Its indexes are unparsed (never touched) and a name cannot be made without both.
            return;
        }
        if (pass.migrationInProgress()) {
            return;
        }
        Planned planned;
        try {
            planned = pass.transaction(tx -> countAndPlan(tx, leaf));
        } catch (DataAccessException e) {
            tally.skippedLeaves++;
            if (QUERY_CANCELED.equals(e.sqlState())) {
                log.warn("event=pci_count_timeout leaf={} timeout_ms={}", leafId(leaf), countTimeout.toMillis());
            } else {
                log.warn("event=pci_count_failed leaf={} sqlstate={} cause=\"{}\"", leafId(leaf), e.sqlState(),
                    e.getMessage());
            }
            return;
        }
        if (planned == null) {
            tally.skippedLeaves++;
            return;
        }
        execute(pass, leaf, planned.dimension(), planned.actions(), tally);
    }

    // -- counting and planning, inside the transaction ---------------------------------------------------------

    private record Planned(List<Action> actions, int dimension) { }

    /** The body of the counting transaction for one leaf; {@code null} means skip the leaf. */
    private Planned countAndPlan(DSLContext tx, Leaf leaf) {
        long startedNanos = System.nanoTime();
        String tenant = leaf.tenant();
        String model = leaf.model();
        // setLocal binds the matching network timeout with the statement timeout (nexus-u9zkn); the pass puts the
        // connection's own, longer one back when the transaction ends.
        PgSession.setLocal(tx, "statement_timeout", Long.toString(countTimeout.toMillis()));
        tx.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val("plan_cache_mode"),
            DSL.val("force_custom_plan"), DSL.inline(true))).fetch();
        tenantBinder.bind(tx, tenant);
        String bound = tx.select(DSL.function("current_setting", SQLDataType.VARCHAR, DSL.val("nexus.tenant"),
            DSL.inline(true))).fetchOne(0, String.class);
        if (!tenant.equals(bound)) {
            log.error("event=pci_count_tenant_mismatch leaf={} expected_tenant={} actual_tenant={} "
                + "detail=\"counts under the wrong tenant read other rows or none; the leaf is skipped\"",
                leafId(leaf), tenant, bound);
            return null;
        }

        Integer dimension = tx.select(EMBEDDING_MODELS.DIMENSION).from(EMBEDDING_MODELS)
            .where(EMBEDDING_MODELS.EMBEDDING_MODEL.eq(model)).fetchOne(EMBEDDING_MODELS.DIMENSION);
        if (dimension == null) {
            log.warn("event=pci_reconcile_leaf_skipped leaf={} reason=unknown_model model={}", leafId(leaf), model);
            return null;
        }
        List<RegistryRow> registry = tx.select(CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.LIFECYCLE_STATE,
                CATALOG_COLLECTIONS.SUPERSEDED_BY)
            .from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant))
            .and(CATALOG_COLLECTIONS.EMBEDDING_MODEL.eq(model))
            .fetch(r -> new RegistryRow(r.value1(), r.value2(), r.value3()));

        Set<String> listed = new HashSet<>();
        for (RegistryRow row : registry) {
            listed.add(row.name());
        }
        forgetStaleBackoff(leaf, listed);

        Set<String> indexed = new LinkedHashSet<>();
        for (Index index : leaf.indexes()) {
            if (index.parsed()) {
                indexed.add(index.collection());
                if (index.valid()) {
                    failures.remove(new LeafKey(leafId(leaf), index.collection()));
                }
            }
        }
        Set<String> toCount = new LinkedHashSet<>(indexed);
        for (RegistryRow row : registry) {
            // Only a collection that can build counts for anything; a quarantined one without an index decides nothing.
            if (row.live() && !row.superseded()) {
                toCount.add(row.name());
            }
        }

        int b = settings.buildMinRows();
        Map<String, Integer> counts = new HashMap<>();
        for (String collection : toCount) {
            counts.put(collection, count(tx, dimension, collection, model, tenant, b));
        }

        Instant now = clock.instant();
        Set<String> backedOff = backedOff(leaf, now);
        String inFlight = building.get();

        // Preliminary plan with no cap: its builds are the candidates, and the free slots follow from its drops.
        PciSettings uncapped = new PciSettings(settings.enabled(), b, settings.sweepSeconds(), Integer.MAX_VALUE);
        List<Action> preliminary = PciReconcilePlanner.plan(leaf, counts, registry, backedOff, inFlight, uncapped);
        long parsed = leaf.indexes().stream().filter(Index::parsed).count();
        long drops = preliminary.stream().filter(a -> a instanceof Drop).count();
        long slots = settings.maxPerLeaf() - (parsed - drops);
        List<String> candidates = preliminary.stream().filter(a -> a instanceof Build)
            .map(a -> ((Build) a).collection()).toList();
        boolean recounted = slots > 0 && candidates.size() > slots;
        if (recounted) {
            int rankCap = Math.multiplyExact(b, RANK_CAP_FACTOR);
            for (String collection : candidates) {
                counts.put(collection, count(tx, dimension, collection, model, tenant, rankCap));
            }
        }
        List<Action> actions = PciReconcilePlanner.plan(leaf, counts, registry, backedOff, inFlight, settings);
        log.debug("event=pci_count_done leaf={} counted={} recounted={} actions={} took_ms={}", leafId(leaf),
            counts.size(), recounted ? candidates.size() : 0, actions.size(),
            (System.nanoTime() - startedNanos) / 1_000_000);
        return new Planned(actions, dimension);
    }

    /** Rows of {@code collection} on the leaf's (model, tenant) key, stopped at {@code cap + 1}. */
    private static int count(DSLContext tx, int dim, String collection, String model, String tenant, int cap) {
        Integer n = PgVectorRepository.probeSelectedRowsQuery(tx, dim, new String[] {collection}, model, tenant, cap)
            .fetchOne(0, Integer.class);
        return n == null ? 0 : n;
    }

    // -- execution, in autocommit ------------------------------------------------------------------------------

    private void execute(PciBuilderSession.Pass pass, Leaf leaf, int dim, List<Action> actions, Tally tally) {
        for (Action action : actions) {
            if (!pass.live() || Thread.currentThread().isInterrupted()) {
                return;
            }
            switch (action) {
                case SkipCountSuspect s -> log.warn("event=pci_count_zero_indexed leaf={} collection={} "
                    + "detail=\"a live, indexed collection counted zero rows: a counting failure, never acted on\"",
                    leafId(leaf), s.collection());
                case Drop d -> drop(pass, leaf, d, tally);
                case Build b -> build(pass, leaf, dim, b, tally);
            }
        }
    }

    private void drop(PciBuilderSession.Pass pass, Leaf leaf, Drop drop, Tally tally) {
        Index index = leaf.indexes().stream().filter(i -> i.name().equals(drop.name())).findFirst().orElse(null);
        if (index == null) {
            return;
        }
        DdlOutcome outcome = pass.drop(leaf, index);
        if (outcome == DdlOutcome.DONE) {
            tally.drops++;
            log.info("event=pci_index_dropped leaf={} index={} collection={} reason={}", leafId(leaf), drop.name(),
                drop.collection(), drop.reason().label());
            refreshSweep("drop");
        } else {
            log.info("event=pci_index_drop_not_done leaf={} index={} outcome={} detail=\"the next pass retries\"",
                leafId(leaf), drop.name(), outcome);
        }
    }

    private void build(PciBuilderSession.Pass pass, Leaf leaf, int dim, Build build, Tally tally) {
        LeafKey key = new LeafKey(leafId(leaf), build.collection());
        building.set(build.name());
        DdlOutcome outcome;
        try {
            outcome = pass.build(leaf, dim, build.collection());
        } finally {
            building.set(null);
        }
        switch (outcome) {
            case DONE -> {
                tally.builds++;
                failures.remove(key);
                log.info("event=pci_index_built leaf={} index={} collection={}", leafId(leaf), build.name(),
                    build.collection());
                refreshSweep("build");
            }
            case FAILED, REJECTED -> {
                tally.failedBuilds++;
                recordFailure(key, build.name());
            }
            // The pass ended before the statement went out (a migration, a privilege error): not this collection's fault.
            default -> log.info("event=pci_index_build_not_done leaf={} index={} outcome={}", leafId(leaf),
                build.name(), outcome);
        }
    }

    private void refreshSweep(String after) {
        if (!sweep.refresh()) {
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

    /** A collection that left the registry has nothing to retry. */
    private void forgetStaleBackoff(Leaf leaf, Set<String> listed) {
        String id = leafId(leaf);
        failures.keySet().removeIf(k -> k.leaf().equals(id) && !listed.contains(k.collection()));
    }
}
