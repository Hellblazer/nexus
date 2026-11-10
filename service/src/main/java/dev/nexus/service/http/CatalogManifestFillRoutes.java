// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.sun.net.httpserver.HttpExchange;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.ManifestFill;
import dev.nexus.service.db.ManifestFillableCount;
import dev.nexus.service.http.ManifestFillJobs.Progress;
import dev.nexus.service.http.ManifestFillJobs.Snapshot;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.function.Consumer;

/**
 * nexus-f3yqd, route R2 of T2 {@code nexus/rung-detect-route-spec-2026-11-10}, split out of {@link CatalogHandler}
 * (which is at its file-size cap): the engine-side fill of the manifest section labels and spans as a background job.
 *
 * <ul>
 *   <li>{@code POST /v1/catalog/manifest/fill} {@code {kind: "labels"|"spans", dry_run?: bool, batch_rows?: int,
 *       after?: {doc_id, position}}} answers 202 {@code {job_id, created}}. {@code kind} is required and exact;
 *       {@code dry_run} defaults to false; {@code batch_rows} is 1 to {@link ManifestFill#MAX_BATCH_ROWS} (default
 *       {@link ManifestFill#DEFAULT_BATCH_ROWS}); {@code after} is the {@code cursor} a poll showed (a non-empty
 *       {@code doc_id} string and a non-negative integer {@code position}), and the job's walk begins after that
 *       manifest key instead of at the start; anything else is a 400. One fill job runs per tenant, whatever its kind
 *       (Sam, 2026-11-10): a start with the same {@code kind} and {@code dry_run} while one runs is handed that job
 *       ({@code created: false}; its {@code batch_rows} and {@code after} are ignored), and one with another kind or the
 *       other {@code dry_run} is a 409 naming it; a full registry is a 503. A client of a managed service may start it
 *       unattended (Sam, 2026-11-10): a later box joins the running one.</li>
 *   <li>{@code GET /v1/catalog/manifest/fill/{job_id}} reads it: {@code {job_id, kind, dry_run, state, stage,
 *       elapsed_ms, done_rows, total_rows, scanned_rows, manifest_rows_estimate, filled, cursor, counts, safe_to_stop,
 *       on_stop}}, and {@code http_status}, {@code error} and {@code reason} once it has failed. 404 for an id the
 *       engine does not hold (never started, expired, lost with a restart, evicted) and for another tenant's id
 *       alike.</li>
 * </ul>
 *
 * <p>The job fills exactly the rows {@code GET /v1/catalog/manifest/fillable} counts as {@code fillable} ({@link
 * ManifestFill}), so after a completed job that count answers {@code fillable} 0 for the kind and the {@code edge} rows,
 * which no job writes, are unchanged, except for the rows a concurrent writer held (below), which the next start fills.
 *
 * <p><b>Progress.</b> {@code progress = scanned_rows / manifest_rows_estimate}. {@code scanned_rows} is the manifest
 * rows the batches' windows have covered; a window holds only rows the kind's prefilter admits ({@code labels_written =
 * false}; a NULL span column), so {@code manifest_rows_estimate} is the count of exactly those rows past the start cursor
 * when the job started: a count of the tenant's manifest on its primary key range under the fillable count's
 * statement timeout (10 s), null when it ran past that. It is a count, not {@code pg_class.reltuples}, because the
 * statistics are of the whole table, every tenant's rows, and say nothing of the prefilter; "estimate" because writers
 * change the manifest while the job runs, so the fraction can pass 1 on a busy tenant. {@code done_rows} is the
 * fillable rows picked, {@code total_rows} the fillable count when the job started (null when that count was capped or
 * timed out; with {@code after} it still counts the whole tenant); {@code filled} is what the UPDATEs wrote (0 for a
 * dry run); {@code cursor} is the last manifest key the batches have scanned, {@code {doc_id, position}} (the {@code
 * after} passed, before the first batch), or null. {@code counts} holds {@code fillable} (the rows picked),
 * {@code edge} (when the start-of-job count was exact) and {@code skipped_changed} (picked rows the UPDATE did not
 * write because a concurrent writer changed or held them; at least 0; 0 on a dry run), so a dry run's counts equal the
 * real run's over the same data.
 *
 * <p><b>Cost.</b> A batch is a window of {@code batch_rows} manifest rows the prefilter admits, found by a range scan
 * that stops once it has admitted that many: its cost is the rows it scans to do so, which is {@code batch_rows} only
 * while the admitted rows are dense. A sparse admitted set scans more rows per admitted one, and for spans the
 * converged case (rows whose chunk records no span stay NULL for good) walks every such row on every start. See {@link
 * ManifestFill}.
 *
 * <p><b>Pacing and locks.</b> The system property {@value ManifestFillJobs#PAUSE_PROPERTY} (milliseconds, default 0)
 * is a pause the job takes after each batch, so an operator can slow a fill on a managed tenant. A batch's UPDATE waits
 * at most {@code nexus.manifest.fill.lock_timeout_ms} (default 5000) for a row another writer holds; a writer holding
 * longer does not fail the job: the batch is run again skipping the held rows, which count in {@code skipped_changed}.
 *
 * <p>{@code safe_to_stop} is always true: a restart, a cancel or a crash loses no committed batch ({@code on_stop}:
 * committed batches stay written; a new start re-reads from the beginning, or from the cursor passed as {@code after}).
 * The job is not listed in {@code GET /v1/status}'s {@code boot_work}: that route is unauthenticated, holds one entry
 * per id, and a tenant's job there would show one tenant's activity to every caller of a shared service and collide
 * with another tenant's.
 */
final class CatalogManifestFillRoutes {

    private static final Logger log = LoggerFactory.getLogger(CatalogManifestFillRoutes.class);
    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() { };

    /** The path prefix of the poll route, {@code GET /v1/catalog/manifest/fill/{job_id}}, after {@code /v1/catalog}. */
    static final String POLL_PREFIX = "/manifest/fill/";

    private final ManifestFill fill;
    private final ManifestFillableCount count;
    /** Null answers both routes 503. */
    private final EngineJobs<Progress> jobs;

    CatalogManifestFillRoutes(ManifestFill fill, ManifestFillableCount count, EngineJobs<Progress> jobs) {
        this.fill = fill;
        this.count = count;
        this.jobs = jobs;
    }

    // ── POST /manifest/fill ─────────────────────────────────────────────────

    void start(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body;
        try (InputStream in = exchange.getRequestBody()) {
            byte[] bytes = in.readAllBytes();
            body = bytes.length == 0 ? Map.of() : CatalogHandler.MAPPER.readValue(bytes, MAP_TYPE);
        } catch (JsonProcessingException e) {
            badRequest(exchange, "the body must be a JSON object");
            return;
        }
        ManifestFillableCount.Kind kind = body.get("kind") instanceof String k ? ManifestFillableCount.Kind.parse(k) : null;
        if (kind == null) { badRequest(exchange, "kind is required and must be labels or spans"); return; }
        Object rawDry = body.get("dry_run");
        if (rawDry != null && !(rawDry instanceof Boolean)) { badRequest(exchange, "dry_run must be true or false"); return; }
        boolean dryRun = Boolean.TRUE.equals(rawDry);
        int batchRows = ManifestFill.DEFAULT_BATCH_ROWS;
        Object rawRows = body.get("batch_rows");
        if (rawRows != null) {
            if (!(rawRows instanceof Integer n) || n < 1 || n > ManifestFill.MAX_BATCH_ROWS) {
                badRequest(exchange, "batch_rows must be an integer 1.." + ManifestFill.MAX_BATCH_ROWS);
                return;
            }
            batchRows = n;
        }
        ManifestFill.Cursor after = null;
        Object rawAfter = body.get("after");
        if (rawAfter != null) {
            after = parseAfter(rawAfter);
            if (after == null) {
                badRequest(exchange, "after must be {doc_id: a non-empty string, position: an integer of 0 or more}");
                return;
            }
        }
        exchange.getResponseHeaders().set("Cache-Control", "no-store");
        if (jobs == null) {
            HttpUtil.send(exchange, 503, "{\"error\":\"the manifest fill job registry is not available\"}");
            return;
        }
        final int rows = batchRows;
        final ManifestFill.Cursor from = after;
        EngineJobs.Started<Progress> started;
        try {
            started = jobs.start(tenant, ManifestFillJobs.SCOPE, ManifestFillJobs.fingerprint(kind, dryRun),
                job -> new Progress(kind, dryRun, rows, from), (job, stage) -> run(tenant, job, stage));
        } catch (EngineJobs.Conflict c) {
            Map<String, Object> err = new LinkedHashMap<>();
            err.put("error", c.getMessage());
            err.put("job_id", c.jobId());
            if (c.running().data() instanceof Progress running) {
                err.put("kind", running.kind().wire());
                err.put("dry_run", running.dryRun());
            }
            send(exchange, 409, err);
            return;
        } catch (EngineJobs.Busy b) {
            send(exchange, 503, Map.of("error", b.getMessage()));
            return;
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("job_id", started.job().id());
        out.put("created", started.created());
        send(exchange, 202, out);
    }

    /** {@code {doc_id, position}} exactly, or null when it is anything else. */
    private static ManifestFill.Cursor parseAfter(Object raw) {
        if (!(raw instanceof Map<?, ?> m) || m.size() != 2) return null;
        if (!(m.get("doc_id") instanceof String docId) || docId.isEmpty()) return null;
        if (!(m.get("position") instanceof Integer position) || position < 0) return null;
        return new ManifestFill.Cursor(docId, position);
    }

    // ── GET /manifest/fill/{job_id} ─────────────────────────────────────────

    void poll(HttpExchange exchange, String tenant, String method, String jobId) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        exchange.getResponseHeaders().set("Cache-Control", "no-store");
        if (jobs == null) {
            HttpUtil.send(exchange, 503, "{\"error\":\"the manifest fill job registry is not available\"}");
            return;
        }
        EngineJobs.Job<Progress> job = jobId.isEmpty() || jobId.indexOf('/') >= 0 ? null : jobs.find(jobId, tenant);
        if (job == null) {
            HttpUtil.send(exchange, 404, "{\"error\":\"no such manifest fill job\"}");
            return;
        }
        send(exchange, 200, status(job));
    }

    private Map<String, Object> status(EngineJobs.Job<Progress> job) {
        Progress p = job.data();
        Snapshot s = p.snapshot();
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("job_id", job.id());
        m.put("kind", p.kind().wire());
        m.put("dry_run", p.dryRun());
        m.put("state", job.state().wire());
        m.put("stage", job.stage());
        m.put("elapsed_ms", job.elapsedMs(jobs.nowMs()));
        m.put("done_rows", s.picked());
        m.put("total_rows", s.total());
        m.put("scanned_rows", s.scanned());
        m.put("manifest_rows_estimate", s.admitted());
        m.put("filled", s.filled());
        if (s.cursor() == null) {
            m.put("cursor", null);
        } else {
            Map<String, Object> c = new LinkedHashMap<>();
            c.put("doc_id", s.cursor().docId());
            c.put("position", s.cursor().position());
            m.put("cursor", c);
        }
        Map<String, Object> counts = new LinkedHashMap<>();
        counts.put("fillable", s.picked());
        if (s.edge() != null) counts.put("edge", s.edge());
        counts.put("skipped_changed", p.dryRun() ? 0 : Math.max(0, s.picked() - s.filled()));
        m.put("counts", counts);
        m.put("safe_to_stop", true);
        m.put("on_stop", ManifestFillJobs.ON_STOP);
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

    /**
     * One fill job: count the fillable rows once (for {@code total_rows} and {@code edge}) and the rows its walk will
     * scan (for {@code manifest_rows_estimate}), then batch after batch, each its own transaction over the next window
     * of manifest rows, until a window is shorter than the batch size. The job asks {@link
     * EngineJobs.Job#stopReason()} and its thread's interrupt flag before each batch, so it is never cut inside one, and
     * pauses {@link ManifestFillJobs#PAUSE_PROPERTY} milliseconds after each.
     */
    private EngineJobs.Outcome run(String tenant, EngineJobs.Job<Progress> job, Consumer<String> stage) {
        Progress p = job.data();
        ManifestFillableCount.Kind kind = p.kind();
        stage.accept("counting");
        Long total = null;
        Long edge = null;
        try {
            var counted = count.count(tenant, kind, ManifestFillableCount.MAX_LIMIT);
            if (!counted.capped()) {
                total = (long) counted.fillable();
                edge = (long) counted.edge();
            }
        } catch (RuntimeException e) {
            // A count that outlasts its bound only leaves the totals unknown; the fill does not depend on it.
            if (!CatalogHandler.hasSqlState(e, CatalogRepository.QUERY_CANCELED)) throw e;
            log.warn("event=manifest_fill_count_timeout tenant={} kind={}", tenant, kind.wire());
        }
        Long admitted = fill.admittedRows(tenant, kind, p.after());
        Snapshot now = new Snapshot(0, 0, 0, total, edge, admitted, p.after());
        p.snapshot(now);
        stage.accept("filling");
        ManifestFill.Cursor cursor = p.after();
        while (true) {
            String stop = job.stopReason();
            if (stop == null && Thread.interrupted()) stop = "interrupted";
            if (stop != null) return stopped(tenant, kind, stop, now);
            ManifestFill.Batch batch = fill.batch(tenant, kind, cursor, p.batchRows(), p.dryRun());
            now = new Snapshot(now.picked() + batch.picked(), now.filled() + batch.filled(),
                now.scanned() + batch.scanned(), total, edge, admitted,
                batch.next() != null ? batch.next() : now.cursor());
            p.snapshot(now);
            // a window shorter than the batch size reached the end of the manifest
            if (batch.scanned() < p.batchRows()) break;
            cursor = batch.next();
            if (!pause()) return stopped(tenant, kind, "interrupted", now);
        }
        log.info("event=manifest_fill_done tenant={} kind={} dry_run={} picked={} filled={}",
            tenant, kind.wire(), p.dryRun(), now.picked(), now.filled());
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("picked", now.picked());
        out.put("filled", now.filled());
        return new EngineJobs.Outcome(200, out);
    }

    private static EngineJobs.Outcome stopped(String tenant, ManifestFillableCount.Kind kind, String reason,
                                              Snapshot now) {
        log.info("event=manifest_fill_stopped tenant={} kind={} reason={} picked={} filled={}",
            tenant, kind.wire(), reason, now.picked(), now.filled());
        return ManifestFillJobs.failed(500, "job_stopped", "the fill stopped (" + reason + ") after "
            + now.picked() + " rows; every committed batch stays written, a new start re-reads from the beginning, "
            + "or from the cursor passed as after");
    }

    /** The pause between batches; false when the thread was interrupted during it (the engine is shutting down). */
    private static boolean pause() {
        long ms = Long.getLong(ManifestFillJobs.PAUSE_PROPERTY, 0L);
        if (ms <= 0) return true;
        try {
            Thread.sleep(ms);
            return true;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return false;
        }
    }

    // ── shared ──────────────────────────────────────────────────────────────

    private static void badRequest(HttpExchange exchange, String message) throws IOException {
        HttpUtil.send(exchange, 400, "{\"error\":" + HttpUtil.jsonString(message) + "}");
    }

    private static void send(HttpExchange exchange, int status, Map<String, Object> body) throws IOException {
        try {
            HttpUtil.send(exchange, status, CatalogHandler.MAPPER.writeValueAsString(body));
        } catch (JsonProcessingException e) {
            throw new IOException(e);
        }
    }
}
