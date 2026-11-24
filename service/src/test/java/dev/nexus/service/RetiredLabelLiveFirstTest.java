// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.RetiredLabelReadersTest.Reader;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;

import java.sql.Connection;
import java.util.stream.Stream;

import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 R7 (linda's Q3, live first): a label held by BOTH a retired row with no successor and a live row resolves to the live row, so a
 * read of it on a refused route is answered from the live row and never refused. Two orders, because the registry's tie-break must not be
 * the newest id: the tombstone older than the live row (a label registered again after its old row was retired), and the tombstone NEWER
 * (a retired row that took the label of an older live one). The harness drops the registry's name unique keys, as
 * {@code Rdr229P35cSharedLabelAndHistoryTest} does (P4.1 drops them for real; until then a tombstone and a live row cannot share a label).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class RetiredLabelLiveFirstTest {

    CollectionRouteFixture fx;
    int n;

    @BeforeAll
    void startAll() throws Exception {
        fx = new CollectionRouteFixture();
        try (Connection su = fx.pg.createConnection("")) {
            su.setAutoCommit(true);
            RegistryRenameSupport.dropForeignKeysOntoTheRegistry(DSL.using(su, org.jooq.SQLDialect.POSTGRES));
            PgContainerHelper.dropConstraint(su, CATALOG_COLLECTIONS, "catalog_collections_pk");
            PgContainerHelper.dropConstraint(su, CATALOG_COLLECTIONS, "catalog_collections_tenant_name_model_uq");
        }
    }

    @AfterAll
    void stopAll() {
        if (fx != null) fx.close();
    }

    static Stream<Arguments> readersAndOrders() {
        return RetiredLabelReadersTest.readers().flatMap(a -> Stream.of(Arguments.of(a.get()[0], true), Arguments.of(a.get()[0], false)));
    }

    /** Retires row {@code id} with no successor: its own id in {@code superseded_by_id}, a dead label in {@code superseded_by}. */
    private void retireNoSuccessor(long id, String dead) {
        fx.asSuperuser(ctx -> ctx.update(CATALOG_COLLECTIONS)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY, dead).set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, CATALOG_COLLECTIONS.COLLECTION_ID)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(id)).execute());
    }

    @ParameterizedTest(name = "{0}, tombstone older: {1}")
    @MethodSource("readersAndOrders")
    void aLabelHeldByATombstoneAndALiveRow_readsTheLiveRow_inEitherOrderOfIds(Reader reader, boolean tombstoneOlder) {
        String label = label("r7-lf-" + (n++));
        String dead = label("r7-lf-dead-" + (n++));
        long tombstoneId;
        if (tombstoneOlder) {
            fx.register(label);
            tombstoneId = fx.idOf(label);
            retireNoSuccessor(tombstoneId, dead);
            fx.register(label);                       // the label is registered again: a NEW live row, the higher id
        } else {
            fx.register(label);                       // the live row first, the lower id
            String other = label("r7-lf-other-" + (n++));
            fx.register(other);
            tombstoneId = fx.idOf(other);
            retireNoSuccessor(tombstoneId, dead);
            fx.asSuperuser(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, label)   // the retired row takes the label: the higher id
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(tombstoneId)).execute());
        }
        Long rows = fx.asSuperuser(ctx -> (long) ctx.fetchCount(CATALOG_COLLECTIONS,
            CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant).and(CATALOG_COLLECTIONS.NAME.eq(label))));
        assertThat(rows).as("setup: the label is held by two rows").isEqualTo(2L);
        Long liveId = fx.asSuperuser(ctx -> ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.NAME.eq(label))
            .and(CATALOG_COLLECTIONS.SUPERSEDED_BY.eq("")).fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID));
        assertThat(liveId).as("setup: exactly one is live, and the tombstone is " + (tombstoneOlder ? "older" : "newer")).isNotEqualTo(tombstoneId);
        assertThat(tombstoneOlder ? liveId > tombstoneId : liveId < tombstoneId).as("setup: the id order").isTrue();

        Resp r = reader.post() ? fx.post(reader.path(), reader.byLabel(label)) : fx.get(reader.path() + "?" + reader.byLabel(label));

        assertThat(r.reason()).as("the live row answers, the label is not refused: %s", r.body()).isNotEqualTo("collection_superseded");
        assertThat(r.status()).as(r.body()).isEqualTo(reader.reads());
    }
}
