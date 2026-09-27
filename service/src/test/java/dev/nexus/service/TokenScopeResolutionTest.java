package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TokenHashing;
import dev.nexus.service.db.TokenStore;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.testcontainers.containers.PostgreSQLContainer;
import liquibase.Liquibase;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.time.Clock;
import java.util.Optional;

import static dev.nexus.service.jooq.nexus.Tables.SERVICE_TOKENS;
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

    private void insertRow(String rawToken, String tenant, String label, String scope)
            throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(
                DSL.using(su, SQLDialect.POSTGRES), rawToken, tenant, label, scope, null, null);
        }
    }

    private String scopeOfHash(String tokenHash) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var rows = DSL.using(su, SQLDialect.POSTGRES)
                .select(SERVICE_TOKENS.SCOPE).from(SERVICE_TOKENS)
                .where(SERVICE_TOKENS.TOKEN_HASH.eq(tokenHash))
                .fetch(SERVICE_TOKENS.SCOPE);
            assertThat(rows).as("row must exist for hash " + tokenHash).hasSize(1);
            return rows.get(0);
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
    void rotate_mixedScopes_withNoExplicitScope_refuses() throws Exception {
        // nexus-r3ur5 critique: silently collapsing a mixed-scope tenant to the
        // oldest row's scope could grace-expire a narrow-scope credential (e.g.
        // board-ci) and replace it with a full tenant-scope token. Refuse instead,
        // naming both scopes, and touch NOTHING — no expiry, no new token.
        TokenStore.IssuedToken first =
            store.issueToken("rotate-mixed-tenant", "original", null, TokenStore.SCOPE_MINT);
        TokenStore.IssuedToken second =
            store.issueToken("rotate-mixed-tenant", "later", null, TokenStore.SCOPE_TENANT);
        assertThatThrownBy(() -> store.rotateTokens("rotate-mixed-tenant", 60))
            .isInstanceOf(TokenStore.MixedScopeRotationRefused.class)
            .satisfies(e -> assertThat(((TokenStore.MixedScopeRotationRefused) e).scopes())
                .containsExactlyInAnyOrder("mint", "tenant"));
        // Nothing expired, nothing minted.
        assertThat(scopeOfHash(first.tokenHash())).isEqualTo("mint");
        assertThat(scopeOfHash(second.tokenHash())).isEqualTo("tenant");
        try (Connection su = pg.createConnection("")) {
            var stillLive = DSL.using(su, SQLDialect.POSTGRES)
                .select(SERVICE_TOKENS.EXPIRES_AT)
                .from(SERVICE_TOKENS)
                .where(SERVICE_TOKENS.TOKEN_HASH.in(first.tokenHash(), second.tokenHash()))
                .fetch(SERVICE_TOKENS.EXPIRES_AT);
            assertThat(stillLive).as("a refused rotate must not grace-expire anything")
                .allMatch(exp -> exp == null);
        }
    }

    @Test
    void rotate_withExplicitScope_rotatesOnlyThatScope() throws Exception {
        // nexus-r3ur5: a tenant holding BOTH a tenant-scope and a board-ci-scope
        // token can rotate one without touching the other.
        TokenStore.IssuedToken tenantTok =
            store.issueToken("rotate-scoped-tenant", "t", null, TokenStore.SCOPE_TENANT);
        TokenStore.IssuedToken boardCiTok =
            store.issueToken("rotate-scoped-tenant", "b", null, TokenStore.SCOPE_BOARD_CI);

        TokenStore.RotationResult rotated =
            store.rotateTokens("rotate-scoped-tenant", 60, TokenStore.SCOPE_BOARD_CI);

        assertThat(rotated.scope()).isEqualTo("board-ci");
        assertThat(rotated.expiredHashes()).containsExactly(boardCiTok.tokenHash());
        assertThat(scopeOfHash(rotated.issued().tokenHash())).isEqualTo("board-ci");
        // The tenant-scope token is untouched: still live, no expiry set.
        try (Connection su = pg.createConnection("")) {
            var row = DSL.using(su, SQLDialect.POSTGRES)
                .select(SERVICE_TOKENS.EXPIRES_AT, SERVICE_TOKENS.REVOKED_AT)
                .from(SERVICE_TOKENS)
                .where(SERVICE_TOKENS.TOKEN_HASH.eq(tenantTok.tokenHash()))
                .fetchOne();
            assertThat(row).isNotNull();
            assertThat(row.value1()).as("tenant-scope token must not be grace-expired").isNull();
            assertThat(row.value2()).as("tenant-scope token must not be revoked").isNull();
        }
    }

    @Test
    void rotate_withExplicitScope_noLiveRowsOfThatScope_mintsFresh() throws Exception {
        // A scope the tenant holds no live rows of yet: nothing to expire, but the
        // rotation still mints a fresh token of the requested scope.
        TokenStore.RotationResult rotated =
            store.rotateTokens("rotate-scope-cold-start", 60, TokenStore.SCOPE_BOARD_CI);
        assertThat(rotated.expiredHashes()).isEmpty();
        assertThat(rotated.scope()).isEqualTo("board-ci");
        assertThat(scopeOfHash(rotated.issued().tokenHash())).isEqualTo("board-ci");
    }

    @Test
    void rotate_rejectsNonRotatableScope() {
        // 'data' and 'root' are never minted through rotation, same vocabulary as
        // the handler's issuable-scope gate.
        assertThatThrownBy(() -> store.rotateTokens("rotate-bad-scope", 60, TokenStore.SCOPE_DATA))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("scope");
        assertThatThrownBy(() -> store.rotateTokens("rotate-bad-scope", 60, TokenStore.SCOPE_ROOT))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("scope");
    }

    @Test
    void rotate_singleScopeTenant_unaffectedByExplicitScopeSupport() throws Exception {
        // Existing single-scope behavior stays green with no 'scope' argument at all
        // (the two-arg overload), even though rotation now supports one.
        TokenStore.IssuedToken original =
            store.issueToken("rotate-single-scope-tenant", "edge-cred", null, TokenStore.SCOPE_MINT);
        TokenStore.RotationResult rotated = store.rotateTokens("rotate-single-scope-tenant", 60);
        assertThat(rotated.scope()).isEqualTo("mint");
        assertThat(rotated.expiredHashes()).containsExactly(original.tokenHash());
        assertThat(scopeOfHash(rotated.issued().tokenHash())).isEqualTo("mint");
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
