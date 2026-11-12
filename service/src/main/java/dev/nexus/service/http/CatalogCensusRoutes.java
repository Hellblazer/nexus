// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.sun.net.httpserver.HttpExchange;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.IndexRunCensusReads;
import dev.nexus.service.db.NextSeqDriftReads;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Function;

/**
 * nexus-f3yqd: the two read-only catalog census routes of engine 0.1.190, which {@code nx doctor} reads in place of a walk
 * of every document. Each is tenant-scoped and bounded by the section-label count's statement timeout
 * ({@link CatalogRepository#FILLABLE_COUNT_TIMEOUT_MS}). The drift route answers 503
 * {@code {"error":..., "reason":"statement_timeout"}} with none of the answer when it outlasts the bound. The census
 * never answers 503 for a bound: it is three independent buckets under one call budget
 * ({@link IndexRunCensusReads#CALL_BUDGET_MS}), and a bucket that could not be read in time is {@code {"timed_out": true}}
 * in a 200, the others answering. A client that gets a 404 is on an engine before the route; a 503 or a {@code timed_out}
 * bucket is an engine that could not answer in time: all read "cannot say", never "none". A {@code capped} bucket is "at
 * least" (see {@link IndexRunCensusReads}), and a capped {@code failed_hidden} with a count of 0 is "not fully checked". New paths rather than parameters on existing routes: an old engine would ignore a parameter and answer a 200
 * that looks like "nothing found".
 */
final class CatalogCensusRoutes {

    private static final Logger log = LoggerFactory.getLogger(CatalogCensusRoutes.class);

    private final IndexRunCensusReads census;
    private final NextSeqDriftReads drift;

    CatalogCensusRoutes(IndexRunCensusReads census, NextSeqDriftReads drift) {
        this.census = census;
        this.drift = drift;
    }

    /**
     * GET /v1/catalog/index-run/census?limit=N: {@code {limit, threshold_s, floor, stale, fence, failed_hidden}}, each of
     * the three buckets an object or {@code {"timed_out": true}} when that bucket could not be read in time (the others
     * still answer). {@link IndexRunCensusReads} defines every bucket and the client rule it equals.
     * {@code limit} is 1 to {@link IndexRunCensusReads#MAX_LIMIT} (default {@link IndexRunCensusReads#DEFAULT_LIMIT});
     * anything else is a 400.
     */
    void indexRunCensus(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Integer limit = limitParam(exchange, IndexRunCensusReads.DEFAULT_LIMIT, IndexRunCensusReads.MAX_LIMIT);
        if (limit == null) return;
        IndexRunCensusReads.Census c = census.read(tenant, limit);
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("limit", c.limit());
        body.put("threshold_s", IndexRunCensusReads.STALE_THRESHOLD_S);
        body.put("floor", java.time.format.DateTimeFormatter.ISO_INSTANT.format(IndexRunCensusReads.FENCE_FLOOR));
        body.put("stale", bucket(c.stale(), CatalogCensusRoutes::staleBody));
        body.put("fence", bucket(c.fence(), CatalogCensusRoutes::fenceBody));
        body.put("failed_hidden", bucket(c.failedHidden(), CatalogCensusRoutes::failedHiddenBody));
        send(exchange, 200, body);
    }

    /**
     * GET /v1/catalog/owners/next_seq_drift?limit=N: {@code {checked, drifted, limit, capped, examples:[{owner, next_seq,
     * highest_child_seq}]}}. Read-only: the same high-water mark and drift predicate as
     * {@code POST /owners/sweep_next_seq_drift}, which also floors the counters; this writes nothing. {@code drifted} is
     * every drifted owner, {@code examples} at most {@code limit} of them. {@code limit} is 1 to
     * {@link NextSeqDriftReads#MAX_LIMIT} (default {@link NextSeqDriftReads#DEFAULT_LIMIT}); anything else is a 400.
     */
    void nextSeqDrift(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Integer limit = limitParam(exchange, NextSeqDriftReads.DEFAULT_LIMIT, NextSeqDriftReads.MAX_LIMIT);
        if (limit == null) return;
        NextSeqDriftReads.Page page;
        try {
            page = drift.read(tenant, limit);
        } catch (RuntimeException e) {
            if (!CatalogHandler.hasSqlState(e, CatalogRepository.QUERY_CANCELED)) throw e;
            log.warn("event=catalog_next_seq_drift_read_timeout tenant={} limit={}", tenant, limit);
            timeout(exchange, "next_seq drift read exceeded its time bound");
            return;
        }
        List<Map<String, Object>> examples = new ArrayList<>(page.examples().size());
        for (var d : page.examples()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("owner", d.owner());
            m.put("next_seq", d.nextSeq());
            m.put("highest_child_seq", d.highestChildSeq());
            examples.add(m);
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("checked", page.checked());
        body.put("drifted", page.drifted());
        body.put("limit", page.limit());
        body.put("capped", page.capped());
        body.put("examples", examples);
        send(exchange, 200, body);
    }

    // ── bodies ───────────────────────────────────────────────────────────────

    private static <T> Map<String, Object> bucket(IndexRunCensusReads.Outcome<T> outcome, Function<T, Map<String, Object>> body) {
        if (outcome.timedOut()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("timed_out", true);
            return m;
        }
        return body.apply(outcome.value());
    }

    private static Map<String, Object> staleBody(IndexRunCensusReads.Stale s) {
        List<Map<String, Object>> examples = new ArrayList<>();
        for (var d : s.examples()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("tumbler", d.tumbler());
            m.put("source_uri", d.sourceUri());
            m.put("started_at", d.startedAt());
            m.put("age_s", d.ageS());
            m.put("note", d.note());
            examples.add(m);
        }
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("count", s.count());
        m.put("capped", s.capped());
        m.put("notes", s.notes());
        m.put("examples", examples);
        return m;
    }

    private static Map<String, Object> fenceBody(IndexRunCensusReads.Fence f) {
        Map<String, Object> anchor = new LinkedHashMap<>();
        anchor.put("kind", f.anchor().kind());
        anchor.put("at", f.anchor().at());
        anchor.put("earliest_stamped_at", f.anchor().earliestStampedAt());
        anchor.put("proven_at", f.anchor().provenAt());
        List<Map<String, Object>> examples = new ArrayList<>();
        for (var u : f.postAnchor().examples()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("tumbler", u.tumbler());
            m.put("source_uri", u.sourceUri());
            m.put("file_path", u.filePath());
            m.put("indexed_at", u.indexedAt());
            examples.add(m);
        }
        Map<String, Object> post = new LinkedHashMap<>();
        post.put("count", f.postAnchor().count());
        post.put("capped", f.postAnchor().capped());
        post.put("examples", examples);
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("checked", f.checked());
        m.put("reported_null", f.reportedNull());
        m.put("undated_reported_null", f.undatedReportedNull());
        m.put("floor_candidates", f.floorCandidates());
        m.put("anchor", anchor);
        m.put("post_anchor", post);
        return m;
    }

    private static Map<String, Object> failedHiddenBody(IndexRunCensusReads.FailedHidden h) {
        List<Map<String, Object>> examples = new ArrayList<>();
        for (var d : h.examples()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("tumbler", d.tumbler());
            m.put("source_uri", d.sourceUri());
            m.put("chunk_count", d.chunkCount());
            m.put("chunks_stored", d.chunksStored());
            examples.add(m);
        }
        Map<String, Object> failed = new LinkedHashMap<>();
        failed.put("count", h.failed());
        failed.put("capped", h.failedCapped());
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("failed", failed);
        m.put("count", h.count());
        m.put("capped", h.capped());
        m.put("examples", examples);
        return m;
    }

    // ── shared ───────────────────────────────────────────────────────────────

    /** The {@code limit} query parameter, {@code def} when absent; null after answering a 400 for a bad one. */
    private static Integer limitParam(HttpExchange exchange, int def, int max) throws IOException {
        String raw = CatalogBoundedReadRoutes.queryParam(exchange, "limit");
        if (raw == null || raw.isBlank()) return def;
        int limit;
        try {
            limit = Integer.parseInt(raw.trim());
        } catch (NumberFormatException e) {
            HttpUtil.send(exchange, 400, "{\"error\":\"limit must be an integer (1.." + max + ")\"}");
            return null;
        }
        if (limit < 1 || limit > max) {
            HttpUtil.send(exchange, 400, "{\"error\":\"limit must be 1.." + max + "\"}");
            return null;
        }
        return limit;
    }

    private static void timeout(HttpExchange exchange, String message) throws IOException {
        HttpUtil.send(exchange, 503, "{\"error\":" + HttpUtil.jsonString(message) + ",\"reason\":\"statement_timeout\"}");
    }

    private static void send(HttpExchange exchange, int status, Map<String, Object> body) throws IOException {
        try {
            HttpUtil.send(exchange, status, CatalogHandler.MAPPER.writeValueAsString(body));
        } catch (JsonProcessingException e) {
            throw new IOException(e);
        }
    }
}
