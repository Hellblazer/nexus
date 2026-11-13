// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.AdminDdlSession;
import dev.nexus.service.db.ManifestChunkIndexCheckAdmin;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantScope;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.function.Supplier;

/**
 * RDR-236 (nexus-6pfgb.42): the three engine-admin routes of the manifest chunk-index CHECK
 * ({@code chunk_index = position} on {@code nexus.catalog_document_chunks}), a step of its own beside the catalog TTL
 * routes ({@link CatalogTtlCheckHandler}) and the chunk-table routes ({@link ChunkKeysAdminHandler}). Under
 * {@code /v1/admin/manifest-chunk-index-check}, all POST, table wide, root credential
 * ({@link RequestContext#isOperator()}) only, none called by anything on its own.
 *
 * <pre>
 *   POST /v1/admin/manifest-chunk-index-check/census    changes no data (as the table owner it holds a table lock for
 *                                                       the scan): per tenant, the manifest rows and those the CHECK
 *                                                       would refuse
 *   POST /v1/admin/manifest-chunk-index-check/add       the CHECK, NOT VALID; refused while any tenant has a violator
 *   POST /v1/admin/manifest-chunk-index-check/validate  VALIDATE CONSTRAINT; refused while any tenant has a violator
 * </pre>
 *
 * <p>The decisions are the TTL routes' (see {@link CatalogTtlCheckHandler}): the count is every tenant's (a tenant with
 * no {@code service_tokens} row included; 503 {@code admin_cannot_see_all_tenants} for a role the table's FORCE
 * row-level security filters; 503 {@code admin_probe_undercount} for a census below a tenant-scoped count); a refusal
 * names the tenants (409 {@code tenant_violates_constraint}); {@code add} answers {@code present} and {@code validate}
 * {@code already_valid} or {@code absent} from the constraint's state before any count; {@code add} counts again after
 * the ADD ({@code violating_after_add}). The two handlers are siblings, not one parameterised class, so the TTL routes'
 * wire text stays exactly as its tests pin it. DATA EFFECT of add: a NOT VALID CHECK is evaluated on every new row
 * version, so a manifest row that breaks it could no longer be updated until fixed; the route refuses while the count
 * reads one. validate changes no data. The census (up to 10 minutes) and validate (up to 30) are synchronous; the public
 * edge times a request out after about 30 seconds and the engine carries on.
 *
 * <p>Nothing in the engine schedules or calls these routes; {@code NexusService} mounts the handler and {@code Main}
 * binds the admin connection.
 */
public final class ManifestChunkIndexCheckHandler implements HttpHandler {

    public static final String PATH = "/v1/admin/manifest-chunk-index-check";

    private static final Logger log = LoggerFactory.getLogger(ManifestChunkIndexCheckHandler.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final int SCOPED_COUNT_STATEMENT_MS = 120_000;
    private static final int SCOPED_COUNT_LOCK_MS = 2_000;

    private final Supplier<ManifestChunkIndexCheckAdmin> adminSource;
    private final Supplier<List<String>> tenants;
    private final TenantScope scope;

    /**
     * @param adminSource the admin-connection DDL, bound late; null (or a supplier answering null) is 503
     * @param tenants     every tenant that holds a {@code service_tokens} row ({@code TokenStore#listKnownTenants})
     * @param scope       the tenant-scoped session the second-line row counts are taken through
     */
    public ManifestChunkIndexCheckHandler(Supplier<ManifestChunkIndexCheckAdmin> adminSource,
                                          Supplier<List<String>> tenants, TenantScope scope) {
        this.adminSource = adminSource;
        this.tenants = tenants;
        this.scope = scope;
    }

    @Override
    public void handle(HttpExchange ex) throws IOException {
        String tenant = RequestContext.tenant();
        if (tenant == null) {
            HttpUtil.send(ex, 500, "{\"error\":\"internal: tenant not set\"}");
            return;
        }
        String op = ex.getRequestURI().getPath().replaceFirst("^" + PATH, "");
        if (!Set.of("/census", "/add", "/validate").contains(op)) {
            HttpUtil.send(ex, 404, "{\"error\":\"not found\"}");
            return;
        }
        if (!"POST".equals(ex.getRequestMethod().toUpperCase(Locale.ROOT))) {
            HttpUtil.send(ex, 405, "{\"error\":\"method not allowed\"}");
            return;
        }
        try {
            if (!RequestContext.isOperator()) {
                HttpUtil.send(ex, 403, json(Map.of("error",
                    "operator privilege required: the manifest chunk-index check routes need the root token")));
                return;
            }
            ManifestChunkIndexCheckAdmin admin = adminSource == null ? null : adminSource.get();
            if (admin == null) {
                HttpUtil.send(ex, 503, json(Map.of("error",
                    "this engine has no admin connection bound for the manifest chunk-index check")));
                return;
            }
            switch (op) {
                case "/census" -> handleCensus(ex, admin);
                case "/add" -> handleAdd(ex, admin);
                default -> handleValidate(ex, admin);
            }
        } catch (AdminDdlSession.Refused e) {
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("error", e.getMessage());
            body.put("reason", e.reason());
            HttpUtil.send(ex, e.status(), json(body));
        } catch (Exception e) {
            if (!HttpUtil.sendTypedDbError(ex, e, log, "manifest_chunk_index_check", "op=" + op + " tenant=" + tenant)) {
                log.error("event=manifest_chunk_index_check_error op={} tenant={} error={}", op, tenant, e.getMessage(), e);
                HttpUtil.send(ex, 500, "{\"error\":\"internal server error\"}");
            }
        }
    }

    // ---- routes -------------------------------------------------------------------------------------------------

    private void handleCensus(HttpExchange ex, ManifestChunkIndexCheckAdmin admin) throws IOException {
        Survey survey = survey(admin);
        Map<String, Object> out = survey.body();
        out.put("constraint", constraint(null, admin.state()));
        HttpUtil.send(ex, 200, json(out));
    }

    private void handleAdd(HttpExchange ex, ManifestChunkIndexCheckAdmin admin) throws IOException {
        // the state first: a constraint that is already there is answered without a survey, so a rerun with a blind
        // admin role or a racing writer is still "present", not a 503 or a 409
        AdminDdlSession.State state = admin.state();
        if (state.present()) {
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("census_skipped", "the constraint is already present");
            out.put("constraint", constraint("present", state));
            HttpUtil.send(ex, 200, json(out));
            return;
        }
        Survey survey = survey(admin);
        if (survey.refuse(ex, "are not ready for the constraint: a manifest row whose chunk_index differs from its "
            + "position would be frozen (it could not be updated) by a NOT VALID CHECK")) {
            return;
        }
        ManifestChunkIndexCheckAdmin.Action action = admin.add();
        Map<String, Object> out = survey.body();
        out.put("constraint", constraint(action.action(), action.after()));
        if ("added".equals(action.action())) {
            countAfterAdd(out, admin);
        }
        HttpUtil.send(ex, 200, json(out));
    }

    /**
     * The count again, after the ADD: the survey and the ADD are two statements with no lock between them. The CHECK
     * refuses every later violating write, so this count is final. A failure to take it, of any kind, does not undo the
     * ADD, and the answer says so ({@code violating_after_add} null, a warning that the constraint is in place).
     */
    private void countAfterAdd(Map<String, Object> out, ManifestChunkIndexCheckAdmin admin) {
        try {
            Survey after = survey(admin);
            out.put("violating_after_add", after.violatingTotal());
            out.put("violators_after_add", after.offenders());
            if (after.violatingTotal() > 0) {
                out.put("warning", "the constraint was added, but " + after.violatingTotal() + " manifest row"
                    + (after.violatingTotal() == 1 ? "" : "s") + " with chunk_index different from position appeared "
                    + "between the count and the add (see violators_after_add). They are frozen: they cannot be "
                    + "updated until chunk_index is set to position. Fix them, then count again before validate");
            }
        } catch (RuntimeException e) {
            // any failure, not only a refusal: the ADD has landed, so the answer must say so
            String why;
            if (e instanceof AdminDdlSession.Refused r) {
                why = r.reason() + "): " + r.getMessage();
            } else {
                log.error("event=manifest_chunk_index_check_count_after_add_failed error={}", e.getMessage(), e);
                why = "internal_error): see the engine log";
            }
            out.put("violating_after_add", null);
            out.put("warning", "the constraint was added, but the count after it could not be taken (" + why
                + ". Run the census to see whether a row was frozen");
        }
    }

    private void handleValidate(HttpExchange ex, ManifestChunkIndexCheckAdmin admin) throws IOException {
        // convalidated first: a VALID CHECK cannot have a violator, so there is nothing to count or scan.
        AdminDdlSession.State state = admin.state();
        if (!state.present() || Boolean.TRUE.equals(state.validated())) {
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("census_skipped", state.present() ? "the constraint is already valid" : "the constraint is absent");
            out.put("constraint", constraint(state.present() ? "already_valid" : "absent", state));
            HttpUtil.send(ex, 200, json(out));
            return;
        }
        Survey survey = survey(admin);
        if (survey.refuse(ex, "still hold a manifest row whose chunk_index differs from its position")) {
            return;
        }
        ManifestChunkIndexCheckAdmin.Action action = admin.validate();
        Map<String, Object> out = survey.body();
        out.put("constraint", constraint(action.action(), action.after()));
        HttpUtil.send(ex, 200, json(out));
    }

    // ---- the all-tenant look ------------------------------------------------------------------------------------

    /** What one pass over the table found, and the tenants it covered. */
    private record Survey(Set<String> checked, long rowsExamined, List<ManifestChunkIndexCheckAdmin.TenantCensus> census) {

        long violatingTotal() {
            long violating = 0;
            for (var c : census) {
                violating += c.violating();
            }
            return violating;
        }

        /** The tenants with a violator, each with its count. */
        List<Map<String, Object>> offenders() {
            List<Map<String, Object>> offenders = new ArrayList<>();
            for (var c : census) {
                if (c.violating() > 0) {
                    Map<String, Object> m = new LinkedHashMap<>();
                    m.put("tenant", c.tenant());
                    m.put("violating_rows", c.violating());
                    offenders.add(m);
                }
            }
            return offenders;
        }

        Map<String, Object> body() {
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("tenants_checked", new ArrayList<>(checked));
            out.put("table_rows_examined", rowsExamined);
            List<Map<String, Object>> rows = new ArrayList<>();
            for (var c : census) {
                Map<String, Object> m = new LinkedHashMap<>();
                m.put("tenant", c.tenant());
                m.put("rows", c.rows());
                m.put("violating_rows", c.violating());
                rows.add(m);
            }
            out.put("violating_total", violatingTotal());
            out.put("tenants", rows);
            return out;
        }

        /** Answer 409 naming the tenants with a violator and return true, or return false when there is none. */
        boolean refuse(HttpExchange ex, String why) throws IOException {
            List<Map<String, Object>> offenders = offenders();
            if (offenders.isEmpty()) {
                return false;
            }
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("error", "refused: tenant(s) "
                + offenders.stream().map(o -> String.valueOf(o.get("tenant"))).toList() + " " + why);
            out.put("reason", "tenant_violates_constraint");
            out.put("tenants", offenders);
            out.put("tenants_checked", new ArrayList<>(checked));
            out.put("table_rows_examined", rowsExamined);
            HttpUtil.send(ex, 409, json(out));
            return true;
        }
    }

    /**
     * The authoritative look: every tenant with manifest rows, read on the admin session. A tenant-scoped count of each
     * tenant the engine knows is taken FIRST (a concurrent insert then only makes the census larger) and the census
     * may not be below it for any of them.
     */
    private Survey survey(ManifestChunkIndexCheckAdmin admin) {
        Set<String> checked = new TreeSet<>();
        checked.add(TenantConstants.DEFAULT_TENANT);
        checked.addAll(tenants.get());
        Map<String, Long> scoped = new LinkedHashMap<>();
        for (String t : checked) {
            scoped.put(t, scopedRows(t));
        }
        List<ManifestChunkIndexCheckAdmin.TenantCensus> census = admin.census();
        Map<String, Long> seen = new TreeMap<>();
        for (var c : census) {
            seen.merge(c.tenant(), c.rows(), Long::sum);
        }
        List<String> undercounted = ChunkKeysAdminHandler.undercounted(seen, scoped);
        if (!undercounted.isEmpty()) {
            throw new AdminDdlSession.Refused(503, "admin_probe_undercount",
                "the table-wide census counted fewer manifest rows than a tenant-scoped count for tenant(s) "
                    + undercounted + ": the admin session is not reading every row, so no constraint check can be "
                    + "trusted. Nothing was changed; if rows were deleted while this ran, retry");
        }
        long rows = 0;
        for (var c : census) {
            checked.add(c.tenant());
            rows += c.rows();
        }
        return new Survey(checked, rows, census);
    }

    /** Every manifest row of {@code tenant}, counted through the tenant-scoped session: what the app role sees. */
    private long scopedRows(String tenant) {
        return scope.withTenant(tenant, ctx -> {
            PgSession.setStatementAndLockBounds(ctx, SCOPED_COUNT_STATEMENT_MS, SCOPED_COUNT_LOCK_MS);
            return (long) ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant));
        });
    }

    private static Map<String, Object> constraint(String action, AdminDdlSession.State state) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name", ManifestChunkIndexCheckAdmin.CONSTRAINT);
        if (action != null) {
            m.put("action", action);
        }
        m.put("present", state.present());
        m.put("validated", state.validated());
        m.put("definition", state.definition());
        return m;
    }

    private static String json(Object value) throws IOException {
        return MAPPER.writeValueAsString(value);
    }
}
