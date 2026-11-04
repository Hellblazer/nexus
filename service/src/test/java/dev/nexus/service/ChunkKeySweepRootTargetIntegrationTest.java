// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.GC_AUDIT;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TokenHashing;
import dev.nexus.service.db.TokenStore;
import dev.nexus.service.http.ChunkKeysAdminHandler;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * The root credential names the target tenant of the retire-keys sweep (the operator path on a cloud whose root token
 * is bound to the default tenant and nothing off-host mints a tenant token), and every sweep a root credential makes is
 * audited: an intent row before the first read, an outcome row after, both failing closed. Through the real service,
 * its {@code AuthFilter} and a real PostgreSQL, each assertion against rows a superuser reads back. The service runs
 * on the {@code svc_atomic_write_test} role: NOSUPERUSER NOBYPASSRLS and not the owner of the tables, so row-level
 * security binds every sweep here exactly as it binds the product's app role ({@link
 * #theServiceDataSourceIsTheRlsBoundAppRole}).
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

    private record AuditRow(long id, String actor, boolean dryRun, JsonNode details) {
        String phase() {
            return details.path("phase").asText();
        }
    }

    private List<AuditRow> auditRows(String tenant) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES)
                .select(GC_AUDIT.ID, GC_AUDIT.ACTOR, GC_AUDIT.DRY_RUN, GC_AUDIT.DETAILS)
                .from(GC_AUDIT)
                .where(GC_AUDIT.TENANT_ID.eq(tenant)).and(GC_AUDIT.OPERATION.eq(OPERATION))
                .orderBy(GC_AUDIT.ID)
                .fetch(r -> {
                    try {
                        return new AuditRow(r.get(GC_AUDIT.ID), r.get(GC_AUDIT.ACTOR), r.get(GC_AUDIT.DRY_RUN),
                            MAPPER.readTree(r.get(GC_AUDIT.DETAILS).data()));
                    } catch (Exception e) {
                        throw new IllegalStateException(e);
                    }
                });
        }
    }

    private long auditCountAnywhere() throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).fetchCount(GC_AUDIT, GC_AUDIT.OPERATION.eq(OPERATION));
        }
    }

    @FunctionalInterface
    private interface Body {
        void run() throws Exception;
    }

    /** Run {@code body} with the engine's audit sink replaced by {@code sink} (null: no sink), then restore it. */
    private void withSink(ChunkKeysAdminHandler.Audit sink, Body body) throws Exception {
        ChunkKeysAdminHandler.Audit original = service.chunkKeysAudit();
        service.chunkKeysAudit(sink);
        try {
            body.run();
        } finally {
            service.chunkKeysAudit(original);
        }
    }

    // ---- naming a tenant ---------------------------------------------------------------------------------------

    @Test
    void rootDryRunNamesARegisteredTenant_countsItsRows_writesNoChunk_andLeavesIntentAndOutcomeRows() throws Exception {
        String t = "rt-dry";
        String col = tenantWithRows(t, 3);
        String other = "rt-dry-other";
        String otherCol = tenantWithRows(other, 2);

        HttpResponse<String> r = sweep(ROOT, plan("tenant", t, "operator", "sam-laptop"));

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode body = json(r);
        assertThat(body.path("tenant").asText()).as("the answer names the target").isEqualTo(t);
        assertThat(body.path("totals").path("sweepable_rows").asLong()).isEqualTo(3);
        assertThat(retiredRowCount(t, col)).as("a dry run writes no chunk").isEqualTo(3);
        assertThat(retiredRowCount(other, otherCol)).isEqualTo(2);
        List<AuditRow> audit = auditRows(t);
        assertThat(audit).hasSize(2);
        AuditRow intent = audit.get(0);
        AuditRow outcome = audit.get(1);
        assertThat(intent.phase()).isEqualTo("started");
        assertThat(outcome.phase()).isEqualTo("finished");
        assertThat(intent.dryRun()).isTrue();
        JsonNode d = intent.details();
        assertThat(d.path("target_tenant").asText()).isEqualTo(t);
        assertThat(d.path("caller_tenant").asText()).isEqualTo(TenantConstants.DEFAULT_TENANT);
        assertThat(d.path("mode").asText()).isEqualTo("dry-run");
        assertThat(d.path("batch_size").asInt()).isPositive();
        assertThat(d.path("resumed").asBoolean()).isFalse();
        assertThat(d.path("accept").isObject()).as("the accept map is recorded, empty here").isTrue();
        assertThat(d.path("operator").asText()).as("the optional operator label").isEqualTo("sam-laptop");
        assertThat(outcome.details().path("counts").path("sweepable_rows").asLong()).isEqualTo(3);
        assertThat(outcome.details().path("intent_audit_id").asLong()).isEqualTo(intent.id());
        assertThat(outcome.details().path("operator").asText()).isEqualTo("sam-laptop");
        assertThat(body.path("audit").path("intent_id").asLong()).as("the answer carries the row ids")
            .isEqualTo(intent.id());
        assertThat(body.path("audit").path("outcome_id").asLong()).isEqualTo(outcome.id());
        assertThat(body.path("audit").path("actor").asText()).isEqualTo(intent.actor());
        assertThat(auditRows(other)).as("no audit row in a tenant that was not named").isEmpty();
    }

    @Test
    void rootApplySweepsOnlyTheNamedTenant_inItsScope_andAuditsIntentAcceptAndCounts() throws Exception {
        String t = "rt-apply";
        String col = tenantWithRows(t, 4);
        String other = "rt-apply-other";
        String otherCol = tenantWithRows(other, 2);
        String defaultCol = rowsFor(TenantConstants.DEFAULT_TENANT, "rt-apply-default", 2);
        long defaultAuditsBefore = auditRows(TenantConstants.DEFAULT_TENANT).size();

        HttpResponse<String> r = sweep(ROOT, apply("tenant", t, "accept", accept("keyless_notes", 0), "batch_size", 2));

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode body = json(r);
        assertThat(body.path("tenant").asText()).isEqualTo(t);
        assertThat(body.path("rows_swept").asLong()).isEqualTo(4);
        assertThat(body.path("done").asBoolean()).isTrue();
        assertThat(retiredRowCount(t, col)).isZero();
        assertThat(retiredRowCount(other, otherCol)).as("another tenant is untouched").isEqualTo(2);
        assertThat(retiredRowCount(TenantConstants.DEFAULT_TENANT, defaultCol))
            .as("the root credential's own tenant is untouched").isEqualTo(2);
        List<AuditRow> audit = auditRows(t);
        assertThat(audit).hasSize(2);
        assertThat(audit.get(0).phase()).isEqualTo("started");
        assertThat(audit.get(0).dryRun()).isFalse();
        assertThat(audit.get(0).details().path("accept").path("keyless_notes").asLong()).isZero();
        assertThat(audit.get(0).details().path("batch_size").asInt()).isEqualTo(2);
        assertThat(audit.get(0).details().path("mode").asText()).isEqualTo("apply");
        assertThat(audit.get(1).phase()).isEqualTo("finished");
        assertThat(audit.get(1).details().path("counts").path("rows_swept").asLong()).isEqualTo(4);
        assertThat(auditRows(TenantConstants.DEFAULT_TENANT)).as("the audit lives in the target tenant")
            .hasSize((int) defaultAuditsBefore);
    }

    @Test
    void anUnregisteredTenantIsRefused_noTokenNoChunkRowsNoSweepNoAudit() throws Exception {
        String ghost = "rt-ghost";   // no credential and no chunk row anywhere: not registered
        long auditsBefore = auditCountAnywhere();

        for (Map<String, Object> body : List.of(plan("tenant", ghost), apply("tenant", ghost))) {
            HttpResponse<String> r = sweep(ROOT, body);
            assertThat(r.statusCode()).as(r.body()).isEqualTo(422);
            assertThat(json(r).path("reason").asText()).isEqualTo("unknown_tenant");
        }
        assertThat(auditCountAnywhere()).isEqualTo(auditsBefore);
        assertThat(sweep(ROOT, plan("tenant", TenantConstants.BOOTSTRAP_ANY_TENANT)).statusCode()).isEqualTo(422);
        assertThat(auditCountAnywhere()).as("the wildcard is refused too").isEqualTo(auditsBefore);
    }

    @Test
    void aTenantWithChunkRowsButNoTokenRowIsAccepted_theProbeFindsIt() throws Exception {
        String t = "rt-rows-no-token";
        String col = rowsFor(t, t, 3);   // chunk rows, no service_tokens row ever issued

        JsonNode dry = json(sweep(ROOT, plan("tenant", t)));
        assertThat(dry.path("tenant").asText()).isEqualTo(t);
        assertThat(dry.path("totals").path("sweepable_rows").asLong()).isEqualTo(3);
        HttpResponse<String> applied = sweep(ROOT, apply("tenant", t));

        assertThat(applied.statusCode()).as(applied.body()).isEqualTo(200);
        assertThat(retiredRowCount(t, col)).isZero();
        assertThat(auditRows(t)).hasSize(4);
    }

    @Test
    void aTenantWhoseOnlyTokenIsRevokedIsStillRegistered() throws Exception {
        String t = "rt-revoked";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), "tok-revoked-" + t + "-0123456789abcdef",
                t, "old", TokenStore.SCOPE_TENANT, null, OffsetDateTime.parse("2026-01-01T00:00:00Z"));
        }

        HttpResponse<String> r = sweep(ROOT, plan("tenant", t));

        assertThat(r.statusCode()).as("a revoked row still registers the tenant: " + r.body()).isEqualTo(200);
        assertThat(auditRows(t)).hasSize(2);
    }

    @Test
    void caseWhitespaceAndZeroWidthVariantsOfARealTenantAreNotRegistered_422AndNoAuditRow() throws Exception {
        String t = "rt-var";
        String col = tenantWithRows(t, 2);
        long auditsBefore = auditCountAnywhere();

        List<String> variants = List.of("RT-VAR", "Rt-Var", " rt-var", "rt-var ", "rt-var​", "​rt-var",
            "rt-var\u0000", "rt-var\n", "rt-var%", "rt-var*");
        for (String v : variants) {
            for (Map<String, Object> body : List.of(plan("tenant", v), apply("tenant", v))) {
                HttpResponse<String> r = sweep(ROOT, body);
                assertThat(r.statusCode()).as("variant %s: %s".formatted(v.replace("\u0000", "NUL"), r.body()))
                    .isEqualTo(422);
                assertThat(json(r).path("reason").asText()).isEqualTo("unknown_tenant");
            }
        }
        assertThat(retiredRowCount(t, col)).as("the real tenant is untouched").isEqualTo(2);
        assertThat(auditCountAnywhere()).as("no variant left an audit row").isEqualTo(auditsBefore);
        assertThat(auditRows(t)).isEmpty();
    }

    @Test
    void aTenantCredentialNamingAnotherTenantIsRefused_butMayNameItsOwn_andIsNotAudited() throws Exception {
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
        assertThat(json(own).has("audit")).as("a tenant credential's sweep carries no audit block").isFalse();
        assertThat(retiredRowCount(a, colA)).isZero();
        assertThat(auditCountAnywhere()).as("only a root credential's sweep is audited").isEqualTo(auditsBefore);
    }

    @Test
    void aDataOrMintCredentialNamingAnotherTenantGets403_noTenantMismatchReason_noSweep() throws Exception {
        String a = "rt-scope-a";
        String b = "rt-scope-b";
        String colB = tenantWithRows(b, 2);
        long auditsBefore = auditCountAnywhere();

        for (String scope : List.of(TokenStore.SCOPE_DATA, TokenStore.SCOPE_MINT)) {
            String raw = token(a, scope);
            for (Map<String, Object> body : List.of(plan("tenant", b), apply("tenant", b))) {
                HttpResponse<String> r = sweep(raw, body);
                assertThat(r.statusCode()).as(scope + ": " + r.body()).isEqualTo(403);
                JsonNode j = json(r);
                assertThat(j.has("reason")).as("the scope guard carries no reason code").isFalse();
                if (TokenStore.SCOPE_DATA.equals(scope)) {
                    assertThat(j.path("error").asText()).as("the handler's scope guard").contains("scope data");
                } else {
                    // A mint credential never reaches the handler: the filter refuses the path.
                    assertThat(j.path("error").asText()).isEqualTo("forbidden");
                }
            }
        }
        assertThat(retiredRowCount(b, colB)).isEqualTo(2);
        assertThat(auditCountAnywhere()).isEqualTo(auditsBefore);
    }

    @Test
    void aBadTenantFieldOrOperatorLabelIsABadRequest_andLeavesNoAuditRow() throws Exception {
        long auditsBefore = auditCountAnywhere();
        assertThat(sweep(ROOT, plan("tenant", 7)).statusCode()).isEqualTo(400);
        assertThat(sweep(ROOT, plan("tenant", "  ")).statusCode()).isEqualTo(400);
        assertThat(sweep(ROOT, plan("operator", 7)).statusCode()).isEqualTo(400);
        assertThat(sweep(ROOT, plan("operator", "  ")).statusCode()).isEqualTo(400);
        assertThat(sweep(ROOT, plan("operator", "x".repeat(81))).statusCode()).isEqualTo(400);
        assertThat(sweep(ROOT, plan("operator", "a\nb")).statusCode()).isEqualTo(400);
        assertThat(auditCountAnywhere()).as("a request refused as malformed is refused before the audit").isEqualTo(auditsBefore);
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
        List<AuditRow> auditA = auditRows(a);
        assertThat(auditA.get(auditA.size() - 2).details().path("resumed").asBoolean())
            .as("the intent row says the call carried a resume object").isTrue();
        assertThat(auditA.get(auditA.size() - 1).details().path("counts").path("resumed").asBoolean()).isTrue();
    }

    // ---- every root sweep is audited ---------------------------------------------------------------------------

    @Test
    void aRootCallThatNamesNoTenant_oneThatNamesItsOwn_isAudited_inTheCallersTenant() throws Exception {
        rowsFor(TenantConstants.DEFAULT_TENANT, "rt-none-default", 1);
        int before = auditRows(TenantConstants.DEFAULT_TENANT).size();

        JsonNode unnamed = json(sweep(ROOT, plan()));
        JsonNode own = json(sweep(ROOT, plan("tenant", TenantConstants.DEFAULT_TENANT)));

        assertThat(unnamed.path("tenant").asText()).isEqualTo(TenantConstants.DEFAULT_TENANT);
        assertThat(own.path("tenant").asText()).isEqualTo(TenantConstants.DEFAULT_TENANT);
        List<AuditRow> audit = auditRows(TenantConstants.DEFAULT_TENANT);
        assertThat(audit).as("two calls, an intent and an outcome row each").hasSize(before + 4);
        assertThat(unnamed.path("audit").path("intent_id").asLong()).isPositive();
        assertThat(audit.get(before).details().path("target_tenant").asText())
            .isEqualTo(TenantConstants.DEFAULT_TENANT);
    }

    @Test
    void anEngineWithNoAuditSinkRefusesEveryRootSweep_503_dryRunAndApply_nothingReadOrChanged() throws Exception {
        String t = "rt-nosink";
        String col = tenantWithRows(t, 3);
        String defaultCol = rowsFor(TenantConstants.DEFAULT_TENANT, "rt-nosink-default", 2);
        long auditsBefore = auditCountAnywhere();

        withSink(null, () -> {
            List<Map<String, Object>> bodies = new ArrayList<>();
            for (String named : new String[] {t, TenantConstants.DEFAULT_TENANT, null}) {
                for (boolean dry : new boolean[] {true, false}) {
                    Map<String, Object> m = dry ? plan() : apply();
                    if (named != null) m.put("tenant", named);
                    bodies.add(m);
                }
            }
            for (Map<String, Object> body : bodies) {
                HttpResponse<String> r = sweep(ROOT, body);
                assertThat(r.statusCode()).as(body + " -> " + r.body()).isEqualTo(503);
                assertThat(json(r).path("reason").asText()).isEqualTo("audit_unavailable");
            }
            // A tenant credential is not audited, so a missing sink does not stop it.
            assertThat(sweep(tenantToken(t), plan()).statusCode()).isEqualTo(200);
        });

        assertThat(retiredRowCount(t, col)).as("nothing was swept").isEqualTo(3);
        assertThat(retiredRowCount(TenantConstants.DEFAULT_TENANT, defaultCol)).isEqualTo(2);
        assertThat(auditCountAnywhere()).as("and no audit row was written").isEqualTo(auditsBefore);
    }

    @Test
    void anIntentRowThatCannotBeWrittenRefusesBeforeAnyBatch_dryRunAndApply() throws Exception {
        String t = "rt-intent-fail";
        String col = tenantWithRows(t, 3);
        AtomicInteger calls = new AtomicInteger();
        long auditsBefore = auditCountAnywhere();

        withSink((tenant, row) -> {
            calls.incrementAndGet();
            throw new IllegalStateException("gc_audit is not writable");
        }, () -> {
            for (Map<String, Object> body : List.of(plan("tenant", t), apply("tenant", t))) {
                HttpResponse<String> r = sweep(ROOT, body);
                assertThat(r.statusCode()).as(r.body()).isEqualTo(500);
                assertThat(json(r).path("reason").asText()).isEqualTo("audit_intent_failed");
                assertThat(json(r).path("error").asText()).contains("nothing was read or changed");
            }
        });

        assertThat(calls.get()).as("one attempt per call, the intent; the work never ran to reach an outcome")
            .isEqualTo(2);
        assertThat(retiredRowCount(t, col)).as("no row was swept").isEqualTo(3);
        assertThat(auditCountAnywhere()).isEqualTo(auditsBefore);
    }

    @Test
    void anOutcomeRowThatCannotBeWrittenAfterASweptBatch_is500WithCountsAndResume_andTheRowsReallyChanged()
            throws Exception {
        String t = "rt-outcome-fail";
        String col = tenantWithRows(t, 3);
        ChunkKeysAdminHandler.Audit real = service.chunkKeysAudit();
        AtomicInteger n = new AtomicInteger();

        withSink((tenant, row) -> {
            if (n.incrementAndGet() == 1) {
                return real.record(tenant, row);   // the intent goes in
            }
            throw new IllegalStateException("gc_audit went away after the batch");
        }, () -> {
            HttpResponse<String> r = sweep(ROOT, apply("tenant", t, "batch_size", 1, "max_batches", 1));

            assertThat(r.statusCode()).as(r.body()).isEqualTo(500);
            JsonNode j = json(r);
            assertThat(j.path("reason").asText()).isEqualTo("audit_write_failed");
            assertThat(j.path("rows_swept").asLong()).isEqualTo(1);
            assertThat(j.path("rows_changed").asBoolean()).isTrue();
            assertThat(j.path("done").asBoolean()).isFalse();
            assertThat(j.path("resume").path("sig").asText()).as("the continuation is handed back").isNotBlank();
            assertThat(j.path("audit").path("intent_id").asLong()).isPositive();
            assertThat(j.path("audit").path("outcome_id").isNull()).isTrue();
        });

        assertThat(retiredRowCount(t, col)).as("one row really was swept").isEqualTo(2);
        List<AuditRow> audit = auditRows(t);
        assertThat(audit).as("the intent row only").hasSize(1);
        assertThat(audit.get(0).phase()).isEqualTo("started");
    }

    @Test
    void aDryRunWhoseOutcomeRowCannotBeWrittenFailsClosed_noRowsChanged() throws Exception {
        String t = "rt-dry-outcome-fail";
        String col = tenantWithRows(t, 2);
        ChunkKeysAdminHandler.Audit real = service.chunkKeysAudit();
        AtomicInteger n = new AtomicInteger();

        withSink((tenant, row) -> {
            if (n.incrementAndGet() == 1) return real.record(tenant, row);
            throw new IllegalStateException("gc_audit went away");
        }, () -> {
            HttpResponse<String> r = sweep(ROOT, plan("tenant", t));
            assertThat(r.statusCode()).as(r.body()).isEqualTo(500);
            JsonNode j = json(r);
            assertThat(j.path("reason").asText()).isEqualTo("audit_write_failed");
            assertThat(j.path("rows_changed").asBoolean()).isFalse();
        });

        assertThat(retiredRowCount(t, col)).isEqualTo(2);
    }

    @Test
    void aPreconditionsRefusal409LeavesAnIntentAndARefusedOutcomeRow() throws Exception {
        String t = "rt-409";
        String col = tenantWithRows(t, 2);
        document(t, "note.1", "knowledge", col, null);   // a keyless note: a residue that blocks an unaccepted apply

        HttpResponse<String> r = sweep(ROOT, apply("tenant", t));

        assertThat(r.statusCode()).as(r.body()).isEqualTo(409);
        JsonNode j = json(r);
        assertThat(j.path("reason").asText()).isEqualTo("sweep_preconditions_not_met");
        assertThat(retiredRowCount(t, col)).as("nothing was swept").isEqualTo(2);
        List<AuditRow> audit = auditRows(t);
        assertThat(audit).hasSize(2);
        assertThat(audit.get(0).phase()).isEqualTo("started");
        assertThat(audit.get(1).phase()).isEqualTo("refused");
        assertThat(audit.get(1).details().path("counts").path("blocking").toString()).contains("keyless_notes");
        assertThat(j.path("audit").path("outcome_id").asLong()).isEqualTo(audit.get(1).id());
    }

    @Test
    void aFaultInBatchTwoLeavesAnOutcomeRowWithThePartialCounts_andATyped500() throws Exception {
        String t = "rt-fault";
        String col = tenantWithRows(t, 3);
        List<String> byKey = new ArrayList<>();
        for (int i = 1; i <= 3; i++) byKey.add(hexOf(t, col, "r" + i));
        Collections.sort(byKey);   // the sweep takes rows in (collection, chash) order
        byte[] second = Chash.fromHex(byKey.get(1)).toBytes();
        // A CHECK the second row cannot satisfy once its title is stripped: batch 1 commits, batch 2 fails.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).alterTable(CHUNKS).add(DSL.constraint("chunks_test_fault")
                .check(CHUNKS.CHASH.ne(second).or(DSL.jsonbGetAttribute(CHUNKS.METADATA, "title").isNotNull())))
                .execute();
        }
        try {
            HttpResponse<String> r = sweep(ROOT, apply("tenant", t, "batch_size", 1));

            assertThat(r.statusCode()).as(r.body()).isEqualTo(500);
            JsonNode j = json(r);
            assertThat(j.path("reason").asText()).isEqualTo("sweep_failed");
            assertThat(j.path("rows_swept").asLong()).isEqualTo(1);
            assertThat(j.path("batches").asLong()).isEqualTo(1);
            assertThat(j.path("rows_changed").asBoolean()).isTrue();
            assertThat(retiredRowCount(t, col)).as("batch 1 committed, batch 2 rolled back").isEqualTo(2);
            List<AuditRow> audit = auditRows(t);
            assertThat(audit).hasSize(2);
            assertThat(audit.get(1).phase()).isEqualTo("failed");
            assertThat(audit.get(1).details().path("counts").path("rows_swept").asLong()).isEqualTo(1);
            assertThat(audit.get(1).details().path("counts").path("batches").asLong()).isEqualTo(1);
            assertThat(audit.get(1).details().path("failure").asText()).isNotBlank();
            assertThat(audit.get(1).details().toString()).as("no SQL text in a row a tenant can read")
                .doesNotContain("chunks_test_fault");
            assertThat(j.path("audit").path("outcome_id").asLong()).isEqualTo(audit.get(1).id());
        } finally {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSL.using(su, SQLDialect.POSTGRES).alterTable(CHUNKS).dropConstraint("chunks_test_fault").execute();
            }
        }
    }

    @Test
    void theActorIsAHashPrefix_neverTheTokenOrTheWholeHash() throws Exception {
        String t = "rt-actor";
        tenantWithRows(t, 1);
        String hash = TokenHashing.sha256Hex(ROOT);

        HttpResponse<String> r = sweep(ROOT, apply("tenant", t));

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        List<AuditRow> audit = auditRows(t);
        assertThat(audit).hasSize(2);
        for (AuditRow row : audit) {
            assertThat(row.actor()).isEqualTo("root:" + hash.substring(0, 16));
            assertThat(row.details().toString()).doesNotContain(ROOT).doesNotContain(hash);
            assertThat(row.actor()).doesNotContain(ROOT).doesNotContain(hash);
        }
        assertThat(r.body()).doesNotContain(ROOT).doesNotContain(hash);
        assertThat(json(r).path("audit").path("actor").asText()).isEqualTo("root:" + hash.substring(0, 16));
    }

    @Test
    void theServiceDataSourceIsTheRlsBoundAppRole() throws Exception {
        try (Connection c = svcDs.getConnection()) {
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            String user = ctx.select(DSL.currentUser()).fetchOne(0, String.class);
            var roles = DSL.table(DSL.name("pg_catalog", "pg_roles"));
            var rolname = DSL.field(DSL.name("rolname"), String.class);
            assertThat(ctx.select(DSL.field(DSL.name("rolsuper"), Boolean.class)).from(roles).where(rolname.eq(user))
                .fetchOne(0, Boolean.class)).as("not a superuser").isFalse();
            assertThat(ctx.select(DSL.field(DSL.name("rolbypassrls"), Boolean.class)).from(roles)
                .where(rolname.eq(user)).fetchOne(0, Boolean.class)).as("not BYPASSRLS").isFalse();
            var tables = DSL.table(DSL.name("pg_catalog", "pg_tables"));
            String owner = ctx.select(DSL.field(DSL.name("tableowner"), String.class)).from(tables)
                .where(DSL.field(DSL.name("schemaname"), String.class).eq(CHUNKS.getSchema().getName()))
                .and(DSL.field(DSL.name("tablename"), String.class).eq(CHUNKS.getName()))
                .fetchOne(0, String.class);
            assertThat(owner).as("not the owner of nexus.chunks, so RLS is not bypassed by ownership")
                .isNotEqualTo(user);
        }
    }
}
