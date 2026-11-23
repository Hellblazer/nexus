// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.AdminDdlSession;
import dev.nexus.service.db.PgSession;
import org.jooq.DSLContext;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Clock;
import java.util.Set;

/**
 * RDR-229 P3.7 (linda 2026-11-23): the rename readiness read binds a short network timeout on the check's session
 * ({@link CompletionCheck#READINESS_READ_TIMEOUT_MS} plus the margin, through {@code PgSession.setLocal}) and the session outlives the
 * read ({@link CompletionBootWork} keeps it between samples). The session carries its own socket bound ({@code AdminDdlSession}'s 35 minutes),
 * so after every sample it must carry THAT value again, not the short one and not "none". Two samples on the one kept session: the first
 * binds and restores, the second finds the original and does the same.
 *
 * <p>Non-vacuity: the original is read from a fresh session opened the way the check opens its own, and is asserted to be neither 0 nor
 * the short bound; the kept session is asserted to be the same object across the two samples.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CompletionSessionNetworkBoundIntegrationTest {

    private PostgreSQLContainer<?> pg;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
    }

    @AfterAll
    void stop() {
        if (pg != null) {
            pg.stop();
        }
    }

    private static int networkTimeoutOf(DSLContext ctx) {
        return ctx.connectionResult(Connection::getNetworkTimeout);
    }

    @Test
    void theKeptSessionCarriesItsOwnNetworkTimeoutAfterEverySample() throws Exception {
        var admin = new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword());
        int original;
        try (AdminDdlSession fresh = AdminDdlSession.open(admin,
                new AdminDdlSession.Settings("nexus-rdr229-completion-probe", 0L, "network bound probe", "nexus", "data_moves", "none"), false)) {
            original = networkTimeoutOf(fresh.ctx());
        }
        int margin = 3_000;
        int shortBound = (int) CompletionCheck.READINESS_READ_TIMEOUT_MS + margin;
        assertThat(original).as("non-vacuity: the session's own bound is a socket bound, not none").isGreaterThan(shortBound);
        PgSession.setNetworkBoundMarginMsForTests(margin);
        try {
            var work = new CompletionBootWork(new BootWorkRegistry(Clock.systemUTC()), admin, new CompletionCheck.ReadSet(java.util.List.of(), java.util.List.of()),
                CompletionCheck.Gate3Source.NOT_INSTALLED, () -> Set.of("p37-network-bound-probe"));
            assertThat(work.keptSessionCtxForTests()).as("no session before the first sample").isNull();

            work.refresh();
            DSLContext kept = work.keptSessionCtxForTests();
            assertThat(kept).as("the first sample opened the kept session").isNotNull();
            assertThat(networkTimeoutOf(kept)).as("after the first sample the session carries its own bound again").isEqualTo(original);

            work.refresh();
            assertThat(work.keptSessionCtxForTests()).as("non-vacuity: the second sample read on the same session").isSameAs(kept);
            assertThat(networkTimeoutOf(kept)).as("after the second sample too").isEqualTo(original);
            work.close();
        } finally {
            PgSession.setNetworkBoundMarginMsForTests(-1);
        }
    }
}
