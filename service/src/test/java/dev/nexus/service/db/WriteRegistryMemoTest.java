// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
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
import java.util.concurrent.ConcurrentLinkedQueue;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.5a, linda's option (b) (bead nexus-p2m1x.23): the write transaction's registry check, {@link CollectionRegistry#requireForWrite},
 * on a real database. One {@code FOR KEY SHARE} read of the collection's row per transaction for a label request (the memo lives on the
 * transaction's configuration), the lock it takes (a verb's {@code FOR UPDATE NOWAIT} is refused while the transaction is open), the
 * memo against the verbs (a transaction that retires the row itself reads the retired row afterwards) and against a savepoint (a
 * rollback to one drops the entry, because the rollback released the lock the entry says is held), and the history stamp, which takes no
 * lock.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class WriteRegistryMemoTest {

    private static final String TENANT = "write-memo-tenant";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;
    int n;
    final ConcurrentLinkedQueue<String> reads = new ConcurrentLinkedQueue<>();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername("nexus_svc");
        cfg.setPassword("nexus_svc_pass");
        cfg.setMaximumPoolSize(2);
        cfg.setConnectionTimeout(15000);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        scope = new TenantScope(svcDs);
        CollectionRegistry.setWriteReadHookForTests(reads::add);
    }

    @AfterEach
    void clearReads() {
        reads.clear();
    }

    @AfterAll
    void stopAll() {
        CollectionRegistry.setWriteReadHookForTests(null);
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private String seeded(String stem) throws Exception {
        String label = "docs__" + stem + "-" + (n++) + "__minilm-l6-v2-384__v1";
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, label);
        }
        return label;
    }

    /** True when another session can take the row {@code FOR UPDATE NOWAIT} now: nothing holds it against a verb. */
    private boolean rowFreeForAVerb(String label) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            try {
                DSL.using(c, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(label)).forUpdate().noWait().fetch();
                return true;
            } catch (DataAccessException e) {
                return false;
            } finally {
                c.rollback();
            }
        }
    }

    @Test
    void aWriteTransactionReadsTheRowOncePerLabel_whateverAsksForItAgain() throws Exception {
        String x = seeded("once");
        scope.withTenant(TENANT, ctx -> {
            CollectionRegistry.requireForWrite(ctx, TENANT, x);
            CollectionRegistry.requireForWrite(ctx, TENANT, x);
            CollectionRegistry.requireWritable(ctx, TENANT, x);
            CollectionIds.require(ctx, TENANT, x);
            CollectionRegistry.requireForWriteIfPresent(ctx, TENANT, x);
            return null;
        });
        assertThat(reads).as("five asks, one FOR KEY SHARE read").containsExactly(TENANT + "|" + x);
    }

    @Test
    void anotherTransactionReadsAgain_theMemoDiesWithTheTransaction() throws Exception {
        String x = seeded("again");
        for (int i = 0; i < 2; i++) {
            scope.withTenant(TENANT, ctx -> CollectionRegistry.requireForWrite(ctx, TENANT, x).collectionId());
        }
        assertThat(reads).as("one read per transaction").hasSize(2);
    }

    @Test
    void aLabelRequestsReadHoldsTheRowAgainstAVerbUntilTheTransactionEnds_andAReaderHoldsNothing() throws Exception {
        String x = seeded("lock");
        boolean[] freeWhileWriting = new boolean[1];
        boolean[] freeWhileReading = new boolean[1];
        scope.withTenant(TENANT, ctx -> {
            CollectionRegistry.shared().resolveLabel(ctx, TENANT, x);   // a reader: the cache or a plain read
            try {
                freeWhileReading[0] = rowFreeForAVerb(x);
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
            CollectionRegistry.requireForWrite(ctx, TENANT, x);
            try {
                freeWhileWriting[0] = rowFreeForAVerb(x);
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
            return null;
        });
        assertThat(freeWhileReading[0]).as("a read of the label takes no lock: a verb's FOR UPDATE NOWAIT goes through").isTrue();
        assertThat(freeWhileWriting[0]).as("the write's registry check holds the row FOR KEY SHARE: the verb's lock is refused").isFalse();
        assertThat(rowFreeForAVerb(x)).as("and lets go when the transaction ends").isTrue();
    }

    @Test
    void aTransactionThatRetiresTheRowItselfReadsTheRetiredRowAfterwards_theMemoIsForgotten() throws Exception {
        String x = seeded("retire-x");
        String y = seeded("retire-y");
        scope.withTenant(TENANT, ctx -> {
            assertThat(CollectionRegistry.requireWritable(ctx, TENANT, x).isSuperseded()).as("live before the verb").isFalse();
            int updated = CollectionTombstones.supersede(ctx, TENANT, x, y, DSL.currentOffsetDateTime(), dev.nexus.service.datamove.Gate3.never());
            assertThat(updated).as("setup: the verb retired the row").isEqualTo(1);
            assertThatThrownBy(() -> CollectionRegistry.requireWritable(ctx, TENANT, x))
                .as("the memo must not hand back the live row it read before the verb's own retire")
                .isInstanceOf(CollectionSupersededException.class);
            return null;
        });
    }

    @Test
    void aRollbackToASavepointForgetsTheMemo_becauseTheRollbackReleasedTheLockTheEntrySaysIsHeld() throws Exception {
        String x = seeded("savepoint");
        scope.withTenant(TENANT, ctx -> {
            java.sql.Savepoint[] sp = new java.sql.Savepoint[1];
            ctx.connection(c -> sp[0] = c.setSavepoint());
            CollectionRegistry.requireForWrite(ctx, TENANT, x);   // read inside the savepoint
            ctx.connection(c -> c.rollback(sp[0]));
            CollectionRegistry.forgetWriteMemo(ctx);
            CollectionRegistry.requireForWrite(ctx, TENANT, x);   // read again: the entry was dropped
            return null;
        });
        assertThat(reads).as("the second ask reads again after the rollback").hasSize(2);
        try {
            assertThat(rowFreeForAVerb(x)).as("and the lock is held again by the end of the transaction, then released").isTrue();
        } finally {
            reads.clear();
        }
    }

    @Test
    void aHistoryStamp_readsTheLiveRowsIdWithoutALock_andGivesNullForANameNoRowHas() throws Exception {
        String x = seeded("history");
        boolean[] free = new boolean[1];
        Long[] stamped = new Long[2];
        scope.withTenant(TENANT, ctx -> {
            stamped[0] = CollectionIds.stampHistory(ctx, TENANT, x);
            stamped[1] = CollectionIds.stampHistory(ctx, TENANT, "docs__no-such-row__minilm-l6-v2-384__v1");
            try {
                free[0] = rowFreeForAVerb(x);
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
            return null;
        });
        Long id;
        try (Connection su = pg.createConnection("")) {
            id = DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(x)).fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
        }
        assertThat(stamped[0]).as("the id of the live row").isEqualTo(id);
        assertThat(stamped[1]).as("no row, no id, and no refusal").isNull();
        assertThat(free[0]).as("a history stamp takes no lock: a verb's FOR UPDATE NOWAIT goes through").isTrue();
        assertThat(reads).as("and is not a write-transaction registry read").isEmpty();
    }

    @Test
    void aRequestByIdAloneFollowsAOneRowRename_andOneThatSentALabelBesideItsIdIsRefusedWhenTheyNoLongerAgree() throws Exception {
        String b = seeded("rename-b");
        String c = "docs__rename-c-" + (n++) + "__minilm-l6-v2-384__v1";
        long x;
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            x = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(b)).fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, c)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(x)).execute();
        }
        final long id = x;
        // the boundary resolved the id to B, and a one-row rename made the row C: an id-only write goes to the row, under C
        ExpectedCollectionIds.set(java.util.Map.of(b, id), null, null);
        try {
            String landedUnder = scope.withTenant(TENANT, ctx -> CollectionRegistry.requireForWrite(ctx, TENANT, b).label());
            assertThat(landedUnder).as("the id is the identity: the row's current label").isEqualTo(c);
            String again = scope.withTenant(TENANT, ctx -> {
                CollectionRegistry.requireForWrite(ctx, TENANT, b);
                return CollectionRegistry.requireForWrite(ctx, TENANT, c).label();   // a writer that stamps the current label asks by it
            });
            assertThat(again).isEqualTo(c);
        } finally {
            ExpectedCollectionIds.clear();
        }
        // the client sent the label B beside the id: the two no longer name one collection
        ExpectedCollectionIds.set(java.util.Map.of(b, id), java.util.Set.of(b), null, null);
        try {
            assertThatThrownBy(() -> scope.withTenant(TENANT, ctx -> CollectionRegistry.requireForWrite(ctx, TENANT, b)))
                .isInstanceOfSatisfying(CollectionIdMismatchException.class, e -> {
                    assertThat(e.labelDisagreement()).as("the 409 form, not the registry-inconsistency 500").isTrue();
                    assertThat(e.requested()).isEqualTo(id);
                    assertThat(e.requestedLabel()).isEqualTo(b);
                    assertThat(e.currentLabel()).isEqualTo(c);
                });
        } finally {
            ExpectedCollectionIds.clear();
        }
    }

    /** The id of {@code label}'s row. */
    private long idOf(String label) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(label)).fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
        }
    }

    /** Retires {@code label}'s row as a supersede does; P3.1's CHECK ties {@code superseded_by} to {@code superseded_by_id}. */
    private void retire(String label, String successor) throws Exception {
        long successorId = idOf(successor);
        try (Connection su = pg.createConnection("")) {
            int retired = DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_COLLECTIONS)
                .set(CATALOG_COLLECTIONS.SUPERSEDED_BY, successor).set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, successorId)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(label)).execute();
            assertThat(retired).isEqualTo(1);
        }
    }

    @Test
    void aRetiredRowReadForCleanup_isNeverAnsweredAsLiveByAnIdWrite() throws Exception {
        String retired = seeded("cleanup-retired");
        String successor = seeded("cleanup-succ");
        retire(retired, successor);
        long id = idOf(retired);

        assertThatThrownBy(() -> scope.withTenant(TENANT, ctx -> {
            CollectionRegistry.lockForCleanup(ctx, TENANT, java.util.List.of(id), java.util.List.of());   // reads the retired row, memoises it
            return CollectionRegistry.requireIdForWrite(ctx, TENANT, id);   // must still be refused: the memo is not a live answer
        })).as("a retired row the cleanup read is not handed back as live").isInstanceOf(CollectionRetiredIdException.class);
    }

    @Test
    void aLiveRowReadForCleanup_isAnsweredFromTheMemoByAnIdWrite_withoutASecondRead() throws Exception {
        String live = seeded("cleanup-live");
        long id = idOf(live);

        long answered = scope.withTenant(TENANT, ctx -> {
            CollectionRegistry.lockForCleanup(ctx, TENANT, java.util.List.of(id), java.util.List.of());
            return CollectionRegistry.requireIdForWrite(ctx, TENANT, id).collectionId();
        });

        assertThat(answered).isEqualTo(id);
    }
}
