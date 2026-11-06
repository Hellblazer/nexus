// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.vectors.LeafBloatCatalog;
import dev.nexus.service.vectors.LeafBloatSweep;
import dev.nexus.service.vectors.PciMaintenanceStatus;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Supplier;

/**
 * {@code GET /v1/admin/chunks-leaves} (nexus-i827c): the leaves of {@code nexus.chunks} that need attention, named.
 *
 * <p><b>Why this route exists.</b> {@code GET /v1/status} is unauthenticated, so by Sam's ruling of 2026-10-19 its
 * {@code chunks_leaf_bloat} object carries counts over every leaf and names none: a leaf name embeds a tenant hash, and
 * a per-leaf row count lets anyone holding a guessable tenant id confirm the tenant and watch its churn. This route
 * is where the names are, behind the credential the operator already holds.
 *
 * <p><b>Authorization (the security point).</b> It rides the same two layers the token admin surface does
 * ({@link TokenAdminHandler}): {@link AuthFilter} resolves the bearer server-side, so a missing, malformed, revoked or
 * expired credential is a 401 before this handler runs, and {@link RequestContext#isOperator()} (the persistent root
 * token, {@code service_tokens.scope = 'root'}, resolved server-side and never client-asserted) is required here, so
 * a valid tenant-bound token is a 403. The rows span every tenant's leaves, which is exactly why a tenant token must
 * not read them. A mint-scoped credential never reaches this handler ({@code AuthFilter} refuses it on every path
 * but the data-token mint). The handler checks the principal itself rather than trusting the filter's presence: if it
 * were ever mounted without one, {@link RequestContext#isOperator()} reads false and the answer is 403.
 *
 * <p><b>What a row is.</b> The same data the {@link LeafBloatSweep} cache holds (this route reads that cache and runs
 * no catalog query): a leaf appears when {@link LeafBloatSweep.LeafStatus#problems()} is not empty, that is, it is
 * {@code pinned} or {@code starved}, or lacks one of the four vectors-034 autovacuum settings, or has autovacuum
 * switched off. {@code rows} is the figure the overdue rule scales by ({@link LeafBloatCatalog.Leaf#rows()}),
 * {@code overdue_state} is {@code none}, {@code waiting}, {@code pinned} or {@code starved}, and
 * {@code pci_index_bytes} is the total size of the {@code pci_} indexes on the leaf ({@code null} when a lock held on
 * one of them kept its size from being read; unknown is never 0), {@code pci_index_count} how many.
 * A leaf that is merely {@code waiting} is not listed (a vacuum may be running); {@code leaves_total} says how many
 * the last read saw.
 *
 * <p><b>The recall canary's index list (RDR-230 Phase 1, Sam, 2026-11-06).</b> {@code canary_indexes} is the per-index
 * list that {@code GET /v1/status} does not carry (it is unauthenticated and names no {@code pci_} index): at most 64
 * entries of {@link PciMaintenanceStatus.Entry#wire()}, red first, then unmeasured, then green, from the reconciler's
 * memory. The key is absent when no canary source is wired, and an empty array when one is wired and has measured
 * nothing; it sits behind the same operator check as every other row here.
 */
public final class ChunksLeavesHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(ChunksLeavesHandler.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();

    /** The route's path; {@code NexusService} mounts the handler here, behind {@link AuthFilter}. */
    public static final String PATH = "/v1/admin/chunks-leaves";

    private final Supplier<LeafBloatSweep.Status> sweep;
    private final Supplier<Long> sweepSeconds;
    private final Supplier<PciMaintenanceStatus> canary;

    /**
     * @param sweep        the leaf bloat sweep's view, from memory; null (or a supplier answering null) before the
     *                     sweep exists, which the route answers as 503
     * @param sweepSeconds the sweep period, for the reader to judge how old a read may be
     * @param canary       the recall canary's status, from the reconciler's memory; null (or a supplier answering
     *                     null) when no canary is wired, which omits {@code canary_indexes}
     */
    public ChunksLeavesHandler(Supplier<LeafBloatSweep.Status> sweep, Supplier<Long> sweepSeconds,
                               Supplier<PciMaintenanceStatus> canary) {
        this.sweep = sweep;
        this.sweepSeconds = sweepSeconds;
        this.canary = canary;
    }

    @Override
    public void handle(HttpExchange ex) throws IOException {
        if (!"GET".equalsIgnoreCase(ex.getRequestMethod())) {
            HttpUtil.send(ex, 405, "{\"error\":\"method not allowed\"}");
            return;
        }
        if (!RequestContext.isOperator()) {
            log.debug("event=chunks_leaves_denied reason=operator_required tenant={}", RequestContext.tenant());
            HttpUtil.send(ex, 403, "{\"error\":\"operator privilege required: this read needs the root token\"}");
            return;
        }
        LeafBloatSweep.Status status = sweep == null ? null : sweep.get();
        if (status == null) {
            HttpUtil.send(ex, 503, "{\"error\":\"the chunks leaf bloat sweep is not running yet\"}");
            return;
        }
        PciMaintenanceStatus maintenance = canary == null ? null : canary.get();
        HttpUtil.send(ex, 200,
            MAPPER.writeValueAsString(body(status, sweepSeconds == null ? null : sweepSeconds.get(), maintenance)));
    }

    /**
     * The response body for {@code status}; package-visible so a test can read it without a server.
     *
     * @param maintenance the canary's status, or null to omit {@code canary_indexes}
     */
    static Map<String, Object> body(LeafBloatSweep.Status status, Long sweepSeconds, PciMaintenanceStatus maintenance) {
        var rows = new ArrayList<Map<String, Object>>();
        for (LeafBloatSweep.LeafStatus l : status.leaves()) {
            List<String> problems = l.problems();
            if (problems.isEmpty()) {
                continue;
            }
            LeafBloatCatalog.Leaf leaf = l.leaf();
            var row = new LinkedHashMap<String, Object>();
            row.put("leaf", leaf.name());
            row.put("problems", problems);
            row.put("rows", leaf.rows());
            row.put("dead_rows", Math.max(0L, leaf.deadRows()));
            row.put("last_autovacuum", seconds(leaf.lastAutovacuum()));
            row.put("overdue_state", l.state().wire());
            row.put("overdue_for_seconds", l.overdueForSeconds());
            row.put("vacuums_since_overdue", l.vacuumsSinceOverdue());
            row.put("policy_set", leaf.policySet());
            row.put("autovacuum_enabled", leaf.autovacuumEnabled());
            row.put("pci_index_count", leaf.pciIndexes().size());
            row.put("pci_index_bytes", leaf.pciIndexBytes());
            rows.add(row);
        }
        var body = new LinkedHashMap<String, Object>();
        body.put("last_read_at", seconds(status.lastReadAt()));
        body.put("expired", status.expired());
        body.put("sweep_seconds", sweepSeconds);
        body.put("ever_read", status.everRead());
        body.put("consecutive_failures", status.consecutiveFailures());
        body.put("leaves_total", status.leaves().size());
        body.put("needing_attention", rows.size());
        body.put("leaves", rows);
        if (maintenance != null) {
            body.put("canary_indexes",
                maintenance.canary().indexes().stream().map(PciMaintenanceStatus.Entry::wire).toList());
        }
        return body;
    }

    /** A time in whole seconds, as the status route's are, or null. */
    private static String seconds(Instant at) {
        return at == null ? null : at.truncatedTo(ChronoUnit.SECONDS).toString();
    }
}
