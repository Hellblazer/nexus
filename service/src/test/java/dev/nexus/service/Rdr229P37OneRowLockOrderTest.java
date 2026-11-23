// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27; linda, folio's P3.5a review): the one-row rename's lock order is the rename lock, then FOR UPDATE on the registry rows it
 * touches in ascending collection_id order, then everything else. The id writers hold FOR KEY SHARE on the registry row; once the name key is gone the
 * name UPDATE is a non-key update, so without FOR UPDATE a writer could commit into the row the rename just retired. On a real engine:
 * (a) an id write in flight (FOR KEY SHARE) makes the rename wait, and after the writer commits the rename proceeds and the write is visible under the
 * new label; (b) the reverse: with the rename in flight, a writer asking for the retired label waits and then finds no row, while by id it finds the row
 * under the new label, never a row under the retired label.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P37OneRowLockOrderTest {

    private static final org.jooq.Table<?> ACTIVITY = DSL.table(DSL.name("pg_catalog", "pg_stat_activity"));

    private Rdr229RenameEngine engine;

    @BeforeAll
    void boot() throws Exception {
        engine = new Rdr229RenameEngine();
        Rdr229RenameRestartPrecondition.reach(engine).reached().require();
    }

    @AfterAll
    void stop() {
        if (engine != null) {
            engine.close();
        }
    }

    private static String coll(String owner) {
        return "knowledge__" + owner + "__minilm-l6-v2-384__v1";
    }

    /** Backends of this database waiting on a lock. */
    private int lockWaiters() {
        return engine.su(ctx -> ctx.fetchCount(ACTIVITY, DSL.field(DSL.name("wait_event_type"), String.class).eq("Lock")
            .and(DSL.field(DSL.name("datname"), String.class).eq(DSL.function("current_database", String.class)))));
    }

    @Test
    void anIdWriteInFlightMakesTheRenameWait_andAfterItCommitsTheRenameProceedsAndTheWriteIsVisibleUnderTheNewLabel() throws Exception {
        String tenant = "p37-lo-a";
        String token = engine.newTenant(tenant);
        String a = coll("p37loaa");
        String b = coll("p37loab");
        engine.su(ctx -> {
            Rdr229RenameSeeds.chunksAndRing(ctx, tenant, a);
            return null;
        });
        long id = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, a));
        String chash = dev.nexus.service.db.Chash.ofText("lock order writer").toHex();
        var pool = Executors.newFixedThreadPool(2);
        try (Connection w = engine.pg.createConnection("")) {
            w.setAutoCommit(false);
            DSLContext wc = DSL.using(w, SQLDialect.POSTGRES);
            wc.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(a))).forKeyShare().fetch();   // an id write in flight
            int waitersBefore = lockWaiters();

            Future<Rdr229RenameEngine.Renamed> rename = pool.submit(() -> engine.rename(token, a, b));

            assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> lockWaiters() > waitersBefore))
                .as("the rename waits on the registry row the writer holds FOR KEY SHARE").isTrue();
            assertThat(rename.isDone()).as("and has not renamed under the writer").isFalse();
            PgContainerHelper.insertChunks(wc, tenant, a, List.of(chash), List.of("written by the in-flight writer"), List.of(new float[384]), List.of(Map.of()));
            w.commit();

            var r = rename.get(60, TimeUnit.SECONDS);
            assertThat(r.status()).as("after the writer commits the rename proceeds: " + r).isEqualTo(200);
        } finally {
            pool.shutdownNow();
        }
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, b))).as("one-row: B holds the id").isEqualTo(id);
        assertThat(engine.<Long>su(ctx -> (long) ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION_ID.eq(id))
            .and(CHUNKS.CHASH.eq(java.util.HexFormat.of().parseHex(chash))))))
            .as("the write is visible under the new label's id").isEqualTo(1L);
    }

    @Test
    void withTheRenameInFlight_aWriterForTheRetiredLabelWaitsAndFindsNoRow_whileByIdItFindsTheRowUnderTheNewLabel() throws Exception {
        String tenant = "p37-lo-b";
        String token = engine.newTenant(tenant);
        String a = coll("p37lobaa");
        String b = coll("p37lobab");
        String t = coll("p37lobat");
        engine.su(ctx -> {
            Rdr229RenameSeeds.chunksAndRing(ctx, tenant, a);
            PgContainerHelper.insertCollection(ctx, tenant, t);
            return null;
        });
        long id = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, a));
        var sup = engine.call("POST", "/v1/catalog/collections/supersede", token, "{\"name\":\"" + t + "\",\"superseded_by\":\"" + a + "\"}");
        assertThat(sup.statusCode()).as("non-vacuity: a tombstone T that names A (the rename repoints it): " + sup.body()).isEqualTo(200);
        var pool = Executors.newFixedThreadPool(2);
        try (Connection h = engine.pg.createConnection(""); Connection w = engine.pg.createConnection("")) {
            h.setAutoCommit(false);
            DSL.using(h, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(t))).forUpdate().fetch();   // stalls the rename after its name UPDATE
            int base = lockWaiters();
            Future<Rdr229RenameEngine.Renamed> rename = pool.submit(() -> engine.rename(token, a, b));
            assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> lockWaiters() > base)).as("the rename is in flight, parked on the tombstone").isTrue();

            w.setAutoCommit(false);
            DSLContext wc = DSL.using(w, SQLDialect.POSTGRES);
            Future<List<Long>> byLabel = pool.submit(() -> wc.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(a))).forKeyShare().fetch(CATALOG_COLLECTIONS.COLLECTION_ID));
            assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> lockWaiters() > base + 1))
                .as("the writer for the retired label waits on the row the rename holds FOR UPDATE").isTrue();
            assertThat(byLabel.isDone()).as("it has not slipped a lock onto the retired row").isFalse();

            h.commit();   // the tombstone is released: the rename finishes

            assertThat(rename.get(60, TimeUnit.SECONDS).status()).as("the rename").isEqualTo(200);
            assertThat(byLabel.get(60, TimeUnit.SECONDS)).as("the writer for the retired label finds no row: never a row under the retired label").isEmpty();
            var byId = wc.select(CATALOG_COLLECTIONS.NAME).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(id))).forKeyShare().fetch(CATALOG_COLLECTIONS.NAME);
            assertThat(byId).as("by id the writer finds the row under the new label").containsExactly(b);
            w.commit();
        } finally {
            pool.shutdownNow();
        }
    }
}
