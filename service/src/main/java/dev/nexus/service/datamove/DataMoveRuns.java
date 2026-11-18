// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.time.OffsetDateTime;
import java.util.List;

/**
 * Reads and controls a move's dml_utils runs ({@code dml_utils_data.migration_run} and {@code migration_error}),
 * on the admin connection. Reads need {@code dml_utils_caller}'s SELECT, which the migrating role holds with INHERIT
 * (RDR-246, Install sequence step 1); the two controls are the library's own functions.
 *
 * <p>A move's runs are the active ({@code archived_at IS NULL}) rows whose label is the move's label or begins with
 * {@code <label>/}, selected with {@code starts_with}, never {@code LIKE}, since {@code _} and {@code %} are legal in a
 * label. dml_utils keeps one active run per label.
 */
public final class DataMoveRuns {

    private DataMoveRuns() { /* static */ }

    private static final Table<?> RUN = DSL.table(DSL.name("dml_utils_data", "migration_run"));
    private static final Table<?> ERROR = DSL.table(DSL.name("dml_utils_data", "migration_error"));

    private static final Field<Long> RUN_ID = DSL.field(DSL.name("run_id"), Long.class);
    private static final Field<String> LABEL = DSL.field(DSL.name("label"), String.class);
    private static final Field<OffsetDateTime> ARCHIVED_AT =
        DSL.field(DSL.name("archived_at"), SQLDataType.TIMESTAMPWITHTIMEZONE);
    private static final Field<OffsetDateTime> COMPLETED_AT =
        DSL.field(DSL.name("completed_at"), SQLDataType.TIMESTAMPWITHTIMEZONE);
    private static final Field<OffsetDateTime> CANCELLED_AT =
        DSL.field(DSL.name("cancelled_at"), SQLDataType.TIMESTAMPWITHTIMEZONE);
    private static final Field<Integer> SLEEP_MS = DSL.field(DSL.name("sleep_ms"), Integer.class);
    private static final Field<Boolean> PARTITION_WISE = DSL.field(DSL.name("partition_wise"), Boolean.class);
    private static final Field<Long> TOTAL = DSL.field(DSL.name("total_chunks"), Long.class);
    private static final Field<Long> DONE = DSL.field(DSL.name("total_chunks_completed"), Long.class);

    /** What a move's active runs add up to. {@code total} grows as each run is created, so the fraction can fall. */
    public record Progress(long done, long total) { }

    /** The active run of one run label. */
    public record ActiveRun(long runId, int sleepMs, OffsetDateTime completedAt, OffsetDateTime cancelledAt,
                            boolean partitionWise, long done, long total) {
        public boolean finished() {
            return completedAt != null || cancelledAt != null;
        }
    }

    /** One chunk error a run recorded. */
    public record RunError(long errorId, long boundaryNo, String sqlstate, String message) { }

    /** The active runs that belong to {@code moveLabel}: the label itself, or one that begins with {@code moveLabel/}. */
    static Condition belongsTo(String moveLabel) {
        return LABEL.eq(moveLabel)
            .or(DSL.condition(DSL.function("starts_with", SQLDataType.BOOLEAN, LABEL, DSL.val(moveLabel + "/"))));
    }

    /** The active runs of the move, summed. A move with no run has {@code done} 0 and {@code total} 0. */
    public static Progress progress(DSLContext admin, String moveLabel) {
        var row = admin.select(DSL.coalesce(DSL.sum(DONE), DSL.val(java.math.BigDecimal.ZERO)),
                DSL.coalesce(DSL.sum(TOTAL), DSL.val(java.math.BigDecimal.ZERO)))
            .from(RUN).where(ARCHIVED_AT.isNull()).and(belongsTo(moveLabel)).fetchOne();
        return new Progress(row.value1().longValue(), row.value2().longValue());
    }

    /** The labels of the move's active runs, in label order. */
    static List<String> activeRunLabels(DSLContext admin, String moveLabel) {
        return admin.select(LABEL).from(RUN).where(ARCHIVED_AT.isNull()).and(belongsTo(moveLabel)).orderBy(LABEL)
            .fetch(LABEL);
    }

    /** The active run of {@code runLabel}, or null when it has none yet (the first CALL creates it). */
    static ActiveRun activeRun(DSLContext admin, String runLabel) {
        var r = admin.select(RUN_ID, SLEEP_MS, COMPLETED_AT, CANCELLED_AT, PARTITION_WISE, DONE, TOTAL)
            .from(RUN).where(LABEL.eq(runLabel)).and(ARCHIVED_AT.isNull()).fetchOne();
        return r == null ? null
            : new ActiveRun(r.value1(), r.value2(), r.value3(), r.value4(), r.value5(), r.value6(), r.value7());
    }

    /**
     * The run's highest {@code error_id}, 0 when the run does not exist yet or has no error rows. Held before each CALL: a
     * row above it is this pass's recorded chunk error. dml_utils never deletes error rows on resume, so an older row says
     * nothing about this pass, and an id mark, unlike a clock, needs no agreement between the engine and the database.
     */
    static long errorMark(DSLContext admin, String runLabel) {
        var e = ERROR.as("e");
        var r = RUN.as("r");
        Long mark = admin.select(DSL.coalesce(DSL.max(DSL.field(DSL.name("e", "error_id"), Long.class)), 0L))
            .from(e).join(r).on(DSL.field(DSL.name("r", "run_id"), Long.class)
                .eq(DSL.field(DSL.name("e", "run_id"), Long.class)))
            .where(DSL.field(DSL.name("r", "label"), String.class).eq(runLabel))
            .and(DSL.field(DSL.name("r", "archived_at"), SQLDataType.TIMESTAMPWITHTIMEZONE).isNull())
            .fetchOne(0, Long.class);
        return mark == null ? 0L : mark;
    }

    /** The chunk errors the run recorded above {@code mark}, oldest first. */
    static List<RunError> errorsAbove(DSLContext admin, String runLabel, long mark) {
        var e = ERROR.as("e");
        var r = RUN.as("r");
        Field<Long> errorId = DSL.field(DSL.name("e", "error_id"), Long.class);
        return admin.select(errorId, DSL.field(DSL.name("e", "boundary_no"), Long.class),
                DSL.field(DSL.name("e", "sqlstate"), String.class), DSL.field(DSL.name("e", "message"), String.class))
            .from(e).join(r).on(DSL.field(DSL.name("r", "run_id"), Long.class)
                .eq(DSL.field(DSL.name("e", "run_id"), Long.class)))
            .where(DSL.field(DSL.name("r", "label"), String.class).eq(runLabel))
            .and(DSL.field(DSL.name("r", "archived_at"), SQLDataType.TIMESTAMPWITHTIMEZONE).isNull())
            .and(errorId.gt(mark))
            .orderBy(errorId)
            .fetch(x -> new RunError(x.value1(), x.value2(), x.value3(), x.value4()));
    }

    /** {@code dml_utils.set_migration_run_sleep_ms}: the pause after each chunk of an unfinished run, for its next CALL. */
    static void setSleepMs(DSLContext admin, String runLabel, int sleepMs) {
        admin.select(DSL.function(DSL.name("dml_utils", "set_migration_run_sleep_ms"), Object.class,
            DSL.val(runLabel), DSL.val(sleepMs))).fetch();
    }

    /** {@code dml_utils.archive_migration_run}: ends the active run of the label (cancelling it first) and frees the label. */
    static void archive(DSLContext admin, String runLabel) {
        admin.select(DSL.function(DSL.name("dml_utils", "archive_migration_run"), SQLDataType.BIGINT,
            DSL.val(runLabel))).fetch();
    }
}
