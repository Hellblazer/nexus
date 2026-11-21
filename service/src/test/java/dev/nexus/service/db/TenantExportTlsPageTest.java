// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.TenantExport.ExportTable;
import dev.nexus.service.db.TenantExportSessions.Config;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.security.MessageDigest;
import java.sql.Connection;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * An export page over TLS, the way the managed cloud connects to Postgres (bead nexus-csayy.9). The first chunks page
 * of a cloud export hung on 2026-11-14: the byte cut left the rest of the COPY in the stream, the pool's close of the
 * discarded connection rolled it back, skipping that rest, while Hikari's close thread closed the same TLS socket, and
 * PgJDBC's skip loop spun for ever on the end of stream a closed TLS socket reads as. The session's page gate stayed
 * held. Plain TCP throws there instead, so only a TLS server shows it: this class runs one.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TenantExportTlsPageTest {

    private static final String A = "t-export-tls";
    private static final String BGE = "bge-base-en-v15-768";
    private static final String COLLECTION = "knowledge__exporttls__bge-base-en-v15-768__v1";
    /** About 25 MB of chunk text: a 1 MiB page leaves most of the COPY unread when it is cut. */
    private static final int CHUNKS_SEEDED = 600;
    private static final int PAGE_BYTES = 1 << 20;

    private PostgreSQLContainer<?> pg;
    private HikariDataSource ds;
    private final List<TenantExportSessions> opened = new ArrayList<>();

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.startDedicated(List.of("-c", "ssl=on",
            "-c", "ssl_cert_file=/etc/ssl/certs/ssl-cert-snakeoil.pem",
            "-c", "ssl_key_file=/etc/ssl/private/ssl-cert-snakeoil.key"));
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.ensureTenantPartitions(dsl, A);
            PgContainerHelper.insertCollection(dsl, A, COLLECTION, BGE);
            String filler = "lorem ipsum dolor sit amet ".repeat(1_500);   // few distinct trigrams: a cheap index entry
            for (int i = 0; i < CHUNKS_SEEDED; i++) {
                String text = "chunk " + i + " " + filler;
                dsl.insertInto(CHUNKS)
                    .set(CHUNKS.TENANT_ID, A)
                    .set(CHUNKS.COLLECTION, COLLECTION)
                    .set(CHUNKS.COLLECTION_ID, CollectionIds.idOf(A, COLLECTION))
                    .set(CHUNKS.CHASH, MessageDigest.getInstance("SHA-256")
                        .digest(text.getBytes(java.nio.charset.StandardCharsets.UTF_8)))
                    .set(CHUNKS.EMBEDDING_MODEL, BGE)
                    .set(CHUNKS.CHUNK_TEXT, text)
                    .set(CHUNKS.EMBEDDING_768, Vector.of(new float[768]))
                    .execute();
            }
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl().replace("sslmode=disable", "sslmode=require"));
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
        try (Connection c = ds.getConnection()) {   // non-vacuity: the pool really speaks TLS
            Boolean ssl = DSL.using(c, SQLDialect.POSTGRES)
                .select(DSL.field(DSL.name("ssl"), Boolean.class))
                .from(DSL.table(DSL.name("pg_stat_ssl")))
                .where(DSL.field(DSL.name("pid"), Integer.class).eq(DSL.function("pg_backend_pid", Integer.class)))
                .fetchOne(0, Boolean.class);
            assertThat(ssl).as("the test pool's connection is TLS").isTrue();
        }
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

    private static ExportTable chunks() {
        return TenantExport.exportable("chunks").orElseThrow();
    }

    /** One first chunks page through the gate, as the route reads it, on another thread so a hang is a failure. */
    private static TenantExportReads.Page firstPage(TenantExportSessions s, TenantExportSessions.Session session)
            throws Exception {
        return CompletableFuture.supplyAsync(() -> s.withPage(session, held -> s.reads().page(held.snapshotId(),
                held.owner(), chunks(), null, PAGE_BYTES, 100_000)))
            .get(20, TimeUnit.SECONDS);
    }

    @Test
    void aPageCutAtItsByteTargetOverTlsReturnsAndFreesTheGate() throws Exception {
        var s = sessions();
        for (int round = 0; round < 5; round++) {   // before the fix the first round already spun (the close race)
            var session = s.open(A).session();
            var page = firstPage(s, session);
            assertThat(page.rows()).as("a cut page holds rows").isPositive().isLessThan(CHUNKS_SEEDED);
            assertThat(page.done()).as("the cut left most of the table").isFalse();
            assertThat(page.length()).isGreaterThanOrEqualTo(PAGE_BYTES);
            // the gate is free and the pool is whole: the next page of the same session reads at once
            var next = s.withPage(session, held -> s.reads().page(held.snapshotId(), held.owner(), chunks(),
                TenantExportKeys.binds(chunks().primaryKey(), TenantExportKeys.decode(page.lastKey(), chunks().primaryKey())),
                PAGE_BYTES, 100_000));
            assertThat(next.rows()).isPositive();
            s.close(A, session.id());
        }
    }

    @Test
    void aPageStillReadingAtTheWallAnswersOverBudgetReleasesTheGateAndTheInterruptEndsTheReader() throws Exception {
        var s = sessions();
        var session = s.open(A).session();
        var pool = ds.getHikariPoolMXBean();
        int baseline = pool.getActiveConnections();   // the session's held connection
        s.pageWallMsForTests = 1_500;
        s.reads().beforeCopyForTests = ctx -> ctx.select(DSL.function("pg_sleep", Object.class, DSL.inline(6))).fetch();
        long t0 = System.nanoTime();
        try {
            assertThatThrownBy(() -> s.withPage(session, held -> s.reads().page(held.snapshotId(), held.owner(),
                    chunks(), null, PAGE_BYTES, 100_000)))
                .isInstanceOf(TenantExportReads.PageOverBudgetException.class)
                .hasMessageContaining("wall deadline")
                .satisfies(e -> assertThat(((TenantExportReads.PageOverBudgetException) e).wallMs()).isEqualTo(1_500));
        } finally {
            s.reads().beforeCopyForTests = null;
            s.pageWallMsForTests = 0;
        }
        long elapsedMs = (System.nanoTime() - t0) / 1_000_000L;
        assertThat(elapsedMs).as("the gate is given up at the wall, not when the 6 s statement ends").isLessThan(4_000);
        // The interrupt ended the reader: it gave its connection back well before pg_sleep's 6 s are up (without the
        // interrupt the reader holds the connection until the statement ends, about 4.5 s after the wall)
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(2);
        while (pool.getActiveConnections() > baseline && System.nanoTime() < deadline) Thread.sleep(25);
        assertThat(pool.getActiveConnections()).as("the interrupted reader returned its pool connection")
            .isEqualTo(baseline);
        assertThat(s.liveReadersForTests(session)).as("and is no longer a live reader").isZero();
        // the gate is free at once, and the pool is usable
        var page = s.withPage(session, held -> s.reads().page(held.snapshotId(), held.owner(), chunks(), null,
            PAGE_BYTES, 100_000));
        assertThat(page.rows()).isPositive();
    }
}
