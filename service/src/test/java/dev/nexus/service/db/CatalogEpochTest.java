// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_META;
import static org.assertj.core.api.Assertions.assertThat;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.SQLException;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.function.Function;

/**
 * RDR-229 P3.11 (nexus-p2m1x.31): {@link CatalogEpoch}, the token a client cache keyed by
 * {@code (catalog_epoch, collection_id)} compares to drop on a tenant restore and on a PITR rewind.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogEpochTest {

    private static final String SVC_ROLE = "svc_catalog_epoch";
    private static final String SVC_PASS = "svc_catalog_epoch_pass";
    private static final String A = "epoch-tenant-a";
    private static final String B = "epoch-tenant-b";
    private static final List<String> CONTROL_FNS =
        List.of("pg_catalog.pg_control_system()", "pg_catalog.pg_control_checkpoint()");

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        scope = new TenantScope(svcDs);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /** Whatever a test revoked is given back to PUBLIC, the state a stock cluster starts in. */
    @AfterEach
    void restoreControlPrivileges() throws Exception {
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-pgcontrol-grant.xml",
                "databasechangelog_test_pgcontrol_grant", Map.of());
        }
    }

    private <R> R su(Function<DSLContext, R> work) {
        try (Connection c = pg.createConnection("")) {
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    /** The tenant's restore marker, written the way TenantImportLoad.writeMarker writes it (upsert on the key). */
    private void writeMarker(String tenant, String json) {
        scope.withTenant(tenant, ctx -> ctx.insertInto(CATALOG_META)
            .set(CATALOG_META.TENANT_ID, tenant)
            .set(CATALOG_META.KEY, TenantImportLoad.MARKER_KEY)
            .set(CATALOG_META.VALUE, json)
            .onConflict(CATALOG_META.TENANT_ID, CATALOG_META.KEY)
            .doUpdate().set(CATALOG_META.VALUE, json)
            .execute());
    }

    private Optional<String> epoch(String tenant) {
        return scope.withTenant(tenant, ctx -> CatalogEpoch.of(ctx, tenant));
    }

    private String epoch(String tenant, CatalogEpoch.ClusterIdentity cluster) {
        return scope.withTenant(tenant, ctx -> CatalogEpoch.of(ctx, tenant, cluster));
    }

    private static String sha16(String text) throws Exception {
        byte[] d = MessageDigest.getInstance("SHA-256").digest(text.getBytes(StandardCharsets.UTF_8));
        return HexFormat.of().formatHex(d).substring(0, 16);
    }

    // ── what the epoch is made of ────────────────────────────────────────────────────

    @Test
    void theEpochIsTheFirst16HexOfSha256OfSystemTimelineAndMarker() throws Exception {
        var cluster = new CatalogEpoch.ClusterIdentity("7123456789012345678", "3");
        assertThat(epoch("epoch-formula-none", cluster))
            .as("no marker row: the marker part is the empty string")
            .isEqualTo(sha16("7123456789012345678:3:"));
        writeMarker("epoch-formula-marked", "{\"session_id\":\"s1\"}");
        assertThat(epoch("epoch-formula-marked", cluster))
            .isEqualTo(sha16("7123456789012345678:3:{\"session_id\":\"s1\"}"))
            .matches("[0-9a-f]{16}");
    }

    // ── test 1: a tenant restore ─────────────────────────────────────────────────────

    @Test
    void aTenantRestoreChangesThatTenantsEpochAndNoOthers() {
        writeMarker(B, "{\"session_id\":\"b-load-1\"}");
        Optional<String> aBefore = epoch(A);
        Optional<String> bBefore = epoch(B);
        assertThat(aBefore).as("the real probe reads the cluster").isPresent();
        assertThat(bBefore).isPresent().isNotEqualTo(aBefore);

        writeMarker(A, "{\"session_id\":\"a-restore-1\",\"committed_at\":\"2026-11-22T00:00:00Z\"}");
        Optional<String> aAfterFirst = epoch(A);
        assertThat(aAfterFirst).as("the first restore of A").isPresent().isNotEqualTo(aBefore);
        assertThat(epoch(B)).as("B's epoch is untouched by A's restore").isEqualTo(bBefore);

        writeMarker(A, "{\"session_id\":\"a-restore-2\",\"committed_at\":\"2026-11-22T01:00:00Z\"}");
        assertThat(epoch(A)).as("a second restore of A replaces the row").isPresent().isNotEqualTo(aAfterFirst);
        assertThat(epoch(B)).isEqualTo(bBefore);
    }

    // ── test 2: a PITR rewind or a promotion ─────────────────────────────────────────

    @Test
    void aChangedTimelineOrSystemIdentifierChangesTheEpoch() {
        var base = new CatalogEpoch.ClusterIdentity("7000000000000000001", "1");
        String before = epoch(A, base);
        assertThat(epoch(A, new CatalogEpoch.ClusterIdentity("7000000000000000001", "2")))
            .as("a PITR recovery ends on a new timeline").isNotEqualTo(before);
        assertThat(epoch(A, new CatalogEpoch.ClusterIdentity("7000000000000000002", "1")))
            .as("a restore into a fresh cluster is a new system").isNotEqualTo(before);
        assertThat(epoch(A, base)).as("the same cluster, the same epoch").isEqualTo(before);
    }

    // ── test 3: nothing changed ──────────────────────────────────────────────────────

    @Test
    void twoCallsWithNothingChangedReturnTheSameValue() {
        writeMarker(A, "{\"session_id\":\"stable\"}");
        Optional<String> first = epoch(A);
        assertThat(first).isPresent();
        assertThat(epoch(A)).as("a reboot reads the same cluster and the same row").isEqualTo(first);
        assertThat(epoch(A)).isEqualTo(first);
    }

    // ── test 4: a missing privilege ──────────────────────────────────────────────────

    @Test
    void withoutExecuteOnThePgControlFunctionsTheEpochIsEmptyAndTheTransactionSurvives() throws Exception {
        assertThat(epoch(A)).as("non-vacuity: the epoch is there before the revoke").isPresent();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-pgcontrol-revoke.xml",
                "databasechangelog_test_pgcontrol_revoke", Map.of());
        }
        assertThat(serviceRoleCanExecute(CONTROL_FNS.get(0)))
            .as("the revoke took: the service role cannot execute pg_control_system()").isFalse();
        assertThat(serviceRoleCanExecute(CONTROL_FNS.get(1)))
            .as("the revoke took: the service role cannot execute pg_control_checkpoint()").isFalse();

        assertThat(epoch(A)).as("no epoch reads as unstable").isEmpty();

        // An explicit transaction on the engine's role: a 42501 from the call would abort it.
        try (Connection c = svcDs.getConnection()) {
            c.setAutoCommit(false);
            try {
                DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
                assertThat(CatalogEpoch.of(ctx, A)).isEmpty();
                assertThat(ctx.select(DSL.inline(1)).fetchOne(0, Integer.class))
                    .as("the caller's transaction is still usable after of()").isEqualTo(1);
                writeInTx(ctx);
                c.commit();
            } finally {
                c.rollback();
            }
        }
        int probeRows = scope.withTenant(A, ctx -> ctx.fetchCount(CATALOG_META,
            CATALOG_META.TENANT_ID.eq(A).and(CATALOG_META.KEY.eq("epoch-tx-probe"))));
        assertThat(probeRows).as("a write after of() in the same transaction committed").isEqualTo(1);
    }

    private boolean serviceRoleCanExecute(String signature) {
        Boolean can = su(ctx -> ctx.select(DSL.field(DSL.function("has_function_privilege", Boolean.class,
            DSL.inline(SVC_ROLE), DSL.inline(signature), DSL.inline("EXECUTE")))).fetchOne(0, Boolean.class));
        return Boolean.TRUE.equals(can);
    }

    private static void writeInTx(DSLContext ctx) {
        ctx.select(DSL.field(DSL.function("set_config", String.class, DSL.inline(TenantConstants.GUC_NAME), DSL.inline(A),
            DSL.inline(true)))).fetch();
        ctx.insertInto(CATALOG_META)
            .set(CATALOG_META.TENANT_ID, A)
            .set(CATALOG_META.KEY, "epoch-tx-probe")
            .set(CATALOG_META.VALUE, "{}")
            .onConflict(CATALOG_META.TENANT_ID, CATALOG_META.KEY).doNothing()
            .execute();
    }

    // ── test 5: the real probe ───────────────────────────────────────────────────────

    @Test
    void withThePrivilegesTheRealProbeReturnsSixteenHex() {
        assertThat(serviceRoleCanExecute(CONTROL_FNS.get(0)))
            .as("default state: the service role can execute pg_control_system()").isTrue();
        assertThat(serviceRoleCanExecute(CONTROL_FNS.get(1)))
            .as("default state: the service role can execute pg_control_checkpoint()").isTrue();
        assertThat(epoch(A)).hasValueSatisfying(e -> assertThat(e).matches("[0-9a-f]{16}"));
        Optional<String> asSuperuser = su(ctx -> CatalogEpoch.of(ctx, A));
        assertThat(asSuperuser).as("a superuser reads it too").isPresent();
    }
}
