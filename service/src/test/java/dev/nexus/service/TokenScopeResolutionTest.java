package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TokenHashing;
import dev.nexus.service.db.TokenStore;
import org.testcontainers.containers.PostgreSQLContainer;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.sql.ResultSet;
import java.time.Clock;
import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-868dq Phase 2 — scope resolution through {@link TokenStore}.
 *
 * <p>The privilege model moves from a single label-derived {@code isRoot} bit to a
 * server-assigned {@code scope} column ({@code root|tenant|mint|data}). The
 * load-bearing property pinned here: PRIVILEGE READS FROM SCOPE, NOT LABEL —
 * labels are client-supplied on {@code /v1/service-tokens/issue}, so a
 * label-derived privilege would be a self-escalation footgun (the reason the
 * design rejected extending the nexus-e4130 label-marker idiom).
 *
 * <p>Hermetic: Testcontainers Postgres + real Liquibase chain, {@link TokenStore}
 * exercised directly (no HTTP). EXACT assertions throughout.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TokenScopeResolutionTest {

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

    private void insertRow(String rawToken, String tenant, String label, String scope)
            throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label, scope) "
                + "VALUES ('" + TokenHashing.sha256Hex(rawToken) + "', '" + tenant
                + "', '" + label + "', '" + scope + "')");
        }
    }

    private String scopeOfHash(String tokenHash) throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT scope FROM nexus.service_tokens WHERE token_hash = '" + tokenHash + "'");
            assertThat(rs.next()).as("row must exist for hash " + tokenHash).isTrue();
            return rs.getString("scope");
        }
    }

    // ── lookup carries scope ─────────────────────────────────────────────────

    @Test
    void lookup_returnsRowScope() throws Exception {
        insertRow("scope-lookup-mint", "edge-tenant", "edge-cred", "mint");
        Optional<TokenStore.ServiceToken> t =
            store.lookupServiceToken(TokenHashing.sha256Hex("scope-lookup-mint"));
        assertThat(t).isPresent();
        assertThat(t.get().scope()).isEqualTo("mint");
        assertThat(t.get().isRoot()).isFalse();
        assertThat(t.get().tenantId()).isEqualTo("edge-tenant");
    }

    // ── isRoot derives from SCOPE, not label ─────────────────────────────────

    @Test
    void isRoot_derivesFromScope_notLabel() throws Exception {
        // An ordinary label with scope='root' IS root (scope is the authority)...
        insertRow("scope-root-ordinary-label", "default", "ordinary", "root");
        Optional<TokenStore.ServiceToken> rootByScope =
            store.lookupServiceToken(TokenHashing.sha256Hex("scope-root-ordinary-label"));
        assertThat(rootByScope).isPresent();
        assertThat(rootByScope.get().isRoot()).isTrue();
        assertThat(rootByScope.get().scope()).isEqualTo("root");

        // ...and a crafted root-LOOKING label with scope='tenant' is NOT root
        // (label grants nothing; scope is server-assigned). Uses a near-miss
        // label since the exact root label is pinned unique by service-tokens-002.
        insertRow("scope-tenant-crafted-label", "attacker", "bootstrap-legacy-token2", "tenant");
        Optional<TokenStore.ServiceToken> craftedLabel =
            store.lookupServiceToken(TokenHashing.sha256Hex("scope-tenant-crafted-label"));
        assertThat(craftedLabel).isPresent();
        assertThat(craftedLabel.get().isRoot()).isFalse();
        assertThat(craftedLabel.get().scope()).isEqualTo("tenant");
    }

    // ── issueToken: scoped overload ──────────────────────────────────────────

    @Test
    void issueToken_withScope_persistsScope() throws Exception {
        TokenStore.IssuedToken issued =
            store.issueToken("edge-tenant", "edge-mint-cred", null, TokenStore.SCOPE_MINT);
        assertThat(scopeOfHash(issued.tokenHash())).isEqualTo("mint");
    }

    @Test
    void issueToken_rejectsUnknownScope() {
        assertThatThrownBy(() ->
                store.issueToken("edge-tenant", "lbl", null, "bogus"))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("scope");
    }

    @Test
    void issueToken_rejectsDataScopeWithoutTtl() {
        // Gate-A critique: RDR-005 pin iii (bulk revoke deferred to v2) rests on
        // every data token draining by TTL — a permanent data token must be
        // unmintable at the store boundary.
        assertThatThrownBy(() ->
                store.issueToken("acme", "data-token", null, TokenStore.SCOPE_DATA))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("ttl");
    }

    @Test
    void issueToken_rejectsRootScope() {
        // Gate-A review: privilege keys on scope, but the single-root DB invariant
        // keys on the LABEL — a scope='root' row under an ordinary label would be
        // a SECOND operator credential outside every label-keyed lockout
        // (revocable, enumerable, rotate-swept). Root is seeded exclusively by
        // ensureBootstrapToken.
        assertThatThrownBy(() ->
                store.issueToken("edge-tenant", "sneaky", null, TokenStore.SCOPE_ROOT))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("root");
    }

    @Test
    void issueToken_threeArgOverload_defaultsToTenantScope() throws Exception {
        TokenStore.IssuedToken issued = store.issueToken("legacy-tenant", "legacy", null);
        assertThat(scopeOfHash(issued.tokenHash())).isEqualTo("tenant");
    }

    // ── ensureBootstrapToken stamps scope='root' ─────────────────────────────

    @Test
    void ensureBootstrapToken_setsRootScope() throws Exception {
        store.ensureBootstrapToken("scope-bootstrap-raw", "default");
        String hash = TokenHashing.sha256Hex("scope-bootstrap-raw");
        assertThat(scopeOfHash(hash)).isEqualTo("root");
        Optional<TokenStore.ServiceToken> t = store.lookupServiceToken(hash);
        assertThat(t).isPresent();
        assertThat(t.get().isRoot()).isTrue();
    }

    // ── rotateTokens preserves scope (Task 2.5 — the found gap) ──────────────

    @Test
    void rotate_preservesMintScope() throws Exception {
        // The tenant's only live token is mint-scoped; a rotation must issue a
        // replacement that is ALSO mint-scoped — the schema default 'tenant'
        // would silently strip the mint privilege from the rotated credential.
        TokenStore.IssuedToken original =
            store.issueToken("rotate-mint-tenant", "edge-cred", null, TokenStore.SCOPE_MINT);
        TokenStore.RotationResult rotated = store.rotateTokens("rotate-mint-tenant", 60);
        assertThat(rotated.expiredHashes()).containsExactly(original.tokenHash());
        assertThat(scopeOfHash(rotated.issued().tokenHash())).isEqualTo("mint");
    }

    @Test
    void rotate_withNoLiveTokens_defaultsToTenantScope() throws Exception {
        TokenStore.RotationResult rotated = store.rotateTokens("rotate-empty-tenant", 60);
        assertThat(rotated.expiredHashes()).isEmpty();
        assertThat(scopeOfHash(rotated.issued().tokenHash())).isEqualTo("tenant");
    }

    @Test
    void rotate_mixedScopes_carriesOldestDeterministically() throws Exception {
        // Gate-A review: a tenant holding live tokens of DIFFERENT scopes must
        // rotate deterministically — the replacement carries the OLDEST live
        // row's scope (the tenant's original credential), never an arbitrary one.
        TokenStore.IssuedToken first =
            store.issueToken("rotate-mixed-tenant", "original", null, TokenStore.SCOPE_MINT);
        // Ensure a strictly later created_at for the second row.
        Thread.sleep(5);
        TokenStore.IssuedToken second =
            store.issueToken("rotate-mixed-tenant", "later", null, TokenStore.SCOPE_TENANT);
        TokenStore.RotationResult rotated = store.rotateTokens("rotate-mixed-tenant", 60);
        assertThat(rotated.expiredHashes())
            .containsExactlyInAnyOrder(first.tokenHash(), second.tokenHash());
        assertThat(scopeOfHash(rotated.issued().tokenHash()))
            .as("mixed-scope rotate carries the OLDEST live row's scope")
            .isEqualTo("mint");
    }

    // ── listTokens carries scope ─────────────────────────────────────────────

    @Test
    void listTokens_carriesScope() throws Exception {
        store.issueToken("list-scope-tenant", "lbl-a", null, TokenStore.SCOPE_MINT);
        var infos = store.listTokens("list-scope-tenant");
        assertThat(infos).hasSize(1);
        assertThat(infos.get(0).scope()).isEqualTo("mint");
    }
}
