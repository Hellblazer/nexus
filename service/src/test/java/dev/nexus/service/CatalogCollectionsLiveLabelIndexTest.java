// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.DSLContext;
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

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.6 (nexus-p2m1x.26): a label names exactly one LIVE collection per tenant, held by the partial UNIQUE
 * index {@code idx_catalog_collections_live_label} on {@code nexus.catalog_collections (tenant_id, name)} over the
 * rows that are not superseded (catalog-052).
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
        dsl.execute("INSERT INTO nexus.catalog_collections (tenant_id, name, content_type, owner_id, embedding_model, "
            + "model_version, superseded_by) VALUES (?, ?, 'knowledge', 'label-notes', 'bge-base-en-v15-768', 'v1', ?)",
            tenant, label, supersededBy);
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
            String def = (String) dsl.fetchValue(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = 'nexus' AND tablename = 'catalog_collections' "
                + "AND indexname = ?", INDEX);
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
            // From P4.1 the name is no longer the key and this is a no-op; until then it removes the other refusal.
            dsl.execute("ALTER TABLE nexus.catalog_collections DROP CONSTRAINT IF EXISTS catalog_collections_pk");
            String tenant = TENANT + "-index-alone";

            insert(dsl, tenant, "history label", "someone-else");   // superseded: holds no live slot
            insert(dsl, tenant, "history label", "");                // the live row beside it
            insert(dsl, tenant, "history label", "another-successor"); // a second superseded row: still allowed

            assertThatThrownBy(() -> insert(dsl, tenant, "history label", ""))
                .isInstanceOf(DataAccessException.class)
                .satisfies(e -> {
                    assertThat(sqlState(e)).isEqualTo("23505");
                    assertThat(e.getMessage()).as("refused by the live-label index, not the key").contains(INDEX);
                });
        }
    }
}
