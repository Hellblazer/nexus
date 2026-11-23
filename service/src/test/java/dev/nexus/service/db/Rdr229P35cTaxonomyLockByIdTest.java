// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import dev.nexus.service.AtomicWriteTestBase;
import dev.nexus.service.RegistryRenameSupport;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * RDR-229 P3.5c (linda 2026-11-23, census L1): the taxonomy persist lock is keyed by the registry row's id. After a one-row rename a persist
 * through the old label and one through the new label are the same collection and serialize; an unrelated collection does not wait.
 */
class Rdr229P35cTaxonomyLockByIdTest extends AtomicWriteTestBase {

    @BeforeAll
    void dropTheNameKeys() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            RegistryRenameSupport.dropForeignKeysOntoTheRegistry(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    @Test
    void twoPersistsOnOneCollectionThroughItsOldAndNewLabel_serialize() throws Exception {
        String oldLabel = "code__p35clock" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        String newLabel = "code__p35clock" + seq.incrementAndGet() + "new__minilm-l6-v2-384__v1";
        String unrelated = "code__p35clock" + seq.incrementAndGet() + "u__minilm-l6-v2-384__v1";
        registerCollection(oldLabel);
        registerCollection(unrelated);
        CountDownLatch held = new CountDownLatch(1), release = new CountDownLatch(1);
        CompletableFuture<Void> holder = CompletableFuture.runAsync(() -> tenantScope.withTenant(TENANT, ctx -> {
            TaxonomyCollectionLock.lockTaxonomyCollection(ctx, TENANT, oldLabel);
            held.countDown();
            try {
                release.await(30, TimeUnit.SECONDS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            return null;
        }));
        try {
            assertThat(held.await(20, TimeUnit.SECONDS)).as("the first persist holds the lock").isTrue();
            // the one-row rename happens while the first persist holds the lock: the label it took no longer names the row
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                RegistryRenameSupport.rename(DSL.using(su, SQLDialect.POSTGRES), TENANT, oldLabel, newLabel);   // the one-row rename: the id stays
            }

            tenantScope.withTenant(TENANT, ctx -> {
                TaxonomyCollectionLock.lockTaxonomyCollection(ctx, TENANT, unrelated);
                return null;
            });   // an unrelated collection takes its lock at once

            assertThatThrownBy(() -> tenantScope.withTenant(TENANT, ctx -> {
                TaxonomyCollectionLock.lockTaxonomyCollection(ctx, TENANT, newLabel);
                return null;
            })).as("the same collection under its new label waits for the first, and times out (lock timeout) while it is held")
                .hasStackTraceContaining("lock timeout");
        } finally {
            release.countDown();
            holder.get(30, TimeUnit.SECONDS);
        }
    }
}
