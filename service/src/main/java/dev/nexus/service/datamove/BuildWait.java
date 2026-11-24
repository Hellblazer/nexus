// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.db.PciBuilders;
import org.jooq.DSLContext;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.Map;
import java.util.Objects;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.Function;
import java.util.stream.Collectors;

/**
 * RDR-229 P3.7 row 23 (nexus-p2m1x.27): the wait of a move that does not end index builds ({@link ChunkFamily#waitsForIndexBuilds},
 * the name-key drop). What {@link VerifyingFinisher} asks of it: whether a build is in flight (one detector,
 * {@link #read}), and, when one is, the not-now it reports ({@link #defer}): a {@link MoveFinisher.Result#blocked} that counts no failure,
 * pages no one and ends no build (Sam 2026-11-23, T2 {@code nexus/sam-ruling-rdr229-drop-move-waits-for-id-builds-2026-11-24},
 * "Fix: wait, don't end").
 *
 * <p><b>What it waits for.</b> A builder-named backend that appears in {@code pg_stat_progress_create_index}
 * ({@link PciBuilders#building}; linda 2026-11-24): a {@code CREATE INDEX} in flight, the PCI build, a leaf's unique index or the
 * id-unique index. A builder session that only holds a lock or counts leaves does not defer the drop. The wide count
 * ({@link PciBuilders#count}) stays what the termination paths use. The detector is the one method the deferral calls, so a change
 * of what counts as a build is a change of {@link #detector}.
 *
 * <p><b>How long, and the age.</b> A wait has no bound that ends a build, fails the move or pages (the ruling). It is visible: the
 * first deferral of an unbroken run is kept in memory ({@code since}, per label, lost with the process), every deferral reports its
 * age, and past {@link #OVERDUE_AFTER} the reason says {@code overdue}, names the running builders' pids, and the engine logs a
 * WARN instead of an INFO ({@link MoveReasons} turns that into the {@code reason_class} {@code waiting_for_builds_overdue}, which
 * {@code nx doctor} shows as a warning). The run ends when the finisher returns anything else for the move ({@link #clear}).
 *
 * <p>One log line per deferral, from here; the worker does not also log the not-now as {@code data_move_blocked}.
 */
final class BuildWait {

    private static final Logger log = LoggerFactory.getLogger(BuildWait.class);

    /** The start of the reason a deferral carries; {@link MoveReasons} recognises it. */
    static final String REASON_PREFIX = "waiting for in-flight index builds";

    /** A deferral that has gone on longer than this is overdue: visibility only, nothing ends or pages (linda 2026-11-24). */
    static final Duration OVERDUE_AFTER = Duration.ofHours(6);

    /** Which read made the deferral. */
    enum Path {
        /** The read before the completion transaction found a build. */
        BEFORE_LOCKS("seen before the drop asked for its locks", "precheck"),
        /** The drop's lock wait timed out (55P03) and the read after it found a build that began after the first. */
        LOCK_WAIT("the drop's lock wait timed out behind a build that began after the first read", "lock_wait");

        final String text;
        final String event;

        Path(String text, String event) {
            this.text = text;
            this.event = event;
        }
    }

    /** Test seam: the clock the age is read from. */
    Clock clock = Clock.systemUTC();

    /** The detector: the one method a deferral calls (test seam too: a test makes it unreadable, or names builds the database lacks). */
    Function<DSLContext, PciBuilders.Building> detector = ctx -> PciBuilders.building(ctx, log);

    private final Map<String, Instant> since = new ConcurrentHashMap<>();

    PciBuilders.Building read(DSLContext ctx) {
        return Objects.requireNonNull(detector.apply(ctx), "detector");
    }

    /** True when {@code result} is a deferral made here (the worker logs no second line for it). */
    static boolean isDeferral(MoveFinisher.Result result) {
        return result.outcome() == MoveFinisher.Outcome.BLOCKED && result.reason() != null
            && result.reason().startsWith(REASON_PREFIX);
    }

    /** The move's deferral run ends: it completed, failed, paused or was left, or the finisher did anything but defer. */
    void clear(String label) {
        since.remove(label);
    }

    /** How long the move's unbroken deferral has gone on, or null when it is not deferring. */
    Duration age(String label) {
        Instant first = since.get(label);
        return first == null ? null : Duration.between(first, clock.instant());
    }

    /** The deferral's result, and its one log line. {@code builds} has at least one running build. */
    MoveFinisher.Result defer(String label, PciBuilders.Building builds, Path path) {
        Instant now = clock.instant();
        Duration age = Duration.between(since.computeIfAbsent(label, k -> now), now);
        if (age.isNegative()) {
            age = Duration.ZERO;
        }
        boolean overdue = age.compareTo(OVERDUE_AFTER) > 0;
        String pids = builds.pids().stream().map(String::valueOf).collect(Collectors.joining(","));
        StringBuilder why = new StringBuilder(REASON_PREFIX).append(": running=").append(builds.count())
            .append("; deferred_minutes=").append(age.toMinutes());
        if (overdue) {
            why.append("; overdue; pids=").append(pids);
        }
        why.append("; ").append(path.text).append("; the completion tries again later and ends none");
        if (overdue) {
            log.warn("event=data_move_completion_overdue label=\"{}\" path={} builds={} pids={} deferred_minutes={}",
                MoveEvents.value(label), path.event, builds.count(), pids, age.toMinutes());
        } else {
            log.info("event=data_move_completion_deferred label=\"{}\" path={} builds={} deferred_minutes={}",
                MoveEvents.value(label), path.event, builds.count(), age.toMinutes());
        }
        return MoveFinisher.Result.blocked(why.toString());
    }

    /** What a failed read of the detector says, for the counted transient the finisher reports instead of waiting. */
    static String unreadable(String label) {
        return "the index builders of " + label + " could not be read, so the completion did not run: a build in flight cannot be "
            + "ruled out, and the drop does not end one (the engine log has event=pci_builders_building_failed)";
    }
}
