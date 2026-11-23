// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.RegistryRenameSupport;
import dev.nexus.service.datamove.Gate3;
import org.jooq.DSLContext;
import org.jooq.Record3;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * RDR-229 G15 (nexus-p2m1x.25, linda 2026-11-23): the registry's label upsert in {@code CollectionRename#writeSuccessor}
 * arbitrates on the LIVE-label key, {@code ON CONFLICT (tenant_id, name) WHERE superseded_by = '' AND superseded_by_id IS NULL}.
 * That predicate infers the live-label partial unique index (catalog-053-1), so a label held only by a tombstone is no longer a
 * conflict: the write inserts a new live row with its own id, where it used to revive the tombstone and keep its id.
 *
 * <p>Until P4.1 the name is still the primary key, a second arbiter beside the index, and a retired label still conflicts on it
 * and is revived. The first test is that state (the behaviour today, the guard); the second drops the key as P4.1 will (P3.7 drops
 * only the foreign keys onto the name; P4.1 drops the primary key and the (tenant, name, model) unique) and requires the new row. The order is fixed, since the second changes the schema for the rest of the class.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class Rdr229P35cCollectionRenameReviveTest {

    private static final String TENANT = "g15-revive-tenant";
    private static final String MODEL_TOKEN = "minilm-l6-v2-384";

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
    }

    @AfterAll
    void stopAll() {
        if (pg != null) pg.stop();
    }

    private static String label(String tag) {
        return "code__" + tag + "__" + MODEL_TOKEN + "__v1";
    }

    /** X live, and L a tombstone (a past rename's debris: it names a successor that is gone, so its own id, R7). */
    private static void seedLiveAndTombstone(DSLContext dsl, String live, String tombstone) {
        PgContainerHelper.insertCollection(dsl, TENANT, live);
        PgContainerHelper.insertCollection(dsl, TENANT, tombstone);
        Long id = dsl.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(tombstone))
            .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
        dsl.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, "long-gone")
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, id)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(tombstone)).execute();
    }

    /** The rows of {@code name}: (collection_id, superseded_by, superseded_by_id), tombstone first. */
    private static List<Record3<Long, String, Long>> rows(DSLContext dsl, String name) {
        return dsl.select(CATALOG_COLLECTIONS.COLLECTION_ID, CATALOG_COLLECTIONS.SUPERSEDED_BY, CATALOG_COLLECTIONS.SUPERSEDED_BY_ID)
            .from(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .orderBy(CATALOG_COLLECTIONS.SUPERSEDED_BY.desc()).fetch();
    }

    private static Map<String, Integer> retire(DSLContext dsl, String from, String to) {
        Map<String, Integer> counts = new LinkedHashMap<>();
        CollectionRename.retireAndRegister(dsl, TENANT, from, to, null, CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
            counts, new String[5], Gate3.never());
        return counts;
    }

    /** Drops the foreign keys onto the registry and then its name primary key and the (tenant, name, model) unique, as P4.1 will. */
    private static void dropNameKey(Connection su, DSLContext dsl) throws Exception {
        RegistryRenameSupport.dropForeignKeysOntoTheRegistry(dsl);
        PgContainerHelper.dropConstraint(su, CATALOG_COLLECTIONS, "catalog_collections_pk");
        PgContainerHelper.dropConstraint(su, CATALOG_COLLECTIONS, "catalog_collections_tenant_name_model_uq");
    }

    @Test
    @Order(1)
    void whileTheNameKeyExistsARetiredLabelStillConflictsAndIsRevivedKeepingItsId() throws Exception {
        String from = label("rvx1"), retired = label("rvt1");
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(CollectionMint.nameKeyExists(dsl)).as("the guard's premise: the name primary key exists").isTrue();
            seedLiveAndTombstone(dsl, from, retired);
            long tombstoneId = rows(dsl, retired).get(0).value1();

            Map<String, Integer> counts = retire(dsl, from, retired);

            var after = rows(dsl, retired);
            assertThat(after).as("one row holds the label: the tombstone is revived, as today").hasSize(1);
            assertThat(after.get(0).value1()).as("and it keeps its id").isEqualTo(tombstoneId);
            assertThat(after.get(0).value2()).as("live again").isEmpty();
            assertThat(after.get(0).value3()).isNull();
            assertThat(counts.get("catalog_collections_inserted")).isEqualTo(1);
            assertThat(rows(dsl, from).get(0).value3()).as("the source is retired onto the revived row's id").isEqualTo(tombstoneId);
        }
    }

    @Test
    @Order(2)
    void onceTheNameKeyIsGoneALabelHeldOnlyByATombstoneIsNoConflictAndTheWriteInsertsANewLiveRow() throws Exception {
        String from = label("rvx2"), retired = label("rvt2");
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            dropNameKey(su, dsl);
            assertThat(CollectionMint.nameKeyExists(dsl)).as("the name primary key is gone").isFalse();
            seedLiveAndTombstone(dsl, from, retired);
            long tombstoneId = rows(dsl, retired).get(0).value1();

            Map<String, Integer> counts = retire(dsl, from, retired);

            var after = rows(dsl, retired);
            assertThat(after).as("two rows hold the label: the tombstone stands, a new live row is written").hasSize(2);
            var tombstone = after.get(0);
            var live = after.get(1);
            assertThat(tombstone.value1()).as("the tombstone is untouched").isEqualTo(tombstoneId);
            assertThat(tombstone.value2()).isEqualTo("long-gone");
            assertThat(live.value2()).as("the new row is live").isEmpty();
            assertThat(live.value3()).isNull();
            assertThat(live.value1()).as("with an id of its own, not the tombstone's").isNotEqualTo(tombstoneId);
            assertThat(counts.get("catalog_collections_inserted")).isEqualTo(1);
            assertThat(rows(dsl, from).get(0).value3()).as("the source is retired onto the NEW row's id").isEqualTo(live.value1());
        }
    }
}
