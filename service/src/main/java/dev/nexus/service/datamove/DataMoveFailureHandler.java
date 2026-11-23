// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.AdminDdlSession;
import org.jooq.DSLContext;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.Objects;
import java.util.OptionalInt;
import java.util.function.Function;

/**
 * nexus-f3yqd.2.15 (RDR-246, The move worker, the failure classes): the {@link FailureHandler} an engine boots with. It reads
 * the run's recorded chunk errors where the stage calls for it, classifies the failure ({@link DataMoveFailureClassifier}),
 * and writes the registry before it answers: a permanent failure sets {@code failed_at} and {@code failure} (the move is
 * never retried, by this process or a later boot); a transient one adds a retry to {@code transient_retries} and stamps
 * {@code last_transient_at} and {@code last_transient}, and the worker waits out {@link MoveRetryPolicy#backoff} from that
 * row before the next pass.
 *
 * <p>When the registry cannot be written (the admin connection is gone) the answer is {@code blocked:} with that said, never
 * {@code failed:}: a failure not recorded in the row would be retried by the next boot, so it is not reported as final.
 */
public final class DataMoveFailureHandler implements FailureHandler {

    private static final Logger log = LoggerFactory.getLogger(DataMoveFailureHandler.class);

    /** How long a raised 42501, 42883 or class 08 error waits for dml_utils' asynchronous recorder before it is read again. */
    static final Duration RECORDER_WAIT = Duration.ofMillis(750);

    private final AdminConnection admin;
    private final String applicationName;
    private final Clock clock;
    private final Duration recorderWait;
    /** Per move, the run of {@code on_complete} bounds this process has seen; in memory (the row keeps no stage). */
    private final MoveRetryPolicy.OnCompleteRuns runs = new MoveRetryPolicy.OnCompleteRuns();

    public DataMoveFailureHandler(AdminConnection admin, String applicationName, Clock clock) {
        this(admin, applicationName, clock, RECORDER_WAIT);
    }

    DataMoveFailureHandler(AdminConnection admin, String applicationName, Clock clock, Duration recorderWait) {
        this.admin = Objects.requireNonNull(admin, "admin");
        this.applicationName = Objects.requireNonNull(applicationName, "applicationName");
        this.clock = Objects.requireNonNull(clock, "clock");
        this.recorderWait = Objects.requireNonNull(recorderWait, "recorderWait");
    }

    @Override
    public Disposition onFailure(Failure unmasked) {
        Failure failure = unmasked.masked();
        DataMoveFailureClassifier.Verdict verdict;
        try {
            verdict = classify(failure);
        } catch (RuntimeException e) {
            // the recorded errors could not be read (a lost connection, say): not the move's fault, so retry
            verdict = DataMoveFailureClassifier.Verdict.transientFailure(failure.detail()
                + " (the run's recorded errors could not be read: " + FailureHandler.oneLine(e.toString()) + ")");
        }
        Instant now = clock.instant();
        try {
            return verdict.permanent() ? permanent(failure, verdict, now) : transientFailure(failure, verdict, now);
        } catch (RuntimeException e) {
            log.warn("event=data_move_failure_unrecorded label=\"{}\" cause=\"{}\"",
                MoveEvents.value(failure.move().label()), MoveEvents.value(FailureHandler.oneLine(e.toString())));
            return Disposition.blocked(verdict.reason() + " (" + (verdict.permanent() ? "failed_at" : "the retry count")
                + " could not be recorded: " + FailureHandler.oneLine(e.toString()) + ")", verdict.sqlstate(),
                Disposition.NOT_COUNTED);
        }
    }

    private DataMoveFailureClassifier.Verdict classify(Failure f) {
        List<DataMoveRuns.RunError> recorded = List.of();
        boolean readable = f.stage() == Stage.CALL || f.stage() == Stage.INCOMPLETE;
        if (readable && f.admin() != null && f.runLabel() != null) {
            recorded = DataMoveRuns.errorsAbove(f.admin(), f.runLabel(), f.errorMark());
            if (recorded.isEmpty() && DataMoveFailureClassifier.needsRecorderRecheck(f.stage(), f.raised())) {
                pause();
                recorded = DataMoveRuns.errorsAbove(f.admin(), f.runLabel(), f.errorMark());
            }
        }
        return DataMoveFailureClassifier.classify(f.stage(), f.raised(), recorded, f.detail());
    }

    private void pause() {
        try {
            Thread.sleep(recorderWait.toMillis());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    private Disposition permanent(Failure f, DataMoveFailureClassifier.Verdict verdict, Instant now) {
        String reason = verdict.reason();
        boolean marked = withAdmin(f, ctx -> MoveFailureRecords.markFailed(ctx, f.move().label(), now, reason));
        if (!marked) {
            // the row is no longer open (completed, failed or superseded meanwhile): report it as the worker saw it
            log.info("event=data_move_failure_not_recorded label=\"{}\" reason=\"the move is no longer open\"",
                MoveEvents.value(f.move().label()));
        }
        return Disposition.failed(reason, verdict.sqlstate());
    }

    private Disposition transientFailure(Failure f, DataMoveFailureClassifier.Verdict verdict, Instant now) {
        String reason = verdict.reason();
        OptionalInt retries = withAdmin(f, ctx -> MoveFailureRecords.recordTransient(ctx, f.move().label(), now, reason));
        if (retries.isEmpty()) {
            return Disposition.blocked(reason, verdict.sqlstate(), Disposition.NOT_COUNTED);
        }
        int count = retries.getAsInt();
        var run = runs.record(f.move().label(), count, MoveRetryPolicy.isOnCompleteBound(f.stage(), verdict.sqlstate()));
        boolean indexBuilds = run.run() >= MoveRetryPolicy.NEEDS_OPERATOR_AT_ON_COMPLETE && ChunkFamily.touches(f.move());
        if (MoveRetryPolicy.entersNeedsOperator(count - 1, run.before(), count, run.run())) {
            // the count is bumped by one UPDATE and the run by one thread per move, so exactly one failure enters the state:
            // the page fires once per transition
            MoveEvents.needsOperator(f.move().label(), MoveRetryPolicy.step(f.stage()), verdict.sqlstate(),
                MoveRetryPolicy.boundMs(f.stage(), f.move(), verdict.sqlstate()), count, indexBuilds,
                indexBuilds ? "each retry ends in-flight index builds (PCI, leaf, id-unique)"
                    : "the move has failed transiently " + count + " times in a row and keeps retrying");
        }
        return Disposition.blocked(MoveRetryPolicy.blockedReason(count, run.run(), indexBuilds, now, reason),
            verdict.sqlstate(), count);
    }

    /** Runs {@code write} on the pass's admin connection, or on a short one of its own when the pass has none. */
    private <T> T withAdmin(Failure f, Function<DSLContext, T> write) {
        if (f.admin() != null) {
            return write.apply(f.admin());
        }
        try (AdminDdlSession session = AdminDdlSession.open(admin,
                new AdminDdlSession.Settings(applicationName, 0L, "data move failure", f.drivingSchema(), f.drivingTable(),
                    "none"), false)) {
            return write.apply(session.ctx());
        }
    }
}
