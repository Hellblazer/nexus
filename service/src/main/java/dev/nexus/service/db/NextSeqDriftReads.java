// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeUnit;
import java.util.function.LongSupplier;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS;

/**
 * nexus-f3yqd: the read-only form of {@code POST /v1/catalog/owners/sweep_next_seq_drift}, behind
 * {@code GET /v1/catalog/owners/next_seq_drift?limit=N}: which owners of the tenant hold a {@code next_seq} below their
 * own highest child sequence, and by how much, without flooring anyone. {@code nx doctor}'s "tumbler allocator" row
 * reads it in place of a walk of every document.
 *
 * <p><b>One computation, two callers.</b> The high-water mark is {@link CatalogRepository#highestChildSeq} (the
 * function the registration claim and the sweep use: tombstoned children included, digits-only child segments) and the
 * drift predicate is {@link #drifted}; the sweep calls both under its per-owner row lock and writes the floor, this read
 * calls both in one read transaction and writes nothing. A change to either reaches both.
 *
 * <p>Every owner is examined, deactivated ones included, as the sweep does (a deactivated owner can still receive a
 * late write with an explicit prefix). The count is exact: it covers every owner whatever {@code limit} says; {@code limit}
 * bounds the examples listed (in owner-prefix order) and sets {@code capped} when more owners drifted than were listed.
 *
 * <p>BOUNDS. One {@code tenantScope.withTenant} transaction (FORCE row-level security scopes each table; the tenant is
 * also an explicit equality), under a {@code statement_timeout} for each statement and the same bound for the call as a
 * whole (one probe per owner, so a tenant with very many owners is bounded by the deadline, not by the statement). A read
 * that outlasts either has a {@code DataAccessException} whose cause chain holds SQLSTATE
 * {@link CatalogRepository#QUERY_CANCELED}, and the route answers 503 with no part of the answer. The bound and its test
 * hook are the section-label count's ({@link CatalogRepository#FILLABLE_COUNT_TIMEOUT_MS}).
 */
public final class NextSeqDriftReads {

    /** The most examples one call lists; the route answers 400 above it. */
    public static final int MAX_LIMIT = CatalogRepository.FILLABLE_COUNT_MAX_LIMIT;

    /** The limit a call without one uses. */
    public static final int DEFAULT_LIMIT = 1_000;

    /** One drifted owner. */
    public record Drift(String owner, long nextSeq, long highestChildSeq) {}

    /** The answer: {@code drifted} is every drifted owner, {@code checked} every owner examined, {@code examples} at most {@code limit} of the drifted. */
    public record Page(int checked, int drifted, int limit, boolean capped, List<Drift> examples) {}

    private final CatalogRepository repo;
    private final LongSupplier nanos;

    public NextSeqDriftReads(CatalogRepository repo) {
        this(repo, System::nanoTime);
    }

    /** {@code nanos} is the monotonic source the call deadline reads; a test replaces it to spend the deadline deterministically. */
    public NextSeqDriftReads(CatalogRepository repo, LongSupplier nanos) {
        this.repo = repo;
        this.nanos = nanos;
    }

    /**
     * Whether an owner whose counter holds {@code nextSeq} has fallen behind its highest child. {@code next_seq} is the
     * LAST CLAIMED sequence (a claim stores {@code max(next_seq, high_water) + 1}), so equality is the healthy steady
     * state and only a counter strictly below its high-water mark is drift.
     */
    static boolean drifted(long nextSeq, long highWater) {
        return highWater > nextSeq;
    }

    /**
     * @param limit 1 to {@link #MAX_LIMIT}
     * @throws IllegalArgumentException when {@code limit} is outside that range
     */
    public Page read(String tenant, int limit) {
        if (limit < 1 || limit > MAX_LIMIT) {
            throw new IllegalArgumentException("limit must be 1.." + MAX_LIMIT);
        }
        int boundMs = Integer.getInteger(CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY, CatalogRepository.FILLABLE_COUNT_TIMEOUT_MS);
        long deadline = nanos.getAsLong() + TimeUnit.MILLISECONDS.toNanos(boundMs);
        return repo.tenantScope().withTenant(tenant, ctx -> {
            PgSession.setLocal(ctx, "statement_timeout", Integer.toString(boundMs));
            var owners = ctx.select(CATALOG_OWNERS.TUMBLER_PREFIX, CATALOG_OWNERS.NEXT_SEQ)
                .from(CATALOG_OWNERS)
                .where(CATALOG_OWNERS.TENANT_ID.eq(tenant))
                .orderBy(CATALOG_OWNERS.TUMBLER_PREFIX)
                .fetch();
            int drifted = 0;
            List<Drift> examples = new ArrayList<>();
            for (var o : owners) {
                if (nanos.getAsLong() > deadline) {
                    // The call as a whole outlasted its bound: the same SQLSTATE a cancelled statement carries, so the
                    // route answers it as it answers a statement timeout (503, no partial answer).
                    throw new org.jooq.exception.DataAccessException("next_seq drift read exceeded its time bound",
                        new java.sql.SQLException("time bound", CatalogRepository.QUERY_CANCELED));
                }
                String prefix = o.value1();
                long nextSeq = o.value2() == null ? 0L : o.value2();
                long high = CatalogRepository.highestChildSeq(ctx, tenant, prefix);
                if (!drifted(nextSeq, high)) continue;
                drifted++;
                if (examples.size() < limit) examples.add(new Drift(prefix, nextSeq, high));
            }
            return new Page(owners.size(), drifted, limit, drifted > limit, List.copyOf(examples));
        });
    }
}
