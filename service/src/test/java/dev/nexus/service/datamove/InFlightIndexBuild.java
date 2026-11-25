// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.PciBuilders;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.slf4j.LoggerFactory;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.DriverManager;
import java.util.HexFormat;
import java.util.Properties;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

/**
 * A real {@code CREATE INDEX CONCURRENTLY} held in flight (RDR-229 P3.7 row 23, nexus-p2m1x.27), on a tenant's leaf of {@code nexus.chunks}, by
 * a session named as a PCI builder: a blocker session holds ROW EXCLUSIVE on the leaf (a {@code DELETE} that matches nothing), so the build
 * waits "for writers before build" and shows in {@code pg_stat_progress_create_index} until {@link #close} ends the blocker's transaction. The
 * build runs as the engine's admin role, which is the role the finisher reads {@code pg_stat_activity} as (another role's application name is
 * hidden from it). The statements are the repo's test helpers' ({@link PgContainerHelper#createLeafHnswIndexConcurrently}); no SQL text lives
 * here. Never ended by the test: {@link #close} lets it finish.
 */
final class InFlightIndexBuild implements AutoCloseable {

    private static final String MODEL = "voyage-code-3";
    private static final String COLUMN = "embedding_1024";
    private static final String COLLECTION = "code__row23__voyage-code-3__v1";

    private final Connection blocker;
    private final Connection buildSession;
    private final CompletableFuture<Void> build;

    private InFlightIndexBuild(Connection blocker, Connection buildSession, CompletableFuture<Void> build) {
        this.blocker = blocker;
        this.buildSession = buildSession;
        this.build = build;
    }

    /** {@code nexus.partition_name('chunks', model, tenant)}. */
    static String leafName(String tenant) {
        return "chunks_m" + sha256Hex(MODEL).substring(0, 8) + "_t_" + sha256Hex(tenant).substring(0, 16);
    }

    private static String sha256Hex(String s) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(s.getBytes(StandardCharsets.UTF_8)));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private static Connection admin(DataMoveIntegrationBase t, String applicationName) throws Exception {
        var props = new Properties();
        props.setProperty("user", DataMoveIntegrationBase.ADMIN);
        props.setProperty("password", DataMoveIntegrationBase.ADMIN_PASSWORD);
        props.setProperty("ApplicationName", applicationName);
        return DriverManager.getConnection(t.pg.getJdbcUrl(), props);
    }

    /**
     * Starts the build and returns once the server shows it in progress under {@code applicationName}.
     *
     * @param tenant a tenant whose leaf is minted by a {@code service_tokens} row, seeded here
     * @param index  the index the build makes: a plain identifier, new to the database
     */
    static InFlightIndexBuild start(DataMoveIntegrationBase t, String tenant, String applicationName, String index) throws Exception {
        PgContainerHelper.seedServiceToken(t.ctx(), "tok-" + tenant + "-0123456789abcdef000000", tenant, "row23");
        String leaf = leafName(tenant);
        Connection blocker = admin(t, "row23-blocker");
        blocker.setAutoCommit(false);
        DSL.using(blocker, SQLDialect.POSTGRES).deleteFrom(DSL.table(DSL.name("nexus", leaf))).where(DSL.falseCondition()).execute();
        Connection session = admin(t, applicationName);
        CompletableFuture<Void> build = CompletableFuture.runAsync(() -> {
            try {
                PgContainerHelper.createLeafHnswIndexConcurrently(session, "nexus", leaf, index, COLUMN, COLLECTION);
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
        var started = new InFlightIndexBuild(blocker, session, build);
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
        while (PciBuilders.countBuilding(t.ctx(), LoggerFactory.getLogger("in-flight-index-build")) < 1) {
            if (build.isCompletedExceptionally() || System.nanoTime() > deadline) {
                started.close();
                throw new AssertionError("the index build never showed in pg_stat_progress_create_index (" + build + ")");
            }
            Thread.sleep(50);
        }
        return started;
    }

    /** The blocker lets go, the build finishes, and the sessions close. */
    @Override
    public void close() throws Exception {
        try {
            blocker.rollback();
            blocker.close();
            build.get(60, TimeUnit.SECONDS);
        } finally {
            buildSession.close();
        }
    }
}
