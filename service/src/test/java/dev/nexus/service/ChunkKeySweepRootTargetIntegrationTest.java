// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.GC_AUDIT;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.db.TenantConstants;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * The root credential names the target tenant of the retire-keys sweep (the operator path on a cloud whose root token
 * is bound to the default tenant and nothing off-host mints a tenant token). Through the real service, its
 * {@code AuthFilter} and a real PostgreSQL, each assertion against rows a superuser reads back.
 */
class ChunkKeySweepRootTargetIntegrationTest extends ChunkKeysTestBase {

    private static final String OPERATION = "retire_keys_sweep";

    private static Map<String, Object> legacyMeta(Map<String, Object> keyed) {
        Map<String, Object> m = new LinkedHashMap<>(keyed);
        m.put("title", "a title");
        m.put("source_path", "/x/y");
        return m;
    }

    /** A registered tenant (it holds a token) with {@code n} swept-able rows in one collection. */
    private String tenantWithRows(String tenant, int n) throws Exception {
        tenantToken(tenant);
        return rowsFor(tenant, tenant, n);
    }

    /** {@code n} swept-able rows for {@code tenant} in the collection {@code slug} names (registered if need be). */
    private String rowsFor(String tenant, String slug, int n) throws Exception {
        String col = conformant(slug);
        Map<String, Map<String, Object>> rows = new LinkedHashMap<>();
        for (int i = 1; i <= n; i++) {
            rows.put("r" + i, legacyMeta(keyed(tenant, col, "r" + i)));
        }
        seed(tenant, col, rows);
        return col;
    }

    private Map<String, Object> apply(Object... extra) {
        Object[] all = new Object[extra.length + 2];
        all[0] = "dry_run";
        all[1] = false;
        System.arraycopy(extra, 0, all, 2, extra.length);
        return plan(all);
    }

    private List<Map<String, Object>> auditRows(String tenant) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES)
                .select(GC_AUDIT.ACTOR, GC_AUDIT.DRY_RUN, GC_AUDIT.DETAILS)
                .from(GC_AUDIT)
                .where(GC_AUDIT.TENANT_ID.eq(tenant)).and(GC_AUDIT.OPERATION.eq(OPERATION))
                .orderBy(GC_AUDIT.ID)
                .fetch(r -> {
                    Map<String, Object> m = new LinkedHashMap<>();
                    m.put("actor", r.get(GC_AUDIT.ACTOR));
                    m.put("dry_run", r.get(GC_AUDIT.DRY_RUN));
                    try {
                        m.put("details", MAPPER.readTree(r.get(GC_AUDIT.DETAILS).data()));
                    } catch (Exception e) {
                        throw new IllegalStateException(e);
                    }
                    return m;
                });
        }
    }

    private long auditCountAnywhere() throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).fetchCount(GC_AUDIT, GC_AUDIT.OPERATION.eq(OPERATION));
        }
    }

    @Test
    void rootDryRunNamesARegisteredTenant_countsItsRows_writesNoChunk_andLeavesAnAuditRow() throws Exception {
        String t = "rt-dry";
        String col = tenantWithRows(t, 3);
        String other = "rt-dry-other";
        String otherCol = tenantWithRows(other, 2);

        HttpResponse<String> r = sweep(ROOT, plan("tenant", t));

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode body = json(r);
        assertThat(body.path("tenant").asText()).as("the answer names the target").isEqualTo(t);
        assertThat(body.path("totals").path("sweepable_rows").asLong()).isEqualTo(3);
        assertThat(retiredRowCount(t, col)).as("a dry run writes no chunk").isEqualTo(3);
        assertThat(retiredRowCount(other, otherCol)).isEqualTo(2);
        List<Map<String, Object>> audit = auditRows(t);
        assertThat(audit).hasSize(1);
        assertThat(audit.get(0).get("dry_run")).isEqualTo(true);
        JsonNode d = (JsonNode) audit.get(0).get("details");
        assertThat(d.path("target_tenant").asText()).isEqualTo(t);
        assertThat(d.path("caller_tenant").asText()).isEqualTo(TenantConstants.DEFAULT_TENANT);
        assertThat(d.path("mode").asText()).isEqualTo("dry-run");
        assertThat(d.path("counts").path("sweepable_rows").asLong()).isEqualTo(3);
        assertThat((String) audit.get(0).get("actor")).startsWith("root:").doesNotContain(ROOT);
        assertThat(auditRows(other)).as("no audit row in a tenant that was not named").isEmpty();
    }

    @Test
    void rootApplySweepsOnlyTheNamedTenant_inItsScope_andAuditsTheCounts() throws Exception {
        String t = "rt-apply";
        String col = tenantWithRows(t, 4);
        String other = "rt-apply-other";
        String otherCol = tenantWithRows(other, 2);
        String defaultCol = rowsFor(TenantConstants.DEFAULT_TENANT, "rt-apply-default", 2);

        HttpResponse<String> r = sweep(ROOT, apply("tenant", t));

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode body = json(r);
        assertThat(body.path("tenant").asText()).isEqualTo(t);
        assertThat(body.path("rows_swept").asLong()).isEqualTo(4);
        assertThat(body.path("done").asBoolean()).isTrue();
        assertThat(retiredRowCount(t, col)).isZero();
        assertThat(retiredRowCount(other, otherCol)).as("another tenant is untouched").isEqualTo(2);
        assertThat(retiredRowCount(TenantConstants.DEFAULT_TENANT, defaultCol))
            .as("the root credential's own tenant is untouched").isEqualTo(2);
        List<Map<String, Object>> audit = auditRows(t);
        assertThat(audit).hasSize(1);
        assertThat(audit.get(0).get("dry_run")).isEqualTo(false);
        JsonNode d = (JsonNode) audit.get(0).get("details");
        assertThat(d.path("mode").asText()).isEqualTo("apply");
        assertThat(d.path("target_tenant").asText()).isEqualTo(t);
        assertThat(d.path("counts").path("rows_swept").asLong()).isEqualTo(4);
        assertThat(auditRows(TenantConstants.DEFAULT_TENANT)).as("the audit lives in the target tenant").isEmpty();
    }

    @Test
    void anUnregisteredTenantIsRefused_noTenantTokenNoSweepNoAudit() throws Exception {
        String ghost = "rt-ghost";
        String col = rowsFor(ghost, ghost, 2);   // chunk rows, but no credential ever issued: not registered
        long auditsBefore = auditCountAnywhere();

        for (Map<String, Object> body : List.of(plan("tenant", ghost), apply("tenant", ghost))) {
            HttpResponse<String> r = sweep(ROOT, body);
            assertThat(r.statusCode()).as(r.body()).isEqualTo(422);
            assertThat(json(r).path("reason").asText()).isEqualTo("unknown_tenant");
        }
        assertThat(retiredRowCount(ghost, col)).isEqualTo(2);
        assertThat(auditCountAnywhere()).isEqualTo(auditsBefore);
        assertThat(sweep(ROOT, plan("tenant", TenantConstants.BOOTSTRAP_ANY_TENANT)).statusCode()).isEqualTo(422);
    }

    @Test
    void aTenantCredentialNamingAnotherTenantIsRefused_butMayNameItsOwn() throws Exception {
        String a = "rt-ta";
        String b = "rt-tb";
        String tokenA = tenantToken(a);
        String colA = rowsFor(a, a, 2);
        String colB = tenantWithRows(b, 2);
        long auditsBefore = auditCountAnywhere();

        for (Map<String, Object> body : List.of(plan("tenant", b), apply("tenant", b))) {
            HttpResponse<String> r = sweep(tokenA, body);
            assertThat(r.statusCode()).as(r.body()).isEqualTo(403);
            assertThat(json(r).path("reason").asText()).isEqualTo("tenant_mismatch");
        }
        assertThat(retiredRowCount(b, colB)).isEqualTo(2);
        assertThat(retiredRowCount(a, colA)).isEqualTo(2);

        HttpResponse<String> own = sweep(tokenA, apply("tenant", a));
        assertThat(own.statusCode()).as(own.body()).isEqualTo(200);
        assertThat(json(own).path("tenant").asText()).isEqualTo(a);
        assertThat(retiredRowCount(a, colA)).isZero();
        assertThat(auditCountAnywhere()).as("only a root-targeted call is audited").isEqualTo(auditsBefore);
    }

    @Test
    void aBadTenantFieldIsABadRequest() throws Exception {
        assertThat(sweep(ROOT, plan("tenant", 7)).statusCode()).isEqualTo(400);
        assertThat(sweep(ROOT, plan("tenant", "  ")).statusCode()).isEqualTo(400);
    }

    @Test
    void aResumeTokenIsBoundToItsTenant_aRootCallForAnotherTenantIgnoresIt() throws Exception {
        String a = "rt-ra";
        String b = "rt-rb";
        String colA = tenantWithRows(a, 3);
        String colB = tenantWithRows(b, 3);
        JsonNode first = json(sweep(ROOT, apply("tenant", a, "batch_size", 1, "max_batches", 1)));
        assertThat(first.path("done").asBoolean()).isFalse();
        Map<String, Object> resume = MAPPER.convertValue(first.path("resume"), new TypeReference<>() { });

        JsonNode asB = json(sweep(ROOT, apply("tenant", b, "batch_size", 1, "max_batches", 1, "resume", resume)));
        assertThat(asB.path("tenant").asText()).isEqualTo(b);
        assertThat(asB.path("resume_ignored").asBoolean()).as("a token minted for A is not B's").isTrue();

        JsonNode asA = json(sweep(ROOT, apply("tenant", a, "batch_size", 1, "max_batches", 1, "resume", resume)));
        assertThat(asA.path("resume_ignored").asBoolean()).as("A's own token is taken").isFalse();
        assertThat(retiredRowCount(a, colA)).isEqualTo(1);
        assertThat(retiredRowCount(b, colB)).as("B swept one batch, on its own first-call checks").isEqualTo(2);
    }

    @Test
    void controlAnOldClientsRootCallThatNamesNoTenantSweepsItsOwn_asBefore_andIsNotAudited() throws Exception {
        long auditsBefore = auditCountAnywhere();
        rowsFor(TenantConstants.DEFAULT_TENANT, "rt-none-default", 1);

        JsonNode body = json(sweep(ROOT, plan()));

        assertThat(body.path("tenant").asText()).isEqualTo(TenantConstants.DEFAULT_TENANT);
        assertThat(auditCountAnywhere()).isEqualTo(auditsBefore);
    }
}
