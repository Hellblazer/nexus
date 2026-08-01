// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TokenHashing;
import dev.nexus.service.db.TokenStore;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.ResultSet;
import java.time.Clock;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Rotating {@code NX_SERVICE_TOKEN} must not take the service down (nexus-kjjab).
 *
 * <p><strong>The defect.</strong> {@code ensureBootstrapToken} arbitrated
 * {@code ON CONFLICT (token_hash) DO NOTHING} — the PK. But the table also carries
 * {@code idx_service_tokens_single_root}, a partial unique index on {@code (label)
 * WHERE label = 'bootstrap-legacy-token'}. A rotated token has a NEW hash and the SAME
 * label, so the arbiter missed it entirely and the label index fired as an unhandled
 * 23505 — from a call site outside any try in {@code Main}, on the auth bootstrap path.
 * Boot aborted with a bare stack trace and HTTP never bound. Recurring, because the env
 * var stays rotated; and not self-healing, because the index predicate carries no
 * {@code revoked_at} term, so revoking the incumbent does not free the slot.
 *
 * <p><strong>Why a dedicated container.</strong> The root slot is GLOBALLY unique — the
 * index has no {@code tenant_id} — so exactly one root row can exist per database.
 * Sharing {@code TokenScopeResolutionTest}'s container would couple these assertions to
 * whichever test seeded the slot first. Each test here starts from a truncated table so
 * the arrangement under test is the only thing in it.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class BootstrapTokenRotationTest {

    private static final String ROOT_LABEL = TokenStore.ROOT_TOKEN_LABEL;
    private static final String TENANT = "default";

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    TokenStore store;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN "
                + "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN "
                + "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; "
                + "  END IF; "
                + "END $$");
        }
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db).update(new Contexts());
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

    @BeforeEach
    void clearTokens() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("TRUNCATE nexus.service_tokens");
        }
    }

    /** THE REGRESSION. Pre-fix this threw an unhandled 23505 and took the boot down. */
    @Test
    void rotatingTheProvisionedToken_replacesTheRootRowInsteadOfThrowing() throws Exception {
        store.ensureBootstrapToken("root-token-v1", TENANT);
        assertThat(rootHash()).isEqualTo(TokenHashing.sha256Hex("root-token-v1"));

        assertThatCode(() -> store.ensureBootstrapToken("root-token-v2", TENANT))
            .as("a rotated NX_SERVICE_TOKEN must not abort startup")
            .doesNotThrowAnyException();

        // Still exactly one root row — the single-root invariant the index exists for.
        assertThat(rootRowCount()).isEqualTo(1);
        assertThat(rootHash())
            .as("the NEW token is the root token after rotation")
            .isEqualTo(TokenHashing.sha256Hex("root-token-v2"));
    }

    /**
     * The security half, and the reason rotation REPLACES rather than co-exists: the old
     * credential must stop authenticating. A rotation that left the previous hash valid
     * would mean a compromised token still works after the operator "rotated" it —
     * silently, which is worse than the crash this replaced.
     */
    @Test
    void rotating_invalidatesThePreviousToken() throws Exception {
        store.ensureBootstrapToken("root-old", TENANT);
        String oldHash = TokenHashing.sha256Hex("root-old");
        assertThat(store.lookupServiceToken(oldHash)).isPresent();

        store.ensureBootstrapToken("root-new", TENANT);

        assertThat(store.lookupServiceToken(oldHash))
            .as("the rotated-away token must no longer authenticate")
            .isEmpty();
        assertThat(store.lookupServiceToken(TokenHashing.sha256Hex("root-new")))
            .as("the new token must authenticate")
            .isPresent();
    }

    /** Scope survives rotation — a root row that came back as 'tenant' would silently demote. */
    @Test
    void rotating_keepsRootScope() throws Exception {
        store.ensureBootstrapToken("scoped-v1", TENANT);
        store.ensureBootstrapToken("scoped-v2", TENANT);

        var t = store.lookupServiceToken(TokenHashing.sha256Hex("scoped-v2"));
        assertThat(t).isPresent();
        assertThat(t.get().isRoot())
            .as("the rotated root token must still be root-scoped")
            .isTrue();
    }

    /** NON-VACUITY: the ordinary re-seed of an UNCHANGED token must stay a silent no-op. */
    @Test
    void reseedingTheSameToken_isIdempotent() throws Exception {
        store.ensureBootstrapToken("same-token", TENANT);
        String before = rootHash();

        assertThatCode(() -> store.ensureBootstrapToken("same-token", TENANT))
            .doesNotThrowAnyException();

        assertThat(rootRowCount()).isEqualTo(1);
        assertThat(rootHash()).isEqualTo(before);
    }

    /**
     * A REVOKED incumbent holds the slot, because the index predicate has no
     * {@code revoked_at} term. Both silent options are wrong — resurrecting overrides a
     * deliberate revocation, rotating onto it mints a root token that authenticates
     * nothing — so this refuses, and the message must name the remedy.
     */
    @Test
    void revokedIncumbent_refusesLoudlyInsteadOfResurrectingOrMintingADeadToken() throws Exception {
        store.ensureBootstrapToken("revoked-root", TENANT);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "UPDATE nexus.service_tokens SET revoked_at = now() WHERE label = '" + ROOT_LABEL + "'");
        }

        assertThatThrownBy(() -> store.ensureBootstrapToken("brand-new-root", TENANT))
            .isInstanceOf(TokenStore.BootstrapTokenConflict.class)
            .hasMessageContaining("REVOKED");

        // The refusal must not have half-applied.
        assertThat(rootHash())
            .as("a refused seed changes nothing")
            .isEqualTo(TokenHashing.sha256Hex("revoked-root"));
    }

    /** A blank/absent NX_SERVICE_TOKEN stays a no-op — it must not create a slot. */
    @Test
    void blankToken_isANoOp() throws Exception {
        store.ensureBootstrapToken("", TENANT);
        store.ensureBootstrapToken(null, TENANT);
        assertThat(rootRowCount()).isZero();
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private String rootHash() throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT token_hash FROM nexus.service_tokens WHERE label = '" + ROOT_LABEL + "'");
            return rs.next() ? rs.getString(1) : null;
        }
    }

    private int rootRowCount() throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT count(*) FROM nexus.service_tokens WHERE label = '" + ROOT_LABEL + "'");
            rs.next();
            return rs.getInt(1);
        }
    }
}
