// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.Optional;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23), linda's corrected Q3 ruling of 2026-11-23: no resolver filters strictly to live in Phase 3. A
 * label held by several rows resolves to the live row first, then to the NEWEST tombstone
 * ({@link CollectionRegistry#labelResolutionOrder()}), and every label read that must pick one row uses that one order:
 * {@link CollectionRegistry#resolveLabel}, {@link CollectionRegistry#lifecycleState} and {@link CollectionPurgeScope#resolve}.
 * "Live" is "not superseded", never {@code lifecycle_state = 'live'}: a quarantined row that names no successor still holds its
 * label.
 *
 * <p>Before P4.1 the registry's name keys keep a label to one row, so this class makes the case on its own throwaway database:
 * as the superuser it drops every unique key that holds {@code name} (as {@code CollectionDeleteScopeByIdTest} drops the primary
 * key) and inserts the rows directly. The ids run oldest to newest: a tombstone, the live row, a newer tombstone. The live row is
 * in the middle on purpose: an order that is only "highest id" picks the newer tombstone, one that is live first and then "lowest
 * id" picks the older one once the live row is gone, and neither passes both states below.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionLabelResolutionOrderTest {

    private static final String TENANT = "label-order";
    private static final String LABEL = "docs__label-order__minilm-l6-v2-384__v1";
    private static final String SUCCESSOR = "docs__label-order-succ__minilm-l6-v2-384__v1";

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private TenantScope scope;

    @BeforeAll
    void start() throws Exception {
        CollectionRegistry.shared().evictTenant(TENANT);
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), "tok-label-order-0123456789abcdef", TENANT,
                "label-order");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(2);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        scope = new TenantScope(svcDs);
        asSuperuser(ctx -> {
            for (var key : CATALOG_COLLECTIONS.getKeys()) {
                if (key.getFields().contains(CATALOG_COLLECTIONS.NAME)) {
                    ctx.alterTable(CATALOG_COLLECTIONS).dropConstraint(key.getName()).cascade().execute();
                }
            }
            // two live rows under one label (the AMBIGUOUS case) need the live-label index gone as well; jOOQ renders the DROP INDEX
            // unqualified, so the connection is pointed at nexus first
            ctx.setSchema("nexus").execute();
            ctx.dropIndexIfExists(org.jooq.impl.DSL.name("nexus", "idx_catalog_collections_live_label")).execute();
            successorId = insert(ctx, SUCCESSOR, "label-order-succ", "live", false);
            return null;
        });
    }

    @AfterAll
    void stop() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private <T> T asSuperuser(java.util.function.Function<DSLContext, T> work) {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            return work.apply(DSL.using(su, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    /** The registry id of {@link #SUCCESSOR}, set by {@link #start}: every tombstone names it by label and by id (the P3.1 CHECK). */
    private long successorId;

    private long insert(DSLContext ctx, String name, String owner, String lifecycle, boolean tombstone) {
        return ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL,
                CATALOG_COLLECTIONS.LIFECYCLE_STATE, CATALOG_COLLECTIONS.SUPERSEDED_BY, CATALOG_COLLECTIONS.SUPERSEDED_BY_ID)
            .values(TENANT, name, "docs", owner, "minilm-l6-v2-384", lifecycle, tombstone ? SUCCESSOR : "",
                tombstone ? successorId : null)
            .returning(CATALOG_COLLECTIONS.COLLECTION_ID).fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
    }

    private record Resolved(long registryId, Optional<String> lifecycle, Long scopeId) {
    }

    /** What each of the three resolvers answers for {@link #LABEL}, each on an empty cache. */
    private Resolved resolveAll() {
        CollectionRegistry.shared().evictTenant(TENANT);
        long registryId = scope.withTenant(TENANT, ctx -> new CollectionRegistry().resolveLabel(ctx, TENANT, LABEL)).collectionId();
        Optional<String> lifecycle = CollectionRegistry.lifecycleState(scope, TENANT, LABEL);
        Long scopeId = scope.withTenant(TENANT, ctx -> CollectionPurgeScope.resolve(ctx, true, TENANT, LABEL, null).id());
        return new Resolved(registryId, lifecycle, scopeId);
    }

    @Test
    void aLabelHeldByALiveRowAndTwoTombstonesResolvesToTheLiveRow_thenToTheNewestTombstone() {
        long[] ids = asSuperuser(ctx -> {
            long older = insert(ctx, LABEL, "label-order-a", "dormant", true);
            long live = insert(ctx, LABEL, "label-order-b", "live", false);
            long newer = insert(ctx, LABEL, "label-order-c", "dormant", true);
            return new long[] {older, live, newer};
        });
        long older = ids[0];
        long live = ids[1];
        long newer = ids[2];
        assertThat(older).as("setup: the ids run oldest to newest").isLessThan(live);
        assertThat(live).isLessThan(newer);

        // state 1: the live row beats both tombstones, though the newer tombstone has the highest id
        Resolved first = resolveAll();
        CollectionDeletes.Verdict deleteOfLabel = CollectionDeletes.verdict(scope, TENANT, LABEL, null);
        assertThat(deleteOfLabel.kind()).isEqualTo(CollectionDeletes.Kind.PROCEED);
        assertThat(deleteOfLabel.collectionId()).as("a delete by the label acts on the live row").isEqualTo(live);
        // the older tombstone asked for by its ID is refused, naming its successor by id (a row retired since the boundary read it)
        CollectionDeletes.Verdict deleteOfRetiredId = CollectionDeletes.verdict(scope, TENANT, LABEL, older);
        assertThat(deleteOfRetiredId.kind()).isEqualTo(CollectionDeletes.Kind.SUPERSEDED);
        assertThat(deleteOfRetiredId.successor()).isEqualTo(new CollectionSuccessor.Successor(successorId, SUCCESSOR));
        assertThat(first.registryId()).as("resolveLabel picks the live row").isEqualTo(live);
        assertThat(first.lifecycle()).as("lifecycleState reads the live row's").contains("live");
        assertThat(first.scopeId()).as("the purge scope's row is the live row").isEqualTo(live);

        // state 2: the live row is gone; the newest tombstone wins, not the oldest
        asSuperuser(ctx -> {
            ctx.deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.COLLECTION_ID.eq(live)).execute();
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, "live")
                .where(CATALOG_COLLECTIONS.COLLECTION_ID.eq(newer)).execute();
            return null;
        });
        Resolved second = resolveAll();
        assertThat(second.registryId()).as("resolveLabel picks the newest tombstone").isEqualTo(newer);
        assertThat(second.lifecycle()).as("lifecycleState reads the newest tombstone's, not the older one's (dormant)")
            .contains("live");
        assertThat(second.scopeId()).as("the purge scope's row is the newest tombstone").isEqualTo(newer);
        assertThat(older).as("the older tombstone was never the answer").isNotEqualTo(second.registryId());
        CollectionDeletes.Verdict deleteOfLabel2 = CollectionDeletes.verdict(scope, TENANT, LABEL, null);
        assertThat(deleteOfLabel2.kind()).isEqualTo(CollectionDeletes.Kind.PROCEED);
        assertThat(deleteOfLabel2.collectionId()).as("with no live row a delete by the label acts on the newest tombstone")
            .isEqualTo(newer);
    }

    /** Two live rows under one label is the one state the delete by label refuses, naming both ids. */
    @Test
    void twoLiveRowsUnderOneLabelAreAmbiguousForADeleteByLabel() {
        String label = "docs__label-order-amb__minilm-l6-v2-384__v1";
        long[] ids = asSuperuser(ctx -> new long[] {insert(ctx, label, "label-order-amb-a", "live", false),
            insert(ctx, label, "label-order-amb-b", "live", false)});
        CollectionDeletes.Verdict v = CollectionDeletes.verdict(scope, TENANT, label, null);
        assertThat(v.kind()).isEqualTo(CollectionDeletes.Kind.AMBIGUOUS);
        assertThat(v.ids()).containsExactlyInAnyOrder(ids);
        // the id path is the way out, and reaches exactly one of them
        CollectionDeletes.Verdict byId = CollectionDeletes.verdict(scope, TENANT, label, ids[0]);
        assertThat(byId.kind()).isEqualTo(CollectionDeletes.Kind.PROCEED);
        assertThat(byId.collectionId()).isEqualTo(ids[0]);
    }

    // ---- CollectionSuccessor: a tombstone names its successor by id ------------------------------------------------------------------

    /** Points the live row {@code id} at {@code toId}: a tombstone naming that row, by label text and by id. */
    private void retire(DSLContext ctx, long id, String text, long toId) {
        ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, text).set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, toId)
            .where(CATALOG_COLLECTIONS.COLLECTION_ID.eq(id)).execute();
    }

    private Optional<CollectionSuccessor.Successor> successorOf(long id) {
        return CollectionSuccessor.of(scope, TENANT, id);
    }

    /**
     * X superseded by Y; Y deleted, which sets X.superseded_by_id to X and leaves the text; Y's label registered again as a new
     * row. X names no successor, though a live row now bears the text: the label is not the identity.
     */
    @Test
    void aDeletedSuccessorWhoseLabelIsTakenAgainIsNotTheSuccessor() {
        String yLabel = "docs__label-order-y__minilm-l6-v2-384__v1";
        long[] ids = asSuperuser(ctx -> {
            long x = insert(ctx, "docs__label-order-x__minilm-l6-v2-384__v1", "label-order-x", "live", false);
            long reregistered = insert(ctx, yLabel, "label-order-y2", "live", false);   // the new row that bears Y's text
            retire(ctx, x, yLabel, x);   // what the delete of the old Y left: the text, and the id of the tombstone itself
            return new long[] {x, reregistered};
        });
        assertThat(successorOf(ids[0])).as("a self-referencing id names no successor, whatever bears the text").isEmpty();
    }

    /** A chain A to B to C whose first tombstone was never re-pointed: A's id names C, by C's current label. */
    @Test
    void aChainIsFollowedByIdToTheLiveEnd_andTheLabelIsTheCurrentOne() {
        long[] ids = asSuperuser(ctx -> {
            long a = insert(ctx, "docs__label-order-ca__minilm-l6-v2-384__v1", "label-order-ca", "live", false);
            long b = insert(ctx, "docs__label-order-cb__minilm-l6-v2-384__v1", "label-order-cb", "live", false);
            long c = insert(ctx, "docs__label-order-cc__minilm-l6-v2-384__v1", "label-order-cc", "live", false);
            retire(ctx, b, "docs__label-order-cc__minilm-l6-v2-384__v1", c);
            retire(ctx, a, "docs__label-order-cb__minilm-l6-v2-384__v1", b);
            return new long[] {a, b, c};
        });
        assertThat(successorOf(ids[0])).contains(
            new CollectionSuccessor.Successor(ids[2], "docs__label-order-cc__minilm-l6-v2-384__v1"));
        assertThat(successorOf(ids[1])).contains(
            new CollectionSuccessor.Successor(ids[2], "docs__label-order-cc__minilm-l6-v2-384__v1"));
        // the live end is renamed in place (P3.7's rename): the answer is its label now, not the text the tombstones carry
        asSuperuser(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, "docs__label-order-cc-new__minilm-l6-v2-384__v1")
            .where(CATALOG_COLLECTIONS.COLLECTION_ID.eq(ids[2])).execute());
        assertThat(successorOf(ids[0])).contains(
            new CollectionSuccessor.Successor(ids[2], "docs__label-order-cc-new__minilm-l6-v2-384__v1"));
    }

    @Test
    void aCycleAndAChainPastTheDepthCapNameNoSuccessor_aChainWithinItDoes() {
        long[] cycle = asSuperuser(ctx -> {
            long a = insert(ctx, "docs__label-order-cy-a__minilm-l6-v2-384__v1", "label-order-cy-a", "live", false);
            long b = insert(ctx, "docs__label-order-cy-b__minilm-l6-v2-384__v1", "label-order-cy-b", "live", false);
            retire(ctx, a, "b", b);
            retire(ctx, b, "a", a);
            return new long[] {a, b};
        });
        assertThat(successorOf(cycle[0])).as("a cycle").isEmpty();

        for (int length : new int[] {CollectionSuccessor.MAX_HOPS - 1, CollectionSuccessor.MAX_HOPS + 8}) {
            long[] chain = asSuperuser(ctx -> {
                long[] rows = new long[length + 1];
                for (int i = 0; i <= length; i++) {
                    rows[i] = insert(ctx, "docs__label-order-ch" + length + "-" + i + "__minilm-l6-v2-384__v1", "label-order-ch" + length + "-" + i,
                        "live", false);
                }
                for (int i = 0; i < length; i++) {
                    retire(ctx, rows[i], "next", rows[i + 1]);
                }
                return rows;
            });
            if (length < CollectionSuccessor.MAX_HOPS) {
                assertThat(successorOf(chain[0])).as("a chain of %d hops", length).map(CollectionSuccessor.Successor::id)
                    .contains(chain[length]);
            } else {
                assertThat(successorOf(chain[0])).as("a chain of %d hops is past the cap", length).isEmpty();
            }
        }
    }

    /** A row that names no successor is live whatever its lifecycle: a quarantined row still holds its label (linda/elsinore). */
    @Test
    void aQuarantinedRowThatIsNotSupersededIsLiveForTheOrder() {
        String label = "docs__label-order-q__minilm-l6-v2-384__v1";
        long[] ids = asSuperuser(ctx -> {
            long tomb = insert(ctx, label, "label-order-qa", "live", true);
            long quarantined = insert(ctx, label, "label-order-qb", "quarantine", false);
            return new long[] {tomb, quarantined};
        });
        CollectionRegistry.shared().evictTenant(TENANT);
        Long scopeId = scope.withTenant(TENANT, ctx -> CollectionPurgeScope.resolve(ctx, true, TENANT, label, null).id());
        assertThat(scopeId).as("the quarantined, not-superseded row beats the tombstone").isEqualTo(ids[1]);
        assertThat(scope.withTenant(TENANT, ctx -> new CollectionRegistry().resolveLabel(ctx, TENANT, label)).collectionId())
            .isEqualTo(ids[1]);
    }
}
