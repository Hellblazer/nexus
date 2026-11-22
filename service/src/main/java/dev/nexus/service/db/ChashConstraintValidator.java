// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

import dev.nexus.service.db.ChashConformanceConstraints.Row;
import dev.nexus.service.db.ChashConformanceConstraints.Target;
import dev.nexus.service.db.ChashConformanceConstraints.Tree;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Table;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.HexFormat;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.LinkedHashMap;
import java.util.function.Supplier;

/**
 * nexus-f3yqd, engine 0.1.190: the work of {@code POST /v1/admin/validate-constraints}, {@code VALIDATE CONSTRAINT} of the
 * two chash width CHECKs the migrations leave NOT VALID ({@link ChashConformanceConstraints#TARGETS}: the one list, never a
 * second copy). The job, its progress and its routes are {@code ValidateConstraintsJobs} and
 * {@code ValidateConstraintsHandler}; this class holds what touches the database.
 *
 * <p><b>Never at boot.</b> Sam's rule (2026-11-11): a VALIDATE scans a whole table, so it runs only as a job a user starts
 * (an {@code nx} command, {@code nx doctor --fix}), never from a changeset or a boot hook; a boot-time VALIDATE crash-loops
 * an install whose rows differ from the measured one. Nothing in the engine calls this class on its own.
 *
 * <p><b>Which constraints.</b> Exactly the rows {@link ChashConformanceConstraints#query} reads for the targets: each
 * target's constraint on its declaring table and every same-named copy on a partition below it ({@code nexus.chunks} is
 * partitioned model by tenant, so a store holds a copy per leaf). A row whose definition is not the expected check (a
 * same-named weaker constraint) is refused and reported, never validated; so is every row above it in the partition tree,
 * because {@code VALIDATE CONSTRAINT} on a partitioned table recurses into its copies and would validate the weak one.
 *
 * <p><b>Order.</b> Deepest first: each leaf partition's copy, each as a step of its own on a connection of its own (its own
 * statements, no transaction spanning steps), then the model partitions, then the parent, which is cheap once every copy
 * below it is valid. A row that is already valid is skipped, and that is the whole of resume: a rerun after a stop, a refusal
 * or a crash does the steps that are left. A row with a copy below it that is not valid is not attempted (reported as
 * {@code descendant_not_validated}), since the recursion would meet that copy.
 *
 * <p><b>Per step.</b> (1) a bounded conformance pre-check of a leaf: the rows whose {@code octet_length(chash)} is not 32,
 * counted up to {@code precheck cap} under a statement timeout, with a few example chashes (hex, at most 64 bytes of each);
 * any such row refuses the table, naming it, and leaves its constraint NOT VALID. (2) {@code ALTER TABLE ... VALIDATE
 * CONSTRAINT} under {@code lock_timeout}, retried with backoff while the lock is not available ({@code 55P03}), and a
 * generous bounded {@code statement_timeout}. A step that gives up on the lock or the timeout is {@code deferred}, not
 * refused: nothing is wrong with the data and a rerun tries it again. The VALIDATE is the authority: a pre-check that could
 * not run does not stop it, and a row that breaks the CHECK fails it with {@code check_violation}, which is reported as a
 * refusal with the pre-check's count when there is one. Each step names its pre-check in {@code precheck}: {@code clean},
 * {@code bad_rows}, {@code unavailable} (the session cannot read every row), {@code timeout}, or null for a table with a
 * copy below it (nothing of its own to read).
 *
 * <p><b>Locks.</b> {@code VALIDATE CONSTRAINT} takes SHARE UPDATE EXCLUSIVE on the table it validates: searches, inserts,
 * updates and deletes continue; only another DDL on it, VACUUM, ANALYZE or a second VALIDATE waits. It reads the table once,
 * so a big leaf takes as long as a sequential scan of it, and PostgreSQL gives no row progress; the job reports the current
 * table and its size instead. The pre-check takes only an ACCESS SHARE lock where the session reads every row as it is.
 * Another session's SHARE lock on a leaf (an index build, a second VALIDATE) makes that step wait and retry. An ACCESS
 * EXCLUSIVE lock on a leaf (DDL, VACUUM FULL, TRUNCATE) also blocks the job's catalog read of the constraints
 * ({@code pg_get_constraintdef} waits behind it, which the conformance route's read does too), so the read runs under
 * {@code lock_timeout} with the same retry and a lock held past the bound fails the job as a 503 {@code lock_timeout} having
 * changed nothing.
 *
 * <p><b>Role and connection.</b> The engine's admin connection ({@link AdminConnection}, the {@code NX_DB_ADMIN_*} values
 * the migrator and the other operator steps use): the schema owner, because only a table's owner may {@code ALTER} it, and
 * not {@code nexus_svc}, which does not own the tables and whose every connection is scoped to one tenant. The VALIDATE is
 * not row filtered, so it sees every tenant's rows whatever the role. The pre-check is not so lucky, and it never takes a
 * strong lock to get around that: <b>no step ever lifts FORCE row-level security or takes ACCESS EXCLUSIVE on any table</b>
 * (search reads {@code nexus.catalog_document_chunks} through {@code chunk_live_owners}, so an exclusive lock there stalls
 * every search and index write). The pre-check reads a table only where this session already reads every row of it
 * ({@link AdminDdlSession#requireUnrestrictedView}: a superuser, BYPASSRLS, or a permissive SELECT policy with a true qual
 * that applies to the role; the {@code nexus.chunks} leaves copy {@code chunks_gate_probe_owner_read} from the parent, which
 * names the migrating role, the admin role). It then holds an ACCESS SHARE lock only. Where it does not (production's
 * {@code nexus.catalog_document_chunks}: FORCE row-level security, no owner policy) the pre-check is <b>unavailable</b>: the
 * job runs the VALIDATE, which is atomic and takes SHARE UPDATE EXCLUSIVE, and a {@code check_violation} (23514) is the
 * refusal, with {@code bad_rows} unknown (null) and the constraint left NOT VALID; a dry run reports {@code precheck:
 * unavailable} and does not say the table would validate. The engine holds no {@code nexus_diag} connection (that role's
 * credentials live in the client's {@code pg_credentials}, and in the view era it has no SELECT on the tables), so the
 * cross-tenant count is not reachable another way.
 *
 * <p><b>Bounds.</b> One job, one connection holding the run's advisory lock for the job and one connection per step (so two
 * at a time, from the admin role's own connections, not the service pool). The properties are read once per run:
 * {@code nexus.validate.lock_timeout_ms} (5000), {@code .lock_attempts} (5), {@code .retry_backoff_ms} (1000, doubling,
 * at most 15 s), {@code .statement_timeout_ms} (30 minutes), {@code .precheck_timeout_ms} (60000), {@code .precheck_cap}
 * (1000), {@code .tenant_count_budget_ms} (30000) and {@code .pause_ms} between steps (100, to spread the IO). The three
 * timeouts are clamped to {@link AdminDdlSession#MAX_STATEMENT_TIMEOUT_MS}, a minute under the admin connection's socket
 * timeout, so the server's own timeout always fires before the client's read does.
 *
 * <p><b>Shutdown and restart.</b> {@link #cancelRunning} cancels the VALIDATE a step has in flight (the engine's shutdown
 * calls it), and the backends carry the {@code application_name} prefix {@link #APPLICATION_NAME_PREFIX}, which the schema
 * migration walk terminates beside the index builders when it has schema work to do (a VALIDATE is atomic: ending it
 * changes nothing, and the next run resumes), so an orphaned VALIDATE never makes a walk's ACCESS EXCLUSIVE queue behind it.
 */
public final class ChashConstraintValidator {

    private static final Logger log = LoggerFactory.getLogger(ChashConstraintValidator.class);

    /** The session advisory lock a run holds: the bytes of {@code "chashval"} as a bigint. */
    public static final long ADVISORY_LOCK_KEY = 0x636861736876616cL;

    public static final String LOCK_TIMEOUT_PROPERTY = "nexus.validate.lock_timeout_ms";
    public static final String LOCK_ATTEMPTS_PROPERTY = "nexus.validate.lock_attempts";
    public static final String RETRY_BACKOFF_PROPERTY = "nexus.validate.retry_backoff_ms";
    public static final String STATEMENT_TIMEOUT_PROPERTY = "nexus.validate.statement_timeout_ms";
    public static final String PRECHECK_TIMEOUT_PROPERTY = "nexus.validate.precheck_timeout_ms";
    public static final String PRECHECK_CAP_PROPERTY = "nexus.validate.precheck_cap";
    public static final String PAUSE_PROPERTY = "nexus.validate.pause_ms";
    public static final String TENANT_COUNT_BUDGET_PROPERTY = "nexus.validate.tenant_count_budget_ms";

    /** The {@code application_name} prefix of every backend this class opens; {@code SchemaMigrator} ends such backends. */
    public static final String APPLICATION_NAME_PREFIX = "nexus-chash-validate";

    /** How many example chashes a refusal names. */
    static final int EXAMPLES = 3;
    /** The most bytes of a bad chash an example shows (a wrong-width value can be any length). */
    private static final int EXAMPLE_BYTES = 64;
    private static final long MAX_BACKOFF_MS = 15_000L;

    private static final String SQLSTATE_LOCK_NOT_AVAILABLE = "55P03";
    private static final String SQLSTATE_QUERY_CANCELED = "57014";
    private static final String SQLSTATE_CHECK_VIOLATION = "23514";
    private static final String SQLSTATE_UNDEFINED_TABLE = "42P01";
    private static final String SQLSTATE_DEADLOCK = "40P01";
    private static final String SQLSTATE_SERIALIZATION = "40001";

    /** What a step ended as. */
    public enum Outcome {
        /** The constraint is valid now (this run validated it). */
        VALIDATED,
        /** A dry run: every pre-check is clean, so a real run would validate it. */
        WOULD_VALIDATE,
        /** It was valid before the run; nothing was done. */
        ALREADY_VALID,
        /** A row breaks the check, or the definition is not the expected one: the data or the schema needs fixing first. */
        REFUSED,
        /** Not done and nothing wrong with the data: a lock or timeout bound was reached, the constraint is absent, or a copy below it is not valid. A rerun tries it again. */
        DEFERRED
    }

    /**
     * One step's result. {@code badRows} is the count of rows that break the check (at most the pre-check cap; {@code
     * badRowsCapped} says it stopped there), null when no count was taken; {@code exampleChashes} is up to {@value #EXAMPLES}
     * of them, hex, each cut at 64 bytes; {@code reason} names why a refused or deferred step was ({@code bad_rows},
     * {@code definition_mismatch}, {@code lock_timeout}, {@code statement_timeout}, {@code precheck_timeout},
     * {@code constraint_absent}, {@code descendant_not_validated}, {@code not_validated_after}, {@code precheck_unavailable},
     * {@code relation_vanished}, {@code deadlock_detected}, {@code state_unreadable}). {@code tenantRows} is, for a table whose
     * pre-check was unavailable and whose VALIDATE failed on a bad row, the count of bad rows by tenant, taken per known
     * tenant through its own scoped session (ACCESS SHARE only, bounded: {@code badRowsCapped} is true when the cap or the
     * time budget cut it short, or a tenant could not be read; a tenant with rows but no token is not counted).
     */
    public record Step(String table, String constraint, Outcome outcome, String reason, Long badRows,
                       boolean badRowsCapped, List<String> exampleChashes, String precheck, Map<String, Long> tenantRows,
                       Detail detail) {
        public Step(String table, String constraint, Outcome outcome, String reason, Long badRows,
                    boolean badRowsCapped, List<String> exampleChashes, String precheck, Map<String, Long> tenantRows) {
            this(table, constraint, outcome, reason, badRows, badRowsCapped, exampleChashes, precheck, tenantRows, null);
        }

        Step withDetail(Detail d) {
            return new Step(table, constraint, outcome, reason, badRows, badRowsCapped, exampleChashes, precheck, tenantRows, d);
        }

        static Step of(String table, String constraint, Outcome outcome) {
            return new Step(table, constraint, outcome, null, null, false, List.of(), null, Map.of());
        }

        static Step because(String table, String constraint, Outcome outcome, String reason) {
            return new Step(table, constraint, outcome, reason, null, false, List.of(), null, Map.of());
        }

        Step withPrecheck(String state) {
            return new Step(table, constraint, outcome, reason, badRows, badRowsCapped, exampleChashes, state, tenantRows, detail);
        }
    }

    /**
     * What is wrong with a refused step's bad rows, by {@link LegacyChunkSurvey}'s reasons: {@code reasonCounts} counts per
     * reason among the rows it looked at ({@code countsCapped}: it stopped at its bound, or a tenant could not be read, so
     * the figures are a floor); {@code chunks} lists, at most {@value #DETAIL_LIST_CAP}, the bad chunks in key order, each with its
     * tenant, collection, model, chash, reason and OWNERS (null owners: no tenant-scoped session was available to look them up, or
     * the lookup ran out of time); {@code chunksCapped} says more exist; {@code documents} (the manifest's refusal) lists, per
     * reason, the documents that hold the bad rows, at most {@value #DETAIL_LIST_CAP} each with an exact capped flag; {@code tenants}
     * are the tenants whose rows are in the account and {@code unattributed} says rows of any tenant are (a tenant reset drops
     * the account in either case). Null on a step that has
     * no such account (a count that could not be taken).
     */
    public record Detail(Map<String, Long> reasonCounts, boolean countsCapped, List<LegacyChunkSurvey.Bad> chunks,
                         boolean chunksCapped, Map<String, LegacyChunkSurvey.DocList> documents, java.util.Set<String> tenants,
                         boolean unattributed) {}

    /** What the job tells and asks of a run. */
    public interface Listener {
        /** The plan: the constraint rows the run will look at. */
        void planned(int total);

        /** The table the run is working on now, and its size in bytes (null when it could not be read). */
        void current(String table, Long bytes);

        /** A step ended. */
        void stepped(Step step);

        /** Why the run should stop before its next step ({@code cancelled}, {@code evicted}, {@code wall_clock_cap}), or null. */
        String stopReason();

        /** What this run belongs to (the job), so {@link #cancelRunningFor} cancels this run and no other; null names nothing. */
        default Object owner() {
            return null;
        }
    }

    /**
     * What a run ended with. {@code stopped} is the reason it stopped early, or null when it went through every step.
     * {@code allValidated} is the conformance route's answer read after the last step, or null when that read could not get
     * its lock within the bound (the steps are done and reported; ask the conformance route).
     */
    public record Result(String stopped, Boolean allValidated) {}

    /** The bounds of one run, read from the properties. */
    record Config(long lockTimeoutMs, int lockAttempts, long backoffMs, long statementTimeoutMs, long precheckTimeoutMs,
                  int precheckCap, long pauseMs, long tenantCountBudgetMs) {
        static Config fromProperties() {
            long max = AdminDdlSession.MAX_STATEMENT_TIMEOUT_MS;
            return new Config(clamp(Long.getLong(LOCK_TIMEOUT_PROPERTY, 5_000L), 1, max),
                Math.max(1, Integer.getInteger(LOCK_ATTEMPTS_PROPERTY, 5)),
                Math.max(0, Long.getLong(RETRY_BACKOFF_PROPERTY, 1_000L)),
                clamp(Long.getLong(STATEMENT_TIMEOUT_PROPERTY, 30L * 60_000L), 1_000, max),
                clamp(Long.getLong(PRECHECK_TIMEOUT_PROPERTY, 60_000L), 1, max),
                Math.max(1, Integer.getInteger(PRECHECK_CAP_PROPERTY, 1_000)),
                Math.max(0, Long.getLong(PAUSE_PROPERTY, 100L)),
                clamp(Long.getLong(TENANT_COUNT_BUDGET_PROPERTY, 30_000L), 0, max));
        }

        private static long clamp(long v, long lo, long hi) {
            return Math.min(hi, Math.max(lo, v));
        }
    }

    private static final String CONTROLLER_APP = APPLICATION_NAME_PREFIX;
    private static final String PRECHECK_VIEW = "a role that reads every row of the table (a superuser, BYPASSRLS, or a true-qual "
        + "SELECT policy)";

    /** Opens an admin session; {@link AdminDdlSession#open} in production, a test's own where it needs a connection that fails. */
    @FunctionalInterface
    interface Opener {
        AdminDdlSession open(AdminConnection admin, AdminDdlSession.Settings settings, boolean lock);
    }

    private final AdminConnection admin;
    private final Opener opener;
    private final TenantScope scope;
    private final Supplier<List<String>> knownTenants;
    /** Test seam: rewrites the constraint rows the plan is built from (a hand-edited catalog the migrations cannot produce). */
    private volatile java.util.function.UnaryOperator<List<Row>> planRows = r -> r;
    /** The step session in flight, so {@link #cancelRunning} can reach its statement from another thread. */
    private volatile AdminDdlSession activeStep;
    /** The controller session of the run in flight (it reads the catalog and holds the run lock), for the same cancel. */
    private volatile AdminDdlSession activeController;
    /** The listener of the run in flight, so {@link #cancelRunningFor} cancels only the run of the job it names. */
    private volatile Listener activeListener;

    /** A validator that cannot count a refused table's bad rows by tenant ({@code bad_rows} stays null there). */
    public ChashConstraintValidator(AdminConnection admin) {
        this(admin, null, null);
    }

    /**
     * @param scope        the tenant-scoped sessions a refused table's bad rows are counted through (null: no count)
     * @param knownTenants the tenants to count for ({@code TokenStore#listKnownTenants}); the default tenant is added
     */
    public ChashConstraintValidator(AdminConnection admin, TenantScope scope, Supplier<List<String>> knownTenants) {
        this(admin, AdminDdlSession::open, scope, knownTenants);
    }

    ChashConstraintValidator(AdminConnection admin, Opener opener, TenantScope scope, Supplier<List<String>> knownTenants) {
        this.admin = Objects.requireNonNull(admin, "admin");
        this.opener = Objects.requireNonNull(opener, "opener");
        this.scope = scope;
        this.knownTenants = knownTenants;
    }

    /** Test seam: the plan is built from {@code rewrite} of the constraint rows read from the catalog. */
    void planRowsForTests(java.util.function.UnaryOperator<List<Row>> rewrite) {
        this.planRows = rewrite;
    }

    /**
     * Cancels whatever statement the running step (and the run's controller) has in flight, and refuses every later one
     * ({@link AdminDdlSession#cancel} has what that guarantees). A {@code DELETE} of the job calls it after setting the job's
     * stop reason, and the run ends with that reason. The engine's shutdown calls it after it
     * interrupts the job's thread, so the backend does not keep scanning a table for up to the statement timeout after the
     * engine that asked for it is gone. The step ends as stopped; nothing is lost (a VALIDATE is atomic).
     */
    public void cancelRunning() {
        AdminDdlSession step = activeStep;
        AdminDdlSession controller = activeController;
        if (controller != null) controller.cancel();
        if (step != null) step.cancel();
    }

    /**
     * {@link #cancelRunning}, for the run that belongs to {@code owner} (a {@code DELETE} of the job sets its stop reason
     * first). The job named may have ended and another started since: the sessions are read first and the run's listener after
     * (a run publishes its listener before its sessions and clears it after them, all volatile), so a session read here belongs
     * to the listener read after it or to a run that has already ended, and a run of another owner is left alone. Returns
     * whether it cancelled.
     */
    public boolean cancelRunningFor(Object owner) {
        AdminDdlSession step = activeStep;
        AdminDdlSession controller = activeController;
        Listener l = activeListener;
        if (owner == null || l == null || l.owner() != owner) return false;
        if (controller != null) controller.cancel();
        if (step != null) step.cancel();
        return true;
    }

    /**
     * The checkpoint a step passes between phases (the session guard, the size read, the pre-check, the VALIDATE): a stop ends
     * it here. The engine's stop interrupts the job's thread first and cancels the sessions second, so a step whose session was
     * not yet published to {@link #cancelRunning} sees the interrupt, and one that was sees the cancel. A statement of the step
     * is cancelled, or refused if it starts after the stop ({@link AdminDdlSession#cancel} has the guarantee and its one
     * exception), and the checkpoints keep the step from reporting a result for the phase the stop cut short: a cancelled
     * pre-check is a stop, not a "timeout" a dry run would report.
     */
    private static void checkStop(AdminDdlSession s) {
        if (Thread.currentThread().isInterrupted() || s.isCancelled()) throw new Stopped();
    }

    /** The listener's stop reason ({@code cancelled}, {@code evicted}, {@code wall_clock_cap}), or {@code interrupted} for the engine's stop. */
    private static String stopOr(Listener listener) {
        String reason = listener.stopReason();
        return reason != null ? reason : "interrupted";
    }

    /** True when {@code e} is a statement ended by the engine's stop: 57014 on a session that was cancelled (or a thread that was interrupted), or the bounded read's own "interrupted". */
    private static boolean stopSignal(RuntimeException e, AdminDdlSession s) {
        if (e instanceof AdminDdlSession.Refused r && "interrupted".equals(r.reason())) return true;
        String state = e instanceof DataAccessException d ? d.sqlState() : ((AdminDdlSession.Refused) e).sqlState();
        return SQLSTATE_QUERY_CANCELED.equals(state) && (s.isCancelled() || Thread.currentThread().isInterrupted());
    }

    /** One planned step: the constraint row, its target, and whether a copy lies below it. */
    private record Planned(Target target, Row row, int depth, boolean leaf, List<Long> below) {
        String label() {
            return CHUNKS.getSchema().getName() + "." + row.relationName();
        }
    }

    /**
     * Runs the job. With {@code dryRun} every pre-check runs and nothing is altered. Refuses ({@link
     * AdminDdlSession.Refused}) when another run holds the lock (409), a schema migration holds its lock (503) or the admin
     * connection cannot be opened or may not alter the tables (503): none of those is a step's to defer.
     */
    public Result run(boolean dryRun, Listener listener) {
        Config cfg = Config.fromProperties();
        // The controller session holds the run's advisory lock for the whole job and reads the catalog; each step has its own.
        AdminDdlSession.Settings controllerSettings = settings(CHUNKS.getSchema().getName(), CHUNKS.getName(), true);
        try (AdminDdlSession controller = opener.open(admin, controllerSettings, true)) {
            activeListener = listener;
            activeController = controller;
            try {
                return runOn(controller, dryRun, listener, cfg);
            } catch (DataAccessException | AdminDdlSession.Refused e) {
                // a statement of the controller cancelled by the engine's stop is the run's stop, not its failure
                if (stopSignal(e, controller)) return new Result(stopOr(listener), null);
                throw e;
            } finally {
                activeController = null;
                activeListener = null;
            }
        }
    }

    private Result runOn(AdminDdlSession controller, boolean dryRun, Listener listener, Config cfg) {
        {
            controller.guard("30s");
            List<Row> rows = planRows.apply(readRowsBounded(controller, cfg, listener));
            List<Planned> plan = new ArrayList<>();
            List<Step> upFront = new ArrayList<>();
            for (Target t : ChashConformanceConstraints.TARGETS) {
                Tree tree = ChashConformanceConstraints.tree(t, rows);
                if (tree.root() == null) {
                    upFront.add(Step.because(t.qualifiedTable(), t.constraint(), Outcome.DEFERRED, "constraint_absent"));
                    continue;
                }
                List<Row> all = new ArrayList<>();
                all.add(tree.root());
                all.addAll(tree.copies());
                Map<Long, Row> byRelation = new HashMap<>();
                for (Row r : all) byRelation.put(r.relation(), r);
                Map<Long, List<Long>> children = new HashMap<>();
                for (Row r : all) {
                    if (r.relation() != tree.root().relation()) {
                        children.computeIfAbsent(r.relationParent(), k -> new ArrayList<>()).add(r.relation());
                    }
                }
                for (Row r : all) {
                    List<Long> below = descendants(r.relation(), children);
                    plan.add(new Planned(t, r, depth(r, byRelation), below.isEmpty(), below));
                }
            }
            // deepest first, then by name so a rerun walks the same order
            plan.sort(Comparator.comparingInt((Planned p) -> -p.depth()).thenComparing(Planned::label));
            listener.planned(plan.size() + upFront.size());
            for (Step s : upFront) listener.stepped(s);

            Map<Long, Boolean> usable = new HashMap<>();   // relation -> its copy is valid, or would be, after this run
            for (Planned p : plan) {
                String stop = listener.stopReason();
                if (stop == null && Thread.interrupted()) stop = "interrupted";
                if (stop != null) return new Result(stop, null);
                Step step = stepFor(p, usable, dryRun, cfg, listener);
                if (step == null) {   // a sleep was interrupted or a statement cancelled: the engine's stop or a cancel
                    return new Result(stopOr(listener), null);
                }
                usable.put(p.row().relation(), step.outcome() == Outcome.VALIDATED || step.outcome() == Outcome.ALREADY_VALID
                    || step.outcome() == Outcome.WOULD_VALIDATE);
                listener.stepped(step);
                if (cfg.pauseMs() > 0 && !sleep(cfg.pauseMs(), listener)) return new Result(stopOr(listener), null);
            }
            // a cancel that landed after the last step is still the run's stop: the 202 said the job is stopping
            String stop = listener.stopReason();
            if (stop != null) return new Result(stop, null);
            Boolean all;
            try {
                all = ChashConformanceConstraints.aggregate(readRowsBounded(controller, cfg, listener)).allValidated();
            } catch (AdminDdlSession.Refused | DataAccessException e) {
                if (stopSignal(e, controller) || listener.stopReason() != null) return new Result(stopOr(listener), null);
                all = null;   // the steps are done and reported; the conformance route answers what this read could not
            }
            return new Result(null, all);
        }
    }

    /**
     * The catalog read of the constraint rows, tolerant of a lock: {@code pg_get_constraintdef} of a copy waits behind an
     * ACCESS EXCLUSIVE lock on its partition (a DDL, VACUUM FULL, TRUNCATE), so the read runs under {@code lock_timeout} and is
     * retried with backoff, and a lock still held after the attempts is a {@code Refused} 503 {@code lock_timeout}: nothing
     * was changed and a rerun starts again. An ACCESS SHARE or SHARE lock (an index build, another VALIDATE) does not block it.
     */
    private static List<Row> readRowsBounded(AdminDdlSession s, Config cfg, Listener listener) {
        s.setSession("lock_timeout", cfg.lockTimeoutMs() + "ms");
        long backoff = cfg.backoffMs();
        for (int attempt = 1; ; attempt++) {
            try {
                return readRows(s.ctx());
            } catch (DataAccessException e) {
                if (!SQLSTATE_LOCK_NOT_AVAILABLE.equals(e.sqlState())) throw e;
                if (attempt >= cfg.lockAttempts()) {
                    throw new AdminDdlSession.Refused(503, "lock_timeout", "a partition of nexus.chunks is locked by another "
                        + "session (ACCESS EXCLUSIVE), so the constraints could not be read within the lock bound; nothing "
                        + "was changed, and a rerun starts again", SQLSTATE_LOCK_NOT_AVAILABLE);
                }
                if (!sleep(backoff, listener)) throw new AdminDdlSession.Refused(503, "interrupted", "the run was stopped");
                backoff = Math.min(MAX_BACKOFF_MS, Math.max(1, backoff) * 2);
            }
        }
    }

    private static List<Row> readRows(DSLContext ctx) {
        List<Row> rows = new ArrayList<>();
        for (var r : ChashConformanceConstraints.query(ctx).fetch()) {
            rows.add(new Row(r.value1(), r.value2(), r.value3(), r.value4(), Boolean.TRUE.equals(r.value5()), r.value6()));
        }
        return rows;
    }

    private static boolean normalDefinition(Row r, Target t) {
        return ChashConformanceConstraints.normalise(r.definition()).equals(t.expectedDefinition());
    }

    /** The oids of every constraint row below {@code relation} (children, their children, and so on), bounded by the tree's depth. */
    private static List<Long> descendants(long relation, Map<Long, List<Long>> children) {
        List<Long> out = new ArrayList<>();
        List<Long> frontier = List.of(relation);
        for (int level = 0; level < 32 && !frontier.isEmpty(); level++) {
            List<Long> next = new ArrayList<>();
            for (long f : frontier) {
                for (long c : children.getOrDefault(f, List.of())) {
                    out.add(c);
                    next.add(c);
                }
            }
            frontier = next;
        }
        return out;
    }

    private static int depth(Row r, Map<Long, Row> byRelation) {
        int d = 0;
        Row at = r;
        while (d < 32 && at != null && at.relationParent() != 0 && byRelation.containsKey(at.relationParent())) {
            at = byRelation.get(at.relationParent());
            d++;
        }
        return d;
    }

    // ---- one step ------------------------------------------------------------------------------------------------

    private static AdminDdlSession.Settings settings(String schema, String table, boolean controller) {
        return new AdminDdlSession.Settings(controller ? CONTROLLER_APP : CONTROLLER_APP + "-step", ADVISORY_LOCK_KEY,
            "chash-validate", schema, table, "validate-constraints dry run");
    }

    /** The step for {@code p}, or null when the run was told to stop (a backoff sleep or a cancelled VALIDATE was interrupted). */
    private Step stepFor(Planned p, Map<Long, Boolean> usable, boolean dryRun, Config cfg, Listener listener) {
        Target t = p.target();
        String table = p.label();
        String constraint = t.constraint();
        if (!normalDefinition(p.row(), t)) {
            return Step.because(table, constraint, Outcome.REFUSED, "definition_mismatch");
        }
        if (p.row().validated()) {
            return Step.of(table, constraint, Outcome.ALREADY_VALID);
        }
        for (long below : p.below()) {
            if (!Boolean.TRUE.equals(usable.get(below))) {
                return Step.because(table, constraint, Outcome.DEFERRED, "descendant_not_validated");
            }
        }
        String schema = CHUNKS.getSchema().getName();
        try (AdminDdlSession s = opener.open(admin, settings(schema, p.row().relationName(), false), false)) {
            activeStep = s;
            try {
                // a cancel sets the job's reason and then looks for a session to cancel; this reads the reason after publishing
                // the session (both volatile), so a cancel that found no session is seen here and no statement starts
                if (listener.stopReason() != null) return null;
                checkStop(s);   // a stop that landed before the session was published to cancelRunning
                s.guard(statementTimeout(cfg));
                s.setSession("lock_timeout", cfg.lockTimeoutMs() + "ms");   // the size read takes a lock too
                checkStop(s);
                listener.current(table, sizeOf(s.ctx(), p.row().relation()));
                long backoff = cfg.backoffMs();
                for (int attempt = 1; ; attempt++) {
                    String retry;
                    try {
                        return attemptStep(s, p, dryRun, cfg);
                    } catch (Transient e) {
                        retry = e.reason;
                    } catch (DataAccessException | AdminDdlSession.Refused e) {
                        // a step's own database error is that step's, never the whole job's: a lock, a deadlock or a
                        // serialization failure is tried again, a relation dropped since the plan is deferred
                        String state = e instanceof DataAccessException d ? d.sqlState() : ((AdminDdlSession.Refused) e).sqlState();
                        if (SQLSTATE_UNDEFINED_TABLE.equals(state)) {
                            return Step.because(table, constraint, Outcome.DEFERRED, "relation_vanished");
                        }
                        if (SQLSTATE_LOCK_NOT_AVAILABLE.equals(state)) {
                            retry = "lock_timeout";
                        } else if (SQLSTATE_DEADLOCK.equals(state) || SQLSTATE_SERIALIZATION.equals(state)) {
                            retry = "deadlock_detected";
                        } else {
                            throw e;
                        }
                    }
                    if (attempt >= cfg.lockAttempts()) {
                        log.warn("event=chash_validate_deferred table={} reason={} attempts={}", table, retry, attempt);
                        return Step.because(table, constraint, Outcome.DEFERRED, retry);
                    }
                    if (!sleep(backoff, listener)) return null;   // a cancel ends the backoff, as an interrupt does
                    backoff = Math.min(MAX_BACKOFF_MS, Math.max(1, backoff) * 2);
                }
            } catch (Stopped e) {
                return null;
            } catch (DataAccessException | AdminDdlSession.Refused e) {
                // the prelude (guard, size read) cancelled by the stop; anything else is the job's failure as before
                if (stopSignal(e, s)) return null;
                throw e;
            } finally {
                activeStep = null;
            }
        }
    }

    /** A bound that may clear on its own: the lock or the clock. */
    private static final class Transient extends RuntimeException {
        private final String reason;

        Transient(String reason) {
            super(reason, null, false, false);
            this.reason = reason;
        }
    }

    /** The engine is stopping: a cancelled VALIDATE of an interrupted job is the stop, not a step's result. */
    private static final class Stopped extends RuntimeException {
        Stopped() {
            super("stopped", null, false, false);
        }
    }

    private Step attemptStep(AdminDdlSession s, Planned p, boolean dryRun, Config cfg) {
        Target t = p.target();
        String table = p.label();
        String constraint = t.constraint();
        String schema = CHUNKS.getSchema().getName();
        Precheck pre = null;
        checkStop(s);
        if (p.leaf()) {
            pre = precheck(s, schema, p.row().relationName(), cfg);
            checkStop(s);   // a pre-check cut short by a stop is a stop, not a "timeout" a real run would go on from
            if (pre.bad() > 0) {
                return new Step(table, constraint, Outcome.REFUSED, "bad_rows", pre.bad(), pre.capped(), pre.examples(),
                    pre.state(), Map.of()).withDetail(detailFor(s, schema, p.row().relationName(), cfg, pre.bad()));
            }
        }
        String preState = pre == null ? null : pre.state();
        if (dryRun) {
            if (pre != null && pre.unknown() != null) {
                return Step.because(table, constraint, Outcome.DEFERRED, pre.unknown()).withPrecheck(preState);
            }
            return Step.of(table, constraint, Outcome.WOULD_VALIDATE).withPrecheck(preState);
        }
        checkStop(s);
        s.setSession("lock_timeout", cfg.lockTimeoutMs() + "ms");
        s.setSession("statement_timeout", statementTimeout(cfg));
        checkStop(s);
        try {
            s.ddl("ALTER TABLE %I.%I VALIDATE CONSTRAINT %I", List.of(schema, p.row().relationName(), constraint));
        } catch (AdminDdlSession.Refused e) {
            if (SQLSTATE_LOCK_NOT_AVAILABLE.equals(e.sqlState())) throw new Transient("lock_timeout");
            if (SQLSTATE_QUERY_CANCELED.equals(e.sqlState())) {
                if (Thread.currentThread().isInterrupted() || s.isCancelled()) throw new Stopped();   // the shutdown's cancel
                return Step.because(table, constraint, Outcome.DEFERRED, "statement_timeout").withPrecheck(preState);
            }
            if (SQLSTATE_CHECK_VIOLATION.equals(e.sqlState())) {
                // a row broke the check: the pre-check said clean or could not run; count them now, for the report
                Precheck after = precheckOrUnknown(s, schema, p.row().relationName(), cfg);
                Step refused = after.bad() > 0
                    ? new Step(table, constraint, Outcome.REFUSED, "bad_rows", after.bad(), after.capped(),
                        after.examples(), after.state(), Map.of())
                    : countByTenant(schema, p.row().relationName(), cfg, table, constraint, preState);
                // the admin session read every row of a leaf only when its own pre-check counted them; a count by tenant (the pre-check
                // unavailable or failed) leaves a leaf with no account rather than an RLS-subset floor given out as complete
                if (MANIFEST_RELATION.equals(p.row().relationName())) return refused.withDetail(manifestDetail(cfg, refused.badRows()));
                return p.leaf() && after.bad() > 0 ? refused.withDetail(leafDetail(s, schema, p.row().relationName(), cfg)) : refused;
            }
            throw e;
        }
        // the constraint is validated or not as the server says, read only now that the VALIDATE has returned; a read that
        // fails leaves the step deferred (a rerun skips it if it did validate), it does not fail the job
        boolean validated;
        try {
            validated = Boolean.TRUE.equals(s.states(List.of(constraint)).get(0).validated());
        } catch (DataAccessException e) {
            if (SQLSTATE_UNDEFINED_TABLE.equals(e.sqlState())) throw e;
            if (s.isCancelled()) {
                // a stop landed after the VALIDATE returned: it ran on the autocommit session and returned, so it committed
                log.info("event=chash_validated table={} constraint={} state_read=cancelled", table, constraint);
                return Step.of(table, constraint, Outcome.VALIDATED).withPrecheck(preState);
            }
            return Step.because(table, constraint, Outcome.DEFERRED, "state_unreadable").withPrecheck(preState);
        }
        if (!validated) {
            return Step.because(table, constraint, Outcome.DEFERRED, "not_validated_after").withPrecheck(preState);
        }
        log.info("event=chash_validated table={} constraint={}", table, constraint);
        return Step.of(table, constraint, Outcome.VALIDATED).withPrecheck(preState);
    }

    /**
     * The refusal of a table whose VALIDATE failed on a bad row and whose rows this session cannot read: the bad rows counted
     * per known tenant, each through that tenant's own scoped session (ACCESS SHARE only, the tenant's RLS policy admits its
     * rows), within a total time budget and the cap. Only on this path; a clean VALIDATE pays nothing. A tenant with rows and
     * no token is not counted, so the figure is a floor, and {@code bad_rows} stays null when nothing was counted. The
     * pre-check stays {@code unavailable}.
     */
    private Step countByTenant(String schema, String relation, Config cfg, String table, String constraint, String preState) {
        long total = 0;
        boolean capped = false;
        List<String> examples = new ArrayList<>();
        Map<String, Long> byTenant = new LinkedHashMap<>();
        if (scope != null && knownTenants != null) {
            List<String> tenants = new ArrayList<>();
            tenants.add(TenantConstants.DEFAULT_TENANT);
            try {
                for (String t : knownTenants.get()) if (!tenants.contains(t)) tenants.add(t);
            } catch (RuntimeException e) {
                capped = true;
            }
            long deadline = System.nanoTime() + cfg.tenantCountBudgetMs() * 1_000_000L;
            for (String tenant : tenants) {
                if (System.nanoTime() >= deadline) {
                    capped = true;
                    break;
                }
                int room = (int) Math.min(Integer.MAX_VALUE, cfg.precheckCap() - total);
                try {
                    Precheck c = scope.withTenant(tenant, ctx -> {
                        PgSession.setStatementAndLockBounds(ctx, (int) Math.min(10_000, cfg.precheckTimeoutMs()), 2_000);
                        return countBad(ctx, schema, relation, room);
                    });
                    if (c.bad() > 0) {
                        total += c.bad();
                        byTenant.put(tenant, c.bad());
                        for (String e : c.examples()) if (examples.size() < EXAMPLES) examples.add(e);
                    }
                    if (c.capped()) capped = true;   // this tenant has more rows than the room left under the cap
                } catch (RuntimeException e) {
                    capped = true;   // this tenant could not be read within its bound: the count is a floor
                }
            }
        }
        return new Step(table, constraint, Outcome.REFUSED, "bad_rows", total > 0 ? total : null, capped, List.copyOf(examples),
            preState, Map.copyOf(byTenant));
    }

    private static final String MANIFEST_RELATION = "catalog_document_chunks";

    /** The manifest is a plain table (a leaf of its own) with no text to read: its rows are classified by their chunks, per tenant. */
    private Detail detailFor(AdminDdlSession s, String schema, String relation, Config cfg, long expectedBad) {
        return MANIFEST_RELATION.equals(relation) ? manifestDetail(cfg, expectedBad) : leafDetail(s, schema, relation, cfg);
    }

    /** The most chunks (and, for the manifest, documents per reason) a refusal lists by name. */
    public static final int DETAIL_LIST_CAP = 20;
    /** All of one refusal's owner lookups together may take this long. */
    private static final long OWNER_BUDGET_MS = 10_000L;

    /**
     * A refused chunks relation's reasons, read by the admin session, and the listed chunks' owners, looked up through each
     * chunk's own tenant's scoped session (the admin session reads no catalog document under row-level security). Called only
     * where the pre-check counted the relation's rows as that session (so it reads every row, not an RLS subset), and under the
     * pre-check's own statement and lock bounds. Null when a read fails or hits its bound. The counts are not attributed to a
     * tenant (any tenant's rows are in them), so a tenant reset drops the account whole.
     */
    private Detail leafDetail(AdminDdlSession s, String schema, String relation, Config cfg) {
        try {
            s.setSession("lock_timeout", cfg.lockTimeoutMs() + "ms");
            s.setSession("statement_timeout", cfg.precheckTimeoutMs() + "ms");
            Table<?> rel = DSL.table(DSL.name(schema, relation));
            LegacyChunkSurvey.Counts counts = LegacyChunkSurvey.counts(s.ctx(), rel, DSL.trueCondition(), cfg.precheckCap());
            List<LegacyChunkSurvey.Bad> listed = LegacyChunkSurvey.chunks(s.ctx(), rel, DSL.trueCondition(), DETAIL_LIST_CAP);
            boolean listCapped = listed.size() > DETAIL_LIST_CAP;
            if (listCapped) listed = listed.subList(0, DETAIL_LIST_CAP);
            List<LegacyChunkSurvey.Bad> withOwners = withOwners(listed);
            java.util.Set<String> tenants = new java.util.TreeSet<>();
            for (var b : withOwners) tenants.add(b.tenant());
            return new Detail(counts.byReason(), counts.capped(), withOwners, listCapped, Map.of(), tenants, true);
        } catch (DataAccessException e) {
            return null;
        }
    }

    /** Each chunk's owners through its own tenant's scoped session, in a transaction of its own, all under one overall deadline. */
    private List<LegacyChunkSurvey.Bad> withOwners(List<LegacyChunkSurvey.Bad> listed) {
        if (scope == null) return List.copyOf(listed);
        var gate = LegacyChunkSurvey.gate(System.nanoTime() + OWNER_BUDGET_MS * 1_000_000L, 10_000);
        List<LegacyChunkSurvey.Bad> out = new ArrayList<>(listed.size());
        for (LegacyChunkSurvey.Bad b : listed) {
            LegacyChunkSurvey.Owners owners;
            try {
                owners = scope.withTenant(b.tenant(), ctx ->
                    LegacyChunkSurvey.owners(ctx, b.tenant(), b.collection(), b.model(), HexFormat.of().parseHex(b.chashHex()), 5, gate));
            } catch (RuntimeException ex) {
                owners = LegacyChunkSurvey.Owners.unknown();   // not looked up within the bound: unknown, never "no owner"
            }
            out.add(new LegacyChunkSurvey.Bad(b.tenant(), b.collection(), b.model(), b.chashHex(), b.reason(), owners));
        }
        return List.copyOf(out);
    }

    /**
     * The manifest's bad rows by what their chunk is, counted per known tenant through its scoped session (the same
     * floor-and-budget discipline as {@link #countByTenant}), with the documents that hold them per reason. The sum is held to the
     * pre-check cap, and {@code capped} is set when a tenant was not counted, a count stopped at the room left, or the sum is
     * short of {@code expectedBad} (a tenant with rows and no token). Null when no scoped session is available.
     */
    private Detail manifestDetail(Config cfg, Long expectedBad) {
        if (scope == null || knownTenants == null) return null;
        Map<String, Long> by = new LinkedHashMap<>();
        Map<String, List<LegacyChunkSurvey.DocRef>> docs = new java.util.TreeMap<>();
        Map<String, Boolean> docsCapped = new HashMap<>();
        java.util.Set<String> counted = new java.util.TreeSet<>();
        boolean capped = false;
        long total = 0;
        List<String> tenants = new ArrayList<>();
        tenants.add(TenantConstants.DEFAULT_TENANT);
        try {
            for (String t : knownTenants.get()) if (!tenants.contains(t)) tenants.add(t);
        } catch (RuntimeException e) {
            capped = true;
        }
        long deadline = System.nanoTime() + cfg.tenantCountBudgetMs() * 1_000_000L;
        for (String tenant : tenants) {
            if (System.nanoTime() >= deadline) { capped = true; break; }
            int room = (int) Math.min(Integer.MAX_VALUE, Math.max(0, cfg.precheckCap() - total));
            try {
                var got = scope.withTenant(tenant, ctx -> {
                    PgSession.setStatementAndLockBounds(ctx, (int) Math.min(10_000, cfg.precheckTimeoutMs()), 2_000);
                    var c = LegacyChunkSurvey.manifestCounts(ctx, tenant, room);
                    return java.util.Map.entry(c, c.byReason().isEmpty() ? Map.<String, LegacyChunkSurvey.DocList>of()
                        : LegacyChunkSurvey.manifestDocs(ctx, tenant, DETAIL_LIST_CAP));
                });
                LegacyChunkSurvey.Counts c = got.getKey();
                if (!c.byReason().isEmpty()) counted.add(tenant);
                for (var e : c.byReason().entrySet()) {
                    by.merge(e.getKey(), e.getValue(), Long::sum);
                    total += e.getValue();
                }
                if (c.capped()) capped = true;
                for (var e : got.getValue().entrySet()) {
                    List<LegacyChunkSurvey.DocRef> list = docs.computeIfAbsent(e.getKey(), k -> new ArrayList<>());
                    for (var d : e.getValue().docs()) {
                        if (list.size() < DETAIL_LIST_CAP) list.add(d); else docsCapped.put(e.getKey(), true);
                    }
                    if (e.getValue().capped()) docsCapped.put(e.getKey(), true);
                }
            } catch (RuntimeException e) {
                log.warn("event=chash_validate_manifest_detail_failed tenant={} error={}", tenant, e.getMessage());
                capped = true;
            }
        }
        // a tenant with bad rows and no token is not counted: the sum is then short of what the table has
        if (expectedBad != null && total < expectedBad) capped = true;
        Map<String, LegacyChunkSurvey.DocList> docLists = new LinkedHashMap<>();
        docs.forEach((k, v) -> docLists.put(k, new LegacyChunkSurvey.DocList(List.copyOf(v), docsCapped.getOrDefault(k, false))));
        return new Detail(by, capped, List.of(), false, docLists, counted, false);
    }

    private static String statementTimeout(Config cfg) {
        return cfg.statementTimeoutMs() + "ms";
    }

    // ---- the pre-check ---------------------------------------------------------------------------------------------

    /** The pre-check's answer: {@code bad} rows found (up to the cap), or {@code unknown} naming why it could not say. */
    private record Precheck(long bad, boolean capped, List<String> examples, String unknown) {
        static Precheck clean() {
            return new Precheck(0, false, List.of(), null);
        }

        static Precheck unknown(String reason) {
            return new Precheck(0, false, List.of(), reason);
        }

        /** The word a step reports: clean, bad_rows, unavailable, timeout or failed. */
        String state() {
            if (bad > 0) return "bad_rows";
            if (unknown == null) return "clean";
            return switch (unknown) {
                case "precheck_timeout" -> "timeout";
                case "precheck_failed" -> "failed";
                default -> "unavailable";
            };
        }
    }

    /**
     * Counts the rows of {@code relation} whose chash is not 32 bytes, as every tenant's rows, under {@code
     * precheck_timeout_ms} and an ACCESS SHARE lock, or says it cannot. It reads only where this session already reads every
     * row ({@link AdminDdlSession#requireUnrestrictedView}); it never lifts FORCE row-level security and never takes a lock
     * stronger than ACCESS SHARE. A session that cannot see every row is {@code unavailable} (the answer would be a false
     * zero), a scan that outlasts the bound is {@code timeout}, and a lock that is not available is a {@link Transient}.
     */
    private Precheck precheck(AdminDdlSession s, String schema, String relation, Config cfg) {
        try {
            s.requireUnrestrictedView(PRECHECK_VIEW);
        } catch (AdminDdlSession.Refused e) {
            if ("admin_cannot_see_all_tenants".equals(e.reason())) return Precheck.unknown("precheck_unavailable");
            throw e;
        } catch (DataAccessException e) {
            // the policy and role catalogs could not be read: the pre-check cannot say, and the VALIDATE decides
            if (SQLSTATE_LOCK_NOT_AVAILABLE.equals(e.sqlState())) throw new Transient("lock_timeout");
            if (SQLSTATE_UNDEFINED_TABLE.equals(e.sqlState())) throw e;
            return Precheck.unknown("precheck_failed");
        }
        s.setSession("lock_timeout", cfg.lockTimeoutMs() + "ms");
        s.setSession("statement_timeout", cfg.precheckTimeoutMs() + "ms");
        try {
            return countBad(s.ctx(), schema, relation, cfg.precheckCap());
        } catch (DataAccessException e) {
            if (SQLSTATE_QUERY_CANCELED.equals(e.sqlState())) return Precheck.unknown("precheck_timeout");
            if (SQLSTATE_LOCK_NOT_AVAILABLE.equals(e.sqlState())) throw new Transient("lock_timeout");
            throw e;
        }
    }

    /** The pre-check on the failure path of a VALIDATE: any bound it meets means no count, never another failure. */
    private Precheck precheckOrUnknown(AdminDdlSession s, String schema, String relation, Config cfg) {
        try {
            return precheck(s, schema, relation, cfg);
        } catch (Transient | AdminDdlSession.Refused | DataAccessException e) {
            return Precheck.unknown("precheck_failed");
        }
    }

    private static Precheck countBad(DSLContext ctx, String schema, String relation, int cap) {
        Table<?> rel = DSL.table(DSL.name(schema, relation));
        Field<byte[]> chash = DSL.field(DSL.name("chash"), SQLDataType.VARBINARY);
        // a NULL width is NULL, not true: the CHECK passes a NULL, so this counts what the CHECK would refuse
        Condition bad = DSL.function("octet_length", SQLDataType.INTEGER, chash).ne(32);
        // one row more than the cap is read, so the cap is reported only when there really is more
        long found = ctx.selectCount().from(ctx.selectOne().from(rel).where(bad).limit(cap + 1L)).fetchOne(0, Long.class);
        if (found == 0) return Precheck.clean();
        long n = Math.min(found, cap);
        boolean over = found > cap;
        Field<byte[]> head = DSL.function("substring", SQLDataType.VARBINARY, chash, DSL.val(1), DSL.val(EXAMPLE_BYTES));
        Field<String> hex = DSL.function("encode", SQLDataType.VARCHAR, head, DSL.inline("hex"));
        List<String> examples = ctx.select(hex).from(rel).where(bad).limit(Math.min(EXAMPLES, cap)).fetch(hex);
        return new Precheck(n, over, List.copyOf(examples), null);
    }

    // ---- small reads -----------------------------------------------------------------------------------------------

    /** {@code pg_total_relation_size} of the relation (heap, TOAST and indexes), or null when it could not be read. */
    private static Long sizeOf(DSLContext ctx, long relation) {
        try {
            var cls = DSL.table(DSL.name("pg_catalog", "pg_class")).as("c");
            Field<Long> oid = DSL.field(DSL.name("c", "oid")).cast(SQLDataType.BIGINT);
            return ctx.select(DSL.function("pg_total_relation_size", SQLDataType.BIGINT, DSL.field(DSL.name("c", "oid"))))
                .from(cls).where(oid.eq(relation)).fetchOne(0, Long.class);
        } catch (DataAccessException e) {
            return null;
        }
    }

    /** The slice a sleep is cut into, so a cancel (which does not interrupt the thread) is seen within it. */
    private static final long SLEEP_SLICE_MS = 50;

    /**
     * Sleeps; false when interrupted (the engine is shutting down, the flag set again) or when {@code listener} has a stop
     * reason (a cancel), read between slices of {@link #SLEEP_SLICE_MS}. A null listener is the interrupt alone.
     */
    private static boolean sleep(long ms, Listener listener) {
        long end = System.nanoTime() + java.util.concurrent.TimeUnit.MILLISECONDS.toNanos(Math.max(0, ms));
        while (true) {
            if (Thread.currentThread().isInterrupted() || (listener != null && listener.stopReason() != null)) return false;
            long left = java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(end - System.nanoTime());
            if (left <= 0) return true;
            try {
                Thread.sleep(Math.min(left, SLEEP_SLICE_MS));
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return false;
            }
        }
    }
}
