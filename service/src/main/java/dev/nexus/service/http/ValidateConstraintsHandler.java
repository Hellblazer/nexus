// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.ChashConstraintValidator;
import dev.nexus.service.http.ValidateConstraintsJobs.Progress;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;
import java.util.function.Consumer;
import java.util.function.Supplier;

/**
 * nexus-f3yqd, engine 0.1.190: {@code VALIDATE CONSTRAINT} of the chash width CHECKs as a job a user starts.
 *
 * <ul>
 *   <li>{@code POST /v1/admin/validate-constraints} {@code {dry_run?: bool}} answers 202 {@code {job_id, created}}.
 *       {@code dry_run} defaults to false. One job runs on the whole engine: a start with the same {@code dry_run} while one
 *       runs is handed that job ({@code created: false}), and one with the other {@code dry_run} is a 409 naming it.</li>
 *   <li>{@code GET /v1/admin/validate-constraints/{job_id}} answers 200 with {@code job_id}, {@code dry_run}, {@code state}
 *       ({@code running}, {@code done}, {@code failed}), {@code stage}, {@code elapsed_ms}, {@code tables_done},
 *       {@code tables_total}, {@code current} ({@code {table, bytes}} while a step runs, else null), {@code validated},
 *       {@code refused} ({@code [{table, constraint, reason, bad_rows, bad_rows_capped, example_chashes, precheck}]}),
 *       {@code deferred} ({@code [{table, constraint, reason, precheck}]}), their counts, {@code would_validate} on a dry run,
 *       {@code all_validated} (the conformance route's answer after the last step; null until then), {@code safe_to_stop}
 *       (always true) and {@code on_stop}. A failed job adds {@code http_status}, {@code error}, {@code reason}. An id the
 *       engine does not hold (never started, expired, lost with a restart) is a 404.</li>
 *   <li>{@code DELETE /v1/admin/validate-constraints/{job_id}} cancels a running job: the statement it has in flight (a
 *       VALIDATE mid-table, a pre-check, a lock wait, a backoff) is cancelled on the server and no later one starts. A 202
 *       (with the status, {@code stopping: cancelled}) says the cancel was accepted, and the job then ends {@code failed} with
 *       {@code reason: cancelled} and {@code http_status} 409, wherever in its run the cancel landed; the discriminator is
 *       {@code reason}, never the 409 (a start's conflict is a 409 too). A VALIDATE is atomic, so the table it was on stays
 *       NOT VALID and every constraint validated before it stays validated; a new start resumes at the next one. A start sent
 *       while a cancelled job is still ending is a 409 {@code reason: cancelling} naming it: poll it, then start again. A
 *       DELETE of a job that has already ended is a 200 with its status, unchanged; an unknown id is a 404.</li>
 * </ul>
 *
 * <p><b>Authorization.</b> The root token only ({@link RequestContext#isOperator()}), every route: the constraints are
 * table-wide and the job runs on the engine's admin connection. Any other credential is a 403 (the auth filter has already
 * refused a missing or unknown one with 401).
 *
 * <p><b>Never at boot.</b> Nothing in the engine starts this job (Sam, 2026-11-11): an {@code nx} command or
 * {@code nx doctor --fix} sends the POST. A job that is not started does not run, and an engine restart loses a running job's
 * progress but not a constraint it validated; start again and it resumes at the next one ({@link
 * ChashConstraintValidator} has the order, the pre-check, the lock bounds and the role).
 *
 * <p><b>No strong locks.</b> The job never lifts FORCE row-level security and never takes ACCESS EXCLUSIVE on any table, so
 * search and indexing go on while it runs. Where the admin role cannot read every row of a table (the manifest, in
 * production) the pre-check is {@code unavailable}: the VALIDATE decides, a bad row is a refusal with {@code bad_rows} null,
 * and a dry run reports {@code precheck: unavailable} instead of saying the table would validate.
 *
 * <p>{@code tables} in the progress fields are the relations that carry a copy of a chash CHECK: each leaf partition of
 * {@code nexus.chunks}, each model partition, {@code nexus.chunks} and {@code nexus.catalog_document_chunks}. VALIDATE gives
 * no row progress, so a step's progress is the table it is on and its size in bytes. The job is not listed in
 * {@code GET /v1/status}'s {@code boot_work}: it is not boot work.
 */
public final class ValidateConstraintsHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(ValidateConstraintsHandler.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() { };

    /** The route's path; {@code NexusService} mounts the handler here, behind {@code AuthFilter}. */
    public static final String PATH = "/v1/admin/validate-constraints";

    private final Supplier<ChashConstraintValidator> validatorSource;
    private final EngineJobs<Progress> jobs;

    /**
     * @param validatorSource the admin-connection validator, bound late; null (or a supplier answering null) is 503
     * @param jobs            the registry the jobs run on ({@link ValidateConstraintsJobs#registry})
     */
    public ValidateConstraintsHandler(Supplier<ChashConstraintValidator> validatorSource, EngineJobs<Progress> jobs) {
        this.validatorSource = validatorSource;
        this.jobs = jobs;
    }

    @Override
    public void handle(HttpExchange ex) throws IOException {
        String path = ex.getRequestURI().getPath();
        String rest = path.startsWith(PATH) ? path.substring(PATH.length()) : null;
        if (rest == null || !(rest.isEmpty() || rest.startsWith("/"))) {
            HttpUtil.send(ex, 404, "{\"error\":\"not found\"}");
            return;
        }
        String method = ex.getRequestMethod().toUpperCase(Locale.ROOT);
        boolean start = rest.isEmpty();
        if (start ? !"POST".equals(method) : !("GET".equals(method) || "DELETE".equals(method))) {
            HttpUtil.send(ex, 405, "{\"error\":\"method not allowed\"}");
            return;
        }
        if (RequestContext.tenant() == null) {
            HttpUtil.send(ex, 500, "{\"error\":\"internal: tenant not set\"}");
            return;
        }
        if (!RequestContext.isOperator()) {
            HttpUtil.send(ex, 403, "{\"error\":\"operator privilege required: the constraint validate job needs the root token\"}");
            return;
        }
        ex.getResponseHeaders().set("Cache-Control", "no-store");
        if (start) {
            start(ex);
        } else if ("DELETE".equals(method)) {
            cancel(ex, rest.substring(1));
        } else {
            poll(ex, rest.substring(1));
        }
    }

    // ── POST ────────────────────────────────────────────────────────────────

    private void start(HttpExchange ex) throws IOException {
        Map<String, Object> body;
        try (InputStream in = ex.getRequestBody()) {
            byte[] bytes = in.readAllBytes();
            body = bytes.length == 0 ? Map.of() : MAPPER.readValue(bytes, MAP_TYPE);
        } catch (JsonProcessingException e) {
            badRequest(ex, "the body must be a JSON object");
            return;
        }
        if (body == null) {   // a body of the JSON literal null
            badRequest(ex, "the body must be a JSON object");
            return;
        }
        Object rawDry = body.get("dry_run");
        if (rawDry != null && !(rawDry instanceof Boolean)) {
            badRequest(ex, "dry_run must be true or false");
            return;
        }
        boolean dryRun = Boolean.TRUE.equals(rawDry);
        ChashConstraintValidator validator = validatorSource == null ? null : validatorSource.get();
        if (validator == null) {
            HttpUtil.send(ex, 503, "{\"error\":\"this engine has no admin connection bound for the constraint validate job\"}");
            return;
        }
        EngineJobs.Started<Progress> started;
        try {
            started = jobs.start(ValidateConstraintsJobs.JOB_TENANT, null, ValidateConstraintsJobs.fingerprint(dryRun),
                job -> new Progress(dryRun), (job, stage) -> run(validator, job, stage));
        } catch (EngineJobs.Conflict c) {
            Map<String, Object> err = new LinkedHashMap<>();
            err.put("error", c.getMessage());
            err.put("job_id", c.jobId());
            if (c.running().data() instanceof Progress running) {
                err.put("dry_run", running.dryRun());
            }
            send(ex, 409, err);
            return;
        } catch (EngineJobs.Busy b) {
            send(ex, 503, Map.of("error", b.getMessage()));
            return;
        }
        if (!started.created() && ValidateConstraintsJobs.CANCELLED.equals(started.job().stopReason())) {
            // the job a start would join is being cancelled: joining it would hand back a job that ends cancelled
            Map<String, Object> err = new LinkedHashMap<>();
            err.put("error", "the running validate job is being cancelled; poll it until it ends, then start again");
            err.put("reason", "cancelling");
            err.put("job_id", started.job().id());
            send(ex, 409, err);
            return;
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("job_id", started.job().id());
        out.put("created", started.created());
        send(ex, 202, out);
    }

    // ── GET ─────────────────────────────────────────────────────────────────

    private void poll(HttpExchange ex, String jobId) throws IOException {
        EngineJobs.Job<Progress> job = find(jobId);
        if (job == null) {
            HttpUtil.send(ex, 404, "{\"error\":\"no such constraint validate job\"}");
            return;
        }
        send(ex, 200, status(job));
    }

    private EngineJobs.Job<Progress> find(String jobId) {
        return jobId.isEmpty() || jobId.indexOf('/') >= 0 ? null : jobs.find(jobId, ValidateConstraintsJobs.JOB_TENANT);
    }

    // ── DELETE ──────────────────────────────────────────────────────────────

    /**
     * Cancels a running job. The stop reason is set first and the in-flight statements cancelled second: a step that publishes
     * its session after the cancel looked for one reads the reason before its first statement ({@link ChashConstraintValidator}),
     * so no VALIDATE starts after a 202. The cancel names this job ({@link ChashConstraintValidator#cancelRunningFor}), so a
     * job started after this one ended is never touched. Logged, not audited: the route is root only and changes no data.
     */
    private void cancel(HttpExchange ex, String jobId) throws IOException {
        EngineJobs.Job<Progress> job = find(jobId);
        if (job == null) {
            HttpUtil.send(ex, 404, "{\"error\":\"no such constraint validate job\"}");
            return;
        }
        if (job.state() != EngineJobs.State.RUNNING) {
            send(ex, 200, status(job));
            return;
        }
        job.stopFor(ValidateConstraintsJobs.CANCELLED);
        Map<String, Object> asked = status(job);   // before the cancel lands, so the answer shows the job still stopping
        ChashConstraintValidator validator = validatorSource == null ? null : validatorSource.get();
        if (validator != null) validator.cancelRunningFor(job);   // this job's run; never a newer job's statements
        log.info("event=chash_validate_cancel_requested job_id={}", job.id());
        send(ex, 202, asked);
    }

    private Map<String, Object> status(EngineJobs.Job<Progress> job) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("job_id", job.id());
        m.put("state", job.state().wire());
        m.put("stage", job.stage());
        m.put("elapsed_ms", job.elapsedMs(jobs.nowMs()));
        m.putAll(job.data().view());
        m.put("safe_to_stop", true);
        m.put("on_stop", ValidateConstraintsJobs.ON_STOP);
        if (job.state() == EngineJobs.State.RUNNING) {
            String stopping = job.stopReason();
            if (stopping != null) m.put("stopping", stopping);
        }
        EngineJobs.Outcome outcome = job.outcome();
        if (job.state() == EngineJobs.State.FAILED && outcome != null) {
            m.put("http_status", outcome.httpStatus());
            m.put("error", outcome.body().get("error"));
            m.put("reason", outcome.body().get("reason"));
        }
        return m;
    }

    // ── the work ────────────────────────────────────────────────────────────

    private EngineJobs.Outcome run(ChashConstraintValidator validator, EngineJobs.Job<Progress> job, Consumer<String> stage) {
        Progress p = job.data();
        stage.accept("planning");
        var result = validator.run(p.dryRun(), ValidateConstraintsJobs.listener(job, p, stage));
        if (ValidateConstraintsJobs.CANCELLED.equals(result.stopped())) {
            log.info("event=chash_validate_cancelled tables_done={}", p.view().get("tables_done"));
            return ValidateConstraintsJobs.failed(409, ValidateConstraintsJobs.CANCELLED, "the job was cancelled; "
                + ValidateConstraintsJobs.ON_STOP);
        }
        if (result.stopped() != null) {
            log.info("event=chash_validate_stopped reason={}", result.stopped());
            return ValidateConstraintsJobs.failed(500, "job_stopped", "the job stopped (" + result.stopped()
                + "); " + ValidateConstraintsJobs.ON_STOP);
        }
        p.finished(result.allValidated());
        log.info("event=chash_validate_done dry_run={} all_validated={}", p.dryRun(), result.allValidated());
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("all_validated", result.allValidated());
        return new EngineJobs.Outcome(200, out);
    }

    // ── shared ──────────────────────────────────────────────────────────────

    private static void badRequest(HttpExchange ex, String message) throws IOException {
        HttpUtil.send(ex, 400, "{\"error\":" + HttpUtil.jsonString(message) + "}");
    }

    private static void send(HttpExchange ex, int status, Map<String, Object> body) throws IOException {
        try {
            HttpUtil.send(ex, status, MAPPER.writeValueAsString(body));
        } catch (JsonProcessingException e) {
            throw new IOException(e);
        }
    }
}
