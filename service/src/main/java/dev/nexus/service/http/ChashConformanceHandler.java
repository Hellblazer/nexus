// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.ChashConformanceConstraints;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * {@code GET /v1/schema/chash-conformance} (nexus-f3yqd): whether the two poison tables' chash width CHECK constraints are
 * validated, read from {@code pg_constraint.convalidated} in one catalog statement. It does not replace the doctor's
 * legacy-debt legs, and on a fresh or never-validated store it answers {@code all_validated: false}, so it saves nothing
 * there; see {@link ChashConformanceConstraints}.
 *
 * <p>{@code {constraints:[{table, name, present, validated, definition_matches}], all_validated}}, and for the root token also
 * {@code partitions} and {@code partitions_not_validated} on each entry. {@link ChashConformanceConstraints} has which
 * constraints, how a partitioned table's copies fold into {@code validated} (a parent valid with a copy not valid reads NOT
 * validated), why an absent constraint is {@code validated:false}, and {@code definition_matches} (the constraint and every
 * copy deparse to the expected check; a same-named weaker one is not the check). The definition text is never returned. {@code all_validated} is true only when every listed
 * constraint is present and validated, and a VALID width CHECK says no row of its table is non-conformant; it says nothing
 * about the tables that carry no such CHECK, which are not listed.
 *
 * <p><b>Authorization.</b> Any authenticated credential reads it: {@link AuthFilter} resolves the bearer server-side, so a
 * missing, malformed, revoked or expired credential is a 401 before this runs. A tenant-bound token (the data token a
 * managed user's doctor holds, or a long-lived tenant token) and the root token both get 200. A mint-scoped credential
 * exists only to call the data-token mint; {@code AuthFilter} refuses it on every other path (403) before this handler, as
 * for every route, and that is unchanged here. The path is under {@code /v1/schema}, not {@code /v1/admin}, because it is
 * not operator-only.
 *
 * <p><b>What a non-root caller may not learn.</b> {@code nexus.chunks} is partitioned model by tenant, so on a shared engine
 * the number of partition copies reveals how many tenants the engine holds. {@code partitions} and
 * {@code partitions_not_validated} are therefore withheld from every caller but the root token, and what is left (two table
 * and constraint names and booleans about the shared schema) is identical for every tenant. Nothing is tenant data, no table
 * is scanned and no tenant predicate applies.
 *
 * <p><b>Opt-in detail.</b> {@code ?detail=legacy_chunks} adds, when a width CHECK is not validated, {@code legacy_chunks}: for
 * the CALLER's own tenant only, counts per reason among its non-32-byte chunks and manifest rows ({@code chunk_reasons},
 * {@code manifest_reasons}) and up to 20 of those chunks ({@code chunks}, each with its
 * collection, model, chash, reason and owners). It is not free: no index serves a width test, so it scans the tenant's own
 * chunk partition under a 5 s bound ({@code available:false} when the bound is hit), which is why the route does it only when
 * asked and the default answer still scans nothing.
 *
 * <p>An old engine answers 404; a client reads that and a 503 as "not checked", never as "validated".
 */
public final class ChashConformanceHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(ChashConformanceHandler.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();

    /** The route's path; {@code NexusService} mounts the handler here, behind {@link AuthFilter}. */
    public static final String PATH = "/v1/schema/chash-conformance";

    /** The most chunks and manifest rows the opt-in detail counts, the chunks it lists, and how long its scan may run. */
    static final int DETAIL_COUNT_CAP = 1_000;
    static final int DETAIL_LIST_CAP = 20;
    static final int DETAIL_TIMEOUT_MS = 5_000;
    /** The whole detail's budget: its three scans and every owner lookup together. */
    static final long DETAIL_DEADLINE_MS = 8_000L;
    /** Overrides {@link #DETAIL_DEADLINE_MS} (milliseconds), read once when the handler is built: a test's way to run out of time. */
    static final String DEADLINE_PROPERTY = "nexus.doctor.legacy_detail_deadline_ms";

    private final ChashConformanceConstraints constraints;
    private final dev.nexus.service.db.TenantScope scope;
    private final long detailDeadlineMs;
    /** The tenants whose detail is being read now: one scan at a time per tenant (a second call answers busy, it does not scan again). Cleared as each call ends. */
    private final java.util.Set<String> detailInFlight = java.util.concurrent.ConcurrentHashMap.newKeySet();

    public ChashConformanceHandler(ChashConformanceConstraints constraints) {
        this(constraints, null);
    }

    /** @param scope the tenant-scoped sessions the opt-in {@code detail=legacy_chunks} reads through (null: that detail is not offered) */
    public ChashConformanceHandler(ChashConformanceConstraints constraints, dev.nexus.service.db.TenantScope scope) {
        this.constraints = constraints;
        this.scope = scope;
        // read once, when the handler is built: a test's way to run out of time, not a setting a request can change
        this.detailDeadlineMs = Long.getLong(DEADLINE_PROPERTY, DETAIL_DEADLINE_MS);
    }

    @Override
    public void handle(HttpExchange ex) throws IOException {
        if (!"GET".equalsIgnoreCase(ex.getRequestMethod())) {
            HttpUtil.send(ex, 405, "{\"error\":\"method not allowed\"}");
            return;
        }
        String tenant = RequestContext.tenant();
        if (tenant == null) {
            HttpUtil.send(ex, 500, "{\"error\":\"internal: tenant not set\"}");
            return;
        }
        ChashConformanceConstraints.Report report;
        try {
            report = constraints.read(tenant);
        } catch (RuntimeException e) {
            if (!HttpUtil.sendTypedDbError(ex, e, log, "chash_conformance", "tenant=" + tenant)) {
                log.error("event=chash_conformance_error tenant={} error={}", tenant, e.getMessage(), e);
                HttpUtil.send(ex, 500, "{\"error\":\"internal server error\"}");
            }
            return;
        }
        Map<String, Object> out = body(report, RequestContext.isOperator());
        String query = ex.getRequestURI().getRawQuery();
        // opt-in only: the route itself scans nothing, and this reads the caller's own legacy-width chunks (no index serves it)
        if (scope != null && query != null && java.util.Arrays.asList(query.split("&")).contains("detail=legacy_chunks")
                && !report.allValidated()) {
            out.put("legacy_chunks", legacyChunks(tenant));
        }
        HttpUtil.send(ex, 200, MAPPER.writeValueAsString(out));
    }

    /**
     * (Public so a test can drive one detail without the HTTP stack.) The caller's own legacy-width chunks by reason, and up to a bounded number of them listed with their owning documents
     * ({@link dev.nexus.service.db.LegacyChunkSurvey}); bounded by {@link #DETAIL_COUNT_CAP}, {@link #DETAIL_LIST_CAP} and
     * {@link #DETAIL_TIMEOUT_MS} per statement and {@link #DETAIL_DEADLINE_MS} for the whole detail ({@code deadline_exceeded}), and
     * {@code {"available": false}} when a statement outlasts its bound.
     */
    public Map<String, Object> legacyChunks(String tenant) {
        Map<String, Object> out = new LinkedHashMap<>();
        if (!detailInFlight.add(tenant)) {
            out.put("available", false);
            out.put("busy", true);
            return out;
        }
        try {
            var d = dev.nexus.service.db.LegacyChunkSurvey.forTenant(scope, tenant, DETAIL_COUNT_CAP, DETAIL_LIST_CAP, DETAIL_TIMEOUT_MS,
                detailDeadlineMs);
            out.put("available", true);
            // a stage that was cut by the request deadline is null (unknown), never an empty answer
            out.put("chunk_reasons", d.chunkCounts() == null ? null : d.chunkCounts().byReason());
            out.put("chunk_reasons_capped", d.chunkCounts() == null ? null : d.chunkCounts().capped());
            out.put("manifest_reasons", d.manifestCounts() == null ? null : d.manifestCounts().byReason());
            out.put("manifest_reasons_capped", d.manifestCounts() == null ? null : d.manifestCounts().capped());
            out.put("manifest_documents", d.manifestDocs() == null ? null : dev.nexus.service.db.LegacyChunkSurvey.docsJson(d.manifestDocs()));
            out.put("chunks", d.chunks() == null ? null : d.chunks().stream().map(b -> {
                Map<String, Object> e = new LinkedHashMap<>();
                e.put("collection", b.collection());
                e.put("embedding_model", b.model());
                e.put("chash_hex", b.chashHex());
                e.put("reason", b.reason());
                e.put("owners", dev.nexus.service.db.LegacyChunkSurvey.ownersJson(b.owners()));
                e.put("owners_capped", b.owners() != null && b.owners().capped());
                return e;
            }).toList());
            out.put("chunks_capped", d.chunks() == null ? null : d.chunksCapped());
            // the request deadline passed before every stage ran: what is missing is empty, and owners not looked up are null
            out.put("deadline_exceeded", d.partial());
        } catch (RuntimeException e) {
            log.warn("event=chash_conformance_detail_unavailable tenant={} error={}", tenant, e.getMessage());
            out.put("available", false);
        } finally {
            detailInFlight.remove(tenant);
        }
        return out;
    }

    /**
     * The response body for {@code report}; package-visible so a test can read it without a server. The partition counts
     * (which reveal how many tenants share the engine) are in it only when {@code includePartitionCounts}, which the handler
     * sets for the root token alone.
     */
    static Map<String, Object> body(ChashConformanceConstraints.Report report, boolean includePartitionCounts) {
        List<Map<String, Object>> rows = new ArrayList<>(report.constraints().size());
        for (var e : report.constraints()) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("table", e.table());
            row.put("name", e.name());
            row.put("present", e.present());
            row.put("validated", e.validated());
            // schema metadata, so every caller sees it; the definition text it was judged on is never returned
            row.put("definition_matches", e.definitionMatches());
            if (includePartitionCounts) {
                row.put("partitions", e.partitions());
                row.put("partitions_not_validated", e.partitionsNotValidated());
            }
            rows.add(row);
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("constraints", rows);
        body.put("all_validated", report.allValidated());
        return body;
    }
}
