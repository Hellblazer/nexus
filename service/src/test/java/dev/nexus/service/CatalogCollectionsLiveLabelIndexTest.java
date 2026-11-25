// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.DSLContext;
import org.jooq.Record3;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.SQLException;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.6 (nexus-p2m1x.26): a label names exactly one LIVE collection per tenant, held by the partial UNIQUE
 * index {@code idx_catalog_collections_live_label} on {@code nexus.catalog_collections (tenant_id, name)} over the
 * rows that are not superseded (catalog-053).
 *
 * <p><b>This test stays in the suite through P4.1</b> (RDR-229 P3.6). Until P4.1 the name is also the primary key
 * ({@code catalog_collections_pk}), so a second live row with one label is refused by the key whether or not the
 * index exists; the first test therefore asserts the refusal AND that the index is present, and the second drops
 * the key (IF EXISTS, so it is a no-op from P4.1, when the key moves to the id) to show the index alone refuses a
 * second live row and lets a superseded row and a live one share a label, which the key never could. P4.1
 * re-creates the index over {@code superseded_by_id IS NULL} in the same changeset as the key swap and changes the
 * {@code SUPERSEDED} predicate below with it.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogCollectionsLiveLabelIndexTest {

    private static final String INDEX = "idx_catalog_collections_live_label";
    private static final String TENANT = "live-label-tenant";

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

    private static void insert(DSLContext dsl, String tenant, String label, String supersededBy) {
        insert(dsl, tenant, label, supersededBy, "bge-base-en-v15-768");
    }

    /** The model varies where a row must not also collide on the (tenant_id, name, embedding_model) UNIQUE of vectors-030. */
    private static void insert(DSLContext dsl, String tenant, String label, String supersededBy, String model) {
        // superseded_by_id: RDR-229 P3.1's CHECK (superseded_by = '') = (superseded_by_id IS NULL); any id stands for
        // the successor here, since this test reads only the label index.
        dsl.insertInto(CATALOG_COLLECTIONS)
            .set(CATALOG_COLLECTIONS.TENANT_ID, tenant).set(CATALOG_COLLECTIONS.NAME, label)
            .set(CATALOG_COLLECTIONS.CONTENT_TYPE, "knowledge").set(CATALOG_COLLECTIONS.OWNER_ID, "label-notes")
            .set(CATALOG_COLLECTIONS.EMBEDDING_MODEL, model).set(CATALOG_COLLECTIONS.MODEL_VERSION, "v1")
            .set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, "live").set(CATALOG_COLLECTIONS.SUPERSEDED_BY, supersededBy)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, supersededBy.isEmpty() ? null : 1L)
            .execute();
    }

    /** {@code pg_get_indexdef} of the live-label index, or null when it is absent. */
    private static String indexDefinition(DSLContext dsl) {
        var oid = DSL.function("to_regclass", Object.class, DSL.val("nexus." + INDEX));
        return dsl.select(DSL.function("pg_get_indexdef", String.class, oid)).fetchOne(0, String.class);
    }

    /**
     * Drops the (tenant_id, name) primary key through the typed helper. ddl_utils' drop does not cascade, so the
     * foreign keys that target the key (topic_assignments_collection_fk and others) go first, each by name from
     * pg_constraint; this scratch clone needs none of them.
     */
    private static void dropNameKey(Connection su, DSLContext dsl) throws SQLException {
        var con = DSL.table(DSL.name("pg_catalog", "pg_constraint"));
        var cls = DSL.table(DSL.name("pg_catalog", "pg_class"));
        var nsp = DSL.table(DSL.name("pg_catalog", "pg_namespace"));
        var target = DSL.function("to_regclass", Object.class, DSL.val("nexus.catalog_collections"));
        for (Record3<String, String, String> fk : dsl
                .select(DSL.field(DSL.name("pg_namespace", "nspname"), String.class),
                        DSL.field(DSL.name("pg_class", "relname"), String.class),
                        DSL.field(DSL.name("pg_constraint", "conname"), String.class))
                .from(con)
                .join(cls).on(DSL.field(DSL.name("pg_class", "oid")).eq(DSL.field(DSL.name("pg_constraint", "conrelid"))))
                .join(nsp).on(DSL.field(DSL.name("pg_namespace", "oid")).eq(DSL.field(DSL.name("pg_class", "relnamespace"))))
                .where(DSL.field(DSL.name("pg_constraint", "contype"), String.class).eq("f"))
                .and(DSL.field(DSL.name("pg_constraint", "confrelid")).eq(target))
                .fetch()) {
            PgContainerHelper.dropConstraint(su, DSL.table(DSL.name(fk.value1(), fk.value2())), fk.value3());
        }
        PgContainerHelper.dropConstraint(su, CATALOG_COLLECTIONS, "catalog_collections_pk");
    }

    private static String sqlState(Throwable t) {
        for (Throwable c = t; c != null; c = c.getCause()) {
            if (c instanceof SQLException se) {
                return se.getSQLState();
            }
        }
        return null;
    }

    @Test
    void theIndexIsPresent_uniqueAndPartialOverTheLiveRows() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            String def = indexDefinition(dsl);
            assertThat(def).as("the partial UNIQUE index exists").isNotNull();
            assertThat(def).contains("CREATE UNIQUE INDEX").contains("(tenant_id, name)").contains("WHERE");
        }
    }

    @Test
    void aSecondLiveRowWithOneLabelIsRefused() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            insert(dsl, TENANT, "twin label", "");
            assertThatThrownBy(() -> insert(dsl, TENANT, "twin label", ""))
                .isInstanceOf(DataAccessException.class)
                .satisfies(e -> assertThat(sqlState(e)).as("unique violation").isEqualTo("23505"));
            // Another tenant is independent.
            insert(dsl, TENANT + "-other", "twin label", "");
        }
    }

    @Test
    void theIndexAloneRefusesASecondLiveRow_andLetsASupersededRowShareTheLabel() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            // Until P4.1 the name is the key: drop it, so the live-label index is the only refusal (P4.1 rewrites this).
            dropNameKey(su, dsl);
            String tenant = TENANT + "-index-alone";

            // The three models differ, so only the live-label index is in play (the key is dropped, and the
            // (tenant_id, name, embedding_model) UNIQUE of vectors-030 sees three different triples).
            insert(dsl, tenant, "history label", "someone-else", "minilm-l6-v2-384");        // superseded: no live slot
            insert(dsl, tenant, "history label", "", "bge-base-en-v15-768");                  // the live row beside it
            insert(dsl, tenant, "history label", "another-successor", "voyage-context-3");   // a second superseded row

            assertThatThrownBy(() -> insert(dsl, tenant, "history label", "", "voyage-code-3"))
                .isInstanceOf(DataAccessException.class)
                .satisfies(e -> {
                    assertThat(sqlState(e)).isEqualTo("23505");
                    assertThat(e.getMessage()).as("refused by the live-label index, not the key").contains(INDEX);
                });
        }
    }
}
