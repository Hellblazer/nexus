// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.sql.DataSource;
import java.security.SecureRandom;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.Base64;
import java.util.Optional;

import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_TOKENS;

/**
 * RDR-253 Step 2: the identity tokens. A token names an identity and one generation of it, lives one hour, and is
 * what {@code AuthFilter} resolves from the {@code X-Nexus-Identity-Token} header so the fences can compare its
 * generation with the identity's current one.
 *
 * <p>Every mint is an INSERT, keyed by the token hash. Nothing is replaced, which is the reason this is not
 * {@code session_tokens} (see {@code service-tokens-007-identity-tokens.xml}): the token of a superseded generation
 * stays resolvable until it expires, so the engine can answer {@code 409 generation_superseded} in place of a
 * {@code 401}, and a {@code /clear} in the same process mints a token for the same generation without
 * invalidating the one the MCP server holds. The table has no row-level security (a token is resolved before a
 * tenant is stamped), so, like {@code session_tokens}, every statement here names the tenant it means where it
 * has one, and a lookup by hash alone returns the tenant it was minted for.
 */
public final class IdentityTokenStore {

    private static final Logger log = LoggerFactory.getLogger(IdentityTokenStore.class);

    /** One hour: the ceiling for a token minted under the data credential (RDR-253 Renewal, refresh, lapse and sleep). */
    public static final long TTL_SECONDS = 3600L;

    private static final SecureRandom RNG = new SecureRandom();
    private static final Base64.Encoder ENCODER = Base64.getUrlEncoder().withoutPadding();

    /** A freshly minted token: the raw secret (returned once, only its hash is stored) and when it stops working. */
    public record Issued(String rawToken, long expiresInSeconds, Instant expiresAt) {
    }

    /** What a live token names. */
    public record Principal(String tenantId, String identityId, int generation) {
    }

    private final DataSource dataSource;
    private final Clock clock;

    public IdentityTokenStore(DataSource dataSource, Clock clock) {
        this.dataSource = dataSource;
        this.clock = clock;
    }

    private DSLContext dsl() {
        return DSL.using(dataSource, SQLDialect.POSTGRES);
    }

    /**
     * Mints a token for {@code (identityId, generation)} held by the process {@code procStart} (null when the
     * binding names none). Also removes this identity's tokens that have already expired, so a long-lived seat
     * that refreshes every half hour never accumulates rows between sweeps.
     */
    public Issued issue(String tenant, String identityId, int generation, String procStart) {
        if (tenant == null || tenant.isBlank() || identityId == null || identityId.isBlank()) {
            throw new IllegalArgumentException("tenant and identity are required");
        }
        String raw = newRawToken();
        Instant now = clock.instant();
        Instant expires = now.plusSeconds(TTL_SECONDS);
        OffsetDateTime created = OffsetDateTime.ofInstant(now, ZoneOffset.UTC);
        dsl().transaction(cfg -> {
            DSLContext tx = DSL.using(cfg);
            tx.deleteFrom(IDENTITY_TOKENS)
                .where(IDENTITY_TOKENS.TENANT_ID.eq(tenant)).and(IDENTITY_TOKENS.IDENTITY_ID.eq(identityId))
                .and(IDENTITY_TOKENS.EXPIRES_AT.lt(created)).execute();
            tx.insertInto(IDENTITY_TOKENS)
                .set(IDENTITY_TOKENS.TOKEN_HASH, TokenHashing.sha256Hex(raw))
                .set(IDENTITY_TOKENS.TENANT_ID, tenant)
                .set(IDENTITY_TOKENS.IDENTITY_ID, identityId)
                .set(IDENTITY_TOKENS.GENERATION, generation)
                .set(IDENTITY_TOKENS.PROC_START, procStart)
                .set(IDENTITY_TOKENS.CREATED_AT, created)
                .set(IDENTITY_TOKENS.EXPIRES_AT, OffsetDateTime.ofInstant(expires, ZoneOffset.UTC))
                .execute();
        });
        log.info("event=identity_token_minted tenant={} identity={} generation={}", tenant, identityId, generation);
        return new Issued(raw, TTL_SECONDS, expires);
    }

    /** The principal a token hash names, or empty when it is unknown or has expired. */
    public Optional<Principal> resolve(String tokenHash) {
        if (tokenHash == null || tokenHash.isBlank()) {
            return Optional.empty();
        }
        var rec = dsl().select(IDENTITY_TOKENS.TENANT_ID, IDENTITY_TOKENS.IDENTITY_ID, IDENTITY_TOKENS.GENERATION,
                IDENTITY_TOKENS.EXPIRES_AT)
            .from(IDENTITY_TOKENS).where(IDENTITY_TOKENS.TOKEN_HASH.eq(tokenHash)).fetchOne();
        if (rec == null || !rec.get(IDENTITY_TOKENS.EXPIRES_AT).toInstant().isAfter(clock.instant())) {
            return Optional.empty();
        }
        return Optional.of(new Principal(rec.get(IDENTITY_TOKENS.TENANT_ID), rec.get(IDENTITY_TOKENS.IDENTITY_ID),
            rec.get(IDENTITY_TOKENS.GENERATION)));
    }

    /** Deletes the tenant's tokens that expired before {@code now}; the scheduled sweep calls it. Returns the count. */
    public int sweepExpired(String tenant, Instant now, Duration statementTimeout) {
        if (tenant == null || tenant.isBlank()) {
            return 0;
        }
        OffsetDateTime cutoff = OffsetDateTime.ofInstant(now, ZoneOffset.UTC);
        int deleted = dsl().transactionResult(cfg -> {
            DSLContext tx = DSL.using(cfg);
            SweepBounds.applyStatementTimeout(tx, statementTimeout);
            return tx.deleteFrom(IDENTITY_TOKENS)
                .where(IDENTITY_TOKENS.TENANT_ID.eq(tenant)).and(IDENTITY_TOKENS.EXPIRES_AT.lt(cutoff)).execute();
        });
        if (deleted > 0) {
            log.info("event=identity_token_sweep tenant={} cutoff={} deleted={}", tenant, cutoff, deleted);
        }
        return deleted;
    }

    private static String newRawToken() {
        byte[] bytes = new byte[32];
        RNG.nextBytes(bytes);
        return ENCODER.encodeToString(bytes);
    }
}
