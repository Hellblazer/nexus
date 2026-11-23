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
 * RDR-229 P3.5c (nexus-p2m1x.25), ruling 4: {@code CollectionIds.idOf} (and {@link CollectionKey#in}) pick the LIVE registry row
 * of a label, {@code superseded_by = '' AND superseded_by_id IS NULL}. Once P3.6's live-label index replaces the name primary key a
 * tombstone and a live row can share a label, and a bare {@code WHERE name = ?} scalar subselect then raises 21000. This test builds
 * that state (the name key dropped, which only a P3.6 install has) in a container of its own, so no other test runs without it.
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

    @Test
    void idOfPicksTheLiveRowWhenATombstoneSharesTheLabel() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT, LABEL);
            Long liveId = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(LABEL))
                .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);

            // a tombstone-only label names no collection
            assertThat(ctx.select(CollectionIds.idOf(TENANT, "gone-label")).fetchOne(0)).isNull();

            // the state of a P3.6 install: the name key is gone, so a tombstone row can carry the live row's label
            // every key that holds the label unique: the primary key and the (tenant, name, model) unique key the child FKs use
            for (var key : CATALOG_COLLECTIONS.getKeys()) {
                if (key.getFields().contains(CATALOG_COLLECTIONS.NAME)) {
                    ctx.alterTable(CATALOG_COLLECTIONS).dropConstraint(DSL.constraint(key.getName())).cascade().execute();
                }
            }
            var tomb = ctx.selectFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT))
                .and(CATALOG_COLLECTIONS.NAME.eq(LABEL)).fetchOne().copy();
            tomb.setSupersededBy("successor-" + LABEL);
            tomb.setSupersededById(liveId);
            tomb.setTenantId(TENANT);
            tomb.setName(LABEL);
            tomb.changed(true);
            tomb.changed(CATALOG_COLLECTIONS.COLLECTION_ID, false);
            ctx.executeInsert(tomb);
            assertThat(ctx.fetchCount(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)
                .and(CATALOG_COLLECTIONS.NAME.eq(LABEL)))).as("two rows carry the label").isEqualTo(2);

            assertThat(ctx.select(CollectionIds.idOf(TENANT, LABEL)).fetchOne(0)).as("the live row's id").isEqualTo(liveId);
            var t = CATALOG_COLLECTIONS.as("t");
            assertThat(ctx.select(CollectionIds.idOf(t.TENANT_ID, t.NAME)).from(t).where(t.TENANT_ID.eq(TENANT))
                .and(t.SUPERSEDED_BY.eq("")).fetchOne(0)).as("the column shape, over the live row").isEqualTo(liveId);
            assertThat(CollectionIds.require(ctx, TENANT, LABEL)).isEqualTo(liveId);

            // a label held only by tombstones names no collection: idOf is NULL and require refuses it
            String retired = "code__p35cretired__minilm-l6-v2-384__v1";
            var only = ctx.selectFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT))
                .and(CATALOG_COLLECTIONS.NAME.eq(LABEL)).and(CATALOG_COLLECTIONS.SUPERSEDED_BY.eq("")).fetchOne().copy();
            only.setTenantId(TENANT);
            only.setName(retired);
            only.setSupersededBy("successor-" + retired);
            only.setSupersededById(liveId);
            only.changed(true);
            only.changed(CATALOG_COLLECTIONS.COLLECTION_ID, false);
            ctx.executeInsert(only);
            assertThat(ctx.select(CollectionIds.idOf(TENANT, retired)).fetchOne(0)).as("tombstone-only: no collection").isNull();
            assertThatThrownBy(() -> CollectionIds.require(ctx, TENANT, retired))
                .isInstanceOf(dev.nexus.service.db.UnregisteredCollectionException.class);

            // the registry-label reads that go through CollectionKey take the live row too
            var byId = CollectionKey.of(() -> true);
            assertThat(ctx.fetchCount(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)
                .and(byId.in(TENANT, CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.COLLECTION_ID, List.of(LABEL)))))
                .as("in(): the ids of the live rows only, so the live row alone").isEqualTo(1);
        }
    }
}
