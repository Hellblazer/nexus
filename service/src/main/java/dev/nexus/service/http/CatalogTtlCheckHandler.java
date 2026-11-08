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
 *   POST /v1/admin/catalog-ttl-check/census    changes no data (as the table owner it holds a table lock for the
 *                                              scan, see below): per tenant, the entries that carry a TTL and those the
 *                                              CHECK would refuse
 *   POST /v1/admin/catalog-ttl-check/add       the CHECK, NOT VALID; refused while any tenant has a violator
 *   POST /v1/admin/catalog-ttl-check/validate  VALIDATE CONSTRAINT; refused while any tenant has a violator
 * </pre>
 *
 * <p><b>The count is all tenants.</b> Each route that counts reads {@link CatalogTtlCheckAdmin#census} on the admin
 * session, which sees the tenants with no {@code service_tokens} row. It reads them as a superuser or BYPASSRLS role,
 * or as the table's owner with FORCE row-level security lifted for the read (an ACCESS EXCLUSIVE lock held for the
 * scan, bounded; see {@link AdminDdlSession#readEveryTenant}), and refuses (503
 * {@code admin_cannot_see_all_tenants}) any other role the table's FORCE row-level security filters. As a second
 * line, a tenant-scoped row count of each tenant the engine knows is taken first, and the census may not be below it
 * for any of them (503 {@code admin_probe_undercount}). A refusal names the tenants (409
 * {@code tenant_violates_constraint}); the answer lists {@code tenants_checked}.
 *
 * <p><b>Idempotence.</b> {@code add} and {@code validate} both read the constraint's state first, before any count: on
 * an existing constraint {@code add} answers {@code present} and {@code validate} answers {@code already_valid}
 * ({@code pg_constraint.convalidated}; a VALID CHECK cannot have a violator), or {@code absent} when there is nothing
 * to validate, so a rerun needs no all-tenant view and cannot be refused by a racing writer. DATA EFFECT of add: a NOT
 * VALID CHECK is evaluated on every new row version, so an entry that breaks it could no longer be updated or
 * tombstoned; the route refuses while the count reads one. The count and the ADD are two statements, so a writer
 * that stores a TTL with no put_at between them can still leave one: the answer then carries
 * {@code violating_after_add} (a second count taken after the ADD, final because the CHECK refuses later ones) and a
 * warning naming the tenants. DATA EFFECT of validate: none.
 *
 * <p><b>Synchronous.</b> The census (up to 10 minutes) and validate (up to 30) answer when they finish. The public
 * edge times a request out after about 30 seconds and the engine carries on. Add and validate count BEFORE they take
 * the run lock, so a retry is answered 409 {@code constraint_run_in_progress} only once the constraint statement is
 * running; a retry during the count runs a second count (a superuser or BYPASSRLS role in parallel; the table owner
 * waits behind the first count's table lock and is answered 503 {@code lock_timeout} after 5 seconds). The census
 * holds no run lock.
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
        // the state first, as validate does: a constraint that is already there is answered without a survey, so a
        // rerun with a blind admin role or a racing writer is still "present", not a 503 or a 409
        AdminDdlSession.State state = admin.state();
        if (state.present()) {
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("census_skipped", "the constraint is already present");
            out.put("constraint", constraint("present", state));
            HttpUtil.send(ex, 200, json(out));
            return;
        }
        Survey survey = survey(admin);
        if (survey.refuse(ex, "are not ready for the constraint: an entry with a TTL and no put_at would be frozen "
            + "(it could not be updated or tombstoned) by a NOT VALID CHECK")) {
            return;
        }
        CatalogTtlCheckAdmin.Action action = admin.add();
        Map<String, Object> out = survey.body();
        out.put("constraint", constraint(action.action(), action.after()));
        if ("added".equals(action.action())) {
            countAfterAdd(out, admin);
        }
        HttpUtil.send(ex, 200, json(out));
    }

    /**
     * The count again, after the ADD: the survey and the ADD are two statements with no lock between them, so a
     * writer that stored a TTL with no put_at in that window left an entry the new CHECK now freezes. The CHECK
     * refuses every later such write, so this count is final. A failure to take it, of any kind, does not undo the ADD, and
     * the answer says so ({@code violating_after_add} null, a warning that the constraint is in place).
     */
    private void countAfterAdd(Map<String, Object> out, CatalogTtlCheckAdmin admin) {
        try {
            Survey after = survey(admin);
            out.put("violating_after_add", after.violatingTotal());
            out.put("violators_after_add", after.offenders());
            if (after.violatingTotal() > 0) {
                out.put("warning", "the constraint was added, but " + after.violatingTotal() + " entr"
                    + (after.violatingTotal() == 1 ? "y" : "ies") + " with a TTL and no put_at appeared between the "
                    + "count and the add (see violators_after_add). They are frozen: they cannot be updated or "
                    + "tombstoned until a put_at is stamped on them. Stamp them (the backfill, or the "
                    + "remedies in the runbook's tombstoned-violators section), then count again before validate");
            }
        } catch (RuntimeException e) {
            // any failure, not only a refusal: the ADD has landed, so the answer must say so (a DataAccessException
            // from the second survey, a lock timeout behind an owner census, a read error)
            String why;
            if (e instanceof AdminDdlSession.Refused r) {
                why = r.reason() + "): " + r.getMessage();
            } else {
                log.error("event=catalog_ttl_check_count_after_add_failed error={}", e.getMessage(), e);
                why = "internal_error): see the engine log";
            }
            out.put("violating_after_add", null);
            out.put("warning", "the constraint was added, but the count after it could not be taken (" + why
                + ". Run the census to see whether an entry was frozen");
        }
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

        long violatingTotal() {
            long violating = 0;
            for (var c : census) {
                violating += c.violating();
            }
            return violating;
        }

        /** The tenants with a violator: each with its count and the tombstoned part of it. */
        List<Map<String, Object>> offenders() {
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
            return offenders;
        }

        Map<String, Object> body() {
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("tenants_checked", new ArrayList<>(checked));
            out.put("table_rows_examined", rowsExamined);
            long violating = violatingTotal();
            List<Map<String, Object>> rows = new ArrayList<>();
            for (var c : census) {
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
