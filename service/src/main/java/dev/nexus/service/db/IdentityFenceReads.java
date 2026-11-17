// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.Collection;
import java.util.List;
import java.util.Optional;

import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_BINDINGS;
import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_RECORDS;
import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_SESSIONS;
import static dev.nexus.service.jooq.nexus.Tables.MEMORY;
import static dev.nexus.service.jooq.nexus.Tables.TUPLES;

/**
 * RDR-253 Step 2: the read-only lookups the fences make on the hot path, each one a plain SELECT under the tenant
 * stamp with no row lock. A fence runs on every write to a fenced family, so it must not call
 * {@link IdentityRepository#show} (which locks the identity row and may write a lapse row) or any route that
 * does: the identity's state, generation and lease are read here without either.
 *
 * <p>Also here, because they are the same kind of read and the handlers they serve may add call sites only: the
 * stored entry a threads write is checked against (quarantined rows included, since an upsert lands on a quarantined
 * row of the same title), the subspace of a live claim (ack, nack, renew and release name only a claim id), and
 * the one UPDATE that stamps a T2 row with the identity of the token that wrote it.
 */
public final class IdentityFenceReads {

    private static final Logger log = LoggerFactory.getLogger(IdentityFenceReads.class);

    /**
     * The identity's state, generation and lease, whether a client has recorded the holder gone, and the holder's
     * host and pid for the refusal that names it.
     */
    public record State(String state, int generation, String lease, boolean holderGone, String holderHost,
                        Integer holderPid) {
    }

    /** A stored T2 row as the threads fence reads it. */
    public record MemoryRow(long id, String project, String title, String content) {
    }

    private final TenantScope scope;

    public IdentityFenceReads(TenantScope scope) {
        this.scope = scope;
    }

    /** The current state, generation and lease of an identity, or empty when there is no such identity. */
    public Optional<State> state(String tenant, String id) {
        return scope.withTenant(tenant, ctx -> Optional.ofNullable(ctx
            .select(IDENTITY_RECORDS.STATE, IDENTITY_RECORDS.GENERATION, IDENTITY_RECORDS.LEASE,
                IDENTITY_RECORDS.GONE_AT, IDENTITY_RECORDS.HOLDER_HOST, IDENTITY_RECORDS.HOLDER_PID)
            .from(IDENTITY_RECORDS)
            .where(IDENTITY_RECORDS.TENANT_ID.eq(tenant)).and(IDENTITY_RECORDS.ID.eq(id))
            .fetchOne(r -> new State(r.value1(), r.value2(), r.value3(), r.value4() != null, r.value5(), r.value6()))));
    }

    /**
     * The session id of the identity's newest binding row under {@code generation} that names one: where the
     * {@code sender_session} stamp of an identity token comes from. The binding log appends a row with the new
     * session id under the same generation on {@code /clear}, {@code fork} and a resume, so the newest row is
     * the session the process is in now, and no token ever carries a session id that goes stale.
     */
    public Optional<String> currentSession(String tenant, String id, int generation) {
        return scope.withTenant(tenant, ctx -> Optional.ofNullable(ctx.select(IDENTITY_BINDINGS.SESSION_ID)
            .from(IDENTITY_BINDINGS)
            .where(IDENTITY_BINDINGS.TENANT_ID.eq(tenant)).and(IDENTITY_BINDINGS.IDENTITY_ID.eq(id))
            .and(IDENTITY_BINDINGS.GENERATION.eq(generation)).and(IDENTITY_BINDINGS.SESSION_ID.isNotNull())
            .orderBy(IDENTITY_BINDINGS.SEQ.desc()).limit(1).fetchOne(IDENTITY_BINDINGS.SESSION_ID)));
    }

    /** True when the session index maps {@code sessionId} to this identity at this generation. */
    public boolean sessionMapsTo(String tenant, String sessionId, String id, int generation) {
        return scope.withTenant(tenant, ctx -> ctx.fetchExists(IDENTITY_SESSIONS,
            IDENTITY_SESSIONS.TENANT_ID.eq(tenant).and(IDENTITY_SESSIONS.SESSION_ID.eq(sessionId))
                .and(IDENTITY_SESSIONS.IDENTITY_ID.eq(id)).and(IDENTITY_SESSIONS.GENERATION.eq(generation))));
    }

    /** The subspace of the live claim {@code claimId}, or empty when there is none (expired, acked, unknown). */
    public Optional<String> claimSubspace(String tenant, String claimId) {
        return scope.withTenant(tenant, ctx -> Optional.ofNullable(
            TupleRepository.liveClaimRowQuery(ctx, tenant, claimId).fetchOne(TUPLES.SUBSPACE)));
    }

    // ── T2 rows ──────────────────────────────────────────────────────────────

    /** The row at {@code (project, title)}, quarantined or not (the unique key, so at most one). */
    public Optional<MemoryRow> memoryByTitle(String tenant, String project, String title) {
        return memoryByTitles(tenant, project, List.of(title)).stream().findFirst();
    }

    /** The rows of {@code project} with these titles, quarantined or not. */
    public List<MemoryRow> memoryByTitles(String tenant, String project, Collection<String> titles) {
        if (titles.isEmpty()) {
            return List.of();
        }
        return scope.withTenant(tenant, ctx -> ctx
            .select(MEMORY.ID, MEMORY.PROJECT, MEMORY.TITLE, MEMORY.CONTENT).from(MEMORY)
            .where(MEMORY.TENANT_ID.eq(tenant)).and(MEMORY.PROJECT.eq(project)).and(MEMORY.TITLE.in(titles))
            .fetch(r -> new MemoryRow(r.value1(), r.value2(), r.value3(), r.value4())));
    }

    /** The rows with these ids, in any project, quarantined or not. */
    public List<MemoryRow> memoryByIds(String tenant, Collection<Long> ids) {
        if (ids.isEmpty()) {
            return List.of();
        }
        return scope.withTenant(tenant, ctx -> ctx
            .select(MEMORY.ID, MEMORY.PROJECT, MEMORY.TITLE, MEMORY.CONTENT).from(MEMORY)
            .where(MEMORY.TENANT_ID.eq(tenant)).and(MEMORY.ID.in(ids))
            .fetch(r -> new MemoryRow(r.value1(), r.value2(), r.value3(), r.value4())));
    }

    /**
     * Stamps a row with the identity of the token that wrote it. A label: a failure to stamp is logged and never
     * fails the write it follows (the row is already committed).
     */
    public void stampMemory(String tenant, long id, String identity) {
        try {
            scope.withTenant(tenant, ctx -> ctx.update(MEMORY).set(MEMORY.IDENTITY, identity)
                .where(MEMORY.TENANT_ID.eq(tenant)).and(MEMORY.ID.eq(id)).execute());
        } catch (RuntimeException e) {
            log.warn("event=identity_stamp_failed tenant={} memory_id={} identity={} error={}", tenant, id, identity,
                e.getMessage());
        }
    }
}
