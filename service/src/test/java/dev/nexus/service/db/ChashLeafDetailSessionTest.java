// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.ChashConstraintValidator.Outcome;
import dev.nexus.service.db.ChashConstraintValidator.Step;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * nexus-f3yqd: the per-reason account of a refused chunks leaf is read under the pre-check's own statement and lock bounds, not
 * the session's 30-minute VALIDATE bound. A recording connection shows the statements the leaf's step session sent: between the
 * pre-check's count and the first reason query it set {@code statement_timeout} to the pre-check bound and {@code lock_timeout}.
 * Kills: a leaf account read under whatever the session last held.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChashLeafDetailSessionTest {

    private static final String TENANT = "t-leafsession";
    private static final String COLLECTION = "knowledge__leaf__bge-base-en-v15-768__v1";
    private static final String MODEL = "bge-base-en-v15-768";

    private PostgreSQLContainer<?> pg;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(ctx, "tok-leafsession-0123456789abcdefghij", TENANT, "leaf");
            PgContainerHelper.ensureTenantPartitions(ctx, TENANT);
            PgContainerHelper.insertCollection(ctx, TENANT, COLLECTION);
            ctx.alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraintIfExists("fk_catalog_chunks_chunk").execute();
            ctx.alterTable(CHUNKS).dropConstraintIfExists("chunks_chash_octet_check").execute();
            ctx.alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraintIfExists("catalog_document_chunks_chash_octet_check").execute();
            ctx.insertInto(CHUNKS).set(CHUNKS.TENANT_ID, TENANT).set(CHUNKS.COLLECTION, COLLECTION)
                .set(CHUNKS.CHASH, HexFormat.of().parseHex("ab".repeat(16))).set(CHUNKS.EMBEDDING_MODEL, MODEL)
                .set(CHUNKS.EMBEDDING_768, Vector.of(new float[768])).set(CHUNKS.RETENTION, "reference-only").execute();
            PgContainerHelper.readdChashOctetChecksNotValid(su);
        }
    }

    @AfterAll
    void stop() {
        if (pg != null) pg.stop();
    }

    /** A connection that records each prepared statement's SQL and binds as it executes. */
    private static Connection recording(Connection real, List<String> log) {
        return (Connection) Proxy.newProxyInstance(ChashLeafDetailSessionTest.class.getClassLoader(), new Class<?>[]{Connection.class},
            (proxy, method, args) -> {
                try {
                    Object r = method.invoke(real, args);
                    if (method.getName().equals("prepareStatement") && r instanceof PreparedStatement ps) {
                        String sql = (String) args[0];
                        Map<Integer, String> binds = new TreeMap<>();
                        return Proxy.newProxyInstance(ChashLeafDetailSessionTest.class.getClassLoader(), new Class<?>[]{PreparedStatement.class},
                            (p2, m2, a2) -> {
                                try {
                                    String n = m2.getName();
                                    if (n.startsWith("set") && a2 != null && a2.length >= 2 && a2[0] instanceof Integer i) binds.put(i, String.valueOf(a2[1]));
                                    if (n.startsWith("execute")) log.add(sql + " :: " + binds.values());
                                    return m2.invoke(ps, a2);
                                } catch (InvocationTargetException e) {
                                    throw e.getCause();
                                }
                            });
                    }
                    return r;
                } catch (InvocationTargetException e) {
                    throw e.getCause();
                }
            });
    }

    @Test
    void theLeafAccount_isReadUnderThePrechecksOwnStatementAndLockBounds() throws Exception {
        List<String> log = new ArrayList<>();
        var admin = new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword());
        var validator = new ChashConstraintValidator(admin, (a, settings, lock) -> {
            try {
                if (lock) return AdminDdlSession.open(a, settings, true);
                var props = new java.util.Properties();
                props.setProperty("user", a.user());
                props.setProperty("password", a.password());
                props.setProperty("ApplicationName", settings.applicationName());
                Connection real = DriverManager.getConnection(a.url(), props);
                real.setAutoCommit(true);
                return new AdminDdlSession(settings, recording(real, log));
            } catch (java.sql.SQLException e) {
                throw new IllegalStateException(e);
            }
        }, null, null);
        List<Step> steps = new ArrayList<>();
        String prior = System.getProperty(ChashConstraintValidator.PRECHECK_TIMEOUT_PROPERTY);
        System.setProperty(ChashConstraintValidator.PRECHECK_TIMEOUT_PROPERTY, "12345");
        try {
            validator.run(true, new ChashConstraintValidator.Listener() {
                @Override public void planned(int total) { }
                @Override public void current(String table, Long bytes) { }
                @Override public void stepped(Step step) { steps.add(step); }
                @Override public String stopReason() { return null; }
            });
        } finally {
            if (prior == null) System.clearProperty(ChashConstraintValidator.PRECHECK_TIMEOUT_PROPERTY);
            else System.setProperty(ChashConstraintValidator.PRECHECK_TIMEOUT_PROPERTY, prior);
        }

        Step leaf = steps.stream().filter(s -> s.outcome() == Outcome.REFUSED && s.table().startsWith("nexus.chunks")).findFirst().orElseThrow();
        assertThat(leaf.detail()).isNotNull();
        int firstReasonQuery = -1;
        for (int i = 0; i < log.size() && firstReasonQuery < 0; i++) if (log.get(i).contains("sha256")) firstReasonQuery = i;
        assertThat(firstReasonQuery).as("the leaf account's reason query ran: " + log).isPositive();
        boolean statementBound = false;
        boolean lockBound = false;
        for (int i = firstReasonQuery - 1; i >= 0 && !log.get(i).contains("octet_length"); i--) {
            if (log.get(i).contains("set_config") && log.get(i).contains("statement_timeout") && log.get(i).contains("12345ms")) statementBound = true;
            if (log.get(i).contains("set_config") && log.get(i).contains("lock_timeout")) lockBound = true;
        }
        assertThat(statementBound).as("statement_timeout = the pre-check bound before the reason query").isTrue();
        assertThat(lockBound).as("lock_timeout set before the reason query").isTrue();
    }
}
