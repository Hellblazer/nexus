// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23, row 14): the two registry reads a writer takes through {@code CollectionRegistry} without the key share of a
 * label or an id: {@code readSharedForWrite} (the stale-note repair's read of a collection's content type: {@code FOR SHARE}, so a concurrent retype
 * waits) and {@code lockForCleanup} (a purge of a document's manifest rows, any state of the collection). Each records the rows in the transaction's
 * memo, so the writer census sees the read: a write that binds the collection after it is no longer a {@code NO_REGISTRY_READ}, which the same write
 * before it is. The hand sessions probe the strength of the lock with a lock_timeout (a retype) and NOWAIT (a verb's FOR UPDATE).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class RegistryRowsHeldForWritersTest {

    private static final String SVC_ROLE = "svc_registry_rows_held";
    private static final String SVC_PASS = "svc_registry_rows_held_pass";
    private static final String TENANT = "registry-rows-held";
    private static final String COLL = "knowledge__rowsheld__bge-base-en-v15-768__v1";

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

    @AfterEach
    void census() {
        StrictWriterCensus.setModeForTests("off");
    }

    private long idOfColl() throws Exception {
        try (Connection su = pg.createConnection("")) {
            Long id = DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(COLL)).fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
            return id;
        }
    }

    /** The state of the SQL error a hand session got, or "" when its statement went through. */
    private String sqlStateOf(java.util.function.Consumer<DSLContext> statement) throws Exception {
        try (Connection other = pg.createConnection("")) {
            other.setAutoCommit(true);
            DSLContext dsl = DSL.using(other, SQLDialect.POSTGRES);
            dsl.select(DSL.function("set_config", String.class, DSL.val("lock_timeout"), DSL.val("300ms"), DSL.val(false))).fetch();
            try {
                statement.accept(dsl);
                return "";
            } catch (DataAccessException e) {
                return e.sqlState();
            }
        }
    }

    private void writeChunkOf(DSLContext ctx) {
        ctx.update(CHUNKS).set(CHUNKS.COLLECTION, COLL).where(CHUNKS.TENANT_ID.eq(TENANT)).and(CHUNKS.COLLECTION.eq(COLL)).execute();
    }

    @Test
    void readSharedForWrite_holdsTheRowFORSHARE_soARetypeWaits_andRecordsItInTheMemo() throws Exception {
        long id = idOfColl();
        String retype = scope.withTenant(TENANT, ctx -> {
            List<CollectionRow> rows = CollectionRegistry.readSharedForWrite(ctx, TENANT, List.of(COLL));
            assertThat(rows).as("the row of the label").hasSize(1);
            assertThat(rows.get(0).collectionId()).isEqualTo(id);
            assertThat(CollectionRegistry.holdsRow(ctx, TENANT, COLL)).as("recorded in the memo under the label").isTrue();
            assertThat(CollectionRegistry.holdsRow(ctx, TENANT, "#" + id)).as("and under its id").isTrue();
            try {
                return sqlStateOf(dsl -> dsl.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.CONTENT_TYPE, "docs")
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(COLL)).execute());
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
        assertThat(retype).as("a retype of the row waited on the share lock and timed out (55P03)").isEqualTo("55P03");
    }

    @Test
    void lockForCleanup_takesTheKeyShareByIdAndByLabel_andRecordsIt_andARowOfNoNameIsSkipped() throws Exception {
        long id = idOfColl();
        String verb = scope.withTenant(TENANT, ctx -> {
            CollectionRegistry.lockForCleanup(ctx, TENANT, List.of(id, 987654321L), List.of(COLL, "no-such-label"));
            assertThat(CollectionRegistry.holdsRow(ctx, TENANT, "#" + id)).as("held by id").isTrue();
            assertThat(CollectionRegistry.holdsRow(ctx, TENANT, COLL)).as("held by label").isTrue();
            try {
                return sqlStateOf(dsl -> dsl.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(COLL)).forUpdate().fetch());
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
        assertThat(verb).as("a verb's FOR UPDATE waits behind the key share (55P03)").isEqualTo("55P03");
    }

    @Test
    void theCensus_findsAWriteBeforeTheRead_andNotAfterIt() {
        StrictWriterCensus.setModeForTests("record");
        scope.withTenant(TENANT, ctx -> {
            writeChunkOf(ctx);
            return null;
        });
        assertThat(StrictWriterCensus.violations()).as("before any read: NO_REGISTRY_READ").hasSize(1);
        scope.withTenant(TENANT, ctx -> {
            CollectionRegistry.readSharedForWrite(ctx, TENANT, List.of(COLL));
            writeChunkOf(ctx);
            return null;
        });
        assertThat(StrictWriterCensus.violations()).as("after readSharedForWrite the same write is no finding").hasSize(1);
        scope.withTenant(TENANT, ctx -> {
            CollectionRegistry.lockForCleanup(ctx, TENANT, List.of(), List.of(COLL));
            writeChunkOf(ctx);
            return null;
        });
        assertThat(StrictWriterCensus.violations()).as("nor after lockForCleanup").hasSize(1);
    }
}
