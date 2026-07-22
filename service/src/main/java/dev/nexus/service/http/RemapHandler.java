package dev.nexus.service.http;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.RemapRepository;
import dev.nexus.service.db.RemapRepository.RemapEntry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * RDR-186 bead nexus-146xx.4 — chash_remap HTTP endpoints.
 *
 * <p>Routes (all under {@code /v1/remap/}):
 * <pre>
 *   POST /v1/remap/rekey          RDR-180 per-tenant full-digest rekey (nexus-jxizy.6)
 *                                 ASYNC (nexus-b878d): 202 + {job_id}, never the
 *                                 envelope — the synchronous form outlived the
 *                                 proxy's read timeout and 504'd over a
 *                                 committed transaction
 *   GET  /v1/remap/rekey/{job_id} poll a submitted rekey: running / succeeded
 *                                 (+envelope) / failed; 410 if the engine has
 *                                 restarted since the id was minted
 *   POST /v1/remap/record_batch   persist a batch of old-id → new-chash facts
 *                                 {source_collection, entries:[{old_id, new_chash,
 *                                  target_collection, provenance}]} → {recorded}
 *   POST /v1/remap/clear_leg      the rollback absence-encoding (D2): clear ONE
 *                                 leg's map rows {source_collection,
 *                                  target_collection} → {deleted}. BOTH fields
 *                                 required — a leg is the (source, target) pair
 *                                 (co-residency: a wide clear would delete a
 *                                 sibling leg's claims)
 *   GET  /v1/remap/membership     live leg-convergence counts (bead .5 function):
 *                                 ?source_collection=&amp;target_collection=
 *                                 → {mapped_total, present_count}
 *   GET  /v1/remap/entries        one source collection's facts (bead .6/.8 read
 *                                 shape): ?source_collection= → {entries:
 *                                 [{old_id, new_chash, target_collection}]}
 *   GET  /v1/remap/pairs          paged global (old_id, new_chash) view — the
 *                                 remap cascade's all_pairs input:
 *                                 ?limit=&amp;offset= → {pairs: [[old, new], ...]}
 *   GET  /v1/remap/source_collections  distinct sources — the prior-collections
 *                                 (source-gone) probe input
 *   GET  /v1/remap/count          total fact rows (?source_collection= optional
 *                                 filter) → {total} — the probe-before-fetch
 *                                 short-circuit + paged-read reconcile input
 * </pre>
 *
 * <p>RF-186-1: raw facts and live counts only — no verdict surface exists and
 * none may be added. The membership response is a pair of counts the CLIENT
 * rung interprets (converged iff equal, including 0 == 0), computed fresh by
 * {@code nexus.remap_membership()} on every call.
 *
 * <p>Batch bound: {@link RemapRepository#MAX_BATCH} (300) entries per
 * record_batch call — the chroma_quotas MAX_RECORDS_PER_WRITE heritage cap;
 * oversized batches get 400, matching the client's existing paging contract.
 *
 * <p>new_chash validation (RDR-180, nexus-jxizy.7): the FULL 64-hex digest
 * is the canonical fact form, parsed through {@code Chash.requireCanonical}
 * — nothing is truncated; any other width is rejected 400. Pre-flip 32-hex
 * era facts already persisted stay readable (widened DB CHECK + the
 * remap_membership alias chain).
 *
 * <p>All endpoints require {@code Authorization: Bearer} (enforced by
 * {@link AuthFilter}) and {@code X-Nexus-Tenant}.
 */
public final class RemapHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(RemapHandler.class);

    static final ObjectMapper MAPPER = new ObjectMapper()
            .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false);

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    private final RemapRepository repo;
    private final RekeyJobs rekeyJobs;

    public RemapHandler(RemapRepository repo, RekeyJobs rekeyJobs) {
        this.repo = repo;
        this.rekeyJobs = rekeyJobs;
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        String tenant = RequestContext.tenant();
        if (tenant == null) {
            HttpUtil.send(exchange, 500, "{\"error\":\"internal: tenant not set\"}");
            return;
        }

        String path   = exchange.getRequestURI().getPath();
        String op     = path.replaceFirst("^/v1/remap", "");
        String method = exchange.getRequestMethod().toUpperCase(Locale.ROOT);

        try {
            // The rekey poll carries its job id in the path, so it cannot be an
            // exact-match arm: /rekey/<epoch>-<uuid>.
            if (op.startsWith("/rekey/")) {
                handleRekeyStatus(exchange, tenant, method, op.substring("/rekey/".length()));
                return;
            }
            switch (op) {
                case "/rekey"              -> handleRekey(exchange, tenant, method);
                case "/record_batch"       -> handleRecordBatch(exchange, tenant, method);
                case "/clear_leg"          -> handleClearLeg(exchange, tenant, method);
                case "/membership"         -> handleMembership(exchange, tenant, method);
                case "/entries"            -> handleEntries(exchange, tenant, method);
                case "/pairs"              -> handlePairs(exchange, tenant, method);
                case "/source_collections" -> handleSourceCollections(exchange, tenant, method);
                case "/count"              -> handleCount(exchange, tenant, method);
                default                    -> HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}");
            }
        } catch (IllegalArgumentException e) {
            HttpUtil.send(exchange, 400, "{\"error\":" + MAPPER.writeValueAsString(e.getMessage()) + "}");
        } catch (Exception e) {
            if (!HttpUtil.sendTypedDbError(exchange, e, log, "remap_handler",
                    "op=" + op + " tenant=" + tenant)) {
                log.error("event=remap_handler_error op={} tenant={} error={}",
                        op, tenant, e.getMessage(), e);
                HttpUtil.send(exchange, 500, "{\"error\":\"internal server error\"}");
            }
        }
    }

    // ── POST /v1/remap/rekey ─────────────────────────────────────────────────

    /**
     * RDR-180 per-tenant full-digest rekey (nexus-jxizy.6) — see
     * {@link dev.nexus.service.db.RekeyOps}. Body (optional):
     * {@code {"orphan_policy": "drop"|"synthesize"}} (default drop).
     *
     * <p><strong>Asynchronous (nexus-b878d).</strong> Returns {@code 202} with a
     * {@code job_id} immediately; the envelope is collected from
     * {@code GET /v1/remap/rekey/{job_id}}. This is not a mode — it is the only
     * shape — because the synchronous form could not survive a proxy: the rekey
     * ran ~90s+ at production scale against an nginx {@code proxy_read_timeout}
     * of ~120s, and gate-xr789 took a 504 at 120.3s while the transaction
     * COMMITTED 88s later. An operator who sees a failure over a store that did
     * change is the GH #1390 hazard class, so the long-held request is gone
     * rather than merely lengthened.
     *
     * <p>A second submission while one is in flight for the tenant returns
     * {@code 409} naming the running job, rather than queueing behind the
     * per-tenant advisory lock.
     */
    private void handleRekey(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String policy = body.get("orphan_policy") instanceof String s ? s : "drop";
        if (!"drop".equals(policy) && !"synthesize".equals(policy)) {
            HttpUtil.send(exchange, 400,
                "{\"error\":\"orphan_policy must be 'drop' or 'synthesize'\"}");
            return;
        }
        try {
            String jobId = rekeyJobs.submit(tenant, "synthesize".equals(policy));
            HttpUtil.send(exchange, 202, MAPPER.writeValueAsString(Map.of(
                "job_id", jobId,
                "status", "running",
                "poll", "/v1/remap/rekey/" + jobId)));
        } catch (RekeyJobs.AlreadyRunningException e) {
            HttpUtil.send(exchange, 409, MAPPER.writeValueAsString(Map.of(
                "error", e.getMessage(),
                "job_id", e.runningJobId())));
        }
    }

    // ── GET /v1/remap/rekey/{job_id} ─────────────────────────────────────────

    /**
     * Poll a submitted rekey (nexus-b878d). Fast by construction — it reads an
     * in-memory registry — so no proxy read timeout is in play.
     *
     * <pre>
     *   200 {status:"running"}
     *   200 {status:"succeeded", envelope:{...}}   the RekeyOps counts envelope
     *   200 {status:"failed",    error:"..."}      the run threw
     *   409 {status:"failed",    error:"..."}      legacy-id collision (one old
     *                                              id, two digests) — never
     *                                              resolved silently, same
     *                                              contract the sync form had
     *   410 engine restarted: that transaction rolled back, store unchanged
     *   404 unknown job for this tenant
     *   400 malformed job id
     * </pre>
     *
     * <p>The job id is tenant-scoped on read: a job belonging to another tenant
     * reads as 404, so holding an id is not a way to observe another tenant's
     * rekey.
     */
    private void handleRekeyStatus(HttpExchange exchange, String tenant, String method, String jobId)
            throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }

        switch (rekeyJobs.lookup(jobId)) {
            case RekeyJobs.Lookup.Malformed ignored -> HttpUtil.send(exchange, 400,
                "{\"error\":\"malformed job id\"}");

            // A job id minted before a restart. This is NOT 404 — the id is
            // well-formed and was real — but it is also NOT a claim that the
            // store is unchanged. The commit and the registry's record of it
            // are two steps, so an ill-timed death can leave a committed rekey
            // that never reached SUCCEEDED; asserting "unchanged" here would
            // re-commit the very sin this endpoint was rebuilt to remove.
            // Report unknown, and name what actually settles it.
            case RekeyJobs.Lookup.ForeignEpoch fe -> HttpUtil.send(exchange, 410,
                MAPPER.writeValueAsString(Map.of(
                    "error", "job belongs to a previous engine instance (epoch " + fe.jobEpoch()
                             + ", current " + rekeyJobs.epoch() + "): the engine restarted and "
                             + "this job's outcome is not in the current instance's memory. It "
                             + "most likely rolled back, but that is NOT guaranteed — the store "
                             + "may or may not have changed. The server-side event=rekey_complete "
                             + "log is the authoritative record of what it did. The rekey is "
                             + "idempotent, so re-submitting is safe and self-answering: over an "
                             + "already-rekeyed store it reports all-zero counts.",
                    "status", "lost",
                    "store_changed", "unknown")));

            case RekeyJobs.Lookup.Unknown ignored -> HttpUtil.send(exchange, 404,
                "{\"error\":\"unknown job id\"}");

            case RekeyJobs.Lookup.Found found -> {
                RekeyJobs.Job job = found.job();
                if (!tenant.equals(job.tenant())) {
                    HttpUtil.send(exchange, 404, "{\"error\":\"unknown job id\"}");
                    return;
                }
                switch (job.state()) {
                    case RUNNING -> HttpUtil.send(exchange, 200,
                        MAPPER.writeValueAsString(Map.of("job_id", jobId, "status", "running")));
                    case SUCCEEDED -> HttpUtil.send(exchange, 200,
                        MAPPER.writeValueAsString(Map.of(
                            "job_id", jobId, "status", "succeeded", "envelope", job.envelope())));
                    case FAILED -> {
                        Throwable f = job.failure();
                        // The sync form answered a legacy-id collision with 409;
                        // the async form keeps that distinction rather than
                        // flattening every failure into one status.
                        int status = f instanceof dev.nexus.service.db.RekeyOps.RekeyConflictException
                            ? 409 : 200;
                        HttpUtil.send(exchange, status, MAPPER.writeValueAsString(Map.of(
                            "job_id", jobId,
                            "status", "failed",
                            "error", String.valueOf(f == null ? "unknown error" : f.getMessage()))));
                    }
                }
            }
        }
    }

    // ── POST /v1/remap/record_batch ──────────────────────────────────────────

    private void handleRecordBatch(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String sourceCollection = requireString(body, "source_collection");

        Object rawEntries = body.get("entries");
        if (!(rawEntries instanceof List<?> list) || list.isEmpty()) {
            throw new IllegalArgumentException("'entries' must be a non-empty array");
        }
        if (list.size() > RemapRepository.MAX_BATCH) {
            throw new IllegalArgumentException(
                "batch too large: " + list.size() + " entries (max " + RemapRepository.MAX_BATCH
                + " — page the batch)");
        }

        List<RemapEntry> entries = new ArrayList<>(list.size());
        for (Object item : list) {
            if (!(item instanceof Map<?, ?> m)) {
                throw new IllegalArgumentException("each entry must be an object");
            }
            @SuppressWarnings("unchecked")
            Map<String, Object> entry = (Map<String, Object>) m;
            entries.add(new RemapEntry(
                    sourceCollection,
                    requireString(entry, "old_id"),
                    normalizeChash((String) entry.get("new_chash")),
                    requireString(entry, "target_collection"),
                    requireString(entry, "provenance")));
        }

        int recorded = repo.recordBatch(tenant, entries);
        HttpUtil.send(exchange, 200, "{\"recorded\":" + recorded + "}");
    }

    // ── POST /v1/remap/clear_leg ─────────────────────────────────────────────

    private void handleClearLeg(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String sourceCollection = requireString(body, "source_collection");
        // REQUIRED: a leg is the (source, target) PAIR — a wide whole-source
        // clear would delete a co-resident sibling leg's claims (critic
        // finding; RDR-185 .13 r2/C2 is why target_collection exists).
        String targetCollection = requireString(body, "target_collection");

        int deleted = repo.clearLeg(tenant, sourceCollection, targetCollection);
        HttpUtil.send(exchange, 200, "{\"deleted\":" + deleted + "}");
    }

    // ── GET /v1/remap/membership ─────────────────────────────────────────────

    private void handleMembership(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String sourceCollection = queryParam(exchange, "source_collection");
        String targetCollection = queryParam(exchange, "target_collection");
        if (sourceCollection == null || sourceCollection.isBlank()
                || targetCollection == null || targetCollection.isBlank()) {
            throw new IllegalArgumentException(
                "'source_collection' and 'target_collection' query params are required");
        }

        long[] m = repo.membership(tenant, sourceCollection, targetCollection);
        HttpUtil.send(exchange, 200,
                "{\"mapped_total\":" + m[0] + ",\"present_count\":" + m[1] + "}");
    }

    // ── GET /v1/remap/entries ────────────────────────────────────────────────

    private void handleEntries(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String sourceCollection = queryParam(exchange, "source_collection");
        if (sourceCollection == null || sourceCollection.isBlank()) {
            throw new IllegalArgumentException("'source_collection' query param is required");
        }
        var entries = repo.entriesForCollection(tenant, sourceCollection);
        HttpUtil.send(exchange, 200,
                MAPPER.writeValueAsString(Map.of("entries", entries)));
    }

    // ── GET /v1/remap/pairs ──────────────────────────────────────────────────

    private void handlePairs(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        int limit  = intParam(exchange, "limit", RemapRepository.MAX_PAGE);
        int offset = intParam(exchange, "offset", 0);
        var pairs = repo.pairs(tenant, limit, offset);
        HttpUtil.send(exchange, 200,
                MAPPER.writeValueAsString(Map.of("pairs", pairs)));
    }

    // ── GET /v1/remap/source_collections ─────────────────────────────────────

    private void handleSourceCollections(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        var sources = repo.sourceCollections(tenant);
        HttpUtil.send(exchange, 200,
                MAPPER.writeValueAsString(Map.of("source_collections", sources)));
    }

    // ── GET /v1/remap/count ──────────────────────────────────────────────────

    private void handleCount(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String sourceCollection = queryParam(exchange, "source_collection");  // optional
        long total = repo.count(tenant, sourceCollection);
        HttpUtil.send(exchange, 200, "{\"total\":" + total + "}");
    }

    // ── Helpers ──────────────────────────────────────────────────────────────

    private static int intParam(HttpExchange exchange, String key, int defaultValue) {
        String raw = queryParam(exchange, key);
        if (raw == null || raw.isBlank()) return defaultValue;
        try {
            return Integer.parseInt(raw);
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException("'" + key + "' must be an integer, got: " + raw);
        }
    }

    /**
     * Validate a new_chash fact (RDR-180, nexus-jxizy.7): the canonical
     * 64-hex full digest, parsed through the Chash type — the pre-flip
     * 64->32 truncation is retired with the [:32] era. Legacy 32-hex facts
     * already persisted by pre-cohort migrations stay readable (the widened
     * chash_remap CHECK + remap_membership's alias chain cover them); NEW
     * facts on a converged pair always carry the full digest.
     */
    private static String normalizeChash(String chash) {
        if (chash == null || chash.isBlank()) {
            throw new IllegalArgumentException("'new_chash' is required");
        }
        return dev.nexus.service.db.Chash.requireCanonical(chash, "'new_chash'");
    }

    private static String requireString(Map<String, Object> body, String field) {
        Object v = body.get(field);
        if (!(v instanceof String s) || s.isBlank()) {
            throw new IllegalArgumentException("'" + field + "' is required");
        }
        return s;
    }

    private Map<String, Object> readBody(HttpExchange exchange) throws IOException {
        try (InputStream in = exchange.getRequestBody()) {
            byte[] bytes = in.readAllBytes();
            if (bytes.length == 0) throw new IllegalArgumentException("request body is required");
            return MAPPER.readValue(bytes, MAP_TYPE);
        }
    }

    private static String queryParam(HttpExchange exchange, String key) {
        String query = exchange.getRequestURI().getRawQuery();
        if (query == null) return null;
        for (String part : query.split("&")) {
            String[] kv = part.split("=", 2);
            if (kv.length == 2 && kv[0].equals(key)) {
                return java.net.URLDecoder.decode(kv[1], java.nio.charset.StandardCharsets.UTF_8);
            }
        }
        return null;
    }
}
