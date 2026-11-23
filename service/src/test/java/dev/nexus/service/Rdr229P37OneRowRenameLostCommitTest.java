// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.CollectionRegistry;
import dev.nexus.service.db.TenantScope;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

/**
 * RDR-229 row 15 addendum, for the one-row rename (nexus-p2m1x.27): when PostgreSQL applies the rename's COMMIT but the client sees an exception, both
 * labels still leave the registry cache. The cache answers a KNOWN row with no read, so a stale entry for the renamed-away label would let a write
 * pass the registration check and strand rows until restart. This is {@code RegistryEvictOnLostCommitTest}'s pattern ({@link CommitThenThrowDataSource}),
 * over a repository in the one-row regime: the engine's own gate-3 latch, readiness and rename guard on a pool whose connections apply a COMMIT and then throw.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P37OneRowRenameLostCommitTest {

    private Rdr229RenameEngine engine;
    private CommitThenThrowDataSource lossy;
    private CatalogRepository repo;

    @BeforeAll
    void boot() throws Exception {
        engine = new Rdr229RenameEngine();
        Rdr229RenameRestartPrecondition.reach(engine).reached().require();
        lossy = new CommitThenThrowDataSource(engine.dataSource());
        repo = new CatalogRepository(new TenantScope(lossy.dataSource()), engine.completionCheck()::gate3Latched, engine.guard());
    }

    @AfterAll
    void stop() {
        if (engine != null) {
            engine.close();
        }
    }

    private static String coll(String owner, int version) {
        return "knowledge__" + owner + "__minilm-l6-v2-384__v" + version;
    }

    @Test
    void aOneRowRenameWhoseCommitIsLostLeavesNeitherLabelCached() throws Exception {
        String tenant = "p37-lostcommit";
        engine.newTenant(tenant);
        String a = coll("p37lca", 1);
        String b = coll("p37lcb", 1);
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, a);
            return null;
        });
        long id = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, a));
        CollectionRegistry.lookup(engine.scope(), tenant, a);
        assertThat(CollectionRegistry.cached(tenant, a)).as("setup: the old label is cached").isPresent();

        int before = lossy.lostAcknowledgements();
        Throwable thrown = null;
        lossy.arm();
        try {
            repo.renameCollection(tenant, a, b, null, null, null, null);
        } catch (RuntimeException e) {
            thrown = e;
        } finally {
            lossy.disarm();
        }

        assertThat(thrown).as("the rename's COMMIT reached the caller as an exception").isNotNull();
        assertThat(lossy.lostAcknowledgements()).as("setup: a COMMIT was applied and then reported lost; the rename threw %s", thrown).isGreaterThan(before);
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, b)))
            .as("PostgreSQL applied the rename, and it was the one-row rename: the row keeps its id under the new label").isEqualTo(id);
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, a))).as("nothing is left at the old label").isNull();
        assertThat(CollectionRegistry.cached(tenant, a)).as("no stale entry for the label the row left").isEmpty();
        assertThat(CollectionRegistry.cached(tenant, b)).as("no entry for the new label").isEmpty();
    }
}
