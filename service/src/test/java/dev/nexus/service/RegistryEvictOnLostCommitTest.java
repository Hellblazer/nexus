// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.CollectionRegistry;
import dev.nexus.service.db.TenantScope;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.Map;

import static dev.nexus.service.CollectionRouteFixture.label;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 row 15 addendum (nexus-p2m1x.22): every post-commit {@code CollectionRegistry.evict} also runs when the COMMIT reaches the
 * client as an exception (PostgreSQL applied the transaction, the client saw an error), because the registry cache answers a KNOWN
 * row with no read and a stale entry would stand until restart. Each test primes the cache with the row, runs the verb on a
 * repository whose connections apply a COMMIT and then throw ({@link CommitThenThrowDataSource}), and holds that the verb threw, that
 * the database has the change, and that no cache entry survives.
 *
 * <p>The sites are the repository's: the upsert, the supersede, the rename, the delete and the ghost sweep. The one-row rename's
 * lost commit is {@code Rdr229P37OneRowRenameLostCommitTest}'s, and the tenant import's is {@code TenantImportUnconfirmedCommitTest}'s.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class RegistryEvictOnLostCommitTest {

    CollectionRouteFixture fx;
    CommitThenThrowDataSource lossy;
    CatalogRepository repo;
    TenantScope scope;
    int n;

    @BeforeAll
    void startAll() throws Exception {
        fx = new CollectionRouteFixture();
        lossy = new CommitThenThrowDataSource(fx.svcDs);
        scope = new TenantScope(fx.svcDs);
        repo = new CatalogRepository(new TenantScope(lossy.dataSource()));
    }

    @AfterAll
    void stopAll() {
        if (fx != null) fx.close();
    }

    private String registered(String stem) {
        String l = label("lostcommit-" + stem + "-" + (n++));
        fx.register(l);
        return l;
    }

    /** Fills the cache with the row of {@code label}, as a write's registry check does. */
    private void cache(String tenant, String label) {
        CollectionRegistry.lookup(scope, tenant, label);
        assertThat(CollectionRegistry.cached(tenant, label)).as("setup: %s is cached", label).isPresent();
    }

    private void lostCommit(Runnable verb) {
        int before = lossy.lostAcknowledgements();
        Throwable thrown = null;
        lossy.arm();
        try {
            verb.run();
        } catch (RuntimeException e) {
            thrown = e;
        } finally {
            lossy.disarm();
        }
        assertThat(thrown).as("the verb's COMMIT reached the caller as an exception").isNotNull();
        assertThat(lossy.lostAcknowledgements()).as("setup: a COMMIT was applied and then reported lost; the verb threw %s", thrown)
            .isGreaterThan(before);
    }

    @Test
    void anUpsertWhoseCommitIsLostLeavesNoCachedRow() {
        String a = registered("upsert");
        cache(fx.tenant, a);

        lostCommit(() -> repo.upsertCollection(fx.tenant, Map.of("name", a, "content_type", "unknown", "owner_id", fx.tenant)));

        assertThat(CollectionRegistry.cached(fx.tenant, a)).as("no stale entry for the registered label").isEmpty();
    }

    @Test
    void aSupersedeWhoseCommitIsLostLeavesNeitherEndCached() {
        String a = registered("supersede-a");
        String b = registered("supersede-b");
        cache(fx.tenant, a);
        cache(fx.tenant, b);

        lostCommit(() -> repo.supersedeCollection(fx.tenant, a, b, ""));

        assertThat(fx.supersededBy(a)).as("PostgreSQL applied the supersede").isEqualTo(b);
        assertThat(CollectionRegistry.cached(fx.tenant, a)).as("the retired label").isEmpty();
        assertThat(CollectionRegistry.cached(fx.tenant, b)).as("the successor, whose owner the supersede may have re-derived").isEmpty();
    }

    @Test
    void aRenameWhoseCommitIsLostLeavesNeitherLabelCached() {
        String a = registered("rename-a");
        String b = label("lostcommit-rename-b-" + (n++));
        cache(fx.tenant, a);

        lostCommit(() -> repo.renameCollection(fx.tenant, a, b, null, null, null, null));

        assertThat(fx.supersededBy(a)).as("PostgreSQL applied the rename").isEqualTo(b);
        assertThat(CollectionRegistry.cached(fx.tenant, a)).as("the retired old label").isEmpty();
        assertThat(CollectionRegistry.cached(fx.tenant, b)).as("the new label").isEmpty();
    }

    @Test
    void aDeleteWhoseCommitIsLostLeavesNoCachedRow() {
        String a = registered("delete");
        cache(fx.tenant, a);

        lostCommit(() -> repo.deleteCollection(fx.tenant, a));

        assertThat(fx.exists(a)).as("PostgreSQL applied the delete").isFalse();
        assertThat(CollectionRegistry.cached(fx.tenant, a)).as("no stale entry for a label with no row").isEmpty();
    }

    @Test
    void aGhostSweepWhoseCommitIsLostEmptiesTheTenantsCache() {
        String tenant = "row15-sweep-tenant";
        String ghost = label("lostcommit-ghost-" + (n++));
        fx.asSuperuser(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, ghost);   // registered, holds no chunk: a ghost
            return null;
        });
        cache(tenant, ghost);

        lostCommit(() -> repo.sweepGhostsAndMarkDormant(tenant, false));

        assertThat(CollectionRegistry.cached(tenant, ghost)).as("the swept (deleted) ghost leaves the cache").isEmpty();
    }
}
