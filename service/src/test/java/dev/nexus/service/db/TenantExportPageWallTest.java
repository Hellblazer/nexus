// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.TenantExportSessions.Config;
import dev.nexus.service.http.HttpUtil;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import javax.sql.DataSource;
import java.sql.Connection;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * The page wall of {@link TenantExportSessions#withPage} (bead nexus-csayy.9) with readers that do not cooperate: one
 * that ignores the interrupt, and one whose pool borrow never returns. The database is only there to open a session; the
 * reads under test touch none of it. The wall against a real driver over TLS is {@code TenantExportTlsPageTest}'s.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TenantExportPageWallTest {

    private static final String A = "t-export-wall";

    private PostgreSQLContainer<?> pg;
    private HikariDataSource ds;
    private final List<TenantExportSessions> opened = new ArrayList<>();

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.ensureTenantPartitions(DSL.using(su, SQLDialect.POSTGRES), A);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
    }

    @AfterEach
    void closeSessions() {
        opened.forEach(TenantExportSessions::close);
        opened.clear();
    }

    @AfterAll
    void stop() {
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    private TenantExportSessions sessions() {
        var s = new TenantExportSessions(ds, Config.defaults().withMaxSessions(2).withLockTimeoutMs(300)
            .withSweepInterval(Duration.ofHours(1)), new ExportTestClock(Instant.parse("2026-11-14T00:00:00Z")));
        opened.add(s);
        return s;
    }

    @Test
    void aReaderThatIgnoresTheInterruptFreesTheGateAtTheWallAndRefusesTheNextPageAsBusyUntilItEnds() throws Exception {
        var s = sessions();
        var session = s.open(A).session();
        s.pageWallMsForTests = 300;
        var started = new CountDownLatch(1);
        var release = new CountDownLatch(1);
        long t0 = System.nanoTime();
        assertThatThrownBy(() -> s.withPage(session, held -> {
            started.countDown();
            TenantExportReads.BORROW_RETURNED.get().run();   // as a page does once its pool borrow has returned
            while (release.getCount() > 0) {   // an uninterruptible reader: it swallows the interrupt and goes on
                try {
                    release.await();
                } catch (InterruptedException ignored) {
                    // the driver's skip loop of the cloud hang did not look at the flag either
                }
            }
            return "late";
        })).isInstanceOf(TenantExportReads.PageOverBudgetException.class)
            .satisfies(e -> assertThat(((TenantExportReads.PageOverBudgetException) e).wallMs())
                .as("the answer names the wall, not the database budget").isEqualTo(300));
        assertThat(started.getCount()).isZero();
        assertThat(Duration.ofNanos(System.nanoTime() - t0)).as("the wall plus the 1.5 s join, not the spinner's end")
            .isLessThan(Duration.ofSeconds(5));
        assertThat(s.liveReadersForTests(session)).as("the spinner is still alive").isEqualTo(1);
        // the gate is free (not page_in_flight), and the session still has one page read at a time: busy
        assertThatThrownBy(() -> s.withPage(session, held -> "second"))
            .isInstanceOf(TenantExportReads.PageBusyException.class);
        release.countDown();
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
        while (s.liveReadersForTests(session) != 0 && System.nanoTime() < deadline) Thread.sleep(10);
        assertThat(s.liveReadersForTests(session)).isZero();
        String third = s.withPage(session, held -> "third");
        assertThat(third).as("accepted once the spinner ended").isEqualTo("third");
    }

    @Test
    void aWallHitBeforeThePoolBorrowReturnsAnswersAsAnExhaustedPoolNotOverBudget() throws Exception {
        var s = sessions();
        var session = s.open(A).session();
        s.pageWallMsForTests = 300;
        var blocked = new CountDownLatch(1);
        DataSource stuckPool = (DataSource) java.lang.reflect.Proxy.newProxyInstance(
            getClass().getClassLoader(), new Class<?>[] {DataSource.class}, (proxy, method, args) -> {
                if (!method.getName().equals("getConnection")) throw new UnsupportedOperationException(method.getName());
                try {
                    blocked.await();   // a pool with no connection to give; the wall's interrupt ends the wait
                } catch (InterruptedException e) {
                    throw new java.sql.SQLTransientConnectionException("interrupted waiting for a connection", e);
                }
                throw new java.sql.SQLTransientConnectionException("released");
            });
        var reads = new TenantExportReads(stuckPool, 300, 20_000);
        var memory = TenantExport.exportable("memory").orElseThrow();
        assertThatThrownBy(() -> s.withPage(session, held -> reads.page(held.snapshotId(), held.owner(), memory,
                null, 1 << 20, 100)))
            .as("the route's exhausted-pool answer, which the client backs off on, not export_page_over_budget")
            .isNotInstanceOf(TenantExportReads.PageOverBudgetException.class)
            .satisfies(e -> assertThat(HttpUtil.isPoolExhausted(e)).isTrue());
        assertThat(s.liveReadersForTests(session)).as("the interrupted borrow ended").isZero();
    }
}
