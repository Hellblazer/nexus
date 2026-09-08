package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TokenHashing;
import dev.nexus.service.db.TokenStore;
import liquibase.Liquibase;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Clock;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;

import static dev.nexus.service.jooq.nexus.Tables.SESSION_TOKENS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-t23zk — {@link TokenStore#sweepExpiredSessions(String, Instant)}, the
 * crash-safety backstop for {@code session_tokens} rows a dead minting process
 * (crashed MCP, killed dispatch, machine reboot) never got to close. {@code
 * idx_session_tokens_expires} (service-tokens-001-baseline.xml:117-118) was created
 * with the comment "TTL sweep: DELETE FROM nexus.session_tokens WHERE expires_at <
 * now()" — this bead is the first Java caller of exactly that query; NO DDL.
 *
 * <p>Hermetic: Testcontainers Postgres + real Liquibase chain, {@link TokenStore}
 * exercised directly (no HTTP, no scheduler) — mirrors {@code
 * TokenScopeResolutionTest}'s setup shape. {@code session_tokens} carries no RLS
 * (class javadoc on {@link TokenStore}), so the connecting role is the plain
 * Postgres superuser, same as every other direct {@link TokenStore} test in this
 * package.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TokenStoreSessionSweepTest {

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    TokenStore store;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        var config = new HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(pg.getUsername());
        config.setPassword(pg.getPassword());
        config.setMaximumPoolSize(4);
        ds = new HikariDataSource(config);
        store = new TokenStore(ds, Clock.systemUTC());
    }

    @AfterAll
    void stopAll() {
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    private void insertSessionToken(String tenant, String sessionId, Instant expiresAt) throws Exception {
        try (Connection su = pg.createConnection("")) {
            String hash = TokenHashing.sha256Hex(tenant + ":" + sessionId + ":" + expiresAt);
            DSL.using(su, SQLDialect.POSTGRES)
                .insertInto(SESSION_TOKENS)
                .columns(SESSION_TOKENS.SESSION_TOKEN_HASH, SESSION_TOKENS.TENANT_ID,
                    SESSION_TOKENS.SESSION_ID, SESSION_TOKENS.EXPIRES_AT)
                .values(hash, tenant, sessionId, OffsetDateTime.ofInstant(expiresAt, ZoneOffset.UTC))
                .execute();
        }
    }

    private boolean sessionTokenExists(String tenant, String sessionId) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES)
                .fetchExists(DSL.selectOne().from(SESSION_TOKENS)
                    .where(SESSION_TOKENS.TENANT_ID.eq(tenant), SESSION_TOKENS.SESSION_ID.eq(sessionId)));
        }
    }

    @Test
    void sweepExpiredSessions_deletesExpiredRow_survivesUnexpiredRow() throws Exception {
        String tenant = "sweep-sessions-" + System.nanoTime();
        Instant now = Instant.now();

        insertSessionToken(tenant, "expired-session", now.minusSeconds(3600));
        insertSessionToken(tenant, "live-session", now.plusSeconds(3600));

        int deleted = store.sweepExpiredSessions(tenant, now);

        assertThat(deleted)
            .as("exactly the one already-expired row must be swept")
            .isEqualTo(1);
        assertThat(sessionTokenExists(tenant, "expired-session"))
            .as("the expired row must be gone")
            .isFalse();
        assertThat(sessionTokenExists(tenant, "live-session"))
            .as("the unexpired row must survive untouched")
            .isTrue();
    }

    @Test
    void sweepExpiredSessions_doesNotAffectOtherTenants() throws Exception {
        String tenantA = "sweep-sessions-a-" + System.nanoTime();
        String tenantB = "sweep-sessions-b-" + System.nanoTime();
        Instant now = Instant.now();

        insertSessionToken(tenantA, "a-expired", now.minusSeconds(3600));
        insertSessionToken(tenantB, "b-expired", now.minusSeconds(3600));

        int deleted = store.sweepExpiredSessions(tenantA, now);

        assertThat(deleted).isEqualTo(1);
        assertThat(sessionTokenExists(tenantA, "a-expired"))
            .as("tenant A's expired row must be swept")
            .isFalse();
        assertThat(sessionTokenExists(tenantB, "b-expired"))
            .as("tenant B's expired row must survive -- a per-tenant sweep call must never "
                + "reach across tenant boundaries even though session_tokens carries no RLS")
            .isTrue();
    }

    @Test
    void sweepExpiredSessions_noExpiredRows_returnsZero_leavesTableUntouched() throws Exception {
        String tenant = "sweep-sessions-empty-" + System.nanoTime();
        Instant now = Instant.now();
        insertSessionToken(tenant, "still-live", now.plusSeconds(3600));

        int deleted = store.sweepExpiredSessions(tenant, now);

        assertThat(deleted).isEqualTo(0);
        assertThat(sessionTokenExists(tenant, "still-live")).isTrue();
    }

    @Test
    void sweepExpiredSessions_blankOrNullTenant_isNoOp() {
        assertThat(store.sweepExpiredSessions(null, Instant.now())).isEqualTo(0);
        assertThat(store.sweepExpiredSessions("", Instant.now())).isEqualTo(0);
        assertThat(store.sweepExpiredSessions("   ", Instant.now())).isEqualTo(0);
    }
}
