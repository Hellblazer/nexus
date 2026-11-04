// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TokenStore;
import dev.nexus.service.vectors.ChunkConstraintAdmin;
import dev.nexus.service.vectors.ChunkKeyPlan;
import dev.nexus.service.vectors.ChunkKeySweep;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.function.Supplier;

/**
 * RDR-236 Phase 4 Step 3 (nexus-6pfgb.43): the three engine-admin routes that retire the chunk-row metadata keys.
 * Under {@code /v1/admin/chunk-keys}, all POST, none called by anything on its own: an operator or a client verb
 * ({@code nx t3 retire-keys}) sends them, and the upgrade-ladder rung that would run the sweep on every install is a
 * later bead that ships only after a cloud dry run has been read.
 *
 * <pre>
 *   POST /v1/admin/chunk-keys/sweep               the CALLER's tenant, or the tenant a root credential names;
 *                                                 dry run by default
 *   POST /v1/admin/chunk-keys/add-constraints     table-wide, operator (root token) only
 *   POST /v1/admin/chunk-keys/validate-constraints table-wide, operator only
 * </pre>
 *
 * <p><b>Common request fields.</b> {@code allowed_keys} (the CHUNK and ROW keys), {@code bypass_prefixes} (the
 * collection prefixes the allow-list does not apply to) and {@code quarantine_implies} ({@code [by, at]}) are rendered
 * by the client from the Python key table; the engine pins each of them ({@link ChunkKeyPlan#of}): the exact ROW key
 * set by digest, exactly the quarantine and taxonomy prefixes, the implication pair. A list that differs is 400.
 *
 * <p><b>Sweep.</b> {@code dry_run} defaults to <b>true</b>: a request that does not say {@code "dry_run": false} writes
 * nothing, takes no row lock and runs no UPDATE, not even one that is rolled back. It answers per-collection counts (rows
 * with a retired key, rows left alone for a wrong-width chash, the collection's census verdict), the preconditions, the
 * breakdown of {@code generated_nulls} (by content type, by missing key) and {@code null_span_rows} (by column), and the
 * time of the batch SELECT on up to three collections ({@code measure_batch: false} skips it); the projection built from
 * that is a lower bound, since the update half of a batch is not measured. Apply sweeps up to {@code max_batches}
 * batches of {@code batch_size} rows (at most 1000) or {@code budget_seconds}, whichever ends first, in keyset order, and
 * answers {@code done}; the caller loops while it is false. The first call of a run checks the preconditions and
 * answers 409 {@code sweep_preconditions_not_met} with the counts when one stands in the way. The residues in {@link
 * ChunkKeySweep#ACCEPTABLE} may be accepted by naming each with the number of rows accepted, {@code "accept":
 * {"keyless_notes": 3}}: a live count above that number still blocks. A call that carries {@code resume} (the object the
 * previous call returned, signature included) does not repeat the checks or the census, and its time budget starts at
 * once; a {@code resume} whose signature does not verify (edited, another tenant's, or from before an engine restart)
 * is ignored: the answer says {@code resume_ignored: true} and the call ran as a first call, checks and all. The sweep never
 * touches the bypass collections or a row whose chash is not 32 bytes, skips a collection whose census reads {@code
 * legacy-unmanifested} or could not be read, skips a row whose metadata is not an object, and does not set {@code
 * last_written_at}. A run is {@code done} only when a pass from the start finds nothing it can take and nothing is left;
 * rows left by a lock are reported as {@code locked_skipped_rows} and {@code done} stays false.
 * DATA EFFECT: the metadata of every swept row loses its retired keys; nothing else on the row changes.
 *
 * <p><b>Add and validate</b> are table-wide. Both begin with the probe ({@link ChunkConstraintAdmin#probe}): one pass over
 * {@code nexus.chunks} on the admin session, so a tenant with chunk rows and no {@code service_tokens} row is checked
 * too (the checked set is the default tenant, every known tenant, and every tenant the probe found, named in the
 * response). The probe is only as good as the session: it answers 503 {@code admin_cannot_see_all_tenants} when the admin
 * role is row filtered (neither superuser nor BYPASSRLS, and no permissive true-qual SELECT policy applies), and 503
 * {@code admin_probe_undercount} when its row count for a known tenant is below that tenant's scoped count. {@code add} refuses, naming the tenant, while a tenant has rows the sweep can still
 * take, a {@code chunk_text_hash} or {@code embedding_model} that differs from the column, a chash that is not 32 bytes,
 * a half-stamped quarantine row, or retired-key rows the sweep's own view does not account for. {@code validate} refuses
 * while any tenant holds any of those or any retired key, blocked collections included. They run on the admin connection
 * ({@link ChunkConstraintAdmin}); an engine without one answers 503.
 *
 * <p>DATA EFFECT of add: a write carrying a key outside the allow-list is then refused (SQLSTATE 23514), and so is ANY
 * UPDATE of a row that still holds a retired key, because a NOT VALID CHECK is evaluated on every new row version: the
 * rows the sweep leaves behind (a blocked collection) are frozen until fixed. The response lists them per tenant under
 * {@code left_behind}. Validate changes no data.
 *
 * <p><b>Naming a target tenant, and the audit.</b> A request may carry {@code "tenant": "<id>"}. A tenant credential
 * sweeps its own tenant (naming it is the same as naming none); naming ANOTHER tenant is 403 {@code tenant_mismatch}.
 * A data or mint credential is refused by the scope guard (403, an error text, no reason code) whatever it names. The
 * root credential ({@link RequestContext#isOperator()}, the check the table-wide routes use) may name another tenant:
 * the engine then requires it to be registered (the default tenant; a tenant that holds a {@code service_tokens} row,
 * revoked or expired rows included; or a tenant that holds chunk rows even with no token row; else 422
 * {@code unknown_tenant}, and the wildcard, a name with a control character, and a case or whitespace variant of a real
 * tenant never match), and runs the dry run or the apply in that tenant's scope exactly as a tenant credential would
 * (the tenant GUC is set per batch, so RLS applies the same way).
 *
 * <p><b>Every sweep a root credential makes is audited, whether or not it names a tenant, and naming its own tenant
 * included.</b> Two {@code gc_audit} rows (operation {@code retire_keys_sweep}), both in the TARGET tenant: an
 * <i>intent</i> row ({@code phase: started}: mode, target and caller tenant, the actor, the accept map, batch size,
 * whether the call resumed, and the optional {@code "operator"} label) written BEFORE the first read or batch, and an
 * <i>outcome</i> row ({@code finished}, {@code refused} for a 409 preconditions refusal, or {@code failed}) with the
 * counts, in a finally path, so a call that throws in batch N records the rows batches 1..N-1 committed. The actor is a
 * prefix of the credential's hash, never the secret and never the whole hash. Dry run and apply both fail closed: no
 * audit sink is 503 {@code audit_unavailable}; an intent row that cannot be written is 500
 * {@code audit_intent_failed}, in both cases before any read or change; an outcome row that cannot be written after the
 * work is 500 {@code audit_write_failed} with this call's counts, {@code rows_changed} and the continuation; a failure
 * inside the work is 500 {@code sweep_failed} with the partial counts. The answer carries {@code audit}
 * ({@code actor}, {@code intent_id}, {@code outcome_id}). A tenant credential's sweep is not audited. The resume token
 * is signed over the target tenant. The answer's {@code tenant} is the target; a client that asked for a tenant and reads
 * another in the answer is talking to an engine that predates this field.
 *
 * <p><b>Authorization.</b> {@link AuthFilter} has resolved the bearer; the sweep needs a root or tenant credential (a
 * data or mint credential is 403), the table-wide routes need {@link RequestContext#isOperator()}.
 */
public final class ChunkKeysAdminHandler implements HttpHandler {

    public static final String PATH = "/v1/admin/chunk-keys";

    private static final Logger log = LoggerFactory.getLogger(ChunkKeysAdminHandler.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() { };

    static final int DEFAULT_MAX_BATCHES = 25;
    static final int MAX_MAX_BATCHES = 200;
    static final int DEFAULT_BUDGET_SECONDS = 30;
    static final int MAX_BUDGET_SECONDS = 120;
    static final int MAX_OPERATOR_LABEL = 80;

    private final ChunkKeySweep sweep;
    private final Supplier<ChunkConstraintAdmin> constraints;
    private final Supplier<List<String>> tenants;
    private final Supplier<Audit> auditSink;

    /** Where a root-targeted sweep call is recorded: {@code CatalogRepository#recordGcAudit} in the engine. */
    @FunctionalInterface
    public interface Audit {
        long record(String tenant, Map<String, Object> row);
    }

    /** The gc_audit operation of a root-targeted sweep call. */
    public static final String AUDIT_OPERATION = "retire_keys_sweep";

    /**
     * @param sweep       the sweep and its counts
     * @param constraints the admin-connection DDL, bound late; null (or a supplier answering null) is 503 on the
     *                    table-wide routes
     * @param tenants     every tenant that holds a {@code service_tokens} row, revoked or expired ones included
     *                    ({@code TokenStore#listKnownTenants}); the sweep route also accepts a tenant that holds chunk
     *                    rows with no token row, and the table-wide routes add the tenants the probe finds
     */
    public ChunkKeysAdminHandler(ChunkKeySweep sweep, Supplier<ChunkConstraintAdmin> constraints,
                                 Supplier<List<String>> tenants) {
        this(sweep, constraints, tenants, (Supplier<Audit>) () -> null);
    }

    /** As above, with the audit sink a root credential's sweep is recorded through (null: refused, 503). */
    public ChunkKeysAdminHandler(ChunkKeySweep sweep, Supplier<ChunkConstraintAdmin> constraints,
                                 Supplier<List<String>> tenants, Audit audit) {
        this(sweep, constraints, tenants, (Supplier<Audit>) () -> audit);
    }

    /** As above, the sink read per request (it may be bound late or replaced; a supplier answering null: 503). */
    public ChunkKeysAdminHandler(ChunkKeySweep sweep, Supplier<ChunkConstraintAdmin> constraints,
                                 Supplier<List<String>> tenants, Supplier<Audit> auditSink) {
        this.sweep = sweep;
        this.constraints = constraints;
        this.tenants = tenants;
        this.auditSink = auditSink;
    }

    @Override
    public void handle(HttpExchange ex) throws IOException {
        String tenant = RequestContext.tenant();
        if (tenant == null) {
            HttpUtil.send(ex, 500, "{\"error\":\"internal: tenant not set\"}");
            return;
        }
        String op = ex.getRequestURI().getPath().replaceFirst("^" + PATH, "");
        if (sweep == null) {
            HttpUtil.send(ex, 503, "{\"error\":\"this engine has no pgvector repository: the chunk-key routes are unavailable\"}");
            return;
        }
        if (!"POST".equals(ex.getRequestMethod().toUpperCase(Locale.ROOT))) {
            HttpUtil.send(ex, 405, "{\"error\":\"method not allowed\"}");
            return;
        }
        try {
            switch (op) {
                case "/sweep" -> handleSweep(ex, tenant);
                case "/add-constraints" -> handleConstraints(ex, true);
                case "/validate-constraints" -> handleConstraints(ex, false);
                default -> HttpUtil.send(ex, 404, "{\"error\":\"not found\"}");
            }
        } catch (IllegalArgumentException | com.fasterxml.jackson.core.JsonProcessingException e) {
            HttpUtil.send(ex, 400, json(Map.of("error", String.valueOf(e.getMessage()))));
        } catch (ChunkConstraintAdmin.Refused e) {
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("error", e.getMessage());
            body.put("reason", e.reason());
            HttpUtil.send(ex, e.status(), json(body));
        } catch (ChunkKeySweep.PreconditionRefused e) {
            HttpUtil.send(ex, 409, json(preconditionBody(e)));
        } catch (Exception e) {
            if (!HttpUtil.sendTypedDbError(ex, e, log, "chunk_keys_admin", "op=" + op + " tenant=" + tenant)) {
                log.error("event=chunk_keys_admin_error op={} tenant={} error={}", op, tenant, e.getMessage(), e);
                HttpUtil.send(ex, 500, "{\"error\":\"internal server error\"}");
            }
        }
    }

    // ---- sweep --------------------------------------------------------------------------------------------------

    private void handleSweep(HttpExchange ex, String callerTenant) throws IOException {
        String scopeName = RequestContext.scope();
        if (!TokenStore.SCOPE_ROOT.equals(scopeName) && !TokenStore.SCOPE_TENANT.equals(scopeName)) {
            // A data or mint credential: the scope guard, whatever tenant the body names (no reason code).
            HttpUtil.send(ex, 403, json(Map.of("error",
                "the sweep needs a tenant or root credential, not scope " + scopeName)));
            return;
        }
        Map<String, Object> body = readBody(ex);
        String named = namedTenant(body);
        boolean root = RequestContext.isOperator();
        if (named != null && !named.equals(callerTenant) && !root) {
            // Only a tenant-scope credential reaches here. Not a hint about whether the tenant exists: it learns
            // nothing about the others.
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("error", "this credential is bound to tenant " + callerTenant + " and may not sweep another "
                + "tenant: only the root credential names a target tenant");
            out.put("reason", "tenant_mismatch");
            log.warn("event=retire_keys_target_refused caller_tenant={} scope={} reason=tenant_mismatch",
                callerTenant, scopeName);
            HttpUtil.send(ex, 403, json(out));
            return;
        }
        String tenant = named == null ? callerTenant : named;
        if (root && !tenant.equals(callerTenant) && !registered(tenant)) {
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("error", "tenant " + tenant + " is not registered on this engine (it holds no credential and "
                + "no chunk row, and is not the default tenant): nothing was swept");
            out.put("reason", "unknown_tenant");
            HttpUtil.send(ex, 422, json(out));
            return;
        }
        // Every sweep a root credential makes is recorded, whether or not the request names a tenant.
        Audit audit = auditSink.get();
        if (root && audit == null) {
            HttpUtil.send(ex, 503, json(Map.of("error",
                "this engine has no audit sink for a root-credential sweep; nothing was read or swept",
                "reason", "audit_unavailable")));
            return;
        }
        ChunkKeyPlan plan = plan(body);
        boolean dryRun = !Boolean.FALSE.equals(body.get("dry_run"));
        Map<String, Long> accepted = accepted(body);
        int batchSize = intField(body, "batch_size", ChunkKeySweep.DEFAULT_BATCH, 1, ChunkKeySweep.MAX_BATCH);
        String only = body.get("collection") instanceof String s && !s.isBlank() ? s : null;
        String operatorLabel = operatorLabel(body);
        int maxBatches = dryRun ? 0 : intField(body, "max_batches", DEFAULT_MAX_BATCHES, 1, MAX_MAX_BATCHES);
        int budget = dryRun ? 0 : intField(body, "budget_seconds", DEFAULT_BUDGET_SECONDS, 1, MAX_BUDGET_SECONDS);
        ChunkKeySweep.Resume resume = dryRun ? null : resume(body);

        // The intent row goes in BEFORE the first read or batch: a sink that cannot be written refuses the call with
        // nothing read and nothing changed, instead of being found out after the rows are swept.
        Trail trail = null;
        if (root) {
            trail = new Trail(audit, tenant, callerTenant, only, dryRun, accepted, batchSize, resume != null, operatorLabel);
            try {
                trail.intent();
            } catch (RuntimeException e) {
                log.error("event=retire_keys_audit_intent_failed tenant={} caller_tenant={} error={}", tenant,
                    callerTenant, e.getMessage(), e);
                HttpUtil.send(ex, 500, json(Map.of("error",
                    "the audit row for this sweep could not be written; nothing was read or changed",
                    "reason", "audit_intent_failed")));
                return;
            }
        }

        Map<String, Object> counts = new LinkedHashMap<>();
        Map<String, Object> out = null;
        ChunkKeySweep.Progress progress = new ChunkKeySweep.Progress();
        RuntimeException failure = null;
        boolean completed = false;
        try {
            if (dryRun) {
                boolean measure = !Boolean.FALSE.equals(body.get("measure_batch"));
                var report = sweep.dryRun(tenant, plan, accepted, batchSize, measure);
                log.info("event=retire_keys_dry_run tenant={} caller_tenant={} sweepable={} blocked={} blocking={}",
                    tenant, callerTenant, report.sweepableRows(), report.blockedRows(), report.blocking());
                counts.put("retired_key_rows", report.retiredRows());
                counts.put("sweepable_rows", report.sweepableRows());
                counts.put("blocked_rows", report.blockedRows());
                counts.put("wrong_width_rows", report.wrongWidthRows());
                counts.put("blocking", report.blocking());
                out = dryRunBody(report);
                out.put("dry_run", true);
            } else {
                var applied = sweep.apply(tenant, plan, accepted, only, batchSize, maxBatches,
                    Duration.ofSeconds(budget), resume, progress);
                counts.put("rows_swept", applied.rowsSwept());
                counts.put("batches", applied.batches());
                counts.put("done", applied.done());
                counts.put("remaining_rows", applied.remainingRows());
                counts.put("locked_skipped_rows", applied.lockedSkippedRows());
                counts.put("blocked_collections", applied.blocked().size());
                counts.put("resumed", resume != null && !applied.resumeIgnored());
                counts.put("resume_ignored", applied.resumeIgnored());
                out = new LinkedHashMap<>();
                out.put("tenant", applied.tenant());
                out.put("dry_run", false);
                out.put("rows_swept", applied.rowsSwept());
                out.put("batches", applied.batches());
                out.put("done", applied.done());
                out.put("remaining_rows", applied.remainingRows());
                out.put("locked_skipped_rows", applied.lockedSkippedRows());
                out.put("first_batch_ms", applied.firstBatchMs());
                out.put("elapsed_ms", applied.totalMs());
                out.put("blocked_collections", blockedList(applied.blocked()));
                out.put("resume", applied.resume() == null ? null : resumeBody(applied.resume()));
                out.put("resume_ignored", applied.resumeIgnored());
            }
            completed = true;
        } catch (RuntimeException e) {
            failure = e;
        } finally {
            if (trail != null) {
                if (failure instanceof ChunkKeySweep.PreconditionRefused refused) {
                    counts.put("blocking", refused.report().blocking());
                }
                if ((failure != null || !completed) && !dryRun) {
                    counts.put("rows_swept", progress.rowsSwept());
                    counts.put("batches", progress.batches());
                }
                // Never throws: a failure to write the outcome is reported to the caller, it does not hide the work.
                // An Error that skips the catch leaves failure null and completed false: still a failed call.
                trail.outcome(counts, failure != null || completed ? failure
                    : new IllegalStateException("the sweep did not complete"));
            }
        }

        if (failure != null) {
            if (failure instanceof ChunkKeySweep.PreconditionRefused refused) {
                Map<String, Object> refusal = preconditionBody(refused);
                if (trail != null) {
                    refusal.put("audit", trail.body());
                }
                HttpUtil.send(ex, 409, json(refusal));
                return;
            }
            if (trail == null || failure instanceof IllegalArgumentException) {
                throw failure;   // unchanged mapping for the credentials that are not audited
            }
            log.error("event=retire_keys_sweep_failed tenant={} caller_tenant={} rows_swept={} batches={} error={}",
                tenant, callerTenant, progress.rowsSwept(), progress.batches(), failure.getMessage(), failure);
            Map<String, Object> err = new LinkedHashMap<>();
            err.put("tenant", tenant);
            err.put("error", "the sweep failed after " + progress.rowsSwept() + " rows in " + progress.batches()
                + " batches: " + failure.getClass().getSimpleName() + "; the rows counted were committed and are "
                + "not undone; re-run the same command, the sweep takes up where the rows left are");
            err.put("reason", "sweep_failed");
            err.put("dry_run", dryRun);
            err.put("rows_swept", progress.rowsSwept());
            err.put("batches", progress.batches());
            err.put("rows_changed", progress.rowsSwept() > 0);
            err.put("audit", trail.body());
            HttpUtil.send(ex, 500, json(err));
            return;
        }
        if (trail != null) {
            out.put("audit", trail.body());
            if (trail.outcomeFailed) {
                // The work is done and cannot be taken back: say so, with this call's counts and the continuation.
                out.put("error", "the sweep ran but its outcome audit row could not be written; this call's counts "
                    + "are in this answer");
                out.put("reason", "audit_write_failed");
                out.put("rows_changed", !dryRun && ((Number) counts.getOrDefault("rows_swept", 0L)).longValue() > 0);
                HttpUtil.send(ex, 500, json(out));
                return;
            }
        }
        HttpUtil.send(ex, 200, json(out));
    }

    private static Map<String, Object> preconditionBody(ChunkKeySweep.PreconditionRefused e) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("error", e.getMessage());
        body.put("reason", "sweep_preconditions_not_met");
        body.put("blocking", e.report().blocking());
        body.put("report", dryRunBody(e.report()));
        return body;
    }

    /** The {@code tenant} field of the request: absent is null; present it must be a non-blank string. */
    private static String namedTenant(Map<String, Object> body) {
        Object v = body.get("tenant");
        if (v == null) {
            return null;
        }
        if (!(v instanceof String s) || s.isBlank()) {
            throw new IllegalArgumentException("'tenant' must be a non-blank string naming the tenant to sweep");
        }
        return s;
    }

    /** The optional {@code operator} label: who the person behind the shared root credential is. */
    private static String operatorLabel(Map<String, Object> body) {
        Object v = body.get("operator");
        if (v == null) {
            return null;
        }
        if (!(v instanceof String s) || s.isBlank() || s.length() > MAX_OPERATOR_LABEL
                || s.chars().anyMatch(Character::isISOControl)) {
            throw new IllegalArgumentException("'operator' must be a printable string of at most "
                + MAX_OPERATOR_LABEL + " characters naming who runs the sweep");
        }
        return s.strip();
    }

    /**
     * The tenants a root credential may target: the default tenant, a tenant that holds a {@code service_tokens} row
     * (a revoked or expired row still counts), or a tenant that holds chunk rows without one (a data-only tenant
     * whose tokens aged out, e.g.). A name with a control character is never registered; the wildcard is refused.
     * The chunk-row test is the tenant-scoped count the table-wide routes already use ({@link ChunkKeySweep#rowCount}),
     * exact on the tenant string, so a case or whitespace variant of a real tenant holds none.
     */
    private boolean registered(String tenant) {
        if (TenantConstants.BOOTSTRAP_ANY_TENANT.equals(tenant) || tenant.chars().anyMatch(Character::isISOControl)) {
            return false;
        }
        return TenantConstants.DEFAULT_TENANT.equals(tenant) || tenants.get().contains(tenant)
            || sweep.rowCount(tenant) > 0;
    }

    /**
     * The gc_audit rows of one root sweep call, both in the TARGET tenant: an intent row before the first read
     * ({@code phase: started}, the request: mode, accept map, batch size, resumed, operator label) and an outcome row
     * after the work ({@code finished}, {@code refused} or {@code failed}, with the counts; a failed call carries the
     * partial counts of the batches that did commit). The actor is a prefix of the credential's hash, never the secret
     * and never the whole hash; one shared root token names no person, hence the optional {@code operator} label.
     */
    private final class Trail {
        private final Audit audit;
        private final String target;
        private final String callerTenant;
        private final String collection;
        private final boolean dryRun;
        private final Map<String, Long> accepted;
        private final int batchSize;
        private final boolean resumed;
        private final String operatorLabel;
        private final String actor;
        private long intentId = -1;
        private long outcomeId = -1;
        boolean outcomeFailed;

        Trail(Audit audit, String target, String callerTenant, String collection, boolean dryRun, Map<String, Long> accepted,
              int batchSize, boolean resumed, String operatorLabel) {
            this.audit = audit;
            this.target = target;
            this.callerTenant = callerTenant;
            this.collection = collection;
            this.dryRun = dryRun;
            this.accepted = accepted;
            this.batchSize = batchSize;
            this.resumed = resumed;
            this.operatorLabel = operatorLabel;
            String hash = RequestContext.credentialHash();
            this.actor = "root:" + (hash == null ? "unknown" : hash.substring(0, Math.min(16, hash.length())));
        }

        void intent() {
            Map<String, Object> details = details("started");
            details.put("accept", new LinkedHashMap<>(accepted));
            details.put("batch_size", batchSize);
            details.put("resumed", resumed);
            if (operatorLabel != null) {
                details.put("operator", operatorLabel);
            }
            intentId = write(details);
        }

        /** Never throws; {@link #outcomeFailed} says whether the row went in. */
        void outcome(Map<String, Object> counts, RuntimeException failure) {
            String phase = failure == null ? "finished"
                : failure instanceof ChunkKeySweep.PreconditionRefused ? "refused" : "failed";
            Map<String, Object> details = details(phase);
            details.put("intent_audit_id", intentId);
            details.put("counts", counts);
            if (failure != null) {
                // The class and a code, never the message: it can carry SQL or row detail a tenant credential can read
                // back through gc_audit/list; the full text is in the engine log.
                details.put("failure", failure.getClass().getSimpleName());
            }
            if (operatorLabel != null) {
                details.put("operator", operatorLabel);
            }
            try {
                outcomeId = write(details);
            } catch (RuntimeException e) {
                outcomeFailed = true;
                log.error("event=retire_keys_audit_failed tenant={} caller_tenant={} phase={} intent_id={} counts={} "
                        + "error={}", target, callerTenant, phase, intentId, counts, e.getMessage(), e);
            }
        }

        Map<String, Object> body() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("actor", actor);
            m.put("tenant", target);
            m.put("intent_id", intentId);
            m.put("outcome_id", outcomeFailed ? null : outcomeId);
            return m;
        }

        private Map<String, Object> details(String phase) {
            Map<String, Object> details = new LinkedHashMap<>();
            details.put("phase", phase);
            details.put("mode", dryRun ? "dry-run" : "apply");
            details.put("target_tenant", target);
            details.put("caller_tenant", callerTenant);
            details.put("scope", RequestContext.scope());
            return details;
        }

        private long write(Map<String, Object> details) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("operation", AUDIT_OPERATION);
            row.put("collection", collection == null ? "" : collection);
            row.put("actor", actor);
            row.put("dry_run", dryRun);
            row.put("details", details);
            return audit.record(target, row);
        }
    }

    static Map<String, Object> dryRunBody(ChunkKeySweep.DryRun r) {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("tenant", r.tenant());
        out.put("collections", collections(r.collections()));
        Map<String, Object> totals = new LinkedHashMap<>();
        totals.put("retired_key_rows", r.retiredRows());
        totals.put("sweepable_rows", r.sweepableRows());
        totals.put("blocked_rows", r.blockedRows());
        totals.put("wrong_width_rows", r.wrongWidthRows());
        out.put("totals", totals);
        out.put("preconditions", new LinkedHashMap<>(r.preconditions()));
        out.put("precondition_details", r.details());
        out.put("acceptable_residues", ChunkKeySweep.ACCEPTABLE);
        out.put("accepted", new LinkedHashMap<>(r.accepted()));
        List<Map<String, Object>> blockingDetail = new ArrayList<>();
        for (String name : r.blocking()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("name", name);
            m.put("count", r.preconditions().getOrDefault(name, 0L));
            m.put("accepted", r.accepted().get(name));
            blockingDetail.add(m);
        }
        out.put("blocking", r.blocking());
        out.put("blocking_detail", blockingDetail);
        out.put("applicable", r.blocking().isEmpty());
        out.put("batch_size", r.batchSize());
        if (r.samples().isEmpty()) {
            out.put("batch_cost", null);
        } else {
            Map<String, Object> cost = new LinkedHashMap<>();
            List<Map<String, Object>> samples = new ArrayList<>();
            long selectTotal = 0;
            for (var s : r.samples()) {
                Map<String, Object> m = new LinkedHashMap<>();
                m.put("collection", s.collection());
                m.put("rows", s.rows());
                m.put("select_ms", s.selectMs());
                samples.add(m);
                selectTotal += s.selectMs();
            }
            long meanSelectMs = selectTotal / r.samples().size();
            long batches = r.batchSize() == 0 ? 0 : (r.sweepableRows() + r.batchSize() - 1) / r.batchSize();
            cost.put("samples", samples);
            cost.put("select_ms", meanSelectMs);
            cost.put("update_ms", null);
            cost.put("measures", "the batch SELECT only; nothing was locked, updated or rolled back");
            cost.put("projected_seconds", batches * meanSelectMs / 1000.0);
            cost.put("projection_is_lower_bound", true);
            out.put("batch_cost", cost);
        }
        return out;
    }

    private static List<Map<String, Object>> collections(List<ChunkKeySweep.CollectionCount> cs) {
        List<Map<String, Object>> out = new ArrayList<>();
        for (var c : cs) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("collection", c.collection());
            m.put("retired_key_rows", c.retiredRows());
            m.put("wrong_width_rows", c.wrongWidthRows());
            m.put("sweepable_rows", c.sweepableRows());
            m.put("legacy_unmanifested", c.legacyUnmanifested());
            m.put("blocked", c.blockedReason());
            out.add(m);
        }
        return out;
    }

    private static List<Map<String, Object>> blockedList(List<ChunkKeySweep.Blocked> blocked) {
        List<Map<String, Object>> out = new ArrayList<>();
        for (var b : blocked) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("collection", b.collection());
            m.put("blocked", b.reason());
            m.put("retired_key_rows", b.retiredRows());
            out.add(m);
        }
        return out;
    }

    private static Map<String, Object> resumeBody(ChunkKeySweep.Resume r) {
        Map<String, Object> m = new LinkedHashMap<>();
        if (r.cursor() == null) {
            m.put("cursor", null);
        } else {
            Map<String, Object> c = new LinkedHashMap<>();
            c.put("collection", r.cursor().collection());
            c.put("chash", r.cursor().chash());
            m.put("cursor", c);
        }
        m.put("collections", r.collections());
        m.put("blocked", blockedList(r.blocked()));
        m.put("remaining", r.remaining());
        m.put("sig", r.sig());
        return m;
    }

    // ---- table-wide ---------------------------------------------------------------------------------------------

    private void handleConstraints(HttpExchange ex, boolean add) throws IOException {
        if (!RequestContext.isOperator()) {
            HttpUtil.send(ex, 403, json(Map.of("error",
                "operator privilege required: the constraint routes need the root token")));
            return;
        }
        ChunkConstraintAdmin admin = constraints == null ? null : constraints.get();
        if (admin == null) {
            HttpUtil.send(ex, 503, json(Map.of("error",
                "this engine has no admin connection bound for the chunk constraints")));
            return;
        }
        Map<String, Object> body = readBody(ex);
        ChunkKeyPlan plan = plan(body);

        // The authoritative look: every tenant with chunk rows, read on the admin session before any DDL. The probe
        // refuses a session that cannot see every row; and as a second line, a tenant-scoped count of each tenant the
        // engine knows is taken FIRST (a concurrent insert then only makes the probe larger) and the probe may not be
        // below it for any of them.
        Set<String> checked = new TreeSet<>();
        checked.add(TenantConstants.DEFAULT_TENANT);
        checked.addAll(tenants.get());
        Map<String, Long> scoped = new LinkedHashMap<>();
        for (String t : checked) {
            scoped.put(t, sweep.rowCount(t));
        }
        List<ChunkConstraintAdmin.TenantProbe> probe = admin.probe(plan);
        List<String> undercounted = undercounted(probe, scoped);
        if (!undercounted.isEmpty()) {
            throw new ChunkConstraintAdmin.Refused(503, "admin_probe_undercount",
                "the table-wide probe counted fewer chunk rows than a tenant-scoped count for tenant(s) "
                    + undercounted + ": the admin session is not reading every row, so no constraint check can be "
                    + "trusted. Nothing was changed; if rows were deleted while this ran, retry");
        }
        long rowsExamined = 0;
        for (var p : probe) {
            checked.add(p.tenant());
            rowsExamined += p.rows();
        }

        List<Map<String, Object>> offenders = new ArrayList<>();
        List<Map<String, Object>> leftBehind = new ArrayList<>();
        boolean anyUnswept = false;
        for (var p : probe) {
            Map<String, Object> row = new LinkedHashMap<>();
            if (add) {
                if (p.retiredRows() > 0) {
                    var cols = sweep.collectionsOf(p.tenant(), plan);
                    long seen = cols.stream().mapToLong(ChunkKeySweep.CollectionCount::retiredRows).sum();
                    long sweepable = cols.stream().mapToLong(ChunkKeySweep.CollectionCount::sweepableRows).sum();
                    if (sweepable > 0) {
                        row.put("unswept_rows", sweepable);
                        anyUnswept = true;
                    }
                    if (seen != p.retiredRows()) {
                        // The probe and the tenant-scoped view disagree: rows the sweep cannot account for.
                        row.put("unaccounted_rows", Math.abs(p.retiredRows() - seen));
                    }
                    List<Map<String, Object>> blocked = new ArrayList<>();
                    for (var c : cols) {
                        if (c.blocked()) {
                            Map<String, Object> m = new LinkedHashMap<>();
                            m.put("collection", c.collection());
                            m.put("retired_key_rows", c.retiredRows() - c.wrongWidthRows());
                            m.put("blocked", c.blockedReason());
                            blocked.add(m);
                        }
                    }
                    if (!blocked.isEmpty()) {
                        Map<String, Object> lb = new LinkedHashMap<>();
                        lb.put("tenant", p.tenant());
                        lb.put("blocked_collections", blocked);
                        leftBehind.add(lb);
                    }
                }
            } else if (p.retiredRows() > 0) {
                row.put("retired_key_rows", p.retiredRows());
            }
            if (p.hashMismatch() > 0) {
                row.put("chunk_text_hash_mismatch", p.hashMismatch());
            }
            if (p.modelMismatch() > 0) {
                row.put("embedding_model_mismatch", p.modelMismatch());
            }
            if (p.wrongWidth() > 0) {
                row.put("wrong_width_rows", p.wrongWidth());
            }
            if (p.quarantineBreach() > 0) {
                row.put("quarantine_implication_violations", p.quarantineBreach());
            }
            if (!row.isEmpty()) {
                Map<String, Object> named = new LinkedHashMap<>();
                named.put("tenant", p.tenant());
                named.putAll(row);
                offenders.add(named);
            }
        }
        if (!offenders.isEmpty()) {
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("error", "refused: tenant(s) "
                + offenders.stream().map(o -> String.valueOf(o.get("tenant"))).toList()
                + (add ? " are not ready for the constraints" : " still break a constraint"));
            out.put("reason", add && anyUnswept ? "tenant_unswept" : "tenant_violates_constraint");
            out.put("tenants", offenders);
            out.put("tenants_checked", new ArrayList<>(checked));
            out.put("table_rows_examined", rowsExamined);
            HttpUtil.send(ex, 409, json(out));
            return;
        }
        List<ChunkConstraintAdmin.Action> actions = add
            ? admin.add(plan, Boolean.TRUE.equals(body.get("replace"))) : admin.validate();
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("tenants_checked", new ArrayList<>(checked));
        out.put("table_rows_examined", rowsExamined);
        if (add) {
            out.put("left_behind", leftBehind);
        }
        List<Map<String, Object>> list = new ArrayList<>();
        for (var a : actions) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("name", a.name());
            m.put("action", a.action());
            if (a.after() != null) {
                m.put("present", a.after().present());
                m.put("validated", a.after().validated());
                m.put("definition", a.after().definition());
            }
            if (a.warning() != null) {
                m.put("warning", a.warning());
            }
            if (a.expectedDefinition() != null) {
                m.put("expected_definition", a.expectedDefinition());
            }
            list.add(m);
        }
        out.put("constraints", list);
        HttpUtil.send(ex, 200, json(out));
    }

    /** The tenants whose probe row count is below their tenant-scoped count (a tenant the probe omits counts 0). */
    static List<String> undercounted(List<ChunkConstraintAdmin.TenantProbe> probe, Map<String, Long> scoped) {
        Map<String, Long> seen = new TreeMap<>();
        for (var p : probe) {
            seen.merge(p.tenant(), p.rows(), Long::sum);
        }
        List<String> out = new ArrayList<>();
        for (var e : scoped.entrySet()) {
            if (seen.getOrDefault(e.getKey(), 0L) < e.getValue()) {
                out.add(e.getKey());
            }
        }
        return out;
    }

    // ---- request parsing ----------------------------------------------------------------------------------------

    private static Map<String, Object> readBody(HttpExchange ex) throws IOException {
        byte[] bytes = ex.getRequestBody().readAllBytes();
        if (bytes.length == 0) {
            throw new IllegalArgumentException("request body is required");
        }
        return MAPPER.readValue(bytes, MAP_TYPE);
    }

    private static ChunkKeyPlan plan(Map<String, Object> body) {
        return ChunkKeyPlan.of(stringList(body, "allowed_keys", true), stringList(body, "bypass_prefixes", true),
            stringList(body, "quarantine_implies", true));
    }

    /** {@code "accept": {"NAME": N}}: each name an acceptable residue, each N a non-negative integer. */
    private static Map<String, Long> accepted(Map<String, Object> body) {
        Object v = body.get("accept");
        Map<String, Long> out = new TreeMap<>();
        if (v == null) {
            return out;
        }
        if (!(v instanceof Map<?, ?> raw)) {
            throw new IllegalArgumentException("'accept' must be an object of residue name to the number of rows "
                + "accepted, e.g. {\"keyless_notes\": 3}");
        }
        for (Map.Entry<?, ?> e : raw.entrySet()) {
            String name = String.valueOf(e.getKey());
            if (!ChunkKeySweep.ACCEPTABLE.contains(name)) {
                throw new IllegalArgumentException("accept names " + name + ", which is not an acceptable residue ("
                    + ChunkKeySweep.ACCEPTABLE + ")");
            }
            if (!(e.getValue() instanceof Number n) || n.longValue() < 0 || n.doubleValue() != n.longValue()) {
                throw new IllegalArgumentException("accept." + name + " must be a non-negative integer (the rows "
                    + "accepted)");
            }
            out.put(name, n.longValue());
        }
        return out;
    }

    private static ChunkKeySweep.Resume resume(Map<String, Object> body) {
        Object v = body.get("resume");
        if (v == null) {
            return null;
        }
        if (!(v instanceof Map<?, ?> m)) {
            throw new IllegalArgumentException("'resume' must be the object a previous apply call returned");
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> r = (Map<String, Object>) m;
        ChunkKeySweep.Cursor cursor = null;
        Object c = r.get("cursor");
        if (c != null) {
            if (!(c instanceof Map<?, ?> cm) || !(cm.get("collection") instanceof String col)
                    || !(cm.get("chash") instanceof String hash) || hash.length() != 64 || !isHex(hash)) {
                throw new IllegalArgumentException("resume.cursor must be {collection, chash} with a 64-hex chash");
            }
            cursor = new ChunkKeySweep.Cursor(col, hash);
        }
        List<String> collections = stringList(r, "collections", true);
        List<ChunkKeySweep.Blocked> blocked = new ArrayList<>();
        if (r.get("blocked") instanceof List<?> bl) {
            for (Object o : bl) {
                if (!(o instanceof Map<?, ?> bm) || !(bm.get("collection") instanceof String col)) {
                    throw new IllegalArgumentException("resume.blocked must hold {collection, blocked, retired_key_rows}");
                }
                String reason = bm.get("blocked") instanceof String s ? s : "";
                long rows = bm.get("retired_key_rows") instanceof Number n ? n.longValue() : 0L;
                blocked.add(new ChunkKeySweep.Blocked(col, reason, rows));
            }
        }
        long remaining = r.get("remaining") instanceof Number n ? n.longValue() : 0L;
        String sig = r.get("sig") instanceof String s ? s : null;
        return new ChunkKeySweep.Resume(cursor, collections, blocked, remaining, sig);
    }

    private static boolean isHex(String s) {
        try {
            HexFormat.of().parseHex(s);
            return true;
        } catch (IllegalArgumentException e) {
            return false;
        }
    }

    private static List<String> stringList(Map<String, Object> body, String field, boolean required) {
        Object v = body.get(field);
        if (v == null) {
            if (required) {
                throw new IllegalArgumentException("'" + field + "' is required");
            }
            return List.of();
        }
        if (!(v instanceof List<?> raw)) {
            throw new IllegalArgumentException("'" + field + "' must be an array of strings");
        }
        List<String> out = new ArrayList<>();
        for (Object o : raw) {
            if (!(o instanceof String s)) {
                throw new IllegalArgumentException("'" + field + "' must hold only strings");
            }
            out.add(s);
        }
        return out;
    }

    private static int intField(Map<String, Object> body, String field, int dflt, int lo, int hi) {
        Object v = body.get(field);
        if (v == null) {
            return dflt;
        }
        if (!(v instanceof Integer i)) {
            throw new IllegalArgumentException("'" + field + "' must be an integer");
        }
        return Math.max(lo, Math.min(hi, i));
    }

    private static String json(Object value) throws IOException {
        return MAPPER.writeValueAsString(value);
    }
}
