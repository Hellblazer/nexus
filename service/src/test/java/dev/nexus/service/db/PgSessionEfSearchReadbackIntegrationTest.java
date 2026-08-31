/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

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
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // Load the extension so hnsw.ef_search is the REAL, range-validated
            // GUC rather than a placeholder set_config would accept blindly.
            su.createStatement().execute("CREATE EXTENSION IF NOT EXISTS vector");
        }
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
            ctx.resultQuery("SELECT '[1]'::vector").fetch();

            // Small request: the floor dominates (default 200 — no
            // NX_HNSW_EF_SEARCH in the test env, pinned by the assertion).
            PgSession.setHnswEfSearch(ctx, 10);
            assertThat(currentSetting(ctx)).isEqualTo("200");

            // Larger-than-floor request rises with nResults.
            PgSession.setHnswEfSearch(ctx, 300);
            assertThat(currentSetting(ctx)).isEqualTo("300");

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
