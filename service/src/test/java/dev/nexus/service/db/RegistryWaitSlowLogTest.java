// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import dev.nexus.service.PgContainerHelper;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;
import org.slf4j.LoggerFactory;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23): an ingest writer's registry read ({@code FOR KEY SHARE}) has no timeout of its own, so it waits as long as a retiring verb
 * holds the row; the wait is not silent. A read that took a second or more is logged after it returns, {@code event=registry_wait_slow}, with the tenant,
 * the collection, its id, the wait in ms and the route. The test holds the row {@code FOR UPDATE} from a hand session for over a second, reads it through
 * {@code CollectionRegistry.requireForWrite} in a write transaction, and asserts the line; an unobstructed read afterwards logs nothing.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class RegistryWaitSlowLogTest {

    private static final String SVC_ROLE = "svc_registry_wait_slow";
    private static final String SVC_PASS = "svc_registry_wait_slow_pass";
    private static final String TENANT = "registry-wait-slow";
    private static final String COLL = "knowledge__waitslow__bge-base-en-v15-768__v1";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
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
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, COLL);
        }
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(3);
        config.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(config);
        scope = new TenantScope(svcDs);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private List<String> logsOf(Runnable body) {
        var logger = (ch.qos.logback.classic.Logger) LoggerFactory.getLogger(CollectionRegistry.class);
        var appender = new ListAppender<ILoggingEvent>();
        appender.start();
        Level before = logger.getLevel();
        logger.setLevel(Level.INFO);
        logger.addAppender(appender);
        try {
            body.run();
        } finally {
            logger.detachAppender(appender);
            logger.setLevel(before);
        }
        return appender.list.stream().map(ILoggingEvent::getFormattedMessage).toList();
    }

    @Timeout(120)
    @Test
    void aRegistryReadThatWaitedOverASecondForAVerbIsLogged_andAnUnobstructedOneIsNot() throws Exception {
        List<String> slow = logsOf(() -> {
            try (Connection verb = pg.createConnection("")) {
                verb.setAutoCommit(false);
                DSL.using(verb, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(COLL)).forUpdate().fetch();
                CompletableFuture<Object> writer = CompletableFuture.supplyAsync(() -> {
                    ExpectedCollectionIds.noteRoute("/v1/test/slow-route");
                    try {
                        return scope.withTenant(TENANT, ctx -> CollectionRegistry.requireForWrite(ctx, TENANT, COLL));
                    } finally {
                        ExpectedCollectionIds.clear();
                    }
                });
                Thread.sleep(1_400);   // the verb keeps the row past the threshold, then lets go
                verb.rollback();
                writer.get(60, TimeUnit.SECONDS);
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
        assertThat(slow).as("one registry_wait_slow line").singleElement().satisfies(line -> {
            assertThat(line).contains("event=registry_wait_slow").contains("tenant=" + TENANT).contains("collection=" + COLL)
                .contains("route=/v1/test/slow-route");
            assertThat(line).containsPattern("wait_ms=1[0-9]{3,}");
            assertThat(line).containsPattern("collection_id=\\d+");
        });

        List<String> quiet = logsOf(() -> scope.withTenant(TENANT, ctx -> CollectionRegistry.requireForWrite(ctx, TENANT, COLL)));
        assertThat(quiet).as("a read that did not wait logs nothing").isEmpty();
    }
}
