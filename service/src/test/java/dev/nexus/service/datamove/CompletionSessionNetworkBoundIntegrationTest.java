// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.P31CheckReplay;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.AdminDdlSession;
import dev.nexus.service.db.PgSession;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Clock;
import java.time.OffsetDateTime;
import java.util.List;
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
    private static final String TENANT = "p37-nb";
    private static final String COLLECTION = "code__p37-nb__voyage-code-3__v1";
    private static final String DOCS_TABLE = "catalog_documents";
    private static final String DOCS_LABEL = StampLabels.labelFor(DOCS_TABLE);
    private static final String DOCS_CHECK = "catalog_documents_physical_collection_id_check";
    private static final Table<?> MOVES = DSL.table(DSL.name("nexus", "data_moves"));
    private static final Table<?> DOCS = DSL.table(DSL.name("nexus", DOCS_TABLE));

    private static Field<String> text(String name) {
        return DSL.field(DSL.name(name), String.class);
    }

    /**
     * The violator scan ({@code CompletionCheck#countViolators}) binds its own short network timeout and must put the session's own bound back,
     * the value it read BEFORE it bound (folio's review FU3): the session is kept between samples, and a hard 0 put back there lost the 35 minute
     * bound for the life of the session, and made the rename readiness read (which runs after the scan in the same sample) capture 0 as its
     * "original". The read set names one stamp label whose move is complete while its CHECK is NOT VALID, so the sample scans it.
     *
     * <p>Non-vacuity: the sample is asserted to have counted the one orphan (the scan ran), the original is not 0 and not the scan's short
     * bound. The second sample answers from the violator cache (ten minutes), so it checks the cache path leaves the bound alone; the scan itself
     * is then run directly twice more on the kept session, each time asserting the bound after it.
     */
    @Test
    void aSampleThatScansANotValidCheckLeavesTheKeptSessionItsOwnNetworkTimeout() throws Exception {
        var admin = new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword());
        int original;
        try (AdminDdlSession fresh = AdminDdlSession.open(admin,
                new AdminDdlSession.Settings("nexus-rdr229-completion-probe", 0L, "network bound probe", "nexus", "data_moves", "none"), false)) {
            original = networkTimeoutOf(fresh.ctx());
        }
        int margin = 3_000;
        assertThat(original).as("non-vacuity: the session's own bound is a socket bound, not none").isGreaterThan(30_000 + margin);

        try (Connection suConn = pg.createConnection("")) {
            suConn.setAutoCommit(true);
            DSLContext su = DSL.using(suConn, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(su, TENANT, COLLECTION);
            P31CheckReplay.lift(su, P31CheckReplay.CATALOG_DOCUMENTS);
            try {
                su.insertInto(DOCS).set(text("tenant_id"), TENANT).set(text("tumbler"), "1.9.1").set(text("title"), "t")
                    .set(text("physical_collection"), "ghost-collection-p37-nb").execute();   // an orphan: no registry row carries the name
            } finally {
                P31CheckReplay.restore(su, P31CheckReplay.CATALOG_DOCUMENTS);   // the CHECK is back, NOT VALID
            }
            try {
                su.insertInto(MOVES).set(text("label"), DOCS_LABEL).set(text("template"), "t")
                    .set(text("driving_schema"), "nexus").set(text("driving_table"), DOCS_TABLE)
                    .set(DSL.field(DSL.name("chunk_size"), Integer.class), 100)
                    .set(DSL.field(DSL.name("sleep_ms"), Integer.class), 0)
                    .set(DSL.field(DSL.name("statement_timeout_ms"), Integer.class), 60_000)
                    .set(text("verification"), "v").set(text("changeset_id"), "test").execute();
                su.update(MOVES).set(DSL.field(DSL.name("completed_at"), SQLDataType.TIMESTAMPWITHTIMEZONE), OffsetDateTime.now())
                    .where(text("label").eq(DOCS_LABEL)).execute();

                PgSession.setNetworkBoundMarginMsForTests(margin);
                try {
                    var work = new CompletionBootWork(new BootWorkRegistry(Clock.systemUTC()), admin,
                        new CompletionCheck.ReadSet(List.of(DOCS_LABEL), List.of()), CompletionCheck.Gate3Source.NOT_INSTALLED,
                        () -> Set.of("p37-network-bound-probe"));
                    try {
                        CompletionCheck.Result first = work.refresh();
                        assertThat(first.violators()).as("non-vacuity: the sample scanned the NOT VALID CHECK and counted its orphan")
                            .singleElement().satisfies(v -> {
                                assertThat(v.constraint()).isEqualTo(DOCS_CHECK);
                                assertThat(v.counted()).isTrue();
                                assertThat(v.orphans()).isEqualTo(1);
                            });
                        DSLContext kept = work.keptSessionCtxForTests();
                        assertThat(kept).isNotNull();
                        assertThat(networkTimeoutOf(kept)).as("after the sample that scanned: the session's own bound, not 0").isEqualTo(original);

                        work.refresh();
                        assertThat(work.keptSessionCtxForTests()).as("non-vacuity: the second sample read on the same session").isSameAs(kept);
                        assertThat(networkTimeoutOf(kept)).as("after the second sample").isEqualTo(original);

                        P31Checks.Row row = P31Checks.load().stream().filter(r -> DOCS_CHECK.equals(r.constraint())).findFirst().orElseThrow();
                        for (int scan = 1; scan <= 2; scan++) {
                            assertThat(CompletionCheck.DIRECT.count(kept, row).counted()).as("scan " + scan + " ran").isTrue();
                            assertThat(networkTimeoutOf(kept)).as("after direct scan " + scan).isEqualTo(original);
                        }
                    } finally {
                        work.close();
                    }
                } finally {
                    PgSession.setNetworkBoundMarginMsForTests(-1);
                }
            } finally {
                su.deleteFrom(MOVES).where(text("label").eq(DOCS_LABEL)).execute();
                su.deleteFrom(DOCS).where(text("tenant_id").eq(TENANT)).execute();
            }
        }
    }
}
