// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import dev.nexus.service.db.ManifestFill;
import dev.nexus.service.db.ManifestFillableCount;

import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.function.LongSupplier;

/**
 * nexus-f3yqd, route R2: the {@link EngineJobs} kind behind {@code POST /v1/catalog/manifest/fill}, the engine-side
 * fill of the manifest section labels and spans ({@link ManifestFill} has the rule). This holds what is the kind's own:
 * its bounds, the progress a job keeps, and its failure outcome; the work and the routes are {@link
 * CatalogManifestFillRoutes}.
 *
 * <p><b>Scope and fingerprint.</b> The scope is the tenant alone: one fill job runs per tenant, whatever its kind
 * (Sam, 2026-11-10: one job per tenant, so a managed tenant cannot hold both of the registry's slots for hours). The
 * fingerprint is {@code (kind, dry_run)}: a second start with the same fingerprint while one runs is handed the running
 * job ({@code created: false}, whichever box sent it, so a later box joins the running job), and a start with another
 * kind or the other {@code dry_run} is a 409 naming it. Another tenant's job is never joined, polled or evicted by
 * this one.
 *
 * <p><b>Memory only, by design.</b> The fill is fill-only: a filled row leaves the predicate, so a restart, a cancel
 * or a crash is resumed by starting again, which re-reads from the beginning over what is left, or from the cursor a
 * poll showed when the start passes it as {@code after}. Nothing here is state a restart needs; a job's cursor and
 * counts are progress for a client to show or hand back.
 *
 * <p><b>Bounds.</b> Two jobs run at once (one connection each, one batch at a time; so of two tenants) and eight wait;
 * a start past that is a 503. A job stops at the next batch boundary after four hours of running, when the tenant
 * reset evicted it, or when its thread is interrupted (the engine's shutdown), and ends failed with what it had
 * committed ({@code job_stopped}); the next job resumes. Between batches it may pause ({@link #PAUSE_PROPERTY}).
 */
public final class ManifestFillJobs {

    private ManifestFillJobs() { }

    /** Jobs running at once, jobs waiting, jobs held, how long a finished job stays readable, the longest a job runs. */
    public static final EngineJobs.Bounds BOUNDS =
        new EngineJobs.Bounds(2, 8, 64, Duration.ofMinutes(30), Duration.ofHours(4));

    /** What stopping the engine does to a running fill: nothing is lost; the committed batches stay written. */
    public static final String ON_STOP =
        "committed batches stay written; a new start re-reads from the beginning, or from the cursor passed as after";

    /**
     * System property, in milliseconds, of the pause between one batch and the next (read on every batch, default 0):
     * an operator's way to slow a fill on a managed tenant so it leaves the database room for its other work.
     */
    public static final String PAUSE_PROPERTY = "nexus.manifest.fill.pause_ms";

    /**
     * What a job has done so far, replaced whole after each batch so a poll reads one consistent view.
     *
     * @param picked   fillable rows the batches have found (a dry run's count; the rows a real run set out to fill)
     * @param filled   rows the UPDATEs wrote (0 on a dry run)
     * @param scanned  manifest rows the batches' windows have covered (the rows the kind's prefilter admitted)
     * @param total    the fillable rows when the job started, or null when that count was capped or timed out
     * @param edge     rows that pass every guard but an edge the SQL form cannot decide, which no fill ever writes
     *                 (see {@link ManifestFillableCount}), or null when that count was capped or timed out
     * @param admitted the manifest rows the prefilter admitted past the start cursor when the job started, the
     *                 denominator of {@code scanned}; null when that count timed out
     * @param cursor   the last manifest key the batches have scanned (the start cursor before the first), or null
     */
    public record Snapshot(long picked, long filled, long scanned, Long total, Long edge, Long admitted,
                           ManifestFill.Cursor cursor) {
        static final Snapshot START = new Snapshot(0, 0, 0, null, null, null, null);
    }

    /** What a fill job keeps on its {@link EngineJobs.Job}: what it was asked, and its latest {@link Snapshot}. */
    public static final class Progress {
        private final ManifestFillableCount.Kind kind;
        private final boolean dryRun;
        private final int batchRows;
        private final ManifestFill.Cursor after;
        private volatile Snapshot snapshot = Snapshot.START;

        Progress(ManifestFillableCount.Kind kind, boolean dryRun, int batchRows, ManifestFill.Cursor after) {
            this.kind = kind;
            this.dryRun = dryRun;
            this.batchRows = batchRows;
            this.after = after;
        }

        /** The cursor the walk starts after, or null for the start of the manifest. */
        public ManifestFill.Cursor after() {
            return after;
        }

        public ManifestFillableCount.Kind kind() {
            return kind;
        }

        public boolean dryRun() {
            return dryRun;
        }

        public int batchRows() {
            return batchRows;
        }

        public Snapshot snapshot() {
            return snapshot;
        }

        void snapshot(Snapshot next) {
            this.snapshot = next;
        }
    }

    /** The scope of a start within its tenant: none, so one fill runs per tenant whatever its kind. */
    static final String SCOPE = null;

    /** The fingerprint of a start: the kind and whether it writes. A start with the same fingerprint joins the running job. */
    static String fingerprint(ManifestFillableCount.Kind kind, boolean dryRun) {
        return kind.wire() + "|dry_run=" + dryRun;
    }

    /** The registry the fill jobs run on: {@link #BOUNDS}, and the failure outcome of {@link #failure}. */
    public static EngineJobs<Progress> registry(LongSupplier clockMs) {
        return new EngineJobs<>("catalog-manifest-fill-job-", clockMs, BOUNDS, ManifestFillJobs::failure);
    }

    /**
     * The outcome of a job whose work threw: the class of what it threw (never its message, which can carry another
     * tenant's data or a statement), the batches' committed progress, and {@code transient_db_error} (503) for a
     * deadlock, serialization failure or lock wait that ran out, {@code batch_timeout} (500) for a batch past its
     * statement timeout, {@code job_failed} (500) otherwise. In each the committed batches stay written and a new start
     * re-reads from the beginning, or from the cursor passed.
     */
    static EngineJobs.Outcome failure(EngineJobs.Job<Progress> job, Throwable thrown) {
        String state = sqlState(thrown);
        int status = 500;
        String reason = "job_failed";
        if ("40P01".equals(state) || "40001".equals(state) || "55P03".equals(state)) {
            status = 503;
            reason = "transient_db_error";
        } else if ("57014".equals(state)) {
            reason = "batch_timeout";
        }
        return failed(status, reason, "the job failed: " + thrown.getClass().getSimpleName()
            + " (the batches it committed stay written; a new start re-reads from the beginning, or from the cursor passed as after)");
    }

    static EngineJobs.Outcome failed(int status, String reason, String error) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("error", error);
        body.put("reason", reason);
        return new EngineJobs.Outcome(status, body);
    }

    private static String sqlState(Throwable e) {
        int depth = 0;
        for (Throwable t = e; t != null && depth < 32; t = t.getCause(), depth++) {
            if (t instanceof java.sql.SQLException se && se.getSQLState() != null) return se.getSQLState();
        }
        return null;
    }
}
