// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.BootWorkRegistry.Step;
import dev.nexus.service.vectors.PciBuilderSession.BuildProgress;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.SQLException;
import java.time.Duration;
import java.util.Objects;
import java.util.Optional;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/**
 * What the reconciler tells {@code GET /v1/status}'s {@code boot_work} about its own pass (nexus-f3yqd.3): the
 * {@code pci-reconcile} entry, "building search indexes", with {@code done} builds finished of {@code total} planned,
 * and, while a build runs, a {@code step} sampled from {@code pg_stat_progress_create_index}.
 *
 * <p><b>Total.</b> A leaf's plan is made when the pass reaches the leaf, so the number of builds the pass will run is
 * not known until the last leaf that can plan one has planned. {@code total} is therefore null until then ("not known
 * yet"), and the real count from then on: a bar built on it can never go backward or read 100% early. A single-leaf
 * install gets its total at once. The entry is registered by the first plan that holds a build, and a pass that plans
 * none registers nothing. {@code done} counts builds that have ended, whatever their outcome: a failed build is no
 * longer pending (the next pass retries it), so {@code done} can reach {@code total} with some builds failed. A planned
 * build the executor does not run counts the same way ({@link #buildSkipped}): the planner rebuilds a failed index in
 * the plan that drops it, and the executor skips that build when the drop did not happen, so {@code total} (counted from
 * the plan) is still reached and never moves.
 *
 * <p><b>Step.</b> The reconciler's thread is inside {@code CREATE INDEX CONCURRENTLY} for the whole build, so each
 * build gets a sampler thread of its own that reads the builder backend's progress every {@code sampleEvery} on a short
 * connection ({@link PciBuilderSession#sampleBuildProgress}). A failed read leaves the last value in place and is
 * logged once per build ({@code event=boot_work_sample_failed}); the next tick tries again. A build with no row yet
 * (no statement running) leaves the step as it was. {@code done} and {@code total} are blocks, or tuples when the
 * phase counts no blocks, or null when it counts neither (a phase that waits).
 *
 * <p><b>Names.</b> {@code GET /v1/status} is unauthenticated, so the step names the build phase only, never a
 * collection, tenant or index. There is no switch that changes that.
 *
 * <p>Passes never overlap on one reconciler, so the per-pass counters need no more than a lock for the sampler and the
 * route. Nothing here throws into the pass: a registry that has lost the entry makes every update a no-op.
 */
final class PciBootWork {

    private static final Logger log = LoggerFactory.getLogger(PciBootWork.class);

    static final String ID = "pci-reconcile";
    static final String WHAT = "building search indexes";
    static final String UNIT = "indexes";
    /**
     * What stopping the engine does to the build in flight, as it is today (nexus-f3yqd): the shutdown hook leaves the
     * builder's backend alone, so an engine-only stop leaves the {@code CREATE INDEX CONCURRENTLY} running in Postgres;
     * its session keeps the builder lock, the next engine reports standby until the build ends, and then finds the
     * valid index and adopts it. Only stopping Postgres itself ends the build: the half-built index is left INVALID, a
     * later pass drops it and rebuilds it from the start in the same pass. Pinned by
     * {@code PciEngineRestartKeepsBuildIntegrationTest} (the engine restart that keeps the build). A change that makes
     * an engine stop end the build again, or that lets a database stop keep it, turns that test red, and this text must
     * change with it.
     */
    static final String ON_STOP = "the build keeps running in the database and is adopted after a restart; "
        + "stopping the database as well loses it and it is rebuilt from the start";
    static final String STEP_UNIT_BLOCKS = "blocks";
    static final String STEP_UNIT_TUPLES = "tuples";

    /** How often a running build's progress is read. */
    static final Duration SAMPLE_EVERY = Duration.ofSeconds(3);

    /** Reads the builder backend's progress; the production one is {@link PciBuilderSession#sampleBuildProgress}. */
    @FunctionalInterface
    interface Sampler {
        Optional<BuildProgress> sample() throws SQLException;
    }

    private final BootWorkRegistry registry;
    private final Sampler sampler;
    private final Duration sampleEvery;

    private final Object lock = new Object();
    private int planned;     // guarded by lock
    private int finished;    // guarded by lock
    private boolean registered;   // guarded by lock
    private boolean totalKnown;   // guarded by lock: the last leaf that can plan a build has planned

    PciBootWork(BootWorkRegistry registry, Sampler sampler, Duration sampleEvery) {
        this.registry = Objects.requireNonNull(registry, "registry");
        this.sampler = Objects.requireNonNull(sampler, "sampler");
        this.sampleEvery = Objects.requireNonNull(sampleEvery, "sampleEvery");
        if (sampleEvery.isZero() || sampleEvery.isNegative()) {
            throw new IllegalArgumentException("sampleEvery must be positive, got " + sampleEvery);
        }
    }

    /**
     * A leaf's plan holds {@code builds} builds: register the entry on the first, grow the count on the rest.
     *
     * @param moreLeaves whether a later leaf of this pass can still plan a build. While it can, {@code total} is sent
     *                   as null; the call for the last such leaf (with {@code false}, even when it plans no build)
     *                   fixes the total.
     */
    void planned(int builds, boolean moreLeaves) {
        synchronized (lock) {
            planned += Math.max(builds, 0);
            if (!moreLeaves) {
                totalKnown = true;
            }
            if (builds <= 0 && (!registered || moreLeaves)) {
                return;   // nothing to register, or nothing new to say
            }
            if (!registered) {
                registered = true;
                registry.register(ID, WHAT, (long) finished, totalOrNull(), UNIT, true, ON_STOP);
            } else {
                registry.progress(ID, (long) finished, totalOrNull());
            }
        }
    }

    private Long totalOrNull() {   // caller holds lock
        return totalKnown ? (long) planned : null;
    }

    /** The pass is over, however it ended: the entry leaves the list and the counters start again. */
    void endPass() {
        synchronized (lock) {
            planned = 0;
            finished = 0;
            totalKnown = false;
            if (registered) {
                registered = false;
                registry.finish(ID);
            }
        }
    }

    /**
     * A build the plan held will not run in this pass (its failed index's drop did not happen; the next pass drops it
     * again and builds). It is no longer pending, so it counts as ended: {@code done} still reaches {@code total}.
     */
    void buildSkipped() {
        buildNoLongerPending();
    }

    private void buildNoLongerPending() {
        synchronized (lock) {
            finished++;
            if (registered) {
                registry.progress(ID, (long) finished, totalOrNull());
            }
        }
    }

    /**
     * Start sampling a build that is about to run. Close the returned watch when the build ends, in a {@code finally}:
     * it stops the sampler, clears the step and counts the build as finished.
     */
    Watch watchBuild() {
        return new Watch();
    }

    final class Watch implements AutoCloseable {
        private final ScheduledExecutorService scheduler;
        private final Object guard = new Object();
        private boolean closed;       // guarded by guard
        private boolean failureLogged;   // sampler thread only

        private Watch() {
            scheduler = Executors.newSingleThreadScheduledExecutor(r -> {
                Thread t = new Thread(r, "nexus-pci-boot-work-sampler");
                t.setDaemon(true);
                return t;
            });
            // The first read is one period in: a build that ends sooner is never sampled, and a read racing the
            // build's own start finds no backend row yet.
            scheduler.scheduleWithFixedDelay(this::sampleOnce, sampleEvery.toMillis(), sampleEvery.toMillis(),
                TimeUnit.MILLISECONDS);
        }

        private void sampleOnce() {
            try {
                Optional<BuildProgress> read = sampler.sample();
                if (read.isEmpty()) {
                    return;
                }
                Step step = toStep(read.get());
                synchronized (guard) {
                    if (!closed) {
                        registry.step(ID, step);
                    }
                }
            } catch (SQLException | RuntimeException e) {
                synchronized (guard) {
                    if (closed) {
                        return;   // the build ended and close() interrupted this read: not a failure
                    }
                }
                if (!failureLogged) {
                    failureLogged = true;
                    log.warn("event=boot_work_sample_failed id={} cause=\"{}\" detail=\"the step keeps its last value; "
                        + "the next sample tries again\"", ID, e.getMessage());
                } else {
                    log.debug("event=boot_work_sample_failed id={} cause=\"{}\"", ID, e.getMessage());
                }
            }
        }

        private Step toStep(BuildProgress p) {
            String phase = p.phase() == null ? "" : p.phase();
            String what = phase;
            if (p.blocksTotal() > 0) {
                return new Step(what, p.blocksDone(), p.blocksTotal(), STEP_UNIT_BLOCKS);
            }
            if (p.tuplesTotal() > 0) {
                return new Step(what, p.tuplesDone(), p.tuplesTotal(), STEP_UNIT_TUPLES);
            }
            return new Step(what, null, null, STEP_UNIT_BLOCKS);
        }

        @Override
        public void close() {
            scheduler.shutdownNow();
            synchronized (guard) {
                closed = true;
                registry.step(ID, null);
            }
            buildNoLongerPending();
        }
    }
}
