// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23), linda's sweep-gate audit (option (d)): a taker's key is built from the label its caller
 * names, never from a content row's stored name, so after a one-row rename a sweep that holds the gate under the NEW label
 * still excludes a rehome and a rename that name the new label. The holder takes the exact key the sweeps take
 * ({@code pg_advisory_xact_lock(hashtext('sweepgate:<tenant>/<label>'))}, the shape of
 * {@code CatalogRepository.acquireSweepGateExclusive} and of the SQL sweeps) from its own session; the one-row rename is
 * simulated as the superuser does it (the registry row takes its label in place, the content keeps the old name).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class SweepGateAfterRenameTest {

    CollectionRouteFixture fx;
    ExecutorService executor;
    int n;

    @BeforeAll
    void startAll() throws Exception {
        fx = new CollectionRouteFixture();
        fx.gate3.set(true);
        executor = Executors.newCachedThreadPool();
    }

    @AfterAll
    void stopAll() {
        if (executor != null) executor.shutdownNow();
        if (fx != null) fx.close();
    }

    private String registered(String stem) {
        String l = label("swg-" + stem + "-" + (n++));
        fx.register(l);
        return l;
    }

    private String oneRowRename(String from, String stem) {
        long id = fx.idOf(from);
        String to = label("swg-" + stem + "-" + (n++));
        fx.asSuperuser(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, to)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(id)).execute());
        return to;
    }

    /** A session holding the sweep gate of {@code label} exclusive, as a sweep does, until {@link Connection#rollback}. */
    private Connection holdGate(String label) throws Exception {
        Connection holder = fx.pg.createConnection("");
        holder.setAutoCommit(false);
        DSL.using(holder, SQLDialect.POSTGRES)
            .select(DSL.function("pg_advisory_xact_lock", Object.class,
                DSL.function("hashtext", Integer.class, DSL.val("sweepgate:" + fx.tenant + "/" + label))))
            .fetch();
        return holder;
    }

    private Resp rehome(String source, String target) {
        return fx.post("/v1/catalog/collections/rehome", "{\"source\":" + json(source) + ",\"target\":" + json(target) + "}");
    }

    private Resp rename(String from, String to) {
        return fx.post("/v1/catalog/collections/rename", "{\"old_name\":" + json(from) + ",\"new_name\":" + json(to) + "}");
    }

    private void assertBlocksWhileHeld(Connection holder, java.util.concurrent.Callable<Resp> request, String what) throws Exception {
        Future<Resp> future = executor.submit(request);
        assertThatThrownBy(() -> future.get(1500, TimeUnit.MILLISECONDS))
            .as(what + " must wait while the sweep gate of its label is held")
            .isInstanceOf(TimeoutException.class);
        holder.rollback();
        holder.close();
        Resp done = future.get(30, TimeUnit.SECONDS);
        assertThat(done.status()).as("it proceeds once the gate is released: %s", done.body()).isLessThan(500);
    }

    @Test
    void afterAOneRowRename_aSweepHoldingTheGateUnderTheNewLabelBlocksARehomeNamingIt() throws Exception {
        String a = registered("rh-a");
        String t = registered("rh-t");
        String a2 = oneRowRename(a, "rh-a2");

        assertBlocksWhileHeld(holdGate(a2), () -> rehome(a2, t), "a re-home of the new label");
    }

    @Test
    void afterAOneRowRename_aSweepHoldingTheGateUnderTheNewLabelBlocksARehomeOntoIt() throws Exception {
        String a = registered("rt-a");
        String s = registered("rt-s");
        String a2 = oneRowRename(a, "rt-a2");

        assertBlocksWhileHeld(holdGate(a2), () -> rehome(s, a2), "a re-home onto the new label");
    }

    @Test
    void afterAOneRowRename_aSweepHoldingTheGateUnderTheNewLabelBlocksARenameOfIt() throws Exception {
        String a = registered("rn-a");
        String a2 = oneRowRename(a, "rn-a2");
        String fresh = label("swg-rn-fresh-" + (n++));

        assertBlocksWhileHeld(holdGate(a2), () -> rename(a2, fresh), "a rename of the new label");
    }

    @Test
    void theGateIsKeyedByTheLabel_soTheOldLabelsKeyHoldsNothingBack() throws Exception {
        String a = registered("old-a");
        String t = registered("old-t");
        String a2 = oneRowRename(a, "old-a2");

        Connection holder = holdGate(a);
        try {
            Resp r = executor.submit(() -> rehome(a2, t)).get(20, TimeUnit.SECONDS);
            assertThat(r.status()).as("a gate held under the old label does not reach the renamed collection: %s", r.body())
                .isEqualTo(200);
        } finally {
            holder.rollback();
            holder.close();
        }
    }
}
