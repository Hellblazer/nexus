// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.ImportTestEnv.IMPORT;
import static dev.nexus.service.ImportTestEnv.json;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.db.ExportTestClock;
import dev.nexus.service.db.RestoreLoadLock;
import dev.nexus.service.db.TenantExport;
import dev.nexus.service.db.TenantExportSessions;
import dev.nexus.service.db.TenantImportSessions;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * What a tenant import does to everything else, and what ends it early (RDR-248 Step 5, bead nexus-csayy.6): the
 * restore-load lock against tenant create and against a second load, the abort before and during a load, the bounds
 * an upload and a load keep, and the guarantee that a load that ends any way but a commit leaves the tenant as it was
 * and never reports otherwise.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TenantImportConcurrencyTest {

    private static final String SRC = "t-imp-cc-source";

    private ImportTestEnv env;
    private ImportTestEnv.BackupFile file;

    @BeforeAll
    void startAll() throws Exception {
        env = ImportTestEnv.start();
        String srcToken = env.newTenant(SRC);
        List<String> failures = new ArrayList<>();
        env.su(ctx -> {
            TenantExportBatteryTest.Seed.rows(ctx, SRC, 1, failures);
            TenantExportBatteryTest.Seed.fillNulls(ctx, SRC, failures);
            return null;
        });
        assertThat(failures).as("the seed").isEmpty();
        file = env.export(srcToken);
    }

    @AfterAll
    void stopAll() {
        if (env != null) env.close();
    }

    @BeforeEach
    void freshSessions() {
        env.freshSessions();
    }

    // ── helpers ─────────────────────────────────────────────────────────────────────────────────

    private JsonNode openAndUpload(String token) throws Exception {
        HttpResponse<String> r = env.open(token, file);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode opened = json(r);
        env.upload(token, opened.get("session_id").asText(), file, opened, 100);
        return opened;
    }

    private void assertTenantEmpty(String tenant) {
        for (var t : TenantExport.TABLES) {
            if (t.exported() && !t.name().equals("catalog_meta")) {
                assertThat(env.count(t.table(), tenant)).as(t.name() + " of " + tenant).isZero();
            }
        }
        assertThat(env.count("catalog_meta", tenant)).as("only the ghost-sweep row the first request wrote").isEqualTo(1);
    }

    /** A connection that holds {@code table} ACCESS EXCLUSIVE until it is rolled back: a load that reaches the table waits. */
    private Connection holdTable(org.jooq.Table<?> table) throws Exception {
        Connection holder = env.pg.createConnection("");
        holder.setAutoCommit(false);
        PgContainerHelper.lockTable(holder, table, PgContainerHelper.LockMode.ACCESS_EXCLUSIVE);
        return holder;
    }

    /** Another session's load, as far as the restore lock can tell: the key held exclusively until the connection ends its transaction. */
    private Connection holdRestoreLock() throws Exception {
        Connection holder = env.pg.createConnection("");
        holder.setAutoCommit(false);
        assertThat(RestoreLoadLock.tryExclusive(DSL.using(holder, SQLDialect.POSTGRES))).isTrue();
        return holder;
    }

    // ── tenant create, and a second load ────────────────────────────────────────────────────────

    @Test
    void aTenantCreateDuringALoadIsRefusedWithA30sRetryAfterAndSucceedsAfterTheCommitWhileAnExistingTenantIsNotDelayed() throws Exception {
        String loading = "t-imp-cc-loading";
        String loadingToken = env.newTenant(loading);
        String waiting = "t-imp-cc-waiting";
        String waitingToken = env.newTenant(waiting);
        env.newTenant("t-imp-cc-existing");   // its leaves exist: a tenant create for it never runs DDL
        var sessions = env.importSessions(TenantImportSessions.Config.defaults(), List.of());
        CountDownLatch held = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        sessions.loadHookForTests(text -> {
            if (text.equals("loading memory")) {   // the chunks are in; the load is between its inserts and its commit
                held.countDown();
                try {
                    release.await(120, TimeUnit.SECONDS);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            }
        });
        JsonNode opened = openAndUpload(loadingToken);
        String id = opened.get("session_id").asText();
        assertThat(env.call("POST", IMPORT + "/" + id + "/commit", loadingToken).statusCode()).isEqualTo(202);
        assertThat(held.await(120, TimeUnit.SECONDS)).as("the load reached its hold").isTrue();
        assertThat(env.count("chunks", loading)).as("the load has written chunks and not committed").isZero();   // invisible to others

        // a NEW tenant: its create needs DDL on the partitions the load writes into
        long t0 = System.nanoTime();
        HttpResponse<String> created = env.callJson("POST", "/v1/tenants/create", ImportTestEnv.ROOT, "{\"name\":\"t-imp-cc-newcomer\"}");
        long elapsedMs = (System.nanoTime() - t0) / 1_000_000;
        assertThat(created.statusCode()).as(created.body()).isEqualTo(503);
        assertThat(json(created).get("reason").asText()).isEqualTo("tenant_creation_busy");
        assertThat(created.headers().firstValue("Retry-After")).contains("30");
        assertThat(json(created).get("retry_after_seconds").asInt()).isEqualTo(30);
        assertThat(elapsedMs).as("within about a second, not a lock wait").isLessThan(3_000);

        // an EXISTING tenant: its leaves are all there, the fast path takes no lock, so the load does not delay it
        t0 = System.nanoTime();
        HttpResponse<String> existing = env.callJson("POST", "/v1/tenants/create", ImportTestEnv.ROOT, "{\"name\":\"t-imp-cc-existing\"}");
        elapsedMs = (System.nanoTime() - t0) / 1_000_000;
        assertThat(existing.statusCode()).as(existing.body()).isEqualTo(200);
        assertThat(elapsedMs).as("not delayed by the load").isLessThan(1_500);

        // a second load is not admitted while one runs; its upload stays staged
        JsonNode openedWaiting = openAndUpload(waitingToken);
        String waitingId = openedWaiting.get("session_id").asText();
        HttpResponse<String> busy = env.call("POST", IMPORT + "/" + waitingId + "/commit", waitingToken);
        assertThat(busy.statusCode()).as(busy.body()).isEqualTo(503);
        assertThat(json(busy).get("reason").asText()).isEqualTo("restore_busy");
        assertThat(busy.headers().firstValue("Retry-After")).contains("60");
        assertThat(json(env.call("GET", IMPORT + "/" + waitingId, waitingToken)).get("status").asText()).isEqualTo("staging");

        release.countDown();
        assertThat(env.awaitEnd(loadingToken, id, Duration.ofMinutes(2)).get("status").asText()).isEqualTo("committed");
        HttpResponse<String> later = env.callJson("POST", "/v1/tenants/create", ImportTestEnv.ROOT, "{\"name\":\"t-imp-cc-newcomer\"}");
        assertThat(later.statusCode()).as("after the commit the create goes through").isEqualTo(200);
        HttpResponse<String> retried = env.call("POST", IMPORT + "/" + waitingId + "/commit", waitingToken);
        assertThat(retried.statusCode()).as(retried.body()).isEqualTo(202);
        assertThat(env.awaitEnd(waitingToken, waitingId, Duration.ofMinutes(2)).get("status").asText()).isEqualTo("committed");
        assertThat(env.count("memory", waiting)).isEqualTo(env.count("memory", SRC));
        assertThat(env.count("chunks", loading)).isEqualTo(env.count("chunks", SRC));
    }

    @Test
    void aLoadWaitsOutATenantCreateHoldingTheKeySharedAndThenAnswersRestoreBusy() throws Exception {
        // a tenant create holds the key shared for the length of its transaction; the load retries its try-lock for a
        // bounded wait and then gives up with restore_busy, leaving the upload staged
        String tenant = "t-imp-cc-shared";
        String token = env.newTenant(tenant);
        env.importSessions(TenantImportSessions.Config.defaults().withRestoreLockWait(Duration.ofMillis(700)), List.of());
        JsonNode opened = openAndUpload(token);
        String id = opened.get("session_id").asText();
        try (Connection create = env.pg.createConnection("")) {
            create.setAutoCommit(false);
            var ctx = DSL.using(create, SQLDialect.POSTGRES);
            // pg_try_advisory_xact_lock_shared on the same key, as create_tenant_partitions' slow path does
            assertThat(ctx.select(DSL.function("pg_try_advisory_xact_lock_shared", org.jooq.impl.SQLDataType.BOOLEAN,
                DSL.function("hashtextextended", org.jooq.impl.SQLDataType.BIGINT, DSL.val(RestoreLoadLock.KEY_NAME), DSL.inline(0L))))
                .fetchOne(0, Boolean.class)).isTrue();
            long t0 = System.nanoTime();
            HttpResponse<String> r = env.call("POST", IMPORT + "/" + id + "/commit", token);
            long ms = (System.nanoTime() - t0) / 1_000_000;
            assertThat(r.statusCode()).as(r.body()).isEqualTo(503);
            assertThat(json(r).get("reason").asText()).isEqualTo("restore_busy");
            assertThat(ms).as("it waited for the shared holder").isGreaterThanOrEqualTo(600);
            create.rollback();
        }
        HttpResponse<String> ok = env.call("POST", IMPORT + "/" + id + "/commit", token);
        assertThat(ok.statusCode()).isEqualTo(202);
        assertThat(env.awaitEnd(token, id, Duration.ofMinutes(2)).get("status").asText()).isEqualTo("committed");
    }

    // ── abort ───────────────────────────────────────────────────────────────────────────────────

    @Test
    void anAbortBeforeTheCommitRollsTheStagingBackAndFreesTheTenantsPlace() throws Exception {
        String tenant = "t-imp-cc-abort-early";
        String token = env.newTenant(tenant);
        JsonNode opened = openAndUpload(token);
        String id = opened.get("session_id").asText();

        assertThat(env.call("DELETE", IMPORT + "/" + id, token).statusCode()).isEqualTo(200);

        JsonNode status = json(env.call("GET", IMPORT + "/" + id, token));
        assertThat(status.get("status").asText()).isEqualTo("failed: aborted");
        assertTenantEmpty(tenant);
        assertThat(env.ds.getHikariPoolMXBean().getActiveConnections()).as("the held connection is gone").isZero();
        assertThat(env.open(token, file).statusCode()).as("the tenant may open again").isEqualTo(200);
    }

    @Test
    void anAbortDuringTheLoadCancelsTheBackendAndTheTenantIsAsItWas() throws Exception {
        String tenant = "t-imp-cc-abort-load";
        String token = env.newTenant(tenant);
        JsonNode opened = openAndUpload(token);
        String id = opened.get("session_id").asText();
        try (Connection holder = holdTable(dev.nexus.service.jooq.nexus.Tables.MEMORY)) {
            assertThat(env.call("POST", IMPORT + "/" + id + "/commit", token).statusCode()).isEqualTo(202);
            env.awaitBlockedBackends(holder, 1);   // the load is parked on memory, after its earlier tables

            assertThat(env.call("DELETE", IMPORT + "/" + id, token).statusCode()).isEqualTo(200);

            JsonNode end = env.awaitEnd(token, id, Duration.ofMinutes(1));
            assertThat(end.get("status").asText()).isEqualTo("failed: aborted");
            holder.rollback();
        }
        assertTenantEmpty(tenant);
        assertThat(json(env.call("GET", "/v1/tenant/import-marker", token)).get("present").asBoolean()).isFalse();
        assertThat(env.ds.getHikariPoolMXBean().getActiveConnections()).isZero();
    }

    @Test
    void aLoadPastTheLoadBoundIsCancelledAndTheTenantIsAsItWas() throws Exception {
        String tenant = "t-imp-cc-bound";
        String token = env.newTenant(tenant);
        ExportTestClock clock = new ExportTestClock(Instant.parse("2026-11-13T12:00:00Z"));
        env.exportSessions(TenantExportSessions.Config.defaults().withMaxSessions(6), clock);
        var sessions = env.importSessions(TenantImportSessions.Config.defaults().withLoadMax(Duration.ofMinutes(5)), List.of());
        JsonNode opened = openAndUpload(token);
        String id = opened.get("session_id").asText();
        try (Connection holder = holdTable(dev.nexus.service.jooq.nexus.Tables.MEMORY)) {
            assertThat(env.call("POST", IMPORT + "/" + id + "/commit", token).statusCode()).isEqualTo(202);
            env.awaitBlockedBackends(holder, 1);

            clock.advance(Duration.ofMinutes(6));
            sessions.sweep();

            JsonNode end = env.awaitEnd(token, id, Duration.ofMinutes(1));
            assertThat(end.get("status").asText()).startsWith("failed: the load passed " + TenantImportSessions.ENV_LOAD_MAX);
            holder.rollback();
        }
        assertTenantEmpty(tenant);
    }

    @Test
    void aStatementThatRunsPastTheLoadBoundIsCancelledByItsStatementTimeout() throws Exception {
        String tenant = "t-imp-cc-stmt";
        String token = env.newTenant(tenant);
        env.importSessions(TenantImportSessions.Config.defaults().withLoadMax(Duration.ofSeconds(2))
            .withSweepInterval(Duration.ofMinutes(10)), List.of());
        JsonNode opened = openAndUpload(token);
        String id = opened.get("session_id").asText();
        try (Connection holder = holdTable(dev.nexus.service.jooq.nexus.Tables.MEMORY)) {
            assertThat(env.call("POST", IMPORT + "/" + id + "/commit", token).statusCode()).isEqualTo(202);
            JsonNode end = env.awaitEnd(token, id, Duration.ofMinutes(1));
            assertThat(end.get("status").asText()).startsWith("failed: ").contains("statement timeout");
            holder.rollback();
        }
        assertTenantEmpty(tenant);
    }

    // ── bounds ──────────────────────────────────────────────────────────────────────────────────

    @Test
    void anIdleSessionIsClosedAndItsPlaceIsFree() throws Exception {
        String tenant = "t-imp-cc-idle";
        String token = env.newTenant(tenant);
        ExportTestClock clock = new ExportTestClock(Instant.parse("2026-11-13T12:00:00Z"));
        env.exportSessions(TenantExportSessions.Config.defaults().withMaxSessions(6), clock);
        var sessions = env.importSessions(TenantImportSessions.Config.defaults(), List.of());
        JsonNode opened = openAndUpload(token);
        String id = opened.get("session_id").asText();
        clock.advance(Duration.ofMinutes(9));
        assertThat(json(env.call("GET", IMPORT + "/" + id, token)).get("state").asText()).as("a poll is not activity, and 9 minutes is in bounds").isEqualTo("staging");
        clock.advance(Duration.ofMinutes(2));   // 11 minutes since the last part (a poll does not restart the clock)

        assertThat(sessions.sweep()).isEqualTo(1);

        assertThat(json(env.call("GET", IMPORT + "/" + id, token)).get("status").asText()).startsWith("failed: idle bound");
        assertThat(env.call("POST", IMPORT + "/" + id + "/commit", token).statusCode()).as("the outcome").isEqualTo(200);
        byte[] part = BinaryCopyTestSupport.split(file.pages.get("memory").get(0), 100).get(0);
        assertThat(env.putPart(token, id, "memory", 0, part, ImportTestEnv.sha256(part)).statusCode()).as("no such session").isEqualTo(404);
        assertTenantEmpty(tenant);
        assertThat(env.open(token, file).statusCode()).as("its place is free").isEqualTo(200);
    }

    @Test
    void aSessionPastTheTotalBoundIsClosedEvenWhileItIsBusy() throws Exception {
        String tenant = "t-imp-cc-total";
        String token = env.newTenant(tenant);
        ExportTestClock clock = new ExportTestClock(Instant.parse("2026-11-13T12:00:00Z"));
        env.exportSessions(TenantExportSessions.Config.defaults().withMaxSessions(6), clock);
        var sessions = env.importSessions(TenantImportSessions.Config.defaults(), List.of());
        HttpResponse<String> open = env.open(token, file);
        JsonNode opened = json(open);
        String id = opened.get("session_id").asText();
        for (int i = 0; i < 7; i++) {   // a table every nine minutes keeps it from idling out, and 63 minutes pass
            clock.advance(Duration.ofMinutes(9));
            if (i < 6) env.uploadTable(token, id, file, opened.get("tables").get(i).asText(), 100);
        }

        assertThat(sessions.sweep()).isEqualTo(1);

        assertThat(json(env.call("GET", IMPORT + "/" + id, token)).get("status").asText()).startsWith("failed: total bound");
        assertTenantEmpty(tenant);
    }

    @Test
    void aSessionWaitingOnARestoreBusyCommitIsClosedPastTheRestoreWaitBoundAndKeepsItsUploadUntilThen() throws Exception {
        String tenant = "t-imp-cc-wait";
        String token = env.newTenant(tenant);
        ExportTestClock clock = new ExportTestClock(Instant.parse("2026-11-13T12:00:00Z"));
        env.exportSessions(TenantExportSessions.Config.defaults().withMaxSessions(6), clock);
        var sessions = env.importSessions(TenantImportSessions.Config.defaults().withLoadMax(Duration.ofMinutes(30)), List.of());
        JsonNode opened = openAndUpload(token);
        String id = opened.get("session_id").asText();
        try (Connection other = holdRestoreLock()) {
            assertThat(env.call("POST", IMPORT + "/" + id + "/commit", token).statusCode()).isEqualTo(503);
            clock.advance(Duration.ofMinutes(9));   // the verb retries every 60 s: each refused commit is activity
            assertThat(env.call("POST", IMPORT + "/" + id + "/commit", token).statusCode()).isEqualTo(503);
            clock.advance(Duration.ofMinutes(9));
            assertThat(env.call("POST", IMPORT + "/" + id + "/commit", token).statusCode()).isEqualTo(503);
            assertThat(sessions.sweep()).as("past the 60 minute total, but the total stopped at the first commit attempt").isZero();
            clock.advance(Duration.ofMinutes(9));
            assertThat(env.call("POST", IMPORT + "/" + id + "/commit", token).statusCode()).isEqualTo(503);
            clock.advance(Duration.ofMinutes(5));   // 32 minutes since the first attempt: past the 30 minute restore-wait bound

            assertThat(sessions.sweep()).isEqualTo(1);

            assertThat(json(env.call("GET", IMPORT + "/" + id, token)).get("status").asText()).startsWith("failed: restore-wait bound");
            other.rollback();
        }
        assertTenantEmpty(tenant);
    }

    @Test
    void aSessionThatWasWaitingKeepsItsUploadAndCommitsOnceTheOtherLoadIsGone() throws Exception {
        String tenant = "t-imp-cc-wait-ok";
        String token = env.newTenant(tenant);
        JsonNode opened = openAndUpload(token);
        String id = opened.get("session_id").asText();
        try (Connection other = holdRestoreLock()) {
            HttpResponse<String> busy = env.call("POST", IMPORT + "/" + id + "/commit", token);
            assertThat(busy.statusCode()).isEqualTo(503);
            assertThat(json(busy).get("reason").asText()).isEqualTo("restore_busy");
            other.rollback();
        }
        assertThat(env.call("POST", IMPORT + "/" + id + "/commit", token).statusCode()).isEqualTo(202);
        assertThat(env.awaitEnd(token, id, Duration.ofMinutes(2)).get("status").asText()).isEqualTo("committed");
        assertThat(env.count("memory", tenant)).isEqualTo(env.count("memory", SRC));
    }
}
