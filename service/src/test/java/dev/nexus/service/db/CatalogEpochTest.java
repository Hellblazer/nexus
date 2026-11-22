// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_META;
import static org.assertj.core.api.Assertions.assertThat;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import dev.nexus.service.http.StatusHandler;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.net.InetSocketAddress;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneId;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Function;
import javax.sql.DataSource;

/**
 * RDR-229 P3.11 (nexus-p2m1x.31): {@link CatalogEpoch}, the token a client cache keyed by
 * {@code (catalog_epoch, collection_id)} compares to drop on a tenant restore and on a PITR rewind.
 *
 * <p>Not tested here: the log line for a pg_control function that answers NULL
 * ({@code event=catalog_epoch_cluster_null}, and {@code cause=pg_control_member_null} on the boot line). PostgreSQL
 * cannot be made to answer NULL from {@code pg_control_system()}, {@code pg_control_checkpoint()} or
 * {@code pg_postmaster_start_time()}, so the branch has no input to drive it.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogEpochTest {

    private static final String SVC_ROLE = "svc_catalog_epoch";
    private static final String SVC_PASS = "svc_catalog_epoch_pass";
    private static final String A = "epoch-tenant-a";
    private static final String B = "epoch-tenant-b";
    private static final List<String> CONTROL_FNS =
        List.of("pg_catalog.pg_control_system()", "pg_catalog.pg_control_checkpoint()",
            "pg_catalog.pg_postmaster_start_time()");

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
        runChangelog("grant");
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
        return scope.withTenant(tenant, ctx -> CatalogEpoch.ofCluster(ctx, tenant, cluster));
    }

    private static String sha16(String text) throws Exception {
        byte[] d = MessageDigest.getInstance("SHA-256").digest(text.getBytes(StandardCharsets.UTF_8));
        return HexFormat.of().formatHex(d).substring(0, 16);
    }

    // ── what the epoch is made of ────────────────────────────────────────────────────

    @Test
    void theEpochIsTheFirst16HexOfSha256OfSystemTimelinePostmasterStartAndMarker() throws Exception {
        var cluster = new CatalogEpoch.ClusterIdentity("7123456789012345678", "3", "1795000000123456");
        assertThat(epoch("epoch-formula-none", cluster))
            .as("no marker row: the marker part is the empty string")
            .isEqualTo(sha16("7123456789012345678:3:1795000000123456:"));
        writeMarker("epoch-formula-marked", "{\"session_id\":\"s1\"}");
        assertThat(epoch("epoch-formula-marked", cluster))
            .isEqualTo(sha16("7123456789012345678:3:1795000000123456:{\"session_id\":\"s1\"}"))
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
    void aChangedTimelineSystemIdentifierOrPostmasterStartChangesTheEpoch() {
        var base = new CatalogEpoch.ClusterIdentity("7000000000000000001", "1", "1795000000000000");
        String before = epoch(A, base);
        assertThat(epoch(A, new CatalogEpoch.ClusterIdentity("7000000000000000001", "2", "1795000000000000")))
            .as("a PITR recovery ends on a new timeline").isNotEqualTo(before);
        assertThat(epoch(A, new CatalogEpoch.ClusterIdentity("7000000000000000002", "1", "1795000000000000")))
            .as("a restore into a fresh cluster is a new system").isNotEqualTo(before);
        assertThat(epoch(A, new CatalogEpoch.ClusterIdentity("7000000000000000001", "1", "1795000000000001")))
            .as("a snapshot restore on the same timeline needs a restart, which moves the start time").isNotEqualTo(before);
        assertThat(epoch(A, base)).as("the same cluster, the same epoch").isEqualTo(before);
    }

    // ── the real probe reads the right members ───────────────────────────────────────

    /** The three facts read independently, through the superuser connection and a different jOOQ form than the probe's. */
    private CatalogEpoch.ClusterIdentity clusterReadBySuperuser() {
        return su(ctx -> {
            var sys = DSL.function(DSL.name("pg_catalog", "pg_control_system"), SQLDataType.JSONB);
            var ckpt = DSL.function(DSL.name("pg_catalog", "pg_control_checkpoint"), SQLDataType.JSONB);
            var row = ctx.select(
                    DSL.jsonbGetAttributeAsText(DSL.function("to_jsonb", SQLDataType.JSONB, sys), "system_identifier"),
                    DSL.jsonbGetAttributeAsText(DSL.function("to_jsonb", SQLDataType.JSONB, ckpt), "timeline_id"),
                    DSL.function(DSL.name("pg_catalog", "pg_postmaster_start_time"), SQLDataType.TIMESTAMPWITHTIMEZONE))
                .fetchOne();
            OffsetDateTime t = row.value3();
            long micros = t.toEpochSecond() * 1_000_000L + t.getNano() / 1_000L;
            return new CatalogEpoch.ClusterIdentity(row.value1(), row.value2(), Long.toString(micros));
        });
    }

    @Test
    void theRealProbeReadsSystemIdentifierTimelineAndPostmasterStartFromTheRightMembers() {
        CatalogEpoch.ClusterIdentity expected = clusterReadBySuperuser();
        assertThat(expected.systemIdentifier()).as("non-vacuity: a real system identifier").matches("[0-9]{10,}");
        assertThat(expected.timelineId()).matches("[1-9][0-9]*");
        writeMarker(A, "{\"session_id\":\"probe-pin\"}");
        Optional<String> real = epoch(A);
        assertThat(real).isPresent();
        assertThat(real.get()).isEqualTo(epoch(A, expected));
        assertThat(real.get()).as("a constant or a wrong member would differ")
            .isNotEqualTo(epoch(A, new CatalogEpoch.ClusterIdentity(expected.systemIdentifier(), "2", expected.postmasterStart())))
            .isNotEqualTo(epoch(A, new CatalogEpoch.ClusterIdentity(expected.timelineId(), expected.systemIdentifier(), expected.postmasterStart())));
    }

    // ── tenant scope ─────────────────────────────────────────────────────────────────

    @Test
    void aContextNotScopedToTheTenantGetsNoEpoch_neverARestoreBlindOne() {
        writeMarker(A, "{\"session_id\":\"scoped\"}");
        Optional<String> scoped = epoch(A);
        assertThat(scoped).isPresent();
        Optional<String> unscoped = su(ctx -> CatalogEpoch.of(ctx, A));
        assertThat(unscoped).as("a connection with no tenant GUC").isEmpty();
        Optional<String> wrongTenant = scope.withTenant(B, ctx -> CatalogEpoch.of(ctx, A));
        assertThat(wrongTenant).as("a connection scoped to another tenant").isEmpty();
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

    private void runChangelog(String name) throws Exception {
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-pgcontrol-" + name + ".xml",
                "databasechangelog_test_pgcontrol_" + name.replace('-', '_'), Map.of());
        }
    }

    /** The probe is empty, and the caller's open transaction on the engine's role is still usable: SELECT, INSERT, commit. */
    private void assertEmptyAndTransactionSurvives(String key) throws Exception {
        assertThat(epoch(A)).as("no epoch reads as unstable").isEmpty();
        boolean available = scope.withTenant(A, CatalogEpoch::available);
        assertThat(available).as("available() agrees").isFalse();
        try (Connection c = svcDs.getConnection()) {
            c.setAutoCommit(false);
            try {
                DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
                ctx.select(DSL.field(DSL.function("set_config", String.class, DSL.val(TenantConstants.GUC_NAME),
                    DSL.val(A), DSL.inline(true)))).fetch();
                assertThat(CatalogEpoch.of(ctx, A)).isEmpty();
                assertThat(CatalogEpoch.available(ctx)).isFalse();
                assertThat(ctx.select(DSL.inline(1)).fetchOne(0, Integer.class))
                    .as("the caller's transaction is still usable after of()").isEqualTo(1);
                ctx.insertInto(CATALOG_META)
                    .set(CATALOG_META.TENANT_ID, A)
                    .set(CATALOG_META.KEY, key)
                    .set(CATALOG_META.VALUE, "{}")
                    .onConflict(CATALOG_META.TENANT_ID, CATALOG_META.KEY).doNothing()
                    .execute();
                c.commit();
            } finally {
                c.rollback();
            }
        }
        int rows = scope.withTenant(A, ctx -> ctx.fetchCount(CATALOG_META,
            CATALOG_META.TENANT_ID.eq(A).and(CATALOG_META.KEY.eq(key))));
        assertThat(rows).as("the write after of() in the same transaction committed").isEqualTo(1);
    }

    @Test
    void withoutExecuteOnAnyOfTheThreeFunctionsTheEpochIsEmptyAndTheTransactionSurvives() throws Exception {
        assertThat(epoch(A)).as("non-vacuity: the epoch is there before the revoke").isPresent();
        runChangelog("revoke");
        for (String fn : CONTROL_FNS) {
            assertThat(serviceRoleCanExecute(fn)).as("the revoke took: " + fn).isFalse();
        }
        assertEmptyAndTransactionSurvives("epoch-tx-probe-all");
    }

    @Test
    void withoutExecuteOnPgControlSystemAloneTheEpochIsEmptyAndTheTransactionSurvives() throws Exception {
        halfRevoke("revoke-system", CONTROL_FNS.get(0), "epoch-tx-probe-system");
    }

    @Test
    void withoutExecuteOnPgControlCheckpointAloneTheEpochIsEmptyAndTheTransactionSurvives() throws Exception {
        halfRevoke("revoke-checkpoint", CONTROL_FNS.get(1), "epoch-tx-probe-checkpoint");
    }

    @Test
    void withoutExecuteOnPgPostmasterStartTimeAloneTheEpochIsEmptyAndTheTransactionSurvives() throws Exception {
        halfRevoke("revoke-postmaster", CONTROL_FNS.get(2), "epoch-tx-probe-postmaster");
    }

    private void halfRevoke(String changelog, String revokedFunction, String key) throws Exception {
        assertThat(epoch(A)).as("non-vacuity: the epoch is there before the revoke").isPresent();
        runChangelog(changelog);
        for (String fn : CONTROL_FNS) {
            assertThat(serviceRoleCanExecute(fn)).as("only " + revokedFunction + " is revoked: " + fn)
                .isEqualTo(!fn.equals(revokedFunction));
        }
        assertEmptyAndTransactionSurvives(key);
    }

    // ── /v1/status: catalog_epoch_available ──────────────────────────────────────────

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private JsonNode status() throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        // a supplier built now, as at boot: it asks once and caches
        server.createContext("/v1/status", new StatusHandler(null).catalogEpoch(CatalogEpochStatus.statusSupplier(svcDs)));
        server.start();
        try {
            HttpResponse<String> resp = dev.nexus.service.TestHttp.client().send(
                dev.nexus.service.TestHttp.request("http://127.0.0.1:" + server.getAddress().getPort() + "/v1/status").GET().build(),
                HttpResponse.BodyHandlers.ofString());
            assertThat(resp.statusCode()).isEqualTo(200);
            return MAPPER.readTree(resp.body());
        } finally {
            server.stop(0);
        }
    }

    @Test
    void statusReportsCatalogEpochAvailableByDefault() throws Exception {
        JsonNode body = status();
        assertThat(body.get("catalog_epoch_available").isBoolean()).isTrue();
        assertThat(body.get("catalog_epoch_available").asBoolean()).isTrue();
    }

    @Test
    void statusReportsCatalogEpochUnavailableWhenAnyFunctionIsRevoked() throws Exception {
        runChangelog("revoke");
        assertThat(status().get("catalog_epoch_available").asBoolean()).as("all revoked").isFalse();
        restoreControlPrivileges();
        runChangelog("revoke-postmaster");
        assertThat(status().get("catalog_epoch_available").asBoolean()).as("one revoked").isFalse();
    }

    // ── CatalogEpochStatus: the 5 s cache and the log lines ──────────────────────────

    /** A clock the test moves. */
    private static final class MovableClock extends Clock {
        long millis = 1_000_000L;
        @Override public ZoneId getZone() { return ZoneId.of("UTC"); }
        @Override public Clock withZone(ZoneId zone) { return this; }
        @Override public Instant instant() { return Instant.ofEpochMilli(millis); }
        @Override public long millis() { return millis; }
    }

    /** A DataSource that counts the connections asked of it; {@code failing} makes each ask throw. */
    private static DataSource counting(DataSource real, AtomicInteger asks, boolean failing) {
        return (DataSource) java.lang.reflect.Proxy.newProxyInstance(DataSource.class.getClassLoader(),
            new Class<?>[] {DataSource.class}, (proxy, method, args) -> {
                if (method.getName().equals("getConnection")) {
                    asks.incrementAndGet();
                    if (failing) throw new SQLException("database is down");
                }
                try {
                    return method.invoke(real, args);
                } catch (java.lang.reflect.InvocationTargetException e) {
                    throw e.getCause();
                }
            });
    }

    @Test
    void theStatusAnswerIsCachedForFiveSecondsThenAskedAgain() {
        var asks = new AtomicInteger();
        var clock = new MovableClock();
        var supplier = CatalogEpochStatus.statusSupplier(counting(svcDs, asks, false), clock);
        assertThat(asks.get()).as("the boot ask").isEqualTo(1);
        assertThat(supplier.get()).isTrue();
        clock.millis += CatalogEpochStatus.TTL_MILLIS - 1;
        assertThat(supplier.get()).isTrue();
        assertThat(asks.get()).as("two gets inside the window made no new probe").isEqualTo(1);
        clock.millis += 1;
        assertThat(supplier.get()).isTrue();
        assertThat(asks.get()).as("after the window, a second probe").isEqualTo(2);
    }

    @Test
    void aProbeThatCannotRunIsCachedToo_andLoggedOncePerTransition() {
        var asks = new AtomicInteger();
        var clock = new MovableClock();
        try (var logs = captureLogs()) {
            var supplier = CatalogEpochStatus.statusSupplier(counting(svcDs, asks, true), clock);
            assertThat(supplier.get()).as("cannot tell: no answer").isNull();
            assertThat(supplier.get()).isNull();
            assertThat(asks.get()).as("the failure is cached inside the window: one probe").isEqualTo(1);
            clock.millis += CatalogEpochStatus.TTL_MILLIS;
            assertThat(supplier.get()).isNull();
            assertThat(asks.get()).as("after the window, one more").isEqualTo(2);
            assertThat(logs.lines()).as("one line for the transition, none for the repeat")
                .hasSize(1).allSatisfy(l -> assertThat(l).contains("event=catalog_epoch_probe_unavailable"));
        }
    }

    @Test
    void theBootWarnFiresOnceWhenThePrivilegeIsMissing_naming_theCause() throws Exception {
        runChangelog("revoke-postmaster");
        var clock = new MovableClock();
        try (var logs = captureLogs()) {
            var supplier = CatalogEpochStatus.statusSupplier(svcDs, clock);
            assertThat(supplier.get()).isFalse();
            clock.millis += CatalogEpochStatus.TTL_MILLIS;
            assertThat(supplier.get()).as("asked again, still no").isFalse();
            assertThat(logs.lines()).as("the warn is per transition, not per probe").hasSize(1)
                .allSatisfy(l -> assertThat(l).contains("event=catalog_epoch_unavailable")
                    .contains("cause=pg_control_not_readable").contains("pg_control_system()")
                    .contains("pg_control_checkpoint()").contains("client_keys_by=label"));
        }
    }

    @Test
    void noWarnWhenTheEpochIsAvailable() {
        try (var logs = captureLogs()) {
            var supplier = CatalogEpochStatus.statusSupplier(svcDs, new MovableClock());
            assertThat(supplier.get()).isTrue();
            assertThat(logs.lines()).isEmpty();
        }
    }

    /** The WARN lines of CatalogEpochStatus while open. */
    private static final class Logs implements AutoCloseable {
        private final ch.qos.logback.classic.Logger logger = (ch.qos.logback.classic.Logger)
            org.slf4j.LoggerFactory.getLogger(CatalogEpochStatus.class);
        private final ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent> appender =
            new ch.qos.logback.core.read.ListAppender<>();
        Logs() {
            appender.start();
            logger.addAppender(appender);
        }
        List<String> lines() {
            return appender.list.stream().map(e -> e.getFormattedMessage()).toList();
        }
        @Override public void close() {
            logger.detachAppender(appender);
            appender.stop();
        }
    }

    private static Logs captureLogs() {
        return new Logs();
    }

    // ── test 5: the real probe ───────────────────────────────────────────────────────

    @Test
    void withThePrivilegesTheRealProbeReturnsSixteenHex() {
        for (String fn : CONTROL_FNS) {
            assertThat(serviceRoleCanExecute(fn)).as("default state: the service role can execute " + fn).isTrue();
        }
        assertThat(epoch(A)).hasValueSatisfying(e -> assertThat(e).matches("[0-9a-f]{16}"));
        boolean available = scope.withTenant(A, CatalogEpoch::available);
        assertThat(available).isTrue();
    }

    private boolean serviceRoleCanExecute(String signature) {
        Boolean can = su(ctx -> ctx.select(DSL.field(DSL.function("has_function_privilege", Boolean.class,
            DSL.inline(SVC_ROLE), DSL.inline(signature), DSL.inline("EXECUTE")))).fetchOne(0, Boolean.class));
        return Boolean.TRUE.equals(can);
    }
}
