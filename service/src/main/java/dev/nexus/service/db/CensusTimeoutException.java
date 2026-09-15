// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * RDR-205-family typed error (bead nexus-xapt8, fix round, critique finding
 * 10): {@code subspace_list}'s own request-path {@code statement_timeout}
 * ({@code NX_TUPLE_SUBSPACE_LIST_TIMEOUT_SECONDS}, applied by {@link
 * TupleRepository#subspaceListPage}) fired -- the census query genuinely ran
 * too long against the tenant's current row count, not a connectivity or
 * server-availability problem. Postgres reports this as SQLState {@code
 * 57014} ({@code query_canceled}); {@link TupleRepository} recognises it and
 * raises this typed error instead of letting the raw {@link
 * java.sql.SQLException} fall through to {@code TupleHandler}'s generic
 * 500 ladder, which a caller (the {@code tuples.oldest_unclaimed} doctor
 * row in particular) could otherwise misdiagnose as "engine unreachable" --
 * the engine is fully up; this ONE statement ran past its own budget.
 *
 * <p>503, not 500: the same "retryable, not the caller's fault" posture
 * {@link HttpUtil#sendTypedDbError} already uses for HikariCP pool
 * exhaustion -- a narrower {@code prefix}/{@code limit}, or a retry once
 * load subsides, can succeed where the unbounded scan timed out.
 */
public final class CensusTimeoutException extends TupleException {

    private final long timeoutSeconds;

    public CensusTimeoutException(long timeoutSeconds) {
        super("CensusTimeout", 503,
                "subspace_list census exceeded its " + timeoutSeconds
                        + "s statement_timeout (NX_TUPLE_SUBSPACE_LIST_TIMEOUT_SECONDS) -- "
                        + "narrow with prefix/limit, or retry");
        this.timeoutSeconds = timeoutSeconds;
    }

    /** The configured budget ({@code NX_TUPLE_SUBSPACE_LIST_TIMEOUT_SECONDS}) that fired. */
    public long timeoutSeconds() {
        return timeoutSeconds;
    }
}
