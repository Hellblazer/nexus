// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.MintedLabel;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.TestMethodOrder;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.8 (nexus-p2m1x.28): the mint when the {@code collection_id} sequence is behind the data. A restore that
 * writes explicit ids and runs no {@code setval} leaves the sequence below the rows it wrote; the next mint draws an id
 * that is already a row. The mint heals ONCE (a {@code setval} to the largest id, as the engine role, which holds
 * UPDATE on that one sequence by {@code collections-002}), logs a WARN naming the old and new values, and draws again;
 * a second taken id is a {@link MintedLabel.SequenceBehindDataException} (lead ruling T2
 * {@code nexus_rdr/229-lead-ruling-collection-id-by-default-2026-11-21}). Its own database: it moves the sequence.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CollectionMintHealTest {

    private static final String TENANT = "mint-heal-tenant";
    private static final String SVC_ROLE = "svc_mint_heal";
    private static final String SVC_PASS = "svc_mint_heal_pass";
    private static final String MODEL = "bge-base-en-v15-768";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    CatalogRepository repo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var config = new HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(4);
        config.setAutoCommit(true);
        svcDs = new HikariDataSource(config);
        repo = new CatalogRepository(new TenantScope(svcDs));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private <T> T su(Function<DSLContext, T> work) {
        try (Connection c = pg.createConnection("")) {
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private static Map<String, Object> attrs(String owner) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("content_type", "knowledge");
        m.put("owner_id", owner);
        m.put("embedding_model", MODEL);
        m.put("model_version", "v1");
        return m;
    }

    private static long id(Map<String, Object> row) {
        return ((Number) row.get("collection_id")).longValue();
    }

    private long nextSequenceValue() {
        return su(ctx -> {
            String seq = ctx.fetchValue("SELECT pg_get_serial_sequence('nexus.catalog_collections', 'collection_id')", String.class);
            var r = ctx.fetchOne("SELECT last_value, is_called FROM " + seq);
            return r.get(1, Boolean.class) ? r.get(0, Long.class) + 1 : r.get(0, Long.class);
        });
    }

    /** A live row with an explicit id, as a restore writes it (no sequence involved). */
    private void seedWithExplicitId(String name, long collectionId) {
        su(ctx -> ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                CATALOG_COLLECTIONS.COLLECTION_ID, CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION, CATALOG_COLLECTIONS.LIFECYCLE_STATE)
            .values(TENANT, name, collectionId, "knowledge", "restored-" + name, MODEL, "v1", "live").execute());
    }

    @Test @Order(1)
    void anIdAtOrAboveTheSequenceIsHealedOnceAndTheMintDrawsPastIt() {
        long next = nextSequenceValue();
        seedWithExplicitId("restored-at-next", next);
        seedWithExplicitId("restored-far-above", next + 5);
        var logs = new ListAppender<ILoggingEvent>();
        logs.start();
        var root = (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME);
        root.addAppender(logs);
        Map<String, Object> row;
        try {
            row = repo.mintCollection(TENANT, attrs("subject-heal-1"), true);
        } finally {
            root.detachAppender(logs);
        }

        assertThat(id(row)).as("the sequence was set to the largest id, so the next draw is past every restored row")
            .isEqualTo(next + 6);
        assertThat(row.get("name")).isEqualTo("c" + (next + 6));
        assertThat(logs.list.stream().map(ILoggingEvent::getFormattedMessage))
            .anyMatch(m -> m.contains("event=collection_id_sequence_behind_data"));
        assertThat(nextSequenceValue()).as("the heal left the sequence after the minted id").isEqualTo(next + 7);
    }

    @Test @Order(2)
    void aSecondTakenIdAfterTheHealIsRefusedAsSequenceBehindData() {
        // The sequence cycles over [1, max]: after the heal (setval to max) the next draw wraps to 1, which is a row too.
        su(ctx -> {
            String seq = ctx.fetchValue("SELECT pg_get_serial_sequence('nexus.catalog_collections', 'collection_id')", String.class);
            ctx.execute("DELETE FROM nexus.catalog_collections WHERE tenant_id = '" + TENANT + "' AND collection_id = 1");
            ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                    CATALOG_COLLECTIONS.COLLECTION_ID, CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                    CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION, CATALOG_COLLECTIONS.LIFECYCLE_STATE)
                .values(TENANT, "cycle-anchor", 1L, "knowledge", "cycle-anchor", MODEL, "v1", "live").execute();
            long max = ctx.fetchValue("SELECT max(collection_id) FROM nexus.catalog_collections", Long.class);
            ctx.execute("SELECT setval('" + seq + "', " + max + ")");
            ctx.execute("ALTER SEQUENCE " + seq + " MAXVALUE " + max + " CYCLE");
            return null;
        });

        assertThatThrownBy(() -> repo.mintCollection(TENANT, attrs("subject-heal-2"), true))
            .isInstanceOf(MintedLabel.SequenceBehindDataException.class);
        assertThat(su(ctx -> ctx.fetchCount(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)
            .and(CATALOG_COLLECTIONS.OWNER_ID.eq("subject-heal-2"))))).as("nothing was written").isZero();
    }
}
