// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.datamove.CompletionCheck;
import dev.nexus.service.datamove.RenameReadiness.Missing;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.CollectionOneRowRename;
import dev.nexus.service.db.RenameGuard;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Function;

/**
 * RDR-229 P3.7, review SB3 (linda 2026-11-23), at the repository: the catalog rename takes its path from the latch ROW, read in the rename's own
 * transaction under the shared rename lock ({@link RenameGuard#runRename}), and from nothing else. The in-process {@code Gate3} flag the repository
 * also holds is the readers' signal and has no say here: the path follows the row against a contrary flag in both directions, and a row that cannot be
 * read fails the rename with nothing written (no guess either way).
 *
 * <p>Ordered: the name path runs first, while the name keys are still in place; the latch row and the keys' drop (the name-key drop move, as far as this
 * class needs it) follow.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class Rdr229P37RenameLatchRowTest {

    private static final String TENANT = "p37-latch-row";
    private static final String SVC_ROLE = "svc_p37_latch_row";
    private static final String SVC_PASS = "svc_p37_latch_row_pass";
    private static final org.jooq.Table<?> LATCH = DSL.table(DSL.name("nexus", "rdr229_gate_latch"));

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private CatalogRepository repo;
    /** The in-process flag the repository holds: the readers' signal, deliberately set against the row. */
    private final AtomicBoolean flag = new AtomicBoolean();
    private final AtomicReference<Missing> readiness = new AtomicReference<>(Missing.NAME_KEY_DROP_MOVE);

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        su(ctx -> {
            PgContainerHelper.ensureTenantPartitions(ctx, TENANT);
            return null;
        });
        var config = new HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(4);
        config.setAutoCommit(true);
        svcDs = new HikariDataSource(config);
        repo = new CatalogRepository(new TenantScope(svcDs), flag::get, new RenameGuard(readiness::get, new CollectionOneRowRename()));
    }

    @AfterAll
    void stop() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private <T> T su(Function<DSLContext, T> work) {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private static String coll(String stem, int version) {
        return "knowledge__" + stem + "__minilm-l6-v2-384__v" + version;
    }

    private String seeded(String stem) {
        String c = coll(stem, 1);
        su(ctx -> {
            Rdr229RenameSeeds.chunksAndRing(ctx, TENANT, c);
            return null;
        });
        return c;
    }

    private long chunksNamed(String label) {
        return su(ctx -> (long) ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(label))));
    }

    @Test
    @Order(1)
    void noLatchRow_theRenameIsTheNamePath_whateverTheFlagSays() {
        flag.set(true);   // contrary: the flag says latched, the row does not exist
        readiness.set(Missing.NAME_KEY_DROP_MOVE);
        String from = seeded("lr-a");
        long idBefore = su(ctx -> Rdr229RenameChecks.registryId(ctx, TENANT, from));

        var out = repo.renameCollectionOutcome(TENANT, from, coll("lr-a", 2), null, null, null, null);

        assertThat(out.idsStable()).as("the name path mints a new id").isFalse();
        assertThat(out.collectionId()).isNotEqualTo(idBefore);
        assertThat(chunksNamed(coll("lr-a", 2))).as("the name path re-homes the chunks").isEqualTo(2);
        assertThat(chunksNamed(from)).isZero();
    }

    @Test
    @Order(2)
    void aLatchRow_theRenameIsOneRow_whateverTheFlagSays() {
        // the name-key drop move, as far as this class needs it: every inbound foreign key on a registry key that holds the name, then the latch row
        su(ctx -> {
            for (var key : dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.getKeys()) {
                if (!key.getFields().contains(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.NAME)) continue;
                for (var fk : key.getReferences()) {
                    ctx.alterTable(fk.getTable()).dropConstraintIfExists(fk.getName()).execute();
                }
            }
            for (var key : CHUNKS.getKeys()) {
                if (!key.getFields().contains(CHUNKS.COLLECTION)) continue;
                for (var fk : key.getReferences()) {
                    ctx.alterTable(fk.getTable()).dropConstraintIfExists(fk.getName()).execute();
                }
            }
            CompletionCheck.insertLatch(ctx, CompletionCheck.GATE3, "test-engine", false);
            return null;
        });
        readiness.set(Missing.NONE);
        flag.set(false);   // contrary: the flag says not latched, the row exists
        String from = seeded("lr-b");
        long idBefore = su(ctx -> Rdr229RenameChecks.registryId(ctx, TENANT, from));

        var out = repo.renameCollectionOutcome(TENANT, from, coll("lr-b", 2), null, null, null, null);

        assertThat(out.idsStable()).as("the row says latched: the one-row rename, the id survives").isTrue();
        assertThat(out.collectionId()).isEqualTo(idBefore);
        Long idAfter = su(ctx -> Rdr229RenameChecks.registryId(ctx, TENANT, coll("lr-b", 2)));
        assertThat(idAfter).as("the registry row carries the new label").isEqualTo(idBefore);
        assertThat(chunksNamed(from)).as("the chunks keep the old name column: no child row was rewritten").isEqualTo(2);
        assertThat(chunksNamed(coll("lr-b", 2))).isZero();
    }

    @Test
    @Order(3)
    void aRowThatCannotBeRead_failsTheRename_andWritesNothing() {
        String from = seeded("lr-c");
        long idBefore = su(ctx -> Rdr229RenameChecks.registryId(ctx, TENANT, from));
        // the service role can no longer read the latch table: the rename's read of the row fails, and a failed read is neither latched nor not latched
        su(ctx -> {
            ctx.revoke(DSL.privilege("select")).on(LATCH).from(DSL.role(SVC_ROLE)).execute();
            return null;
        });
        try {
            flag.set(true);
            assertThatThrownBy(() -> repo.renameCollectionOutcome(TENANT, from, coll("lr-c", 2), null, null, null, null))
                .as("an unreadable latch row fails the rename: no guess in either direction").isInstanceOf(RuntimeException.class);
            flag.set(false);
            assertThatThrownBy(() -> repo.renameCollectionOutcome(TENANT, from, coll("lr-c", 2), null, null, null, null))
                .isInstanceOf(RuntimeException.class);
        } finally {
            su(ctx -> {
                ctx.grant(DSL.privilege("select")).on(LATCH).to(DSL.role(SVC_ROLE)).execute();
                return null;
            });
        }
        Long stillOld = su(ctx -> Rdr229RenameChecks.registryId(ctx, TENANT, from));
        Long newLabel = su(ctx -> Rdr229RenameChecks.registryId(ctx, TENANT, coll("lr-c", 2)));
        assertThat(stillOld).as("nothing was written: the row is still under the old label").isEqualTo(idBefore);
        assertThat(newLabel).isNull();
        assertThat(chunksNamed(from)).isEqualTo(2);
    }
}
