// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Clock;
import java.util.UUID;

import static dev.nexus.service.jooq.nexus.Tables.SERVICE_TOKENS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-0bl77 (rtg20 phase A, fix round, critic S2): {@link TokenCache#invalidateTenant} against a resolve that
 * is already in flight. The race the test pins: a request reads a tenant's token row from the store, the reset route
 * then deletes the row and evicts the tenant, and only then does the request put its result in the cache. Without a
 * guard that put stores a positive for a token that no longer exists, and the dead test's bearer keeps
 * authenticating for the cache TTL (30 s), which is exactly what the eviction exists to prevent.
 *
 * <p>Deterministic: {@code testOnlyAfterStoreRead} runs the delete and the eviction between the store read and the
 * cache put, so no thread timing is involved.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TokenCacheTenantEpochTest {

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TokenStore store;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        store = new TokenStore(svcDs, Clock.systemUTC());
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static String tenant(String prefix) {
        return prefix + "-" + UUID.randomUUID().toString().substring(0, 8);
    }

    private void deleteRows(String tenant) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).deleteFrom(SERVICE_TOKENS)
                .where(SERVICE_TOKENS.TENANT_ID.eq(tenant)).execute();
        }
    }

    @Test
    void aResolveThatReadBeforeTheResetDoesNotCacheItsPositiveAfterTheEviction() throws Exception {
        String x = tenant("epoch-x");
        var cache = new TokenCache(store, Clock.systemUTC());
        String hash = store.issueToken(x, "epoch-test", null).tokenHash();

        cache.testOnlyAfterStoreRead = () -> {
            try {
                deleteRows(x);                 // the harness's DELETE ...
            } catch (Exception e) {
                throw new RuntimeException(e);
            }
            cache.invalidateTenant(x);         // ... and the reset route, both between the read and the put
        };
        var inFlight = cache.resolve(hash);
        cache.testOnlyAfterStoreRead = null;

        assertThat(inFlight).as("the request that was already in flight is answered from what it read").isPresent();
        assertThat(cache.size()).as("but its positive was not kept").isZero();
        assertThat(cache.resolve(hash)).as("so the dead test's bearer is refused on its next request").isEmpty();
    }

    @Test
    void aResolveWithNoInvalidationInFlightStillCaches() throws Exception {
        String x = tenant("epoch-plain");
        var cache = new TokenCache(store, Clock.systemUTC());
        String hash = store.issueToken(x, "epoch-test", null).tokenHash();

        assertThat(cache.resolve(hash)).isPresent();

        assertThat(cache.size()).as("the guard costs nothing when no tenant was invalidated").isEqualTo(1);
    }

    @Test
    void anInvalidationOfAnotherTenantInFlightSkipsOneFillAndTheNextRequestFillsIt() throws Exception {
        String x = tenant("epoch-a");
        String y = tenant("epoch-b");
        var cache = new TokenCache(store, Clock.systemUTC());
        String hash = store.issueToken(x, "epoch-test", null).tokenHash();

        // The tenant is only known after the store read, so the guard is one counter for all tenants: the fill is
        // skipped (conservative), the answer is unchanged, and the next request caches it.
        cache.testOnlyAfterStoreRead = () -> cache.invalidateTenant(y);
        assertThat(cache.resolve(hash)).isPresent();
        cache.testOnlyAfterStoreRead = null;
        assertThat(cache.size()).isZero();

        assertThat(cache.resolve(hash)).isPresent();
        assertThat(cache.size()).isEqualTo(1);
    }
}
