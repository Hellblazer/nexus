// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.AdminDdlSession;
import dev.nexus.service.db.CatalogTtlCheckAdmin;
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
 * RDR-236 (nexus-6pfgb.44): the three engine-admin routes of the catalog TTL CHECK, a step of its own and not part of
 * the chunk-table routes ({@link ChunkKeysAdminHandler}). Under {@code /v1/admin/catalog-ttl-check}, all POST, table
 * wide, root credential ({@link RequestContext#isOperator()}) only, none called by anything on its own: an operator
 * sends them, after the Phase 4 Step 2c paired cut and the backfill that stamps {@code put_at}.
 *
 * <pre>
 *   POST /v1/admin/catalog-ttl-check/census    read only: per tenant, the entries that carry a TTL and those the
 *                                              CHECK would refuse
 *   POST /v1/admin/catalog-ttl-check/add       the CHECK, NOT VALID; refused while any tenant has a violator
 *   POST /v1/admin/catalog-ttl-check/validate  VALIDATE CONSTRAINT; refused while any tenant has a violator
 * </pre>
 *
 * <p><b>The count is all tenants.</b> Each route reads {@link CatalogTtlCheckAdmin#census} on the admin session, which
 * sees the tenants with no {@code service_tokens} row and refuses (503 {@code admin_cannot_see_all_tenants}) when the
 * role is row filtered under the table's FORCE row-level security. As a second line, a tenant-scoped row count of
 * each tenant the engine knows is taken first, and the census may not be below it for any of them (503
 * {@code admin_probe_undercount}). A refusal names the tenants (409 {@code tenant_violates_constraint}); the answer
 * lists {@code tenants_checked}.
 *
 * <p><b>Idempotence.</b> {@code add} on an existing constraint is {@code present}; {@code validate} reads
 * {@code pg_constraint.convalidated} first and answers {@code already_valid} without counting or scanning (a VALID
 * CHECK cannot have a violator), and {@code absent} when there is nothing to validate. DATA EFFECT of add: a NOT VALID
 * CHECK is evaluated on every new row version, so an entry that breaks it could no longer be updated or tombstoned;
 * the route refuses while one exists, so none is frozen. DATA EFFECT of validate: none.
 *
 * <p>Nothing in the engine schedules or calls these routes; {@code NexusService} mounts the handler and {@code Main}
 * binds the admin connection.
 */
public final class CatalogTtlCheckHandler implements HttpHandler {

    public static final String PATH = "/v1/admin/catalog-ttl-check";

    private static final Logger log = LoggerFactory.getLogger(CatalogTtlCheckHandler.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final int SCOPED_COUNT_STATEMENT_MS = 120_000;
    private static final int SCOPED_COUNT_LOCK_MS = 2_000;

    private final Supplier<CatalogTtlCheckAdmin> adminSource;
    private final Supplier<List<String>> tenants;
    private final TenantScope scope;

    /**
     * @param adminSource the admin-connection DDL, bound late; null (or a supplier answering null) is 503
     * @param tenants     every tenant that holds a {@code service_tokens} row ({@code TokenStore#listKnownTenants})
     * @param scope       the tenant-scoped session the second-line row counts are taken through
     */
    public CatalogTtlCheckHandler(Supplier<CatalogTtlCheckAdmin> adminSource, Supplier<List<String>> tenants,
                                  TenantScope scope) {
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
                    "operator privilege required: the catalog TTL check routes need the root token")));
                return;
            }
            CatalogTtlCheckAdmin admin = adminSource == null ? null : adminSource.get();
            if (admin == null) {
                HttpUtil.send(ex, 503, json(Map.of("error",
                    "this engine has no admin connection bound for the catalog TTL check")));
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
            if (!HttpUtil.sendTypedDbError(ex, e, log, "catalog_ttl_check", "op=" + op + " tenant=" + tenant)) {
                log.error("event=catalog_ttl_check_error op={} tenant={} error={}", op, tenant, e.getMessage(), e);
                HttpUtil.send(ex, 500, "{\"error\":\"internal server error\"}");
            }
        }
    }

    // ---- routes -------------------------------------------------------------------------------------------------

    private void handleCensus(HttpExchange ex, CatalogTtlCheckAdmin admin) throws IOException {
        Survey survey = survey(admin);
        Map<String, Object> out = survey.body();
        out.put("constraint", constraint(null, admin.state()));
        HttpUtil.send(ex, 200, json(out));
    }

    private void handleAdd(HttpExchange ex, CatalogTtlCheckAdmin admin) throws IOException {
        Survey survey = survey(admin);
        if (survey.refuse(ex, "are not ready for the constraint: an entry with a TTL and no put_at would be frozen "
            + "(it could not be updated or tombstoned) by a NOT VALID CHECK")) {
            return;
        }
        CatalogTtlCheckAdmin.Action action = admin.add();
        Map<String, Object> out = survey.body();
        out.put("constraint", constraint(action.action(), action.after()));
        HttpUtil.send(ex, 200, json(out));
    }

    private void handleValidate(HttpExchange ex, CatalogTtlCheckAdmin admin) throws IOException {
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
        if (survey.refuse(ex, "still hold an entry with a TTL and no put_at")) {
            return;
        }
        CatalogTtlCheckAdmin.Action action = admin.validate();
        Map<String, Object> out = survey.body();
        out.put("constraint", constraint(action.action(), action.after()));
        HttpUtil.send(ex, 200, json(out));
    }

    // ---- the all-tenant look ------------------------------------------------------------------------------------

    /** What one pass over the table found, and the tenants it covered. */
    private record Survey(Set<String> checked, long rowsExamined, List<CatalogTtlCheckAdmin.TenantCensus> census) {

        Map<String, Object> body() {
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("tenants_checked", new ArrayList<>(checked));
            out.put("table_rows_examined", rowsExamined);
            long violating = 0;
            List<Map<String, Object>> rows = new ArrayList<>();
            for (var c : census) {
                violating += c.violating();
                Map<String, Object> m = new LinkedHashMap<>();
                m.put("tenant", c.tenant());
                m.put("rows", c.rows());
                m.put("ttl_rows", c.ttlRows());
                m.put("violating_rows", c.violating());
                m.put("violating_tombstoned_rows", c.violatingTombstoned());
                rows.add(m);
            }
            out.put("violating_total", violating);
            out.put("tenants", rows);
            return out;
        }

        /** Answer 409 naming the tenants with a violator and return true, or return false when there is none. */
        boolean refuse(HttpExchange ex, String why) throws IOException {
            List<Map<String, Object>> offenders = new ArrayList<>();
            for (var c : census) {
                if (c.violating() > 0) {
                    Map<String, Object> m = new LinkedHashMap<>();
                    m.put("tenant", c.tenant());
                    m.put("violating_rows", c.violating());
                    m.put("violating_tombstoned_rows", c.violatingTombstoned());
                    offenders.add(m);
                }
            }
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
     * The authoritative look: every tenant with catalog entries, read on the admin session. A tenant-scoped count
     * of each tenant the engine knows is taken FIRST (a concurrent insert then only makes the census larger) and the
     * census may not be below it for any of them.
     */
    private Survey survey(CatalogTtlCheckAdmin admin) {
        Set<String> checked = new TreeSet<>();
        checked.add(TenantConstants.DEFAULT_TENANT);
        checked.addAll(tenants.get());
        Map<String, Long> scoped = new LinkedHashMap<>();
        for (String t : checked) {
            scoped.put(t, scopedRows(t));
        }
        List<CatalogTtlCheckAdmin.TenantCensus> census = admin.census();
        Map<String, Long> seen = new TreeMap<>();
        for (var c : census) {
            seen.merge(c.tenant(), c.rows(), Long::sum);
        }
        List<String> undercounted = ChunkKeysAdminHandler.undercounted(seen, scoped);
        if (!undercounted.isEmpty()) {
            throw new AdminDdlSession.Refused(503, "admin_probe_undercount",
                "the table-wide census counted fewer catalog entries than a tenant-scoped count for tenant(s) "
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

    /** Every catalog entry of {@code tenant}, counted through the tenant-scoped session: what the app role sees. */
    private long scopedRows(String tenant) {
        return scope.withTenant(tenant, ctx -> {
            PgSession.setStatementAndLockBounds(ctx, SCOPED_COUNT_STATEMENT_MS, SCOPED_COUNT_LOCK_MS);
            return (long) ctx.fetchCount(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID.eq(tenant));
        });
    }

    private static Map<String, Object> constraint(String action, AdminDdlSession.State state) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name", CatalogTtlCheckAdmin.CONSTRAINT);
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
