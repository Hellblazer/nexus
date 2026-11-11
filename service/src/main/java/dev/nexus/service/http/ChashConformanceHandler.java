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
 * <p>{@code {constraints:[{table, name, present, validated}], all_validated}}, and for the root token also
 * {@code partitions} and {@code partitions_not_validated} on each entry. {@link ChashConformanceConstraints} has which
 * constraints, how a partitioned table's copies fold into {@code validated} (a parent valid with a copy not valid reads NOT
 * validated) and why an absent constraint is {@code validated:false}. {@code all_validated} is true only when every listed
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
 * <p>An old engine answers 404; a client reads that and a 503 as "not checked", never as "validated".
 */
public final class ChashConformanceHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(ChashConformanceHandler.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();

    /** The route's path; {@code NexusService} mounts the handler here, behind {@link AuthFilter}. */
    public static final String PATH = "/v1/schema/chash-conformance";

    private final ChashConformanceConstraints constraints;

    public ChashConformanceHandler(ChashConformanceConstraints constraints) {
        this.constraints = constraints;
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
        HttpUtil.send(ex, 200, MAPPER.writeValueAsString(body(report, RequestContext.isOperator())));
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
