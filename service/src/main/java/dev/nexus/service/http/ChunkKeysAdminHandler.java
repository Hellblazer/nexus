// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.TokenStore;
import dev.nexus.service.vectors.ChunkConstraintAdmin;
import dev.nexus.service.vectors.ChunkKeyPlan;
import dev.nexus.service.vectors.ChunkKeySweep;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.function.Supplier;

/**
 * RDR-236 Phase 4 Step 3 (nexus-6pfgb.43): the three engine-admin routes that retire the chunk-row metadata keys.
 * Under {@code /v1/admin/chunk-keys}, all POST, none called by anything on its own: an operator or a client verb
 * ({@code nx t3 retire-keys}) sends them, and the upgrade-ladder rung that would run the sweep on every install is a
 * later bead that ships only after a cloud dry run has been read.
 *
 * <pre>
 *   POST /v1/admin/chunk-keys/sweep               the CALLER's tenant; dry run by default
 *   POST /v1/admin/chunk-keys/add-constraints     table-wide, operator (root token) only
 *   POST /v1/admin/chunk-keys/validate-constraints table-wide, operator only
 * </pre>
 *
 * <p><b>Common request fields.</b> {@code allowed_keys} (the CHUNK and ROW keys), {@code bypass_prefixes} (the
 * collection prefixes the allow-list does not apply to) and {@code quarantine_implies} ({@code [by, at]}) are rendered
 * by the client from the Python key table; the engine refuses a list that contradicts its own key sets ({@link
 * ChunkKeyPlan#of}). A malformed request is 400.
 *
 * <p><b>Sweep.</b> {@code dry_run} defaults to <b>true</b>: a request that does not say {@code "dry_run": false} writes
 * nothing. The dry run answers per-collection counts (rows with a retired key, rows left alone for a wrong-width chash,
 * the collection's census verdict), the preconditions, and one measured batch (taken and rewritten inside a transaction
 * that is rolled back; {@code measure_batch: false} skips it). Apply sweeps up to {@code max_batches} batches of {@code
 * batch_size} rows (at most 1000) or {@code budget_seconds}, whichever ends first, and answers {@code done}; the caller
 * loops while it is false. It runs the same checks first and answers 409 {@code sweep_preconditions_not_met} with the
 * counts when one stands in the way: the residues in {@link ChunkKeySweep#ACCEPTABLE} may be accepted by naming them in
 * {@code accept}. The sweep never touches the bypass collections or a row whose chash is not 32 bytes, skips a collection
 * whose census reads {@code legacy-unmanifested} or could not be read, and does not set {@code last_written_at}.
 * DATA EFFECT: the metadata of every swept row loses its retired keys; nothing else on the row changes.
 *
 * <p><b>Add and validate</b> are table-wide: they read every known tenant's counts and refuse, naming the tenant, while
 * any tenant is unswept ({@code add}) or still holds a retired key, an unequal hash or model, a quarantine implication
 * breach or a wrong-width chash ({@code validate}). They run on the admin connection ({@link ChunkConstraintAdmin}); an
 * engine without one answers 503. DATA EFFECT of add: a write carrying a key outside the allow-list is then refused
 * (SQLSTATE 23514). Validate changes no data.
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

    private final ChunkKeySweep sweep;
    private final Supplier<ChunkConstraintAdmin> constraints;
    private final Supplier<List<String>> tenants;

    /**
     * @param sweep       the sweep and its counts
     * @param constraints the admin-connection DDL, bound late; null (or a supplier answering null) is 503 on the
     *                    table-wide routes
     * @param tenants     every tenant that may hold chunk rows ({@code TokenStore#listKnownTenants})
     */
    public ChunkKeysAdminHandler(ChunkKeySweep sweep, Supplier<ChunkConstraintAdmin> constraints,
                                 Supplier<List<String>> tenants) {
        this.sweep = sweep;
        this.constraints = constraints;
        this.tenants = tenants;
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
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("error", e.getMessage());
            body.put("reason", "sweep_preconditions_not_met");
            body.put("blocking", e.report().blocking());
            body.put("report", dryRunBody(e.report()));
            HttpUtil.send(ex, 409, json(body));
        } catch (Exception e) {
            if (!HttpUtil.sendTypedDbError(ex, e, log, "chunk_keys_admin", "op=" + op + " tenant=" + tenant)) {
                log.error("event=chunk_keys_admin_error op={} tenant={} error={}", op, tenant, e.getMessage(), e);
                HttpUtil.send(ex, 500, "{\"error\":\"internal server error\"}");
            }
        }
    }

    // ---- sweep --------------------------------------------------------------------------------------------------

    private void handleSweep(HttpExchange ex, String tenant) throws IOException {
        String scopeName = RequestContext.scope();
        if (!TokenStore.SCOPE_ROOT.equals(scopeName) && !TokenStore.SCOPE_TENANT.equals(scopeName)) {
            HttpUtil.send(ex, 403, json(Map.of("error",
                "the sweep needs a tenant or root credential, not scope " + scopeName)));
            return;
        }
        Map<String, Object> body = readBody(ex);
        ChunkKeyPlan plan = plan(body);
        boolean dryRun = !Boolean.FALSE.equals(body.get("dry_run"));
        Set<String> accepted = accepted(body);
        int batchSize = intField(body, "batch_size", ChunkKeySweep.DEFAULT_BATCH, 1, ChunkKeySweep.MAX_BATCH);
        String only = body.get("collection") instanceof String s && !s.isBlank() ? s : null;

        if (dryRun) {
            boolean measure = !Boolean.FALSE.equals(body.get("measure_batch"));
            var report = sweep.dryRun(tenant, plan, accepted, batchSize, measure);
            log.info("event=retire_keys_dry_run tenant={} sweepable={} blocked={} blocking={}", tenant,
                report.sweepableRows(), report.blockedRows(), report.blocking());
            Map<String, Object> out = dryRunBody(report);
            out.put("dry_run", true);
            HttpUtil.send(ex, 200, json(out));
            return;
        }
        int maxBatches = intField(body, "max_batches", DEFAULT_MAX_BATCHES, 1, MAX_MAX_BATCHES);
        int budget = intField(body, "budget_seconds", DEFAULT_BUDGET_SECONDS, 1, MAX_BUDGET_SECONDS);
        var applied = sweep.apply(tenant, plan, accepted, only, batchSize, maxBatches, Duration.ofSeconds(budget));
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("tenant", applied.tenant());
        out.put("dry_run", false);
        out.put("rows_swept", applied.rowsSwept());
        out.put("batches", applied.batches());
        out.put("done", applied.done());
        out.put("remaining_rows", applied.remainingRows());
        out.put("first_batch_ms", applied.firstBatchMs());
        out.put("elapsed_ms", applied.totalMs());
        out.put("blocked_collections", collections(applied.blocked()));
        HttpUtil.send(ex, 200, json(out));
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
        out.put("acceptable_residues", ChunkKeySweep.ACCEPTABLE);
        out.put("blocking", r.blocking());
        out.put("applicable", r.blocking().isEmpty());
        out.put("batch_size", r.batchSize());
        if (r.batchCost() == null) {
            out.put("batch_cost", null);
        } else {
            Map<String, Object> cost = new LinkedHashMap<>();
            cost.put("rows", r.batchCost().rows());
            cost.put("select_ms", r.batchCost().selectMs());
            cost.put("update_ms", r.batchCost().updateMs());
            cost.put("rolled_back", true);
            long perBatchMs = r.batchCost().selectMs() + r.batchCost().updateMs();
            long batches = r.batchSize() == 0 ? 0 : (r.sweepableRows() + r.batchSize() - 1) / r.batchSize();
            cost.put("projected_seconds", batches * perBatchMs / 1000.0);
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

        List<Map<String, Object>> offenders = new ArrayList<>();
        List<String> known = tenants.get();
        for (String t : known) {
            Map<String, Object> row = new LinkedHashMap<>();
            if (add) {
                long unswept = sweep.sweepCount(t, plan);
                if (unswept > 0) {
                    row.put("tenant", t);
                    row.put("unswept_rows", unswept);
                }
            } else {
                long retired = sweep.retiredRowsTotal(t, plan);
                Map<String, Long> pre = sweep.preconditions(t, plan);
                if (retired > 0) {
                    row.put("retired_key_rows", retired);
                }
                for (String k : List.of("chunk_text_hash_mismatch", "embedding_model_mismatch", "wrong_width_rows",
                        "quarantine_implication_violations")) {
                    if (pre.getOrDefault(k, 0L) > 0) {
                        row.put(k, pre.get(k));
                    }
                }
                if (!row.isEmpty()) {
                    row.put("tenant", t);
                }
            }
            if (!row.isEmpty()) {
                offenders.add(row);
            }
        }
        if (!offenders.isEmpty()) {
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("error", "refused: tenant(s) "
                + offenders.stream().map(o -> String.valueOf(o.get("tenant"))).toList()
                + (add ? " are not swept" : " still break a constraint"));
            out.put("reason", add ? "tenant_unswept" : "tenant_violates_constraint");
            out.put("tenants", offenders);
            out.put("tenants_checked", known.size());
            HttpUtil.send(ex, 409, json(out));
            return;
        }
        List<ChunkConstraintAdmin.Action> actions = add
            ? admin.add(plan, Boolean.TRUE.equals(body.get("replace"))) : admin.validate();
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("tenants_checked", known.size());
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
            list.add(m);
        }
        out.put("constraints", list);
        HttpUtil.send(ex, 200, json(out));
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
            stringList(body, "quarantine_implies", false));
    }

    private static Set<String> accepted(Map<String, Object> body) {
        Set<String> out = new LinkedHashSet<>(stringList(body, "accept", false));
        for (String name : out) {
            if (!ChunkKeySweep.ACCEPTABLE.contains(name)) {
                throw new IllegalArgumentException("accept names " + name + ", which is not an acceptable residue ("
                    + ChunkKeySweep.ACCEPTABLE + ")");
            }
        }
        return out;
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
