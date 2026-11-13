// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import dev.nexus.service.db.AdminDdlSession;
import dev.nexus.service.db.ChashConstraintValidator;
import dev.nexus.service.db.ChashConstraintValidator.Step;
import dev.nexus.service.db.LegacyChunkSurvey;

import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.LongSupplier;

/**
 * nexus-f3yqd, engine 0.1.190: the {@link EngineJobs} kind behind {@code POST /v1/admin/validate-constraints}, the
 * {@code VALIDATE CONSTRAINT} of the chash width CHECKs ({@link ChashConstraintValidator} has the rule and the work). This
 * holds what is the kind's own: its bounds, the progress a job keeps and its failure outcome; the routes are {@link
 * ValidateConstraintsHandler}.
 *
 * <p><b>Scope.</b> Engine-wide, not per tenant: the constraints are table-wide, so one job runs at a time on the whole
 * engine. The job's tenant is the fixed {@link #JOB_TENANT}, whoever sent the start (the root token's own tenant is not part
 * of the scope), so a second start joins the running one ({@code created: false}) whichever caller sent it, and a start with
 * the other {@code dry_run} while one runs is a 409 naming it. No tenant's reset reaches it (its tenant is not a tenant), and
 * its status names tables, never a tenant's rows.
 *
 * <p><b>Memory only, by design.</b> What a restart, a cancel or a crash loses is the progress to show; no step is lost,
 * because a constraint that was validated stays validated and a rerun skips it ({@link #ON_STOP}).
 *
 * <p><b>Bounds.</b> One job runs and none waits (a second start is handed the first); it stops at the next step boundary after
 * twelve hours or when its thread is interrupted (the engine's shutdown) and ends failed with {@code job_stopped}, what it did
 * kept. A {@code DELETE} cancels it mid-statement and it ends failed with {@link #CANCELLED}. The lists a status carries ({@code validated}, {@code refused}, {@code deferred}) hold at most {@link #LIST_CAP}
 * entries each, with the full counts beside them, so a store with thousands of partitions does not make every poll large.
 */
public final class ValidateConstraintsJobs {

    private static final org.slf4j.Logger log = org.slf4j.LoggerFactory.getLogger(ValidateConstraintsJobs.class);

    private ValidateConstraintsJobs() { }

    /** The tenant every job carries: the job is the engine's, so it is nobody's tenant. */
    public static final String JOB_TENANT = "(engine)";

    /** One running, none waiting, a few held, a finished job readable for half an hour, twelve hours at most. */
    public static final EngineJobs.Bounds BOUNDS = new EngineJobs.Bounds(1, 1, 16, Duration.ofMinutes(30), Duration.ofHours(12));

    /** What stopping the engine does to a running job: nothing is undone. */
    public static final String ON_STOP =
        "a constraint already validated stays validated; a rerun resumes at the next one";

    /** The stop reason a {@code DELETE} gives a running job, and the failed outcome's {@code reason}. */
    public static final String CANCELLED = "cancelled";

    /** The most entries of each list a status carries. */
    public static final int LIST_CAP = 200;

    /** The fingerprint of a start: whether it writes. */
    static String fingerprint(boolean dryRun) {
        return "validate-constraints|dry_run=" + dryRun;
    }

    /** What a job keeps on its {@link EngineJobs.Job}: what it was asked and what it has done. Written by its worker, read by a poll. */
    public static final class Progress {
        private final boolean dryRun;
        private int total;
        private int done;
        private int alreadyValid;
        private int validatedCount;
        private int wouldValidateCount;
        private int refusedCount;
        private int deferredCount;
        private String currentTable;
        private Long currentBytes;
        private final List<String> validated = new ArrayList<>();
        private final List<String> wouldValidate = new ArrayList<>();
        private final List<Step> refused = new ArrayList<>();
        private final List<Step> deferred = new ArrayList<>();
        private Boolean allValidated;

        Progress(boolean dryRun) {
            this.dryRun = dryRun;
        }

        public boolean dryRun() {
            return dryRun;
        }

        synchronized void planned(int totalTables) {
            this.total = totalTables;
        }

        synchronized void current(String table, Long bytes) {
            this.currentTable = table;
            this.currentBytes = bytes;
        }

        synchronized void stepped(Step step) {
            done++;
            currentTable = null;
            currentBytes = null;
            switch (step.outcome()) {
                case VALIDATED -> {
                    validatedCount++;
                    if (validated.size() < LIST_CAP) validated.add(step.table());
                }
                case WOULD_VALIDATE -> {
                    wouldValidateCount++;
                    if (wouldValidate.size() < LIST_CAP) wouldValidate.add(step.table());
                }
                case ALREADY_VALID -> alreadyValid++;
                case REFUSED -> {
                    refusedCount++;
                    if (refused.size() < LIST_CAP) refused.add(step);
                }
                case DEFERRED -> {
                    deferredCount++;
                    if (deferred.size() < LIST_CAP) deferred.add(step);
                }
            }
        }

        /**
         * What a tenant reset leaves of {@code tenant} in a refused step. A step that names the tenant in its per-tenant counts
         * loses that entry, its count leaves {@code badRows}, and its example chashes go (they may be that tenant's). A step
         * whose rows came from the admin pre-check names no tenant (its count and examples may be any tenant's, and cannot be
         * told apart), so its example chashes go on every reset. Returns how many steps changed; a second call changes none.
         */
        synchronized int forgetTenant(String tenant) {
            int changed = 0;
            for (int i = 0; i < refused.size(); i++) {
                Step s = refused.get(i);
                boolean names = s.tenantRows().containsKey(tenant);
                boolean unattributed = s.tenantRows().isEmpty() && !s.exampleChashes().isEmpty();
                // the per-reason account, the listed chunks and the documents: a tenant counted in it need not be in tenantRows
                // (that comes from a different read), and a leaf's counts are any tenant's, so either drops the account whole
                var detail = s.detail();
                boolean detailNames = detail != null && (detail.unattributed() || detail.tenants().contains(tenant));
                if (!names && !unattributed && !detailNames) continue;
                Map<String, Long> rest = new LinkedHashMap<>(s.tenantRows());
                Long removed = rest.remove(tenant);
                Long bad = s.badRows();
                if (removed != null && bad != null) bad = bad - removed > 0 ? bad - removed : null;
                // the per-reason account and the listed chunks (with their owners) may be that tenant's and cannot be told apart
                refused.set(i, new Step(s.table(), s.constraint(), s.outcome(), s.reason(), bad, s.badRowsCapped(), List.of(),
                    s.precheck(), Map.copyOf(rest), null));
                changed++;
            }
            return changed;
        }

        synchronized void finished(Boolean allValidated) {
            this.allValidated = allValidated;
        }

        /** One consistent view of the progress, as the fields a status carries. */
        synchronized Map<String, Object> view() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("dry_run", dryRun);
            m.put("tables_done", done);
            m.put("tables_total", total);
            if (currentTable == null) {
                m.put("current", null);
            } else {
                Map<String, Object> c = new LinkedHashMap<>();
                c.put("table", currentTable);
                c.put("bytes", currentBytes);
                m.put("current", c);
            }
            m.put("validated", List.copyOf(validated));
            m.put("validated_count", validatedCount);
            m.put("already_valid_count", alreadyValid);
            if (dryRun) {
                m.put("would_validate", List.copyOf(wouldValidate));
                m.put("would_validate_count", wouldValidateCount);
            }
            m.put("refused", refused.stream().map(Progress::refusal).toList());
            m.put("refused_count", refusedCount);
            m.put("deferred", deferred.stream().map(Progress::deferral).toList());
            m.put("deferred_count", deferredCount);
            m.put("all_validated", allValidated);
            return m;
        }

        /** A refusal's entry: the table, its constraint, why, how many bad rows (null when no count was taken) and a few of them in hex. */
        private static Map<String, Object> refusal(Step s) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("table", s.table());
            m.put("constraint", s.constraint());
            m.put("reason", s.reason());
            m.put("bad_rows", s.badRows());
            m.put("bad_rows_capped", s.badRowsCapped());
            m.put("example_chashes", s.exampleChashes());
            m.put("precheck", s.precheck());
            // non-32-byte rows, in the manifest or in a chunks leaf: what each bad row is (counts per reason, a floor when capped) and,
            // for a chunks table, the chunks themselves with their owning documents. Nothing here changes a row.
            if ("bad_rows".equals(s.reason()) && ("nexus.catalog_document_chunks".equals(s.table()) || s.table().startsWith("nexus.chunks"))) {
                ChashConstraintValidator.Detail d = s.detail();
                if (d == null) {
                    // no account of the rows (the session could not read them all, or a read failed or was dropped by a reset)
                    m.put("reasons_unavailable", true);
                } else if (!d.reasonCounts().isEmpty()) {
                    m.put("reasons", d.reasonCounts());
                }
                if (d != null) m.put("reasons_capped", d.countsCapped());
                if (d != null && !d.documents().isEmpty()) m.put("documents", LegacyChunkSurvey.docsJson(d.documents()));
                if (d != null && !d.chunks().isEmpty()) {
                    m.put("chunks", d.chunks().stream().map(b -> {
                        Map<String, Object> e = new LinkedHashMap<>();
                        e.put("tenant", b.tenant());
                        e.put("collection", b.collection());
                        e.put("embedding_model", b.model());
                        e.put("chash_hex", b.chashHex());
                        e.put("reason", b.reason());
                        e.put("owners", LegacyChunkSurvey.ownersJson(b.owners()));
                        e.put("owners_capped", b.owners() != null && b.owners().capped());
                        return e;
                    }).toList());
                    m.put("chunks_capped", d.chunksCapped());
                }
            }
            // a table whose rows the admin session cannot read, refused by its VALIDATE: the bad rows by known tenant
            m.put("tenants", s.tenantRows().entrySet().stream().map(e -> {
                Map<String, Object> t = new LinkedHashMap<>();
                t.put("tenant", e.getKey());
                t.put("bad_rows", e.getValue());
                return t;
            }).toList());
            return m;
        }

        private static Map<String, Object> deferral(Step s) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("table", s.table());
            m.put("constraint", s.constraint());
            m.put("reason", s.reason());
            m.put("precheck", s.precheck());
            return m;
        }
    }

    /**
     * The tenant reset route's evictor for these jobs: scrubs {@code tenant} out of every held job's refused steps (the job
     * itself is the engine's and stays). Returns how many steps changed.
     */
    public static int forgetTenant(EngineJobs<Progress> jobs, String tenant) {
        int[] changed = {0};
        jobs.evictWhere(j -> {
            if (j.data() != null) changed[0] += j.data().forgetTenant(tenant);
            return false;   // scrubs, never evicts
        });
        return changed[0];
    }

    /** The registry the validate jobs run on: {@link #BOUNDS}, and the failure outcome of {@link #failure}. */
    public static EngineJobs<Progress> registry(LongSupplier clockMs) {
        return new EngineJobs<>("chash-validate-job-", clockMs, BOUNDS, ValidateConstraintsJobs::failure);
    }

    /**
     * The outcome of a job whose work threw: a {@link AdminDdlSession.Refused} keeps its own status and reason (another run
     * holds the lock, a schema migration is running, the admin connection failed or may not alter the tables; the message
     * names no tenant and no statement), and anything else is a 500 {@code job_failed} naming the class of what it threw,
     * never its message. In each the constraints already validated stay validated and a rerun resumes.
     */
    static EngineJobs.Outcome failure(EngineJobs.Job<Progress> job, Throwable thrown) {
        log.warn("event=chash_validate_failed cause={}", thrown.toString(), thrown);
        if (thrown instanceof AdminDdlSession.Refused r
                && ("57P01".equals(r.sqlState()) || (r.sqlState() != null && r.sqlState().startsWith("08")))) {
            // the backend was ended under the job (the schema walk terminates a validator, or the server restarted)
            return failed(503, "connection_lost", "the job's database session was ended (a schema migration or a server "
                + "restart); a constraint already validated stays validated and a rerun resumes at the next one");
        }
        if (thrown instanceof AdminDdlSession.Refused r) {
            return failed(r.status(), r.reason(), r.getMessage());
        }
        String state = sqlState(thrown);
        if ("40P01".equals(state) || "40001".equals(state) || "55P03".equals(state)) {
            return failed(503, "transient_db_error", "the job failed: " + thrown.getClass().getSimpleName()
                + " (a constraint already validated stays validated; a rerun resumes at the next one)");
        }
        return failed(500, "job_failed", "the job failed: " + thrown.getClass().getSimpleName()
            + " (a constraint already validated stays validated; a rerun resumes at the next one)");
    }

    private static String sqlState(Throwable e) {
        int depth = 0;
        for (Throwable t = e; t != null && depth < 32; t = t.getCause(), depth++) {
            if (t instanceof java.sql.SQLException se && se.getSQLState() != null) return se.getSQLState();
        }
        return null;
    }

    static EngineJobs.Outcome failed(int status, String reason, String error) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("error", error);
        body.put("reason", reason);
        return new EngineJobs.Outcome(status, body);
    }

    /** The listener a job's work hands the validator: it writes the progress and asks the job whether to stop. */
    static ChashConstraintValidator.Listener listener(EngineJobs.Job<Progress> job, Progress p, java.util.function.Consumer<String> stage) {
        return new ChashConstraintValidator.Listener() {
            @Override
            public void planned(int total) {
                p.planned(total);
                stage.accept("validating");
            }

            @Override
            public void current(String table, Long bytes) {
                p.current(table, bytes);
            }

            @Override
            public void stepped(Step step) {
                p.stepped(step);
            }

            @Override
            public String stopReason() {
                return job.stopReason();
            }

            @Override
            public Object owner() {
                return job;
            }
        };
    }
}
