// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.db.CollectionKey;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25), ruling 4 as linda corrected it: {@code CollectionIds.idOf} (both shapes), {@code require} and
 * {@link CollectionKey#in}/{@link CollectionKey#startsWith} resolve a label to its LIVE registry row first
 * ({@code superseded_by = '' AND superseded_by_id IS NULL}) and otherwise to its newest tombstone. This test builds the state a
 * P3.6 install can reach (the name keys dropped, a tombstone and a live row sharing a label) in a container of its own, so no other
 * test runs without those keys.
 */
class CollectionIdsLiveRowTest {

    private static final String TENANT = "p35c-live-row";
    private static final String LABEL = "code__p35clive__minilm-l6-v2-384__v1";

    private static PostgreSQLContainer<?> pg;

    @BeforeAll
    static void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
    }

    @AfterAll
    static void stop() {
        if (pg != null) pg.stop();
    }

    private static Long tombstone(DSLContext ctx, String label, long successorId, Long templateId) {
        var row = ctx.selectFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.COLLECTION_ID.eq(templateId)).fetchOne().copy();
        row.setTenantId(TENANT);
        row.setName(label);
        row.setSupersededBy("successor-" + label);
        row.setSupersededById(successorId);
        row.changed(true);
        row.changed(CATALOG_COLLECTIONS.COLLECTION_ID, false);
        ctx.executeInsert(row);
        return ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(label))
            .and(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID.isNotNull())
            .orderBy(CATALOG_COLLECTIONS.COLLECTION_ID.desc()).limit(1).fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
    }

    /**
     * linda's corrected Q3: no resolver filters strictly to live. A label resolves to its LIVE row first and otherwise to its newest
     * tombstone, so a writer that legitimately files rows under a retired collection keeps working, and two rows sharing a label (the
     * state of a P3.6 install, built here by dropping the name keys) never raise 21000.
     */
    @Test
    void idOfAndRequirePreferTheLiveRowThenTheNewestTombstone() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT, LABEL);
            Long liveId = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(LABEL))
                .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);

            assertThat(ctx.select(CollectionIds.idOf(TENANT, "gone-label")).fetchOne(0)).as("unregistered: NULL").isNull();
            assertThatThrownBy(() -> CollectionIds.require(ctx, TENANT, "gone-label"))
                .isInstanceOf(dev.nexus.service.db.UnregisteredCollectionException.class);

            // the state of a P3.6 install: the name keys are gone, so tombstones can carry a label
            for (var key : CATALOG_COLLECTIONS.getKeys()) {
                if (key.getFields().contains(CATALOG_COLLECTIONS.NAME)) {
                    ctx.alterTable(CATALOG_COLLECTIONS).dropConstraint(DSL.constraint(key.getName())).cascade().execute();
                }
            }

            // a tombstone beside the live row, created LATER (the larger id): the live row still wins
            Long tomb = tombstone(ctx, LABEL, liveId, liveId);
            assertThat(tomb).isGreaterThan(liveId);
            assertThat(ctx.select(CollectionIds.idOf(TENANT, LABEL)).fetchOne(0)).as("live first, though a tombstone is newer").isEqualTo(liveId);
            var t = CATALOG_COLLECTIONS.as("t");
            assertThat(ctx.select(CollectionIds.idOf(t.TENANT_ID, t.NAME)).from(t).where(t.COLLECTION_ID.eq(tomb)).fetchOne(0))
                .as("the column shape too").isEqualTo(liveId);
            assertThat(CollectionIds.require(ctx, TENANT, LABEL)).isEqualTo(liveId);

            // a label held only by tombstones names its NEWEST tombstone: idOf and require both
            String retired = "code__p35cretired__minilm-l6-v2-384__v1";
            Long first = tombstone(ctx, retired, liveId, liveId);
            Long newest = tombstone(ctx, retired, liveId, liveId);
            assertThat(newest).isGreaterThan(first);
            assertThat(ctx.select(CollectionIds.idOf(TENANT, retired)).fetchOne(0)).isEqualTo(newest);
            assertThat(CollectionIds.require(ctx, TENANT, retired)).as("a retired label still names its row").isEqualTo(newest);

            // CollectionKey follows the same ordering: a shared label names its live row, a retired label its newest tombstone
            var byId = CollectionKey.of(() -> true);
            List<Long> ids = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)
                    .and(byId.in(TENANT, CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.COLLECTION_ID, List.of(LABEL, retired))))
                .orderBy(CATALOG_COLLECTIONS.COLLECTION_ID).fetch(CATALOG_COLLECTIONS.COLLECTION_ID);
            assertThat(ids).as("in(): one row per label").containsExactlyInAnyOrder(liveId, newest);
            List<Long> pre = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)
                    .and(byId.startsWith(TENANT, CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.COLLECTION_ID, "code__p35c")))
                .fetch(CATALOG_COLLECTIONS.COLLECTION_ID);
            assertThat(pre).as("startsWith(): one row per label too").containsExactlyInAnyOrder(liveId, newest);
        }
    }
}
