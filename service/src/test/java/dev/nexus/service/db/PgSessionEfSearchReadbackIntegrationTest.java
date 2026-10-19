/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-4ktfm — {@link PgSession#setHnswEfSearch} against a REAL pgvector
 * Postgres: the whitelist admits {@code hnsw.ef_search}, the sized value
 * lands as the extension's actual (range-validated) GUC for the
 * transaction, and it reverts on rollback — the same {@code SET LOCAL}
 * txn-scoping contract the tenant GUC stamp relies on under the
 * transaction-mode pooler.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgSessionEfSearchReadbackIntegrationTest {

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAll() throws Exception {
        // start() hands out a clone of the migrated template, where vectors-001-1 has already created the
        // extension; the test below loads the library in its own backend, and its vector cast fails loudly
        // if the extension were ever absent.
        pg = PgContainerHelper.start();
    }

    @AfterAll
    void stopAll() {
        if (pg != null) {
            pg.stop();
        }
    }

    @Test
    void sizedValueLandsForTheTransactionAndRevertsOnRollback() throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);

            // Load vector.so in THIS backend: pgvector's GUCs are registered
            // per-backend at library load; without this the set below lands
            // on an unvalidated placeholder (measured: current_setting reads
            // "" after rollback instead of the extension default 40).
            ctx.select(DSL.val(Vector.of(new float[] {1f}), CHUNKS.EMBEDDING_384).isNotNull()).fetch();

            // Small request: the floor dominates (default 600 — no
            // NX_HNSW_EF_SEARCH in the test env, pinned by the assertion).
            PgSession.setHnswEfSearch(ctx, 10);
            assertThat(currentSetting(ctx)).isEqualTo("600");

            // Larger-than-floor request rises with nResults.
            PgSession.setHnswEfSearch(ctx, 800);
            assertThat(currentSetting(ctx)).isEqualTo("800");

            c.rollback();

            // SET LOCAL semantics: gone after the transaction — pgvector's
            // default (40) is what the next transaction sees.
            assertThat(currentSetting(ctx)).isEqualTo("40");
            c.rollback();
        }
    }

    private static String currentSetting(DSLContext ctx) {
        return ctx.select(DSL.function("current_setting", String.class,
                DSL.val("hnsw.ef_search")))
            .fetchSingle().value1();
    }
}
