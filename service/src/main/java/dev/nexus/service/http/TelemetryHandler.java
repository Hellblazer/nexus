package dev.nexus.service.http;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.TelemetryRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * RDR-152 bead nexus-gmiaf.12 — Telemetry HTTP endpoints.
 *
 * <p>Routes (all under {@code /v1/telemetry/}):
 * <pre>
 *   POST /v1/telemetry/relevance/log           single relevance event
 *   POST /v1/telemetry/relevance/batch         batch relevance events
 *   GET  /v1/telemetry/relevance/query         query by filters
 *   GET  /v1/telemetry/relevance/stats         count/oldest/newest (nexus-v0x32)
 *   POST /v1/telemetry/relevance/expire        expire old entries
 *   POST /v1/telemetry/search/batch            batch search telemetry
 *   GET  /v1/telemetry/search/stats            collection health stats
 *   POST /v1/telemetry/search/trim             trim old entries; dry_run=true previews the
 *                                              count without deleting (same WHERE predicate
 *                                              as the delete, never a separate count query)
 *   POST /v1/telemetry/rename_collection       rename collection in all tables
 *   POST /v1/telemetry/tier_writes/record      record a tier-write event
 *   GET  /v1/telemetry/tier_writes/query       aggregated counts for nx tier-status (nexus-59wjj)
 *   GET  /v1/telemetry/tier_writes/list        per-row detail incl. target_title, capped page +
 *                                              exact total (nexus-onjvy)
 *   GET  /v1/telemetry/retention/markers       cumulative-deletes retention markers (nexus-24p05)
 *   POST /v1/telemetry/nx_answer_runs/record   record an nx_answer run
 *   GET  /v1/telemetry/nx_answer_runs/query     rows + exact aggregates (nexus-eho3u)
 *   POST /v1/telemetry/hook_failures/record    record a hook failure
 *   GET  /v1/telemetry/hook_failures/list      list hook failures + exact totals (nexus-onjvy)
 *   POST /v1/telemetry/hook_failures/trim      trim old hook-failure entries; same dry_run=true
 *                                              preview contract as search/trim
 *   POST /v1/telemetry/index_failures/record   record one durable index failure (nexus-nukn3)
 *   POST /v1/telemetry/index_failures/record_batch  batch-record (one txn); nx index repo's
 *                                              end-of-run write of every file skipped this run
 *   GET  /v1/telemetry/index_failures/list     list index failures + exact totals, optionally
 *                                              scoped to one run_id (nexus-nukn3)
 *   POST /v1/telemetry/index_failures/trim     delete rows by run_id and/or age; the
 *                                              nx index failures --clear remedy (nexus-nukn3
 *                                              fold-in, critic finding: an all-time fail-first
 *                                              check with no clear path is unfixable forever)
 *   POST /v1/telemetry/frecency/upsert         upsert frecency record
 *   GET  /v1/telemetry/frecency/get            get frecency by chunk_id
 *   POST /v1/telemetry/import                  fidelity ETL for all 6 tables
 *   POST /v1/telemetry/import_batch             bulk fidelity ETL for one table
 *   POST /v1/telemetry/ids/probe                membership probe (verify-fill inner loop)
 *   POST /v1/telemetry/capability_census/record upsert one session's capability census
 *                                                (nexus-gjv9b PART 1)
 *   GET  /v1/telemetry/capability_census/query  read capability_census rows, newest first
 *   POST /v1/telemetry/routing_events/record    append one routing-hook event (nexus-gjv9b PART 2)
 *   POST /v1/telemetry/routing_events/batch     append a batch of routing-hook events
 *   GET  /v1/telemetry/routing_events/list      read routing_events rows, newest first
 * </pre>
 *
 * <p>All endpoints require {@code Authorization: Bearer <token>} (via {@link AuthFilter})
 * and {@code X-Nexus-Tenant} header.
 */
public final class TelemetryHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(TelemetryHandler.class);

    static final ObjectMapper MAPPER = new ObjectMapper()
            .registerModule(new JavaTimeModule())
            .disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS)
            .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false)
            .setSerializationInclusion(JsonInclude.Include.ALWAYS);

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    /** RDR-178 wave-2 (nexus-s3dd4.3): max candidate keys per /ids/probe request. */
    private static final int MAX_PROBE_KEYS = 300;

    /**
     * RDR-196 .p1c-b review fix (nexus-lme1s): max {@code limit} for
     * {@code GET /nx_answer_runs/query}, matching the project's
     * {@code MAX_QUERY_RESULTS} paging convention (src/nexus/db/limits.py).
     * Without this, {@code ?include_steps=true} lets a caller request an
     * unbounded page of runs, each pulling an unbounded number of steps —
     * N runs x M steps with no ceiling on either N or M. Clamped
     * server-side (never a 400) so an over-large request degrades to the
     * capped page rather than failing; a client that actually wanted more
     * than {@value} rows was always going to need pagination (``since``)
     * regardless of this cap.
     */
    private static final int MAX_QUERY_RUNS_LIMIT = 300;

    private final TelemetryRepository repo;

    public TelemetryHandler(TelemetryRepository repo) {
        this.repo = repo;
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        String tenant = RequestContext.tenant();
        if (tenant == null) {
            HttpUtil.send(exchange, 500, "{\"error\":\"internal: tenant not set\"}");
            return;
        }

        String path   = exchange.getRequestURI().getPath();
        String op     = path.replaceFirst("^/v1/telemetry", "");
        String method = exchange.getRequestMethod().toUpperCase(Locale.ROOT);

        try {
            switch (op) {
                case "/relevance/log"          -> handleRelevanceLog(exchange, tenant, method);
                case "/relevance/batch"        -> handleRelevanceBatch(exchange, tenant, method);
                case "/relevance/query"        -> handleRelevanceQuery(exchange, tenant, method);
                case "/relevance/stats"        -> handleRelevanceStats(exchange, tenant, method);
                case "/relevance/expire"       -> handleRelevanceExpire(exchange, tenant, method);
                case "/search/batch"           -> handleSearchBatch(exchange, tenant, method);
                case "/search/stats"           -> handleSearchStats(exchange, tenant, method);
                case "/search/trim"            -> handleSearchTrim(exchange, tenant, method);
                case "/rename_collection"      -> handleRenameCollection(exchange, tenant, method);
                case "/tier_writes/record"     -> handleTierWriteRecord(exchange, tenant, method);
                case "/tier_writes/query"      -> handleTierWritesQuery(exchange, tenant, method);
                case "/tier_writes/list"       -> handleTierWritesList(exchange, tenant, method);
                case "/retention/markers"      -> handleRetentionMarkers(exchange, tenant, method);
                case "/nx_answer_runs/record"  -> handleNxAnswerRunRecord(exchange, tenant, method);
                case "/nx_answer_runs/query"   -> handleNxAnswerRunsQuery(exchange, tenant, method);
                case "/hook_failures/record"   -> handleHookFailureRecord(exchange, tenant, method);
                case "/hook_failures/list"     -> handleHookFailureList(exchange, tenant, method);
                case "/hook_failures/trim"     -> handleHookFailureTrim(exchange, tenant, method);
                case "/index_failures/record"       -> handleIndexFailureRecord(exchange, tenant, method);
                case "/index_failures/record_batch"  -> handleIndexFailureRecordBatch(exchange, tenant, method);
                case "/index_failures/list"         -> handleIndexFailureList(exchange, tenant, method);
                case "/index_failures/trim"          -> handleIndexFailureTrim(exchange, tenant, method);
                case "/frecency/upsert"        -> handleFrecencyUpsert(exchange, tenant, method);
                case "/frecency/get"           -> handleFrecencyGet(exchange, tenant, method);
                case "/import"                 -> handleImport(exchange, tenant, method);
                case "/import_batch"           -> handleImportBatch(exchange, tenant, method);
                case "/ids/probe"               -> handleIdsProbe(exchange, tenant, method);
                case "/capability_census/record" -> handleCapabilityCensusRecord(exchange, tenant, method);
                case "/capability_census/query"  -> handleCapabilityCensusQuery(exchange, tenant, method);
                case "/routing_events/record"    -> handleRoutingEventRecord(exchange, tenant, method);
                case "/routing_events/batch"     -> handleRoutingEventsBatch(exchange, tenant, method);
                case "/routing_events/list"      -> handleRoutingEventsList(exchange, tenant, method);
                default -> HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}");
            }
        } catch (IllegalArgumentException e) {
            HttpUtil.send(exchange, 400, json(Map.of("error", e.getMessage())));
        } catch (Exception e) {
            // Shared typed-DB-error ladder: pool-exhaustion 503 + class-23 409
            // (nexus-h8rf6.2 / nexus-7e057) — see HttpUtil.sendTypedDbError.
            if (!HttpUtil.sendTypedDbError(exchange, e, log, "telemetry_handler",
                    "op=" + op)) {
                log.error("event=telemetry_handler_error op={}", op, e);
                HttpUtil.send(exchange, 500, json(Map.of("error", "internal server error")));
            }
        }
    }

    // ── relevance_log ──────────────────────────────────────────────────────────

    private void handleRelevanceLog(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        String query      = requireString(body, "query");
        // nexus-lgdel.l1: the regrowth gap — with chash_alias and its
        // coalesce-to-hex UPDATE cascades deleted, nothing else normalizes
        // a non-canonical chunk_id written through this endpoint.
        String chunkId    = dev.nexus.service.db.Chash.requireCanonical(
            requireString(body, "chunk_id"), "'chunk_id'");
        String action     = requireString(body, "action");
        String sessionId  = optStr(body, "session_id");
        String collection = optStr(body, "collection");
        long id = repo.logRelevance(tenant, query, chunkId, action, sessionId, collection);
        HttpUtil.send(ex, 200, json(Map.of("id", id)));
    }

    @SuppressWarnings("unchecked")
    private void handleRelevanceBatch(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        var rows = (List<List<String>>) body.getOrDefault("rows", List.of());
        int count = repo.logRelevanceBatch(tenant, rows);
        HttpUtil.send(ex, 200, json(Map.of("inserted", count)));
    }

    private void handleRelevanceQuery(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        var params  = queryParams(ex);
        String query     = params.getOrDefault("query", "");
        String chunkId   = params.getOrDefault("chunk_id", "");
        String action    = params.getOrDefault("action", "");
        String sessionId = params.getOrDefault("session_id", "");
        int limit = parseIntParam(params, "limit", 100);
        var rows = repo.getRelevanceLog(tenant, query, chunkId, action, sessionId, limit);
        HttpUtil.send(ex, 200, json(rows));
    }

    /**
     * GET /v1/telemetry/relevance/stats (nexus-v0x32, playbook §4.5 telemetry
     * baseline): {@code {count, oldest, newest}} for the tenant's whole
     * relevance_log — no filters, no paging, so a single call answers the
     * baseline's "row count, oldest surviving row timestamp" figures without
     * paging through {@code /relevance/query}'s capped 300-row window.
     */
    private void handleRelevanceStats(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        HttpUtil.send(ex, 200, json(repo.relevanceStats(tenant)));
    }

    private void handleRelevanceExpire(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        int days = optInt(body, "days", 90);
        int deleted = repo.expireRelevanceLog(tenant, days);
        HttpUtil.send(ex, 200, json(Map.of("deleted", deleted)));
    }

    // ── search_telemetry ───────────────────────────────────────────────────────

    @SuppressWarnings("unchecked")
    private void handleSearchBatch(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        var rows = (List<List<Object>>) body.getOrDefault("rows", List.of());
        List<Object[]> tuples = rows.stream()
            .map(r -> r.toArray(Object[]::new))
            .toList();
        int count = repo.logSearchBatch(tenant, tuples);
        HttpUtil.send(ex, 200, json(Map.of("inserted", count)));
    }

    private void handleSearchStats(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        var params = queryParams(ex);
        String collection = params.getOrDefault("collection", "");
        int days = parseIntParam(params, "days", 30);
        var stats = repo.queryCollectionStats(tenant, collection, days);
        HttpUtil.send(ex, 200, json(stats));
    }

    /**
     * POST /v1/telemetry/search/trim — trim, or with {@code dry_run=true} PREVIEW,
     * search_telemetry rows older than {@code days}.
     *
     * <p>{@code dry_run} defaults to {@code false} when absent (every pre-existing
     * caller keeps its real-delete behavior). When {@code true} the response
     * carries the count that WOULD be deleted, computed by
     * {@link TelemetryRepository#trimSearchTelemetry(String, int, boolean)} from
     * the identical predicate the real delete uses, and nothing is removed. The
     * echoed {@code dry_run} field lets a caller tell a preview response apart
     * from a real one at a glance.
     */
    private void handleSearchTrim(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        int days = optInt(body, "days", 30);
        boolean dryRun = Boolean.TRUE.equals(body.get("dry_run"));
        int deleted = repo.trimSearchTelemetry(tenant, days, dryRun);
        HttpUtil.send(ex, 200, json(Map.of("deleted", deleted, "dry_run", dryRun)));
    }

    // ── rename_collection ──────────────────────────────────────────────────────

    private void handleRenameCollection(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        String oldName = requireString(body, "old");
        String newName = requireString(body, "new");
        var counts = repo.renameCollection(tenant, oldName, newName);
        HttpUtil.send(ex, 200, json(counts));
    }

    // ── tier_writes ────────────────────────────────────────────────────────────

    private void handleTierWriteRecord(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        String sessionId   = requireString(body, "session_id");
        String tsIso       = optStr(body, "ts");
        String tool        = requireString(body, "tool");
        String tier        = requireString(body, "tier");
        String agent       = optStrNull(body, "agent");
        String project     = optStrNull(body, "project");
        String targetTitle = optStrNull(body, "target_title");
        repo.recordTierWrite(tenant, sessionId, tsIso, tool, tier, agent, project, targetTitle);
        HttpUtil.send(ex, 200, json(Map.of("ok", true)));
    }

    private void handleTierWritesQuery(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        var params = queryParams(ex);
        String sessionId = params.getOrDefault("session_id", "");
        String since     = params.getOrDefault("since", "");
        int lastN = parseIntParam(params, "last_n", 0);
        var rows = repo.queryTierWrites(tenant, sessionId, since, lastN);
        HttpUtil.send(ex, 200, json(rows));
    }

    /**
     * GET /v1/telemetry/tier_writes/list (nexus-onjvy gap 4): per-row detail
     * including {@code target_title}, which {@link #handleTierWritesQuery}'s
     * aggregate cannot carry. Same filter params as {@code /tier_writes/query},
     * plus {@code limit} (default 100, mirrors {@link #handleHookFailureList}) —
     * review finding: this was the sole per-row list route in this handler with
     * no page cap, so an unfiltered call returned every tier_writes row ever
     * recorded for the tenant. Envelope mirrors {@code hook_failures/list}'s
     * capped-page-plus-exact-total discipline: {@code {rows, total}}.
     */
    private void handleTierWritesList(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        var params = queryParams(ex);
        String sessionId = params.getOrDefault("session_id", "");
        String since     = params.getOrDefault("since", "");
        int lastN = parseIntParam(params, "last_n", 0);
        int limit = parseIntParam(params, "limit", 100);
        var result = repo.listTierWrites(tenant, sessionId, since, lastN, limit);
        HttpUtil.send(ex, 200, json(result));
    }

    /**
     * GET /v1/telemetry/retention/markers?relations=a,b (nexus-24p05): the
     * verify-fill watermark's rollback detector. Absent relations are simply
     * omitted (never swept / fresh schema); the client treats absent as 0.
     */
    private void handleRetentionMarkers(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        String q = ex.getRequestURI().getQuery();
        java.util.List<String> relations = java.util.List.of();
        if (q != null) {
            for (String pair : q.split("&")) {
                if (pair.startsWith("relations=")) {
                    String raw = java.net.URLDecoder.decode(
                        pair.substring("relations=".length()), java.nio.charset.StandardCharsets.UTF_8);
                    relations = java.util.Arrays.stream(raw.split(","))
                        .map(String::trim).filter(sv -> !sv.isEmpty()).toList();
                }
            }
        }
        HttpUtil.send(ex, 200, json(Map.of("markers", repo.getRetentionMarkers(tenant, relations))));
    }

    // ── nx_answer_runs ─────────────────────────────────────────────────────────

    private void handleNxAnswerRunRecord(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        String question = requireString(body, "question");
        // Same number-or-string coercion as the /import path (nexus-5gaj7): never a
        // bare ((Number)) cast that 500s on a stringified numeric field.
        Long planId      = optLongNull(body, "plan_id");
        Double conf      = optDoubleNull(body, "matched_confidence");
        int stepCount    = optInt(body, "step_count", 0);
        String finalText = optStr(body, "final_text");
        // RDR-196 .p1c-b (nexus-lme1s): cost_usd stays nullable end to end — a client
        // "no usage observed" null must land as SQL NULL, never a fabricated 0.0
        // indistinguishable from a genuine free call (RDR-196 risk 1). No coercion here.
        Double cost      = optDoubleNull(body, "cost_usd");
        Long durationMs  = optLongNull(body, "duration_ms");
        long durationMsV = durationMs != null ? durationMs : 0L;
        String createdAt = optStr(body, "created_at");
        // RDR-196 .p1c (nexus-nyry9.9): OPTIONAL steps[] — absent/empty writes only
        // the parent, exactly as before this bead (the .p1d degradation contract).
        List<TelemetryRepository.StepInput> steps = parseNxAnswerSteps(body.get("steps"));
        repo.recordNxAnswerRun(tenant, question, planId, conf, stepCount, finalText, cost,
            durationMsV, createdAt, steps);
        HttpUtil.send(ex, 200, json(Map.of("ok", true)));
    }

    /**
     * Parse the optional {@code steps} array on
     * {@code POST /v1/telemetry/nx_answer_runs/record} (RDR-196 .p1c,
     * nexus-nyry9.9) into {@link TelemetryRepository.StepInput} rows.
     * {@code null}/absent yields an empty list — never a synthesized default
     * step. Each element must carry {@code operator}, {@code source}, and
     * {@code ok} (no silent default for a boolean telemetry field — the same
     * "no silent fallback for correctness" reasoning {@code requireBool}
     * already applies to consent records); {@code source}'s own closed-set
     * validity is enforced by the {@code nx_answer_steps_source_chk} CHECK at
     * the DB layer, surfaced as a typed 409 by
     * {@code HttpUtil.sendTypedDbError} rather than re-validated here.
     */
    @SuppressWarnings("unchecked")
    private List<TelemetryRepository.StepInput> parseNxAnswerSteps(Object raw) {
        if (raw == null) return List.of();
        if (!(raw instanceof List<?> rawList)) {
            throw new IllegalArgumentException("field 'steps' must be a JSON array");
        }
        List<TelemetryRepository.StepInput> steps = new ArrayList<>(rawList.size());
        for (Object o : rawList) {
            if (!(o instanceof Map<?, ?> rawStep)) {
                throw new IllegalArgumentException("each element of 'steps' must be a JSON object");
            }
            Map<String, Object> step = (Map<String, Object>) rawStep;
            // step_index feeds the (run_id, step_index) composite PK — a silent
            // default (the old optInt(step,"step_index",0)) let two omitted-
            // step_index rows in the same request collide on PK (run_id,0)
            // instead of 400ing the actual mistake (code-review finding,
            // 2026-08-20). No-silent-fallback-for-correctness, same reasoning
            // as 'ok' below.
            int stepIndex          = requireInt(step, "step_index");
            String operator       = requireString(step, "operator");
            String source         = requireString(step, "source");
            String model          = optStrNull(step, "model");
            Integer inputTokens   = optInt(step, "input_tokens");
            Integer outputTokens  = optInt(step, "output_tokens");
            // nexus-ndoke: a CACHED prompt reports input_tokens=2 with the real
            // size in one of these. Absent stays null — a stored 0 would read as
            // "used no cached input", a claim the run never made.
            Integer cacheRead     = optInt(step, "cache_read_input_tokens");
            Integer cacheCreation = optInt(step, "cache_creation_input_tokens");
            Double stepCostUsd    = optDoubleNull(step, "cost_usd");
            int elapsedMs         = optInt(step, "elapsed_ms", 0);
            boolean ok            = requireBool(step, "ok");
            List<Integer> bundledSteps = parseBundledSteps(step.get("bundled_steps"));
            steps.add(new TelemetryRepository.StepInput(stepIndex, operator, source, model,
                inputTokens, outputTokens, cacheRead, cacheCreation,
                stepCostUsd, elapsedMs, ok, bundledSteps));
        }
        return steps;
    }

    /** {@code bundled_steps}: a JSON array of integer plan indices, or absent (empty). */
    private List<Integer> parseBundledSteps(Object raw) {
        if (raw == null) return List.of();
        if (!(raw instanceof List<?> rawList)) {
            throw new IllegalArgumentException("field 'bundled_steps' must be a JSON array");
        }
        List<Integer> out = new ArrayList<>(rawList.size());
        for (Object o : rawList) {
            if (!(o instanceof Number n)) {
                throw new IllegalArgumentException("each element of 'bundled_steps' must be an integer");
            }
            out.add(n.intValue());
        }
        return out;
    }

    /**
     * GET /v1/telemetry/nx_answer_runs/query — the read half of nx_answer_runs
     * (nexus-eho3u: it was write-only — every nx_answer call recorded a row
     * and no route ever read one back). {@code ?since=<iso>} bounds the
     * window (absent/blank = no bound), {@code ?limit=N} caps the returned
     * page (default 20). Aggregates (total, hit/fallback split, latency
     * buckets, averages) are computed over the WHOLE filtered set, not the
     * page — see {@link TelemetryRepository#queryNxAnswerRuns}.
     *
     * <p>{@code ?include_steps=true} (RDR-196 .p1c-b, nexus-lme1s) — OPTIONAL,
     * defaults to {@code false} so the wire shape is unchanged when absent
     * (matches the write side's {@code steps[]} degradation contract). When
     * true, each row in the response gains a {@code steps} array using the
     * SAME field names {@code parseNxAnswerSteps} accepts on the write side,
     * RLS-scoped through the same tenant-stamped connection as the parent
     * query — a tenant can never see another tenant's steps.
     */
    private void handleNxAnswerRunsQuery(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        var params = queryParams(ex);
        String since = params.getOrDefault("since", "");
        // Clamped to MAX_QUERY_RUNS_LIMIT (never rejected with a 400) — see
        // that constant's javadoc: unbounded here compounds with
        // include_steps into an unbounded N runs x M steps page.
        int limit = Math.min(parseIntParam(params, "limit", 20), MAX_QUERY_RUNS_LIMIT);
        boolean includeSteps = "true".equalsIgnoreCase(params.get("include_steps"));
        HttpUtil.send(ex, 200, json(repo.queryNxAnswerRuns(tenant, since, limit, includeSteps)));
    }

    // ── hook_failures ──────────────────────────────────────────────────────────

    private void handleHookFailureRecord(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        String docId       = optStr(body, "doc_id");
        String collection  = optStr(body, "collection");
        String hookName    = requireString(body, "hook_name");
        String error       = optStr(body, "error");
        String occurredAt  = optStr(body, "occurred_at");
        String batchDocIds = optStrNull(body, "batch_doc_ids");
        boolean isBatch    = Boolean.TRUE.equals(body.get("is_batch"));
        String chain       = optStr(body, "chain");
        // nexus-nydpr: batch_doc_ids writes into a jsonb column now
        // (telemetry-004-type-hygiene.xml) — a non-blank value that fails
        // Jackson#readTree is not valid JSON and 400s here, same shared helper
        // AspectHandler/PlanHandler/TaxonomyHandler use.
        HttpUtil.rejectMalformedJson(MAPPER, "batch_doc_ids", batchDocIds);
        repo.recordHookFailure(tenant, docId, collection, hookName, error, occurredAt,
            batchDocIds, isBatch, chain);
        HttpUtil.send(ex, 200, json(Map.of("ok", true)));
    }

    /**
     * List hook failures, newest first (nexus-onjvy).
     *
     * <p>{@code ?days=N} bounds the window (0 = unbounded), {@code ?hook_name=a,b}
     * restricts to named hooks, {@code ?limit=N} caps the returned page. The
     * response carries {@code total} and {@code oldest_occurred_at} computed over
     * the WHOLE predicate rather than the page, because {@code nx doctor} reports
     * a count and would otherwise under-report whenever failures exceeded the
     * page size.
     */
    private void handleHookFailureList(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        var params = queryParams(ex);
        int days  = parseIntParam(params, "days", 0);
        int limit = parseIntParam(params, "limit", 100);
        String names = params.getOrDefault("hook_name", "");
        List<String> hookNames = names.isBlank()
            ? List.of()
            : java.util.Arrays.stream(names.split(","))
                .map(String::trim).filter(s -> !s.isBlank()).toList();
        HttpUtil.send(ex, 200, json(repo.getHookFailures(tenant, days, hookNames, limit)));
    }

    /**
     * POST /v1/telemetry/hook_failures/trim — same {@code dry_run} contract as
     * {@link #handleSearchTrim}, added alongside it so {@code nx doctor
     * --trim-telemetry --dry-run} cannot preview one trimmed table while
     * silently deleting the other.
     */
    private void handleHookFailureTrim(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        int days = optInt(body, "days", 30);
        boolean dryRun = Boolean.TRUE.equals(body.get("dry_run"));
        int deleted = repo.trimHookFailures(tenant, days, dryRun);
        HttpUtil.send(ex, 200, json(Map.of("deleted", deleted, "dry_run", dryRun)));
    }

    // ── index_failures (nexus-nukn3) ─────────────────────────────────────────────

    private void handleIndexFailureRecord(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        String runId      = optStr(body, "run_id");
        String filePath   = requireString(body, "file_path");
        String errorClass = optStr(body, "error_class");
        String error      = optStr(body, "error");
        String occurredAt = optStr(body, "occurred_at");
        repo.recordIndexFailure(tenant, runId, filePath, errorClass, error, occurredAt);
        HttpUtil.send(ex, 200, json(Map.of("ok", true)));
    }

    /**
     * POST /v1/telemetry/index_failures/record_batch — one transaction for every
     * file skipped this run (mirrors {@link #handleSearchBatch}'s row-tuple shape).
     * Row layout: {@code [run_id, file_path, error_class, error, occurred_at]}.
     */
    @SuppressWarnings("unchecked")
    private void handleIndexFailureRecordBatch(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        var rows = (List<List<Object>>) body.getOrDefault("rows", List.of());
        List<Object[]> tuples = rows.stream()
            .map(r -> r.toArray(Object[]::new))
            .toList();
        int count = repo.recordIndexFailuresBatch(tenant, tuples);
        HttpUtil.send(ex, 200, json(Map.of("inserted", count)));
    }

    /**
     * List index failures, newest first (nexus-nukn3). {@code ?run_id=} scopes to
     * one run (blank/absent = all runs), {@code ?days=N} bounds the window (0 =
     * unbounded), {@code ?limit=N} caps the returned page. {@code total} is
     * computed over the WHOLE predicate — see {@link TelemetryRepository#getIndexFailures}.
     */
    private void handleIndexFailureList(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        var params = queryParams(ex);
        String runId = params.getOrDefault("run_id", "");
        int days  = parseIntParam(params, "days", 0);
        int limit = parseIntParam(params, "limit", 100);
        HttpUtil.send(ex, 200, json(repo.getIndexFailures(tenant, runId, days, limit)));
    }

    /**
     * POST /v1/telemetry/index_failures/trim — the {@code nx index failures --clear}
     * remedy (nexus-nukn3 fold-in, critic Critical finding). Body: {@code run_id}
     * and/or {@code days}; AT LEAST ONE is required — refusing here (400) is what
     * keeps an unscoped body from silently clearing a tenant's entire history,
     * since {@link TelemetryRepository#trimIndexFailures} itself has no opinion
     * (see that method's own javadoc).
     */
    private void handleIndexFailureTrim(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        String runId = optStr(body, "run_id");
        int days = optInt(body, "days", 0);
        if (runId.isBlank() && days <= 0) {
            throw new IllegalArgumentException(
                "index_failures/trim requires run_id and/or days >= 1 -- refusing an "
                + "unscoped clear of the entire tenant history");
        }
        int deleted = repo.trimIndexFailures(tenant, runId, days);
        HttpUtil.send(ex, 200, json(Map.of("deleted", deleted)));
    }

    // ── capability_census (nexus-gjv9b PART 1) ──────────────────────────────────

    /**
     * POST /v1/telemetry/capability_census/record — upsert on
     * {@code (tenant_id, session_id)}. {@code capabilities} is an object
     * keyed by the 8-value vocabulary ({@code skill}, {@code agent},
     * {@code serena}, {@code nx_answer}, {@code search_query},
     * {@code other_nx_mcp}, {@code baseline}, {@code other}); omitted
     * entirely (or {@code null}) on a {@code blindspot=true} record, which
     * this handler never rejects — a census that could not measure the
     * transcript still records THAT fact.
     */
    private void handleCapabilityCensusRecord(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        String sessionId = requireString(body, "session_id");
        String ts        = optStr(body, "ts");
        boolean blindspot = Boolean.TRUE.equals(body.get("blindspot"));
        String unmeasurableReason = optStrNull(body, "unmeasurable_reason");
        Map<String, Integer> capabilities = new java.util.LinkedHashMap<>();
        Object rawCaps = body.get("capabilities");
        if (rawCaps instanceof Map<?, ?> m) {
            for (var e : m.entrySet()) {
                if (e.getKey() instanceof String k && e.getValue() instanceof Number n) {
                    capabilities.put(k, n.intValue());
                }
            }
        }
        Integer dispatches = optInt(body, "dispatches");
        Integer totalCalls = optInt(body, "total_calls");
        repo.recordCapabilityCensus(tenant, sessionId, ts, blindspot, unmeasurableReason,
            capabilities, dispatches, totalCalls);
        HttpUtil.send(ex, 200, json(Map.of("ok", true)));
    }

    /**
     * GET /v1/telemetry/capability_census/query — the read half of
     * {@code nx census capability --from-store}. {@code ?session_id=}
     * returns at most one row; else {@code ?since=} bounds by {@code ts};
     * else every row for the tenant, capped by {@code ?limit=} (default
     * 100).
     */
    private void handleCapabilityCensusQuery(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        var params = queryParams(ex);
        String sessionId = params.getOrDefault("session_id", "");
        String since      = params.getOrDefault("since", "");
        int limit = parseIntParam(params, "limit", 100);
        HttpUtil.send(ex, 200, json(Map.of("rows", repo.queryCapabilityCensus(tenant, sessionId, since, limit))));
    }

    // ── routing_events (nexus-gjv9b PART 2) ─────────────────────────────────────

    /**
     * POST /v1/telemetry/routing_events/record — append one routing-hook
     * event. Called by the standalone (no-nexus-import) routing hooks via
     * urllib with a short (~250ms) timeout; a failure here is the caller's
     * cue to fall back to a metered local drop, never to retry.
     */
    private void handleRoutingEventRecord(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        String ts               = optStr(body, "ts");
        String sessionId        = optStr(body, "session_id");
        String rule             = requireString(body, "rule");
        String outcome          = requireString(body, "outcome");
        String toolName         = optStr(body, "tool_name");
        String commandFragment  = optStr(body, "command_fragment");
        String escapeReason     = optStr(body, "escape_reason");
        repo.recordRoutingEvent(tenant, ts, sessionId, rule, outcome, toolName, commandFragment, escapeReason);
        HttpUtil.send(ex, 200, json(Map.of("ok", true)));
    }

    /**
     * POST /v1/telemetry/routing_events/batch — {@code {events: [...]}},
     * each entry shaped like the single-event body above. Returns
     * {@code {inserted: N}}.
     */
    private void handleRoutingEventsBatch(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        Object rawEvents = body.get("events");
        var events = new ArrayList<TelemetryRepository.RoutingEventInput>();
        if (rawEvents instanceof List<?> list) {
            for (Object o : list) {
                if (!(o instanceof Map<?, ?> m)) continue;
                @SuppressWarnings("unchecked")
                Map<String, Object> e = (Map<String, Object>) m;
                events.add(new TelemetryRepository.RoutingEventInput(
                    optStr(e, "ts"), optStr(e, "session_id"),
                    requireString(e, "rule"), requireString(e, "outcome"),
                    optStr(e, "tool_name"), optStr(e, "command_fragment"),
                    optStr(e, "escape_reason")));
            }
        }
        int inserted = repo.recordRoutingEventsBatch(tenant, events);
        HttpUtil.send(ex, 200, json(Map.of("inserted", inserted)));
    }

    /**
     * GET /v1/telemetry/routing_events/list — the read half of
     * {@code nexus.routing_stats.aggregate}/{@code escape_events}
     * (nexus-gjv9b PART 2). {@code ?since=} bounds by {@code ts};
     * {@code ?limit=} caps the page (default 1000 — routing-hook events
     * fire far more often than hook_failures/tier_writes, and a soak
     * review reads the whole recent window at once).
     */
    private void handleRoutingEventsList(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        var params = queryParams(ex);
        String since = params.getOrDefault("since", "");
        int limit = parseIntParam(params, "limit", 1000);
        HttpUtil.send(ex, 200, json(Map.of("rows", repo.listRoutingEvents(tenant, since, limit))));
    }

    // ── frecency ───────────────────────────────────────────────────────────────

    private void handleFrecencyUpsert(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        // nexus-lgdel.l1: see handleRelevanceLog's identical comment above.
        String chunkId       = dev.nexus.service.db.Chash.requireCanonical(
            requireString(body, "chunk_id"), "'chunk_id'");
        String embeddedAt    = optStr(body, "embedded_at");
        // nexus-tk070.p6b (RDR-194 D5): live single-row path — boundary-
        // validated (400), matching MemoryHandler's /put treatment. Omitted
        // means permanent (null); an explicit ttl_days<=0 is rejected loud.
        Integer ttlDays       = requirePositiveOrNullTtlDays(optInt(body, "ttl_days"));
        double frecencyScore = body.get("frecency_score") != null
            ? ((Number) body.get("frecency_score")).doubleValue() : 0.0;
        int missCount        = optInt(body, "miss_count", 0);
        String lastHitAt     = optStr(body, "last_hit_at");
        repo.upsertFrecency(tenant, chunkId, embeddedAt, ttlDays, frecencyScore, missCount, lastHitAt);
        HttpUtil.send(ex, 200, json(Map.of("ok", true)));
    }

    private void handleFrecencyGet(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        var params  = queryParams(ex);
        String chunkId = params.getOrDefault("chunk_id", "");
        if (chunkId.isBlank()) {
            HttpUtil.send(ex, 400, json(Map.of("error", "chunk_id required")));
            return;
        }
        var result = repo.getFrecency(tenant, chunkId);
        if (result.isEmpty()) {
            HttpUtil.send(ex, 404, json(Map.of("error", "not found")));
        } else {
            HttpUtil.send(ex, 200, json(result.get()));
        }
    }

    // ── import (ETL fidelity-preserving, all tables) ───────────────────────────

    /**
     * Fidelity-preserving bulk import endpoint (ETL path).
     *
     * <p>Request body is a JSON object with optional arrays for each table:
     * <pre>
     * {
     *   "table": "relevance_log" | "search_telemetry" | "tier_writes" |
     *            "nx_answer_runs" | "hook_failures" | "frecency",
     *   ... table-specific fields ...
     * }
     * </pre>
     *
     * <p>One row per request for simplicity (matching the .8/.11 per-row HTTP pattern).
     * Each row is dispatched to the correct import method based on the {@code table} field.
     */
    private void handleImport(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body  = readBody(ex);
        String table = requireString(body, "table");

        switch (table) {
            case "relevance_log" -> {
                repo.importRelevanceRow(tenant,
                    requireString(body, "query"),
                    // nexus-lgdel.l1: see handleRelevanceLog's identical comment.
                    dev.nexus.service.db.Chash.requireCanonical(
                        requireString(body, "chunk_id"), "'chunk_id'"),
                    optStr(body, "collection"),
                    requireString(body, "action"),
                    optStr(body, "session_id"),
                    requireString(body, "timestamp"));
            }
            case "search_telemetry" -> {
                Double topDist = optDoubleNull(body, "top_distance");
                Double threshold = optDoubleNull(body, "threshold");
                Long rawCount = optLongNull(body, "raw_count");
                Long keptCount = optLongNull(body, "kept_count");
                if (rawCount == null) throw new IllegalArgumentException("Missing required field: raw_count");
                if (keptCount == null) throw new IllegalArgumentException("Missing required field: kept_count");
                repo.importSearchRow(tenant,
                    requireString(body, "ts"),
                    requireString(body, "query_hash"),
                    requireString(body, "collection"),
                    rawCount.intValue(),
                    keptCount.intValue(),
                    topDist, threshold);
            }
            case "tier_writes" -> {
                repo.importTierWriteRow(tenant,
                    requireString(body, "session_id"),
                    requireString(body, "ts"),
                    requireString(body, "tool"),
                    requireString(body, "tier"),
                    optStrNull(body, "agent"),
                    optStrNull(body, "project"),
                    optStrNull(body, "target_title"));
            }
            case "nx_answer_runs" -> {
                // plan_id is a BIGINT; the SQLite ETL historically serialized it as a
                // string, causing String->Number ClassCastException (nexus-5gaj7).
                // optLongNull / optDoubleNull accept either a number or a numeric string.
                Long planId = optLongNull(body, "plan_id");
                Double conf = optDoubleNull(body, "matched_confidence");
                // cost_usd: fidelity-preserving import must not coerce an absent/null
                // source value to 0.0 (RDR-196 .p1c-b, nexus-lme1s, risk 1) — pass the
                // nullable Double straight through.
                Double cost = optDoubleNull(body, "cost_usd");
                Long durationMs = optLongNull(body, "duration_ms");
                repo.importNxAnswerRunRow(tenant,
                    requireString(body, "question"),
                    planId, conf,
                    optInt(body, "step_count", 0),
                    optStr(body, "final_text"),
                    cost,
                    durationMs != null ? durationMs : 0L,
                    requireString(body, "created_at"));
            }
            case "hook_failures" -> {
                String batchDocIds = optStrNull(body, "batch_doc_ids");
                // nexus-nydpr: see handleHookFailureRecord's identical comment above.
                HttpUtil.rejectMalformedJson(MAPPER, "batch_doc_ids", batchDocIds);
                repo.importHookFailureRow(tenant,
                    optStr(body, "doc_id"),
                    optStr(body, "collection"),
                    requireString(body, "hook_name"),
                    optStr(body, "error"),
                    requireString(body, "occurred_at"),
                    batchDocIds,
                    Boolean.TRUE.equals(body.get("is_batch")),
                    optStr(body, "chain"));
            }
            case "frecency" -> {
                Double frecScore = optDoubleNull(body, "frecency_score");
                repo.upsertFrecency(tenant,
                    // nexus-lgdel.l1: see handleRelevanceLog's identical comment.
                    dev.nexus.service.db.Chash.requireCanonical(
                        requireString(body, "chunk_id"), "'chunk_id'"),
                    optStr(body, "embedded_at"),
                    // nexus-tk070.p6b (RDR-194 D5): ETL path — carries an
                    // explicit ttl_days verbatim (including an explicit 0,
                    // which then hits the CHECK), but an OMITTED field now
                    // defaults to null (permanent), not 0 (retired). Not
                    // boundary-validated — see requirePositiveOrNullTtlDays's
                    // javadoc for the scope decision.
                    optInt(body, "ttl_days"),
                    frecScore != null ? frecScore : 0.0,
                    optInt(body, "miss_count", 0),
                    optStr(body, "last_hit_at"));
            }
            default -> throw new IllegalArgumentException("Unknown table: " + table);
        }

        HttpUtil.send(ex, 200, json(Map.of("ok", true)));
    }

    /**
     * POST /v1/telemetry/import_batch — RDR-176 P3 (Gap 1, bead nexus-t9rmg.18).
     *
     * <p>Body {@code {"table": "<one of the six>", "rows": [ {<table fields>}, … ]}}.
     * The whole batch for one table lands under ONE tenant transaction (GUC set
     * once) via {@link dev.nexus.service.db.TelemetryRepository#importBatch}.
     * Response 200 {@code {"imported": <int>}}.
     */
    private void handleImportBatch(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        String table = requireString(body, "table");
        Object rowsObj = body.get("rows");
        if (!(rowsObj instanceof List<?> rawRows)) {
            throw new IllegalArgumentException("field 'rows' must be a JSON array");
        }
        List<Map<String, Object>> rows = new ArrayList<>(rawRows.size());
        int rowIndex = 0;
        for (Object o : rawRows) {
            if (!(o instanceof Map<?, ?> rm)) {
                throw new IllegalArgumentException("each element of 'rows' must be an object");
            }
            @SuppressWarnings("unchecked")
            Map<String, Object> row = (Map<String, Object>) rm;
            // nexus-nydpr: the "hook_failures" table's batch_doc_ids field writes into
            // a jsonb column now (telemetry-004-type-hygiene.xml) — validate it
            // here too, same as the single-row /import path, naming the failing row
            // (mirrors TaxonomyHandler.handleImportBatch's rows[i] convention). The
            // value is coerced through optStrNull first, exactly as the single-row
            // /hook_failures/record and /import paths do, so a non-string
            // batch_doc_ids (a native JSON array/object) is validated as its string
            // form rather than skipping the gate (rejectMalformedJson only inspects
            // Strings).
            if ("hook_failures".equals(table)) {
                try {
                    HttpUtil.rejectMalformedJson(MAPPER, "batch_doc_ids", optStrNull(row, "batch_doc_ids"));
                } catch (IllegalArgumentException e) {
                    throw new IllegalArgumentException("rows[" + rowIndex + "]: " + e.getMessage(), e);
                }
            }
            rows.add(row);
            rowIndex++;
        }
        int imported = repo.importBatch(tenant, table, rows);
        HttpUtil.send(ex, 200, json(Map.of("imported", imported)));
    }

    /**
     * POST /v1/telemetry/ids/probe — RDR-178 wave-2 P1 (bead nexus-s3dd4.3).
     *
     * <p>Membership-probe identity endpoint for the verify-fill inner loop: the
     * caller sends up to {@link #MAX_PROBE_KEYS} candidate conflict-key tuples
     * for one of the six telemetry tables; the response is the subset already
     * present, echoed back verbatim (never reconstructed from the stored
     * values — see {@link TelemetryRepository#probeIds}).
     *
     * <p>Body {@code {"table": "<one of the six>", "keys": [[...], ...]}}.
     * Response 200 {@code {"present": [[...], ...]}}. A batch larger than
     * {@value #MAX_PROBE_KEYS} is rejected with HTTP 400 rather than silently
     * truncated (chroma_quotas-style batch discipline).
     */
    private void handleIdsProbe(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        String table = requireString(body, "table");
        Object keysObj = body.get("keys");
        if (!(keysObj instanceof List<?> rawKeys)) {
            throw new IllegalArgumentException("field 'keys' must be a JSON array");
        }
        if (rawKeys.size() > MAX_PROBE_KEYS) {
            throw new IllegalArgumentException(
                "field 'keys' exceeds max batch size of " + MAX_PROBE_KEYS + " (got " + rawKeys.size() + ")");
        }
        List<List<Object>> keys = new ArrayList<>(rawKeys.size());
        for (Object o : rawKeys) {
            if (!(o instanceof List<?> keyList)) {
                throw new IllegalArgumentException("each element of 'keys' must be a JSON array");
            }
            @SuppressWarnings("unchecked")
            List<Object> key = (List<Object>) keyList;
            keys.add(key);
        }
        List<List<Object>> present = repo.probeIds(tenant, table, keys);
        HttpUtil.send(ex, 200, json(Map.of("present", present)));
    }

    // ── Helpers ────────────────────────────────────────────────────────────────

    private String json(Object obj) {
        try {
            return MAPPER.writeValueAsString(obj);
        } catch (Exception e) {
            log.error("event=telemetry_json_serialize_error", e);
            return "{\"error\":\"serialization failed\"}";
        }
    }

    private Map<String, Object> readBody(HttpExchange ex) throws IOException {
        try (InputStream is = ex.getRequestBody()) {
            String text = new String(is.readAllBytes(), StandardCharsets.UTF_8);
            if (text.isBlank()) return Map.of();
            return MAPPER.readValue(text, MAP_TYPE);
        }
    }

    private String requireString(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (v == null || v.toString().isBlank()) {
            throw new IllegalArgumentException("Missing required field: " + key);
        }
        return v.toString();
    }

    /**
     * Like {@link #requireString} but for an int field — used where a
     * numeric wire field feeds identity/PK material and a silent default
     * (as {@link #optInt(Map, String, int)} would produce) would corrupt
     * that identity rather than surface the caller's actual mistake
     * (RDR-196 .p1c step_index, code-review 2026-08-20). Reuses {@link
     * #optInt(Map, String)}'s number-or-numeric-string coercion and its
     * IllegalArgumentException for a genuinely malformed value; only the
     * "absent" case gets a distinct, field-naming message here.
     */
    private int requireInt(Map<String, Object> body, String key) {
        Integer v = optInt(body, key);
        if (v == null) {
            throw new IllegalArgumentException("Missing required field: " + key);
        }
        return v;
    }

    private Object requireObj(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (v == null) throw new IllegalArgumentException("Missing required field: " + key);
        return v;
    }

    /** Strict boolean read (RDR-182 consent audit): a JSON bool, or the
     *  strings "true"/"false" a lenient client might send. Anything else
     *  is a 400 — never a silent default (the consent trail must be honest
     *  about grant vs revoke). */
    private boolean requireBool(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (v instanceof Boolean b) return b;
        if (v != null) {
            String s = v.toString().trim().toLowerCase();
            if (s.equals("true")) return true;
            if (s.equals("false")) return false;
        }
        throw new IllegalArgumentException("Field must be a boolean: " + key);
    }

    private String optStr(Map<String, Object> body, String key) {
        Object v = body.get(key);
        return v != null ? v.toString() : "";
    }

    private String optStrNull(Map<String, Object> body, String key) {
        Object v = body.get(key);
        return v != null ? v.toString() : null;
    }

    private int optInt(Map<String, Object> body, String key, int defaultVal) {
        Object v = body.get(key);
        if (v == null) return defaultVal;
        if (v instanceof Number n) return n.intValue();
        // A numeric string is coerced (same ETL-stringification class as optLongNull);
        // a non-numeric string throws (HTTP 400) rather than silently returning the
        // default, which would corrupt step_count / ttl_days / miss_count telemetry.
        if (v instanceof String s) {
            String t = s.trim();
            if (t.isEmpty()) return defaultVal;
            try {
                return Integer.parseInt(t);
            } catch (NumberFormatException e) {
                throw new IllegalArgumentException(
                    "Field '" + key + "' is not a valid integer: " + s);
            }
        }
        throw new IllegalArgumentException(
            "Field '" + key + "' has unexpected type: " + v.getClass().getSimpleName());
    }

    /**
     * Nullable {@code Integer} read — {@code null} when the field is absent.
     * A genuine 2-arg overload of {@link #optInt(Map, String, int)} (code-
     * review cosmetic finding, 2026-08-20 fix-pass) — matches
     * {@code PlanHandler}/{@code MemoryHandler}'s own no-default
     * {@code optInt(Map, String)} exactly, name and all, rather than a
     * differently-named method the earlier docstring only claimed
     * "mirrored" them (nexus-tk070.p6b, RDR-194 D5): unlike the 3-arg
     * overload, an omitted field must NOT collapse onto a numeric default
     * here — for {@code ttl_days}, {@code 0} is the now-retired sentinel,
     * so silently defaulting to it would immediately fail the CHECK for
     * every caller that simply left the field out.
     */
    private Integer optInt(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (v == null) return null;
        if (v instanceof Number n) return n.intValue();
        if (v instanceof String s) {
            String t = s.trim();
            if (t.isEmpty()) return null;
            try {
                return Integer.parseInt(t);
            } catch (NumberFormatException e) {
                throw new IllegalArgumentException(
                    "Field '" + key + "' is not a valid integer: " + s);
            }
        }
        throw new IllegalArgumentException(
            "Field '" + key + "' has unexpected type: " + v.getClass().getSimpleName());
    }

    /**
     * Boundary rejection for {@code ttl_days} on the live single-row
     * {@code POST /v1/telemetry/frecency/upsert} path (nexus-tk070.p6b,
     * RDR-194 D5) — mirrors {@code MemoryHandler.requirePositiveOrNullTtl}
     * exactly (same message shape, same {@code null}-or-positive contract),
     * naming the field {@code ttl_days} rather than {@code ttl} since that
     * is this table's actual wire field name. Deliberately NOT applied to
     * the ETL import paths ({@code handleImportRow}'s {@code "frecency"}
     * case, {@code TelemetryRepository.importFrecencyBatch}) — those carry
     * source values verbatim (their default moved from {@code 0} to
     * {@code null} instead, a default-value fix, not new rejection logic;
     * see {@code TelemetryRepository.optInteger}'s javadoc), so an explicit
     * {@code ttl_days=0} row from an ETL source still reaches the CHECK and
     * surfaces as a 409 with a remedy (HttpUtil.ttlDaysCheckRemedy already
     * wildcard-matches {@code frecency_ttl_days_positive_chk}) — same scope
     * boundary p6a drew for {@code MemoryHandler.parseImportRow}.
     */
    private static Integer requirePositiveOrNullTtlDays(Integer ttlDays) {
        if (ttlDays != null && ttlDays <= 0) {
            throw new IllegalArgumentException(
                "ttl_days=" + ttlDays + " is invalid: omit the field or pass null for a "
                + "permanent entry — ttl_days must be a positive integer number of "
                + "days (0 does NOT mean permanent; NULL does)");
        }
        return ttlDays;
    }

    /**
     * Coerce an optional numeric field to Long, accepting either a JSON number or a
     * numeric string. Defensive against ETL payloads that serialize an INTEGER column
     * as a string (the nx_answer_runs.plan_id ClassCastException class). Returns null
     * for absent/blank values; throws IllegalArgumentException (HTTP 400, not a 500
     * crash) for a non-numeric string.
     */
    private Long optLongNull(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (v == null) return null;
        if (v instanceof Number n) return n.longValue();
        if (v instanceof String s) {
            String t = s.trim();
            if (t.isEmpty()) return null;
            try {
                return Long.parseLong(t);
            } catch (NumberFormatException e) {
                throw new IllegalArgumentException(
                    "Field '" + key + "' is not a valid integer: " + s);
            }
        }
        throw new IllegalArgumentException(
            "Field '" + key + "' has unexpected type: " + v.getClass().getSimpleName());
    }

    /**
     * Coerce an optional numeric field to Double, accepting a JSON number or numeric
     * string. Companion to {@link #optLongNull} for the same ETL-stringification class.
     */
    private Double optDoubleNull(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (v == null) return null;
        if (v instanceof Number n) return n.doubleValue();
        if (v instanceof String s) {
            String t = s.trim();
            if (t.isEmpty()) return null;
            try {
                return Double.parseDouble(t);
            } catch (NumberFormatException e) {
                throw new IllegalArgumentException(
                    "Field '" + key + "' is not a valid number: " + s);
            }
        }
        throw new IllegalArgumentException(
            "Field '" + key + "' has unexpected type: " + v.getClass().getSimpleName());
    }

    private Map<String, String> queryParams(HttpExchange ex) {
        String query = ex.getRequestURI().getQuery();
        if (query == null || query.isBlank()) return Map.of();
        var map = new java.util.HashMap<String, String>();
        for (String pair : query.split("&")) {
            int eq = pair.indexOf('=');
            if (eq > 0) {
                map.put(java.net.URLDecoder.decode(pair.substring(0, eq), StandardCharsets.UTF_8),
                        java.net.URLDecoder.decode(pair.substring(eq + 1), StandardCharsets.UTF_8));
            }
        }
        return map;
    }

    private int parseIntParam(Map<String, String> params, String key, int def) {
        String v = params.get(key);
        if (v == null || v.isBlank()) return def;
        try { return Integer.parseInt(v); } catch (NumberFormatException e) { return def; }
    }

    private void requireMethod(HttpExchange ex, String actual, String expected) throws IOException {
        if (!actual.equals(expected)) {
            HttpUtil.send(ex, 405, "{\"error\":\"method not allowed\"}");
            throw new IllegalArgumentException("method not allowed");
        }
    }
}
