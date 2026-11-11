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
 * {@code GET /v1/admin/chash-conformance} (nexus-f3yqd): whether the two poison tables' chash width CHECK constraints are
 * validated, read from {@code pg_constraint.convalidated} in one catalog statement. It does not replace the doctor's
 * legacy-debt legs, and on a fresh or never-validated store it answers {@code all_validated: false}, so it saves nothing
 * there; see {@link ChashConformanceConstraints}.
 *
 * <p>{@code {constraints:[{table, name, present, validated, partitions, partitions_not_validated}], all_validated}}.
 * {@link ChashConformanceConstraints} has which constraints, how a partitioned table's copies fold into {@code validated}
 * (a parent valid with a copy not valid reads NOT validated) and why an absent constraint is {@code validated:false}.
 * {@code all_validated} is true only when every listed constraint is present and validated, and a VALID width CHECK says
 * no row of its table is non-conformant; it says nothing about the tables that carry no such CHECK, which are not listed.
 *
 * <p><b>Authorization</b> is the {@code /v1/admin} pair ({@link ChunksLeavesHandler}): {@link AuthFilter} resolves the
 * bearer, so a missing, malformed, revoked or expired credential is a 401 before this runs, and
 * {@link RequestContext#isOperator()} (the root token, resolved server-side) is required here, so a valid tenant-bound
 * token is a 403. Nothing returned is tenant data (table and constraint names and booleans), but the route sits under the
 * admin prefix with the rest of the engine-wide reads. No table is scanned and no tenant predicate applies.
 *
 * <p>An old engine answers 404; a client reads that, a 403 and a 503 as "not checked", never as "validated".
 */
public final class ChashConformanceHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(ChashConformanceHandler.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();

    /** The route's path; {@code NexusService} mounts the handler here, behind {@link AuthFilter}. */
    public static final String PATH = "/v1/admin/chash-conformance";

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
        if (!RequestContext.isOperator()) {
            log.debug("event=chash_conformance_denied reason=operator_required tenant={}", RequestContext.tenant());
            HttpUtil.send(ex, 403, "{\"error\":\"operator privilege required: this read needs the root token\"}");
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
        HttpUtil.send(ex, 200, MAPPER.writeValueAsString(body(report)));
    }

    /** The response body for {@code report}; package-visible so a test can read it without a server. */
    static Map<String, Object> body(ChashConformanceConstraints.Report report) {
        List<Map<String, Object>> rows = new ArrayList<>(report.constraints().size());
        for (var e : report.constraints()) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("table", e.table());
            row.put("name", e.name());
            row.put("present", e.present());
            row.put("validated", e.validated());
            row.put("partitions", e.partitions());
            row.put("partitions_not_validated", e.partitionsNotValidated());
            rows.add(row);
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("constraints", rows);
        body.put("all_validated", report.allValidated());
        return body;
    }
}
